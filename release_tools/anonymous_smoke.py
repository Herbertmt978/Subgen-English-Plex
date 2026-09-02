"""Isolated anonymous registry smoke for the exact Task 12 OCI digest."""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, BinaryIO, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from release_tools.task12 import (  # noqa: E402
    ANONYMOUS_DOCKER_CLIENT_CONTRACT,
    ANONYMOUS_DOCKER_HOST,
    CREDENTIAL_TRANSPORT_CONTRACT,
    REGISTRY_API_ORIGIN,
    REGISTRY_CLIENT_CONTRACT,
    ImageIdentity,
    PublicationBlocked,
    _strict_json_object,
    canonical_json_bytes,
    derive_layer_diff_id,
)


_CHALLENGE_ITEM = re.compile(r'([A-Za-z]+)="([^"\r\n]+)"')
_MAX_METADATA_RESPONSE = 32 * 1024 * 1024
_MAX_LAYER_RESPONSE = 16 * 1024 * 1024 * 1024
_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
_DOCKER_EXECUTABLE = Path("/usr/bin/docker")
_MAX_DOCKER_HEADER_LINE = 8 * 1024
_MAX_DOCKER_HEADERS = 64 * 1024
_DOCKER_REQUEST_TIMEOUT = 1800


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _read_bounded(response: Any, maximum: int) -> bytes:
    if isinstance(maximum, bool) or not 0 < maximum <= _MAX_LAYER_RESPONSE:
        raise PublicationBlocked("anonymous_smoke_response_bound_invalid")
    payload = response.read(maximum + 1)
    if len(payload) > maximum:
        raise PublicationBlocked("anonymous_smoke_response_too_large")
    return payload


class AnonymousRegistryClient:
    def __init__(self, repository: str) -> None:
        self.repository = repository
        self._opener = urllib.request.build_opener(_NoRedirect())
        self._token: str | None = None

    def _request(
        self,
        url: str,
        headers: Mapping[str, str],
        *,
        maximum: int = _MAX_METADATA_RESPONSE,
    ) -> tuple[int, tuple[tuple[str, str], ...], bytes]:
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            with self._opener.open(request, timeout=60) as response:
                if response.geturl() != url:
                    raise PublicationBlocked("anonymous_smoke_redirected")
                return (
                    response.status,
                    tuple(response.headers.raw_items()),
                    _read_bounded(response, maximum),
                )
        except urllib.error.HTTPError as exc:
            return (
                exc.code,
                tuple(exc.headers.raw_items()),
                _read_bounded(exc, maximum),
            )
        except (OSError, urllib.error.URLError) as exc:
            raise PublicationBlocked("anonymous_smoke_transport_failed") from exc

    @staticmethod
    def _header(
        headers: Mapping[str, str] | tuple[tuple[str, str], ...],
        name: str,
    ) -> str | None:
        items = tuple(headers.items()) if isinstance(headers, Mapping) else headers
        matches = [value for key, value in items if key.casefold() == name.casefold()]
        if len(matches) > 1:
            raise PublicationBlocked("anonymous_smoke_header_duplicate")
        return matches[0] if matches else None

    def _anonymous_token(self, challenge: str) -> str:
        if not challenge.startswith("Bearer "):
            raise PublicationBlocked("anonymous_smoke_auth_challenge_invalid")
        values = dict(_CHALLENGE_ITEM.findall(challenge[7:]))
        realm = values.get("realm")
        service = values.get("service")
        scope = values.get("scope")
        if (
            realm != "https://ghcr.io/token"
            or service != "ghcr.io"
            or scope != f"repository:{self.repository}:pull"
        ):
            raise PublicationBlocked("anonymous_smoke_auth_challenge_invalid")
        url = realm + "?" + urllib.parse.urlencode({"service": service, "scope": scope})
        status, headers, body = self._request(
            url,
            {"Accept": "application/json"},
        )
        if status != 200 or 300 <= status < 400:
            raise PublicationBlocked("anonymous_smoke_token_failed")
        document = _strict_json_object(body, "anonymous_token")
        token = document.get("token", document.get("access_token"))
        if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
            raise PublicationBlocked("anonymous_smoke_token_invalid")
        return token

    def get(
        self,
        path: str,
        accept: str,
        *,
        maximum: int = _MAX_METADATA_RESPONSE,
    ) -> tuple[Mapping[str, str], bytes]:
        url = REGISTRY_API_ORIGIN + path
        headers = {"Accept": accept}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        status, response_headers, body = self._request(
            url,
            headers,
            maximum=maximum,
        )
        if status == 401 and self._token is None:
            challenge = self._header(response_headers, "WWW-Authenticate")
            if not isinstance(challenge, str):
                raise PublicationBlocked("anonymous_smoke_auth_challenge_missing")
            self._token = self._anonymous_token(challenge)
            return self.get(path, accept, maximum=maximum)
        if status != 200 or 300 <= status < 400:
            raise PublicationBlocked("anonymous_smoke_get_failed")
        return response_headers, body


def _store_payload(root: Path, digest: str, payload: bytes) -> None:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise PublicationBlocked("anonymous_smoke_digest_invalid")
    target = root / digest.removeprefix("sha256:")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise PublicationBlocked("anonymous_smoke_store_write_failed") from exc


def _verify_descriptor(
    descriptor: object,
    *,
    media_type: str | None = None,
) -> tuple[str, int, str]:
    if not isinstance(descriptor, dict):
        raise PublicationBlocked("anonymous_smoke_descriptor_invalid")
    digest = descriptor.get("digest")
    size = descriptor.get("size")
    observed_media_type = descriptor.get("mediaType")
    if (
        not isinstance(digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size <= 0
        or not isinstance(observed_media_type, str)
        or (media_type is not None and observed_media_type != media_type)
    ):
        raise PublicationBlocked("anonymous_smoke_descriptor_invalid")
    return digest, size, observed_media_type


def _verify_payload(
    headers: Mapping[str, str] | tuple[tuple[str, str], ...],
    payload: bytes,
    digest: str,
    size: int,
    *,
    allow_missing_digest_header: bool = False,
) -> None:
    header_digest = AnonymousRegistryClient._header(headers, "Docker-Content-Digest")
    if (
        (header_digest is None and not allow_missing_digest_header)
        or header_digest not in {None, digest}
        or len(payload) != size
        or "sha256:" + hashlib.sha256(payload).hexdigest() != digest
    ):
        raise PublicationBlocked("anonymous_smoke_payload_mismatch")


class _DockerEngineSession:
    """One bounded HTTP/1.1 stream over one Docker daemon connection."""

    def __init__(self, config_root: Path) -> None:
        self._stderr = tempfile.TemporaryFile()
        self._closed = False
        self._poisoned = False
        try:
            executable = _DOCKER_EXECUTABLE.resolve(strict=True)
            if not executable.is_file():
                raise OSError
            self._process = subprocess.Popen(
                (str(executable), "system", "dial-stdio"),
                cwd=config_root,
                env={
                    "DOCKER_CONFIG": str(config_root),
                    "DOCKER_HOST": ANONYMOUS_DOCKER_HOST,
                    "HOME": str(config_root),
                    "PATH": os.defpath,
                },
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr,
                shell=False,
                bufsize=0,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._stderr.close()
            raise PublicationBlocked("anonymous_docker_command_failed") from exc
        if self._process.stdin is None or self._process.stdout is None:
            self._terminate()
            raise PublicationBlocked("anonymous_docker_command_failed")
        self._input: BinaryIO = self._process.stdin
        self._output: BinaryIO = self._process.stdout

    @staticmethod
    def _readline(stream: BinaryIO) -> bytes:
        line = stream.readline(_MAX_DOCKER_HEADER_LINE + 1)
        if (
            not line
            or len(line) > _MAX_DOCKER_HEADER_LINE
            or not line.endswith(b"\r\n")
        ):
            raise ValueError("invalid Docker API line")
        return line

    @staticmethod
    def _read_exact(stream: BinaryIO, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = stream.read(remaining)
            if not chunk:
                raise ValueError("truncated Docker API response")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _response(self, maximum: int) -> tuple[int, bytes]:
        status_line = self._readline(self._output)
        status_parts = status_line[:-2].split(b" ", 2)
        if (
            len(status_parts) != 3
            or status_parts[0] != b"HTTP/1.1"
            or not status_parts[1].isdigit()
            or len(status_parts[1]) != 3
        ):
            raise ValueError("invalid Docker API status")
        status = int(status_parts[1])
        headers: dict[bytes, bytes] = {}
        header_size = 0
        while True:
            line = self._readline(self._output)
            header_size += len(line)
            if header_size > _MAX_DOCKER_HEADERS:
                raise ValueError("Docker API headers too large")
            if line == b"\r\n":
                break
            if line[:1] in {b" ", b"\t"} or b":" not in line:
                raise ValueError("invalid Docker API header")
            raw_name, raw_value = line[:-2].split(b":", 1)
            name = raw_name.strip().lower()
            value = raw_value.strip()
            if (
                not name
                or re.fullmatch(rb"[!#$%&'*+.^_`|~0-9a-z-]+", name) is None
                or name in headers
                or b"\r" in value
                or b"\n" in value
            ):
                raise ValueError("invalid Docker API header")
            headers[name] = value

        transfer_encoding = headers.get(b"transfer-encoding")
        content_length = headers.get(b"content-length")
        if transfer_encoding is not None:
            if transfer_encoding.lower() != b"chunked" or content_length is not None:
                raise ValueError("invalid Docker API framing")
            chunks: list[bytes] = []
            total = 0
            while True:
                raw_size = self._readline(self._output)[:-2]
                if not raw_size or re.fullmatch(rb"[0-9A-Fa-f]+", raw_size) is None:
                    raise ValueError("invalid Docker API chunk")
                size = int(raw_size, 16)
                if size == 0:
                    if self._readline(self._output) != b"\r\n":
                        raise ValueError("unexpected Docker API trailer")
                    break
                total += size
                if total > maximum:
                    raise ValueError("Docker API response too large")
                chunks.append(self._read_exact(self._output, size))
                if self._read_exact(self._output, 2) != b"\r\n":
                    raise ValueError("invalid Docker API chunk ending")
            return status, b"".join(chunks)
        if (
            content_length is None
            or re.fullmatch(rb"0|[1-9][0-9]*", content_length) is None
        ):
            raise ValueError("missing Docker API response length")
        size = int(content_length)
        if size > maximum:
            raise ValueError("Docker API response too large")
        return status, self._read_exact(self._output, size)

    def request(
        self,
        method: str,
        target: str,
        *,
        maximum: int = _MAX_METADATA_RESPONSE,
        close_connection: bool = False,
    ) -> bytes:
        if (
            self._closed
            or self._poisoned
            or method not in {"GET", "POST", "DELETE"}
            or not target.startswith("/")
            or "\r" in target
            or "\n" in target
            or not 0 < maximum <= _MAX_METADATA_RESPONSE
        ):
            raise PublicationBlocked("anonymous_docker_command_failed")
        connection = "close" if close_connection else "keep-alive"
        request = (
            f"{method} {target} HTTP/1.1\r\n"
            "Host: docker\r\n"
            "Accept: application/json\r\n"
            "Content-Length: 0\r\n"
            f"Connection: {connection}\r\n"
            "X-Registry-Auth: e30=\r\n"
            "\r\n"
        ).encode("ascii")
        result: list[tuple[int, bytes]] = []
        errors: list[Exception] = []

        def exchange() -> None:
            try:
                self._assert_output_idle()
                self._input.write(request)
                self._input.flush()
                result.append(self._response(maximum))
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        worker = threading.Thread(target=exchange, daemon=True)
        worker.start()
        worker.join(timeout=_DOCKER_REQUEST_TIMEOUT)
        if worker.is_alive():
            self._poisoned = True
            self._terminate()
            worker.join(timeout=5)
            raise PublicationBlocked("anonymous_docker_command_failed")
        if errors or len(result) != 1:
            self._poisoned = True
            cause = errors[0] if errors else RuntimeError("missing Docker API response")
            raise PublicationBlocked("anonymous_docker_command_failed") from cause
        status, payload = result[0]
        if close_connection:
            self._closed = True
        if status < 200 or status >= 300:
            raise PublicationBlocked("anonymous_docker_command_failed")
        return payload

    def _assert_output_idle(self) -> None:
        """Reject bytes sent when no Engine API request is in flight."""

        readable, ended = self._output_state(0)
        if readable or ended:
            raise PublicationBlocked("anonymous_docker_command_failed")

    def _output_state(self, timeout: float) -> tuple[bool, bool]:
        try:
            descriptor = self._output.fileno()
        except (AttributeError, OSError):
            # BytesIO-backed unit-test streams expose no selectable descriptor.
            return False, False
        if os.name == "nt":
            try:
                import ctypes
                import msvcrt

                available = ctypes.c_ulong()
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                succeeded = kernel32.PeekNamedPipe(
                    ctypes.c_void_p(msvcrt.get_osfhandle(descriptor)),
                    None,
                    0,
                    None,
                    ctypes.byref(available),
                    None,
                )
                if not succeeded:
                    error = ctypes.get_last_error()
                    if error in {109, 232}:  # broken pipe / no data after close
                        return False, True
                    raise OSError(error, "PeekNamedPipe failed")
                return available.value > 0, False
            except OSError as exc:
                raise PublicationBlocked("anonymous_docker_command_failed") from exc
        try:
            readable, _writable, _exceptional = select.select(
                [descriptor],
                [],
                [],
                timeout,
            )
        except (OSError, ValueError) as exc:
            raise PublicationBlocked("anonymous_docker_command_failed") from exc
        return bool(readable), False

    def _read_terminal_tail(self) -> bytes:
        try:
            self._output.fileno()
        except (AttributeError, OSError):
            return self._output.read(1)
        readable, ended = self._output_state(5)
        if ended:
            return b""
        if not readable:
            raise PublicationBlocked("anonymous_docker_command_failed")
        return self._output.read(1)

    def _terminate(self) -> bool:
        process = getattr(self, "_process", None)
        if process is None:
            return True
        failed = False
        try:
            running = process.poll() is None
        except (OSError, subprocess.SubprocessError):
            running = True
            failed = True
        if running:
            try:
                process.terminate()
            except (OSError, subprocess.SubprocessError):
                failed = True
            try:
                process.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                failed = True
                try:
                    process.kill()
                except (OSError, subprocess.SubprocessError):
                    failed = True
                try:
                    process.wait(timeout=5)
                except (OSError, subprocess.SubprocessError):
                    failed = True
        return not failed

    def finish(self) -> None:
        failed = self._poisoned or not self._closed
        try:
            self._input.close()
        except (AttributeError, OSError):
            failed = True
        if self._closed and not self._poisoned:
            try:
                self._process.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                failed = True
                if not self._terminate():
                    failed = True
        else:
            if not self._terminate():
                failed = True
        try:
            if self._process.returncode == 0 and self._read_terminal_tail():
                failed = True
        except (OSError, PublicationBlocked):
            failed = True
        try:
            self._output.close()
        except (AttributeError, OSError):
            failed = True
        try:
            self._stderr.seek(0)
            stderr = self._stderr.read(_MAX_DOCKER_HEADERS + 1)
            if len(stderr) > _MAX_DOCKER_HEADERS:
                failed = True
        except OSError:
            stderr = b""
            failed = True
        finally:
            self._stderr.close()
        if self._process.returncode != 0 or stderr:
            failed = True
        if failed:
            raise PublicationBlocked("anonymous_docker_command_failed")


def _docker_json_value(payload: bytes, failure_code: str) -> Any:
    if (
        not isinstance(payload, bytes)
        or not payload
        or len(payload) > _MAX_METADATA_RESPONSE
    ):
        raise PublicationBlocked(failure_code)

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PublicationBlocked(failure_code)
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise PublicationBlocked(failure_code) from exc
    return value


def _docker_json_object(payload: bytes) -> dict[str, Any]:
    value = _docker_json_value(payload, "anonymous_docker_inspect_invalid")
    if not isinstance(value, dict):
        raise PublicationBlocked("anonymous_docker_inspect_invalid")
    return value


def _docker_json_array(payload: bytes) -> list[Any]:
    value = _docker_json_value(payload, "anonymous_docker_command_failed")
    if not isinstance(value, list):
        raise PublicationBlocked("anonymous_docker_command_failed")
    return value


def _docker_daemon_id(
    session: _DockerEngineSession,
    *,
    close_connection: bool = False,
) -> str:
    document = _docker_json_value(
        session.request("GET", "/info", close_connection=close_connection),
        "anonymous_docker_identity_invalid",
    )
    daemon_id = document.get("ID") if isinstance(document, dict) else None
    if (
        not isinstance(daemon_id, str)
        or not daemon_id
        or len(daemon_id) > 128
        or "\r" in daemon_id
        or "\n" in daemon_id
    ):
        raise PublicationBlocked("anonymous_docker_identity_invalid")
    return daemon_id


def _assert_docker_daemon(
    session: _DockerEngineSession,
    daemon_id: str,
) -> None:
    if _docker_daemon_id(session) != daemon_id:
        raise PublicationBlocked("anonymous_docker_identity_changed")


def _bound_docker_request(
    session: _DockerEngineSession,
    daemon_id: str,
    method: str,
    target: str,
) -> bytes:
    _assert_docker_daemon(session, daemon_id)
    try:
        return session.request(method, target)
    finally:
        _assert_docker_daemon(session, daemon_id)


def _verify_docker_pull(payload: bytes) -> None:
    lines = payload.splitlines()
    if not lines:
        raise PublicationBlocked("anonymous_docker_command_failed")
    for line in lines:
        event = _docker_json_value(line, "anonymous_docker_command_failed")
        if not isinstance(event, dict) or "error" in event or "errorDetail" in event:
            raise PublicationBlocked("anonymous_docker_command_failed")


def _docker_smoke(
    reference: str,
    identity: ImageIdentity,
    *,
    candidate_engine_id_sha256: str,
) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", candidate_engine_id_sha256) is None:
        raise PublicationBlocked("anonymous_docker_identity_invalid")
    config_root = Path(tempfile.mkdtemp(prefix="subgen-task12-anonymous-docker-"))
    pull_attempted = False
    cleanup_failed = False
    identity_changed = False
    session: _DockerEngineSession | None = None
    daemon_id: str | None = None
    full_reference = f"ghcr.io/{reference}"
    encoded_reference = urllib.parse.quote(full_reference, safe="")
    list_target = "/images/json?all=1"
    pull_target = "/images/create?" + urllib.parse.urlencode(
        {"fromImage": full_reference, "platform": "linux/amd64"}
    )
    inspect_target = f"/images/{encoded_reference}/json"
    remove_target = f"/images/{encoded_reference}?noprune=1"
    try:
        os.chmod(config_root, 0o700)
        if any(config_root.iterdir()):
            raise PublicationBlocked("anonymous_docker_config_not_empty")
        session = _DockerEngineSession(config_root)
        daemon_id = _docker_daemon_id(session)
        daemon_id_sha256 = hashlib.sha256(daemon_id.encode("utf-8")).hexdigest()
        if daemon_id_sha256 == candidate_engine_id_sha256:
            raise PublicationBlocked("anonymous_docker_not_distinct_empty")
        images_before = _docker_json_array(
            _bound_docker_request(session, daemon_id, "GET", list_target)
        )
        if images_before != []:
            raise PublicationBlocked("anonymous_docker_not_distinct_empty")
        pull_attempted = True
        _verify_docker_pull(
            _bound_docker_request(session, daemon_id, "POST", pull_target)
        )
        inspect_raw = _bound_docker_request(
            session,
            daemon_id,
            "GET",
            inspect_target,
        )
        inspect = _docker_json_object(inspect_raw)
        rootfs = inspect.get("RootFS")
        config = inspect.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        repo_digests = inspect.get("RepoDigests")
        if (
            inspect.get("Id") != identity.config_digest
            or inspect.get("Os") != "linux"
            or inspect.get("Architecture") != "amd64"
            or not isinstance(rootfs, dict)
            or rootfs.get("Layers") != list(identity.ordered_diff_ids)
            or not isinstance(labels, dict)
            or labels.get("org.opencontainers.image.revision")
            != identity.revision_label
            or not isinstance(repo_digests, list)
            or full_reference not in repo_digests
        ):
            raise PublicationBlocked("anonymous_docker_identity_mismatch")
    finally:
        if pull_attempted and session is not None and daemon_id is not None:
            try:
                _docker_json_array(
                    _bound_docker_request(
                        session,
                        daemon_id,
                        "DELETE",
                        remove_target,
                    )
                )
            except Exception as exc:
                if (
                    isinstance(exc, PublicationBlocked)
                    and exc.code == "anonymous_docker_identity_changed"
                ):
                    identity_changed = True
                else:
                    cleanup_failed = True
            try:
                images_after = _docker_json_array(
                    _bound_docker_request(
                        session,
                        daemon_id,
                        "GET",
                        list_target,
                    )
                )
                if images_after != []:
                    cleanup_failed = True
            except Exception as exc:
                if (
                    isinstance(exc, PublicationBlocked)
                    and exc.code == "anonymous_docker_identity_changed"
                ):
                    identity_changed = True
                else:
                    cleanup_failed = True
        if session is not None:
            try:
                closing_daemon_id = _docker_daemon_id(
                    session,
                    close_connection=True,
                )
                if daemon_id is not None and closing_daemon_id != daemon_id:
                    identity_changed = True
            except Exception as exc:
                if (
                    isinstance(exc, PublicationBlocked)
                    and exc.code == "anonymous_docker_identity_changed"
                ):
                    identity_changed = True
                else:
                    cleanup_failed = True
            try:
                session.finish()
            except Exception:
                cleanup_failed = True
        try:
            shutil.rmtree(config_root)
        except OSError:
            cleanup_failed = True
    if identity_changed:
        raise PublicationBlocked("anonymous_docker_identity_changed")
    if cleanup_failed or config_root.exists():
        raise PublicationBlocked("anonymous_docker_cleanup_failed")
    return daemon_id_sha256


def _image_from_request(document: dict[str, Any]) -> ImageIdentity:
    image = document.get("image")
    if not isinstance(image, dict) or set(image) != {
        "oci_index",
        "config_digest",
        "ordered_diff_ids",
        "revision_label",
    }:
        raise PublicationBlocked("anonymous_smoke_request_invalid")
    diff_ids = image["ordered_diff_ids"]
    if not isinstance(diff_ids, list):
        raise PublicationBlocked("anonymous_smoke_request_invalid")
    identity = ImageIdentity(
        oci_index=image["oci_index"],
        config_digest=image["config_digest"],
        ordered_diff_ids=tuple(diff_ids),
        revision_label=image["revision_label"],
    )
    identity.validate()
    return identity


def run_smoke(raw: bytes) -> bytes:
    document = _strict_json_object(raw, "anonymous_smoke_request")
    if (
        set(document)
        != {
            "schema",
            "registry_origin",
            "client_contract",
            "credential_transport",
            "docker_host",
            "docker_client_contract",
            "candidate_docker_engine_id_sha256",
            "reference",
            "image",
        }
        or document["schema"] != "subgen.task12.anonymous-smoke-request/v1"
        or document["registry_origin"] != REGISTRY_API_ORIGIN
        or document["client_contract"] != REGISTRY_CLIENT_CONTRACT
        or document["credential_transport"] != CREDENTIAL_TRANSPORT_CONTRACT
        or document["docker_host"] != ANONYMOUS_DOCKER_HOST
        or document["docker_client_contract"] != ANONYMOUS_DOCKER_CLIENT_CONTRACT
        or canonical_json_bytes(document) != raw
    ):
        raise PublicationBlocked("anonymous_smoke_request_invalid")
    identity = _image_from_request(document)
    reference = document["reference"]
    expected_reference = f"herbertmt978/subgen-english-plex@{identity.oci_index}"
    if reference != expected_reference:
        raise PublicationBlocked("anonymous_smoke_reference_invalid")
    candidate_engine_id_sha256 = document["candidate_docker_engine_id_sha256"]
    if (
        not isinstance(candidate_engine_id_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", candidate_engine_id_sha256) is None
    ):
        raise PublicationBlocked("anonymous_docker_identity_invalid")

    repository = "herbertmt978/subgen-english-plex"
    store = Path(tempfile.mkdtemp(prefix="subgen-task12-anonymous-pull-"))
    cleanup_failed = False
    try:
        os.chmod(store, 0o700)
        if any(store.iterdir()):
            raise PublicationBlocked("anonymous_smoke_store_not_empty")
        client = AnonymousRegistryClient(repository)
        index_headers, index_payload = client.get(
            f"/v2/{repository}/manifests/"
            f"{urllib.parse.quote(identity.oci_index, safe=':')}",
            _INDEX_MEDIA_TYPE,
        )
        _verify_payload(
            index_headers,
            index_payload,
            identity.oci_index,
            len(index_payload),
        )
        _store_payload(store, identity.oci_index, index_payload)
        index = _strict_json_object(index_payload, "anonymous_index")
        descriptors = index.get("manifests")
        if (
            index.get("schemaVersion") != 2
            or index.get("mediaType") != _INDEX_MEDIA_TYPE
            or not isinstance(descriptors, list)
            or not descriptors
        ):
            raise PublicationBlocked("anonymous_smoke_index_invalid")

        selected: tuple[dict[str, Any], list[dict[str, Any]]] | None = None
        observed_children: set[str] = set()
        for descriptor in descriptors:
            child_digest, child_size, _media_type = _verify_descriptor(
                descriptor, media_type=_MANIFEST_MEDIA_TYPE
            )
            if child_digest in observed_children:
                raise PublicationBlocked("anonymous_smoke_index_invalid")
            observed_children.add(child_digest)
            manifest_headers, manifest_payload = client.get(
                f"/v2/{repository}/manifests/"
                f"{urllib.parse.quote(child_digest, safe=':')}",
                _MANIFEST_MEDIA_TYPE,
                maximum=child_size,
            )
            _verify_payload(
                manifest_headers,
                manifest_payload,
                child_digest,
                child_size,
            )
            _store_payload(store, child_digest, manifest_payload)
            manifest = _strict_json_object(manifest_payload, "anonymous_manifest")
            config = manifest.get("config")
            layers = manifest.get("layers")
            if (
                manifest.get("schemaVersion") != 2
                or manifest.get("mediaType") != _MANIFEST_MEDIA_TYPE
                or not isinstance(config, dict)
                or not isinstance(layers, list)
                or not all(isinstance(layer, dict) for layer in layers)
            ):
                raise PublicationBlocked("anonymous_smoke_manifest_invalid")
            if config.get("digest") == identity.config_digest:
                if selected is not None or descriptor.get("platform") != {
                    "architecture": "amd64",
                    "os": "linux",
                }:
                    raise PublicationBlocked("anonymous_smoke_platform_mismatch")
                selected = (config, layers)
        if selected is None:
            raise PublicationBlocked("anonymous_smoke_config_missing")

        config_descriptor, layers = selected
        config_digest, config_size, _config_media_type = _verify_descriptor(
            config_descriptor,
            media_type=_CONFIG_MEDIA_TYPE,
        )
        config_headers, config_payload = client.get(
            f"/v2/{repository}/blobs/{urllib.parse.quote(config_digest, safe=':')}",
            _CONFIG_MEDIA_TYPE,
            maximum=config_size,
        )
        _verify_payload(
            config_headers,
            config_payload,
            config_digest,
            config_size,
            allow_missing_digest_header=True,
        )
        _store_payload(store, config_digest, config_payload)
        derived_diff_ids: list[str] = []
        for layer in layers:
            layer_digest, layer_size, layer_media_type = _verify_descriptor(layer)
            layer_headers, layer_payload = client.get(
                f"/v2/{repository}/blobs/{urllib.parse.quote(layer_digest, safe=':')}",
                layer_media_type,
                maximum=layer_size,
            )
            _verify_payload(
                layer_headers,
                layer_payload,
                layer_digest,
                layer_size,
                allow_missing_digest_header=True,
            )
            _store_payload(store, layer_digest, layer_payload)
            derived_diff_ids.append(
                derive_layer_diff_id(layer_payload, layer_media_type)
            )
        config_document = _strict_json_object(config_payload, "anonymous_config")
        rootfs = config_document.get("rootfs")
        runtime_config = config_document.get("config")
        labels = (
            runtime_config.get("Labels") if isinstance(runtime_config, dict) else None
        )
        if (
            config_digest != identity.config_digest
            or config_document.get("os") != "linux"
            or config_document.get("architecture") != "amd64"
            or not isinstance(rootfs, dict)
            or rootfs.get("type") != "layers"
            or rootfs.get("diff_ids") != list(identity.ordered_diff_ids)
            or derived_diff_ids != list(identity.ordered_diff_ids)
            or not isinstance(labels, dict)
            or labels.get("org.opencontainers.image.revision")
            != identity.revision_label
        ):
            raise PublicationBlocked("anonymous_smoke_identity_mismatch")
    finally:
        try:
            shutil.rmtree(store)
        except OSError:
            cleanup_failed = True
    if cleanup_failed or store.exists():
        raise PublicationBlocked("anonymous_smoke_store_cleanup_failed")
    anonymous_engine_id_sha256 = _docker_smoke(
        reference,
        identity,
        candidate_engine_id_sha256=candidate_engine_id_sha256,
    )
    return canonical_json_bytes(
        {
            "schema": "subgen.task12.anonymous-smoke/v1",
            "reference": reference,
            "anonymous": True,
            "distinct_engine": True,
            "empty_before": True,
            "empty_after": True,
            "http_success": True,
            "docker_host": ANONYMOUS_DOCKER_HOST,
            "docker_client_contract": ANONYMOUS_DOCKER_CLIENT_CONTRACT,
            "anonymous_engine_id_sha256": anonymous_engine_id_sha256,
            "candidate_engine_id_sha256": candidate_engine_id_sha256,
            "image": {
                "oci_index": identity.oci_index,
                "config_digest": identity.config_digest,
                "ordered_diff_ids": list(identity.ordered_diff_ids),
                "revision_label": identity.revision_label,
            },
        }
    )


def main() -> int:
    try:
        sys.stdout.buffer.write(run_smoke(sys.stdin.buffer.read()))
        return 0
    except PublicationBlocked as exc:
        print(exc.code, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
