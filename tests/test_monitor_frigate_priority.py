import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import threading
import time

import pytest

import monitor_frigate_priority as monitor
from subgen_core.priority_pressure import PriorityPressureReader, PrioritySignalSnapshot


BOOT_ID = "123e4567-e89b-42d3-a456-426614174000"
GPU_UUID = "GPU-123e4567-e89b-42d3-a456-426614174000"


def policy_document(**overrides):
    value = {
        "schema": 1,
        "frigate_version": "0.17.2",
        "detection_fps_limit": 80.0,
        "source_max_age_seconds": 30,
        "cameras": {"camera_a": 10.0, "camera_b": 10.0},
        "detectors": ["detector_a"],
        "required_embedding_speeds": ["embed_required"],
        "conditional_embedding_pairs": [["embed_activity", "embed_speed"]],
        "frigate_config_sha256": "3" * 64,
        "gpu_uuid": GPU_UUID,
        "nvidia_driver_version": "610.88",
        "gpu_index": 0,
    }
    value.update(overrides)
    raw = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")
    return value, raw, hashlib.sha256(raw).hexdigest()


def make_policy(**overrides):
    _value, raw, digest = policy_document(**overrides)
    return monitor.parse_priority_policy(raw, digest)


def make_stats(
    generation=1,
    *,
    detection_fps=10.0,
    process_a=10.0,
    process_b=10.0,
    skipped_a=0.0,
    skipped_b=0.0,
    detector_speed=5.0,
    required_speed=4.0,
    conditional=(3.0, 2.0),
):
    embeddings = {"embed_required": required_speed}
    if conditional is not None:
        embeddings.update(
            {"embed_activity": conditional[0], "embed_speed": conditional[1]}
        )
    return {
        "service": {"last_updated": generation},
        "detection_fps": detection_fps,
        "cameras": {
            "camera_a": {"process_fps": process_a, "skipped_fps": skipped_a},
            "camera_b": {"process_fps": process_b, "skipped_fps": skipped_b},
        },
        "detectors": {"detector_a": {"inference_speed": detector_speed}},
        "embeddings": embeddings,
    }


def make_source(generation=1, **stats_overrides):
    policy = make_policy()
    nvidia = monitor.NvidiaObservation(0, GPU_UUID, "610.88", "Default")
    return monitor.normalize_frigate_source(
        make_stats(generation, **stats_overrides), "0.17.2", nvidia, policy
    )


def test_configuration_defaults_are_explicit_and_private_inputs_are_required():
    environment = {
        "FRIGATE_PRIORITY_POLICY_FILE": "/private/policy.json",
        "FRIGATE_CONFIG_FILE": "/private/config.yml",
        "FRIGATE_PRIORITY_POLICY_SHA256": "a" * 64,
    }

    config = monitor.ProducerConfig.from_environment(environment)

    assert config.signal_file == "/run/subgen-priority/pressure.json"
    assert config.frigate_origin.port == 5000
    assert config.ollama_origin.port == 11434
    with pytest.raises(ValueError, match="FRIGATE_PRIORITY_POLICY_FILE"):
        monitor.ProducerConfig.from_environment({})


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.1:5000",
        "http://localhost:5000",
        "http://[::1]:5000",
        "http://user@127.0.0.1:5000",
        "http://127.0.0.1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:5000/",
        "http://127.0.0.1:5000?query",
        "http://127.0.0.1:5000#fragment",
    ],
)
def test_origin_is_confined_to_literal_loopback_http(origin):
    with pytest.raises(ValueError):
        monitor.parse_loopback_origin(origin)


def test_policy_parser_accepts_only_exact_canonical_typed_schema():
    value, raw, digest = policy_document()
    parsed = monitor.parse_priority_policy(raw, digest)

    assert parsed.camera_map == value["cameras"]
    assert parsed.sha256 == digest

    malformed = [
        raw[:-1],
        raw + b"\n",
        raw.replace(b'"schema":1', b'"schema":true'),
        raw.replace(b'"detection_fps_limit":80.0', b'"detection_fps_limit":80'),
        raw.replace(b'"schema":1', b'"extra":1,"schema":1'),
        raw.replace(b'"schema":1', b'"schema":1,"schema":1'),
    ]
    for item in malformed:
        with pytest.raises((ValueError, json.JSONDecodeError)):
            monitor.parse_priority_policy(item, hashlib.sha256(item).hexdigest())
    with pytest.raises(ValueError, match="hash"):
        monitor.parse_priority_policy(raw, "f" * 64)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"source_max_age_seconds": True}, "source age"),
        ({"gpu_index": 32}, "GPU index"),
        ({"frigate_version": "bad version"}, "version"),
        ({"cameras": {}}, "cameras"),
        ({"cameras": {"camera_a": 10}}, "JSON float"),
        ({"detectors": ["z", "a"]}, "sorted"),
        ({"required_embedding_speeds": ["a", "a"]}, "sorted"),
        ({"conditional_embedding_pairs": [["z", "a"]]}, "conditional pair"),
        ({"gpu_uuid": "GPU-UPPER"}, "GPU UUID"),
    ],
)
def test_policy_schema_rejects_every_type_order_and_range_drift(overrides, message):
    _value, raw, digest = policy_document(**overrides)
    with pytest.raises(ValueError, match=message):
        monitor.parse_priority_policy(raw, digest)


def test_nvidia_parser_binds_identity_and_classifies_compute_mode(monkeypatch):
    policy = make_policy()
    healthy = monitor.parse_nvidia_output(
        f"0, {GPU_UUID}, 610.88, Default\n".encode(), policy
    )
    degraded = monitor.parse_nvidia_output(
        f"0, {GPU_UUID}, 610.88, Exclusive_Process\n".encode(), policy
    )

    assert healthy.compute_mode == "Default"
    assert degraded.compute_mode == "Exclusive_Process"
    with pytest.raises(monitor.PolicyDrift):
        monitor.parse_nvidia_output(
            f"1, {GPU_UUID}, 610.88, Default\n".encode(), policy
        )
    for raw in (b"", b"0, only-two\n", b"0, a, b, c\nsecond\n", b"\xff"):
        with pytest.raises(monitor.SourceUnavailable):
            monitor.parse_nvidia_output(raw, policy)

    captured = {}

    def command(argv):
        captured["argv"] = argv
        return f"0, {GPU_UUID}, 610.88, Default\n".encode()

    monkeypatch.setattr(monitor, "_bounded_command", command)
    assert monitor.probe_nvidia(policy) == healthy
    assert captured["argv"] == [
        "nvidia-smi",
        "--id=0",
        "--query-gpu=index,uuid,driver_version,compute_mode",
        "--format=csv,noheader,nounits",
    ]


def test_frigate_normalization_enforces_exact_topology_and_numeric_types():
    policy = make_policy()
    nvidia = monitor.NvidiaObservation(0, GPU_UUID, "610.88", "Default")
    source = monitor.normalize_frigate_source(make_stats(), "0.17.2", nvidia, policy)
    assert source.generation == 1
    assert source.ratios(policy) == (1.0, 1.0)

    wrong_camera = make_stats()
    wrong_camera["cameras"]["extra"] = {}
    with pytest.raises(monitor.PolicyDrift, match="camera"):
        monitor.normalize_frigate_source(wrong_camera, "0.17.2", nvidia, policy)
    with pytest.raises(monitor.PolicyDrift, match="version"):
        monitor.normalize_frigate_source(make_stats(), "0.17.1", nvidia, policy)
    for value in (True, "10", -1, float("inf")):
        invalid = make_stats(process_a=value)
        with pytest.raises(monitor.SourceUnavailable):
            monitor.normalize_frigate_source(invalid, "0.17.2", nvidia, policy)


def test_embedding_zero_is_degraded_but_missing_or_half_conditional_is_unavailable():
    zero = make_source(required_speed=0.0)
    assert zero.has_stalled_worker is True

    policy = make_policy()
    nvidia = monitor.NvidiaObservation(0, GPU_UUID, "610.88", "Default")
    missing = make_stats()
    del missing["embeddings"]["embed_required"]
    half = make_stats()
    del half["embeddings"]["embed_speed"]
    for payload in (missing, half):
        with pytest.raises(monitor.SourceUnavailable):
            monitor.normalize_frigate_source(payload, "0.17.2", nvidia, policy)
    idle = make_source(conditional=None)
    assert ("embed_activity", None) in idle.embeddings


def test_ollama_uses_loaded_models_only_and_never_requires_names():
    assert monitor.classify_ollama({"models": []}) is False
    assert monitor.classify_ollama({"models": [{}]}) is True
    for value in ({}, {"models": "installed"}, {"models": ["name"]}):
        with pytest.raises(monitor.SourceUnavailable):
            monitor.classify_ollama(value)


def test_distinct_generation_thresholds_and_deadband_are_exact():
    policy = make_policy()
    evaluator = monitor.FrigatePriorityEvaluator()

    high_one = evaluator.observe(
        make_source(1, detection_fps=80.0),
        policy,
        observed_ns=1,
        ollama_busy=False,
    )
    high_two = evaluator.observe(
        make_source(2, detection_fps=80.0),
        policy,
        observed_ns=2,
        ollama_busy=False,
    )
    assert high_one == monitor.SourceDecision.neutral()
    assert high_two.reason_codes == ("higher_priority_busy",)

    evaluator = monitor.FrigatePriorityEvaluator()
    low_one = evaluator.observe(
        make_source(1, process_a=9.4), policy, observed_ns=1, ollama_busy=False
    )
    low_two = evaluator.observe(
        make_source(2, process_a=10.0, process_b=9.4),
        policy,
        observed_ns=2,
        ollama_busy=False,
    )
    assert low_one == monitor.SourceDecision.neutral()
    assert low_two.reason_codes == ("higher_priority_degraded",)

    evaluator = monitor.FrigatePriorityEvaluator()
    deadband = evaluator.observe(
        make_source(1, process_a=9.7), policy, observed_ns=1, ollama_busy=False
    )
    clear = evaluator.observe(
        make_source(2, process_a=9.8), policy, observed_ns=2, ollama_busy=False
    )
    assert deadband == monitor.SourceDecision.neutral()
    assert clear == monitor.SourceDecision.clear()


def test_immediate_degraded_conditions_and_sorted_reason_union():
    policy = make_policy()
    evaluator = monitor.FrigatePriorityEvaluator()
    decision = evaluator.observe(
        make_source(1, skipped_a=0.1, detector_speed=0.0),
        policy,
        observed_ns=1,
        ollama_busy=True,
    )
    assert decision.reason_codes == (
        "higher_priority_busy",
        "higher_priority_degraded",
    )


def test_ollama_unavailable_preserves_immediate_degraded_reason_on_duplicates():
    policy = make_policy()
    evaluator = monitor.FrigatePriorityEvaluator()
    source = make_source(1, skipped_a=0.1)

    unavailable = evaluator.observe(
        source,
        policy,
        observed_ns=1,
        ollama_busy=False,
        ollama_unavailable=True,
    )
    recovered_duplicate = evaluator.observe(
        source,
        policy,
        observed_ns=2,
        ollama_busy=False,
    )

    assert unavailable.reason_codes == (
        "higher_priority_degraded",
        "higher_priority_unavailable",
    )
    assert recovered_duplicate.reason_codes == ("higher_priority_degraded",)


def test_invalid_source_and_current_ollama_busy_reasons_are_unioned():
    policy = make_policy()
    evaluator = monitor.FrigatePriorityEvaluator()
    source = make_source(5, skipped_a=0.1)
    evaluator.observe(source, policy, observed_ns=1, ollama_busy=False)

    decision = evaluator.observe(
        make_source(5, skipped_a=0.2),
        policy,
        observed_ns=2,
        ollama_busy=True,
    )

    assert decision.reason_codes == (
        "higher_priority_busy",
        "higher_priority_degraded",
        "higher_priority_unavailable",
    )


def test_duplicate_source_never_advances_streak_but_current_ollama_asserts():
    policy = make_policy()
    evaluator = monitor.FrigatePriorityEvaluator()
    source = make_source(1, detection_fps=80.0)
    first = evaluator.observe(source, policy, observed_ns=1, ollama_busy=False)
    duplicate = evaluator.observe(source, policy, observed_ns=2, ollama_busy=False)
    ollama = evaluator.observe(source, policy, observed_ns=3, ollama_busy=True)
    next_generation = evaluator.observe(
        make_source(2, detection_fps=80.0),
        policy,
        observed_ns=4,
        ollama_busy=False,
    )

    assert first == duplicate == monitor.SourceDecision.neutral()
    assert ollama.reason_codes == ("higher_priority_busy",)
    assert next_generation.reason_codes == ("higher_priority_busy",)


def test_duplicate_clear_remains_clear_and_mutation_or_regression_fails_closed():
    policy = make_policy()
    evaluator = monitor.FrigatePriorityEvaluator()
    source = make_source(5)
    assert evaluator.observe(
        source, policy, observed_ns=10, ollama_busy=False
    ).clear_eligible
    duplicate = evaluator.observe(source, policy, observed_ns=11, ollama_busy=False)
    mutated = evaluator.observe(
        make_source(5, process_a=9.9),
        policy,
        observed_ns=12,
        ollama_busy=False,
    )
    regressed = evaluator.observe(
        make_source(4), policy, observed_ns=13, ollama_busy=False
    )

    assert duplicate.clear_eligible is True
    assert mutated.reason_codes == ("higher_priority_unavailable",)
    assert regressed.reason_codes == ("higher_priority_unavailable",)
    assert evaluator.source_generation == 5
    assert evaluator.source_observed_ns == 10


class FakeSignals:
    def __init__(self):
        self.prepared = False
        self.closed = False
        self.payloads = []

    def prepare(self):
        self.prepared = True

    def publish(self, payload):
        self.payloads.append(payload)

    def close(self):
        self.closed = True


class FakePolicyStore:
    def __init__(self, values):
        self.values = iter(values)

    def load(self):
        value = next(self.values)
        if isinstance(value, BaseException):
            raise value
        return value


class FakeHttp:
    def __init__(self, stats, *, version="0.17.2", ollama=None):
        self.stats = stats
        self.version = version
        self.ollama = {"models": []} if ollama is None else ollama
        self.paths = []

    @staticmethod
    def _resolve(value):
        if isinstance(value, BaseException):
            raise value
        return value

    def get_json(self, _origin, path, *, maximum, deadline=None):
        self.paths.append((path, maximum))
        return self._resolve(self.stats if path == "/api/stats" else self.ollama)

    def get_version(self, _origin, *, deadline=None):
        self.paths.append(("/api/version", None))
        return self._resolve(self.version)


def make_config(policy_sha):
    return monitor.ProducerConfig(
        signal_file="/run/subgen-priority/pressure.json",
        policy_file="/private/policy.json",
        frigate_config_file="/private/config.yml",
        expected_policy_sha256=policy_sha,
        frigate_origin=monitor.LoopbackOrigin(5000),
        ollama_origin=monitor.LoopbackOrigin(11434),
    )


def monitor_clock():
    values = iter(range(8_000_000_000, 20_000_000_000, 100_000_000))
    return lambda: next(values)


def test_monitor_leaves_signal_absent_until_first_valid_source():
    policy = make_policy()
    signals = FakeSignals()
    service = monitor.FrigatePriorityMonitor(
        make_config(policy.sha256),
        uid=1000,
        clock_ns=monitor_clock(),
        http_client=FakeHttp(monitor.SourceUnavailable("missing")),
        nvidia_probe=lambda _policy: monitor.NvidiaObservation(
            0, GPU_UUID, "610.88", "Default"
        ),
        boot_id_reader=lambda: BOOT_ID,
        token_hex=lambda count: "a" * (count * 2),
        policy_store=FakePolicyStore([policy]),
        signal_directory=signals,
    )
    service.start()

    assert service.poll_once() is False
    assert signals.payloads == []
    assert signals.prepared is True


def test_monitor_publication_is_accepted_by_real_consumer_contract():
    policy = make_policy()
    signals = FakeSignals()
    service = monitor.FrigatePriorityMonitor(
        make_config(policy.sha256),
        uid=1000,
        clock_ns=monitor_clock(),
        http_client=FakeHttp(make_stats(10)),
        nvidia_probe=lambda _policy: monitor.NvidiaObservation(
            0, GPU_UUID, "610.88", "Default"
        ),
        boot_id_reader=lambda: BOOT_ID,
        token_hex=lambda count: "a" * (count * 2),
        policy_store=FakePolicyStore([policy]),
        signal_directory=signals,
    )
    service.start()
    assert service.poll_once() is True

    raw = signals.payloads[0]
    snapshot = PrioritySignalSnapshot(
        raw=raw,
        parent_uid=1000,
        parent_mode=0o700,
        file_uid=1000,
        file_mode=0o600,
    )
    reader = PriorityPressureReader(
        "/run/subgen-priority/pressure.json",
        clock_ns=lambda: 10_000_000_000,
        uid_reader=lambda: 1000,
        boot_id_reader=lambda: BOOT_ID,
        snapshot_reader=lambda _path: snapshot,
    )
    accepted = reader.read()
    assert accepted.state == "clear"
    assert accepted.policy_sha256 == policy.sha256


def test_post_source_policy_failure_preserves_generation_and_expected_hash():
    policy = make_policy()
    signals = FakeSignals()
    service = monitor.FrigatePriorityMonitor(
        make_config(policy.sha256),
        uid=1000,
        clock_ns=monitor_clock(),
        http_client=FakeHttp(make_stats(10)),
        nvidia_probe=lambda _policy: monitor.NvidiaObservation(
            0, GPU_UUID, "610.88", "Default"
        ),
        boot_id_reader=lambda: BOOT_ID,
        token_hex=lambda count: "b" * (count * 2),
        policy_store=FakePolicyStore([policy, monitor.PolicyDrift("replacement")]),
        signal_directory=signals,
    )
    service.start()
    service.poll_once()
    service.poll_once()

    first = json.loads(signals.payloads[0])
    second = json.loads(signals.payloads[1])
    assert second["source_generation"] == first["source_generation"] == 10
    assert (
        second["source_observed_monotonic_ns"] == first["source_observed_monotonic_ns"]
    )
    assert second["policy_sha256"] == policy.sha256
    assert second["reason_codes"] == ["policy_drift"]


def test_numeric_overflow_publishes_immediate_unavailable_from_last_valid_source():
    policy = make_policy()
    signals = FakeSignals()
    http = FakeHttp(make_stats(10))
    service = monitor.FrigatePriorityMonitor(
        make_config(policy.sha256),
        uid=1000,
        clock_ns=monitor_clock(),
        http_client=http,
        nvidia_probe=lambda _policy: monitor.NvidiaObservation(
            0, GPU_UUID, "610.88", "Default"
        ),
        boot_id_reader=lambda: BOOT_ID,
        token_hex=lambda count: "c" * (count * 2),
        policy_store=FakePolicyStore([policy, policy]),
        signal_directory=signals,
    )
    service.start()
    assert service.poll_once() is True
    overflow = make_stats(11)
    overflow["detection_fps"] = 10**400
    http.stats = overflow

    assert service.poll_once() is True

    first = json.loads(signals.payloads[0])
    second = json.loads(signals.payloads[1])
    assert second["source_generation"] == first["source_generation"] == 10
    assert (
        second["source_observed_monotonic_ns"] == first["source_observed_monotonic_ns"]
    )
    assert second["reason_codes"] == ["higher_priority_unavailable"]


class FakeSocket:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)


class FakeHeaders:
    def __init__(self, values):
        self.values = values

    def get_all(self, name, default=None):
        value = self.values.get(name)
        if value is None:
            return [] if default is None else default
        return value if isinstance(value, list) else [value]

    def get(self, name, default=None):
        value = self.values.get(name, default)
        return value[0] if isinstance(value, list) else value


class FakeResponse:
    def __init__(self, body, *, status=200, content_type="application/json"):
        self.status = status
        self.body = body
        self.offset = 0
        self.headers = FakeHeaders(
            {"Content-Type": content_type, "Content-Length": str(len(body))}
        )

    def read(self, amount):
        chunk = self.body[self.offset : self.offset + amount]
        self.offset += len(chunk)
        return chunk


class FakeConnection:
    def __init__(self, response):
        self.sock = FakeSocket()
        self.response = response
        self.requested = None
        self.closed = False

    def connect(self):
        return None

    def request(self, method, path, headers):
        self.requested = (method, path, headers)

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def http_client_for(response):
    connection = FakeConnection(response)
    client = monitor.BoundedHttpClient(
        monotonic=lambda: 1.0,
        connection_factory=lambda *_args, **_kwargs: connection,
    )
    return client, connection


def test_http_client_accepts_official_plain_text_version_and_strict_json():
    version_client, version_connection = http_client_for(
        FakeResponse(b"0.17.2", content_type="text/plain; charset=utf-8")
    )
    assert version_client.get_version(monitor.LoopbackOrigin(5000)) == "0.17.2"
    assert version_connection.requested[1] == "/api/version"

    json_client, connection = http_client_for(FakeResponse(b'{"models":[]}'))
    assert json_client.get_json(
        monitor.LoopbackOrigin(11434),
        "/api/ps",
        maximum=monitor.MAX_OLLAMA_BODY_BYTES,
    ) == {"models": []}
    assert connection.requested[1] == "/api/ps"


def test_http_client_rejects_redirect_duplicate_json_and_wrong_content_type():
    redirect, _ = http_client_for(FakeResponse(b"", status=302))
    with pytest.raises(monitor.SourceUnavailable):
        redirect.get_version(monitor.LoopbackOrigin(5000))

    duplicate, _ = http_client_for(FakeResponse(b'{"models":[],"models":[]}'))
    with pytest.raises(monitor.SourceUnavailable):
        duplicate.get_json(
            monitor.LoopbackOrigin(11434),
            "/api/ps",
            maximum=monitor.MAX_OLLAMA_BODY_BYTES,
        )

    wrong_type, _ = http_client_for(
        FakeResponse(b'{"models":[]}', content_type="text/plain")
    )
    with pytest.raises(monitor.SourceUnavailable):
        wrong_type.get_json(
            monitor.LoopbackOrigin(11434),
            "/api/ps",
            maximum=monitor.MAX_OLLAMA_BODY_BYTES,
        )


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_http_total_deadline_interrupts_loopback_trickle(phase, monkeypatch):
    monkeypatch.setattr(monitor, "CONNECT_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(monitor, "READ_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(monitor, "TOTAL_TIMEOUT_SECONDS", 0.25)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve_trickle():
        try:
            connection, _address = listener.accept()
            with connection:
                request = bytearray()
                while b"\r\n\r\n" not in request:
                    chunk = connection.recv(4096)
                    if not chunk:
                        return
                    request.extend(chunk)
                headers = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 100\r\n\r\n"
                )
                if phase == "body":
                    connection.sendall(headers)
                    payload = b"{" + (b" " * 98) + b"}"
                else:
                    payload = headers + b"{}"
                for value in payload:
                    time.sleep(0.04)
                    connection.sendall(bytes((value,)))
        except OSError:
            pass
        finally:
            listener.close()

    server = threading.Thread(target=serve_trickle, daemon=True)
    server.start()
    client = monitor.BoundedHttpClient()
    started = time.monotonic()
    with pytest.raises(monitor.SourceUnavailable):
        client.get_json(
            monitor.LoopbackOrigin(port),
            "/api/stats",
            maximum=monitor.MAX_FRIGATE_BODY_BYTES,
        )
    elapsed = time.monotonic() - started
    server.join(timeout=1)

    assert elapsed < 0.75


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner/mode boundary")
def test_signal_directory_invalidates_safe_old_file_and_publishes_atomically(tmp_path):
    os.chmod(tmp_path, 0o700)
    target = tmp_path / "pressure.json"
    target.write_bytes(b"old")
    os.chmod(target, 0o600)
    boundary = monitor.SignalDirectory(str(target), uid=os.geteuid())

    boundary.prepare()
    assert not target.exists()
    boundary.publish(b"new\n")

    assert target.read_bytes() == b"new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".*.tmp"))
    boundary.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner/mode boundary")
@pytest.mark.parametrize("unsafe_mode", [0o644, 0o400])
def test_signal_directory_leaves_unsafe_old_target_untouched(tmp_path, unsafe_mode):
    os.chmod(tmp_path, 0o700)
    target = tmp_path / "pressure.json"
    target.write_bytes(b"do-not-touch")
    os.chmod(target, unsafe_mode)
    boundary = monitor.SignalDirectory(str(target), uid=os.geteuid())

    with pytest.raises(monitor.FatalBoundaryError):
        boundary.prepare()

    assert target.read_bytes() == b"do-not-touch"


def test_host_monitor_has_no_failure_deletion_or_core_reverse_dependency():
    source = Path(monitor.__file__).read_text(encoding="utf-8")
    assert "monitor_subgen_failures" not in source
    assert "subgen_ops_safety" not in source
    assert "/api/tags" not in source
    core_root = Path(monitor.__file__).parent / "subgen_core"
    assert all(
        "monitor_frigate_priority" not in path.read_text(encoding="utf-8")
        for path in core_root.glob("*.py")
    )
