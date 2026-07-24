"""Core-owned project validation, workspace selection, and v2 genesis authority."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import threading
from typing import Literal, Protocol

from pydantic import ConfigDict, Field, ValidationError, model_validator

from openevo.backend.contracts.v2 import models as m
from openevo.backend.contracts.v2.store import (
    CoreControlStoreV2,
    ProjectAuthorityDocumentV2,
    ProjectRecordV2,
)
from openevo.backend.run_admission import (
    EffectiveExecutionSettings,
    EffectiveExecutionSnapshotUnavailable,
    resolve_genesis_execution_snapshot,
)
from openevo.backend.run_control import CoreTaskControlError
from openevo.backend.science_run_owner import CoreScienceTaskOwnerV2
from openevo.backend.science_run_store import ScienceProjectAdmissionAuthorityV2
from openevo.backend.service_supervisor import ServiceExecutionMode, ServiceRunBinding
from openevo.backend.workspace_store_v2 import (
    WorkspaceIntegrityErrorV2,
    WorkspaceNotFoundV2,
    WorkspaceStoreV2,
)
from openevo.evolution.framework import canonical_digest
from openevo.evolution.framework.builtins import (
    VerifiedExecutableRegistry,
    require_verified_executable_registry,
)
from openevo.evolution.framework.profiles import execution_profile_for_release_mode
from openevo.evolution.revisions import (
    ExecutionSnapshotV1,
    VerifiedExecutionSnapshot,
    require_verified_execution_snapshot,
)
from openevo.runtime.managed import MANAGED_RUNTIME_IMAGES
from openevo.experiments import (
    ProjectEvolutionValidationError,
    validate_project_evolution_selections,
)


_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_MAX_RECORD_BYTES = 256 * 1024
_MAX_RECORD_DEPTH = 16
_MAX_RECORD_NODES = 4096


class ProjectAuthorityV2Error(RuntimeError):
    """Core cannot prove the complete authority for a v2 project."""


class ProjectAuthorityConflictV2(ProjectAuthorityV2Error):
    pass


class ProjectAuthorityInvalidV2(ProjectAuthorityV2Error):
    def __init__(
        self,
        *,
        reason_code: str,
        target_id: str | None = None,
        method_id: str | None = None,
    ) -> None:
        super().__init__("the project configuration is invalid")
        self.reason_code = reason_code
        self.target_id = target_id
        self.method_id = method_id


@dataclass(frozen=True, slots=True)
class ProjectAuthorityReadinessV2:
    ready: bool
    checks: tuple[m.ProjectValidationCheckV2, ...]


class ServiceBindingProviderV2(Protocol):
    def ensure(
        self,
        execution_mode: ServiceExecutionMode,
        *,
        codex_model: str | None = None,
        runtime_image: str | None = None,
    ) -> object: ...

    def run_binding(self) -> ServiceRunBinding: ...


def _after_science_authority_publish_before_catalog_commit(*_args: object) -> None:
    """Test-only crash boundary after the atomic science-ledger publication."""


class _ProjectAuthorityRecordV1(m.ContractModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )

    authority_record_version: Literal["1"] = "1"
    project_id: str = Field(min_length=1, max_length=128)
    project_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_evolution_intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_snapshot: m.WorkspaceSnapshotRefV2 | None
    active_project_head: m.ProjectHeadRefV2 | None
    execution_snapshot: ExecutionSnapshotV1 | None
    execution_snapshot_producer_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    service_generation_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    framework_lock_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    publication_state: Literal["draft", "prepared", "published"]

    @model_validator(mode="after")
    def _closed_state(self) -> _ProjectAuthorityRecordV1:
        prepared_values = (
            self.active_project_head,
            self.execution_snapshot,
            self.execution_snapshot_producer_id,
            self.service_generation_sha256,
            self.framework_lock_sha256,
        )
        if self.publication_state == "draft":
            if any(value is not None for value in prepared_values):
                raise ValueError("a draft project authority contains sealed state")
        elif self.workspace_snapshot is None or any(
            value is None for value in prepared_values
        ):
            raise ValueError("a prepared project authority is incomplete")
        if (
            self.workspace_snapshot is not None
            and self.workspace_snapshot.project_id != self.project_id
        ):
            raise ValueError("project authority workspace belongs to another project")
        head = self.active_project_head
        if head is not None:
            if (
                head.project_id != self.project_id
                or head.workspace_snapshot != self.workspace_snapshot
                or self.execution_snapshot is None
                or self.execution_snapshot_producer_id is None
            ):
                raise ValueError("project authority head closure is inconsistent")
            digest = canonical_digest(self.execution_snapshot)
            effective = head.effective_execution_snapshot
            if (
                effective.snapshot_sha256 != digest
                or effective.effective_execution_snapshot_id != f"exec-{digest}"
                or effective.producer_id != self.execution_snapshot_producer_id
            ):
                raise ValueError("project authority execution snapshot differs")
        return self


class ProjectAuthorityV2:
    """Validate one saved Science config and atomically publish generation zero."""

    def __init__(
        self,
        *,
        catalog_store: CoreControlStoreV2,
        workspace_store: WorkspaceStoreV2,
        task_owner: CoreScienceTaskOwnerV2,
        executable_registry: VerifiedExecutableRegistry,
        service_binding_provider: ServiceBindingProviderV2,
        runtime_contract_sha256: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(catalog_store) is not CoreControlStoreV2:
            raise TypeError("project authority requires the exact v2 catalog store")
        if type(workspace_store) is not WorkspaceStoreV2:
            raise TypeError("project authority requires the exact v2 workspace store")
        if type(task_owner) is not CoreScienceTaskOwnerV2:
            raise TypeError("project authority requires the exact v2 Task owner")
        if any(
            not callable(getattr(service_binding_provider, method, None))
            for method in ("ensure", "run_binding")
        ):
            raise TypeError("project authority requires a service binding provider")
        self._catalog = catalog_store
        self._workspaces = workspace_store
        self._tasks = task_owner
        self._registry = require_verified_executable_registry(executable_registry)
        self._services = service_binding_provider
        self._runtime_contract_sha256 = _digest(
            runtime_contract_sha256,
            label="runtime contract",
        )
        self._registry_sha256 = _digest(
            self._registry.snapshot.registry_digest,
            label="registry",
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        for document in self._catalog.list_project_authority_documents():
            record = self._catalog.get_project(document.project_id)
            _load_authority_record(document, expected_project=record)

    def close(self) -> None:
        self._workspaces.close()

    def validate_config(self, config: m.ScienceProjectConfigV2) -> None:
        config = _exact_config(config)
        try:
            validate_project_evolution_selections(
                config.evolution.targets,
                agent_model=config.execution.codex_model,
                reflector_llm={
                    "provider": "codex_cli",
                    "model": config.execution.codex_model,
                },
                registry_snapshot=self._registry.snapshot,
                execution_profile=execution_profile_for_release_mode(
                    config.execution.mode
                ),
            )
        except ProjectEvolutionValidationError as exc:
            raise ProjectAuthorityInvalidV2(
                reason_code=exc.reason_code,
                target_id=exc.target_id,
                method_id=exc.selection,
            ) from exc

    def ensure_project(
        self,
        project: ProjectRecordV2,
    ) -> ScienceProjectAdmissionAuthorityV2 | None:
        project = _exact_project_record(project)
        self.validate_config(project.config)
        with self._lock:
            document = self._catalog.get_project_authority_document(project.project_id)
            if document is None:
                workspace = (
                    self._workspaces.ensure_empty_snapshot(project.project_id)
                    if project.config.workspace.kind == "scratch"
                    else None
                )
                authority_record = _ProjectAuthorityRecordV1(
                    project_id=project.project_id,
                    project_config_sha256=project.project_config_sha256,
                    normalized_evolution_intent_sha256=(
                        normalized_evolution_intent_sha256_for(project.config)
                    ),
                    workspace_snapshot=workspace,
                    active_project_head=None,
                    execution_snapshot=None,
                    execution_snapshot_producer_id=None,
                    service_generation_sha256=None,
                    framework_lock_sha256=None,
                    publication_state="draft",
                )
                document = self._catalog.put_project_authority_document(
                    project_id=project.project_id,
                    record_json=_authority_record_bytes(authority_record),
                    expected_record_sha256=None,
                )
            authority_record = _load_authority_record(
                document,
                expected_project=project,
            )
            if authority_record.publication_state == "published":
                authority = _science_authority(authority_record)
                return self._require_published_authority(authority)
            if authority_record.publication_state == "prepared":
                authority = _science_authority(authority_record)
                published = self._published_authority_or_none(project.project_id)
                if published is not None:
                    if published != authority:
                        raise ProjectAuthorityConflictV2(
                            "science authority differs from prepared genesis"
                        )
                    return self._mark_published(document, authority_record, authority)
            if authority_record.workspace_snapshot is None:
                return None
            resolved = self._resolve_execution(
                project.config,
                ensure_services=True,
            )
            if resolved is None:
                return None
            verified, binding = resolved
            if authority_record.publication_state == "draft":
                authority = _build_genesis_authority(
                    project=project,
                    workspace=authority_record.workspace_snapshot,
                    verified_execution=verified,
                    registry_sha256=self._registry_sha256,
                    runtime_contract_sha256=self._runtime_contract_sha256,
                    normalized_evolution_intent_sha256=(
                        authority_record.normalized_evolution_intent_sha256
                    ),
                )
                prepared = authority_record.model_copy(
                    update={
                        "active_project_head": authority.active_project_head,
                        "execution_snapshot": verified.snapshot,
                        "execution_snapshot_producer_id": verified.producer_id,
                        "service_generation_sha256": binding.generation_digest,
                        "framework_lock_sha256": binding.framework_lock_digest,
                        "publication_state": "prepared",
                    }
                )
                document = self._catalog.put_project_authority_document(
                    project_id=project.project_id,
                    record_json=_authority_record_bytes(prepared),
                    expected_record_sha256=document.record_sha256,
                )
                authority_record = _load_authority_record(
                    document,
                    expected_project=project,
                )
            else:
                authority = _science_authority(authority_record)
                if (
                    verified.snapshot != authority_record.execution_snapshot
                    or verified.producer_id
                    != authority_record.execution_snapshot_producer_id
                    or binding.framework_lock_digest
                    != authority_record.framework_lock_sha256
                    or authority.active_project_head.registry_sha256
                    != self._registry_sha256
                    or authority.active_project_head.runtime_context_snapshot
                    .runtime_contract_sha256
                    != self._runtime_contract_sha256
                ):
                    return None
            published = self._tasks.publish_project_admission_authority(authority)
            if published != authority:
                raise ProjectAuthorityConflictV2(
                    "science owner published another genesis authority"
                )
            _after_science_authority_publish_before_catalog_commit(
                project.project_id,
                authority.active_project_head.project_head_id,
            )
            return self._mark_published(document, authority_record, authority)

    def adopt_workspace_snapshot(
        self,
        project: ProjectRecordV2,
        snapshot: m.WorkspaceSnapshotRefV2,
        *,
        expected_project_head_id: str | None,
        expected_project_config_sha256: str,
    ) -> ScienceProjectAdmissionAuthorityV2 | None:
        project = _exact_project_record(project)
        snapshot = m.WorkspaceSnapshotRefV2.model_validate(
            snapshot.model_dump(mode="python")
        )
        if (
            snapshot.project_id != project.project_id
            or expected_project_config_sha256 != project.project_config_sha256
        ):
            raise ProjectAuthorityConflictV2(
                "workspace publication no longer matches project authority"
            )
        with self._lock:
            document = self._catalog.get_project_authority_document(project.project_id)
            if document is None:
                self.ensure_project(project)
                document = self._catalog.get_project_authority_document(
                    project.project_id
                )
            if document is None:
                raise ProjectAuthorityV2Error("project authority draft is missing")
            authority_record = _load_authority_record(
                document,
                expected_project=project,
            )
            if authority_record.publication_state != "draft":
                active_head = authority_record.active_project_head
                if active_head is None:
                    raise ProjectAuthorityV2Error(
                        "sealed project authority has no active project head"
                    )
                if (
                    expected_project_head_id
                    != active_head.predecessor_project_head_id
                ):
                    raise ProjectAuthorityConflictV2(
                        "workspace publication predecessor changed"
                    )
                if authority_record.workspace_snapshot != snapshot:
                    raise ProjectAuthorityConflictV2(
                        "published genesis workspace is immutable"
                    )
                return _science_authority(authority_record)
            if expected_project_head_id is not None:
                raise ProjectAuthorityConflictV2(
                    "genesis workspace expected a nonexistent project head"
                )
            self._workspaces.snapshot_path(snapshot)
            if authority_record.workspace_snapshot not in {None, snapshot}:
                raise ProjectAuthorityConflictV2(
                    "another workspace snapshot already owns the genesis draft"
                )
            if authority_record.workspace_snapshot is None:
                updated = authority_record.model_copy(
                    update={"workspace_snapshot": snapshot}
                )
                self._catalog.put_project_authority_document(
                    project_id=project.project_id,
                    record_json=_authority_record_bytes(updated),
                    expected_record_sha256=document.record_sha256,
                )
            return self.ensure_project(project)

    def create_workspace_upload(
        self,
        project: ProjectRecordV2,
        request: m.WorkspaceUploadCreateV2,
        *,
        idempotency_key: str,
        now: datetime,
    ) -> tuple[m.WorkspaceUploadSessionV2, bool]:
        project = _exact_project_record(project)
        if type(request) is not m.WorkspaceUploadCreateV2:
            raise TypeError("workspace upload request has the wrong type")
        request = m.WorkspaceUploadCreateV2.model_validate(
            request.model_dump(mode="python")
        )
        with self._lock:
            self.ensure_project(project)
            document = self._catalog.get_project_authority_document(project.project_id)
            if document is None:
                raise ProjectAuthorityV2Error("project authority draft is missing")
            record = _load_authority_record(document, expected_project=project)
            head = record.active_project_head
            if (
                request.expected_project_config_sha256
                != project.project_config_sha256
                or request.expected_project_head_id
                != (None if head is None else head.project_head_id)
                or request.expected_project_head_manifest_sha256
                != (None if head is None else head.manifest_sha256)
            ):
                raise ProjectAuthorityConflictV2(
                    "workspace upload authority changed"
                )
            if record.publication_state != "draft":
                raise ProjectAuthorityConflictV2(
                    "workspace changes require a successor transition"
                )
            return self._workspaces.create_upload(
                project.project_id,
                request,
                idempotency_key=idempotency_key,
                now=now,
            )

    def get_workspace_upload(
        self,
        project: ProjectRecordV2,
        upload_id: str,
    ) -> m.WorkspaceUploadSessionV2:
        project = _exact_project_record(project)
        return self._workspaces.get_upload(project.project_id, upload_id)

    def put_workspace_chunk(
        self,
        project: ProjectRecordV2,
        upload_id: str,
        *,
        chunk_index: int,
        chunk: bytes,
        chunk_sha256: str,
        chunk_byte_size: int,
        if_match: str,
        idempotency_key: str,
        now: datetime,
    ) -> tuple[m.WorkspaceUploadSessionV2, bool]:
        project = _exact_project_record(project)
        return self._workspaces.put_chunk(
            project.project_id,
            upload_id,
            chunk_index=chunk_index,
            chunk=chunk,
            chunk_sha256=chunk_sha256,
            chunk_byte_size=chunk_byte_size,
            if_match=if_match,
            idempotency_key=idempotency_key,
            now=now,
        )

    def finalize_workspace_upload(
        self,
        project: ProjectRecordV2,
        upload_id: str,
        request: m.WorkspaceUploadFinalizeV2,
        *,
        if_match: str,
        idempotency_key: str,
        now: datetime,
    ) -> tuple[m.WorkspaceUploadSessionV2, bool]:
        project = _exact_project_record(project)
        session, replayed = self._workspaces.finalize_upload(
            project.project_id,
            upload_id,
            request,
            if_match=if_match,
            idempotency_key=idempotency_key,
            now=now,
        )
        snapshot = session.workspace_snapshot
        if snapshot is None:
            raise ProjectAuthorityV2Error(
                "finalized workspace upload has no immutable snapshot"
            )
        self.adopt_workspace_snapshot(
            project,
            snapshot,
            expected_project_head_id=session.expected_project_head_id,
            expected_project_config_sha256=(
                session.expected_project_config_sha256
            ),
        )
        return session, replayed

    def abort_workspace_upload(
        self,
        project: ProjectRecordV2,
        upload_id: str,
        request: m.WorkspaceUploadAbortV2,
        *,
        if_match: str,
        idempotency_key: str,
        now: datetime,
    ) -> tuple[m.WorkspaceUploadSessionV2, bool]:
        project = _exact_project_record(project)
        return self._workspaces.abort_upload(
            project.project_id,
            upload_id,
            request,
            if_match=if_match,
            idempotency_key=idempotency_key,
            now=now,
        )

    def readiness(self, project: ProjectRecordV2) -> ProjectAuthorityReadinessV2:
        project = _exact_project_record(project)
        with self._lock:
            checks, ready = self._evaluate(project, include_passed=False)
            return ProjectAuthorityReadinessV2(ready=ready, checks=tuple(checks))

    def validate_project(
        self,
        project: ProjectRecordV2,
        request: m.ProjectValidationRequestV2,
        *,
        now: datetime,
    ) -> m.ProjectValidationResponseV2:
        project = _exact_project_record(project)
        if type(request) is not m.ProjectValidationRequestV2:
            raise TypeError("project validation request has the wrong type")
        request = m.ProjectValidationRequestV2.model_validate(
            request.model_dump(mode="python")
        )
        document = self._catalog.get_project_authority_document(project.project_id)
        authority_record = (
            None
            if document is None
            else _load_authority_record(document, expected_project=project)
        )
        head = None if authority_record is None else authority_record.active_project_head
        if authority_record is not None and authority_record.publication_state == "published":
            published = self._published_authority_or_none(project.project_id)
            if published is not None:
                head = published.active_project_head
        if (
            head is None
            or request.expected_project_head_id != head.project_head_id
            or request.expected_project_head_manifest_sha256 != head.manifest_sha256
            or request.expected_project_config_sha256 != project.project_config_sha256
            or request.expected_registry_sha256 != self._registry_sha256
        ):
            raise ProjectAuthorityConflictV2(
                "project validation authority changed"
            )
        checks, ready = self._evaluate(project, include_passed=True)
        return m.ProjectValidationResponseV2(
            project_id=project.project_id,
            valid=ready,
            registry_sha256=self._registry_sha256,
            checks=checks,
            validated_at=_timestamp(now),
        )

    def _evaluate(
        self,
        project: ProjectRecordV2,
        *,
        include_passed: bool,
    ) -> tuple[list[m.ProjectValidationCheckV2], bool]:
        checks: list[m.ProjectValidationCheckV2] = []
        try:
            self.validate_config(project.config)
        except ProjectAuthorityInvalidV2 as exc:
            checks.append(
                _check(
                    "verified-evolution-configuration",
                    "failed",
                    "The saved evolution configuration is invalid.",
                    target_id=exc.target_id,
                    method_id=exc.method_id,
                )
            )
            return checks, False
        if include_passed:
            checks.append(
                _check(
                    "verified-evolution-configuration",
                    "passed",
                    "The saved evolution configuration matches the verified registry.",
                )
            )
        document = self._catalog.get_project_authority_document(project.project_id)
        authority_record = (
            None
            if document is None
            else _load_authority_record(document, expected_project=project)
        )
        resolved = self._resolve_execution(project.config)
        if resolved is None:
            checks.append(
                _check(
                    "managed-subscription-runtime",
                    "unavailable",
                    "The verified managed Subscription runtime is unavailable.",
                )
            )
            return checks, False
        verified, binding = resolved
        if include_passed:
            checks.append(
                _check(
                    "managed-subscription-runtime",
                    "passed",
                    "The managed Subscription runtime is verified and ready.",
                )
            )
        published = (
            None
            if authority_record is None
            or authority_record.publication_state != "published"
            else self._published_authority_or_none(project.project_id)
        )
        expected_execution = _effective_execution_ref(
            project_id=project.project_id,
            execution_snapshot=verified.snapshot,
            execution_snapshot_producer_id=verified.producer_id,
        )
        effective_matches = bool(
            published is not None
            and authority_record is not None
            and authority_record.framework_lock_sha256
            == binding.framework_lock_digest
            and published.active_project_head.effective_execution_snapshot
            == expected_execution
            and published.active_project_head.registry_sha256
            == self._registry_sha256
            and published.active_project_head.runtime_context_snapshot
            .runtime_contract_sha256
            == self._runtime_contract_sha256
        )
        if include_passed or not effective_matches:
            checks.append(
                _check(
                    "effective-execution-snapshot",
                    "passed" if effective_matches else "failed",
                    (
                        "The active project head pins the current effective execution."
                        if effective_matches
                        else "The effective execution differs from the active project head."
                    ),
                )
            )
        workspace_matches = False
        if published is not None:
            try:
                stored = self._workspaces.get_snapshot(
                    published.workspace_snapshot.workspace_snapshot_id
                )
                self._workspaces.snapshot_path(published.workspace_snapshot)
                workspace_matches = stored == published.workspace_snapshot
            except (WorkspaceIntegrityErrorV2, WorkspaceNotFoundV2):
                workspace_matches = False
        if include_passed or not workspace_matches:
            checks.append(
                _check(
                    "workspace-snapshot",
                    "passed" if workspace_matches else "unavailable",
                    (
                        "The immutable workspace snapshot is verified."
                        if workspace_matches
                        else "The immutable workspace snapshot is unavailable."
                    ),
                )
            )
        science_matches = False
        if (
            authority_record is not None
            and authority_record.publication_state == "published"
            and published is not None
        ):
            expected = _science_authority(authority_record)
            try:
                science_matches = self._require_published_authority(expected) == published
            except ProjectAuthorityConflictV2:
                science_matches = False
        ready = effective_matches and workspace_matches and science_matches
        return checks, ready

    def _resolve_execution(
        self,
        config: m.ScienceProjectConfigV2,
        *,
        ensure_services: bool = False,
    ) -> tuple[VerifiedExecutionSnapshot, ServiceRunBinding] | None:
        try:
            if ensure_services:
                self._services.ensure(
                    ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
                    codex_model=config.execution.codex_model,
                    runtime_image=MANAGED_RUNTIME_IMAGES["managed_science"],
                )
            binding = self._services.run_binding()
        except Exception:
            return None
        if type(binding) is not ServiceRunBinding:
            return None
        try:
            binding.__post_init__()
        except (TypeError, ValueError):
            return None
        if binding.registry_digest != self._registry_sha256:
            return None
        settings = EffectiveExecutionSettings(
            execution_mode=config.execution.mode,
            capture_mode=config.execution.capture_mode,
            harness_id=config.execution.harness_id,
            model_ref=config.execution.codex_model,
            token_limit=config.execution.token_limit,
            task_network_allow_internet=(
                config.execution.task_network_allow_internet
            ),
        )
        try:
            verified = resolve_genesis_execution_snapshot(
                settings=settings,
                service_binding=binding,
            )
        except EffectiveExecutionSnapshotUnavailable:
            return None
        return require_verified_execution_snapshot(verified), binding

    def _published_authority_or_none(
        self,
        project_id: str,
    ) -> ScienceProjectAdmissionAuthorityV2 | None:
        try:
            return self._tasks.project_admission_authority(project_id)
        except CoreTaskControlError as exc:
            if exc.code == "task_not_found":
                return None
            raise ProjectAuthorityV2Error(
                "science project authority is unavailable"
            ) from exc

    def _require_published_authority(
        self,
        expected: ScienceProjectAdmissionAuthorityV2,
    ) -> ScienceProjectAdmissionAuthorityV2:
        actual = self._published_authority_or_none(expected.project_id)
        if actual is None:
            raise ProjectAuthorityConflictV2(
                "published project authority differs from the catalog"
            )
        heads = self._tasks.list_project_heads(expected.project_id)
        if (
            actual.project_id != expected.project_id
            or actual.project_config_sha256 != expected.project_config_sha256
            or actual.normalized_evolution_intent_sha256
            != expected.normalized_evolution_intent_sha256
            or not heads
            or heads[0] != expected.active_project_head
            or heads[-1] != actual.active_project_head
            or actual.workspace_snapshot != actual.active_project_head.workspace_snapshot
        ):
            raise ProjectAuthorityConflictV2(
                "published project authority differs from the catalog"
            )
        return actual

    def _mark_published(
        self,
        document: ProjectAuthorityDocumentV2,
        record: _ProjectAuthorityRecordV1,
        authority: ScienceProjectAdmissionAuthorityV2,
    ) -> ScienceProjectAdmissionAuthorityV2:
        published_record = record.model_copy(update={"publication_state": "published"})
        self._catalog.put_project_authority_document(
            project_id=record.project_id,
            record_json=_authority_record_bytes(published_record),
            expected_record_sha256=document.record_sha256,
        )
        return authority


def normalized_evolution_intent_sha256_for(
    config: m.ScienceProjectConfigV2,
) -> str:
    config = _exact_config(config)
    return canonical_digest(config.evolution)


def _build_genesis_authority(
    *,
    project: ProjectRecordV2,
    workspace: m.WorkspaceSnapshotRefV2,
    verified_execution: VerifiedExecutionSnapshot,
    registry_sha256: str,
    runtime_contract_sha256: str,
    normalized_evolution_intent_sha256: str,
) -> ScienceProjectAdmissionAuthorityV2:
    verified_execution = require_verified_execution_snapshot(verified_execution)
    head = _build_genesis_head(
        project_id=project.project_id,
        workspace=workspace,
        execution_snapshot=verified_execution.snapshot,
        execution_snapshot_producer_id=verified_execution.producer_id,
        registry_sha256=registry_sha256,
        runtime_contract_sha256=runtime_contract_sha256,
    )
    return ScienceProjectAdmissionAuthorityV2(
        project_id=project.project_id,
        active_project_head=head,
        project_config_sha256=project.project_config_sha256,
        workspace_snapshot=workspace,
        normalized_evolution_intent_sha256=(
            normalized_evolution_intent_sha256
        ),
    )


def _build_genesis_head(
    *,
    project_id: str,
    workspace: m.WorkspaceSnapshotRefV2,
    execution_snapshot: ExecutionSnapshotV1,
    execution_snapshot_producer_id: str,
    registry_sha256: str,
    runtime_contract_sha256: str,
) -> m.ProjectHeadRefV2:
    execution = _effective_execution_ref(
        project_id=project_id,
        execution_snapshot=execution_snapshot,
        execution_snapshot_producer_id=execution_snapshot_producer_id,
    )
    evolution_manifest = {
        "artifact_ids": [],
        "evolution_revision_contract_version": "2",
        "project_id": project_id,
    }
    evolution_sha256 = _canonical_sha256(evolution_manifest)
    evolution = m.EvolutionRevisionRefV2(
        evolution_revision_id=f"evolution-{evolution_sha256}",
        project_id=project_id,
        manifest_sha256=evolution_sha256,
        artifact_count=0,
    )
    runtime_manifest = {
        "contributions": [],
        "evolution_revision": evolution.model_dump(mode="json"),
        "project_id": project_id,
        "registry_sha256": registry_sha256,
        "runtime_context_contract_version": "2",
        "runtime_contract_sha256": runtime_contract_sha256,
    }
    runtime_sha256 = _canonical_sha256(runtime_manifest)
    runtime = m.RuntimeContextSnapshotRefV2(
        runtime_context_snapshot_id=f"runtime-context-{runtime_sha256}",
        project_id=project_id,
        evolution_revision_id=evolution.evolution_revision_id,
        evolution_revision_manifest_sha256=evolution.manifest_sha256,
        registry_sha256=registry_sha256,
        runtime_contract_sha256=runtime_contract_sha256,
        manifest_sha256=runtime_sha256,
    )
    head_manifest = {
        "effective_execution_snapshot": execution.model_dump(mode="json"),
        "evolution_revision": evolution.model_dump(mode="json"),
        "generation": 0,
        "predecessor_project_head_id": None,
        "project_head_contract_version": "2",
        "project_id": project_id,
        "registry_sha256": registry_sha256,
        "runtime_context_snapshot": runtime.model_dump(mode="json"),
        "workspace_snapshot": workspace.model_dump(mode="json"),
    }
    head_sha256 = _canonical_sha256(head_manifest)
    head = m.ProjectHeadRefV2(
        project_head_id=f"project-head-{head_sha256}",
        project_id=project_id,
        generation=0,
        predecessor_project_head_id=None,
        workspace_snapshot=workspace,
        evolution_revision=evolution,
        runtime_context_snapshot=runtime,
        effective_execution_snapshot=execution,
        registry_sha256=registry_sha256,
        manifest_sha256=head_sha256,
    )
    return head


def _effective_execution_ref(
    *,
    project_id: str,
    execution_snapshot: ExecutionSnapshotV1,
    execution_snapshot_producer_id: str,
) -> m.EffectiveExecutionSnapshotRefV2:
    execution_sha256 = canonical_digest(execution_snapshot)
    return m.EffectiveExecutionSnapshotRefV2(
        effective_execution_snapshot_id=f"exec-{execution_sha256}",
        project_id=project_id,
        execution_mode="codex_subscription_transcript",
        capture_mode="transcript",
        token_level_metrics_available=False,
        producer_id=execution_snapshot_producer_id,
        snapshot_sha256=execution_sha256,
    )


def _science_authority(
    record: _ProjectAuthorityRecordV1,
) -> ScienceProjectAdmissionAuthorityV2:
    if record.active_project_head is None or record.workspace_snapshot is None:
        raise ProjectAuthorityV2Error("project genesis is not prepared")
    return ScienceProjectAdmissionAuthorityV2(
        project_id=record.project_id,
        active_project_head=record.active_project_head,
        project_config_sha256=record.project_config_sha256,
        workspace_snapshot=record.workspace_snapshot,
        normalized_evolution_intent_sha256=(
            record.normalized_evolution_intent_sha256
        ),
    )


def _load_authority_record(
    document: ProjectAuthorityDocumentV2,
    *,
    expected_project: ProjectRecordV2,
) -> _ProjectAuthorityRecordV1:
    try:
        decoded = _decode_bounded_json(document.record_json)
        record = _ProjectAuthorityRecordV1.model_validate(decoded)
    except (TypeError, ValueError, ValidationError) as exc:
        raise ProjectAuthorityV2Error(
            "persisted project authority record is invalid"
        ) from exc
    if (
        record.project_id != document.project_id
        or record.project_id != expected_project.project_id
        or record.project_config_sha256 != expected_project.project_config_sha256
        or record.normalized_evolution_intent_sha256
        != normalized_evolution_intent_sha256_for(expected_project.config)
        or _authority_record_bytes(record) != document.record_json
    ):
        raise ProjectAuthorityV2Error(
            "persisted project authority record differs from the catalog"
        )
    if (
        record.active_project_head is not None
        and record.workspace_snapshot is not None
        and record.execution_snapshot is not None
        and record.execution_snapshot_producer_id is not None
    ):
        expected_head = _build_genesis_head(
            project_id=record.project_id,
            workspace=record.workspace_snapshot,
            execution_snapshot=record.execution_snapshot,
            execution_snapshot_producer_id=(
                record.execution_snapshot_producer_id
            ),
            registry_sha256=record.active_project_head.registry_sha256,
            runtime_contract_sha256=(
                record.active_project_head.runtime_context_snapshot
                .runtime_contract_sha256
            ),
        )
        if record.active_project_head != expected_head:
            raise ProjectAuthorityV2Error(
                "persisted project genesis is not content addressed"
            )
    return record


def _authority_record_bytes(record: _ProjectAuthorityRecordV1) -> bytes:
    return json.dumps(
        record.model_dump(mode="json"),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _decode_bounded_json(payload: bytes) -> dict[str, object]:
    if type(payload) is not bytes or not 1 <= len(payload) <= _MAX_RECORD_BYTES:
        raise ValueError("project authority record exceeds its byte bound")
    try:
        value = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("project authority record is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("project authority record is not an object")
    stack: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_RECORD_NODES or depth > _MAX_RECORD_DEPTH:
            raise ValueError("project authority record exceeds its structure bound")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return value


def _exact_config(config: m.ScienceProjectConfigV2) -> m.ScienceProjectConfigV2:
    if type(config) is not m.ScienceProjectConfigV2:
        raise TypeError("project authority requires exact ScienceProjectConfigV2")
    return m.ScienceProjectConfigV2.model_validate(config.model_dump(mode="python"))


def _exact_project_record(project: ProjectRecordV2) -> ProjectRecordV2:
    if type(project) is not ProjectRecordV2:
        raise TypeError("project authority requires exact ProjectRecordV2")
    if m.project_config_sha256_for(project.config) != project.project_config_sha256:
        raise ProjectAuthorityV2Error("project record config digest differs")
    return project


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _digest(value: str, *, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"project authority {label} digest is invalid")
    return value


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypeError("project authority timestamp requires timezone information")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00",
        "Z",
    )


def _check(
    check_id: str,
    status: Literal["passed", "failed", "unavailable"],
    message: str,
    *,
    target_id: str | None = None,
    method_id: str | None = None,
) -> m.ProjectValidationCheckV2:
    return m.ProjectValidationCheckV2(
        check_id=check_id,
        status=status,
        message=message,
        target_id=target_id,
        method_id=method_id,
    )


__all__ = [
    "ProjectAuthorityConflictV2",
    "ProjectAuthorityInvalidV2",
    "ProjectAuthorityReadinessV2",
    "ProjectAuthorityV2",
    "ProjectAuthorityV2Error",
    "ServiceBindingProviderV2",
    "normalized_evolution_intent_sha256_for",
]
