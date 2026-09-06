"""Model-free OS backstop check; Linux MUST use a disposable 256 MiB container.

No media, credentials or network are needed. This intentionally exhausts only
the constrained child, not the host. It does not qualify inference recovery or
bound GPU-driver allocations. Run with the candidate directory on PYTHONPATH.
"""
import json
import os
from pathlib import Path
import sys

from subgen_core.process_limits import establish_worker_limits
from subgen_core.resident_worker import ResidentPipeWorker


def main():
    cap = (96 if os.name == 'nt' else 256)*1024**2
    def oom_count():
        return int(dict(line.split() for line in Path('/sys/fs/cgroup/memory.events').read_text().splitlines())['oom_kill'])
    if os.name != 'nt':
        if Path('/sys/fs/cgroup/memory.max').read_text().strip() != str(cap):
            raise RuntimeError('Run only in a disposable container with exactly 256 MiB RAM and no extra swap')
        before = oom_count()
    else:
        before = None
    worker = ResidentPipeWorker(max_result_bytes=1024)
    child = ('import sys; sys.stdin.readline();\n'
        'try: b=bytearray(512*1024**2); sys.exit(99)\n'
        'except MemoryError: sys.exit(23)')
    try:
        worker._spawn([sys._base_executable, '-I', '-c', child],
            establish_limits=establish_worker_limits(cap))
        worker._send({'operation':'test_allocation'})
        code = worker._process.wait(timeout=20)
        worker._terminate()
        if os.name == 'nt':
            assert code == 23, 'The worker did not report a refused allocation'
        else:
            assert code == -9 and oom_count() > before, 'The constrained child was not OOM-killed as expected'
        assert worker.release_confirmed
        print(json.dumps({'passed':True, 'platform':os.name, 'memory_limit_bytes':cap,
            'child_exit':code, 'release_confirmed':worker.release_confirmed,
            'inference_recovery_tested':False}))
    finally:
        worker._terminate()


if __name__ == '__main__':
    main()
