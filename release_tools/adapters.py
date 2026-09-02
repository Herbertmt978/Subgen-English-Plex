"""Injected command/HTTP adapter for the Task 12 publication state machine.

No request or command is issued at import time.  The registry version method is
deliberately conditional-create-only; ordinary overwrite exists only for the
mutable ``latest`` operation that the state machine calls once and last.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from .task12 import (
    ANONYMOUS_DOCKER_CLIENT_CONTRACT,
    ANONYMOUS_DOCKER_HOST,
    AnnotatedTag,
    CANONICAL_GIT_REMOTE_URL,
    CREDENTIAL_TRANSPORT_CONTRACT,
    GITHUB_API_ORIGIN,
    ImageIdentity,
    LocalSourceProof,
    LockObservation,
    PublicationBlocked,
    PublicState,
    RegistryBlob,
    RegistryManifest,
    RegistryProbeObservation,
    RegistryProbeWrite,
    REGISTRY_PROBE_PREFIX,
    REGISTRY_API_ORIGIN,
    REGISTRY_CLIENT_CONTRACT,
    ReleaseIntent,
    ReleaseView,
    _strict_json_object,
    canonical_json_bytes,
    sha256_bytes,
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class CommandRunner(Protocol):
    """Run argv without a shell; ``environment`` replaces inherited variables."""

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes | None = None,
        environment: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CommandResult: ...


class SubprocessCommandRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes | None = None,
        environment: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CommandResult:
        completed = subprocess.run(
            list(argv),
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
            env=dict(environment or {}),
            cwd=cwd,
            timeout=900,
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str] | tuple[tuple[str, str], ...]
    body: bytes
    redirected: bool = False


class HttpClient(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class UrllibHttpClient:
    """Small no-redirect client; credentials are supplied only in memory."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect())

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> HttpResponse:
        request = urllib.request.Request(
            url,
            data=body,
            headers=dict(headers),
            method=method,
        )
        try:
            with self._opener.open(request, timeout=60) as response:
                return HttpResponse(
                    status=response.status,
                    headers=tuple(response.headers.raw_items()),
                    body=response.read(),
                    redirected=response.geturl() != url,
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                status=exc.code,
                headers=tuple(exc.headers.raw_items()),
                body=exc.read(),
                redirected=300 <= exc.code < 400,
            )
        except urllib.error.URLError as exc:
            raise PublicationBlocked("http_transport_failed") from exc


@dataclass(frozen=True)
class AdapterConfig:
    repository_root: Path = field(default_factory=Path.cwd)
    git_environment: Mapping[str, str] = field(default_factory=dict)
    github_headers: Mapping[str, str] = field(default_factory=dict)
    registry_headers: Mapping[str, str] = field(default_factory=dict)
    lock_tagger: Mapping[str, str] = field(default_factory=dict)
    release_verifier_inputs: Mapping[str, str] = field(default_factory=dict)


class Task12HttpCommandAdapter:
    """Authoritative remote adapter with no hidden fallback write paths."""

    def __init__(
        self,
        command_runner: CommandRunner,
        http_client: HttpClient,
        config: AdapterConfig,
    ) -> None:
        self.command_runner = command_runner
        self.http_client = http_client
        self.config = config
        self._registry_headers = dict(config.registry_headers)
        try:
            repository_root = Path(config.repository_root)
            if not repository_root.is_absolute():
                raise OSError
            self.repository_root = repository_root.resolve(strict=True)
        except OSError as exc:
            raise PublicationBlocked("repository_root_not_exact") from exc
        for headers in (config.github_headers, config.registry_headers):
            if any(
                not isinstance(key, str)
                or not isinstance(value, str)
                or "\r" in key
                or "\n" in key
                or "\r" in value
                or "\n" in value
                for key, value in headers.items()
            ):
                raise PublicationBlocked("credential_header_invalid")

    @staticmethod
    def _json(response: HttpResponse, label: str) -> dict[str, object]:
        if response.redirected:
            raise PublicationBlocked(f"{label}_redirected")
        return _strict_json_object(response.body, label)

    @staticmethod
    def _header(response: HttpResponse, name: str) -> str | None:
        lowered = name.casefold()
        items = (
            tuple(response.headers.items())
            if isinstance(response.headers, Mapping)
            else response.headers
        )
        matches = [value for key, value in items if key.casefold() == lowered]
        if len(matches) > 1:
            raise PublicationBlocked("http_singleton_header_duplicate")
        return matches[0] if matches else None

    @staticmethod
    def _strong_etag(value: str | None) -> str | None:
        """Accept exactly one RFC strong entity-tag, never a tag list."""
        if value is None:
            return None
        if re.fullmatch(r'"[\x21\x23-\x7e]*"', value) is None:
            raise PublicationBlocked("registry_etag_invalid")
        return value

    def _github(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
    ) -> HttpResponse:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            **self.config.github_headers,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        response = self.http_client.request(
            method,
            GITHUB_API_ORIGIN + path,
            headers=headers,
            body=body,
        )
        if response.redirected or 300 <= response.status < 400:
            raise PublicationBlocked("github_redirect_forbidden")
        return response

    def _registry(
        self,
        method: str,
        repository: str,
        reference: str,
        *,
        body: bytes | None = None,
        conditional: bool = False,
        if_match: str | None = None,
        content_type: str | None = None,
    ) -> HttpResponse:
        quoted_reference = urllib.parse.quote(reference, safe="")
        headers = {
            "Accept": (
                "application/vnd.oci.image.index.v1+json,"
                "application/vnd.docker.distribution.manifest.list.v2+json"
            ),
            **self._registry_headers,
        }
        if body is not None:
            headers["Content-Type"] = (
                content_type or "application/vnd.oci.image.index.v1+json"
            )
        if conditional and if_match is not None:
            raise PublicationBlocked("registry_conditional_headers_conflict")
        if conditional:
            headers["If-None-Match"] = "*"
        if if_match is not None:
            headers["If-Match"] = self._strong_etag(if_match) or ""
        response = self.http_client.request(
            method,
            f"{REGISTRY_API_ORIGIN}/v2/{repository}/manifests/{quoted_reference}",
            headers=headers,
            body=body,
        )
        if response.redirected or 300 <= response.status < 400:
            raise PublicationBlocked("registry_redirect_forbidden")
        if method == "PUT":
            self._strong_etag(self._header(response, "ETag"))
        return response

    @staticmethod
    def _git_executable() -> str:
        candidate = (
            Path("/usr/bin/git") if os.name != "nt" else Path(shutil.which("git") or "")
        )
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise PublicationBlocked("release_git_executable_unavailable") from exc
        if not resolved.is_absolute() or not resolved.is_file():
            raise PublicationBlocked("release_git_executable_unavailable")
        return str(resolved)

    @staticmethod
    def _local_git_environment() -> dict[str, str]:
        return {
            "PATH": os.defpath,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_PAGER": "cat",
            "GIT_EXTERNAL_DIFF": "",
            "LC_ALL": "C",
        }

    def _release_blob(self, intent: ReleaseIntent, relative_path: str) -> bytes:
        if (
            not isinstance(relative_path, str)
            or re.fullmatch(r"release_tools/[a-z0-9_]+\.py", relative_path) is None
        ):
            raise PublicationBlocked("release_tool_path_invalid")
        result = self.command_runner.run(
            (
                self._git_executable(),
                "--no-replace-objects",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "core.fsmonitor=false",
                "show",
                f"{intent.release_commit}:{relative_path}",
            ),
            environment=self._local_git_environment(),
            cwd=self.repository_root,
        )
        if (
            result.returncode != 0
            or result.stderr
            or not result.stdout
            or len(result.stdout) > 4 * 1024 * 1024
        ):
            raise PublicationBlocked("release_tool_blob_unavailable")
        return result.stdout

    @staticmethod
    def _write_private_file(path: Path, payload: bytes) -> None:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(path, flags, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise PublicationBlocked("release_tool_materialization_failed") from exc

    def _run_release_tool(
        self,
        intent: ReleaseIntent,
        script_name: str,
        label: str,
        *,
        stdin: bytes,
    ) -> bytes:
        if script_name not in {"source_proof.py", "anonymous_smoke.py"}:
            raise PublicationBlocked("release_tool_path_invalid")
        payloads = {
            name: self._release_blob(intent, f"release_tools/{name}")
            for name in (
                "__init__.py",
                "task12.py",
                "adapters.py",
                "journal.py",
                script_name,
            )
        }
        materialized = Path(tempfile.mkdtemp(prefix=f"subgen-task12-{label}-"))
        cleanup_failed = False
        result: CommandResult | None = None
        try:
            os.chmod(materialized, 0o700)
            if any(materialized.iterdir()):
                raise PublicationBlocked("release_tool_materialization_not_empty")
            package = materialized / "release_tools"
            package.mkdir(mode=0o700)
            for name, payload in payloads.items():
                self._write_private_file(package / name, payload)
            result = self.command_runner.run(
                (
                    sys.executable,
                    "-I",
                    str(package / script_name),
                ),
                stdin=stdin,
                environment={
                    "PATH": os.defpath,
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONUTF8": "1",
                },
                cwd=materialized,
            )
        finally:
            try:
                shutil.rmtree(materialized)
            except OSError:
                cleanup_failed = True
        if cleanup_failed or materialized.exists():
            raise PublicationBlocked("release_tool_materialization_cleanup_failed")
        if result is None or result.returncode != 0 or result.stderr:
            raise PublicationBlocked(f"{label}_command_failed")
        return result.stdout

    def _isolated_git_push(self, argv: Sequence[str], label: str) -> bytes:
        if not argv or argv[0] != self._git_executable():
            raise PublicationBlocked(f"{label}_command_missing")
        isolated = Path(tempfile.mkdtemp(prefix="subgen-task12-git-"))
        cleanup_failed = False
        result: CommandResult | None = None
        base_environment = {
            "PATH": os.defpath,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_PAGER": "cat",
            "GIT_EXTERNAL_DIFF": "",
            "LC_ALL": "C",
        }
        push_environment = {
            **self.config.git_environment,
            **base_environment,
        }
        try:
            os.chmod(isolated, 0o700)
            if any(isolated.iterdir()):
                raise PublicationBlocked("isolated_git_directory_not_empty")
            initialized = self.command_runner.run(
                (self._git_executable(), "init", "--bare", "--quiet"),
                environment=base_environment,
                cwd=isolated,
            )
            if initialized.returncode != 0 or initialized.stderr:
                raise PublicationBlocked("isolated_git_init_failed")
            result = self.command_runner.run(
                argv,
                environment=push_environment,
                cwd=isolated,
            )
        finally:
            try:
                shutil.rmtree(isolated)
            except OSError:
                cleanup_failed = True
        if cleanup_failed or isolated.exists():
            raise PublicationBlocked("isolated_git_cleanup_failed")
        if result is None or result.returncode != 0 or result.stderr:
            raise PublicationBlocked(f"{label}_command_failed")
        return result.stdout

    @staticmethod
    def _image_from(document: object, label: str) -> ImageIdentity:
        if not isinstance(document, dict) or set(document) != {
            "oci_index",
            "config_digest",
            "ordered_diff_ids",
            "revision_label",
        }:
            raise PublicationBlocked(f"{label}_image_schema")
        diff_ids = document["ordered_diff_ids"]
        if not isinstance(diff_ids, list) or not all(
            isinstance(item, str) for item in diff_ids
        ):
            raise PublicationBlocked(f"{label}_image_schema")
        image = ImageIdentity(
            oci_index=document["oci_index"],  # type: ignore[arg-type]
            config_digest=document["config_digest"],  # type: ignore[arg-type]
            ordered_diff_ids=tuple(diff_ids),
            revision_label=document["revision_label"],  # type: ignore[arg-type]
        )
        image.validate()
        return image

    def verify_local_sources(self, intent: ReleaseIntent) -> LocalSourceProof:
        request = canonical_json_bytes(
            {
                "schema": "subgen.task12.source-proof-request/v1",
                "binding_sha256": intent.binding_sha256,
                "repository_root": str(self.repository_root),
                "repository": intent.repository,
                "prior_main_commit": intent.prior_main_commit,
                "runtime_commit": intent.runtime_commit,
                "sampler_commit": intent.sampler_commit,
                "release_commit": intent.release_commit,
                "annotated_tag_object": intent.annotated_tag.object_sha,
                "annotated_tag_target": intent.annotated_tag.target_commit,
                "annotated_tag_name": intent.annotated_tag.tag,
                "annotated_tag_message": intent.annotated_tag.message,
                "annotated_tagger_name": intent.annotated_tag.tagger_name,
                "annotated_tagger_email": intent.annotated_tag.tagger_email,
                "annotated_tagger_date": intent.annotated_tag.tagger_date,
                "release_notes_blob": intent.release_notes_blob,
                "release_notes_base64": base64.b64encode(intent.release_notes).decode(
                    "ascii"
                ),
                "task11b_verifier_receipt_sha256": (
                    intent.task11b_verifier_receipt_sha256
                ),
                "task11b_verifier_receipt_base64": base64.b64encode(
                    intent.task11b_verifier_receipt
                ).decode("ascii"),
                "release_verifier_inputs": dict(self.config.release_verifier_inputs),
                "image": {
                    "oci_index": intent.image.oci_index,
                    "config_digest": intent.image.config_digest,
                    "ordered_diff_ids": list(intent.image.ordered_diff_ids),
                    "revision_label": intent.image.revision_label,
                },
            }
        )
        raw = self._run_release_tool(
            intent,
            "source_proof.py",
            "source_proof",
            stdin=request,
        )
        document = _strict_json_object(raw, "source_proof")
        expected = {
            "schema",
            "binding_sha256",
            "clean_worktree",
            "workflows_manual_only",
            "runtime_commit",
            "sampler_commit",
            "release_commit",
            "runtime_is_ancestor_of_sampler",
            "sampler_is_ancestor_of_release",
            "annotated_tag_object",
            "annotated_tag_target",
            "release_notes_blob",
            "release_notes_base64",
            "task11b_verifier_receipt_sha256",
            "candidate_docker_engine_id_sha256",
            "image",
            "git_remote_url",
        }
        if (
            set(document) != expected
            or document["schema"] != "subgen.task12.source-proof/v1"
        ):
            raise PublicationBlocked("source_proof_schema")
        if document["binding_sha256"] != intent.binding_sha256:
            raise PublicationBlocked("source_proof_binding_mismatch")
        encoded_notes = document["release_notes_base64"]
        if not isinstance(encoded_notes, str):
            raise PublicationBlocked("source_proof_release_notes_invalid")
        try:
            notes = base64.b64decode(encoded_notes, validate=True)
        except ValueError as exc:
            raise PublicationBlocked("source_proof_release_notes_invalid") from exc
        if canonical_json_bytes(document) != raw:
            raise PublicationBlocked("source_proof_not_canonical")
        return LocalSourceProof(
            clean_worktree=document["clean_worktree"] is True,
            workflows_manual_only=document["workflows_manual_only"] is True,
            runtime_commit=document["runtime_commit"],  # type: ignore[arg-type]
            sampler_commit=document["sampler_commit"],  # type: ignore[arg-type]
            release_commit=document["release_commit"],  # type: ignore[arg-type]
            runtime_is_ancestor_of_sampler=(
                document["runtime_is_ancestor_of_sampler"] is True
            ),
            sampler_is_ancestor_of_release=(
                document["sampler_is_ancestor_of_release"] is True
            ),
            annotated_tag_object=document["annotated_tag_object"],  # type: ignore[arg-type]
            annotated_tag_target=document["annotated_tag_target"],  # type: ignore[arg-type]
            release_notes_blob=document["release_notes_blob"],  # type: ignore[arg-type]
            release_notes=notes,
            task11b_verifier_receipt_sha256=document["task11b_verifier_receipt_sha256"],  # type: ignore[arg-type]
            candidate_docker_engine_id_sha256=document[
                "candidate_docker_engine_id_sha256"
            ],  # type: ignore[arg-type]
            image=self._image_from(document["image"], "source_proof"),
            git_remote_url=document["git_remote_url"],  # type: ignore[arg-type]
        )

    def fetch_all_actions_run_ids(self, intent: ReleaseIntent) -> Sequence[int]:
        page = 1
        total: int | None = None
        identifiers: list[int] = []
        while True:
            response = self._github(
                "GET",
                f"/repos/{intent.repository}/actions/runs?per_page=100&page={page}",
            )
            if response.status != 200:
                raise PublicationBlocked("actions_query_not_authoritative")
            document = self._json(response, "actions_runs")
            if set(document) < {"total_count", "workflow_runs"}:
                raise PublicationBlocked("actions_response_schema")
            observed_total = document["total_count"]
            runs = document["workflow_runs"]
            if (
                isinstance(observed_total, bool)
                or not isinstance(observed_total, int)
                or observed_total < 0
                or not isinstance(runs, list)
                or (total is not None and observed_total != total)
            ):
                raise PublicationBlocked("actions_response_inconsistent")
            total = observed_total
            for run in runs:
                if not isinstance(run, dict):
                    raise PublicationBlocked("actions_response_schema")
                identifier = run.get("id")
                status = run.get("status")
                if (
                    isinstance(identifier, bool)
                    or not isinstance(identifier, int)
                    or identifier <= 0
                    or status not in {None, "completed"}
                ):
                    raise PublicationBlocked("actions_run_not_quiescent")
                identifiers.append(identifier)
            if len(identifiers) > total:
                raise PublicationBlocked("actions_response_inconsistent")
            if len(identifiers) == total:
                break
            if not runs or len(runs) != 100:
                raise PublicationBlocked("actions_pagination_incomplete")
            page += 1
        if len(set(identifiers)) != len(identifiers):
            raise PublicationBlocked("actions_run_id_duplicate")
        return identifiers

    def _git_ref(self, intent: ReleaseIntent, ref: str) -> tuple[str, str] | None:
        short_ref = ref.removeprefix("refs/")
        response = self._github(
            "GET",
            f"/repos/{intent.repository}/git/ref/{urllib.parse.quote(short_ref, safe='/')}",
        )
        if response.status == 404:
            return None
        if response.status != 200:
            raise PublicationBlocked("git_ref_query_not_authoritative")
        document = self._json(response, "git_ref")
        obj = document.get("object")
        if (
            not isinstance(obj, dict)
            or not isinstance(obj.get("sha"), str)
            or not isinstance(obj.get("type"), str)
        ):
            raise PublicationBlocked("git_ref_response_schema")
        return obj["sha"], obj["type"]

    def _tag_document(
        self, intent: ReleaseIntent, object_sha: str
    ) -> dict[str, object]:
        response = self._github(
            "GET", f"/repos/{intent.repository}/git/tags/{object_sha}"
        )
        if response.status != 200:
            raise PublicationBlocked("git_tag_query_not_authoritative")
        return self._json(response, "git_tag")

    @staticmethod
    def _assert_version_tag_document(
        document: Mapping[str, object], expected: AnnotatedTag
    ) -> None:
        target = document.get("object")
        tagger = document.get("tagger")
        if not isinstance(target, dict) or not isinstance(tagger, dict):
            raise PublicationBlocked("annotated_tag_response_schema")
        if (
            document.get("sha") != expected.object_sha
            or document.get("tag") != expected.tag
            or document.get("message") != expected.message
            or target.get("type") != "commit"
            or target.get("sha") != expected.target_commit
            or tagger.get("name") != expected.tagger_name
            or tagger.get("email") != expected.tagger_email
            or tagger.get("date") != expected.tagger_date
        ):
            raise PublicationBlocked("annotated_tag_remote_mismatch")

    def _release(self, intent: ReleaseIntent) -> ReleaseView | None:
        response = self._github(
            "GET", f"/repos/{intent.repository}/releases/tags/{intent.version_tag}"
        )
        if response.status == 404:
            return None
        if response.status != 200:
            raise PublicationBlocked("github_release_query_not_authoritative")
        document = self._json(response, "github_release")
        body = document.get("body")
        if not isinstance(body, str):
            raise PublicationBlocked("github_release_response_schema")
        try:
            body_bytes = body.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise PublicationBlocked("github_release_body_encoding") from exc
        return ReleaseView(
            tag=document.get("tag_name"),  # type: ignore[arg-type]
            title=document.get("name"),  # type: ignore[arg-type]
            draft=document.get("draft"),  # type: ignore[arg-type]
            prerelease=document.get("prerelease"),  # type: ignore[arg-type]
            body=body_bytes,
        )

    def _manifest_digest(self, intent: ReleaseIntent, reference: str) -> str | None:
        digest, _etag = self._manifest_identity(intent, reference)
        return digest

    def _manifest_identity(
        self, intent: ReleaseIntent, reference: str
    ) -> tuple[str | None, str | None]:
        response = self._registry("GET", intent.image_repository, reference)
        if response.status == 404:
            return None, None
        if response.status != 200:
            raise PublicationBlocked("registry_query_not_authoritative")
        digest = self._header(response, "Docker-Content-Digest")
        if not isinstance(digest, str):
            raise PublicationBlocked("registry_digest_header_missing")
        etag = self._strong_etag(self._header(response, "ETag"))
        return digest, etag

    def read_public_state(
        self, intent: ReleaseIntent, lock_document_sha256: str
    ) -> PublicState:
        main = self._git_ref(intent, intent.main_ref)
        if main is None or main[1] != "commit":
            raise PublicationBlocked("remote_main_missing_or_not_commit")
        tag_ref = self._git_ref(intent, f"refs/tags/{intent.version_tag}")
        tag_object: str | None = None
        if tag_ref is not None:
            if tag_ref[1] != "tag":
                raise PublicationBlocked("remote_version_tag_not_annotated")
            self._assert_version_tag_document(
                self._tag_document(intent, tag_ref[0]), intent.annotated_tag
            )
            tag_object = tag_ref[0]
        lock_ref = self._git_ref(intent, intent.lock_ref)
        lock: LockObservation | None = None
        if lock_ref is not None:
            if lock_ref[1] != "tag":
                raise PublicationBlocked("publication_lock_not_annotated")
            lock_tag = self._tag_document(intent, lock_ref[0])
            target = lock_tag.get("object")
            message = lock_tag.get("message")
            if (
                not isinstance(target, dict)
                or target.get("type") != "commit"
                or target.get("sha") != intent.release_commit
                or not isinstance(message, str)
            ):
                raise PublicationBlocked("publication_lock_document_invalid")
            digest = sha256_bytes(message.encode("utf-8"))
            lock = LockObservation(lock_ref[0], digest)
        return PublicState(
            main_commit=main[0],
            version_tag_object=tag_object,
            release=self._release(intent),
            version_digest=self._manifest_digest(intent, intent.version_tag),
            latest_digest=self._manifest_digest(intent, "latest"),
            lock=lock,
        )

    def _create_tag_object(
        self,
        intent: ReleaseIntent,
        *,
        tag: str,
        message: str,
        tagger: Mapping[str, str],
    ) -> str:
        if set(tagger) != {"name", "email", "date"}:
            raise PublicationBlocked("tagger_identity_incomplete")
        response = self._github(
            "POST",
            f"/repos/{intent.repository}/git/tags",
            body=canonical_json_bytes(
                {
                    "tag": tag,
                    "message": message,
                    "object": intent.release_commit,
                    "type": "commit",
                    "tagger": dict(tagger),
                }
            ),
        )
        if response.status != 201:
            raise PublicationBlocked("git_tag_object_create_failed")
        document = self._json(response, "git_tag_create")
        object_sha = document.get("sha")
        if not isinstance(object_sha, str):
            raise PublicationBlocked("git_tag_create_response_schema")
        return object_sha

    def create_lock_object(self, intent: ReleaseIntent, lock_document: bytes) -> str:
        try:
            message = lock_document.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise PublicationBlocked("lock_document_encoding") from exc
        return self._create_tag_object(
            intent,
            tag=intent.lock_ref.rsplit("/", 1)[-1],
            message=message,
            tagger=self.config.lock_tagger,
        )

    def _create_ref(self, intent: ReleaseIntent, ref: str, sha: str) -> None:
        response = self._github(
            "POST",
            f"/repos/{intent.repository}/git/refs",
            body=canonical_json_bytes({"ref": ref, "sha": sha}),
        )
        if response.status != 201:
            raise PublicationBlocked("git_ref_create_failed")

    def create_lock_ref(self, intent: ReleaseIntent, object_sha: str) -> None:
        self._create_ref(intent, intent.lock_ref, object_sha)

    def assert_lock(
        self,
        intent: ReleaseIntent,
        object_sha: str,
        lock_document_sha256: str,
    ) -> None:
        observed = self._git_ref(intent, intent.lock_ref)
        if observed != (object_sha, "tag"):
            raise PublicationBlocked("publication_lock_missing_or_replaced")
        document = self._tag_document(intent, object_sha)
        message = document.get("message")
        target = document.get("object")
        if (
            not isinstance(message, str)
            or sha256_bytes(message.encode("utf-8")) != lock_document_sha256
            or not isinstance(target, dict)
            or target.get("type") != "commit"
            or target.get("sha") != intent.release_commit
        ):
            raise PublicationBlocked("publication_lock_missing_or_replaced")

    def advance_main(self, intent: ReleaseIntent) -> None:
        response = self._github(
            "PATCH",
            f"/repos/{intent.repository}/git/refs/heads/main",
            body=canonical_json_bytes(
                {
                    "sha": intent.release_commit,
                    "force": False,
                }
            ),
        )
        if response.status != 200:
            raise PublicationBlocked("main_fast_forward_failed")
        document = self._json(response, "main_fast_forward")
        target = document.get("object")
        if not isinstance(target, dict) or target.get("sha") != intent.release_commit:
            raise PublicationBlocked("main_fast_forward_response_mismatch")

    def create_version_tag_object(self, intent: ReleaseIntent) -> str:
        expected = intent.annotated_tag
        result = self._create_tag_object(
            intent,
            tag=expected.tag,
            message=expected.message,
            tagger={
                "name": expected.tagger_name,
                "email": expected.tagger_email,
                "date": expected.tagger_date,
            },
        )
        if result != expected.object_sha:
            raise PublicationBlocked("annotated_tag_object_response_mismatch")
        return result

    def create_version_tag_ref(self, intent: ReleaseIntent) -> None:
        self._create_ref(
            intent,
            f"refs/tags/{intent.version_tag}",
            intent.annotated_tag.object_sha,
        )

    def registry_blob_present(self, intent: ReleaseIntent, blob: RegistryBlob) -> bool:
        response = self.http_client.request(
            "HEAD",
            f"{REGISTRY_API_ORIGIN}/v2/{intent.image_repository}"
            f"/blobs/{urllib.parse.quote(blob.digest, safe=':')}",
            headers={**self._registry_headers},
        )
        if response.redirected or 300 <= response.status < 400:
            raise PublicationBlocked("registry_blob_redirect_forbidden")
        if response.status == 404:
            return False
        if response.status != 200:
            raise PublicationBlocked("registry_blob_query_not_authoritative")
        digest = self._header(response, "Docker-Content-Digest")
        length = self._header(response, "Content-Length")
        if digest != blob.digest or length != str(blob.size):
            raise PublicationBlocked("registry_blob_remote_mismatch")
        return True

    def start_registry_blob_upload(
        self, intent: ReleaseIntent, blob: RegistryBlob
    ) -> str:
        response = self.http_client.request(
            "POST",
            f"{REGISTRY_API_ORIGIN}/v2/{intent.image_repository}/blobs/uploads/",
            headers={**self._registry_headers},
        )
        if response.redirected or response.status != 202:
            raise PublicationBlocked("registry_blob_upload_start_failed")
        location = self._header(response, "Location")
        if not isinstance(location, str):
            raise PublicationBlocked("registry_blob_upload_location_invalid")
        return self._validated_upload_url(intent, location)

    def _validated_upload_url(self, intent: ReleaseIntent, location: str) -> str:
        base = REGISTRY_API_ORIGIN + "/"
        absolute = urllib.parse.urljoin(base, location)
        base_parts = urllib.parse.urlsplit(base)
        parts = urllib.parse.urlsplit(absolute)
        expected_prefix = f"/v2/{intent.image_repository}/blobs/uploads/"
        if (
            parts.scheme != base_parts.scheme
            or parts.netloc != base_parts.netloc
            or parts.username is not None
            or parts.password is not None
            or not parts.path.startswith(expected_prefix)
            or parts.fragment
        ):
            raise PublicationBlocked("registry_blob_upload_location_invalid")
        return absolute

    def finish_registry_blob_upload(
        self, intent: ReleaseIntent, blob: RegistryBlob, upload_url: str
    ) -> None:
        absolute = self._validated_upload_url(intent, upload_url)
        parts = urllib.parse.urlsplit(absolute)
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if any(key == "digest" for key, _value in query):
            raise PublicationBlocked("registry_blob_upload_location_invalid")
        query.append(("digest", blob.digest))
        final_url = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), "")
        )
        response = self.http_client.request(
            "PUT",
            final_url,
            headers={
                "Content-Type": "application/octet-stream",
                **self._registry_headers,
            },
            body=blob.payload,
        )
        if response.redirected or response.status != 201:
            raise PublicationBlocked("registry_blob_upload_finish_failed")
        if self._header(response, "Docker-Content-Digest") != blob.digest:
            raise PublicationBlocked("registry_blob_upload_digest_mismatch")

    def registry_manifest_present(
        self, intent: ReleaseIntent, manifest: RegistryManifest
    ) -> bool:
        response = self._registry("GET", intent.image_repository, manifest.digest)
        if response.status == 404:
            return False
        if response.status != 200:
            raise PublicationBlocked("registry_manifest_query_not_authoritative")
        if (
            self._header(response, "Docker-Content-Digest") != manifest.digest
            or self._header(response, "Content-Length") != str(manifest.size)
            or response.body != manifest.payload
        ):
            raise PublicationBlocked("registry_manifest_remote_mismatch")
        return True

    def put_registry_manifest(
        self, intent: ReleaseIntent, manifest: RegistryManifest
    ) -> None:
        response = self._registry(
            "PUT",
            intent.image_repository,
            manifest.digest,
            body=manifest.payload,
            content_type=manifest.media_type,
        )
        if response.status != 201:
            raise PublicationBlocked("registry_manifest_put_failed")
        if self._header(response, "Docker-Content-Digest") != manifest.digest:
            raise PublicationBlocked("registry_manifest_put_digest_mismatch")

    @staticmethod
    def _validate_probe_reference(reference: str) -> None:
        if (
            not isinstance(reference, str)
            or not reference.startswith(REGISTRY_PROBE_PREFIX)
            or len(reference) > 128
            or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", reference) is None
            or reference in {"latest", "v0.5.0"}
        ):
            raise PublicationBlocked("registry_probe_reference_invalid")

    def read_registry_probe(
        self, intent: ReleaseIntent, reference: str
    ) -> RegistryProbeObservation:
        self._validate_probe_reference(reference)
        response = self._registry("GET", intent.image_repository, reference)
        if response.status == 404:
            return RegistryProbeObservation(None, None, None)
        if response.status != 200:
            raise PublicationBlocked("registry_probe_query_not_authoritative")
        digest = self._header(response, "Docker-Content-Digest")
        if not isinstance(digest, str):
            raise PublicationBlocked("registry_probe_digest_missing")
        etag = self._strong_etag(self._header(response, "ETag"))
        if self._header(response, "Content-Length") != str(len(response.body)):
            raise PublicationBlocked("registry_probe_content_length_invalid")
        return RegistryProbeObservation(digest, etag, response.body)

    def put_registry_probe(
        self,
        intent: ReleaseIntent,
        operation: str,
        reference: str,
        manifest: RegistryManifest,
        *,
        if_none_match: bool = False,
        if_match: str | None = None,
    ) -> RegistryProbeWrite:
        allowed_operations = {
            "registry_version_probe_seed",
            "registry_version_probe_reject",
            "registry_latest_probe_seed",
            "registry_latest_probe_cas",
            "registry_latest_probe_stale",
        }
        self._validate_probe_reference(reference)
        manifest.validate()
        if (
            operation not in allowed_operations
            or (if_none_match == (if_match is not None))
            or manifest.media_type != "application/vnd.oci.image.index.v1+json"
        ):
            raise PublicationBlocked("registry_probe_request_invalid")
        response = self._registry(
            "PUT",
            intent.image_repository,
            reference,
            body=manifest.payload,
            conditional=if_none_match,
            if_match=if_match,
            content_type=manifest.media_type,
        )
        if response.status not in {201, 412}:
            raise PublicationBlocked("registry_probe_response_invalid")
        digest = self._header(response, "Docker-Content-Digest")
        if digest is not None and not isinstance(digest, str):
            raise PublicationBlocked("registry_probe_response_invalid")
        return RegistryProbeWrite(response.status, digest)

    def conditional_create_version(self, intent: ReleaseIntent) -> None:
        response = self._registry(
            "PUT",
            intent.image_repository,
            intent.version_tag,
            body=intent.sealed_manifest,
            conditional=True,
        )
        if response.status == 201:
            response_digest = self._header(response, "Docker-Content-Digest")
            if response_digest not in {None, intent.image.oci_index}:
                raise PublicationBlocked("registry_version_create_digest_mismatch")
            return
        if (
            response.status == 412
            and self._manifest_digest(intent, intent.version_tag)
            == intent.image.oci_index
        ):
            return
        raise PublicationBlocked("registry_conditional_create_failed")

    def anonymous_pull_smoke(
        self,
        intent: ReleaseIntent,
        candidate_docker_engine_id_sha256: str,
    ) -> ImageIdentity:
        if re.fullmatch(r"[0-9a-f]{64}", candidate_docker_engine_id_sha256) is None:
            raise PublicationBlocked("anonymous_docker_identity_unbound")
        reference = f"{intent.image_repository}@{intent.image.oci_index}"
        request = canonical_json_bytes(
            {
                "schema": "subgen.task12.anonymous-smoke-request/v1",
                "registry_origin": REGISTRY_API_ORIGIN,
                "client_contract": REGISTRY_CLIENT_CONTRACT,
                "credential_transport": CREDENTIAL_TRANSPORT_CONTRACT,
                "docker_host": ANONYMOUS_DOCKER_HOST,
                "docker_client_contract": ANONYMOUS_DOCKER_CLIENT_CONTRACT,
                "candidate_docker_engine_id_sha256": (
                    candidate_docker_engine_id_sha256
                ),
                "reference": reference,
                "image": {
                    "oci_index": intent.image.oci_index,
                    "config_digest": intent.image.config_digest,
                    "ordered_diff_ids": list(intent.image.ordered_diff_ids),
                    "revision_label": intent.image.revision_label,
                },
            }
        )
        raw = self._run_release_tool(
            intent,
            "anonymous_smoke.py",
            "anonymous_smoke",
            stdin=request,
        )
        document = _strict_json_object(raw, "anonymous_smoke")
        if (
            set(document)
            != {
                "schema",
                "reference",
                "anonymous",
                "distinct_engine",
                "empty_before",
                "empty_after",
                "http_success",
                "docker_host",
                "docker_client_contract",
                "anonymous_engine_id_sha256",
                "candidate_engine_id_sha256",
                "image",
            }
            or document["schema"] != "subgen.task12.anonymous-smoke/v1"
            or document["reference"] != reference
            or document["docker_host"] != ANONYMOUS_DOCKER_HOST
            or document["docker_client_contract"] != ANONYMOUS_DOCKER_CLIENT_CONTRACT
            or document["candidate_engine_id_sha256"]
            != candidate_docker_engine_id_sha256
            or not isinstance(document["anonymous_engine_id_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", document["anonymous_engine_id_sha256"])
            is None
            or document["anonymous_engine_id_sha256"]
            == candidate_docker_engine_id_sha256
            or any(
                document[key] is not True
                for key in (
                    "anonymous",
                    "distinct_engine",
                    "empty_before",
                    "empty_after",
                    "http_success",
                )
            )
            or canonical_json_bytes(document) != raw
        ):
            raise PublicationBlocked("anonymous_smoke_contract_failed")
        return self._image_from(document["image"], "anonymous_smoke")

    def create_release(self, intent: ReleaseIntent) -> None:
        if self._release(intent) is not None:
            raise PublicationBlocked("github_release_not_absent_before_create")
        body_text = intent.release_notes.decode("utf-8", errors="strict")
        response = self._github(
            "POST",
            f"/repos/{intent.repository}/releases",
            body=canonical_json_bytes(
                {
                    "tag_name": intent.version_tag,
                    "target_commitish": intent.release_commit,
                    "name": intent.release_title,
                    "body": body_text,
                    "draft": False,
                    "prerelease": False,
                }
            ),
        )
        if response.status != 201:
            raise PublicationBlocked("github_release_create_failed")
        observed = self._release(intent)
        if observed is None or not observed.is_exact(intent):
            raise PublicationBlocked("github_release_postcondition_failed")

    def update_latest(
        self,
        intent: ReleaseIntent,
        expected_prior_digest: str | None,
    ) -> None:
        current_digest, current_etag = self._manifest_identity(intent, "latest")
        if current_digest != expected_prior_digest:
            raise PublicationBlocked("registry_latest_changed_before_compare_and_set")
        if expected_prior_digest is not None and current_etag is None:
            raise PublicationBlocked("registry_latest_etag_missing")
        response = self._registry(
            "PUT",
            intent.image_repository,
            "latest",
            body=intent.sealed_manifest,
            conditional=expected_prior_digest is None,
            if_match=current_etag if expected_prior_digest is not None else None,
        )
        if response.status == 201:
            response_digest = self._header(response, "Docker-Content-Digest")
            if response_digest not in {None, intent.image.oci_index}:
                raise PublicationBlocked("registry_latest_update_digest_mismatch")
            return
        if response.status == 412:
            settled, _etag = self._manifest_identity(intent, "latest")
            if settled == intent.image.oci_index:
                return
            raise PublicationBlocked("registry_latest_compare_and_set_failed")
        raise PublicationBlocked("registry_latest_update_failed")

    def remove_lock_exact(self, intent: ReleaseIntent, object_sha: str) -> None:
        argv = (
            self._git_executable(),
            "push",
            "--porcelain",
            f"--force-with-lease={intent.lock_ref}:{object_sha}",
            CANONICAL_GIT_REMOTE_URL,
            f":{intent.lock_ref}",
        )
        self._isolated_git_push(argv, "lock_remove")
