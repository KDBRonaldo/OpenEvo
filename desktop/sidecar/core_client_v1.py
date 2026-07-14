"""Strict active-tunnel client for the Core Control API v1 contract."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
import re
import threading
from typing import Any, Literal, TypeVar
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from openevo.backend.contracts.v1 import models as v1


MAX_CORE_ERROR_RESPONSE_BYTES = 64 * 1024
MAX_CORE_JSON_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_CORE_CAPABILITIES_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_CORE_ARTIFACT_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_CORE_LOG_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CORE_REQUEST_BYTES = 2 * 1024 * 1024
MAX_CORE_WORKSPACE_CHUNK_REQUEST_BYTES = ((v1.MAX_WORKSPACE_CHUNK_BYTES + 2) // 3) * 4 + 4096
MAX_CORE_SSE_FRAME_BYTES = 4 * 1024 * 1024
MAX_CORE_SSE_RESPONSE_BYTES = 64 * 1024 * 1024

_BEARER = re.compile(r"[A-Za-z0-9._~+/\-]{43,510}={0,2}\Z", re.ASCII)
_ETAG = re.compile(r'"[0-9a-f]{64}"\Z', re.ASCII)
_HEADER_VALUE = re.compile(r"[^\x00-\x1f\x7f]{1,256}\Z", re.ASCII)
_OPAQUE_ID = TypeAdapter(v1.OpaqueId)
_CURSOR = TypeAdapter(v1.Cursor)

ResponseT = TypeVar("ResponseT")


class CoreClientLocalErrorCodeV1(StrEnum):
    CONNECTION_FAILED = "core_connection_failed"
    CLIENT_CLOSED = "core_client_closed"
    INVALID_CONNECTION = "invalid_core_tunnel_connection"
    INVALID_REQUEST = "invalid_core_request"
    RESPONSE_TOO_LARGE = "core_response_too_large"
    INVALID_RESPONSE = "invalid_core_response"
    INVALID_ERROR_RESPONSE = "invalid_core_error_response"
    REDIRECT_REJECTED = "core_redirect_rejected"
    ACTIVE_PROJECT_MISMATCH = "active_project_mismatch"
    SSE_PROTOCOL_ERROR = "core_sse_protocol_error"


_LOCAL_ERROR_MESSAGES: dict[CoreClientLocalErrorCodeV1, str] = {
    CoreClientLocalErrorCodeV1.CONNECTION_FAILED: "Desktop could not reach the active Core tunnel.",
    CoreClientLocalErrorCodeV1.CLIENT_CLOSED: "The Core client is closed.",
    CoreClientLocalErrorCodeV1.INVALID_CONNECTION: "The active Core tunnel connection is invalid.",
    CoreClientLocalErrorCodeV1.INVALID_REQUEST: "The Core request does not satisfy the v1 contract.",
    CoreClientLocalErrorCodeV1.RESPONSE_TOO_LARGE: "Core returned a response above the allowed limit.",
    CoreClientLocalErrorCodeV1.INVALID_RESPONSE: "Core returned an invalid v1 response.",
    CoreClientLocalErrorCodeV1.INVALID_ERROR_RESPONSE: "Core returned an invalid v1 error.",
    CoreClientLocalErrorCodeV1.REDIRECT_REJECTED: "Core redirects are not allowed.",
    CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH: (
        "The Core resource does not belong to the active project."
    ),
    CoreClientLocalErrorCodeV1.SSE_PROTOCOL_ERROR: "Core returned an invalid v1 event stream.",
}


class CoreClientLocalErrorV1(BaseModel):
    """Closed, user-safe errors produced locally by the transport boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    code: CoreClientLocalErrorCodeV1
    message: str
    retryable: bool


class CoreClientErrorV1(RuntimeError):
    """Raised with either the exact Core ApiErrorV1 or a closed local error."""

    def __init__(
        self,
        status_code: int,
        error: v1.ApiErrorV1 | CoreClientLocalErrorV1,
    ) -> None:
        super().__init__("OpenEvo Core request failed.")
        self.status_code = status_code
        self.error = error


@dataclass(frozen=True, slots=True)
class CoreTunnelConnectionV1:
    """One active project session's private loopback tunnel authority."""

    endpoint: str
    bearer_token: str = field(repr=False)
    project_id: str
    session_id: str
    origin: str = field(init=False)

    def __post_init__(self) -> None:
        try:
            parsed = urlsplit(self.endpoint)
            port = parsed.port
        except (TypeError, ValueError) as exc:
            raise _local_exception(CoreClientLocalErrorCodeV1.INVALID_CONNECTION, 400) from exc
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or port is None
            or not 1 <= port <= 65535
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_CONNECTION, 400)
        if not isinstance(self.bearer_token, str) or not _BEARER.fullmatch(self.bearer_token):
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_CONNECTION, 400)
        if len(set(self.bearer_token.rstrip("="))) < 8:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_CONNECTION, 400)
        try:
            project_id = _OPAQUE_ID.validate_python(self.project_id, strict=True)
            session_id = _OPAQUE_ID.validate_python(self.session_id, strict=True)
        except ValidationError as exc:
            raise _local_exception(CoreClientLocalErrorCodeV1.INVALID_CONNECTION, 400) from exc
        host = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "origin", f"http://{host}:{port}")


class CoreSseStreamV1:
    """Single-pass, bounded SSE adapter yielding validated contract envelopes."""

    def __init__(
        self,
        chunks: Iterator[bytes],
        *,
        active_project_id: str,
        bearer_token: str,
        declared_length: int | None,
    ) -> None:
        self._chunks = chunks
        self._active_project_id = active_project_id
        self._bearer_token = bearer_token.encode("utf-8")
        self._declared_length = declared_length
        self._started = False

    def __iter__(self) -> Iterator[v1.SseFrameV1]:
        if self._started:
            raise RuntimeError("Core SSE streams are single-pass")
        self._started = True
        try:
            for frame in _iter_sse_frames(
                self._chunks,
                declared_length=self._declared_length,
            ):
                if self._bearer_token in frame:
                    _raise_local(CoreClientLocalErrorCodeV1.SSE_PROTOCOL_ERROR, 502)
                validated_frame = _validate_sse_frame(frame)
                _ensure_event_project(validated_frame.data, self._active_project_id)
                yield validated_frame
        except CoreClientErrorV1:
            raise
        except (httpx.HTTPError, UnicodeError, ValueError, ValidationError) as exc:
            raise _local_exception(CoreClientLocalErrorCodeV1.SSE_PROTOCOL_ERROR, 502) from exc


class CoreControlClientV1:
    """Thread-safe strict client bound to one active project's SSH tunnel."""

    def __init__(
        self,
        connection: CoreTunnelConnectionV1,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float | httpx.Timeout = 30.0,
    ) -> None:
        if not isinstance(connection, CoreTunnelConnectionV1):
            raise TypeError("connection must be CoreTunnelConnectionV1")
        self._connection = connection
        self._state_lock = threading.Lock()
        self._closed = False
        self._http = httpx.Client(
            base_url=f"{connection.origin}/",
            transport=transport,
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
            headers={"Accept-Encoding": "identity", "User-Agent": "OpenEvo-Desktop-CoreClient/1"},
        )

    def __enter__(self) -> CoreControlClientV1:
        self._ensure_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Idempotently stop new calls and close active HTTP/SSE transports."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._http.close()

    def version(self) -> v1.VersionResponseV1:
        return self._json("GET", "/version", v1.VersionResponseV1, authenticated=False)

    def health(self) -> v1.HealthResponseV1:
        return self._json("GET", "/health", v1.HealthResponseV1, authenticated=False)

    def status(self) -> v1.CoreStatusV1:
        return self._json("GET", "/v1/status", v1.CoreStatusV1)

    def environment_doctor(
        self,
        request: v1.EnvironmentDoctorRequestV1,
        *,
        idempotency_key: str,
    ) -> v1.EnvironmentDoctorResponseV1:
        return self._mutation(
            "POST",
            "/v1/environment/doctor",
            request,
            v1.EnvironmentDoctorRequestV1,
            v1.EnvironmentDoctorResponseV1,
            idempotency_key=idempotency_key,
        )

    def environment_repair(
        self,
        request: v1.EnvironmentRepairRequestV1,
        *,
        idempotency_key: str,
    ) -> v1.OperationV1:
        return self._mutation(
            "POST",
            "/v1/environment/repair",
            request,
            v1.EnvironmentRepairRequestV1,
            v1.OperationV1,
            idempotency_key=idempotency_key,
            expected_status=202,
        )

    def capabilities(self, execution_mode: v1.ExecutionMode) -> v1.CapabilitiesResponseV1:
        mode = _enum_query(execution_mode, v1.ExecutionMode)
        return self._json(
            "GET",
            "/v1/capabilities",
            v1.CapabilitiesResponseV1,
            params={"execution_mode": mode},
            max_response_bytes=MAX_CORE_CAPABILITIES_RESPONSE_BYTES,
        )

    def list_projects(
        self,
        *,
        limit: int = 50,
        after: str | None = None,
        sort: Literal["created_at", "updated_at", "name"] = "updated_at",
        direction: Literal["asc", "desc"] = "desc",
    ) -> v1.ProjectPageV1:
        result = self._json(
            "GET",
            "/v1/projects",
            v1.ProjectPageV1,
            params=_page_query(limit, after, sort, direction, {"created_at", "updated_at", "name"}),
        )
        for project in result.items:
            self._ensure_active_project(project.id)
        return result

    def create_project(
        self,
        request: v1.ProjectCreateV1,
        *,
        idempotency_key: str,
    ) -> v1.ProjectV1:
        result = self._mutation(
            "POST",
            "/v1/projects",
            request,
            v1.ProjectCreateV1,
            v1.ProjectV1,
            idempotency_key=idempotency_key,
            expected_status=201,
        )
        self._ensure_active_project(result.id)
        return result

    def get_project(self, project_id: str | None = None) -> v1.ProjectV1:
        project_id = self._active_project(project_id)
        result = self._json("GET", f"/v1/projects/{_segment(project_id)}", v1.ProjectV1)
        self._ensure_active_project(result.id)
        return result

    def patch_project(
        self,
        request: v1.ProjectPatchV1,
        *,
        if_match: str,
        idempotency_key: str,
        project_id: str | None = None,
    ) -> v1.ProjectV1:
        project_id = self._active_project(project_id)
        result = self._mutation(
            "PATCH",
            f"/v1/projects/{_segment(project_id)}",
            request,
            v1.ProjectPatchV1,
            v1.ProjectV1,
            if_match=if_match,
            idempotency_key=idempotency_key,
        )
        self._ensure_active_project(result.id)
        return result

    def delete_project(
        self,
        *,
        if_match: str,
        idempotency_key: str,
        project_id: str | None = None,
    ) -> None:
        project_id = self._active_project(project_id)
        self._no_content_mutation(
            "DELETE",
            f"/v1/projects/{_segment(project_id)}",
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    def list_revisions(
        self,
        *,
        limit: int = 50,
        after: str | None = None,
        sort: Literal["generation", "created_at", "updated_at"] = "generation",
        direction: Literal["asc", "desc"] = "desc",
        project_id: str | None = None,
    ) -> v1.RevisionPageV1:
        project_id = self._active_project(project_id)
        result = self._json(
            "GET",
            f"/v1/projects/{_segment(project_id)}/revisions",
            v1.RevisionPageV1,
            params=_page_query(
                limit,
                after,
                sort,
                direction,
                {"generation", "created_at", "updated_at"},
            ),
        )
        for revision in result.items:
            self._ensure_active_project(revision.revision.project_id)
        return result

    def revision_head(self, project_id: str | None = None) -> v1.RevisionHeadV1:
        project_id = self._active_project(project_id)
        result = self._json(
            "GET", f"/v1/projects/{_segment(project_id)}/revisions/head", v1.RevisionHeadV1
        )
        self._ensure_active_project(result.project_id)
        return result

    def get_revision(self, revision_id: str, *, project_id: str) -> v1.RevisionV1:
        self._ensure_active_project(project_id)
        result = self._json("GET", f"/v1/revisions/{_segment(revision_id)}", v1.RevisionV1)
        self._ensure_active_project(result.revision.project_id)
        return result

    def create_workspace_upload(
        self,
        request: v1.WorkspaceUploadCreateV1,
        *,
        if_match: str,
        idempotency_key: str,
        project_id: str | None = None,
    ) -> v1.WorkspaceUploadSessionV1:
        project_id = self._active_project(project_id)
        result = self._mutation(
            "POST",
            f"/v1/projects/{_segment(project_id)}/workspace-uploads",
            request,
            v1.WorkspaceUploadCreateV1,
            v1.WorkspaceUploadSessionV1,
            if_match=if_match,
            idempotency_key=idempotency_key,
            expected_status=201,
        )
        self._ensure_active_project(result.project_id)
        return result

    def get_workspace_upload(
        self,
        upload_id: str,
        *,
        project_id: str | None = None,
    ) -> v1.WorkspaceUploadSessionV1:
        project_id = self._active_project(project_id)
        result = self._json(
            "GET",
            f"/v1/projects/{_segment(project_id)}/workspace-uploads/{_segment(upload_id)}",
            v1.WorkspaceUploadSessionV1,
        )
        self._ensure_active_project(result.project_id)
        return result

    def put_workspace_upload_chunk(
        self,
        upload_id: str,
        request: v1.WorkspaceUploadChunkV1,
        *,
        if_match: str,
        idempotency_key: str,
        project_id: str | None = None,
    ) -> v1.WorkspaceUploadSessionV1:
        project_id = self._active_project(project_id)
        result = self._mutation(
            "PUT",
            f"/v1/projects/{_segment(project_id)}/workspace-uploads/{_segment(upload_id)}/chunk",
            request,
            v1.WorkspaceUploadChunkV1,
            v1.WorkspaceUploadSessionV1,
            if_match=if_match,
            idempotency_key=idempotency_key,
            max_request_bytes=MAX_CORE_WORKSPACE_CHUNK_REQUEST_BYTES,
        )
        self._ensure_active_project(result.project_id)
        return result

    def finalize_workspace_upload(
        self,
        upload_id: str,
        request: v1.WorkspaceUploadFinalizeV1,
        *,
        if_match: str,
        if_project_match: str,
        idempotency_key: str,
        project_id: str | None = None,
    ) -> v1.WorkspaceUploadFinalizeResponseV1:
        project_id = self._active_project(project_id)
        result = self._mutation(
            "POST",
            f"/v1/projects/{_segment(project_id)}/workspace-uploads/{_segment(upload_id)}/finalize",
            request,
            v1.WorkspaceUploadFinalizeV1,
            v1.WorkspaceUploadFinalizeResponseV1,
            if_match=if_match,
            if_project_match=if_project_match,
            idempotency_key=idempotency_key,
            expected_status=201,
        )
        self._ensure_active_project(result.project_id)
        return result

    def abort_workspace_upload(
        self,
        upload_id: str,
        request: v1.WorkspaceUploadAbortV1,
        *,
        if_match: str,
        idempotency_key: str,
        project_id: str | None = None,
    ) -> v1.WorkspaceUploadSessionV1:
        project_id = self._active_project(project_id)
        result = self._mutation(
            "POST",
            f"/v1/projects/{_segment(project_id)}/workspace-uploads/{_segment(upload_id)}/abort",
            request,
            v1.WorkspaceUploadAbortV1,
            v1.WorkspaceUploadSessionV1,
            if_match=if_match,
            idempotency_key=idempotency_key,
        )
        self._ensure_active_project(result.project_id)
        return result

    def validate_project(
        self,
        request: v1.ProjectValidationRequestV1,
        *,
        idempotency_key: str,
        project_id: str | None = None,
    ) -> v1.ProjectValidationResponseV1:
        project_id = self._active_project(project_id)
        return self._mutation(
            "POST",
            f"/v1/projects/{_segment(project_id)}/validate",
            request,
            v1.ProjectValidationRequestV1,
            v1.ProjectValidationResponseV1,
            idempotency_key=idempotency_key,
        )

    def list_runs(
        self,
        *,
        limit: int = 50,
        after: str | None = None,
        sort: Literal["created_at", "started_at", "finished_at"] = "created_at",
        direction: Literal["asc", "desc"] = "desc",
        status: v1.RunStatus | None = None,
    ) -> v1.RunPageV1:
        params = _page_query(
            limit,
            after,
            sort,
            direction,
            {"created_at", "started_at", "finished_at"},
        )
        params["project_id"] = self._connection.project_id
        if status is not None:
            params["status"] = _enum_query(status, v1.RunStatus)
        result = self._json("GET", "/v1/runs", v1.RunPageV1, params=params)
        for run in result.items:
            self._ensure_active_project(run.project_id)
        return result

    def create_run(
        self,
        request: v1.RunCreateV1,
        *,
        idempotency_key: str,
    ) -> v1.RunV1:
        self._ensure_active_project(request.project_id)
        result = self._mutation(
            "POST",
            "/v1/runs",
            request,
            v1.RunCreateV1,
            v1.RunV1,
            idempotency_key=idempotency_key,
            expected_status=202,
        )
        self._ensure_active_project(result.project_id)
        return result

    def get_run(self, run_id: str, *, project_id: str) -> v1.RunV1:
        self._ensure_active_project(project_id)
        result = self._json("GET", f"/v1/runs/{_segment(run_id)}", v1.RunV1)
        self._ensure_active_project(result.project_id)
        return result

    def delete_run(
        self,
        run_id: str,
        *,
        project_id: str,
        if_match: str,
        idempotency_key: str,
    ) -> None:
        self._ensure_active_project(project_id)
        self._no_content_mutation(
            "DELETE",
            f"/v1/runs/{_segment(run_id)}",
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    def cancel_run(
        self,
        run_id: str,
        request: v1.RunCancelRequestV1,
        *,
        project_id: str,
        if_match: str,
        idempotency_key: str,
    ) -> v1.RunV1:
        self._ensure_active_project(project_id)
        result = self._mutation(
            "POST",
            f"/v1/runs/{_segment(run_id)}/cancel",
            request,
            v1.RunCancelRequestV1,
            v1.RunV1,
            if_match=if_match,
            idempotency_key=idempotency_key,
            expected_status=202,
        )
        self._ensure_active_project(result.project_id)
        return result

    def retry_run(
        self,
        run_id: str,
        request: v1.RunRetryRequestV1,
        *,
        project_id: str,
        if_match: str,
        idempotency_key: str,
    ) -> v1.RunV1:
        self._ensure_active_project(project_id)
        result = self._mutation(
            "POST",
            f"/v1/runs/{_segment(run_id)}/retry",
            request,
            v1.RunRetryRequestV1,
            v1.RunV1,
            if_match=if_match,
            idempotency_key=idempotency_key,
            expected_status=202,
        )
        self._ensure_active_project(result.project_id)
        return result

    def run_timeline(
        self,
        run_id: str,
        *,
        project_id: str,
        limit: int = 50,
        after: str | None = None,
        sort: Literal["sequence", "occurred_at"] = "sequence",
        direction: Literal["asc", "desc"] = "asc",
    ) -> v1.RunTimelinePageV1:
        self._ensure_active_project(project_id)
        return self._json(
            "GET",
            f"/v1/runs/{_segment(run_id)}/timeline",
            v1.RunTimelinePageV1,
            params=_page_query(limit, after, sort, direction, {"sequence", "occurred_at"}),
        )

    def run_logs(
        self,
        run_id: str,
        *,
        project_id: str,
        limit: int = 100,
        after: str | None = None,
        sort: Literal["sequence", "occurred_at"] = "sequence",
        direction: Literal["asc", "desc"] = "asc",
        stream: v1.LogStream | None = None,
    ) -> v1.LogPageV1:
        self._ensure_active_project(project_id)
        params = _page_query(limit, after, sort, direction, {"sequence", "occurred_at"})
        if stream is not None:
            params["stream"] = _enum_query(stream, v1.LogStream)
        return self._json(
            "GET",
            f"/v1/runs/{_segment(run_id)}/logs",
            v1.LogPageV1,
            params=params,
            max_response_bytes=MAX_CORE_LOG_RESPONSE_BYTES,
        )

    def run_context(self, run_id: str, *, project_id: str) -> v1.RunContextV1:
        self._ensure_active_project(project_id)
        result = self._json("GET", f"/v1/runs/{_segment(run_id)}/context", v1.RunContextV1)
        self._ensure_active_project(result.project_id)
        return result

    def run_artifacts(
        self,
        run_id: str,
        *,
        project_id: str,
        limit: int = 50,
        after: str | None = None,
        sort: Literal["created_at", "title", "artifact_type"] = "created_at",
        direction: Literal["asc", "desc"] = "asc",
        artifact_type: v1.ArtifactType | None = None,
    ) -> v1.ArtifactPageV1:
        self._ensure_active_project(project_id)
        params = _page_query(
            limit,
            after,
            sort,
            direction,
            {"created_at", "title", "artifact_type"},
        )
        if artifact_type is not None:
            params["artifact_type"] = _enum_query(artifact_type, v1.ArtifactType)
        result = self._json(
            "GET", f"/v1/runs/{_segment(run_id)}/artifacts", v1.ArtifactPageV1, params=params
        )
        for artifact in result.items:
            self._ensure_active_project(artifact.project_id)
        return result

    def get_artifact(self, artifact_id: str, *, project_id: str) -> v1.ArtifactSummaryV1:
        self._ensure_active_project(project_id)
        result = self._json(
            "GET", f"/v1/artifacts/{_segment(artifact_id)}", v1.ArtifactSummaryV1
        )
        self._ensure_active_project(result.project_id)
        return result

    def artifact_content(
        self, artifact_id: str, *, project_id: str
    ) -> v1.ArtifactContentV1:
        self._ensure_active_project(project_id)
        return self._json(
            "GET",
            f"/v1/artifacts/{_segment(artifact_id)}/content",
            v1.ArtifactContentV1,
            max_response_bytes=MAX_CORE_ARTIFACT_RESPONSE_BYTES,
        )

    def artifact_diff(
        self,
        artifact_id: str,
        *,
        project_id: str,
        previous_artifact_id: str | None = None,
    ) -> v1.ArtifactDiffV1:
        self._ensure_active_project(project_id)
        params = None
        if previous_artifact_id is not None:
            params = {"previous_artifact_id": _opaque(previous_artifact_id)}
        return self._json(
            "GET",
            f"/v1/artifacts/{_segment(artifact_id)}/diff",
            v1.ArtifactDiffV1,
            params=params,
            max_response_bytes=MAX_CORE_ARTIFACT_RESPONSE_BYTES,
        )

    def list_services(
        self,
        *,
        limit: int = 50,
        after: str | None = None,
        sort: Literal["kind", "status", "updated_at"] = "kind",
        direction: Literal["asc", "desc"] = "asc",
    ) -> v1.ServicePageV1:
        return self._json(
            "GET",
            "/v1/services",
            v1.ServicePageV1,
            params=_page_query(limit, after, sort, direction, {"kind", "status", "updated_at"}),
        )

    def get_service(self, service_id: str) -> v1.ServiceSummaryV1:
        return self._json("GET", f"/v1/services/{_segment(service_id)}", v1.ServiceSummaryV1)

    def restart_service(
        self,
        service_id: str,
        request: v1.ServiceRestartRequestV1,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> v1.OperationV1:
        return self._mutation(
            "POST",
            f"/v1/services/{_segment(service_id)}/restart",
            request,
            v1.ServiceRestartRequestV1,
            v1.OperationV1,
            if_match=if_match,
            idempotency_key=idempotency_key,
            expected_status=202,
        )

    def service_logs(
        self,
        service_id: str,
        *,
        limit: int = 100,
        after: str | None = None,
        sort: Literal["sequence", "occurred_at"] = "sequence",
        direction: Literal["asc", "desc"] = "asc",
    ) -> v1.LogPageV1:
        return self._json(
            "GET",
            f"/v1/services/{_segment(service_id)}/logs",
            v1.LogPageV1,
            params=_page_query(limit, after, sort, direction, {"sequence", "occurred_at"}),
            max_response_bytes=MAX_CORE_LOG_RESPONSE_BYTES,
        )

    def get_operation(self, operation_id: str) -> v1.OperationV1:
        return self._json(
            "GET", f"/v1/operations/{_segment(operation_id)}", v1.OperationV1
        )

    def cancel_operation(
        self,
        operation_id: str,
        request: v1.OperationCancelRequestV1,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> v1.OperationV1:
        return self._mutation(
            "POST",
            f"/v1/operations/{_segment(operation_id)}/cancel",
            request,
            v1.OperationCancelRequestV1,
            v1.OperationV1,
            if_match=if_match,
            idempotency_key=idempotency_key,
            expected_status=202,
        )

    def logs_by_ref(
        self,
        logs_ref: str,
        *,
        limit: int = 100,
        after: str | None = None,
        sort: Literal["sequence", "occurred_at"] = "sequence",
        direction: Literal["asc", "desc"] = "asc",
    ) -> v1.ReferencedLogPageV1:
        result = self._json(
            "GET",
            f"/v1/logs/{_segment(logs_ref)}",
            v1.ReferencedLogPageV1,
            params=_page_query(limit, after, sort, direction, {"sequence", "occurred_at"}),
            max_response_bytes=MAX_CORE_LOG_RESPONSE_BYTES,
        )
        if result.logs_ref != logs_ref:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        return result

    def create_diagnostic(
        self,
        request: v1.DiagnosticsRequestV1,
        *,
        idempotency_key: str,
    ) -> v1.DiagnosticV1:
        _ensure_diagnostic_request_project(request, self._connection.project_id)
        result = self._mutation(
            "POST",
            "/v1/diagnostics",
            request,
            v1.DiagnosticsRequestV1,
            v1.DiagnosticV1,
            idempotency_key=idempotency_key,
            expected_status=202,
        )
        _ensure_diagnostic_project(result, self._connection.project_id)
        return result

    def get_diagnostic(self, diagnostic_id: str) -> v1.DiagnosticV1:
        result = self._json(
            "GET", f"/v1/diagnostics/{_segment(diagnostic_id)}", v1.DiagnosticV1
        )
        _ensure_diagnostic_project(result, self._connection.project_id)
        return result

    def delete_diagnostic(
        self,
        diagnostic_id: str,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> None:
        self._no_content_mutation(
            "DELETE",
            f"/v1/diagnostics/{_segment(diagnostic_id)}",
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    def cache_cleanup(
        self,
        request: v1.CacheCleanupRequestV1,
        *,
        idempotency_key: str,
    ) -> v1.OperationV1:
        return self._mutation(
            "POST",
            "/v1/maintenance/cache-cleanup",
            request,
            v1.CacheCleanupRequestV1,
            v1.OperationV1,
            idempotency_key=idempotency_key,
            expected_status=202,
        )

    @contextmanager
    def events(self, *, last_event_id: str | None = None) -> Iterator[CoreSseStreamV1]:
        """Open the authenticated event stream; closing the context closes the response."""
        headers = self._headers(authenticated=True, accept="text/event-stream")
        if last_event_id is not None:
            headers["Last-Event-ID"] = _cursor(last_event_id)
        self._ensure_open()
        try:
            with self._http.stream(
                "GET", "/v1/events", headers=headers, follow_redirects=False
            ) as response:
                self._ensure_response_origin(response)
                if 300 <= response.status_code < 400:
                    _read_bounded(response, MAX_CORE_ERROR_RESPONSE_BYTES)
                    _raise_local(CoreClientLocalErrorCodeV1.REDIRECT_REJECTED, 502)
                if response.status_code != 200:
                    body = _read_bounded(response, MAX_CORE_ERROR_RESPONSE_BYTES)
                    _require_content_type(response, "application/json", error_response=True)
                    self._raise_http_error(response.status_code, body)
                _require_content_type(response, "text/event-stream")
                if response.headers.get("content-encoding", "identity").lower() not in {
                    "",
                    "identity",
                }:
                    _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
                declared_length = _bounded_content_length(
                    response,
                    MAX_CORE_SSE_RESPONSE_BYTES,
                    invalid_code=CoreClientLocalErrorCodeV1.SSE_PROTOCOL_ERROR,
                )
                yield CoreSseStreamV1(
                    response.iter_bytes(),
                    active_project_id=self._connection.project_id,
                    bearer_token=self._connection.bearer_token,
                    declared_length=declared_length,
                )
        except CoreClientErrorV1:
            raise
        except httpx.HTTPError as exc:
            raise _local_exception(CoreClientLocalErrorCodeV1.CONNECTION_FAILED, 503) from exc

    def _mutation(
        self,
        method: str,
        path: str,
        request: BaseModel,
        request_model: type[BaseModel],
        response_model: Any,
        *,
        idempotency_key: str,
        if_match: str | None = None,
        if_project_match: str | None = None,
        expected_status: int = 200,
        max_request_bytes: int = MAX_CORE_REQUEST_BYTES,
    ) -> Any:
        body = _encode_request(request, request_model, max_request_bytes)
        if self._connection.bearer_token.encode("utf-8") in body:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
        headers = self._mutation_headers(
            idempotency_key=idempotency_key,
            if_match=if_match,
            if_project_match=if_project_match,
        )
        headers["Content-Type"] = "application/json"
        return self._json(
            method,
            path,
            response_model,
            content=body,
            headers=headers,
            expected_status=expected_status,
        )

    def _no_content_mutation(
        self,
        method: str,
        path: str,
        *,
        idempotency_key: str,
        if_match: str | None = None,
    ) -> None:
        headers = self._mutation_headers(
            idempotency_key=idempotency_key,
            if_match=if_match,
        )
        self._json(method, path, None, headers=headers, expected_status=204)

    def _json(
        self,
        method: str,
        path: str,
        response_model: Any,
        *,
        authenticated: bool = True,
        params: Mapping[str, str] | None = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        expected_status: int = 200,
        max_response_bytes: int = MAX_CORE_JSON_RESPONSE_BYTES,
    ) -> Any:
        request_headers = self._headers(authenticated=authenticated, accept="application/json")
        if headers:
            request_headers.update(headers)
        self._ensure_open()
        try:
            with self._http.stream(
                method,
                path,
                params=params,
                content=content,
                headers=request_headers,
                follow_redirects=False,
            ) as response:
                self._ensure_response_origin(response)
                limit = (
                    max_response_bytes
                    if response.status_code == expected_status
                    else MAX_CORE_ERROR_RESPONSE_BYTES
                )
                body = _read_bounded(response, limit)
                status_code = response.status_code
                content_type = response.headers.get("content-type")
        except CoreClientErrorV1:
            raise
        except httpx.HTTPError as exc:
            raise _local_exception(CoreClientLocalErrorCodeV1.CONNECTION_FAILED, 503) from exc
        if 300 <= status_code < 400:
            _raise_local(CoreClientLocalErrorCodeV1.REDIRECT_REJECTED, 502)
        if status_code != expected_status:
            if content_type is None or (
                content_type.split(";", 1)[0].strip().lower() != "application/json"
            ):
                _raise_local(CoreClientLocalErrorCodeV1.INVALID_ERROR_RESPONSE, 502)
            self._raise_http_error(status_code, body)
        if expected_status == 204:
            if body:
                _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
            return None
        if content_type is None or content_type.split(";", 1)[0].strip().lower() != "application/json":
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        if self._connection.bearer_token.encode("utf-8") in body:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        try:
            if isinstance(response_model, type) and issubclass(response_model, BaseModel):
                return response_model.model_validate_json(body, strict=True)
            return TypeAdapter(response_model).validate_json(body, strict=True)
        except (ValidationError, ValueError) as exc:
            raise _local_exception(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502) from exc

    def _raise_http_error(self, status_code: int, body: bytes) -> None:
        if self._connection.bearer_token.encode("utf-8") in body:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_ERROR_RESPONSE, 502)
        try:
            error = v1.ApiErrorV1.model_validate_json(body, strict=True)
        except (ValidationError, ValueError) as exc:
            raise _local_exception(
                CoreClientLocalErrorCodeV1.INVALID_ERROR_RESPONSE, 502
            ) from exc
        if error.http_status != status_code:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_ERROR_RESPONSE, 502)
        raise CoreClientErrorV1(status_code, error)

    def _headers(self, *, authenticated: bool, accept: str) -> dict[str, str]:
        headers = {"Accept": accept}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._connection.bearer_token}"
        return headers

    def _mutation_headers(
        self,
        *,
        idempotency_key: str,
        if_match: str | None = None,
        if_project_match: str | None = None,
    ) -> dict[str, str]:
        key = _header(idempotency_key)
        if self._connection.bearer_token in key:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
        headers = {"Idempotency-Key": key}
        if if_match is not None:
            headers["If-Match"] = _etag(if_match)
        if if_project_match is not None:
            headers["If-Project-Match"] = _etag(if_project_match)
        return headers

    def _active_project(self, project_id: str | None) -> str:
        if project_id is None:
            return self._connection.project_id
        self._ensure_active_project(project_id)
        return project_id

    def _ensure_active_project(self, project_id: str) -> None:
        try:
            candidate = _opaque(project_id)
        except ValueError as exc:
            raise _local_exception(
                CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH, 409
            ) from exc
        if candidate != self._connection.project_id:
            _raise_local(CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH, 409)

    def _ensure_open(self) -> None:
        with self._state_lock:
            if self._closed:
                _raise_local(CoreClientLocalErrorCodeV1.CLIENT_CLOSED, 503)

    def _ensure_response_origin(self, response: httpx.Response) -> None:
        url = response.request.url
        origin = f"{url.scheme}://{url.host}:{url.port}"
        expected = self._connection.origin.replace("[::1]", "::1")
        if origin != expected:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)


def _encode_request(request: BaseModel, request_model: type[BaseModel], limit: int) -> bytes:
    if type(request) is not request_model:
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
    try:
        body = request.model_dump_json().encode("utf-8")
    except (UnicodeError, ValueError) as exc:
        raise _local_exception(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400) from exc
    if len(body) > limit:
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
    return body


def _read_bounded(response: httpx.Response, limit: int) -> bytes:
    if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
    declared = _bounded_content_length(response, limit)
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > limit:
                _raise_local(CoreClientLocalErrorCodeV1.RESPONSE_TOO_LARGE, 502)
            chunks.append(chunk)
    except CoreClientErrorV1:
        raise
    except httpx.HTTPError as exc:
        raise _local_exception(CoreClientLocalErrorCodeV1.CONNECTION_FAILED, 503) from exc
    if declared is not None and total != declared:
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
    return b"".join(chunks)


def _bounded_content_length(
    response: httpx.Response,
    limit: int,
    *,
    invalid_code: CoreClientLocalErrorCodeV1 = CoreClientLocalErrorCodeV1.INVALID_RESPONSE,
) -> int | None:
    content_length = response.headers.get("content-length")
    if content_length is None:
        return None
    try:
        if not re.fullmatch(r"0|[1-9][0-9]*", content_length):
            raise ValueError
        declared = int(content_length)
    except ValueError as exc:
        raise _local_exception(invalid_code, 502) from exc
    if declared > limit:
        _raise_local(CoreClientLocalErrorCodeV1.RESPONSE_TOO_LARGE, 502)
    return declared


def _page_query(
    limit: int,
    after: str | None,
    sort: str,
    direction: str,
    allowed_sorts: set[str],
) -> dict[str, str]:
    if type(limit) is not int or not 1 <= limit <= 100:
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
    if sort not in allowed_sorts or direction not in {"asc", "desc"}:
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
    params = {"limit": str(limit), "sort": sort, "direction": direction}
    if after is not None:
        params["after"] = _cursor(after)
    return params


def _enum_query(value: object, enum_type: type[StrEnum]) -> str:
    if not isinstance(value, enum_type):
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
    return value.value


def _opaque(value: str) -> str:
    try:
        return _OPAQUE_ID.validate_python(value, strict=True)
    except ValidationError as exc:
        raise ValueError("invalid opaque identity") from exc


def _cursor(value: str) -> str:
    try:
        return _CURSOR.validate_python(value, strict=True)
    except ValidationError as exc:
        raise _local_exception(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400) from exc


def _segment(value: str) -> str:
    try:
        return quote(_opaque(value), safe="")
    except ValueError as exc:
        raise _local_exception(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400) from exc


def _header(value: str) -> str:
    if not isinstance(value, str) or not _HEADER_VALUE.fullmatch(value):
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
    return value


def _etag(value: str) -> str:
    if not isinstance(value, str) or not _ETAG.fullmatch(value):
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
    return value


def _require_content_type(
    response: httpx.Response,
    expected: str,
    *,
    error_response: bool = False,
) -> None:
    actual = response.headers.get("content-type")
    if actual is None or actual.split(";", 1)[0].strip().lower() != expected:
        code = (
            CoreClientLocalErrorCodeV1.INVALID_ERROR_RESPONSE
            if error_response
            else CoreClientLocalErrorCodeV1.INVALID_RESPONSE
        )
        _raise_local(code, 502)


def _iter_sse_frames(
    chunks: Iterator[bytes],
    *,
    declared_length: int | None,
) -> Iterator[bytes]:
    buffer = bytearray()
    lines: list[bytes] = []
    frame_bytes = 0
    total_bytes = 0
    for chunk in chunks:
        if not chunk:
            continue
        total_bytes += len(chunk)
        if total_bytes > MAX_CORE_SSE_RESPONSE_BYTES:
            _raise_local(CoreClientLocalErrorCodeV1.RESPONSE_TOO_LARGE, 502)
        buffer.extend(chunk)
        if len(buffer) > MAX_CORE_SSE_FRAME_BYTES:
            raise ValueError("SSE line exceeds limit")
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(buffer[:newline])
            del buffer[: newline + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            if not line:
                if lines:
                    yield b"\n".join(lines)
                    lines = []
                    frame_bytes = 0
                continue
            if line.startswith(b":"):
                continue
            frame_bytes += len(line) + 1
            if frame_bytes > MAX_CORE_SSE_FRAME_BYTES:
                raise ValueError("SSE frame exceeds limit")
            lines.append(line)
    if buffer or lines:
        raise ValueError("SSE stream ended with an incomplete frame")
    if declared_length is not None and total_bytes != declared_length:
        raise ValueError("SSE stream length differs from Content-Length")


def _validate_sse_frame(frame: bytes) -> v1.SseFrameV1:
    fields: dict[str, bytes] = {}
    for line in frame.split(b"\n"):
        name, separator, raw_value = line.partition(b":")
        if not separator or name not in {b"id", b"event", b"data"}:
            raise ValueError("unexpected SSE field")
        key = name.decode("ascii")
        if key in fields:
            raise ValueError("duplicate SSE field")
        fields[key] = raw_value[1:] if raw_value.startswith(b" ") else raw_value
    if set(fields) != {"id", "event", "data"}:
        raise ValueError("incomplete SSE frame")
    event_id = fields["id"].decode("utf-8", errors="strict")
    event_name = fields["event"].decode("utf-8", errors="strict")
    envelope = v1.EventEnvelopeV1.model_validate_json(fields["data"], strict=True)
    return v1.SseFrameV1.model_validate(
        {"id": event_id, "event": event_name, "data": envelope},
        strict=True,
    )


def _ensure_diagnostic_request_project(
    request: v1.DiagnosticsRequestV1,
    active_project_id: str,
) -> None:
    target = request.target
    if isinstance(target, (v1.ProjectDiagnosticTargetV1, v1.RunDiagnosticTargetV1)):
        if target.project_id != active_project_id:
            _raise_local(CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH, 409)


def _ensure_diagnostic_project(value: v1.DiagnosticV1, active_project_id: str) -> None:
    target = value.target
    if isinstance(target, (v1.ProjectDiagnosticTargetV1, v1.RunDiagnosticTargetV1)):
        if target.project_id != active_project_id:
            _raise_local(CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH, 409)


def _ensure_event_project(envelope: v1.EventEnvelopeV1, active_project_id: str) -> None:
    event = envelope.root
    project_id: str | None = None
    payload = getattr(event, "payload", None)
    if isinstance(payload, (v1.ProjectSummaryV1, v1.ProjectV1)):
        project_id = payload.id
    elif hasattr(payload, "project_id"):
        project_id = payload.project_id
    elif isinstance(payload, v1.RevisionV1):
        project_id = payload.revision.project_id
    elif isinstance(payload, v1.RevisionHeadV1):
        project_id = payload.project_id
    elif isinstance(payload, v1.DiagnosticV1):
        target = payload.target
        if isinstance(target, (v1.ProjectDiagnosticTargetV1, v1.RunDiagnosticTargetV1)):
            project_id = target.project_id
    if project_id is not None and project_id != active_project_id:
        _raise_local(CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH, 409)


def _local_exception(
    code: CoreClientLocalErrorCodeV1,
    status_code: int,
) -> CoreClientErrorV1:
    return CoreClientErrorV1(
        status_code,
        CoreClientLocalErrorV1(
            code=code,
            message=_LOCAL_ERROR_MESSAGES[code],
            retryable=code in {
                CoreClientLocalErrorCodeV1.CONNECTION_FAILED,
                CoreClientLocalErrorCodeV1.CLIENT_CLOSED,
            },
        ),
    )


def _raise_local(code: CoreClientLocalErrorCodeV1, status_code: int) -> None:
    raise _local_exception(code, status_code)


__all__ = [
    "CoreClientErrorV1",
    "CoreClientLocalErrorCodeV1",
    "CoreClientLocalErrorV1",
    "CoreControlClientV1",
    "CoreSseStreamV1",
    "CoreTunnelConnectionV1",
]
