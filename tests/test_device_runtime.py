"""User-facing device composition; injected inventories are not hardware tests."""
from dataclasses import asdict
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from subgen_core.device_bundle import load_device_bundle, DeviceBundle, ProvisionedModel
from subgen_core.device_runtime import ProvisionedDeviceRuntime, configure_selected_devices
from subgen_core.execution_policy import ExecutionDevice, resolve_execution_policy
from subgen_core.model_envelope_catalog import ModelArtifactIdentity
from subgen_core.resource_management import GIB, MemoryPressureYield
from subgen_core.resource_probes import PressureSample


def artifact(model, backend):
    return ModelArtifactIdentity(model, 'ctranslate2' if backend == 'cuda' else 'ggml',
        'float16', 'sha256:'+'a'*64, 4, 'sha256:'+'b'*64)


def runtime(**updates):
    values = dict(memory_pressure_reserve_gib=None, gpu_memory_reserve_gib=None,
        requested_whisper_model='auto', segmentation_chunk_minutes=None, whisper_threads=2,
        execution_policy=resolve_execution_policy({}), logging=MagicMock(),
        check_model_runtime_cancelled=MagicMock(), model_runtime_cancel_event=threading.Event(),
        priority_pressure_reader=None, canonical_shared_cuda=False,
        task11b_gate_config=SimpleNamespace(enabled=False))
    values.update(updates)
    return SimpleNamespace(**values)


def provider(tmp_path, monkeypatch, *, ram=64, requested='auto', topology='shared', **kwargs):
    devices = (ExecutionDevice('cuda', 0, '1'*32, 'NVIDIA test', 'dedicated'),
        ExecutionDevice('vulkan', 1, '2'*32, 'Integrated test', topology))
    models = {model: {backend: ProvisionedModel(tmp_path/'model.bin', artifact(model, backend))
        for backend in ('cuda', 'vulkan')} for model in ('large-v3', 'medium', 'small', 'base', 'tiny')}
    p = ProvisionedDeviceRuntime(runtime(requested_whisper_model=requested, **kwargs),
        'cuda:0,vulkan:1', DeviceBundle(models, {}, None, '12.8', {}), tmp_path)
    import time
    host = PressureSample(observed_at=time.monotonic(), host_total_bytes=ram*GIB,
        host_available_bytes=(ram-1)*GIB, cgroup_limit_bytes=ram*GIB, cgroup_current_bytes=GIB)
    samples = tuple(PressureSample(gpu_total_bytes=24*GIB, gpu_free_bytes=22*GIB,
        gpu_device_id=d.physical_uuid, gpu_observed_at=time.monotonic()) for d in devices)
    monkeypatch.setattr(p, '_inventory', lambda h: (devices, {d.selector:object() for d in devices}))
    monkeypatch.setattr(p, '_samples', lambda d: (host, samples))
    monkeypatch.setattr('subgen_core.device_runtime.read_pressure_sample', lambda **k: host)
    return p


@pytest.mark.parametrize('ram,expected', [(4, None), (6, 'tiny'), (9, 'base'), (12, 'small'),
    (16, 'small'), (24, 'medium'), (32, 'large-v3'), (64, 'large-v3'), (128, 'large-v3')])
def test_public_provider_combines_worker_memory(tmp_path, monkeypatch, ram, expected):
    p = provider(tmp_path, monkeypatch, ram=ram)
    # These are TWO model copies, including the shared GPU working set; not
    # the single-worker CPU/GPU hardware guide. No reserve is counted twice.
    if expected is None:
        with pytest.raises(MemoryPressureYield):
            p(file_path='unused', language='en', task='transcribe')
        return
    plan = p(file_path='unused', language='en', task='transcribe')
    assert [s.artifact.model for s in plan.specs] == [expected]*2
    assert [s.device.selector for s in plan.specs] == ['cuda:0', 'vulkan:1']
    assert plan.decide_admission().admitted
    assert 'provisioned' in plan.selection_reason


@pytest.mark.parametrize('activity', ['passive', 'balanced', 'max'])
@pytest.mark.parametrize('mode', ['adaptive', 'dedicated'])
def test_modes_reach_public_provider(tmp_path, monkeypatch, activity, mode):
    policy = resolve_execution_policy({'SUBGEN_ACTIVITY':activity, 'SUBGEN_RUN_MODE':mode})
    p = provider(tmp_path, monkeypatch, execution_policy=policy)
    plan = p(file_path='unused', language='en', task='transcribe')
    assert plan.specs[0].artifact.model == 'large-v3'
    assert all(300 <= value <= policy.automatic_chunk_ceiling_minutes*60 for value in plan.chunk_seconds)


def test_explicit_large_never_downgrades(tmp_path, monkeypatch):
    p = provider(tmp_path, monkeypatch, ram=6, requested='large-v3')
    with pytest.raises(MemoryPressureYield):
        p(file_path='unused', language='en', task='transcribe')


@pytest.mark.parametrize('task,expected', [('translate','native-profile'), ('transcribe','fallback')])
def test_native_profile_reaches_admission_and_bounds_chunk_length(tmp_path, monkeypatch, task, expected):
    from dataclasses import replace
    from subgen_core.native_memory_profile import NativeMemoryProfile, NativeRunPeak, native_profile_key
    from subgen_core.model_envelope_catalog import NativeArtifactIdentity
    from subgen_core.vulkan_probe import VulkanDeviceObservation
    p = provider(tmp_path,monkeypatch,requested='base',segmentation_chunk_minutes=30)
    host,samples = p._samples(())
    devices,_ = p._inventory(host)
    device = devices[1]
    observed = VulkanDeviceObservation(1,device.name,device.physical_uuid,None,4098,1,2,3,'shared',True,(),0)
    monkeypatch.setattr(p,'_inventory',lambda _h:((device,),{device.selector:observed}))
    monkeypatch.setattr(p,'_samples',lambda _d:(host,(samples[1],)))
    artifacts = {name:NativeArtifactIdentity(name,'sha256:'+'c'*64,123)
        for name in ('worker','whisper','ggml-base','ggml-vulkan')}
    key = native_profile_key(p.bundle.models['base']['vulkan'].identity,artifacts,observed,threads=2,task='translate')
    peak = NativeRunPeak(200*1024**2,300*1024**2,100*1024**2,250*1024**2,310,True)
    profile = NativeMemoryProfile(key,'base',300,(peak,)*3)
    p.bundle = replace(p.bundle,native_artifacts=artifacts,native_profiles=(profile,))
    plan = p(file_path='unused',language='auto',task=task)
    decision = plan.decide_admission()
    assert decision.admitted
    assert decision.workers[0].requirement.provenance == expected
    if expected == 'native-profile':
        assert plan.chunk_seconds == (300,)
        assert decision.required_host_bytes < 2*GIB
    else:
        assert decision.required_host_bytes > 3*GIB


def test_factories_accept_lifecycle_spec_argument(tmp_path, monkeypatch):
    p = provider(tmp_path, monkeypatch)
    p.bundle.native_artifacts['worker.exe'] = SimpleNamespace(component='worker')
    cuda, native, wrapper = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr('subgen_core.device_runtime.CudaCohortWorker', cuda)
    monkeypatch.setattr('subgen_core.device_runtime.ResidentWhisperWorker', native)
    monkeypatch.setattr('subgen_core.device_runtime.VulkanCohortWorker', wrapper)
    plan = p(file_path='unused', language='en', task='transcribe')
    assert plan.specs[0].make_worker(plan.specs[0]) is cuda.return_value
    assert plan.specs[1].make_worker(plan.specs[1]) is wrapper.return_value
    assert cuda.call_args.kwargs['device_index'] == 0
    assert native.call_args.kwargs['env']['GGML_VK_VISIBLE_DEVICES'] == '1'
    assert native.call_args.kwargs['defer_load'] is True


def test_missing_explicit_artifact_is_configuration_error(tmp_path, monkeypatch):
    p = provider(tmp_path, monkeypatch, requested='base.en')
    with pytest.raises(ValueError, match='not provisioned'):
        p(file_path='unused', language='en', task='transcribe')


def test_unset_does_nothing():
    configure_selected_devices(runtime(), {})


@pytest.mark.parametrize('backend', ['cuda', 'vulkan'])
def test_single_device_selection_does_not_probe_other_backend(tmp_path, monkeypatch, backend):
    p = provider(tmp_path, monkeypatch)
    p.selectors = backend + ':1'
    devices = tuple(ExecutionDevice(backend, index, str(index + 1)*32,
        'Available GPU ' + str(index), 'dedicated') for index in (0, 1))
    discover = MagicMock(return_value=tuple(SimpleNamespace(device=d) for d in devices))
    monkeypatch.setattr(p, '_discover', discover)
    monkeypatch.setattr('subgen_core.device_runtime.vulkan_execution_devices', lambda _: devices)
    selected, _ = ProvisionedDeviceRuntime._inventory(p, object())
    assert selected == (devices[1],)
    assert [call.args[0] for call in discover.call_args_list] == [backend]


@pytest.mark.parametrize('backend', ['cuda', 'vulkan'])
def test_single_device_plan_never_creates_unselected_worker(tmp_path, monkeypatch, backend):
    p = provider(tmp_path, monkeypatch)
    host, samples = p._samples(())
    available, observations = p._inventory(host)
    selected = next(d for d in available if d.backend == backend)
    p.selectors = selected.selector
    monkeypatch.setattr(p, '_inventory', lambda _: ((selected,), observations))
    monkeypatch.setattr(p, '_samples', lambda _: (host, (samples[available.index(selected)],)))
    p.bundle.native_artifacts['worker.exe'] = SimpleNamespace(component='worker')
    cuda, native, wrapper = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr('subgen_core.device_runtime.CudaCohortWorker', cuda)
    monkeypatch.setattr('subgen_core.device_runtime.ResidentWhisperWorker', native)
    monkeypatch.setattr('subgen_core.device_runtime.VulkanCohortWorker', wrapper)
    plan = p(file_path='unused', language='auto', task='translate')
    assert len(plan.specs) == 1
    assert plan.specs[0].device == selected
    plan.specs[0].make_worker(plan.specs[0])
    if backend == 'cuda':
        cuda.assert_called_once()
        native.assert_not_called()
        wrapper.assert_not_called()
    else:
        native.assert_called_once()
        wrapper.assert_called_once()
        cuda.assert_not_called()


@pytest.mark.parametrize('name', ['Intel integrated test', 'AMD integrated test'])
def test_vulkan_only_plan_does_not_require_cuda_bundle_discovery_or_worker(tmp_path, monkeypatch, name):
    import time
    device = ExecutionDevice('vulkan', 0, '2'*32, name, 'shared')
    bundle = DeviceBundle({'base': {'vulkan': ProvisionedModel(tmp_path/'weights.bin', artifact('base','vulkan'))}},
        {'worker.exe':SimpleNamespace(component='worker')}, None, None, {})
    p = ProvisionedDeviceRuntime(runtime(requested_whisper_model='base'), 'vulkan:0', bundle, tmp_path)
    host = PressureSample(observed_at=time.monotonic(), host_total_bytes=24*GIB,
        host_available_bytes=20*GIB, cgroup_limit_bytes=24*GIB, cgroup_current_bytes=GIB)
    monkeypatch.setattr('subgen_core.device_runtime.read_pressure_sample', lambda **kw:host)
    discoveries=[]
    def discover(backend, sample):
        assert backend == 'vulkan'
        discoveries.append(backend)
        return (object(),)
    monkeypatch.setattr(p,'_discover',discover)
    monkeypatch.setattr('subgen_core.device_runtime.vulkan_execution_devices',lambda values:(device,))
    cuda=MagicMock(side_effect=AssertionError('CUDA must not be requested'))
    native, wrapper = MagicMock(), MagicMock()
    monkeypatch.setattr('subgen_core.device_runtime.CudaCohortWorker',cuda)
    monkeypatch.setattr('subgen_core.device_runtime.ResidentWhisperWorker',native)
    monkeypatch.setattr('subgen_core.device_runtime.VulkanCohortWorker',wrapper)
    monkeypatch.setattr('subgen_core.device_runtime.subprocess.run',
        MagicMock(side_effect=AssertionError('nvidia-smi must not be requested')))
    plan=p(file_path='unused',language='auto',task='translate')
    assert plan.decide_admission().admitted
    assert len(plan.specs)==1 and plan.specs[0].device==device
    assert plan.specs[0].make_worker(plan.specs[0]) is wrapper.return_value
    assert discoveries==['vulkan']
    cuda.assert_not_called()


def test_discovery_owner_retained_until_release(tmp_path, monkeypatch):
    p = provider(tmp_path, monkeypatch)
    handle = MagicMock(release_confirmed=False)
    handle.release.return_value = False
    p._discovery_handles.append(handle)
    with pytest.raises(RuntimeError, match='unconfirmed'):
        p.release(timeout=1)
    assert p._discovery_handles == [handle]
    handle.release.return_value = True
    p.release(timeout=1)
    assert not p._discovery_handles


def test_selected_without_bundle_fails_instead_of_legacy_fallback():
    with pytest.raises(ValueError, match='requires SUBGEN_DEVICE_BUNDLE'):
        configure_selected_devices(runtime(), {'SUBGEN_DEVICES':'cuda:0'})


def test_shared_production_receipt_cannot_be_relabelled():
    with pytest.raises(ValueError, match='production acceptance'):
        configure_selected_devices(runtime(canonical_shared_cuda=True), {'SUBGEN_DEVICES':'cuda:0'})


def manifest(tmp_path):
    (tmp_path/'weights.bin').write_bytes(b'test')
    return {'schema':'subgen.device-bundle/v1', 'models':{'base':{'vulkan':{
        'path':'weights.bin', 'identity':asdict(artifact('base', 'vulkan'))}}}}


def test_bundle_loads_relative_paths_without_allocating(tmp_path):
    data = manifest(tmp_path)
    path = tmp_path/'bundle.json'
    path.write_text(json.dumps(data))
    loaded = load_device_bundle(path)
    assert loaded.models['base']['vulkan'].path == tmp_path/'weights.bin'


@pytest.mark.parametrize('case', ['schema', 'unknown', 'parent', 'wrong_model', 'checkpoint', 'precision', 'empty'])
def test_bundle_refuses_invalid_contract(tmp_path, case):
    data = manifest(tmp_path)
    item = data['models']['base']['vulkan']
    if case == 'schema': data['schema'] = 'wrong'
    elif case == 'unknown': data['command'] = 'untrusted'
    elif case == 'parent': item['path'] = '../weights.bin'
    elif case == 'wrong_model': item['identity']['model'] = 'medium'
    elif case == 'checkpoint': item['identity']['source_checkpoint_sha256'] = None
    elif case == 'precision':
        data['models']['base']['cuda'] = {'path':'weights.bin', 'identity':asdict(artifact('base', 'cuda'))}
        data['models']['base']['cuda']['identity']['precision'] = 'float32'
    else: data['models'] = {}
    path = tmp_path/'bundle.json'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_device_bundle(path)


def test_bundle_duplicate_json_keys_rejected(tmp_path):
    path = tmp_path/'bundle.json'
    path.write_text('{"models":{}, "models":{}, "schema":"subgen.device-bundle/v1"}')
    with pytest.raises(RuntimeError, match='duplicate'):
        load_device_bundle(path)
