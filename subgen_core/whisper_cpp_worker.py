"""Bounded private-pipe supervision for the experimental resident Vulkan worker.

This transport owns one child, not resource policy or chunk scheduling. Its
caller must establish verified process limits before permitting model load.
It is not wired to the public runtime until backend identity and accounting
are qualified. A killed child is reusable only by creating a new worker.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence

from .resident_worker import ResidentPipeWorker, WorkerProtocolError, WorkerCancelled, _seconds
from .whisper_cpp_result import MAX_RESULT_BYTES, decode_whisper_cpp_result
from .vulkan_probe import VulkanDeviceObservation, decode_vulkan_observations
from .model_envelope_catalog import (
    ModelArtifactIdentity, NativeArtifactIdentity, ggml_weight_ftype,
    verify_model_artifact, verify_native_artifact,
)


def _native_manifest(command, artifacts):
    if not isinstance(artifacts, Mapping) or not 4 <= len(artifacts) <= 32:
        raise ValueError("Native runtime requires a bounded provisioned artifact mapping")
    manifest, components, names = {}, set(), set()
    for path, identity in artifacts.items():
        if (not isinstance(path, str) or not Path(path).is_absolute()
                or type(identity) is not NativeArtifactIdentity):
            raise ValueError("Native artifacts require absolute provisioned paths and identities")
        identity.__post_init__()
        normalized = os.path.normcase(os.path.abspath(path))
        name = os.path.basename(normalized)
        if normalized in manifest or name in names or identity.component in components:
            raise ValueError("Native runtime repeats a component or file")
        manifest[normalized] = identity
        names.add(name)
        components.add(identity.component)
    if not {"worker", "whisper", "ggml-base", "ggml-vulkan"}.issubset(components):
        raise ValueError("Native runtime is missing required inference components")
    executable = os.path.normcase(os.path.abspath(command[0]))
    if not Path(command[0]).is_absolute() or executable not in manifest or manifest[executable].component != "worker":
        raise ValueError("Native runtime executable contradicts its manifest")
    return manifest


def _confirm_native_modules(packet, manifest):
    modules = packet.get("runtime_modules")
    if not isinstance(modules, list) or not 1 <= len(modules) <= 256:
        raise WorkerProtocolError("Native worker did not provide a bounded module inventory")
    observed = set()
    for path in modules:
        if (not isinstance(path, str) or not 1 <= len(path) <= 32768
                or any(ord(c) < 32 for c in path) or not Path(path).is_absolute()):
            raise WorkerProtocolError("Native worker module inventory contains an invalid path")
        normalized = os.path.normcase(os.path.abspath(path))
        if normalized in observed:
            raise WorkerProtocolError("Native worker module inventory repeats a path")
        observed.add(normalized)
    if not set(manifest).issubset(observed):
        raise WorkerProtocolError("Native worker loaded a different provisioned runtime")
    expected_names = {os.path.basename(path) for path in manifest}
    if any(os.path.basename(path) in expected_names and path not in manifest for path in observed):
        raise WorkerProtocolError("Native worker loaded a shadowed runtime library")
    # Never open a path supplied only by the child. The caller verifies only
    # provisioned manifest files after this comparison. OS/driver modules are
    # not promoted to a verified runtime identity merely by being listed here.


class ResidentWhisperWorker(ResidentPipeWorker):
    """Single-flight protocol client; cancellation is supplied by the owner.

    Pipe readers/writer have bounded queues. All potentially blocking pipe IO
    runs outside the control thread, which checks cancellation/deadline every
    50 ms. No raw child stderr, subtitle text or paths appear in exceptions.
    The native executable must not spawn descendants; Windows job containment
    (or the enclosing Linux cgroup) remains the caller's responsibility.
    """

    def __init__(self, command: Sequence[str], *, establish_limits: Callable,
                 expected_ready: Mapping, timeout: float, cancel=None,
                 env=None, cwd=None, expected_observation: VulkanDeviceObservation | None = None,
                 model_artifact_identity: ModelArtifactIdentity | None = None,
                 runtime_artifacts: Mapping[str, NativeArtifactIdentity] | None = None,
                 defer_load: bool = False):
        """Optionally construct a cold handle for the cohort lifecycle owner.

        A deferred handle starts no child and allocates no model. The owner can
        retain it before calling load, including when startup cleanup fails.
        The default preserves the existing constructor-and-load interface.
        """
        _seconds(timeout)
        if type(defer_load) is not bool:
            raise ValueError("Deferred loading must be a boolean")
        if isinstance(command, (str, bytes)) or not command or any(not isinstance(arg, str) for arg in command):
            raise ValueError("Worker command must be an argument sequence")
        self._configuration = dict(command=tuple(command), establish_limits=establish_limits,
            expected_ready=dict(expected_ready), env=None if env is None else dict(env), cwd=cwd,
            expected_observation=expected_observation, model_artifact_identity=model_artifact_identity,
            runtime_artifacts=None if runtime_artifacts is None else dict(runtime_artifacts))
        super().__init__(max_result_bytes=MAX_RESULT_BYTES)
        self._expected_observation = expected_observation
        self.latest_observation = None
        self.model_artifact_identity = None
        self.runtime_artifact_identities = None
        if not defer_load:
            self.load(timeout=timeout, cancel=cancel)

    def load(self, *, timeout, cancel=None):
        """Start this generation once, after aggregate resource admission."""
        _seconds(timeout)
        if not self._busy.acquire(blocking=False):
            raise WorkerProtocolError("Worker already has an active operation")
        try:
            if self._attempted_load or self._released:
                raise WorkerProtocolError("Create a new worker for another residency generation")
            self._attempted_load = True
            self._start(timeout=timeout, cancel=cancel, **self._configuration)
        finally:
            self._busy.release()

    def _start(self, command, *, establish_limits, expected_ready, timeout, cancel,
               env, cwd, expected_observation, model_artifact_identity, runtime_artifacts):
        timeout = _seconds(timeout)
        deadline = time.monotonic() + timeout
        if isinstance(command, (str, bytes)) or not command or any(not isinstance(arg, str) for arg in command):
            raise ValueError("Worker command must be an argument sequence")
        if not callable(establish_limits):
            raise ValueError("Worker requires a process-limit owner")
        required = {"device", "device_description", "model_type", "multilingual"}
        if not required.issubset(expected_ready) or type(expected_ready["multilingual"]) is not bool:
            raise ValueError("Worker requires an expected device and model receipt")
        if any(not isinstance(expected_ready[key], str) or not expected_ready[key]
               for key in required - {"multilingual"}):
            raise ValueError("Worker device and model receipt must be non-empty")
        def check_artifacts():
            if cancel is not None and cancel.is_set():
                raise WorkerCancelled("Runtime artifact verification was cancelled")
            if time.monotonic() >= deadline:
                raise TimeoutError("Runtime artifact verification timed out")

        def verify_weights():
            verify_model_artifact(command[1], model_artifact_identity, check_cancelled=check_artifacts)

        manifest = None
        if runtime_artifacts is not None:
            if model_artifact_identity is None:
                raise ValueError("Native runtime binding also requires exact model identity")
            manifest = _native_manifest(command, runtime_artifacts)
            for path, identity in manifest.items():
                verify_native_artifact(path, identity, check_cancelled=check_artifacts)
        self.runtime_artifact_identities = None

        if model_artifact_identity is not None:
            if (type(model_artifact_identity) is not ModelArtifactIdentity
                    or model_artifact_identity.backend_format != "ggml"
                    or len(command) != 4 or not Path(command[1]).is_absolute()):
                raise ValueError("Native worker requires a GGML artifact and an absolute model argument")
            expected_ftype = ggml_weight_ftype(model_artifact_identity)
            expected_type = model_artifact_identity.model.removesuffix(".en")
            if expected_type.startswith("large-v"):
                expected_type = "large"  # Native receipt reports the family, not its revision.
            if (expected_ready["model_type"] != expected_type
                    or expected_ready["multilingual"] != (not model_artifact_identity.model.endswith(".en"))):
                raise ValueError("Worker receipt contradicts the selected model artifact")
            verify_weights()  # Refuse wrong bytes before a child is created.
        self.model_artifact_identity = None
        if expected_observation is not None and not isinstance(expected_observation, VulkanDeviceObservation):
            raise ValueError("Worker physical observation has an invalid type")
        self._expected_observation = expected_observation
        self.latest_observation = None
        check_artifacts()
        try:
            # Child waits for load until the common owner establishes limits.
            self._spawn(command, establish_limits=establish_limits, env=env, cwd=cwd)
            load = {"operation": "load"}
            if expected_observation is not None:
                load["physical_uuid"] = expected_observation.uuid
            self._send(load)
            packet = self._receive(deadline, cancel)
            if (packet.get("event") != "ready" or type(packet.get("protocol")) is not int
                    or packet["protocol"] != 1 or packet.get("backend") != "Vulkan"
                    or any(packet.get(key) != expected_ready[key] for key in required)
                    or type(packet.get("multilingual")) is not bool):
                raise WorkerProtocolError("Worker did not confirm the selected Vulkan device and model")
            self.ready = {key: packet[key] for key in required | {"backend", "protocol"}}
            if manifest is not None:
                _confirm_native_modules(packet, manifest)
                for path, identity in manifest.items():
                    verify_native_artifact(path, identity, check_cancelled=check_artifacts)
                self.runtime_artifact_identities = tuple(sorted(manifest.values(), key=lambda item: item.component))
            if model_artifact_identity is not None:
                if type(packet.get("model_ftype")) is not int or packet["model_ftype"] != expected_ftype:
                    raise WorkerProtocolError("Loaded GGML weight format contradicts the selected model artifact")
                verify_weights()  # Detect file replacement during cold load.
                self.model_artifact_identity = model_artifact_identity
                self.ready["model_ftype"] = packet["model_ftype"]
            self._loaded = True
        except BaseException:
            self._terminate()
            raise

    def _accept_memory(self, packet):
        expected = self._expected_observation
        if expected is None:
            return  # Harmless protocol fixtures; native worker requires UUID.
        try:
            observations = decode_vulkan_observations(
                json.dumps(packet.get("memory"), allow_nan=False).encode(),
                observed_at=time.monotonic(),
            )
            if len(observations) != 1:
                raise ValueError("selected device count")
            observation = observations[0]
            if observation.query_scope != "allocating_instance":
                raise ValueError("observation is not from the allocating Vulkan instance")
            identity_fields = ("uuid", "pci_id", "name", "vendor_id", "device_id",
                               "driver_version_raw", "api_version_raw", "memory_topology")
            if any(getattr(observation, key) != getattr(expected, key) for key in identity_fields):
                raise ValueError("selected device changed")
        except (ValueError, TypeError):
            raise WorkerProtocolError("Worker memory observation is missing or belongs to another device") from None
        self.latest_observation = observation

    def _raise_remote_error(self, packet):
        # Only translate fixed producer codes, never arbitrary child text or
        # stderr. Preserve the existing failure type and termination policy.
        messages = {
            "invalid_segment_timing": "Native decoder returned invalid subtitle timings",
            "inference_failed": "Native speech recognition failed",
            "worker_failed": "Native subtitle worker failed unexpectedly",
        }
        if isinstance(packet, dict) and set(packet) == {"event", "code"}:
            code = packet["code"]
            if packet["event"] == "error" and isinstance(code, str) and code in messages:
                raise WorkerProtocolError(f"{messages[code]} ({code})")
        super()._raise_remote_error(packet)

    def observe_memory(self, *, timeout=5):
        """Request a fresh same-process idle observation, not global GPU usage."""
        timeout = _seconds(timeout)
        if self._expected_observation is None:
            raise WorkerProtocolError("Worker has no bound physical observation")
        if not self._busy.acquire(blocking=False):
            raise WorkerProtocolError("Worker already has an active operation")
        try:
            if not self.model_is_loaded:
                raise WorkerProtocolError("Worker model is not resident")
            self._send({"operation": "observe"})
            packet = self._receive(time.monotonic() + timeout, None)
            if packet.get("event") != "memory":
                raise WorkerProtocolError("Worker did not return its memory observation")
            return self.latest_observation
        except BaseException:
            self._terminate()
            raise
        finally:
            self._busy.release()

    def transcribe(self, audio_path: str, *, duration_seconds: float, language: str,
                   translate: bool, timeout: float, cancel=None, progress=None):
        timeout = _seconds(timeout)
        if not self._busy.acquire(blocking=False):
            raise WorkerProtocolError("Worker already has an active operation")
        try:
            if not self.model_is_loaded:
                raise WorkerProtocolError("Worker model is not resident")
            if type(translate) is not bool or not isinstance(language, str) or not isinstance(audio_path, str):
                raise ValueError("Invalid transcription request")
            if (isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float))
                    or not math.isfinite(duration_seconds) or not 0 < duration_seconds <= 1810):
                raise ValueError("Worker audio must be a bounded chunk of at most 1810 seconds")
            self._request += 1
            deadline = time.monotonic() + timeout
            self._send({"operation": "transcribe", "request_id": self._request,
                        "audio_path": audio_path, "language": language, "translate": translate})
            last = -1
            while True:
                packet = self._receive(deadline, cancel)
                if type(packet.get("request_id")) is not int or packet["request_id"] != self._request:
                    raise WorkerProtocolError("Worker result belongs to another chunk")
                if packet.get("event") == "progress":
                    percent = packet.get("percent")
                    if type(percent) is not int or not last < percent <= 100 or percent < 0:
                        raise WorkerProtocolError("Worker progress is invalid or out of order")
                    last = percent
                    if progress is not None:
                        progress(percent)
                elif packet.get("event") == "result":
                    result = decode_whisper_cpp_result(
                        json.dumps(packet.get("result"), ensure_ascii=True, allow_nan=False).encode(),
                        duration_seconds=duration_seconds,
                    )
                    self._check(deadline, cancel)
                    return result
                else:
                    raise WorkerProtocolError("Worker sent an unexpected chunk event")
        except BaseException:
            self._terminate()
            raise
        finally:
            self._busy.release()
