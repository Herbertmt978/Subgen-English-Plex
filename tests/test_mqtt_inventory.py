import asyncio
from dataclasses import replace
import json
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from language_code import LanguageCode
from subgen_core import media, scanner
from subgen_core.media import (
    AudioTrack,
    MediaOutcome,
    MediaValidation,
    ValidatorEvidence,
    ValidatorOutcome,
)
from subgen_core.mqtt_inventory import (
    DEFAULT_REFRESH_SECONDS,
    DEFAULT_SCAN_TIMEOUT_SECONDS,
    InventoryCoordinator,
    InventoryPublisher,
    MqttInventoryConfig,
    discovery_messages,
    load_mqtt_inventory_config,
    state_message,
)


def enabled_config(**overrides):
    config = MqttInventoryConfig(
        enabled=True,
        host="mqtt.local",
        username="subgen",
        password="secret",
    )
    return replace(config, **overrides)


class RecordingPublisher:
    def __init__(self):
        self.started = 0
        self.stopped = 0
        self.updates = []

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def update(self, snapshot, *, urgent=False):
        self.updates.append((snapshot, urgent))


class NoopObserver:
    def schedule(self, _handler, _path, *, recursive):
        assert recursive is True

    def start(self):
        return None

    def stop(self):
        return None

    def join(self, *, timeout):
        assert timeout == 5.0

    def is_alive(self):
        return False


def test_feature_is_off_by_default_and_refresh_is_sixty_seconds():
    config = MqttInventoryConfig.from_environment({})

    assert config.enabled is False
    assert config.refresh_seconds == DEFAULT_REFRESH_SECONDS == 60.0
    assert config.scan_timeout_seconds == DEFAULT_SCAN_TIMEOUT_SECONDS == 21600.0


def test_enabled_config_validates_topics_without_exposing_credentials():
    config = MqttInventoryConfig.from_environment(
        {
            "MQTT_INVENTORY_ENABLED": "true",
            "MQTT_HOST": "broker.internal",
            "MQTT_PORT": "1884",
            "MQTT_USERNAME": "private-user",
            "MQTT_PASSWORD": "private-password",
            "MQTT_TOPIC_PREFIX": "house/subgen",
            "MQTT_DISCOVERY_PREFIX": "homeassistant",
            "MQTT_INVENTORY_NODE_ID": "subgen_frigate",
            "MQTT_INVENTORY_LIBRARY_NAMES": "Movies|TV",
        }
    )

    assert config.enabled is True
    assert config.port == 1884
    assert config.topic_prefix == "house/subgen"
    assert config.library_names == ("Movies", "TV")
    assert "private-user" not in repr(config)
    assert "private-password" not in repr(config)


def test_invalid_optional_config_disables_mqtt_without_logging_secret():
    logger = MagicMock()
    config = load_mqtt_inventory_config(
        {
            "MQTT_INVENTORY_ENABLED": "true",
            "MQTT_HOST": "",
            "MQTT_PASSWORD": "do-not-log-this",
        },
        logger,
    )

    assert config.enabled is False
    encoded_log = repr(logger.warning.call_args)
    assert "do-not-log-this" not in encoded_log
    assert "transcription will continue" in encoded_log


def test_home_assistant_discovery_has_two_aggregate_retained_sensor_payloads():
    config = enabled_config(node_id="subgen_test", topic_prefix="media/subgen")
    messages = discovery_messages(config)

    assert [topic for topic, _payload in messages] == [
        "homeassistant/sensor/subgen_test/items_left/config",
        "homeassistant/sensor/subgen_test/scan_percent/config",
    ]
    payloads = [json.loads(payload) for _topic, payload in messages]
    assert [payload["name"] for payload in payloads] == [
        "Items Left",
        "Scan %",
    ]
    assert all(payload["device"]["name"] == "Subgen" for payload in payloads)
    assert all(payload["state_topic"] == "media/subgen/inventory/state" for payload in payloads)
    assert all(payload["availability_topic"] == "media/subgen/availability" for payload in payloads)
    assert [payload["object_id"] for payload in payloads] == [
        "subgen_items_left",
        "subgen_scan",
    ]
    assert payloads[1]["unit_of_measurement"] == "%"


def test_coordinator_seeds_snapshot_before_starting_publisher_thread():
    events = []

    class OrderedPublisher(RecordingPublisher):
        def start(self):
            events.append("start")
            super().start()

        def update(self, snapshot, *, urgent=False):
            events.append(("update", snapshot.scan_complete, urgent))
            super().update(snapshot, urgent=urgent)

    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=OrderedPublisher()
    )
    coordinator.arm_scan()
    events.clear()

    coordinator.start()

    assert events[:2] == [("update", False, True), "start"]
    coordinator.stop()


def test_inventory_payload_has_library_summaries_but_never_full_paths():
    publisher = RecordingPublisher()
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=publisher
    )
    labels = coordinator.begin_scan(
        ["/mnt/media/Movies", "/mnt/media/TV"],
        mapped_paths=["/media/Movies", "/media/TV"],
        library_names=["Movies", "TV"],
    )
    coordinator.set_library_total(labels[0], 3)
    coordinator.set_library_total(labels[1], 7)
    coordinator.record_scanned(labels[0], 2)
    coordinator.mark_item_queued("/media/Movies/Private Film Name.mkv")
    coordinator.mark_item_queued("/media/TV/Private Show/S01E01.mkv")

    snapshot = coordinator.snapshot()
    payload = state_message(snapshot)
    decoded = json.loads(payload)

    assert snapshot.items_left == 2
    assert snapshot.scan_percent == 20.0
    assert set(decoded["libraries"]) == {"Movies", "TV"}
    assert decoded["libraries"]["Movies"] == {
        "items_left": 1,
        "scanned": 2,
        "total": 3,
    }
    assert "/mnt/media" not in payload
    assert "/media/" not in payload
    assert "Private Film Name" not in payload
    assert "Private Show" not in payload


def test_duplicate_library_names_are_disambiguated_without_paths():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )

    labels = coordinator.begin_scan(
        ["/first/TV", "/second/TV"],
        library_names=["TV", "TV"],
    )

    assert labels == ("TV", "TV (2)")
    assert [item.name for item in coordinator.snapshot().libraries] == [
        "TV",
        "TV (2)",
    ]


def test_long_duplicate_library_names_keep_a_visible_unique_suffix():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    long_name = "T" * 100

    labels = coordinator.begin_scan(
        [f"/first/{long_name}", f"/second/{long_name}"],
        library_names=[long_name, long_name],
    )

    assert len(labels) == 2
    assert labels[0] != labels[1]
    assert labels[1].endswith(" (2)")
    assert all(len(label) <= 80 for label in labels)


def test_library_names_remain_globally_unique_when_a_natural_suffix_collides():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )

    labels = coordinator.begin_scan(
        ["/a/TV", "/b/TV", "/c/TV (2)"],
        library_names=["TV", "TV", "TV (2)"],
    )

    assert labels == ("TV", "TV (2)", "TV (2) (2)")
    assert len(set(labels)) == 3


def test_scan_barrier_opens_only_after_finish_and_failed_scan_fails_open():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    assert coordinator.wait_until_scanned(0) is True

    coordinator.begin_scan(["/media/Movies"])
    assert coordinator.wait_until_scanned(0) is False

    coordinator.record_scan_error()
    coordinator.finish_scan(successful=False)
    snapshot = coordinator.snapshot()
    assert coordinator.wait_until_scanned(0) is True
    assert snapshot.scan_complete is False
    assert snapshot.scan_errors == 1


def test_completed_item_decrements_aggregate_and_library_count():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    coordinator.begin_scan(["/media/Movies"])
    path = "/media/Movies/film.mkv"
    coordinator.mark_item_queued(path)
    coordinator.mark_item_queued(path)

    coordinator.mark_item_completed(path)
    coordinator.mark_item_completed(path)

    snapshot = coordinator.snapshot()
    assert snapshot.items_left == 0
    assert snapshot.libraries[0].items_left == 0


def test_successful_completion_keeps_scanned_media_in_inventory_totals():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    path = "/media/Movies/film.mkv"
    (label,) = coordinator.begin_scan(
        ["/media/Movies"], library_names=["Movies"]
    )
    coordinator.record_counted_item(label, path)
    coordinator.set_library_total(label, 1)
    coordinator.mark_item_queued(path)
    coordinator.record_scanned_item(label, path)
    coordinator.finish_scan()

    coordinator.mark_item_completed(path)

    snapshot = coordinator.snapshot()
    assert snapshot.items_left == 0
    assert [
        (item.total, item.scanned, item.items_left) for item in snapshot.libraries
    ] == [(1, 1, 0)]


def test_deleted_media_is_removed_from_live_inventory_totals():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    path = "/media/Movies/film.mkv"
    (label,) = coordinator.begin_scan(
        ["/media/Movies"], library_names=["Movies"]
    )
    coordinator.record_counted_item(label, path)
    coordinator.set_library_total(label, 1)
    coordinator.mark_item_queued(path)
    coordinator.record_scanned_item(label, path)
    coordinator.finish_scan()

    coordinator.mark_item_removed(path)

    snapshot = coordinator.snapshot()
    assert snapshot.items_left == 0
    assert [
        (item.total, item.scanned, item.items_left) for item in snapshot.libraries
    ] == [(0, 0, 0)]


def test_watcher_and_startup_scan_count_the_same_file_only_once():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    path = "/media/TV/show.mkv"
    (label,) = coordinator.begin_scan(
        ["/media/TV"],
        library_names=["TV"],
    )
    coordinator.record_counted_item(label, path)
    coordinator.set_library_total(label, 1)
    coordinator.mark_item_queued(path, source="runtime")
    coordinator.mark_item_queued(path, source="startup_scan")
    coordinator.record_scanned_item(label, path)
    coordinator.finish_scan()

    snapshot = coordinator.snapshot()
    assert snapshot.items_left == 1
    assert [
        (item.total, item.scanned, item.items_left)
        for item in snapshot.libraries
    ] == [(1, 1, 1)]


def test_queue_between_arm_and_begin_is_rebound_to_its_library():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    path = "/media/Movies/early-webhook.mkv"

    coordinator.arm_scan()
    assert coordinator.mark_item_queued(path, source="runtime") is True
    coordinator.begin_scan(
        ["/host/Movies"],
        mapped_paths=["/media/Movies"],
        library_names=["Movies"],
    )
    coordinator.finish_scan()

    snapshot = coordinator.snapshot()
    assert snapshot.items_left == 1
    assert [(item.name, item.total, item.scanned, item.items_left) for item in snapshot.libraries] == [
        ("Movies", 1, 1, 1)
    ]
    assert sum(item.items_left for item in snapshot.libraries) == snapshot.items_left


def test_runtime_queue_before_count_keeps_published_library_totals_coherent():
    publisher = RecordingPublisher()
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=publisher
    )
    coordinator.begin_scan(["/media/Movies"], library_names=["Movies"])

    assert coordinator.mark_item_queued(
        "/media/Movies/early.mkv",
        source="runtime",
    )

    snapshot = coordinator.snapshot()
    assert snapshot.scan_complete is False
    assert snapshot.scan_percent == 0.0
    assert [
        (item.total, item.scanned, item.items_left)
        for item in snapshot.libraries
    ] == [(1, 0, 1)]
    assert all(
        library.items_left <= library.total
        for published, _urgent in publisher.updates
        for library in published.libraries
    )


def test_idempotent_begin_scan_preserves_prepared_pending_counts():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    begin_kwargs = {
        "mapped_paths": ["/media/Movies"],
        "library_names": ["Movies"],
    }
    coordinator.begin_scan(["/host/Movies"], **begin_kwargs)
    coordinator.mark_item_queued("/media/Movies/early.mkv", source="runtime")

    coordinator.begin_scan(["/host/Movies"], **begin_kwargs)
    coordinator.finish_scan()

    snapshot = coordinator.snapshot()
    assert [(item.name, item.total, item.scanned, item.items_left) for item in snapshot.libraries] == [
        ("Movies", 1, 1, 1)
    ]


def test_concurrent_mutations_cannot_leave_publisher_with_older_snapshot():
    class BlockingPublisher(RecordingPublisher):
        def __init__(self):
            super().__init__()
            self.pause = False
            self.first_entered = threading.Event()
            self.release_first = threading.Event()
            self.calls = 0

        def update(self, snapshot, *, urgent=False):
            self.calls += 1
            if self.pause and self.calls == 1:
                self.first_entered.set()
                assert self.release_first.wait(2.0)
            super().update(snapshot, urgent=urgent)

    publisher = BlockingPublisher()
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=publisher
    )
    coordinator.begin_scan(["/media/Movies"], library_names=["Movies"])
    coordinator.finish_scan()
    publisher.updates.clear()
    publisher.calls = 0
    publisher.pause = True

    first = threading.Thread(
        target=coordinator.mark_item_queued,
        args=("/media/Movies/first.mkv",),
    )
    second_done = threading.Event()

    def queue_second():
        coordinator.mark_item_queued("/media/Movies/second.mkv")
        second_done.set()

    second = threading.Thread(target=queue_second)
    first.start()
    assert publisher.first_entered.wait(2.0)
    second.start()
    assert second_done.wait(0.05) is False
    publisher.release_first.set()
    first.join(timeout=2.0)
    second.join(timeout=2.0)

    assert first.is_alive() is False
    assert second.is_alive() is False
    assert publisher.updates[-1][0].items_left == 2
    assert coordinator.snapshot().items_left == 2


def test_publisher_or_logger_failure_never_escapes_coordinator():
    publisher = MagicMock()
    publisher.update.side_effect = RuntimeError("broker failure")
    logger = MagicMock()
    coordinator = InventoryCoordinator(enabled_config(), logger, publisher=publisher)

    coordinator.begin_scan(["/media/Movies"])
    coordinator.record_scanned("Movies")
    coordinator.finish_scan()

    assert coordinator.wait_until_scanned(0) is True
    assert logger.warning.called


class FakeMqttClient:
    def __init__(self):
        self.on_connect = None
        self.on_disconnect = None
        self.credentials = None
        self.will = None
        self.published = []
        self.disconnected = False

    def username_pw_set(self, username, password):
        self.credentials = (username, password)

    def will_set(self, topic, *, payload, qos, retain):
        self.will = (topic, payload, qos, retain)

    def connect(self, _host, _port, *, keepalive):
        assert keepalive == 60
        self.on_connect(self, None, None, 0)

    def loop_start(self):
        return None

    def loop_stop(self):
        return None

    def publish(self, topic, *, payload, qos, retain):
        self.published.append((topic, payload, qos, retain))

    def disconnect(self):
        self.disconnected = True


def test_loop_start_failure_cleans_up_the_connection_and_client_reference():
    class FailingLoopClient(FakeMqttClient):
        def loop_start(self):
            raise RuntimeError("loop could not start")

    client = FailingLoopClient()
    publisher = InventoryPublisher(
        enabled_config(),
        MagicMock(),
        client_factory=lambda _config: client,
    )

    with pytest.raises(RuntimeError, match="loop could not start"):
        publisher._connect_and_run()

    assert client.disconnected is True
    assert publisher._client is None
    assert publisher._connected.is_set() is False


def test_client_setup_failure_clears_the_failed_client_reference():
    class FailingWillClient(FakeMqttClient):
        def will_set(self, _topic, *, payload, qos, retain):
            raise RuntimeError("will setup failed")

    client = FailingWillClient()
    publisher = InventoryPublisher(
        enabled_config(),
        MagicMock(),
        client_factory=lambda _config: client,
    )

    with pytest.raises(RuntimeError, match="will setup failed"):
        publisher._connect_and_run()

    assert publisher._client is None
    assert publisher._connected.is_set() is False


def test_publisher_sets_lwt_and_retains_discovery_state_and_availability():
    client = FakeMqttClient()
    config = enabled_config(refresh_seconds=0.01)
    publisher = InventoryPublisher(
        config,
        MagicMock(),
        client_factory=lambda _config: client,
    )
    coordinator = InventoryCoordinator(
        config,
        MagicMock(),
        publisher=publisher,
    )
    coordinator.begin_scan(["/media/Movies"])
    coordinator.set_library_total("Movies", 1)
    coordinator.finish_scan()

    publisher.start()
    deadline = time.monotonic() + 2
    while not any(topic.endswith("/inventory/state") for topic, *_ in client.published):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    publisher.stop()

    assert client.will == ("subgen/availability", "offline", 1, True)
    topics = [topic for topic, *_rest in client.published]
    assert "homeassistant/sensor/subgen_inventory/items_left/config" in topics
    assert "homeassistant/sensor/subgen_inventory/scan_percent/config" in topics
    assert "subgen/inventory/state" in topics
    assert topics.count("subgen/availability") >= 2
    assert all(retain is True and qos == 1 for _topic, _payload, qos, retain in client.published)
    assert client.disconnected is True


def test_fast_connack_before_first_snapshot_preserves_discovery_request():
    client = FakeMqttClient()
    loop_claim_checked = threading.Event()
    clock_calls = 0

    def observed_clock():
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls >= 2:
            loop_claim_checked.set()
        return time.monotonic()

    publisher = InventoryPublisher(
        enabled_config(refresh_seconds=60.0),
        MagicMock(),
        client_factory=lambda _config: client,
        clock=observed_clock,
    )

    publisher.start()
    assert loop_claim_checked.wait(2.0)
    publisher.update(
        InventoryCoordinator(
            enabled_config(), MagicMock(), publisher=RecordingPublisher()
        ).snapshot(),
        urgent=True,
    )
    deadline = time.monotonic() + 2.0
    while not any(topic.endswith("/config") for topic, *_rest in client.published):
        assert time.monotonic() < deadline
        threading.Event().wait(0.01)
    publisher.stop()

    discovery_topics = [
        topic for topic, *_rest in client.published if topic.endswith("/config")
    ]
    assert discovery_topics == [
        "homeassistant/sensor/subgen_inventory/items_left/config",
        "homeassistant/sensor/subgen_inventory/scan_percent/config",
    ]


def test_reconnect_during_publish_cannot_erase_required_rediscovery():
    rediscovery_complete = threading.Event()

    class ReconnectingClient(FakeMqttClient):
        reconnected = False

        def publish(self, topic, *, payload, qos, retain):
            super().publish(topic, payload=payload, qos=qos, retain=retain)
            if topic == "subgen/inventory/state" and not self.reconnected:
                self.reconnected = True
                self.on_connect(self, None, None, 0)
            discovery_count = sum(
                published_topic.endswith("/config")
                for published_topic, *_rest in self.published
            )
            if discovery_count >= 4:
                rediscovery_complete.set()

    client = ReconnectingClient()
    publisher = InventoryPublisher(
        enabled_config(refresh_seconds=60.0),
        MagicMock(),
        client_factory=lambda _config: client,
    )
    publisher.update(
        InventoryCoordinator(
            enabled_config(), MagicMock(), publisher=RecordingPublisher()
        ).snapshot(),
        urgent=True,
    )

    publisher.start()
    assert rediscovery_complete.wait(2.0)
    publisher.stop()

    discovery_topics = [
        topic for topic, *_rest in client.published if topic.endswith("/config")
    ]
    assert discovery_topics.count(
        "homeassistant/sensor/subgen_inventory/items_left/config"
    ) == 2
    assert discovery_topics.count(
        "homeassistant/sensor/subgen_inventory/scan_percent/config"
    ) == 2


def test_qos_publish_requires_confirmation_after_bounded_wait():
    class UnconfirmedResult:
        rc = 0

        @staticmethod
        def wait_for_publish(*, timeout):
            assert timeout == 5.0

        @staticmethod
        def is_published():
            return False

    client = MagicMock()
    client.publish.return_value = UnconfirmedResult()

    with pytest.raises(TimeoutError, match="not confirmed"):
        InventoryPublisher._publish_message(
            client,
            "subgen/inventory/state",
            "{}",
            retain=True,
        )


def test_unconfirmed_offline_publish_uses_lwt_close_not_clean_disconnect():
    class Result:
        rc = 0

        def __init__(self, confirmed):
            self.confirmed = confirmed

        @staticmethod
        def wait_for_publish(*, timeout):
            assert timeout == 5.0

        def is_published(self):
            return self.confirmed

    class LwtClient(FakeMqttClient):
        def __init__(self):
            super().__init__()
            self.socket_handle = MagicMock()

        def publish(self, topic, *, payload, qos, retain):
            super().publish(topic, payload=payload, qos=qos, retain=retain)
            return Result(not (topic == "subgen/availability" and payload == "offline"))

        def socket(self):
            return self.socket_handle

    client = LwtClient()
    publisher = InventoryPublisher(
        enabled_config(refresh_seconds=0.01),
        MagicMock(),
        client_factory=lambda _config: client,
    )
    publisher.update(
        InventoryCoordinator(
            enabled_config(), MagicMock(), publisher=RecordingPublisher()
        ).snapshot(),
        urgent=True,
    )

    publisher.start()
    deadline = time.monotonic() + 2.0
    while not any(topic.endswith("/inventory/state") for topic, *_ in client.published):
        assert time.monotonic() < deadline
        threading.Event().wait(0.01)
    publisher.stop()

    assert client.disconnected is False
    client.socket_handle.close.assert_called_once_with()


def test_connection_exception_message_cannot_leak_credentials():
    logger = MagicMock()
    config = enabled_config(refresh_seconds=0.01, password="very-private")

    def failing_factory(_config):
        raise RuntimeError("very-private")

    publisher = InventoryPublisher(config, logger, client_factory=failing_factory)
    publisher.start()
    deadline = time.monotonic() + 2
    while not logger.warning.called:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    publisher.stop()

    assert "very-private" not in repr(logger.warning.call_args_list)


def test_publisher_does_not_hot_spin_while_waiting_for_connack():
    class NoConnackClient(FakeMqttClient):
        def connect(self, _host, _port, *, keepalive):
            assert keepalive == 60

    clock_calls = 0

    def counting_clock():
        nonlocal clock_calls
        clock_calls += 1
        return time.monotonic()

    publisher = InventoryPublisher(
        enabled_config(refresh_seconds=0.05),
        MagicMock(),
        client_factory=lambda _config: NoConnackClient(),
        clock=counting_clock,
    )
    publisher.update(
        InventoryCoordinator(
            enabled_config(), MagicMock(), publisher=RecordingPublisher()
        ).snapshot(),
        urgent=True,
    )

    publisher.start()
    time.sleep(0.12)
    publisher.stop()

    assert clock_calls < 40


def test_stop_keeps_ownership_when_publisher_thread_does_not_terminate():
    logger = MagicMock()
    publisher = InventoryPublisher(enabled_config(), logger)
    stuck_thread = MagicMock()
    stuck_thread.is_alive.return_value = True
    publisher._thread = stuck_thread

    publisher.stop()

    assert publisher._thread is stuck_thread
    stuck_thread.join.assert_called_once_with(timeout=5.0)
    assert "will not be duplicated" in repr(logger.warning.call_args_list)


def test_graceful_stop_waits_for_retained_offline_before_disconnect():
    events = []

    class PublishResult:
        rc = 0

        def __init__(self, topic, payload):
            self.topic = topic
            self.payload = payload

        def wait_for_publish(self, timeout):
            events.append(("wait", self.topic, self.payload, timeout))

    class DeliveryClient(FakeMqttClient):
        def publish(self, topic, *, payload, qos, retain):
            super().publish(topic, payload=payload, qos=qos, retain=retain)
            events.append(("publish", topic, payload))
            return PublishResult(topic, payload)

        def disconnect(self):
            events.append(("disconnect",))
            super().disconnect()

    client = DeliveryClient()
    publisher = InventoryPublisher(
        enabled_config(refresh_seconds=0.01),
        MagicMock(),
        client_factory=lambda _config: client,
    )
    publisher.update(
        InventoryCoordinator(
            enabled_config(), MagicMock(), publisher=RecordingPublisher()
        ).snapshot(),
        urgent=True,
    )
    publisher.start()
    deadline = time.monotonic() + 2
    while not any(
        event[:3] == ("publish", "subgen/availability", "online")
        for event in events
    ):
        assert time.monotonic() < deadline
        time.sleep(0.01)

    publisher.stop()

    offline_publish = max(
        index
        for index, event in enumerate(events)
        if event[:3] == ("publish", "subgen/availability", "offline")
    )
    offline_wait = next(
        index
        for index, event in enumerate(events[offline_publish + 1 :], offline_publish + 1)
        if event[:3] == ("wait", "subgen/availability", "offline")
    )
    disconnect = next(
        index
        for index, event in enumerate(events[offline_wait + 1 :], offline_wait + 1)
        if event[0] == "disconnect"
    )
    assert offline_publish < offline_wait < disconnect


def test_disconnect_failure_force_closes_socket_after_confirmed_offline_publish():
    class DisconnectFailureClient(FakeMqttClient):
        def __init__(self):
            super().__init__()
            self.socket_handle = MagicMock()

        def disconnect(self):
            raise RuntimeError("disconnect failed")

        def socket(self):
            return self.socket_handle

    client = DisconnectFailureClient()
    publisher = InventoryPublisher(
        enabled_config(),
        MagicMock(),
        client_factory=lambda _config: client,
    )
    publisher._stop.set()

    publisher._connect_and_run()

    client.socket_handle.close.assert_called_once_with()
    assert publisher._client is None
    assert publisher._connected.is_set() is False


def test_nonzero_disconnect_result_force_closes_socket():
    class DisconnectFailureClient(FakeMqttClient):
        def __init__(self):
            super().__init__()
            self.socket_handle = MagicMock()

        def disconnect(self):
            return 4

        def socket(self):
            return self.socket_handle

    client = DisconnectFailureClient()
    publisher = InventoryPublisher(
        enabled_config(),
        MagicMock(),
        client_factory=lambda _config: client,
    )
    publisher._stop.set()

    publisher._connect_and_run()

    client.socket_handle.close.assert_called_once_with()
    assert publisher._client is None
    assert publisher._connected.is_set() is False


def test_full_startup_inventory_holds_barrier_until_every_library_is_scanned(
    tmp_path,
):
    movies = tmp_path / "Movies"
    television = tmp_path / "TV"
    movies.mkdir()
    television.mkdir()
    (movies / "film.mkv").touch()
    (movies / "film.en.srt").touch()
    (movies / "poster.jpg").touch()
    (television / "episode-1.mkv").touch()
    (television / "episode-2.mkv").touch()
    coordinator = InventoryCoordinator(
        enabled_config(library_names=("Movies", "TV")),
        MagicMock(),
        publisher=RecordingPublisher(),
    )
    barrier_observations = []

    def queue_file(path, _task_type, _language, **_kwargs):
        barrier_observations.append(coordinator.wait_until_scanned(0))
        coordinator.mark_item_queued(path)
        return True

    runtime = SimpleNamespace(
        os=os,
        SKIP_MARKER=scanner.SKIP_MARKER,
        logging=MagicMock(),
        path_mapping=lambda path: path,
        transcribe_or_translate="translate",
        gen_subtitles_queue=queue_file,
        has_video_extension=lambda name: name.endswith(".mkv"),
        has_audio_extension=lambda _name: False,
        inventory_coordinator=coordinator,
        skip_startup_scan=False,
        monitor=False,
        Observer=NoopObserver,
        NewFileHandler=lambda: object(),
    )

    scanner.transcribe_existing(
        runtime,
        f"{movies}|{television}",
        LanguageCode.NONE,
    )

    snapshot = coordinator.snapshot()
    assert barrier_observations == [False, False, False]
    assert coordinator.wait_until_scanned(0) is True
    assert snapshot.scan_complete is True
    assert snapshot.scan_percent == 100.0
    assert snapshot.items_left == 3
    assert [(item.name, item.total, item.scanned, item.items_left) for item in snapshot.libraries] == [
        ("Movies", 1, 1, 1),
        ("TV", 2, 2, 2),
    ]


def test_direct_file_inventory_uses_a_generic_label_and_never_publishes_filename(
    tmp_path,
):
    media_file = tmp_path / "Private Film Name.mkv"
    media_file.touch()
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    runtime = SimpleNamespace(
        os=os,
        SKIP_MARKER=scanner.SKIP_MARKER,
        logging=MagicMock(),
        path_mapping=lambda path: path,
        transcribe_or_translate="translate",
        gen_subtitles_queue=lambda *_args, **_kwargs: False,
        has_video_extension=lambda name: name.endswith(".mkv"),
        has_audio_extension=lambda _name: False,
        inventory_coordinator=coordinator,
        skip_startup_scan=False,
        monitor=False,
        Observer=NoopObserver,
        NewFileHandler=lambda: object(),
    )

    scanner.transcribe_existing(runtime, str(media_file))

    payload = state_message(coordinator.snapshot())
    assert list(json.loads(payload)["libraries"]) == ["Direct file 1"]
    assert "Private Film Name" not in payload
    assert str(media_file) not in payload


def test_directory_names_are_generic_unless_the_operator_configures_labels(tmp_path):
    private_library = tmp_path / "Private Film Title"
    private_library.mkdir()
    (private_library / "episode.mkv").touch()
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    runtime = SimpleNamespace(
        os=os,
        SKIP_MARKER=scanner.SKIP_MARKER,
        logging=MagicMock(),
        path_mapping=lambda path: path,
        transcribe_or_translate="translate",
        gen_subtitles_queue=lambda *_args, **_kwargs: False,
        has_video_extension=lambda name: name.endswith(".mkv"),
        has_audio_extension=lambda _name: False,
        inventory_coordinator=coordinator,
        skip_startup_scan=False,
        monitor=False,
        Observer=NoopObserver,
        NewFileHandler=lambda: object(),
    )

    scanner.transcribe_existing(runtime, str(private_library))

    payload = state_message(coordinator.snapshot())
    assert list(json.loads(payload)["libraries"]) == ["Library 1"]
    assert "Private Film Title" not in payload


def test_scan_watchdog_releases_workers_and_cancels_a_blocked_walk():
    release_walk = threading.Event()
    coordinator = InventoryCoordinator(
        enabled_config(scan_timeout_seconds=0.05),
        MagicMock(),
        publisher=RecordingPublisher(),
    )

    def blocked_walk(_path, *, onerror=None):
        assert onerror is not None
        release_walk.wait(2)
        yield "/library", [], ["movie.mkv"]

    fake_path = SimpleNamespace(
        isfile=lambda _path: False,
        isdir=lambda _path: True,
        basename=lambda _path: "Movies",
        join=lambda root, name: f"{root}/{name}",
    )
    queue = MagicMock()
    runtime = SimpleNamespace(
        os=SimpleNamespace(path=fake_path, walk=blocked_walk),
        SKIP_MARKER=scanner.SKIP_MARKER,
        logging=MagicMock(),
        path_mapping=lambda path: path,
        transcribe_or_translate="translate",
        gen_subtitles_queue=queue,
        has_video_extension=lambda name: name.endswith(".mkv"),
        has_audio_extension=lambda _name: False,
        inventory_coordinator=coordinator,
        skip_startup_scan=False,
        monitor=False,
        Observer=NoopObserver,
        NewFileHandler=lambda: object(),
    )
    scan_thread = threading.Thread(
        target=scanner.transcribe_existing,
        args=(runtime, "/library"),
        daemon=True,
    )

    scan_thread.start()
    assert coordinator.wait_until_scanned(1.0) is True
    snapshot = coordinator.snapshot()
    assert snapshot.scan_complete is False
    assert snapshot.scan_errors == 1
    assert coordinator.scan_cancelled is True

    release_walk.set()
    scan_thread.join(timeout=2)
    assert scan_thread.is_alive() is False
    queue.assert_not_called()


def test_watcher_starts_before_inventory_and_counts_an_import_scan_will_not_see(
    tmp_path,
):
    library = tmp_path / "Movies"
    library.mkdir()
    existing = library / "existing.mkv"
    existing.touch()
    imported = library / "arr-imported-after-walk.mkv"
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    events = []
    queued = set()

    def queue_file(path, _task_type, _language=LanguageCode.NONE, **kwargs):
        if path in queued:
            return False
        queued.add(path)
        coordinator.mark_item_queued(
            path,
            source=kwargs.get("_inventory_source", "runtime"),
        )
        return True

    def walk(path, **kwargs):
        events.append("walk")
        yield from os.walk(path, **kwargs)

    class Observer:
        def schedule(self, _handler, _path, *, recursive):
            assert recursive is True

        def start(self):
            events.append("observer")
            imported.touch()
            queue_file(str(imported), "translate")

    runtime = SimpleNamespace(
        os=SimpleNamespace(path=os.path, walk=walk),
        SKIP_MARKER=scanner.SKIP_MARKER,
        logging=MagicMock(),
        path_mapping=lambda path: path,
        transcribe_or_translate="translate",
        gen_subtitles_queue=queue_file,
        has_video_extension=lambda name: name.endswith(".mkv"),
        has_audio_extension=lambda _name: False,
        inventory_coordinator=coordinator,
        skip_startup_scan=False,
        monitor=True,
        Observer=Observer,
        NewFileHandler=lambda: object(),
    )

    scanner.transcribe_existing(runtime, str(library))

    snapshot = coordinator.snapshot()
    assert events[0] == "observer"
    assert snapshot.scan_complete is True
    assert snapshot.items_left == 2
    assert [(item.total, item.scanned, item.items_left) for item in snapshot.libraries] == [
        (2, 2, 2)
    ]


def test_watcher_won_import_uses_mapped_identity_without_double_counting(tmp_path):
    library = tmp_path / "Movies"
    library.mkdir()
    media_file = library / "movie.mkv"
    media_file.touch()
    mapped_root = "/media/Movies"
    coordinator = InventoryCoordinator(
        enabled_config(library_names=("Movies",)),
        MagicMock(),
        publisher=RecordingPublisher(),
    )
    queued = set()

    def map_path(path):
        return str(path).replace(str(library), mapped_root)

    def queue_file(path, _task_type, _language=LanguageCode.NONE, **kwargs):
        if path in queued:
            return False
        queued.add(path)
        coordinator.mark_item_queued(
            path,
            source=kwargs.get("_inventory_source", "runtime"),
        )
        return True

    class Observer:
        def schedule(self, _handler, _path, *, recursive):
            assert recursive is True

        def start(self):
            queue_file(map_path(media_file), "translate")

    runtime = SimpleNamespace(
        os=os,
        SKIP_MARKER=scanner.SKIP_MARKER,
        logging=MagicMock(),
        path_mapping=map_path,
        transcribe_or_translate="translate",
        gen_subtitles_queue=queue_file,
        has_video_extension=lambda name: name.endswith(".mkv"),
        has_audio_extension=lambda _name: False,
        inventory_coordinator=coordinator,
        skip_startup_scan=False,
        monitor=True,
        Observer=Observer,
        NewFileHandler=lambda: object(),
    )

    scanner.transcribe_existing(runtime, str(library))

    snapshot = coordinator.snapshot()
    assert snapshot.scan_complete is True
    assert snapshot.items_left == 1
    assert [(item.name, item.total, item.scanned, item.items_left) for item in snapshot.libraries] == [
        ("Movies", 1, 1, 1)
    ]


def test_finish_scan_waits_for_dispatched_created_event_before_opening_barrier():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    coordinator.begin_scan(["/media/Movies"], library_names=["Movies"])
    handler_entered = threading.Event()
    release_handler = threading.Event()
    barrier_observations = []

    def controlled_sleep(seconds):
        assert seconds == 5
        handler_entered.set()
        assert release_handler.wait(2.0)

    def queue_file(path, *_args, **_kwargs):
        barrier_observations.append(coordinator.wait_until_scanned(0))
        coordinator.mark_item_queued(path, source="runtime")
        return True

    runtime = SimpleNamespace(
        time=SimpleNamespace(sleep=controlled_sleep),
        logging=MagicMock(),
        _is_in_skipped_dir=lambda _path: False,
        path_mapping=lambda path: path,
        gen_subtitles_queue=queue_file,
        transcribe_or_translate="translate",
        is_file_stable=lambda _path: True,
        inventory_coordinator=coordinator,
    )
    handler = scanner.NewFileHandler(runtime)
    handler_thread = threading.Thread(
        target=handler.dispatch,
        args=(
            SimpleNamespace(
                event_type="created",
                is_directory=False,
                src_path="/media/Movies/late.mkv",
            ),
        ),
    )
    handler_thread.start()
    assert handler_entered.wait(2.0)

    finish_thread = threading.Thread(target=coordinator.finish_scan)
    finish_thread.start()
    deadline = time.monotonic() + 2.0
    while True:
        probe = coordinator.acquire_scan_event()
        if probe is None:
            break
        coordinator.release_scan_event(probe)
        assert time.monotonic() < deadline
        threading.Event().wait(0.01)

    assert coordinator.wait_until_scanned(0) is False
    assert finish_thread.is_alive() is True
    release_handler.set()
    handler_thread.join(timeout=2.0)
    finish_thread.join(timeout=2.0)

    assert handler_thread.is_alive() is False
    assert finish_thread.is_alive() is False
    assert barrier_observations == [False]
    snapshot = coordinator.snapshot()
    assert [(item.name, item.total, item.scanned, item.items_left) for item in snapshot.libraries] == [
        ("Movies", 1, 1, 1)
    ]


def test_dispatch_releases_scan_lease_when_handler_raises():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    coordinator.begin_scan(["/media/Movies"], library_names=["Movies"])
    runtime = SimpleNamespace(
        time=SimpleNamespace(sleep=lambda _seconds: None),
        logging=MagicMock(),
        _is_in_skipped_dir=lambda _path: False,
        path_mapping=lambda path: path,
        gen_subtitles_queue=MagicMock(side_effect=RuntimeError("boom")),
        transcribe_or_translate="translate",
        is_file_stable=lambda _path: True,
        inventory_coordinator=coordinator,
    )

    with pytest.raises(RuntimeError, match="boom"):
        scanner.NewFileHandler(runtime).dispatch(
            SimpleNamespace(
                event_type="created",
                is_directory=False,
                src_path="/media/Movies/late.mkv",
            )
        )

    coordinator.finish_scan()
    assert coordinator.wait_until_scanned(0) is True
    assert coordinator.snapshot().scan_complete is True


def test_scan_watchdog_fail_opens_with_a_stuck_event_lease():
    coordinator = InventoryCoordinator(
        enabled_config(scan_timeout_seconds=0.05),
        MagicMock(),
        publisher=RecordingPublisher(),
    )
    coordinator.begin_scan(["/media/Movies"], library_names=["Movies"])
    generation = coordinator.acquire_scan_event()
    assert generation is not None

    assert coordinator.wait_until_scanned(1.0) is True
    snapshot = coordinator.snapshot()
    assert snapshot.scan_complete is False
    assert snapshot.scan_errors == 1

    coordinator.release_scan_event(generation)
    assert coordinator.snapshot().scan_complete is False


def test_stale_scan_watchdog_callback_cannot_expire_new_generation():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    coordinator.arm_scan()
    stale_timer = coordinator._scan_watchdog
    stale_generation = coordinator.scan_generation
    coordinator.finish_scan(successful=False, generation=stale_generation)
    coordinator.arm_scan()
    current_generation = coordinator.scan_generation
    assert current_generation != stale_generation

    stale_timer.function(*stale_timer.args, **stale_timer.kwargs)

    assert coordinator.scan_generation == current_generation
    assert coordinator.scan_cancelled is False
    assert coordinator.wait_until_scanned(0) is False
    assert coordinator.snapshot().scan_errors == 0
    coordinator.finish_scan(successful=False, generation=current_generation)


def test_stale_event_lease_cannot_release_a_new_scan_generation():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    coordinator.begin_scan(["/media/Movies"], library_names=["Movies"])
    stale_generation = coordinator.acquire_scan_event()
    coordinator.finish_scan(successful=False)

    coordinator.begin_scan(["/media/Movies"], library_names=["Movies"])
    current_generation = coordinator.acquire_scan_event()
    assert stale_generation is not None
    assert current_generation is not None
    assert current_generation != stale_generation
    coordinator.release_scan_event(stale_generation)

    finished = threading.Event()

    def finish_current_scan():
        coordinator.finish_scan()
        finished.set()

    finish_thread = threading.Thread(target=finish_current_scan)
    finish_thread.start()
    assert finished.wait(0.05) is False
    coordinator.release_scan_event(current_generation)
    assert finished.wait(2.0)
    finish_thread.join(timeout=2.0)

    assert coordinator.snapshot().scan_complete is True


def test_stale_delete_callback_cannot_remove_a_new_scan_replacement():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    path = "/media/Movies/replacement.mkv"
    coordinator.begin_scan(["/media/Movies"], library_names=["Movies"])
    stale_generation = coordinator.scan_generation
    mapping_entered = threading.Event()
    release_mapping = threading.Event()

    def delayed_mapping(value):
        mapping_entered.set()
        assert release_mapping.wait(2.0)
        return value

    runtime = SimpleNamespace(
        logging=MagicMock(),
        inventory_item_removed=coordinator.mark_item_removed,
        inventory_coordinator=coordinator,
        path_mapping=delayed_mapping,
    )
    handler = scanner.NewFileHandler(runtime)
    callback = threading.Thread(
        target=handler.dispatch,
        args=(
            SimpleNamespace(
                event_type="deleted",
                is_directory=False,
                src_path=path,
            ),
        ),
    )
    callback.start()
    assert mapping_entered.wait(2.0)

    coordinator._expire_scan(stale_generation)
    coordinator.arm_scan()
    coordinator.begin_scan(["/media/Movies"], library_names=["Movies"])
    current_generation = coordinator.scan_generation
    assert current_generation != stale_generation
    assert coordinator.mark_item_queued(
        path,
        source="startup_scan",
        generation=current_generation,
    )

    release_mapping.set()
    callback.join(timeout=2.0)

    assert callback.is_alive() is False
    assert coordinator.snapshot().items_left == 1
    coordinator.finish_scan(successful=False, generation=current_generation)


def test_disabled_inventory_dispatch_does_not_use_scan_event_admission():
    coordinator = InventoryCoordinator(
        MqttInventoryConfig(), MagicMock(), publisher=RecordingPublisher()
    )
    coordinator.acquire_scan_event = MagicMock(side_effect=AssertionError("called"))
    queued = MagicMock()
    runtime = SimpleNamespace(
        time=SimpleNamespace(sleep=lambda _seconds: None),
        logging=MagicMock(),
        _is_in_skipped_dir=lambda _path: False,
        path_mapping=lambda path: path,
        gen_subtitles_queue=queued,
        transcribe_or_translate="translate",
        is_file_stable=lambda _path: True,
        inventory_coordinator=coordinator,
    )

    scanner.NewFileHandler(runtime).dispatch(
        SimpleNamespace(
            event_type="created",
            is_directory=False,
            src_path="/watch/new.mkv",
        )
    )

    coordinator.acquire_scan_event.assert_not_called()
    queued.assert_called_once_with("/watch/new.mkv", "translate")


def test_final_cutoff_pass_queues_file_added_after_primary_walk(tmp_path):
    library = tmp_path / "Movies"
    library.mkdir()
    existing = library / "existing.mkv"
    existing.touch()
    late = library / "late.mkv"
    walk_calls = 0

    def changing_walk(path, **kwargs):
        nonlocal walk_calls
        walk_calls += 1
        if walk_calls == 3:
            late.touch()
        yield from os.walk(path, **kwargs)

    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    queued = []

    def queue_file(path, _task_type, _language=LanguageCode.NONE, **kwargs):
        queued.append(path)
        coordinator.mark_item_queued(
            path,
            source=kwargs.get("_inventory_source", "runtime"),
        )
        return True

    runtime = SimpleNamespace(
        os=SimpleNamespace(path=os.path, walk=changing_walk),
        SKIP_MARKER=scanner.SKIP_MARKER,
        logging=MagicMock(),
        path_mapping=lambda path: path,
        transcribe_or_translate="translate",
        gen_subtitles_queue=queue_file,
        has_video_extension=lambda name: name.endswith(".mkv"),
        has_audio_extension=lambda _name: False,
        inventory_coordinator=coordinator,
        skip_startup_scan=False,
        monitor=True,
        Observer=NoopObserver,
        NewFileHandler=lambda: object(),
    )

    scanner.transcribe_existing(runtime, str(library))

    assert walk_calls == 3
    assert queued == [str(existing), str(late)]
    snapshot = coordinator.snapshot()
    assert snapshot.scan_complete is True
    assert snapshot.items_left == 2
    assert [(item.total, item.scanned, item.items_left) for item in snapshot.libraries] == [
        (2, 2, 2)
    ]


def test_file_queued_after_final_snapshot_survives_reconciliation(tmp_path):
    library = tmp_path / "Movies"
    library.mkdir()
    late = library / "late.mkv"
    coordinator = InventoryCoordinator(
        enabled_config(library_names=("Movies",)),
        MagicMock(),
        publisher=RecordingPublisher(),
    )
    queued = []

    def queue_file(path, _task_type, _language=LanguageCode.NONE, **kwargs):
        queued.append(path)
        return coordinator.mark_item_queued(
            path,
            source=kwargs.get("_inventory_source", "runtime"),
            generation=kwargs.get("_inventory_generation"),
        )

    runtime = SimpleNamespace(
        os=os,
        time=SimpleNamespace(sleep=lambda _seconds: None),
        SKIP_MARKER=scanner.SKIP_MARKER,
        logging=MagicMock(),
        _is_in_skipped_dir=lambda _path: False,
        path_mapping=lambda path: path,
        transcribe_or_translate="translate",
        gen_subtitles_queue=queue_file,
        is_file_stable=lambda _path: True,
        has_video_extension=lambda name: name.endswith(".mkv"),
        has_audio_extension=lambda _name: False,
        inventory_coordinator=coordinator,
        skip_startup_scan=False,
        monitor=True,
        Observer=NoopObserver,
    )
    handler = scanner.NewFileHandler(runtime)
    runtime.NewFileHandler = lambda: handler
    reconcile = coordinator.reconcile_final_library
    injected = False

    def reconcile_after_late_arrival(label, paths, *, generation):
        nonlocal injected
        if not injected:
            injected = True
            assert paths == []
            late.touch()
            handler.dispatch(
                SimpleNamespace(
                    event_type="created",
                    is_directory=False,
                    src_path=str(late),
                )
            )
        return reconcile(label, paths, generation=generation)

    coordinator.reconcile_final_library = reconcile_after_late_arrival

    scanner.transcribe_existing(runtime, str(library))

    snapshot = coordinator.snapshot()
    assert queued == [str(late)]
    assert snapshot.scan_complete is True
    assert snapshot.items_left == 1
    assert [
        (item.name, item.total, item.scanned, item.items_left)
        for item in snapshot.libraries
    ] == [("Movies", 1, 1, 1)]


def test_inventory_uses_a_temporary_watcher_when_continuous_monitoring_is_off(
    tmp_path,
):
    library = tmp_path / "Movies"
    library.mkdir()
    existing = library / "existing.mkv"
    existing.touch()
    imported = library / "arr-imported-during-scan.mkv"
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    queued = set()
    observer_events = []

    def queue_file(path, _task_type, _language=LanguageCode.NONE, **kwargs):
        if path in queued:
            return False
        queued.add(path)
        coordinator.mark_item_queued(
            path,
            source=kwargs.get("_inventory_source", "runtime"),
        )
        return True

    class TemporaryObserver:
        def schedule(self, _handler, _path, *, recursive):
            assert recursive is True

        def start(self):
            observer_events.append("start")
            imported.touch()
            queue_file(str(imported), "translate")

        def stop(self):
            observer_events.append("stop")

        def join(self, *, timeout):
            assert timeout == 5.0
            observer_events.append("join")

        def is_alive(self):
            return False

    runtime = SimpleNamespace(
        os=os,
        SKIP_MARKER=scanner.SKIP_MARKER,
        logging=MagicMock(),
        path_mapping=lambda path: path,
        transcribe_or_translate="translate",
        gen_subtitles_queue=queue_file,
        has_video_extension=lambda name: name.endswith(".mkv"),
        has_audio_extension=lambda _name: False,
        inventory_coordinator=coordinator,
        skip_startup_scan=False,
        monitor=False,
        Observer=TemporaryObserver,
        NewFileHandler=lambda: object(),
    )

    scanner.transcribe_existing(runtime, str(library))

    snapshot = coordinator.snapshot()
    assert observer_events == ["start", "stop", "join"]
    assert snapshot.scan_complete is True
    assert snapshot.items_left == 2
    assert [
        (item.total, item.scanned, item.items_left)
        for item in snapshot.libraries
    ] == [(2, 2, 2)]


def test_temporary_watcher_stops_before_the_authoritative_final_walk(tmp_path):
    library = tmp_path / "Movies"
    library.mkdir()
    late = library / "late-during-stop.mkv"
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    queued = []
    observer_events = []

    def queue_file(path, _task_type, _language=LanguageCode.NONE, **kwargs):
        queued.append(path)
        return coordinator.mark_item_queued(
            path,
            source=kwargs.get("_inventory_source", "runtime"),
            generation=kwargs.get("_inventory_generation"),
        )

    class TemporaryObserver:
        def schedule(self, _handler, _path, *, recursive):
            assert recursive is True

        def start(self):
            observer_events.append("start")

        def stop(self):
            observer_events.append("stop")
            late.touch()

        def join(self, *, timeout):
            assert timeout == 5.0
            observer_events.append("join")

        def is_alive(self):
            return False

    runtime = SimpleNamespace(
        os=os,
        SKIP_MARKER=scanner.SKIP_MARKER,
        logging=MagicMock(),
        path_mapping=lambda path: path,
        transcribe_or_translate="translate",
        gen_subtitles_queue=queue_file,
        has_video_extension=lambda name: name.endswith(".mkv"),
        has_audio_extension=lambda _name: False,
        inventory_coordinator=coordinator,
        skip_startup_scan=False,
        monitor=False,
        Observer=TemporaryObserver,
        NewFileHandler=lambda: object(),
    )

    scanner.transcribe_existing(runtime, str(library))

    snapshot = coordinator.snapshot()
    assert observer_events == ["start", "stop", "join"]
    assert queued == [str(late)]
    assert snapshot.scan_complete is True
    assert [
        (item.total, item.scanned, item.items_left)
        for item in snapshot.libraries
    ] == [(1, 1, 1)]


def test_file_removed_between_inventory_passes_is_not_reported_as_unvisited(
    tmp_path,
):
    walk_calls = 0

    def changing_walk(path, **_kwargs):
        nonlocal walk_calls
        walk_calls += 1
        files = ["present.mkv", "removed.mkv"] if walk_calls == 1 else ["present.mkv"]
        yield str(path), [], files

    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    runtime = SimpleNamespace(
        os=SimpleNamespace(path=os.path, walk=changing_walk),
        SKIP_MARKER=scanner.SKIP_MARKER,
        logging=MagicMock(),
        path_mapping=lambda path: path,
        transcribe_or_translate="translate",
        gen_subtitles_queue=lambda *_args, **_kwargs: False,
        has_video_extension=lambda name: name.endswith(".mkv"),
        has_audio_extension=lambda _name: False,
        inventory_coordinator=coordinator,
        skip_startup_scan=False,
        monitor=False,
        Observer=NoopObserver,
        NewFileHandler=lambda: object(),
    )

    scanner.transcribe_existing(runtime, str(tmp_path))

    snapshot = coordinator.snapshot()
    assert snapshot.scan_complete is True
    assert snapshot.scan_percent == 100.0
    assert [
        (item.total, item.scanned, item.items_left)
        for item in snapshot.libraries
    ] == [(1, 1, 0)]


def test_configured_direct_file_removed_before_final_pass_is_not_left_pending(
    tmp_path,
):
    media_file = tmp_path / "direct.mkv"
    media_file.touch()
    isfile_calls = 0

    def changing_isfile(path):
        nonlocal isfile_calls
        assert path == str(media_file)
        isfile_calls += 1
        return isfile_calls < 4

    fake_path = SimpleNamespace(
        basename=os.path.basename,
        isfile=changing_isfile,
        isdir=lambda path: False,
    )
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )

    def queue_file(path, _task_type, _language=LanguageCode.NONE, **kwargs):
        return coordinator.mark_item_queued(
            path,
            source=kwargs.get("_inventory_source", "runtime"),
            generation=kwargs.get("_inventory_generation"),
        )

    runtime = SimpleNamespace(
        os=SimpleNamespace(path=fake_path, walk=MagicMock()),
        SKIP_MARKER=scanner.SKIP_MARKER,
        logging=MagicMock(),
        path_mapping=lambda path: path,
        transcribe_or_translate="translate",
        gen_subtitles_queue=queue_file,
        has_video_extension=lambda name: name.endswith(".mkv"),
        has_audio_extension=lambda _name: False,
        inventory_coordinator=coordinator,
        skip_startup_scan=False,
        monitor=False,
        Observer=NoopObserver,
        NewFileHandler=lambda: object(),
    )

    scanner.transcribe_existing(runtime, str(media_file))

    snapshot = coordinator.snapshot()
    assert isfile_calls == 5
    assert snapshot.scan_complete is False
    assert snapshot.scan_errors == 1
    assert snapshot.items_left == 0
    assert [
        (item.total, item.scanned, item.items_left)
        for item in snapshot.libraries
    ] == [(0, 0, 0)]
    runtime.os.walk.assert_not_called()


def test_queued_file_removed_before_the_final_pass_is_not_left_pending(tmp_path):
    walk_calls = 0

    def changing_walk(path, **_kwargs):
        nonlocal walk_calls
        walk_calls += 1
        files = (
            ["present.mkv", "removed.mkv"]
            if walk_calls < 3
            else ["present.mkv"]
        )
        yield str(path), [], files

    coordinator = InventoryCoordinator(
        enabled_config(library_names=("Movies",)),
        MagicMock(),
        publisher=RecordingPublisher(),
    )
    mapped_root = "/media/Movies"

    def map_path(path):
        return str(path).replace(str(tmp_path), mapped_root)

    def queue_file(path, _task_type, _language=LanguageCode.NONE, **kwargs):
        if not path.endswith("removed.mkv"):
            return False
        return coordinator.mark_item_queued(
            path,
            source=kwargs.get("_inventory_source", "runtime"),
            generation=kwargs.get("_inventory_generation"),
        )

    runtime = SimpleNamespace(
        os=SimpleNamespace(path=os.path, walk=changing_walk),
        SKIP_MARKER=scanner.SKIP_MARKER,
        logging=MagicMock(),
        path_mapping=map_path,
        transcribe_or_translate="translate",
        gen_subtitles_queue=queue_file,
        has_video_extension=lambda name: name.endswith(".mkv"),
        has_audio_extension=lambda _name: False,
        inventory_coordinator=coordinator,
        skip_startup_scan=False,
        monitor=False,
        Observer=NoopObserver,
        NewFileHandler=lambda: object(),
    )

    scanner.transcribe_existing(runtime, str(tmp_path))

    snapshot = coordinator.snapshot()
    assert walk_calls == 3
    assert snapshot.scan_complete is True
    assert snapshot.items_left == 0
    assert [
        (item.name, item.total, item.scanned, item.items_left)
        for item in snapshot.libraries
    ] == [("Movies", 1, 1, 0)]


def test_final_reconciliation_removes_absent_runtime_item_without_generation():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    (label,) = coordinator.begin_scan(
        ["/host/Movies"],
        mapped_paths=["/media/Movies"],
        library_names=["Movies"],
    )
    generation = coordinator.scan_generation
    assert coordinator.mark_item_queued(
        "/media/Movies/removed.mkv",
        source="runtime",
    )

    coordinator.reconcile_final_library(label, [], generation=generation)
    coordinator.finish_scan(generation=generation)

    snapshot = coordinator.snapshot()
    assert snapshot.items_left == 0
    assert [
        (item.total, item.scanned, item.items_left)
        for item in snapshot.libraries
    ] == [(0, 0, 0)]


def test_enabled_inventory_requires_scan_even_when_legacy_skip_is_set(tmp_path):
    media_file = tmp_path / "movie.mkv"
    media_file.touch()
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    queued = []
    runtime = SimpleNamespace(
        os=os,
        SKIP_MARKER=scanner.SKIP_MARKER,
        logging=MagicMock(),
        path_mapping=lambda path: path,
        transcribe_or_translate="translate",
        gen_subtitles_queue=lambda path, *_args, **_kwargs: queued.append(path) or True,
        inventory_coordinator=coordinator,
        skip_startup_scan=True,
        monitor=False,
        Observer=NoopObserver,
        NewFileHandler=lambda: object(),
    )

    scanner.transcribe_existing(runtime, str(tmp_path))

    assert queued == [str(media_file)]
    assert coordinator.snapshot().scan_complete is True
    assert coordinator.wait_until_scanned(0) is True


def test_inventory_walk_failure_opens_barrier_without_raising():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    fake_path = SimpleNamespace(
        isfile=lambda _path: False,
        isdir=lambda _path: False,
    )

    def fail_walk(_path):
        raise OSError("private mount detail")

    runtime = SimpleNamespace(
        os=SimpleNamespace(path=fake_path, walk=fail_walk),
        SKIP_MARKER=scanner.SKIP_MARKER,
        logging=MagicMock(),
        path_mapping=lambda path: path,
        transcribe_or_translate="translate",
        gen_subtitles_queue=MagicMock(),
        inventory_coordinator=coordinator,
        skip_startup_scan=False,
        monitor=False,
        Observer=NoopObserver,
        NewFileHandler=lambda: object(),
    )

    scanner.transcribe_existing(runtime, "/private/library")

    snapshot = coordinator.snapshot()
    assert coordinator.wait_until_scanned(0) is True
    assert snapshot.scan_complete is False
    assert snapshot.scan_errors >= 1
    assert "private mount detail" not in repr(runtime.logging.warning.call_args_list)


def test_missing_configured_library_is_not_reported_as_a_complete_empty_scan():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    fake_path = SimpleNamespace(
        isfile=lambda _path: False,
        isdir=lambda _path: False,
    )
    runtime = SimpleNamespace(
        os=SimpleNamespace(path=fake_path, walk=MagicMock()),
        SKIP_MARKER=scanner.SKIP_MARKER,
        logging=MagicMock(),
        path_mapping=lambda path: path,
        transcribe_or_translate="translate",
        gen_subtitles_queue=MagicMock(),
        inventory_coordinator=coordinator,
        skip_startup_scan=False,
        monitor=False,
        Observer=NoopObserver,
        NewFileHandler=lambda: object(),
    )

    scanner.transcribe_existing(runtime, "/private/missing-library")

    snapshot = coordinator.snapshot()
    assert coordinator.wait_until_scanned(0) is True
    assert snapshot.scan_complete is False
    assert snapshot.scan_errors == 2
    assert snapshot.scan_percent == 0.0
    runtime.gen_subtitles_queue.assert_not_called()


def test_walk_permission_error_is_not_silently_reported_as_complete():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    fake_path = SimpleNamespace(
        isfile=lambda _path: False,
        isdir=lambda _path: True,
    )

    def denied_walk(_path, *, onerror=None):
        assert onerror is not None
        onerror(PermissionError("private mount detail"))
        return ()

    runtime = SimpleNamespace(
        os=SimpleNamespace(path=fake_path, walk=denied_walk),
        SKIP_MARKER=scanner.SKIP_MARKER,
        logging=MagicMock(),
        path_mapping=lambda path: path,
        transcribe_or_translate="translate",
        gen_subtitles_queue=MagicMock(),
        inventory_coordinator=coordinator,
        skip_startup_scan=False,
        monitor=False,
        Observer=NoopObserver,
        NewFileHandler=lambda: object(),
    )

    scanner.transcribe_existing(runtime, "/private/library")

    snapshot = coordinator.snapshot()
    assert snapshot.scan_complete is False
    assert snapshot.scan_errors == 2
    assert "private mount detail" not in repr(runtime.logging.warning.call_args_list)


def test_first_pass_path_mapping_failure_cannot_leave_worker_barrier_closed():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    coordinator.arm_scan()
    fake_path = SimpleNamespace(
        isfile=lambda _path: False,
        isdir=lambda _path: False,
    )
    runtime = SimpleNamespace(
        os=SimpleNamespace(path=fake_path, walk=lambda _path: ()),
        SKIP_MARKER=scanner.SKIP_MARKER,
        logging=MagicMock(),
        path_mapping=MagicMock(side_effect=OSError("private mapping")),
        transcribe_or_translate="translate",
        gen_subtitles_queue=MagicMock(),
        inventory_coordinator=coordinator,
        skip_startup_scan=False,
        monitor=False,
        Observer=NoopObserver,
        NewFileHandler=lambda: object(),
    )

    scanner.transcribe_existing(runtime, "/private/library")

    snapshot = coordinator.snapshot()
    assert coordinator.wait_until_scanned(0) is True
    assert snapshot.scan_complete is False
    assert snapshot.scan_errors == 1
    assert "private mapping" not in repr(runtime.logging.warning.call_args_list)


def test_one_file_inspection_error_marks_scan_incomplete_but_opens_barrier(tmp_path):
    media_file = tmp_path / "broken.mkv"
    media_file.touch()
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    runtime = SimpleNamespace(
        os=os,
        SKIP_MARKER=scanner.SKIP_MARKER,
        logging=MagicMock(),
        path_mapping=lambda path: path,
        transcribe_or_translate="translate",
        gen_subtitles_queue=MagicMock(side_effect=RuntimeError("private path")),
        has_video_extension=lambda name: name.endswith(".mkv"),
        has_audio_extension=lambda _name: False,
        inventory_coordinator=coordinator,
        skip_startup_scan=False,
        monitor=False,
        Observer=NoopObserver,
        NewFileHandler=lambda: object(),
    )

    scanner.transcribe_existing(runtime, str(tmp_path))

    snapshot = coordinator.snapshot()
    assert coordinator.wait_until_scanned(0) is True
    assert snapshot.scan_complete is False
    assert snapshot.scan_percent == 100.0
    assert snapshot.scan_errors == 1
    assert snapshot.items_left == 0
    assert "private path" not in repr(runtime.logging.warning.call_args_list)


def test_disabled_inventory_preserves_single_pass_and_legacy_exception_behavior():
    publisher = RecordingPublisher()
    coordinator = InventoryCoordinator(
        MqttInventoryConfig(), MagicMock(), publisher=publisher
    )
    walk_calls = []

    def walk(_path):
        walk_calls.append(1)
        yield "/library", [], ["movie.mkv"]

    fake_path = SimpleNamespace(
        isfile=lambda _path: False,
        isdir=lambda _path: False,
        join=lambda root, name: f"{root}/{name}",
    )
    queued = []
    runtime = SimpleNamespace(
        os=SimpleNamespace(path=fake_path, walk=walk),
        SKIP_MARKER=scanner.SKIP_MARKER,
        logging=MagicMock(),
        path_mapping=lambda path: path,
        transcribe_or_translate="translate",
        gen_subtitles_queue=lambda path, *_args: queued.append(path) or True,
        has_video_extension=lambda _name: False,
        has_audio_extension=lambda _name: False,
        inventory_coordinator=coordinator,
        skip_startup_scan=False,
        monitor=False,
        Observer=NoopObserver,
        NewFileHandler=lambda: object(),
    )

    scanner.transcribe_existing(runtime, "/library")

    assert len(walk_calls) == 1
    assert queued == ["/library/movie.mkv"]
    assert coordinator.wait_until_scanned(0) is True
    assert publisher.updates == []

    runtime.os.walk = MagicMock(side_effect=OSError("legacy failure"))
    with pytest.raises(OSError, match="legacy failure"):
        scanner.transcribe_existing(runtime, "/library")


def test_disabled_inventory_preserves_raw_pipe_split_semantics():
    assert scanner._configured_paths(" /first ||/second ") == [
        " /first ",
        "",
        "/second ",
    ]
    assert scanner._configured_paths(
        " /first ||/second ",
        normalize=True,
    ) == ["/first", "/second"]


def test_watcher_queue_adds_item_through_media_queue_callback(tmp_path):
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    coordinator.begin_scan([str(tmp_path)])
    coordinator.finish_scan()
    media_file = tmp_path / "new-film.mkv"
    media_file.touch()
    track = AudioTrack(index=0, language=LanguageCode.ENGLISH, default=True)
    evidence = ValidatorEvidence(
        ValidatorOutcome.AUDIO_PRESENT,
        duration_seconds=60.0,
        audio_tracks=(track,),
    )
    validation = MediaValidation(
        MediaOutcome.VALID_AUDIO,
        evidence,
        evidence,
        duration_seconds=60.0,
        audio_tracks=(track,),
    )

    class Queue:
        @staticmethod
        def is_active(_path):
            return False

        @staticmethod
        def put(_task):
            return True

    runtime = SimpleNamespace(
        os=os,
        logging=MagicMock(),
        _is_in_skipped_dir=lambda _path: False,
        path_mapping=lambda path: path,
        transcribe_or_translate="translate",
        is_file_stable=lambda _path: True,
        task_queue=Queue(),
        skip_marked_failed_files=False,
        validate_media=lambda _path: validation,
        choose_transcribe_language=lambda _path, _language, audio_tracks=None: (
            LanguageCode.ENGLISH
        ),
        select_audio_track=lambda _tracks, _language: track.as_task_dict(),
        should_skip_file=lambda _path, _language, audio_langs=None: False,
        should_whisper_detect_audio_language=False,
        force_detected_language_to=LanguageCode.NONE,
        inventory_item_queued=coordinator.mark_item_queued,
    )
    runtime.gen_subtitles_queue = lambda path, task_type: media.gen_subtitles_queue(
        runtime,
        path,
        task_type,
    )
    handler = scanner.NewFileHandler(runtime)

    handler.create_subtitle(
        SimpleNamespace(is_directory=False, src_path=str(media_file))
    )

    snapshot = coordinator.snapshot()
    assert snapshot.items_left == 1
    assert snapshot.libraries[0].items_left == 1
    assert snapshot.libraries[0].total == 1
    assert snapshot.libraries[0].scanned == 1


def test_move_event_uses_destination_and_replaces_the_pending_source(tmp_path):
    library = tmp_path / "Movies"
    library.mkdir()
    source = library / "importing.tmp"
    destination = library / "movie.mkv"
    destination.touch()
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    mapped_root = "/media/Movies"
    map_path = lambda path: str(path).replace(str(library), mapped_root)
    coordinator.begin_scan(
        [str(library)],
        mapped_paths=[mapped_root],
        library_names=["Movies"],
    )
    coordinator.mark_item_queued(map_path(source), source="runtime")
    stable_paths = []
    queued_paths = []

    def queue_file(path, _task_type):
        queued_paths.append(path)
        return coordinator.mark_item_queued(path, source="runtime")

    runtime = SimpleNamespace(
        logging=MagicMock(),
        _is_in_skipped_dir=lambda _path: False,
        path_mapping=map_path,
        transcribe_or_translate="translate",
        is_file_stable=lambda path: stable_paths.append(path) or True,
        gen_subtitles_queue=queue_file,
        inventory_item_removed=coordinator.mark_item_removed,
        inventory_coordinator=coordinator,
    )
    handler = scanner.NewFileHandler(runtime)

    handler.on_moved(
        SimpleNamespace(
            is_directory=False,
            src_path=str(source),
            dest_path=str(destination),
        )
    )
    coordinator.finish_scan()

    assert stable_paths == [str(destination)]
    assert queued_paths == [map_path(destination)]
    snapshot = coordinator.snapshot()
    assert snapshot.items_left == 1
    assert [
        (item.total, item.scanned, item.items_left)
        for item in snapshot.libraries
    ] == [(1, 1, 1)]


def test_live_move_replaces_the_counted_source_without_growing_inventory(tmp_path):
    library = tmp_path / "Movies"
    library.mkdir()
    source = library / "old-name.mkv"
    destination = library / "new-name.mkv"
    destination.touch()
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    mapped_root = "/media/Movies"
    map_path = lambda path: str(path).replace(str(library), mapped_root)
    (label,) = coordinator.begin_scan(
        [str(library)],
        mapped_paths=[mapped_root],
        library_names=["Movies"],
    )
    coordinator.record_counted_item(label, map_path(source))
    coordinator.set_library_total(label, 1)
    coordinator.mark_item_queued(map_path(source), source="startup_scan")
    coordinator.record_scanned_item(label, map_path(source))
    coordinator.finish_scan()

    runtime = SimpleNamespace(
        logging=MagicMock(),
        _is_in_skipped_dir=lambda _path: False,
        path_mapping=map_path,
        transcribe_or_translate="translate",
        is_file_stable=lambda _path: True,
        gen_subtitles_queue=lambda path, _task_type: coordinator.mark_item_queued(
            path, source="runtime"
        ),
        inventory_item_moved=coordinator.mark_item_moved,
        inventory_item_removed=coordinator.mark_item_removed,
        inventory_coordinator=coordinator,
    )

    scanner.NewFileHandler(runtime).on_moved(
        SimpleNamespace(
            is_directory=False,
            src_path=str(source),
            dest_path=str(destination),
        )
    )

    snapshot = coordinator.snapshot()
    assert snapshot.items_left == 1
    assert [
        (item.total, item.scanned, item.items_left)
        for item in snapshot.libraries
    ] == [(1, 1, 1)]


def test_live_move_preserves_counted_media_when_destination_needs_no_work(tmp_path):
    library = tmp_path / "Movies"
    library.mkdir()
    source = library / "old-name.mkv"
    destination = library / "new-name.mkv"
    destination.touch()
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    (label,) = coordinator.begin_scan(
        [str(library)], library_names=["Movies"]
    )
    coordinator.record_counted_item(label, str(source))
    coordinator.set_library_total(label, 1)
    coordinator.mark_item_queued(str(source), source="startup_scan")
    coordinator.record_scanned_item(label, str(source))
    coordinator.finish_scan()
    coordinator.mark_item_completed(str(source))

    runtime = SimpleNamespace(
        logging=MagicMock(),
        _is_in_skipped_dir=lambda _path: False,
        path_mapping=lambda path: path,
        transcribe_or_translate="translate",
        is_file_stable=lambda _path: True,
        gen_subtitles_queue=MagicMock(return_value=False),
        inventory_item_moved=coordinator.mark_item_moved,
        inventory_item_removed=coordinator.mark_item_removed,
        inventory_coordinator=coordinator,
    )

    scanner.NewFileHandler(runtime).on_moved(
        SimpleNamespace(
            is_directory=False,
            src_path=str(source),
            dest_path=str(destination),
        )
    )

    snapshot = coordinator.snapshot()
    assert snapshot.items_left == 0
    assert [
        (item.total, item.scanned, item.items_left)
        for item in snapshot.libraries
    ] == [(1, 1, 0)]


def test_supported_created_file_without_work_updates_live_totals(tmp_path):
    library = tmp_path / "Movies"
    library.mkdir()
    arrival = library / "already-subtitled.mkv"
    arrival.touch()
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    coordinator.begin_scan([str(library)], library_names=["Movies"])
    coordinator.finish_scan()
    queue_file = MagicMock(return_value=False)
    runtime = SimpleNamespace(
        os=os,
        time=SimpleNamespace(sleep=lambda _seconds: None),
        logging=MagicMock(),
        _is_in_skipped_dir=lambda _path: False,
        path_mapping=lambda path: path,
        transcribe_or_translate="translate",
        is_file_stable=lambda _path: True,
        gen_subtitles_queue=queue_file,
        has_video_extension=lambda name: name.endswith(".mkv"),
        has_audio_extension=lambda _name: False,
        inventory_item_observed=coordinator.mark_item_observed,
        inventory_coordinator=coordinator,
    )

    scanner.NewFileHandler(runtime).on_created(
        SimpleNamespace(is_directory=False, src_path=str(arrival))
    )

    queue_file.assert_called_once()
    snapshot = coordinator.snapshot()
    assert snapshot.items_left == 0
    assert [
        (item.total, item.scanned, item.items_left)
        for item in snapshot.libraries
    ] == [(1, 1, 0)]


def test_unknown_source_move_without_work_counts_the_supported_destination(tmp_path):
    library = tmp_path / "Movies"
    downloads = tmp_path / "Downloads"
    library.mkdir()
    downloads.mkdir()
    source = downloads / "download.mkv"
    destination = library / "import.mkv"
    destination.touch()
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    coordinator.begin_scan([str(library)], library_names=["Movies"])
    coordinator.finish_scan()
    runtime = SimpleNamespace(
        os=os,
        logging=MagicMock(),
        _is_in_skipped_dir=lambda _path: False,
        path_mapping=lambda path: path,
        transcribe_or_translate="translate",
        is_file_stable=lambda _path: True,
        gen_subtitles_queue=MagicMock(return_value=False),
        has_video_extension=lambda name: name.endswith(".mkv"),
        has_audio_extension=lambda _name: False,
        inventory_item_observed=coordinator.mark_item_observed,
        inventory_item_moved=coordinator.mark_item_moved,
        inventory_item_removed=coordinator.mark_item_removed,
        inventory_coordinator=coordinator,
    )

    scanner.NewFileHandler(runtime).on_moved(
        SimpleNamespace(
            is_directory=False,
            src_path=str(source),
            dest_path=str(destination),
        )
    )

    snapshot = coordinator.snapshot()
    assert snapshot.items_left == 0
    assert [
        (item.total, item.scanned, item.items_left)
        for item in snapshot.libraries
    ] == [(1, 1, 0)]


@pytest.mark.parametrize("pending", [False, True])
def test_move_into_skip_marked_tree_removes_live_inventory(tmp_path, pending):
    library = tmp_path / "Movies"
    quarantine = library / "quarantine"
    quarantine.mkdir(parents=True)
    (quarantine / scanner.SKIP_MARKER).touch()
    source = library / "movie.mkv"
    destination = quarantine / "movie.mkv"
    destination.touch()
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    (label,) = coordinator.begin_scan(
        [str(library)], library_names=["Movies"]
    )
    coordinator.record_counted_item(label, str(source))
    coordinator.set_library_total(label, 1)
    coordinator.mark_item_queued(str(source), source="startup_scan")
    coordinator.record_scanned_item(label, str(source))
    coordinator.finish_scan()
    if not pending:
        coordinator.mark_item_completed(str(source))
    queue_file = MagicMock(return_value=False)
    runtime = SimpleNamespace(
        os=os,
        logging=MagicMock(),
        _is_in_skipped_dir=lambda path: str(quarantine) in str(path),
        path_mapping=lambda path: path,
        transcribe_or_translate="translate",
        is_file_stable=lambda _path: True,
        gen_subtitles_queue=queue_file,
        has_video_extension=lambda name: name.endswith(".mkv"),
        has_audio_extension=lambda _name: False,
        inventory_item_observed=coordinator.mark_item_observed,
        inventory_item_moved=coordinator.mark_item_moved,
        inventory_item_removed=coordinator.mark_item_removed,
        inventory_coordinator=coordinator,
    )

    scanner.NewFileHandler(runtime).on_moved(
        SimpleNamespace(
            is_directory=False,
            src_path=str(source),
            dest_path=str(destination),
        )
    )

    queue_file.assert_not_called()
    snapshot = coordinator.snapshot()
    assert snapshot.items_left == 0
    assert [
        (item.total, item.scanned, item.items_left)
        for item in snapshot.libraries
    ] == [(0, 0, 0)]


def test_watcher_observation_during_scan_is_not_double_counted():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    path = "/media/Movies/arrival.mkv"
    (label,) = coordinator.begin_scan(
        ["/media/Movies"], library_names=["Movies"]
    )
    generation = coordinator.scan_generation

    coordinator.mark_item_observed(path, generation=generation)
    coordinator.mark_item_queued(path, source="runtime", generation=generation)
    coordinator.record_counted_item(label, path, generation=generation)
    coordinator.set_library_total(label, 1, generation=generation)
    coordinator.record_scanned_item(label, path, generation=generation)
    coordinator.finish_scan(generation=generation)

    snapshot = coordinator.snapshot()
    assert snapshot.items_left == 1
    assert [
        (item.total, item.scanned, item.items_left)
        for item in snapshot.libraries
    ] == [(1, 1, 1)]


def test_post_cutoff_delete_cannot_be_readded_by_a_stale_final_walk():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    source = "/media/Movies/deleted.mkv"
    (label,) = coordinator.begin_scan(
        ["/media/Movies"], library_names=["Movies"]
    )
    generation = coordinator.scan_generation
    coordinator.record_counted_item(label, source, generation=generation)
    coordinator.set_library_total(label, 1, generation=generation)
    coordinator.mark_item_queued(
        source, source="startup_scan", generation=generation
    )
    coordinator.record_scanned_item(label, source, generation=generation)
    coordinator.close_scan_event_cutoff(generation=generation)
    stale_final_paths = [source]

    coordinator.mark_item_removed(source)
    coordinator.reconcile_final_library(
        label, stale_final_paths, generation=generation
    )
    coordinator.finish_scan(generation=generation)

    snapshot = coordinator.snapshot()
    assert snapshot.items_left == 0
    assert [
        (item.total, item.scanned, item.items_left)
        for item in snapshot.libraries
    ] == [(0, 0, 0)]


def test_post_cutoff_move_replaces_a_stale_source_snapshot_without_queueing():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    source = "/media/Movies/old-name.mkv"
    destination = "/media/Movies/new-name.mkv"
    (label,) = coordinator.begin_scan(
        ["/media/Movies"], library_names=["Movies"]
    )
    generation = coordinator.scan_generation
    coordinator.record_counted_item(label, source, generation=generation)
    coordinator.set_library_total(label, 1, generation=generation)
    coordinator.record_scanned_item(label, source, generation=generation)
    coordinator.close_scan_event_cutoff(generation=generation)
    stale_final_paths = [source]

    coordinator.mark_item_moved(source, destination)
    coordinator.reconcile_final_library(
        label, stale_final_paths, generation=generation
    )
    coordinator.finish_scan(generation=generation)

    snapshot = coordinator.snapshot()
    assert snapshot.items_left == 0
    assert [
        (item.total, item.scanned, item.items_left)
        for item in snapshot.libraries
    ] == [(1, 1, 0)]


def test_post_cutoff_move_replaces_stale_source_and_keeps_destination_backlog():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    source = "/media/Movies/old-name.mkv"
    destination = "/media/Movies/new-name.mkv"
    (label,) = coordinator.begin_scan(
        ["/media/Movies"], library_names=["Movies"]
    )
    generation = coordinator.scan_generation
    coordinator.record_counted_item(label, source, generation=generation)
    coordinator.set_library_total(label, 1, generation=generation)
    coordinator.mark_item_queued(
        source, source="startup_scan", generation=generation
    )
    coordinator.record_scanned_item(label, source, generation=generation)
    coordinator.close_scan_event_cutoff(generation=generation)
    stale_final_paths = [source]

    coordinator.mark_item_moved(source, destination)
    coordinator.mark_item_queued(destination, source="runtime")
    coordinator.reconcile_final_library(
        label, stale_final_paths, generation=generation
    )
    coordinator.finish_scan(generation=generation)

    snapshot = coordinator.snapshot()
    assert snapshot.items_left == 1
    assert [
        (item.total, item.scanned, item.items_left)
        for item in snapshot.libraries
    ] == [(1, 1, 1)]


def test_failed_scan_does_not_double_count_an_already_transferred_move():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    source = "/media/Movies/old-name.mkv"
    destination = "/media/Movies/new-name.mkv"
    (label,) = coordinator.begin_scan(
        ["/media/Movies"], library_names=["Movies"]
    )
    generation = coordinator.scan_generation
    coordinator.record_counted_item(label, source, generation=generation)
    coordinator.set_library_total(label, 1, generation=generation)
    coordinator.record_scanned_item(label, source, generation=generation)

    coordinator.mark_item_moved(source, destination, generation=generation)
    coordinator.finish_scan(successful=False, generation=generation)

    snapshot = coordinator.snapshot()
    assert snapshot.scan_complete is False
    assert [
        (item.total, item.scanned, item.items_left)
        for item in snapshot.libraries
    ] == [(1, 1, 0)]


def test_delete_event_removes_a_pending_item_from_the_live_inventory(tmp_path):
    path = tmp_path / "deleted.mkv"
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    coordinator.begin_scan([str(tmp_path)], library_names=["Movies"])
    coordinator.mark_item_queued(str(path), source="runtime")
    runtime = SimpleNamespace(
        logging=MagicMock(),
        inventory_item_removed=coordinator.mark_item_removed,
        inventory_coordinator=coordinator,
        path_mapping=lambda value: value,
    )

    scanner.NewFileHandler(runtime).on_deleted(
        SimpleNamespace(is_directory=False, src_path=str(path))
    )
    coordinator.finish_scan()

    assert coordinator.snapshot().items_left == 0


def test_move_events_keep_legacy_default_off_behavior_unchanged():
    queue_file = MagicMock()
    remove_item = MagicMock()
    runtime = SimpleNamespace(
        logging=MagicMock(),
        inventory_coordinator=InventoryCoordinator(
            MqttInventoryConfig(), MagicMock(), publisher=RecordingPublisher()
        ),
        inventory_item_removed=remove_item,
        path_mapping=lambda path: path,
        is_file_stable=MagicMock(return_value=True),
        gen_subtitles_queue=queue_file,
    )

    scanner.NewFileHandler(runtime).on_moved(
        SimpleNamespace(
            is_directory=False,
            src_path="/watch/old.mkv",
            dest_path="/watch/new.mkv",
        )
    )

    remove_item.assert_not_called()
    runtime.is_file_stable.assert_not_called()
    queue_file.assert_not_called()


def test_inventory_reservation_precedes_queue_visibility_and_fast_completion():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    path = "/media/Movies/fast.mkv"
    coordinator.begin_scan(["/media/Movies"], library_names=["Movies"])
    coordinator.finish_scan()

    class CompletingQueue:
        @staticmethod
        def put(_task):
            coordinator.mark_item_completed(path)
            return True

        @staticmethod
        def is_active(_path):
            return False

    runtime = SimpleNamespace(
        task_queue=CompletingQueue(),
        logging=MagicMock(),
        inventory_item_queued=coordinator.mark_item_queued,
        inventory_item_unqueued=coordinator.cancel_item_queue,
    )

    queued = media._put_task_with_inventory(
        runtime,
        {"path": path, "type": "transcribe"},
        path,
        source="runtime",
    )

    assert queued is True
    snapshot = coordinator.snapshot()
    assert snapshot.items_left == 0
    assert [
        (item.total, item.scanned, item.items_left)
        for item in snapshot.libraries
    ] == [(1, 1, 0)]


def test_rejected_queue_rolls_back_an_unowned_inventory_reservation():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    path = "/media/Movies/rejected.mkv"
    coordinator.begin_scan(["/media/Movies"], library_names=["Movies"])
    coordinator.finish_scan()

    class RejectingQueue:
        @staticmethod
        def put(_task):
            return False

        @staticmethod
        def is_active(_path):
            return False

    runtime = SimpleNamespace(
        task_queue=RejectingQueue(),
        logging=MagicMock(),
        inventory_item_queued=coordinator.mark_item_queued,
        inventory_item_unqueued=coordinator.cancel_item_queue,
    )

    queued = media._put_task_with_inventory(
        runtime,
        {"path": path, "type": "transcribe"},
        path,
        source="runtime",
    )

    assert queued is False
    snapshot = coordinator.snapshot()
    assert snapshot.items_left == 0
    assert [(item.total, item.scanned) for item in snapshot.libraries] == [(0, 0)]


def test_generation_bound_queue_rollback_uses_the_same_scan_generation():
    queued = MagicMock(return_value=True)
    unqueued = MagicMock()
    task_queue = MagicMock()
    task_queue.put.return_value = False
    task_queue.is_active.return_value = False
    runtime = SimpleNamespace(
        task_queue=task_queue,
        logging=MagicMock(),
        inventory_generation_active=MagicMock(return_value=True),
        inventory_item_queued=queued,
        inventory_item_unqueued=unqueued,
    )
    path = "/media/Movies/rejected.mkv"

    accepted = media._put_task_with_inventory(
        runtime,
        {"path": path, "type": "transcribe", "_inventory_generation": 7},
        path,
        source="startup_scan",
        generation=7,
    )

    assert accepted is False
    queued.assert_called_once_with(
        path,
        source="startup_scan",
        generation=7,
    )
    unqueued.assert_called_once_with(path, generation=7)


def test_stale_generation_never_reaches_inventory_or_task_queue():
    queued = MagicMock(return_value=True)
    task_queue = MagicMock()
    runtime = SimpleNamespace(
        task_queue=task_queue,
        logging=MagicMock(),
        inventory_generation_active=MagicMock(return_value=False),
        inventory_item_queued=queued,
        inventory_item_unqueued=MagicMock(),
    )

    accepted = media._put_task_with_inventory(
        runtime,
        {"path": "/media/Movies/stale.mkv", "type": "transcribe"},
        "/media/Movies/stale.mkv",
        source="startup_scan",
        generation=3,
    )

    assert accepted is False
    queued.assert_not_called()
    task_queue.put.assert_not_called()


def test_enabled_monitor_start_failures_remain_nonblocking(tmp_path):
    media_file = tmp_path / "movie.mkv"
    media_file.touch()
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    queued = []
    observers = []

    class FailingObserver:
        def __init__(self):
            self.live = False
            self.stopped = False
            self.joined = False
            observers.append(self)

        def schedule(self, *_args, **_kwargs):
            return None

        def start(self):
            self.live = True
            raise RuntimeError("private observer detail")

        def stop(self):
            self.stopped = True
            self.live = False

        def join(self, *, timeout):
            assert timeout == 5.0
            self.joined = True

    runtime = SimpleNamespace(
        os=os,
        SKIP_MARKER=scanner.SKIP_MARKER,
        logging=MagicMock(),
        path_mapping=lambda path: path,
        transcribe_or_translate="translate",
        gen_subtitles_queue=lambda path, *_args, **_kwargs: queued.append(path)
        or False,
        has_video_extension=lambda name: name.endswith(".mkv"),
        has_audio_extension=lambda _name: False,
        inventory_coordinator=coordinator,
        skip_startup_scan=False,
        monitor=True,
        Observer=FailingObserver,
        NewFileHandler=lambda: object(),
    )

    scanner.transcribe_existing(runtime, str(tmp_path))

    snapshot = coordinator.snapshot()
    assert queued == [str(media_file)]
    assert snapshot.scan_complete is False
    assert snapshot.scan_errors >= 2
    assert len(observers) == 2
    assert all(not observer.live for observer in observers)
    assert all(observer.stopped and observer.joined for observer in observers)
    assert "private observer detail" not in repr(
        runtime.logging.warning.call_args_list
    )


def test_worker_waits_for_inventory_barrier_before_decode(monkeypatch):
    import subgen

    class StopWorker(BaseException):
        pass

    task = {
        "path": "/media/movie.mkv",
        "type": "transcribe",
        "transcribe_or_translate": "translate",
        "force_language": LanguageCode.NONE,
    }
    events = []

    class OneTaskQueue:
        consumed = False

        def get(self, **_kwargs):
            if not self.consumed:
                self.consumed = True
                return task
            raise StopWorker

        @staticmethod
        def get_processing_tasks():
            return []

        @staticmethod
        def get_queued_tasks():
            return []

        @staticmethod
        def task_done():
            return None

        @staticmethod
        def mark_done(_task):
            return None

    barrier = MagicMock()
    barrier.wait_until_scanned.side_effect = lambda: events.append("barrier") or True
    monkeypatch.setattr(subgen, "inventory_coordinator", barrier)
    monkeypatch.setattr(subgen, "task_queue", OneTaskQueue())
    monkeypatch.setattr(subgen, "emit_subgen_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        subgen,
        "gen_subtitles",
        lambda *_args, **_kwargs: events.append("decode"),
    )
    monkeypatch.setattr(subgen, "inventory_item_completed", lambda _path: None)
    monkeypatch.setattr(subgen, "cleanup_task_result", lambda _task_id: None)
    monkeypatch.setattr(subgen, "delete_model", lambda: None)

    with pytest.raises(StopWorker):
        subgen.transcription_worker()

    assert events[:2] == ["barrier", "decode"]


def test_worker_discards_task_from_a_replaced_inventory_generation(monkeypatch):
    import subgen

    class StopWorker(BaseException):
        pass

    task = {
        "path": "/media/stale.mkv",
        "type": "transcribe",
        "transcribe_or_translate": "translate",
        "force_language": LanguageCode.NONE,
        "_inventory_generation": 4,
    }
    events = []

    class OneTaskQueue:
        consumed = False

        def get(self, **_kwargs):
            if not self.consumed:
                self.consumed = True
                return task
            raise StopWorker

        @staticmethod
        def get_processing_tasks():
            return []

        @staticmethod
        def get_queued_tasks():
            return []

        @staticmethod
        def task_done():
            events.append("task_done")

        @staticmethod
        def mark_done(_task):
            events.append("mark_done")

    barrier = MagicMock()
    barrier.wait_until_scanned.side_effect = lambda: events.append("barrier") or True
    monkeypatch.setattr(subgen, "inventory_coordinator", barrier)
    monkeypatch.setattr(subgen, "task_queue", OneTaskQueue())
    monkeypatch.setattr(
        subgen,
        "inventory_generation_current",
        lambda generation: events.append(("generation", generation)) or False,
        raising=False,
    )
    decode = MagicMock()
    completed = MagicMock()
    monkeypatch.setattr(subgen, "gen_subtitles", decode)
    monkeypatch.setattr(subgen, "inventory_item_completed", completed)
    monkeypatch.setattr(subgen, "cleanup_task_result", lambda _task_id: None)
    monkeypatch.setattr(subgen, "delete_model", lambda: None)

    with pytest.raises(StopWorker):
        subgen.transcription_worker()

    assert events[:2] == ["barrier", ("generation", 4)]
    assert events[-2:] == ["task_done", "mark_done"]
    decode.assert_not_called()
    completed.assert_not_called()


def test_lifespan_prepares_inventory_layout_before_publisher_or_scan_thread(
    monkeypatch,
):
    import subgen

    events = []

    class Coordinator:
        enabled = True

        @staticmethod
        def start():
            events.append("start")

        @staticmethod
        def stop():
            events.append("stop")

        @staticmethod
        def record_scan_error():
            events.append("error")

        @staticmethod
        def finish_scan(*, successful):
            events.append(("finish", successful))

    class Thread:
        def __init__(self, *args, **kwargs):
            self.name = kwargs.get("name")

        def start(self):
            events.append(("thread", self.name))

    monkeypatch.setattr(subgen, "inventory_coordinator", Coordinator())
    monkeypatch.setattr(subgen, "transcribe_folders", "/media/Movies")
    monkeypatch.setattr(subgen, "memory_pressure_yield", False)
    monkeypatch.setattr(subgen.threading, "Thread", Thread)
    monkeypatch.setattr(
        subgen._scanner,
        "prepare_inventory_scan",
        lambda _runtime, folders: events.append(("prepare", folders)),
    )

    async def exercise():
        async with subgen.lifespan(subgen.app):
            events.append("yield")

    asyncio.run(exercise())

    assert events[:3] == [
        ("prepare", "/media/Movies"),
        "start",
        ("thread", "subgen-startup-inventory"),
    ]


def test_synchronous_inventory_layout_preparation_does_not_stat_library_paths():
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=RecordingPublisher()
    )
    isfile = MagicMock(side_effect=AssertionError("synchronous filesystem I/O"))
    runtime = SimpleNamespace(
        os=SimpleNamespace(
            path=SimpleNamespace(
                basename=os.path.basename,
                isfile=isfile,
            )
        ),
        path_mapping=lambda path: path,
        has_video_extension=lambda name: name.endswith(".mkv"),
        has_audio_extension=lambda _name: False,
        inventory_coordinator=coordinator,
    )

    labels = scanner.prepare_inventory_scan(
        runtime,
        "/media/Movies|/media/one-off.mkv",
    )

    assert labels == ("Library 1", "Direct file 1")
    isfile.assert_not_called()
    coordinator.stop()


def test_armed_coordinator_first_publishes_scan_in_progress_not_false_complete():
    publisher = RecordingPublisher()
    coordinator = InventoryCoordinator(
        enabled_config(), MagicMock(), publisher=publisher
    )

    coordinator.arm_scan()
    coordinator.start()

    published_snapshots = [snapshot for snapshot, _urgent in publisher.updates]
    assert published_snapshots
    assert all(snapshot.scan_complete is False for snapshot in published_snapshots)
    assert all(snapshot.scan_percent == 0.0 for snapshot in published_snapshots)
    assert all(snapshot.items_left == 0 for snapshot in published_snapshots)
    coordinator.stop()
