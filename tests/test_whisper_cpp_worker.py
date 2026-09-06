"""Real, harmless subprocess tests of supervision; no GPU/model is loaded."""

import subprocess
import json
import hashlib
import sys
import threading
import time

import pytest

from subgen_core import whisper_cpp_worker as worker_module
from subgen_core.whisper_cpp_worker import ResidentWhisperWorker, WorkerCancelled, WorkerProtocolError
from subgen_core.vulkan_probe import decode_vulkan_observations
from subgen_core.model_envelope_catalog import ArtifactValidationError, ModelArtifactIdentity, NativeArtifactIdentity


READY = {"device": "Vulkan0", "device_description": "Test integrated GPU",
         "model_type": "base", "multilingual": False}
PROGRAM = r'''
import json, sys, time
mode = sys.argv[1]
memory = json.loads(sys.argv[2]) if len(sys.argv) > 2 else None
def emit(packet):
    if memory is not None and mode != 'missing_memory':
        packet['memory'] = memory
        if mode == 'wrong_memory' and packet['event'] != 'ready':
            packet['memory']['devices'][0]['uuid'] = 'abcd' * 8
    print(json.dumps(packet), flush=True)
if mode == 'no_read':
    time.sleep(20)
    sys.exit()
load = json.loads(input())
assert load == ({'operation':'load', 'physical_uuid':memory['devices'][0]['uuid']} if memory else {'operation':'load'})
ready = {'event':'ready', 'protocol':1, 'backend':'Vulkan',
         'device':'Vulkan0', 'device_description':'Test integrated GPU',
         'model_type':'base', 'multilingual':False, 'model_ftype':1}
if mode == 'wrong_ftype': ready['model_ftype'] = 7
if mode == 'missing_ftype': ready.pop('model_ftype')
if mode == 'bool_ftype': ready['model_ftype'] = True
if len(sys.argv) > 3: ready['runtime_modules'] = json.loads(sys.argv[3])
if mode == 'wrong_device': ready['device'] = 'Vulkan1'
if mode == 'cpu': ready['backend'] = 'CPU'
if mode == 'wrong_model': ready['model_type'] = 'small'
if mode == 'bad_ready': ready['protocol'] = True
if mode == 'duplicate':
    print('{"event":"ready","event":"ready"}', flush=True)
elif mode == 'utf8':
    sys.stdout.buffer.write(b'\xff\n'); sys.stdout.buffer.flush()
elif mode == 'oversize':
    sys.stdout.write('x' * 65536); sys.stdout.flush()
elif mode == 'partial':
    sys.stdout.write('{'); sys.stdout.flush(); sys.exit(0)
elif mode == 'stderr':
    sys.stderr.write('private data ' * 100000); sys.stderr.flush(); emit(ready)
else: emit(ready)
if mode == 'no_more_reads': time.sleep(20)
for line in sys.stdin:
    command = json.loads(line)
    if command['operation'] == 'observe':
        emit({'event':'memory'})
        continue
    if command['operation'] == 'unload':
        if mode == 'no_release': sys.exit(0)
        emit({'event':'released', 'protocol':1})
        if mode == 'release_hang': time.sleep(20)
        sys.exit(7 if mode == 'release_exit' else 0)
    request = command['request_id']
    if mode == 'exit': sys.exit(1)
    if mode == 'error': emit({'event':'error', 'code':'private data'})
    if mode == 'timing_error': emit({'event':'error', 'code':'invalid_segment_timing'})
    if mode == 'wrong_request': request += 1
    emit({'event':'progress', 'request_id':request, 'percent':0})
    if mode == 'hang': time.sleep(20)
    if mode == 'bad_progress': emit({'event':'progress', 'request_id':request, 'percent':0})
    if mode == 'bool_request': request = True
    emit({'event':'result', 'request_id':request, 'result':{
        'result':{'language':'en'}, 'transcription':[
            {'offsets':{'from':0, 'to':1000}, 'text':' example'}]}})
'''


def start(mode="ok", *, bound=False, **kwargs):
    processes = []
    def establish(process):
        assert process.poll() is None
        processes.append(process)
    # No model/large allocations: this explicitly supplied limit-owner stub is
    # ONLY for the harmless fixture, not evidence of OS memory enforcement.
    command = [sys.executable, "-u", "-c", PROGRAM, mode]
    if bound:
        memory = memory_document()
        command.append(json.dumps(memory))
        kwargs["expected_observation"] = decode_vulkan_observations(json.dumps(memory).encode(), observed_at=0)[0]
    worker = ResidentWhisperWorker(command, establish_limits=establish,
        expected_ready=READY, timeout=2, **kwargs)
    return worker, processes[0]


def transcribe(worker, **kwargs):
    return worker.transcribe("fixture.f32le", duration_seconds=1, language="en",
                             translate=False, timeout=1, **kwargs)


def assert_stopped(worker, process):
    assert process.poll() is not None
    assert worker.release_confirmed
    assert not worker.model_is_loaded
    assert all(not thread.is_alive() for thread in worker._threads)


def test_native_timing_failure_is_readable_and_still_releases_child():
    worker, process = start('timing_error')
    with pytest.raises(WorkerProtocolError, match='invalid subtitle timings.*invalid_segment_timing'):
        transcribe(worker)
    assert_stopped(worker, process)


@pytest.mark.parametrize('packet', [
    {'event':'error', 'code':'private dialogue'},
    {'event':'error', 'code':['invalid_segment_timing']},
    {'event':'error', 'code':'invalid_segment_timing', 'detail':'private dialogue'},
    {'event':'result', 'code':'invalid_segment_timing'},
])
def test_native_failure_messages_do_not_trust_unknown_or_extended_packets(packet):
    worker = deferred()
    with pytest.raises(WorkerProtocolError) as error:
        worker._raise_remote_error(packet)
    assert str(error.value) == 'Subtitle worker reported a processing failure'
    assert worker.pid is None
    worker.release(timeout=1)


def deferred(mode='ok', *, establish=None):
    return ResidentWhisperWorker([sys.executable, '-u', '-c', PROGRAM, mode],
        establish_limits=establish or (lambda process: None), expected_ready=READY,
        timeout=2, defer_load=True)


def test_deferred_handle_starts_only_on_load_and_releases_once():
    processes = []
    worker = deferred(establish=processes.append)
    assert worker.pid is None and not worker.model_is_loaded and not processes
    worker.load(timeout=2)
    assert worker.model_is_loaded and worker.pid == processes[0].pid
    assert transcribe(worker)['segments']
    assert worker.release(timeout=2) is True
    assert_stopped(worker, processes[0])
    assert worker.release(timeout=1) is True
    with pytest.raises(WorkerProtocolError, match='new worker'):
        worker.load(timeout=1)


def test_cold_release_never_starts_a_process():
    worker = deferred()
    assert worker.release(timeout=1) is True
    assert worker.pid is None and not worker.model_is_loaded
    with pytest.raises(WorkerProtocolError, match='new worker'):
        worker.load(timeout=1)


def test_deferred_load_cancellation_before_spawn():
    worker = deferred()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(WorkerCancelled):
        worker.load(timeout=1, cancel=cancel)
    assert worker.pid is None
    assert worker.release(timeout=1) is True


def test_failed_deferred_load_keeps_handle_for_confirmed_cleanup():
    processes = []
    worker = deferred('wrong_device', establish=processes.append)
    with pytest.raises(WorkerProtocolError):
        worker.load(timeout=2)
    assert_stopped(worker, processes[0])
    assert worker.release(timeout=1) is True


def test_unconfirmed_startup_cleanup_keeps_the_deferred_handle(monkeypatch):
    worker = deferred('wrong_device')
    original = worker._terminate
    monkeypatch.setattr(worker, '_terminate', lambda: (_ for _ in ()).throw(WorkerProtocolError('unconfirmed cleanup')))
    try:
        with pytest.raises(WorkerProtocolError, match='unconfirmed cleanup'):
            worker.load(timeout=2)
        assert worker.pid is not None and not worker.release_confirmed
    finally:
        monkeypatch.setattr(worker, '_terminate', original)
        worker._terminate()
    assert worker.release(timeout=1) is True


@pytest.mark.parametrize('second_mode', ['ok', 'wrong_device'])
def test_cohort_drives_real_pipe_lifecycle_and_wav_adapter(tmp_path, second_mode):
    """Real transport/adapter/owner, harmless child; no physical GPU claim."""
    import io
    import wave
    from subgen_core.cohort_runtime import CohortModelRuntime, CohortWorkerSpec
    from subgen_core.execution_policy import ExecutionDevice
    from subgen_core.resource_management import CohortAdmissionDecision, CohortReservation
    from subgen_core.vulkan_transcription import VulkanCohortWorker
    path = tmp_path / 'fixture-weights'
    path.write_bytes(b'fixture')
    identity = ModelArtifactIdentity('base.en', 'ggml', 'float16',
        'sha256:' + hashlib.sha256(b'fixture').hexdigest(), 7, 'sha256:' + 'a' * 64)
    handles, processes = [], []
    def factory(spec):
        native = deferred('ok' if spec.device.index == 0 else second_mode, establish=processes.append)
        handle = VulkanCohortWorker(native, result_factory=dict, scratch_directory=tmp_path)
        handles.append(handle)
        assert native.pid is None
        return handle
    specs = tuple(CohortWorkerSpec(ExecutionDevice('vulkan', i, f'{i + 1:032x}',
        'Test integrated GPU', 'shared'), identity, path, factory) for i in range(2))
    cohort = CohortModelRuntime(specs, reservation=CohortReservation(),
        decide_admission=lambda: CohortAdmissionDecision(True, (), 10, 20, ()),
        check_healthy=lambda: True)
    try:
        if second_mode != 'ok':
            with pytest.raises(WorkerProtocolError):
                cohort.load(timeout=5)
            assert cohort.state == 'released'
        else:
            cohort.load(timeout=5)
            audio = io.BytesIO()
            with wave.open(audio, 'wb') as target:
                target.setnchannels(1)
                target.setsampwidth(2)
                target.setframerate(16000)
                target.writeframes(b'\0\0' * 16000)
            for spec in specs:
                result = cohort.transcribe(spec.device.selector, audio.getvalue(),
                    timeout=2, language='en')
                assert result['segments'][0]['end'] == 1
            assert list(tmp_path.iterdir()) == [path]
    finally:
        cohort.release(timeout=5)
    assert len(processes) == 2 and all(p.poll() is not None for p in processes)
    assert all(h.model.release_confirmed and not h.model_is_loaded for h in handles)


def memory_document():
    return {"protocol": 1, "usage_scope": "process", "query_scope": "allocating_instance", "devices": [{
        "physical_index": 1, "name": "Test integrated GPU", "uuid": "1234" * 8,
        "pci_id": None, "vendor_id": 1, "device_id": 2, "driver_version_raw": 3,
        "api_version_raw": 4, "memory_topology": "shared", "budget_supported": True,
        "heaps": [{"index": 0, "size_bytes": 1024, "device_local": True,
                   "budget_bytes": 900, "usage_bytes": 300, "available_bytes": 600}]}]}


def test_bound_ready_idle_progress_result_and_release_memory():
    worker, process = start(bound=True)
    try:
        assert worker.latest_observation.uuid == "1234" * 8
        previous = worker.latest_observation.observed_at
        assert worker.observe_memory().observed_at >= previous
        assert transcribe(worker)["segments"]
        assert worker.latest_observation.usage_scope == "process"
    finally:
        worker.unload_model()
    assert_stopped(worker, process)
    assert worker.latest_observation.heaps[0].usage_bytes == 300


def test_wrong_physical_observation_aborts_uncommitted_chunk():
    worker, process = start("wrong_memory", bound=True)
    with pytest.raises(WorkerProtocolError, match="another device"):
        transcribe(worker)
    assert_stopped(worker, process)


def test_bound_worker_rejects_missing_ready_memory():
    processes = []
    memory = memory_document()
    with pytest.raises(WorkerProtocolError, match="missing"):
        ResidentWhisperWorker([sys.executable, "-u", "-c", PROGRAM, "missing_memory", json.dumps(memory)],
            establish_limits=processes.append, expected_ready=READY, timeout=2,
            expected_observation=decode_vulkan_observations(json.dumps(memory).encode(), observed_at=0)[0])
    assert processes[0].poll() is not None


def test_same_process_but_independent_instance_memory_is_not_accepted():
    processes = []
    memory = memory_document()
    memory["query_scope"] = "independent_instance"
    with pytest.raises(WorkerProtocolError, match="observation"):
        ResidentWhisperWorker([sys.executable, "-u", "-c", PROGRAM, "ok", json.dumps(memory)],
            establish_limits=processes.append, expected_ready=READY, timeout=2,
            expected_observation=decode_vulkan_observations(json.dumps(memory).encode(), observed_at=0)[0])
    assert processes[0].poll() is not None


def test_two_requests_keep_actual_child_until_clean_release():
    worker, process = start()
    try:
        progress = []
        assert transcribe(worker, progress=progress.append)["text"] == " example"
        assert transcribe(worker)["segments"][0]["end"] == 1
        assert progress == [0]
        assert worker.model_is_loaded and not worker.release_confirmed
    finally:
        worker.unload_model()
    assert_stopped(worker, process)
    assert process.returncode == 0
    worker.unload_model()  # idempotent, no second release/pipe operation


@pytest.mark.parametrize("mode", ["wrong_device", "wrong_model", "cpu", "bad_ready", "duplicate", "utf8", "partial", "oversize"])
def test_invalid_ready_terminates_owned_process(mode, monkeypatch):
    monkeypatch.setattr(worker_module, "MAX_RESULT_BYTES", 32768)
    processes = []
    with pytest.raises(WorkerProtocolError):
        ResidentWhisperWorker([sys.executable, "-u", "-c", PROGRAM, mode],
            establish_limits=processes.append, expected_ready=READY, timeout=2)
    assert len(processes) == 1 and processes[0].poll() is not None


@pytest.mark.parametrize("mode", ["wrong_request", "bool_request", "bad_progress", "exit", "error"])
def test_inference_protocol_fault_releases_without_returning_result(mode):
    worker, process = start(mode)
    with pytest.raises(WorkerProtocolError) as error:
        transcribe(worker)
    assert "private data" not in str(error.value)
    assert_stopped(worker, process)


def test_cancel_during_chunk_then_new_worker_can_recover():
    worker, process = start("hang")
    cancel = threading.Event()
    with pytest.raises(WorkerCancelled):
        transcribe(worker, cancel=cancel, progress=lambda percent: cancel.set())
    assert_stopped(worker, process)
    replacement, replacement_process = start()
    try:
        assert replacement.ready == worker.ready
        assert transcribe(replacement)["text"] == " example"
    finally:
        replacement.unload_model()
    assert_stopped(replacement, replacement_process)


def test_owner_pressure_callback_exception_is_preserved_after_verified_kill():
    worker, process = start("hang")
    pressure = RuntimeError("owner pressure yield")
    def on_progress(_percent):
        raise pressure
    with pytest.raises(RuntimeError) as error:
        transcribe(worker, progress=on_progress)
    assert error.value is pressure
    assert_stopped(worker, process)


def test_chunk_deadline_stops_child():
    worker, process = start("hang")
    with pytest.raises(TimeoutError):
        worker.transcribe("fixture", duration_seconds=1, language="en", translate=False, timeout=.15)
    assert_stopped(worker, process)


def test_load_deadline_covers_nonresponsive_pipe_child():
    processes = []
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        ResidentWhisperWorker([sys.executable, "-u", "-c", PROGRAM, "no_read"],
            establish_limits=processes.append, expected_ready=READY, timeout=.15)
    assert processes[0].poll() is not None and time.monotonic() - started < 4


def test_deadline_covers_blocked_command_writer():
    worker, process = start("no_more_reads")
    with pytest.raises(TimeoutError):
        worker.transcribe("x" * 15000, duration_seconds=1, language="en", translate=False, timeout=.15)
    assert_stopped(worker, process)


def test_unverified_termination_never_releases_reservation(monkeypatch):
    worker, process = start()
    original_wait = process.wait
    def unverified(*args, **kwargs):
        raise subprocess.TimeoutExpired("owned-worker", 5)
    monkeypatch.setattr(process, "wait", unverified)
    try:
        with pytest.raises(WorkerProtocolError, match="keep its reservation"):
            worker._terminate()
        assert not worker.release_confirmed
    finally:
        monkeypatch.setattr(process, "wait", original_wait)
        worker._terminate()
    assert_stopped(worker, process)


@pytest.mark.parametrize("duration", [0, 1811, float("nan"), True])
def test_unbounded_audio_request_is_rejected_and_released(duration):
    worker, process = start()
    with pytest.raises(ValueError, match="bounded chunk"):
        worker.transcribe("fixture", duration_seconds=duration, language="en", translate=False, timeout=1)
    assert_stopped(worker, process)


@pytest.mark.parametrize("mode", ["no_release", "release_hang", "release_exit"])
def test_release_requires_receipt_and_successful_exit(mode):
    worker, process = start(mode)
    with pytest.raises((WorkerProtocolError, TimeoutError)):
        worker.unload_model(timeout=.15)
    assert_stopped(worker, process)


def test_stderr_flood_is_drained_with_bounded_retention():
    worker, process = start("stderr")
    try:
        assert transcribe(worker)["text"]
        assert len(worker._stderr_tail) <= 65536
    finally:
        worker.unload_model()
    assert_stopped(worker, process)


def test_limit_owner_failure_prevents_load_and_kills_child():
    processes = []
    def denied(process):
        processes.append(process)
        raise RuntimeError("limit establishment failed")
    with pytest.raises(RuntimeError, match="limit establishment failed"):
        ResidentWhisperWorker([sys.executable, "-u", "-c", PROGRAM, "ok"],
            establish_limits=denied, expected_ready=READY, timeout=1)
    assert processes[0].poll() is not None


def test_concurrent_operation_refused_without_aborting_active_owner():
    worker, process = start()
    worker._busy.acquire()
    try:
        with pytest.raises(WorkerProtocolError, match="active operation"):
            transcribe(worker)
        with pytest.raises(WorkerProtocolError, match="Cancel"):
            worker.unload_model()
        assert worker.model_is_loaded
    finally:
        worker._busy.release()
        worker.unload_model()
    assert_stopped(worker, process)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_invalid_deadline_does_not_launch(timeout):
    with pytest.raises(ValueError, match="timeout"):
        ResidentWhisperWorker(["must-not-launch"], establish_limits=lambda process: None,
                              expected_ready=READY, timeout=timeout)


def test_native_artifact_checked_before_spawn_and_again_after_ready(tmp_path, monkeypatch):
    model = tmp_path / "model.bin"
    model.write_bytes(b"weights")
    identity = ModelArtifactIdentity("base.en", "ggml", "float16",
        "sha256:" + hashlib.sha256(b"weights").hexdigest(), 7)
    actual_popen = subprocess.Popen
    launched = []
    def spawn(_command, **options):
        process = actual_popen([sys.executable, "-u", "-c", PROGRAM, "ok"], **options)
        launched.append(process)
        return process
    monkeypatch.setattr(worker_module.subprocess, "Popen", spawn)
    command = ["fixture-native-worker", str(model), "0", "1"]
    worker = ResidentWhisperWorker(command, establish_limits=lambda p: None,
        expected_ready=READY, timeout=2, model_artifact_identity=identity)
    assert worker.model_artifact_identity == identity
    assert worker.ready['model_ftype'] == 1
    worker.unload_model()
    assert_stopped(worker, launched[0])

    model.write_bytes(b"changed")
    with pytest.raises(ArtifactValidationError, match="digest_mismatch"):
        ResidentWhisperWorker(command, establish_limits=lambda p: None,
            expected_ready=READY, timeout=2, model_artifact_identity=identity)
    assert len(launched) == 1, "Wrong artifact must be refused before spawn"

    model.write_bytes(b"weights")
    def replace_during_load(process):
        model.write_bytes(b"changed")
    with pytest.raises(ArtifactValidationError, match="digest_mismatch"):
        ResidentWhisperWorker(command, establish_limits=replace_during_load,
            expected_ready=READY, timeout=2, model_artifact_identity=identity)
    assert launched[-1].poll() is not None


def test_model_family_receipt_must_match_artifact_before_spawn(tmp_path):
    identity = ModelArtifactIdentity("medium", "ggml", "float16", "sha256:" + "a" * 64, 7)
    with pytest.raises(ValueError, match="receipt contradicts"):
        ResidentWhisperWorker(["must-not-launch", str(tmp_path / "weights.bin"), "0", "1"],
            establish_limits=lambda p: None, expected_ready=READY, timeout=1,
            model_artifact_identity=identity)


@pytest.mark.parametrize("mode", ["wrong_ftype", "missing_ftype", "bool_ftype"])
def test_bound_model_requires_actual_weight_format_receipt(tmp_path, monkeypatch, mode):
    model = tmp_path / "model.bin"
    model.write_bytes(b"weights")
    identity = ModelArtifactIdentity("base.en", "ggml", "float16",
        "sha256:" + hashlib.sha256(b"weights").hexdigest(), 7)
    actual_popen, launched = subprocess.Popen, []
    def spawn(_command, **options):
        process = actual_popen([sys.executable, "-u", "-c", PROGRAM, mode], **options)
        launched.append(process)
        return process
    monkeypatch.setattr(worker_module.subprocess, "Popen", spawn)
    with pytest.raises(WorkerProtocolError, match="weight format contradicts"):
        ResidentWhisperWorker(["fixture-native-worker", str(model), "0", "1"],
            establish_limits=lambda p: None, expected_ready=READY, timeout=2,
            model_artifact_identity=identity)
    assert launched[0].poll() is not None


def native_manifest(tmp_path):
    result = {}
    for component in ("worker", "whisper", "ggml-base", "ggml-vulkan"):
        path = tmp_path / (component + ".bin")
        content = component.encode()
        path.write_bytes(content)
        result[str(path)] = NativeArtifactIdentity(component,
            "sha256:" + hashlib.sha256(content).hexdigest(), len(content))
    return result


@pytest.mark.parametrize("change", ["none", "missing", "wrong", "duplicate", "relative", "shadow"])
def test_native_inventory_is_matched_before_child_paths_are_trusted(tmp_path, change):
    manifest = worker_module._native_manifest([str(tmp_path / "worker.bin")], native_manifest(tmp_path))
    observed = list(manifest)
    if change == "missing": observed = None
    if change == "wrong": observed[0] += ".wrong"
    if change == "duplicate": observed.append(observed[0])
    if change == "relative": observed[0] = "relative-library.bin"
    if change == "shadow": observed.append(str(tmp_path / "shadow" / "worker.bin"))
    if change == "none":
        worker_module._confirm_native_modules({"runtime_modules": observed}, manifest)
    else:
        with pytest.raises(WorkerProtocolError):
            worker_module._confirm_native_modules({"runtime_modules": observed}, manifest)


@pytest.mark.parametrize("wrong_inventory", [False, True])
def test_provisioned_native_files_bound_to_actual_ready_inventory(tmp_path, monkeypatch, wrong_inventory):
    manifest = native_manifest(tmp_path)
    model = tmp_path / "model.bin"
    model.write_bytes(b"weights")
    identity = ModelArtifactIdentity("base.en", "ggml", "float16",
        "sha256:" + hashlib.sha256(b"weights").hexdigest(), 7)
    actual_popen, launched = subprocess.Popen, []
    observed = list(manifest)
    if wrong_inventory: observed[0] += ".wrong"
    def spawn(_command, **options):
        process = actual_popen([sys.executable, "-u", "-c", PROGRAM, "ok", "null", json.dumps(observed)], **options)
        launched.append(process)
        return process
    monkeypatch.setattr(worker_module.subprocess, "Popen", spawn)
    def load():
        return ResidentWhisperWorker([str(tmp_path / "worker.bin"), str(model), "0", "1"],
            establish_limits=lambda p: None, expected_ready=READY, timeout=2,
            model_artifact_identity=identity, runtime_artifacts=manifest)
    if wrong_inventory:
        with pytest.raises(WorkerProtocolError, match="different provisioned runtime"):
            load()
    else:
        worker = load()
        assert {item.component for item in worker.runtime_artifact_identities} == {
            "worker", "whisper", "ggml-base", "ggml-vulkan"}
        assert "runtime_modules" not in worker.ready  # No private paths in routine receipts.
        worker.unload_model()
        assert_stopped(worker, launched[0])
    assert launched[0].poll() is not None
    (tmp_path / "worker.bin").write_bytes(b"WRONG!")
    with pytest.raises(ArtifactValidationError):
        load()
    assert len(launched) == 1, "Incorrect native bytes must not reach process creation"
