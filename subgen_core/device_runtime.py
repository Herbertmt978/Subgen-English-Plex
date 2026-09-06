"""Application composition for SUBGEN_DEVICES, using existing policy/lifecycle.

All allocations still go through aggregate admission, fixed artifact identities,
resident process ownership and the existing file journal / atomic publisher.
"""
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from . import resource_management as resources
from .cohort_policy import CohortPressureController
from .cohort_runtime import CohortWorkerSpec, FileCohortPlan, CohortCancelled
from .cuda_worker import CudaCohortWorker
from .device_bundle import load_device_bundle
from .device_discovery import GpuDiscoveryWorker, vulkan_execution_devices
from .execution_policy import resolve_execution_devices
from .human_progress import cohort_model_selection_lines
from .model_envelope_catalog import verify_native_artifact
from .native_memory_profile import native_profile_key
from .process_limits import establish_worker_limits
from .resource_probes import read_pressure_sample, PressureSample
from .vulkan_transcription import VulkanCohortWorker
from .whisper_cpp_worker import ResidentWhisperWorker


def _python_environment():
    environment = os.environ.copy()
    # The Windows venv launcher creates a second process. Run the real
    # interpreter with the already-selected application's import paths instead.
    environment['PYTHONPATH'] = os.pathsep.join(str(Path(p).absolute()) for p in sys.path if p)
    environment['HF_HUB_OFFLINE'] = '1'
    return sys._base_executable, environment


class ProvisionedDeviceRuntime:
    def __init__(self, runtime, selectors, bundle, scratch):
        self.runtime, self.selectors, self.bundle = runtime, selectors, bundle
        self.scratch = Path(scratch)
        if not self.scratch.is_absolute() or not self.scratch.is_dir() or self.scratch.is_symlink():
            raise ValueError('SUBGEN_DEVICE_SCRATCH must be an existing absolute local directory')
        self.reservation = resources.CohortReservation()
        self._discovery_handles = []
        self._lock = threading.Lock()
        self.python, self.environment = _python_environment()
        if bundle.native_artifacts:
            directories = sorted({str(Path(p).parent) for p in bundle.native_artifacts})
            self.environment['PATH'] = os.pathsep.join(directories + [self.environment.get('PATH', '')])

    def release(self, *, timeout):
        """Drain discovery too; a failed probe is still an owned real process."""
        deadline = time.monotonic() + timeout
        for handle in self._discovery_handles:
            if not handle.release_confirmed:
                remaining = deadline-time.monotonic()
                if remaining <= 0 or handle.release(timeout=remaining) is not True:
                    raise RuntimeError('GPU discovery release is unconfirmed')
        self._discovery_handles.clear()

    def _cancel(self):
        from .model_runtime import ModelRuntimeCancelled
        try:
            self.runtime.check_model_runtime_cancelled()
        except ModelRuntimeCancelled:
            raise CohortCancelled('Selected GPU work was stopped') from None

    def _limits(self, host, worker_count=1):
        if os.name != 'nt':
            return establish_worker_limits(host.cgroup_limit_bytes)
        reserve = resources.host_reserve_bytes(host.host_total_bytes,
            explicit_reserve_gib=self.runtime.memory_pressure_reserve_gib)
        # This is the OS backstop. Actual admission uses the combined current
        # free budget and the highest-safe-model requirements separately.
        cap = max(64*1024**2, (host.host_total_bytes-reserve)//worker_count)
        return establish_worker_limits(cap)

    def _discover(self, backend, host):
        self._cancel()
        if any(not worker.release_confirmed for worker in self._discovery_handles):
            raise RuntimeError('Previous GPU discovery release is unconfirmed')
        self._discovery_handles.clear()
        if backend == 'cuda':
            command = (self.python, '-m', 'subgen_core.cuda_discovery')
        else:
            if self.bundle.vulkan_probe is None:
                raise ValueError('Vulkan discovery executable is not provisioned')
            path, identity = self.bundle.vulkan_probe
            verify_native_artifact(path, identity, check_cancelled=self._cancel)
            command = (str(path), '--managed')
        limits = (establish_worker_limits(512*1024**2) if os.name == 'nt' else self._limits(host))
        handle = GpuDiscoveryWorker(command, backend=backend, establish_limits=limits,
            env=self.environment)
        self._discovery_handles.append(handle)
        return handle.discover(cancel=self.runtime.model_runtime_cancel_event)

    def _inventory(self, host):
        backends = {part.strip().split(':')[0].lower() for part in self.selectors.split(',')}
        inventory, observations = [], {}
        for backend in sorted(backends):
            if backend not in ('cuda', 'vulkan'):
                raise ValueError('SUBGEN_DEVICES must select cuda:N or vulkan:N')
            values = self._discover(backend, host)
            devices = tuple(v.device for v in values) if backend == 'cuda' else vulkan_execution_devices(values)
            inventory.extend(devices)
            observations.update((d.selector, value) for d, value in zip(devices, values))
        return resolve_execution_devices(self.selectors, tuple(inventory)), observations

    def _samples(self, devices):
        host = read_pressure_sample(priority_reader=(self.runtime.priority_pressure_reader
            if self.runtime.execution_policy.priority_signal_enabled else None))
        samples = []
        vulkan = None
        for device in devices:
            self._cancel()
            now = time.monotonic()
            if device.memory_topology == 'shared':
                samples.append(PressureSample(observed_at=now))
                continue
            if device.backend == 'cuda':
                # UUID, not nvidia-smi index: CUDA_VISIBLE_DEVICES may reorder.
                uuid = device.physical_uuid
                name = 'GPU-' + '-'.join((uuid[:8], uuid[8:12], uuid[12:16], uuid[16:20], uuid[20:]))
                result = subprocess.run(['nvidia-smi', '--id='+name,
                    '--query-gpu=uuid,memory.total,memory.free', '--format=csv,noheader,nounits'],
                    capture_output=True, text=True, timeout=5, check=True,
                    **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
                if len(result.stdout) > 1024:
                    raise ValueError('GPU memory response exceeds its bound')
                returned, total, free = [part.strip() for part in result.stdout.strip().split(',')]
                if returned.lower().removeprefix('gpu-').replace('-', '') != uuid:
                    raise ValueError('GPU memory response has a different physical identity')
                total, free = int(total)*1024**2, int(free)*1024**2
            else:
                if vulkan is None:
                    vulkan = self._discover('vulkan', host)
                observed = next(o for o in vulkan if o.uuid == device.physical_uuid)
                heaps = [h for h in observed.heaps if h.device_local]
                if not observed.budget_supported or len(heaps) != 1 or heaps[0].available_bytes is None:
                    raise ValueError('Selected Vulkan GPU cannot report an unambiguous memory budget')
                total, free = heaps[0].size_bytes, heaps[0].available_bytes
            if not 0 <= free <= total < 2**60 or total == 0:
                raise ValueError('GPU memory response is inconsistent')
            samples.append(PressureSample(gpu_total_bytes=total, gpu_free_bytes=free,
                gpu_device_id=device.physical_uuid, gpu_observed_at=now))
        return host, tuple(samples)

    def __call__(self, *, file_path, language, task):
        with self._lock:
            return self._plan(language=language, task=task)

    def _plan(self, *, language='auto', task='transcribe'):
        self._cancel()
        devices, observations = self._inventory(read_pressure_sample())
        host, samples = self._samples(devices)
        reserve = resources.host_reserve_bytes(host.host_total_bytes,
            explicit_reserve_gib=self.runtime.memory_pressure_reserve_gib)
        staging = 512*1024**2  # Includes a bounded discovery process, not model capacity.
        candidates = {}
        matched_profiles = {}
        def requirement(model, device):
            if (device.backend == 'vulkan' and device.memory_topology == 'shared'
                    and language in (None, 'auto') and self.bundle.native_profiles):
                key = native_profile_key(self.bundle.models[model]['vulkan'].identity,
                    self.bundle.native_artifacts, observations[device.selector],
                    threads=self.runtime.whisper_threads, task=task)
                profile = next((p for p in self.bundle.native_profiles if p.key == key and p.model == model), None)
                if profile is not None:
                    matched_profiles[model, device.selector] = profile
                    return resources.native_model_load_requirement(profile)
            return resources.model_load_requirement(model)
        for model, entries in self.bundle.models.items():
            if not all(d.backend in entries for d in devices):
                continue
            candidates[model] = tuple(resources.WorkerAdmissionRequest(device,
                requirement(model, device), sample,
                resources.gpu_priority_reserve_bytes(sample.gpu_total_bytes,
                    explicit_reserve_gib=self.runtime.gpu_memory_reserve_gib)
                if device.memory_topology == 'dedicated' else 0)
                for device, sample in zip(devices, samples))
        if not candidates:
            raise ValueError('No same-checkpoint model is provisioned for all selected backends')
        selection = resources.select_cohort_model(candidates, host,
            requested_model=self.runtime.requested_whisper_model, host_reserve_bytes=reserve,
            staging_reserve_bytes=staging, require_cgroup=os.name != 'nt')
        for line in cohort_model_selection_lines(selection):
            self.runtime.logging.info('%s', line)
        if selection.selected_model is None:
            if selection.reason == 'explicit_unavailable':
                raise ValueError('The requested model is not provisioned for every selected backend')
            raise resources.MemoryPressureYield('The selected model cannot currently fit all workers; no lower model was substituted')
        model = selection.selected_model
        self.runtime.whisper_model = model
        self.runtime.logging.info('Selected devices: %s', ', '.join(f'{d.selector} — {d.name}' for d in devices))
        self.runtime.logging.info('Model choices available in the local bundle: %s', ', '.join(candidates))
        requests = candidates[model]
        pressure = CohortPressureController(requests, sample_reader=lambda: self._samples(devices),
            host_reserve_bytes=reserve, staging_reserve_bytes=staging,
            require_cgroup=os.name != 'nt', check_cancelled=self._cancel,
            release_verified=lambda: getattr(self.runtime, 'active_file_cohort', None) is None
                or self.runtime.active_file_cohort.state == 'released')
        limits = self._limits(host, len(devices))
        specs = []
        for device in devices:
            entry = self.bundle.models[model][device.backend]
            def make_worker(_spec, device=device, entry=entry):
                if device.backend == 'cuda':
                    return CudaCohortWorker((self.python, '-m', 'subgen_core.cuda_worker_entry'),
                        model_directory=entry.path.parent, artifact=entry.identity,
                        support_files=entry.support_files, device_index=device.index,
                        physical_uuid=device.physical_uuid, expected_runtime=self.bundle.cuda_packages,
                        cuda_runtime=self.bundle.cuda_runtime, establish_limits=limits,
                        scratch_directory=self.scratch, env=self.environment)
                manifest = self.bundle.native_artifacts
                executable = next(path for path, identity in manifest.items() if identity.component == 'worker')
                environment = dict(self.environment, GGML_VK_VISIBLE_DEVICES=str(device.index))
                worker = ResidentWhisperWorker((executable, str(entry.path), '0', str(self.runtime.whisper_threads)),
                    establish_limits=limits, expected_ready={'device': 'Vulkan0',
                        'device_description': device.name, 'model_type': ('large' if model.startswith('large-v') else model.removesuffix('.en')),
                        'multilingual': not model.endswith('.en')}, timeout=120, defer_load=True,
                    env=environment, expected_observation=observations[device.selector],
                    model_artifact_identity=entry.identity, runtime_artifacts=manifest)
                return VulkanCohortWorker(worker, result_factory=lambda value: value, scratch_directory=self.scratch)
            specs.append(CohortWorkerSpec(device, entry.identity, entry.path, make_worker))
        effective = min(v for v in (host.host_total_bytes, host.cgroup_limit_bytes) if v is not None)
        capacity = resources.CapacityProfile(effective, host.host_total_bytes, host.cgroup_limit_bytes, 'device_runtime')
        seconds = resources.activity_chunk_seconds(capacity, self.runtime.segmentation_chunk_minutes,
            self.runtime.execution_policy, host_reserve=reserve)
        reason = ('user-selected model fits the combined budget' if selection.explicit
                  else 'highest-quality provisioned model that fits every GPU and combined RAM')
        chunk_seconds = tuple(min(seconds, matched_profiles[model,d.selector].maximum_chunk_seconds)
            if (model,d.selector) in matched_profiles else seconds for d in devices)
        return FileCohortPlan(tuple(specs), chunk_seconds, self.reservation,
            pressure.decide_admission, pressure.check_healthy, pressure.wait_for_recovery,
            self.scratch, reason)


def configure_selected_devices(runtime, environment):
    """Called once during application startup, before inventory/worker threads."""
    selectors = environment.get('SUBGEN_DEVICES', '').strip()
    if not selectors:
        return
    if getattr(runtime, 'cohort_plan_provider', None) is not None:
        return
    if runtime.canonical_shared_cuda or runtime.task11b_gate_config.enabled:
        raise ValueError('The single-device production acceptance profile cannot activate a device cohort')
    filename = environment.get('SUBGEN_DEVICE_BUNDLE', '').strip()
    if not filename:
        raise ValueError('SUBGEN_DEVICES requires SUBGEN_DEVICE_BUNDLE; no GPU or CPU fallback was selected')
    provider = ProvisionedDeviceRuntime(runtime, selectors, load_device_bundle(filename),
        environment.get('SUBGEN_DEVICE_SCRATCH', ''))
    from .model_runtime import configure_cohort_provider
    configure_cohort_provider(runtime, provider)
