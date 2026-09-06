"""Bounded private-pipe transport shared by resident inference backends.

Backend subclasses own artifact, device, memory and result validation. This
owner alone handles pipe bounds, cancellation and verified process release.
"""
from __future__ import annotations

import json
import math
import os
import queue
import subprocess
import threading
import time

class WorkerProtocolError(RuntimeError):
    """The child did not fulfil the bounded resident-worker contract."""


class WorkerCancelled(RuntimeError):
    """Uncommitted inference was cancelled and the owned child terminated."""


class WorkerAllocationFailure(MemoryError):
    """Confirmed model/chunk-processing allocation failure, not corrupt media."""

    def __init__(self, phase):
        if phase not in ('load', 'transcribe'):
            raise ValueError('Unknown allocation operation')
        super().__init__(f'Subtitle worker could not allocate memory during {phase}')
        self.phase = phase
        self.worker = None  # Bound by the owning cohort, not trusted child text.


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkerProtocolError("Worker sent duplicate JSON keys")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise WorkerProtocolError("Worker sent a non-finite JSON number")


def _seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("Worker timeout must be finite and positive")
    return float(value)


class ResidentPipeWorker:
    def __init__(self, *, max_result_bytes):
        if type(max_result_bytes) is not int or not 0 < max_result_bytes <= 8 * 1024 * 1024:
            raise ValueError("Resident result limit must be bounded")
        self._maximum_result_bytes = max_result_bytes
        self._process = None
        self._limits = None
        self._attempted_load = False
        self._loaded = False
        self._released = False
        self._request = 0
        self._busy = threading.Lock()
        self._stop = threading.Event()
        self._fault = threading.Event()
        self._incoming = queue.Queue(maxsize=4)
        self._outgoing = queue.Queue(maxsize=1)
        self._threads = []
        self._stderr_tail = bytearray()
        self._stderr_lock = threading.Lock()

    def _spawn(self, command, *, establish_limits, env=None, cwd=None):
        options = ({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt"
                   else {"start_new_session": True})
        self._process = subprocess.Popen(list(command), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
            env=env, cwd=cwd, **options)
        self._limits = establish_limits(self._process)
        for target in (self._read_stdout, self._read_stderr, self._write_stdin):
            thread = threading.Thread(target=target, daemon=True, name="subgen-worker-pipe")
            self._threads.append(thread)
            thread.start()

    def _accept_memory(self, packet):
        raise NotImplementedError("Backend must validate its own memory observations")

    def _raise_remote_error(self, packet):
        # Backends can recognize a strictly validated typed control packet.
        # Unknown/native failures never inject arbitrary child text into logs.
        raise WorkerProtocolError("Subtitle worker reported a processing failure")

    @property
    def pid(self):
        return None if self._process is None else self._process.pid

    @property
    def model_is_loaded(self):
        # Unexpected child exit is not a successful resident state.
        return self._loaded and self._process.poll() is None

    @property
    def release_confirmed(self):
        return self._released

    def _put(self, value):
        while not self._stop.is_set():
            try:
                self._incoming.put(value, timeout=.05)
                return
            except queue.Full:
                pass

    def _read_stdout(self):
        try:
            pending = bytearray()
            scanned = 0
            while not self._stop.is_set():
                data = self._process.stdout.read(4096)
                if not data:
                    if pending:
                        self._fault.set()
                    self._put(None)
                    return
                pending.extend(data)
                while (end := pending.find(b"\n", scanned)) >= 0:
                    if end > self._maximum_result_bytes:
                        self._fault.set()
                        return
                    self._put(bytes(pending[:end]))
                    del pending[:end + 1]
                    scanned = 0
                if len(pending) > self._maximum_result_bytes:
                    self._fault.set()
                    return
                scanned = len(pending)
        except (OSError, ValueError):
            self._fault.set()

    def _read_stderr(self):
        try:
            while not self._stop.is_set():
                data = self._process.stderr.read(4096)
                if not data:
                    return
                with self._stderr_lock:
                    self._stderr_tail.extend(data)
                    del self._stderr_tail[:-65536]
        except (OSError, ValueError):
            self._fault.set()

    def _write_stdin(self):
        try:
            while not self._stop.is_set():
                try:
                    payload = self._outgoing.get(timeout=.05)
                except queue.Empty:
                    continue
                view = memoryview(payload)
                while view and not self._stop.is_set():
                    written = self._process.stdin.write(view)
                    if not written:
                        raise OSError("closed pipe")
                    view = view[written:]
        except (OSError, ValueError):
            self._fault.set()

    def _send(self, command):
        try:
            payload = json.dumps(command, ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n"
            if len(payload) > 16384:
                raise ValueError("command bound")
            self._outgoing.put_nowait(payload)
        except (ValueError, TypeError, queue.Full) as error:
            raise WorkerProtocolError("Worker command is invalid or exceeds its bound") from error

    def _check(self, deadline, cancel):
        if cancel is not None and cancel.is_set():
            raise WorkerCancelled("Subtitle chunk cancelled; worker release required")
        if time.monotonic() >= deadline:
            raise TimeoutError("Subtitle worker exceeded its operation deadline")
        if self._fault.is_set():
            raise WorkerProtocolError("Worker pipe failed or exceeded its output bound")

    def _receive(self, deadline, cancel):
        while True:
            self._check(deadline, cancel)
            try:
                line = self._incoming.get(timeout=min(.05, max(.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if line is None:
                raise WorkerProtocolError("Worker exited before completing its operation")
            try:
                packet = json.loads(line.decode("utf-8"), object_pairs_hook=_unique_object,
                                    parse_constant=_invalid_constant)
            except (ValueError, UnicodeError, RecursionError) as error:
                raise WorkerProtocolError("Worker sent invalid JSON") from error
            if not isinstance(packet, dict):
                raise WorkerProtocolError("Worker packet must be an object")
            if packet.get("event") == "error":
                self._raise_remote_error(packet)
            self._accept_memory(packet)
            return packet

    def unload_model(self, *, timeout=10):
        timeout = _seconds(timeout)
        if not self._busy.acquire(blocking=False):
            raise WorkerProtocolError("Cancel the active chunk before unloading its worker")
        try:
            if self._released:
                return
            if self._process is None:
                self._loaded = False
                self._released = True
                return
            deadline = time.monotonic() + timeout
            self._send({"operation": "unload"})
            packet = self._receive(deadline, None)
            if (packet.get("event") != "released" or packet.get("protocol") != 1
                    or type(packet.get("protocol")) is not int
                    or set(packet) - {"event", "protocol", "memory"}):
                raise WorkerProtocolError("Worker did not confirm model release")
            while self._process.poll() is None:
                self._check(deadline, None)
                time.sleep(.01)
            if self._process.returncode != 0:
                raise WorkerProtocolError("Worker failed while releasing its model")
            self._finish_pipes()
        except BaseException:
            self._terminate()
            raise
        finally:
            self._busy.release()

    def release(self, *, timeout):
        """Cohort release contract, including cold and failed-load handles."""
        self.unload_model(timeout=timeout)
        return self.release_confirmed and self.model_is_loaded is False

    def _terminate(self):
        if self._process is None:
            self._loaded = False
            self._released = True
            return
        if self._limits is not None:
            self._limits.terminate()
        if self._process.poll() is None:
            self._process.kill()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired as error:
            raise WorkerProtocolError("Worker termination could not be verified; keep its reservation") from error
        self._finish_pipes()

    def _finish_pipes(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=1)
        if any(thread.is_alive() for thread in self._threads):
            raise WorkerProtocolError("Worker pipes remain active; keep its reservation")
        for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
            stream.close()
        for pending in (self._incoming, self._outgoing):
            while True:
                try:
                    pending.get_nowait()
                except queue.Empty:
                    break
        self._loaded = False
        if self._limits is not None:
            self._limits.close()
        self._released = True
