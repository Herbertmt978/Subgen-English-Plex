"""Explicit, bounded calibration for one shared-memory Vulkan GPU.

Runs the normal provider/lifecycle and writes a new bundle only after three
cold runs, valid results and confirmed worker release. Does not touch media.
"""
import argparse
from dataclasses import replace
import io
import json
import logging
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import wave

from .cohort_runtime import CohortModelRuntime
from .device_bundle import load_device_bundle
from .device_runtime import ProvisionedDeviceRuntime
from .execution_policy import resolve_execution_policy
from .native_memory_profile import NativeMemoryProfile, NativeRunPeak, native_profile_key
from .resource_probes import read_pressure_sample, read_process_peak_bytes


def calibration_audio(filename, chunk_seconds):
    """Include the runner's maximum ten seconds of extraction overlap."""
    with wave.open(str(filename),'rb') as source:
        if (source.getnchannels(),source.getsampwidth(),source.getframerate(),source.getcomptype()) != (1,2,16000,'NONE'):
            raise ValueError('Calibration requires mono 16 kHz PCM16 WAV audio')
        frames = (chunk_seconds+10)*16000
        if source.getnframes() < frames:
            raise ValueError('Calibration audio must cover a full chunk plus ten seconds')
        pcm = source.readframes(frames)
    if len(pcm) != frames*2 or not any(pcm):
        raise ValueError('Calibration requires complete, non-silent audio')
    buffer = io.BytesIO()
    with wave.open(buffer,'wb') as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
        output.writeframes(pcm)
    return buffer.getvalue()


def calibrate(bundle_path, output, *, selector, model, audio_path, scratch,
              chunk_seconds=300, task='translate', threads=2, host_reserve_gib=None):
    output = Path(output)
    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise ValueError('Calibration writes a new absolute bundle; it never overwrites one')
    if output.parent.resolve() != Path(bundle_path).parent.resolve():
        raise ValueError('The calibrated bundle must be saved beside its source bundle')
    if type(threads) is not int or not 1 <= threads <= 256 or task not in ('transcribe','translate'):
        raise ValueError('Invalid native calibration decoder settings')
    if type(chunk_seconds) is not int or not 300 <= chunk_seconds <= 1800:
        raise ValueError('Calibration chunks must be between five and thirty minutes')
    if not selector.startswith('vulkan:') or ',' in selector:
        raise ValueError('Calibrate exactly one Vulkan GPU at a time')
    bundle = replace(load_device_bundle(bundle_path),native_profiles=())
    audio = calibration_audio(audio_path,chunk_seconds)
    stopped = threading.Event()
    started = time.monotonic()
    def check():
        if stopped.is_set() or time.monotonic()-started > 7200:
            raise RuntimeError('Native calibration was cancelled or exceeded two hours')
    runtime = SimpleNamespace(memory_pressure_reserve_gib=host_reserve_gib,gpu_memory_reserve_gib=None,
        execution_policy=resolve_execution_policy({'SUBGEN_ACTIVITY':'max','SUBGEN_RUN_MODE':'dedicated'}),
        priority_pressure_reader=None,requested_whisper_model=model,segmentation_chunk_minutes=chunk_seconds//60,
        whisper_threads=threads,check_model_runtime_cancelled=check,model_runtime_cancel_event=stopped,
        logging=logging,active_file_cohort=None)
    provider = ProvisionedDeviceRuntime(runtime,selector,bundle,Path(scratch))
    runs, profile_key = [], None
    try:
        for index in range(3):
            plan = provider(file_path=str(audio_path),language='auto',task=task)
            spec = plan.specs[0]
            if len(plan.specs) != 1 or spec.device.memory_topology != 'shared':
                raise ValueError('This profiler is for one shared-memory integrated GPU')
            # The factory remains cold. Keep its handle for observational reads;
            # the existing cohort still owns all allocations and termination.
            handles = []
            def factory(value):
                worker = spec.make_worker(value); handles.append(worker); return worker
            cohort = CohortModelRuntime((replace(spec,make_worker=factory),), reservation=plan.reservation,
                decide_admission=plan.decide_admission,check_healthy=plan.check_healthy)
            runtime.active_file_cohort = cohort
            before = read_pressure_sample()
            peaks = dict(process=0,gpu=0,host=0,cgroup=0)
            def observe(*_args):
                check()
                if not plan.check_healthy():
                    from .resource_management import MemoryPressureYield
                    raise MemoryPressureYield('Calibration yielded; no profile will be saved')
                worker = handles[0]
                observation = worker.latest_observation
                if (observation is None or not observation.budget_supported
                        or observation.query_scope != 'allocating_instance'
                        or any(h.usage_bytes is None for h in observation.heaps)):
                    raise ValueError('Native allocation telemetry is unavailable')
                current_key = native_profile_key(spec.artifact,bundle.native_artifacts,observation,
                                                 threads=threads,task=task)
                if profile_key is not None and current_key != profile_key:
                    raise ValueError('Native identity changed during calibration')
                host = read_pressure_sample()
                if before.host_available_bytes is None or host.host_available_bytes is None:
                    raise ValueError('Host memory telemetry is unavailable')
                peaks['process'] = max(peaks['process'],read_process_peak_bytes(worker.model.pid))
                peaks['gpu'] = max(peaks['gpu'],sum(h.usage_bytes for h in observation.heaps))
                peaks['host'] = max(peaks['host'],before.host_available_bytes-host.host_available_bytes)
                if os.name != 'nt' and (before.cgroup_current_bytes is None or host.cgroup_current_bytes is None):
                    raise ValueError('Container memory telemetry is unavailable')
                if before.cgroup_current_bytes is not None and host.cgroup_current_bytes is not None:
                    peaks['cgroup'] = max(peaks['cgroup'],host.cgroup_current_bytes-before.cgroup_current_bytes)
                return current_key
            print(f'Calibration {index+1}/3: {model} on {selector}; emergency memory checks remain enabled',flush=True)
            try:
                cohort.load(timeout=120)
                profile_key = observe()
                result = cohort.transcribe(selector,audio,timeout=2100,language='auto',task=task,
                                           progress_callback=observe)
                observe()
                if not result.get('segments'):
                    raise ValueError('Calibration produced no speech; use a representative spoken recording')
            finally:
                cohort.release(timeout=30)
            if cohort.state != 'released':
                raise RuntimeError('Native release was not confirmed')
            runs.append(NativeRunPeak(peaks['process'],peaks['gpu'],peaks['host'],peaks['cgroup'],chunk_seconds+10,True))
        profile = NativeMemoryProfile(profile_key,model,chunk_seconds,tuple(runs))
        data = json.loads(Path(bundle_path).read_text(encoding='utf8'))
        # Existing relative model/runtime paths were relative to the old bundle.
        # Keep this output beside it, so relocation cannot silently change bytes.
        data['native_profiles'] = [p for p in data.get('native_profiles',[]) if p['key'] != profile.key]+[profile.to_dict()]
        pending = output.with_name(output.name+'.pending')
        with pending.open('x',encoding='utf8') as stream:
            json.dump(data,stream,indent=2); stream.flush(); os.fsync(stream.fileno())
        load_device_bundle(pending)
        pending.rename(output)
        print(f'Calibration complete: measured shared-RAM upper bound {profile.host_peak_bytes/1024**3:.2f} GiB '
              f'+ {profile.host_margin_bytes/1024**3:.2f} GiB margin; maximum chunk {chunk_seconds//60} minutes',flush=True)
        return profile
    finally:
        provider.release(timeout=15)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--device',required=True)
    parser.add_argument('--model',required=True)
    parser.add_argument('--audio',required=True)
    parser.add_argument('--scratch',required=True)
    parser.add_argument('--chunk-seconds',type=int,default=300)
    parser.add_argument('--task',choices=['transcribe','translate'],default='translate')
    parser.add_argument('--threads',type=int,default=2)
    parser.add_argument('--host-reserve-gib',type=float)
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(message)s')
    calibrate(args.bundle,args.output,selector=args.device,model=args.model,audio_path=args.audio,
        scratch=args.scratch,chunk_seconds=args.chunk_seconds,task=args.task,threads=args.threads,
        host_reserve_gib=args.host_reserve_gib)


if __name__=='__main__':
    main()
