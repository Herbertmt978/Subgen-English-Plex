"""Real small-process Windows containment plus resident lifecycle regressions."""
import json
import os
import subprocess
import sys

import pytest

from subgen_core.process_limits import establish_worker_limits, WindowsJobLimit, ProcessLimitError
from subgen_core.resident_worker import ResidentPipeWorker


@pytest.mark.parametrize('value', [True, 0, -1, 2**60, 1.5, '100', 1024])
def test_invalid_capacity(value):
    with pytest.raises(ValueError):
        establish_worker_limits(value)


@pytest.mark.skipif(os.name != 'nt', reason='Real Windows job test')
def test_real_job_refuses_allocation_and_releases():
    # The base interpreter avoids Windows venv redirector grandchildren.
    process = subprocess.Popen([sys._base_executable, '-I', '-c',
        'import sys,json; sys.stdin.readline();\n'
        'try: b=bytearray(256*1024**2); print(json.dumps({"bounded":False}))\n'
        'except MemoryError: print(json.dumps({"bounded":True}))'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    limit = None
    try:
        limit = WindowsJobLimit(process, 96*1024**2)
        output, _ = process.communicate('\n', timeout=10)
        assert process.returncode == 0
        assert json.loads(output) == {'bounded': True}
        limit.close()
        # Windows' peak counter can include the refused commit request. The
        # child's MemoryError proves refusal; this counter is diagnostic only.
        assert limit.peak_bytes > 0
        limit.close()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        if limit is not None:
            limit.close()


@pytest.mark.skipif(os.name != 'nt', reason='Real Windows job test')
def test_job_refuses_close_while_worker_remains():
    process = subprocess.Popen([sys._base_executable, '-I', '-c', 'import sys; sys.stdin.readline()'],
        stdin=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    limit = None
    try:
        limit = WindowsJobLimit(process, 96*1024**2)
        with pytest.raises(ProcessLimitError, match='still active'):
            limit.close()
        limit.terminate()
        process.wait(timeout=5)
        limit.close()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdin.close()
        if limit is not None:
            limit.close()


def test_transport_retains_unconfirmed_limit_owner():
    worker = ResidentPipeWorker(max_result_bytes=1024)
    calls = []

    class Limits:
        def terminate(self):
            calls.append('terminate')

        def close(self):
            calls.append('close')
            raise ProcessLimitError('unconfirmed')

    worker._spawn([sys._base_executable, '-I', '-c', 'import sys; sys.stdin.readline()'],
        establish_limits=lambda p: Limits())
    with pytest.raises(ProcessLimitError, match='unconfirmed'):
        worker._terminate()
    assert worker._process.poll() is not None
    assert not worker.release_confirmed
    assert calls == ['terminate', 'close']
    assert worker._limits is not None


def test_transport_closes_limit_owner_after_exit():
    worker = ResidentPipeWorker(max_result_bytes=1024)
    calls = []

    class Limits:
        def terminate(self):
            calls.append('terminate')

        def close(self):
            assert worker._process.poll() is not None
            calls.append('close')

    worker._spawn([sys._base_executable, '-I', '-c', 'import sys; sys.stdin.readline()'],
        establish_limits=lambda p: Limits())
    worker._terminate()
    assert worker.release_confirmed
    assert calls == ['terminate', 'close']
