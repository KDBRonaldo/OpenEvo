"""Release maintenance ownership for the frozen Core Control v1 contract."""

from __future__ import annotations

from datetime import datetime, timezone
import hmac
import threading
from typing import Literal, cast

from fastapi.responses import JSONResponse

from openevo.backend.contracts.v1 import models as m
from openevo.backend.contracts.v1.store import (
    CoreControlStoreV1,
    ETagPreconditionError,
    ResourceConflictError,
    ResourceNotFoundError,
    StoredResult,
)
from openevo.backend.run_control import CoreRunControl, CoreRunControlError
from openevo.backend.service_control import (
    CoreServiceControl,
    CoreServiceControlError,
    ServiceRestartAttempt,
    ServiceRestartAttemptState,
)
from openevo.evolution.framework.builtins import VerifiedExecutableRegistry


_MAX_SUPERVISOR_LOGS = 10_000


class CoreMaintenanceOwnerV1:
    """Owns bounded doctor, repair, service, diagnostic, and cache operations."""

    def __init__(
        self,
        store: CoreControlStoreV1,
        *,
        registry: VerifiedExecutableRegistry | None,
        service_control: CoreServiceControl | None,
        run_control: CoreRunControl | None,
        clock=None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._service_control = service_control
        self._service_control_complete = service_control is not None and all(
            callable(getattr(service_control, name, None))
            for name in (
                "acknowledge_restart_attempt",
                "cancel",
                "get",
                "list",
                "list_restart_attempts",
                "logs",
                "restart",
                "restart_once",
                "run_binding",
            )
        )
        self._run_control = run_control
        self._run_control_complete = run_control is not None and all(
            callable(getattr(run_control, name, None))
            for name in ("close", "counts", "invoke", "verify")
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._repair_lock = threading.Lock()
        self._restart_lock = threading.Lock()
        self._cleanup_lock = threading.Lock()
        self._diagnostic_lock = threading.Lock()
        self._cancellation_lock = threading.Lock()
        self._cancellations: dict[str, threading.Event] = {}
        if self._service_control_complete:
            self._reconcile_service_restart_operations()

    @property
    def diagnostics_available(self) -> bool:
        return True

    @property
    def service_control_available(self) -> bool:
        return self._service_control_complete

    @property
    def maintenance_available(self) -> bool:
        return self._registry is not None and self._run_control_complete

    def doctor(
        self,
        request: m.EnvironmentDoctorRequestV1,
        *,
        idempotency_key: str,
    ) -> StoredResult:
        response = self._doctor_response(request)
        return self._store.store_doctor_response(
            request,
            response,
            idempotency_key=idempotency_key,
        )

    def repair(
        self,
        request: m.EnvironmentRepairRequestV1,
        *,
        idempotency_key: str,
    ) -> StoredResult:
        with self._repair_lock:
            operation_request = m.EnvironmentRepairOperationRequestV1(
                kind=m.OperationKind.ENVIRONMENT_REPAIR,
                request=request,
            )
            created = self._store.create_maintenance_operation(
                "repairCoreEnvironmentV1",
                operation_request,
                resource_scope="environment",
                idempotency_key=idempotency_key,
            )
            operation = cast(m.OperationV1, created.model)
            current = self._store.get_maintenance_operation(operation.id)
            if created.replayed or _operation_terminal(current):
                return StoredResult(202, current, current.etag, replayed=created.replayed)
            blocked = self._maintenance_busy_error(operation.id)
            if blocked is not None:
                terminal = self._store.fail_maintenance_operation(operation.id, blocked)
                return StoredResult(202, terminal, terminal.etag)
            cancellation = threading.Event()
            with self._cancellation_lock:
                self._cancellations[operation.id] = cancellation
            try:
                self._store.mark_maintenance_operation_running(operation.id)
                results: list[m.RepairActionResultV1] = []
                for action in request.actions:
                    if cancellation.is_set():
                        return self._cancelled_result(operation.id)
                    results.append(
                        self._repair_action(
                            action,
                            operation_id=operation.id,
                        )
                    )
                if cancellation.is_set():
                    return self._cancelled_result(operation.id)
                response = m.EnvironmentRepairResponseV1(
                    status=_doctor_status(result.status for result in results),
                    results=results,
                    checked_at=self._timestamp(),
                )
                terminal = self._store.complete_maintenance_operation(
                    operation.id,
                    m.EnvironmentRepairOperationResultV1(
                        kind=m.OperationKind.ENVIRONMENT_REPAIR,
                        response=response,
                    ),
                )
                return StoredResult(202, terminal, terminal.etag)
            except ResourceConflictError:
                current = self._store.get_maintenance_operation(operation.id)
                if current.status is m.OperationStatus.CANCELLING:
                    return self._cancelled_result(operation.id)
                raise
            except Exception as exc:
                terminal = self._fail_operation(
                    operation.id,
                    code="environment_repair_failed",
                    message="Core could not complete the requested environment repair.",
                    category=m.ErrorCategory.ENVIRONMENT,
                    retryable=isinstance(exc, CoreServiceControlError),
                )
                return StoredResult(202, terminal, terminal.etag)
            finally:
                with self._cancellation_lock:
                    self._cancellations.pop(operation.id, None)

    def restart_service(
        self,
        service_id: str,
        request: m.ServiceRestartRequestV1,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> StoredResult:
        service_control = self._require_service_control()
        with self._restart_lock:
            operation_request = m.ServiceRestartOperationRequestV1(
                kind=m.OperationKind.SERVICE_RESTART,
                service_id=service_id,
                request=request,
            )
            replay = self._store.replay_maintenance_operation_request(
                "restartCoreServiceV1",
                operation_request,
                resource_scope=service_id,
                idempotency_key=idempotency_key,
                semantic_headers={"if-match": if_match},
            )
            if replay is not None:
                return replay
            service = self._service(service_id)
            if not hmac.compare_digest(service.etag, if_match):
                raise ETagPreconditionError("service")
            if not service.restartable:
                raise ResourceConflictError(
                    "service_not_restartable",
                    "The managed service is not restartable in its current state.",
                )
            created = self._store.create_maintenance_operation(
                "restartCoreServiceV1",
                operation_request,
                resource_scope=service_id,
                idempotency_key=idempotency_key,
                semantic_headers={"if-match": if_match},
            )
            operation = cast(m.OperationV1, created.model)
            current = self._store.get_maintenance_operation(operation.id)
            if created.replayed or _operation_terminal(current):
                return StoredResult(202, current, current.etag, replayed=created.replayed)
            blocked = self._maintenance_busy_error(operation.id)
            if blocked is not None:
                terminal = self._store.fail_maintenance_operation(operation.id, blocked)
                return StoredResult(202, terminal, terminal.etag)
            try:
                self._store.mark_maintenance_operation_running(operation.id)
                current_service = service_control.get(service_id).to_contract()
                if not hmac.compare_digest(current_service.etag, if_match):
                    raise ETagPreconditionError("service")
                restarted = service_control.restart_once(
                    service_id,
                    operation_id=operation.id,
                    expected_service_etag=if_match,
                ).to_contract()
                if restarted.id != service_id:
                    raise CoreServiceControlError(
                        "service restart returned a different service identity"
                    )
                terminal = self._store.complete_service_restart_operation(
                    operation.id,
                    restarted,
                )
                try:
                    service_control.acknowledge_restart_attempt(
                        operation.id,
                        service_id=service_id,
                        expected_service_etag=if_match,
                    )
                except CoreServiceControlError:
                    # The operation result is already committed.  Retain the
                    # receipt so startup reconciliation can acknowledge it.
                    pass
                return StoredResult(202, terminal, terminal.etag)
            except Exception as exc:
                recovered = self._recover_restart_after_exception(
                    operation.id,
                    service_id=service_id,
                    expected_service_etag=if_match,
                )
                if recovered is not None:
                    return StoredResult(202, recovered, recovered.etag)
                terminal = self._fail_operation(
                    operation.id,
                    code="service_restart_failed",
                    message="Core could not restart the managed service.",
                    category=m.ErrorCategory.SERVICE,
                    retryable=isinstance(exc, CoreServiceControlError),
                )
                return StoredResult(202, terminal, terminal.etag)

    def _recover_restart_after_exception(
        self,
        operation_id: str,
        *,
        service_id: str,
        expected_service_etag: str,
    ) -> m.OperationV1 | None:
        service_control = self._require_service_control()
        attempts = {
            attempt.operation_id: attempt
            for attempt in service_control.list_restart_attempts()
        }
        attempt = attempts.get(operation_id)
        if attempt is None:
            return None
        self._require_matching_restart_attempt(
            attempt,
            service_id=service_id,
            expected_service_etag=expected_service_etag,
        )
        current = self._store.get_maintenance_operation(operation_id)
        if current.status is m.OperationStatus.SUCCEEDED:
            terminal = current
        elif attempt.state is ServiceRestartAttemptState.COMPLETED:
            if attempt.service is None:
                raise CoreServiceControlError(
                    "completed service restart receipt lacks its result"
                )
            terminal = self._store.complete_service_restart_operation(
                operation_id,
                attempt.service.to_contract(),
            )
        else:
            terminal = self._store.fail_maintenance_operation(
                operation_id,
                _service_restart_recovery_error(
                    operation_id,
                    code="service_restart_outcome_unknown",
                    message=(
                        "Core cannot prove the outcome of the interrupted managed "
                        "service restart and will not repeat it."
                    ),
                    retryable=False,
                ),
            )
        try:
            service_control.acknowledge_restart_attempt(
                operation_id,
                service_id=service_id,
                expected_service_etag=expected_service_etag,
            )
        except CoreServiceControlError:
            pass
        return terminal

    def _reconcile_service_restart_operations(self) -> None:
        service_control = self._require_service_control()
        attempts = service_control.list_restart_attempts()
        by_operation: dict[str, ServiceRestartAttempt] = {}
        for attempt in attempts:
            if attempt.operation_id in by_operation:
                raise CoreServiceControlError(
                    "service restart receipt identity is duplicated"
                )
            by_operation[attempt.operation_id] = attempt

        interrupted = {
            operation.id: (operation, expected_etag)
            for operation, expected_etag in (
                self._store.interrupted_service_restart_operations()
            )
        }
        for operation_id, (operation, expected_etag) in interrupted.items():
            request = cast(m.ServiceRestartOperationRequestV1, operation.request)
            attempt = by_operation.pop(operation_id, None)
            if attempt is None:
                self._store.fail_maintenance_operation(
                    operation_id,
                    _service_restart_recovery_error(
                        operation_id,
                        code="service_restart_interrupted_before_side_effect",
                        message=(
                            "Core restarted before the managed service restart "
                            "side effect was durably admitted."
                        ),
                        retryable=True,
                    ),
                )
                continue
            self._require_matching_restart_attempt(
                attempt,
                service_id=request.service_id,
                expected_service_etag=expected_etag,
            )
            if attempt.state is ServiceRestartAttemptState.STARTED:
                self._store.fail_maintenance_operation(
                    operation_id,
                    _service_restart_recovery_error(
                        operation_id,
                        code="service_restart_outcome_unknown",
                        message=(
                            "Core cannot prove the outcome of the interrupted "
                            "managed service restart and will not repeat it."
                        ),
                        retryable=False,
                    ),
                )
            else:
                if attempt.service is None:
                    raise CoreServiceControlError(
                        "completed service restart receipt lacks its result"
                    )
                restarted = attempt.service.to_contract()
                if restarted.id != request.service_id:
                    raise CoreServiceControlError(
                        "service restart receipt returned a different service identity"
                    )
                self._store.complete_service_restart_operation(
                    operation_id,
                    restarted,
                )
            service_control.acknowledge_restart_attempt(
                operation_id,
                service_id=request.service_id,
                expected_service_etag=expected_etag,
            )

        for operation_id, attempt in by_operation.items():
            operation, expected_etag = self._store.service_restart_operation_authority(
                operation_id
            )
            request = cast(m.ServiceRestartOperationRequestV1, operation.request)
            self._require_matching_restart_attempt(
                attempt,
                service_id=request.service_id,
                expected_service_etag=expected_etag,
            )
            if operation.status is m.OperationStatus.FAILED:
                if attempt.state is not ServiceRestartAttemptState.STARTED:
                    raise CoreServiceControlError(
                        "failed service restart operation has a completed receipt"
                    )
            elif operation.status is m.OperationStatus.SUCCEEDED:
                if (
                    attempt.state is not ServiceRestartAttemptState.COMPLETED
                    or attempt.service is None
                    or not isinstance(
                        operation.result,
                        m.ServiceRestartOperationResultV1,
                    )
                    or operation.result.service != attempt.service.to_contract()
                ):
                    raise CoreServiceControlError(
                        "service restart receipt conflicts with its terminal operation"
                    )
            else:
                raise CoreServiceControlError(
                    "service restart receipt has no matching terminal operation"
                )
            service_control.acknowledge_restart_attempt(
                operation_id,
                service_id=request.service_id,
                expected_service_etag=expected_etag,
            )

    @staticmethod
    def _require_matching_restart_attempt(
        attempt: ServiceRestartAttempt,
        *,
        service_id: str,
        expected_service_etag: str,
    ) -> None:
        if (
            attempt.service_id != service_id
            or not hmac.compare_digest(
                attempt.expected_service_etag,
                expected_service_etag,
            )
        ):
            raise CoreServiceControlError(
                "service restart receipt does not match its durable operation authority"
            )

    def service_logs(
        self,
        service_id: str,
        *,
        limit: int,
        after: str | None,
        sort: str,
        direction: str,
    ) -> m.LogPageV1:
        service_control = self._require_service_control()
        self._service(service_id)
        entries: list[m.LogEntryV1] = []
        after_sequence = 0
        while len(entries) < _MAX_SUPERVISOR_LOGS:
            batch = service_control.logs(
                service_id,
                after_sequence=after_sequence,
                limit=100,
            )
            if not batch:
                break
            converted = [item.to_contract() for item in batch]
            if any(item.service_id != service_id for item in converted) or any(
                item.sequence <= after_sequence for item in converted
            ):
                raise CoreServiceControlError(
                    "service log snapshot has an invalid service or sequence identity"
                )
            entries.extend(converted)
            after_sequence = converted[-1].sequence
            if len(batch) < 100:
                break
        if len(entries) >= _MAX_SUPERVISOR_LOGS:
            extra = service_control.logs(
                service_id,
                after_sequence=after_sequence,
                limit=1,
            )
            if extra:
                raise CoreServiceControlError("service log snapshot exceeds its bound")
        return self._store.paginate_observed_logs(
            service_id,
            entries,
            limit=limit,
            after=after,
            sort=cast(Literal["sequence", "occurred_at"], sort),
            direction=cast(Literal["asc", "desc"], direction),
        )

    def get_operation(self, operation_id: str) -> m.OperationV1:
        return self._store.get_maintenance_operation(operation_id)

    def cancel_operation(
        self,
        operation_id: str,
        request: m.OperationCancelRequestV1,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> StoredResult:
        result = self._store.cancel_maintenance_operation(
            operation_id,
            request,
            if_match=if_match,
            idempotency_key=idempotency_key,
        )
        if not result.replayed:
            with self._cancellation_lock:
                cancellation = self._cancellations.get(operation_id)
                if cancellation is not None:
                    cancellation.set()
        return result

    def referenced_logs(
        self,
        logs_ref: str,
        *,
        limit: int,
        after: str | None,
        sort: str,
        direction: str,
    ) -> m.ReferencedLogPageV1:
        return self._store.get_referenced_logs(
            logs_ref,
            limit=limit,
            after=after,
            sort=cast(Literal["sequence", "occurred_at"], sort),
            direction=cast(Literal["asc", "desc"], direction),
        )

    def create_diagnostic(
        self,
        request: m.DiagnosticsRequestV1,
        *,
        idempotency_key: str,
    ) -> StoredResult:
        self._validate_diagnostic_target(request.target)
        with self._diagnostic_lock:
            created = self._store.create_diagnostic(
                request,
                idempotency_key=idempotency_key,
            )
            diagnostic = cast(m.DiagnosticV1, created.model)
            current = self._store.get_diagnostic(diagnostic.id)
            if created.replayed or _diagnostic_terminal(current):
                return StoredResult(202, current, current.etag, replayed=created.replayed)
            try:
                self._store.mark_diagnostic_running(diagnostic.id)
                checks = self._diagnostic_checks(request)
                terminal = self._store.complete_diagnostic(diagnostic.id, checks)
                return StoredResult(202, terminal, terminal.etag)
            except Exception as exc:
                terminal = self._store.fail_diagnostic(
                    diagnostic.id,
                    _operation_error(
                        diagnostic.id,
                        code="diagnostic_failed",
                        message="Core could not complete the requested diagnostic.",
                        category=m.ErrorCategory.SERVICE,
                        retryable=isinstance(exc, (CoreServiceControlError, CoreRunControlError)),
                    ),
                )
                return StoredResult(202, terminal, terminal.etag)

    def get_diagnostic(self, diagnostic_id: str) -> m.DiagnosticV1:
        return self._store.get_diagnostic(diagnostic_id)

    def delete_diagnostic(
        self,
        diagnostic_id: str,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> StoredResult:
        return self._store.delete_diagnostic(
            diagnostic_id,
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    def cleanup_caches(
        self,
        request: m.CacheCleanupRequestV1,
        *,
        idempotency_key: str,
    ) -> StoredResult:
        with self._cleanup_lock:
            operation_request = m.CacheCleanupOperationRequestV1(
                kind=m.OperationKind.CACHE_CLEANUP,
                request=request,
            )
            created = self._store.create_maintenance_operation(
                "cleanupCoreCachesV1",
                operation_request,
                resource_scope="caches",
                idempotency_key=idempotency_key,
            )
            operation = cast(m.OperationV1, created.model)
            current = self._store.get_maintenance_operation(operation.id)
            if created.replayed or _operation_terminal(current):
                return StoredResult(202, current, current.etag, replayed=created.replayed)
            blocked = self._maintenance_busy_error(operation.id)
            if blocked is not None:
                terminal = self._store.fail_maintenance_operation(operation.id, blocked)
                return StoredResult(202, terminal, terminal.etag)
            try:
                self._store.mark_maintenance_operation_running(operation.id)
                unsupported = [
                    scope
                    for scope in request.scopes
                    if scope is not m.CacheScope.COMPLETED_DIAGNOSTICS
                ]
                if unsupported:
                    terminal = self._store.fail_maintenance_operation(
                        operation.id,
                        _operation_error(
                            operation.id,
                            code="cache_scope_unsupported",
                            message=(
                                "One or more requested cache scopes have no "
                                "verified Core cache owner."
                            ),
                            category=m.ErrorCategory.SERVICE,
                            retryable=False,
                        ),
                    )
                    return StoredResult(202, terminal, terminal.etag)
                removed_entries = 0
                reclaimed_bytes = 0
                if m.CacheScope.COMPLETED_DIAGNOSTICS in request.scopes:
                    removed_entries, reclaimed_bytes = self._store.cleanup_completed_diagnostics(
                        older_than_days=request.older_than_days
                    )
                terminal = self._store.complete_maintenance_operation(
                    operation.id,
                    m.CacheCleanupOperationResultV1(
                        kind=m.OperationKind.CACHE_CLEANUP,
                        result=m.CacheCleanupResultV1(
                            scopes=request.scopes,
                            removed_entries=removed_entries,
                            reclaimed_bytes=reclaimed_bytes,
                        ),
                    ),
                )
                return StoredResult(202, terminal, terminal.etag)
            except Exception as exc:
                terminal = self._fail_operation(
                    operation.id,
                    code="cache_cleanup_failed",
                    message="Core could not clean its owned regenerable caches.",
                    category=m.ErrorCategory.SERVICE,
                    retryable=not isinstance(exc, ValueError),
                )
                return StoredResult(202, terminal, terminal.etag)

    def _doctor_response(
        self, request: m.EnvironmentDoctorRequestV1
    ) -> m.EnvironmentDoctorResponseV1:
        kinds = request.checks or _default_environment_checks(request.execution_mode)
        service_snapshot: list[m.ServiceSummaryV1] | None = None
        binding_ready = False
        if self._service_control_complete:
            assert self._service_control is not None
            try:
                service_snapshot = [
                    service.to_contract() for service in self._service_control.list()
                ]
                self._service_control.run_binding()
                binding_ready = True
            except CoreServiceControlError:
                binding_ready = False
        checks = [
            self._environment_check(
                kind,
                execution_mode=request.execution_mode,
                services=service_snapshot,
                binding_ready=binding_ready,
            )
            for kind in kinds
        ]
        return m.EnvironmentDoctorResponseV1(
            status=_doctor_status(check.status for check in checks),
            checks=checks,
            checked_at=self._timestamp(),
        )

    def _environment_check(
        self,
        kind: m.EnvironmentCheckKind,
        *,
        execution_mode: m.ExecutionMode,
        services: list[m.ServiceSummaryV1] | None,
        binding_ready: bool,
    ) -> m.EnvironmentCheckV1:
        if kind is m.EnvironmentCheckKind.PYTHON:
            return _environment_check(
                kind,
                m.CheckStatus.OK,
                "The isolated Core runtime is serving the control request.",
                m.RepairAction.OPENEVO_CAN_RETRY,
            )
        if kind is m.EnvironmentCheckKind.REGISTRY:
            if self._registry is None:
                return _environment_check(
                    kind,
                    m.CheckStatus.UNAVAILABLE,
                    "The verified executable registry is unavailable.",
                    m.RepairAction.USER_ACTION_REQUIRED,
                    "Restore the exact release registry installation.",
                )
            return _environment_check(
                kind,
                m.CheckStatus.OK,
                "The executable registry is verified for this Core process.",
                m.RepairAction.OPENEVO_CAN_RETRY,
            )
        if kind is m.EnvironmentCheckKind.STORAGE:
            try:
                self._store.verify_maintenance_authority()
            except Exception:
                return _environment_check(
                    kind,
                    m.CheckStatus.BLOCKING,
                    "Core durable maintenance state failed integrity validation.",
                    m.RepairAction.USER_ACTION_REQUIRED,
                    "Stop Core and restore the provider state from a trusted backup.",
                )
            return _environment_check(
                kind,
                m.CheckStatus.OK,
                "Core durable maintenance state passed integrity validation.",
                m.RepairAction.OPENEVO_CAN_RETRY,
            )
        if kind in {
            m.EnvironmentCheckKind.CONTAINER_RUNTIME,
            m.EnvironmentCheckKind.CODEX_SUBSCRIPTION,
        }:
            if (
                kind is m.EnvironmentCheckKind.CODEX_SUBSCRIPTION
                and execution_mode is not m.ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT
            ):
                return _environment_check(
                    kind,
                    m.CheckStatus.UNAVAILABLE,
                    "Codex subscription readiness does not apply to this execution mode.",
                    m.RepairAction.OPENEVO_CAN_RECONFIGURE,
                    "Remove the inapplicable check or select subscription execution.",
                )
            if not self._service_control_complete or not binding_ready:
                return _environment_check(
                    kind,
                    m.CheckStatus.UNAVAILABLE,
                    "Verified managed-run readiness evidence is unavailable.",
                    m.RepairAction.USER_ACTION_REQUIRED,
                    "Restore the managed service owner and its verified runtime readiness.",
                )
            return _environment_check(
                kind,
                m.CheckStatus.OK,
                "The managed service owner supplied verified run-readiness evidence.",
                m.RepairAction.OPENEVO_CAN_RETRY,
            )
        if kind is m.EnvironmentCheckKind.MODEL_SERVICE:
            if execution_mode is m.ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT:
                preparation = m.ModelPreparationV1(
                    model_ref="codex-subscription",
                    status=m.ModelPreparationStatus.UNRESOLVED,
                    updated_at=self._timestamp(),
                )
                return _environment_check(
                    kind,
                    m.CheckStatus.UNAVAILABLE,
                    "A managed inference service does not apply to subscription execution.",
                    m.RepairAction.OPENEVO_CAN_RECONFIGURE,
                    "Remove the inapplicable check or select self-deployed execution.",
                    model_preparation=preparation,
                )
            inference = next(
                (service for service in services or [] if service.kind is m.ServiceKind.INFERENCE),
                None,
            )
            if inference is None:
                preparation = m.ModelPreparationV1(
                    model_ref=(
                        "codex-subscription"
                        if execution_mode is m.ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT
                        else "unconfigured-self-deployed-model"
                    ),
                    status=m.ModelPreparationStatus.UNRESOLVED,
                    updated_at=self._timestamp(),
                )
                return _environment_check(
                    kind,
                    m.CheckStatus.UNAVAILABLE,
                    "No authoritative inference-service preparation is available.",
                    m.RepairAction.OPENEVO_CAN_RECONFIGURE,
                    "Select a supported project model and prepare its managed service.",
                    model_preparation=preparation,
                )
            status = (
                m.CheckStatus.OK
                if inference.status is m.ServiceStatus.RUNNING
                else m.CheckStatus.BLOCKING
            )
            return _environment_check(
                kind,
                status,
                "The inference service was inspected through the managed service owner.",
                (
                    m.RepairAction.OPENEVO_CAN_RETRY
                    if status is m.CheckStatus.OK
                    else m.RepairAction.OPENEVO_CAN_RECONFIGURE
                ),
                model_preparation=inference.model_preparation,
            )
        return _environment_check(
            kind,
            m.CheckStatus.UNAVAILABLE,
            "No verified network probe owner is installed.",
            m.RepairAction.USER_ACTION_REQUIRED,
            "Verify required outbound endpoints from the supported host environment.",
        )

    def _repair_action(
        self,
        action: m.EnvironmentRepairAction,
        *,
        operation_id: str,
    ) -> m.RepairActionResultV1:
        if action is m.EnvironmentRepairAction.RECONCILE_MANAGED_STATE:
            self._store.verify_maintenance_authority()
            return m.RepairActionResultV1(
                action=action,
                status=m.CheckStatus.OK,
                message=(
                    "Core-owned durable state passed its bounded consistency "
                    "and recovery verification."
                ),
            )
        if action is m.EnvironmentRepairAction.REPAIR_REGISTRY_INSTALL:
            if self._registry is not None:
                return m.RepairActionResultV1(
                    action=action,
                    status=m.CheckStatus.OK,
                    message="The verified registry installation requires no repair.",
                )
            return m.RepairActionResultV1(
                action=action,
                status=m.CheckStatus.UNAVAILABLE,
                message=(
                    "Registry installation repair requires the verified release lifecycle owner."
                ),
            )
        if action is m.EnvironmentRepairAction.RESTART_MODEL_SERVICE:
            if not self._service_control_complete:
                return m.RepairActionResultV1(
                    action=action,
                    status=m.CheckStatus.UNAVAILABLE,
                    message="The managed service owner is unavailable.",
                )
            service_control = self._require_service_control()
            services = [
                service
                for service in service_control.list()
                if service.to_contract().kind is m.ServiceKind.INFERENCE
            ]
            if not services:
                return m.RepairActionResultV1(
                    action=action,
                    status=m.CheckStatus.UNAVAILABLE,
                    message="No managed inference service is registered.",
                )
            service = sorted(services, key=lambda item: item.id)[0]
            if not service.restartable:
                return m.RepairActionResultV1(
                    action=action,
                    status=m.CheckStatus.UNAVAILABLE,
                    message="The managed inference service is not restartable.",
                )
            restarted = service_control.restart(
                service.id,
                operation_id=f"{operation_id}-model",
            ).to_contract()
            self._store.append_service_update(restarted)
            return m.RepairActionResultV1(
                action=action,
                status=(
                    m.CheckStatus.OK
                    if restarted.status is m.ServiceStatus.RUNNING
                    else m.CheckStatus.BLOCKING
                ),
                message="The managed inference service restart completed.",
            )
        if action is m.EnvironmentRepairAction.RESTART_CONTAINER_RUNTIME:
            return m.RepairActionResultV1(
                action=action,
                status=m.CheckStatus.UNAVAILABLE,
                message=(
                    "A non-root Core release cannot restart the host container "
                    "runtime or change system policy."
                ),
            )
        return m.RepairActionResultV1(
            action=action,
            status=m.CheckStatus.UNAVAILABLE,
            message="No verified network repair owner is installed.",
        )

    def _validate_diagnostic_target(self, target: m.DiagnosticTargetV1) -> None:
        if isinstance(target, m.GlobalDiagnosticTargetV1):
            return
        self._store.get_project(target.project_id)
        if isinstance(target, m.ProjectDiagnosticTargetV1):
            return
        if self._run_control is None:
            raise CoreRunControlError(
                "run_owner_unavailable",
                "Core cannot validate the diagnostic run owner.",
                http_status=503,
                retryable=True,
            )
        response = self._run_control.invoke(
            "getCoreRunV1",
            {"run_id": target.run_id},
        )
        if not isinstance(response, JSONResponse):
            raise CoreRunControlError(
                "run_owner_invalid",
                "Core run ownership returned an invalid response.",
                http_status=503,
                retryable=False,
            )
        run = m.RunV1.model_validate_json(response.body)
        if run.project_id != target.project_id:
            raise ResourceNotFoundError("run", target.run_id)

    def _diagnostic_checks(self, request: m.DiagnosticsRequestV1) -> list[m.DiagnosticCheckV1]:
        target = request.target
        checks: list[m.DiagnosticCheckV1] = []
        for scope in request.scopes:
            if scope is m.DiagnosticScope.ENVIRONMENT:
                doctor = self._doctor_response(
                    m.EnvironmentDoctorRequestV1(
                        execution_mode=m.ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
                        checks=[],
                    )
                )
                checks.extend(
                    m.DiagnosticCheckV1(
                        id=f"environment-{check.id}",
                        scope=scope,
                        status=check.status,
                        message=check.message,
                        repair_action=check.repair_action,
                    )
                    for check in doctor.checks
                )
            elif scope is m.DiagnosticScope.SERVICES:
                checks.extend(self._service_diagnostic_checks())
            elif scope is m.DiagnosticScope.REGISTRY:
                checks.append(
                    m.DiagnosticCheckV1(
                        id="registry-identity",
                        scope=scope,
                        status=(
                            m.CheckStatus.OK
                            if self._registry is not None
                            else m.CheckStatus.UNAVAILABLE
                        ),
                        message=(
                            "The verified executable registry is available."
                            if self._registry is not None
                            else "The verified executable registry is unavailable."
                        ),
                        repair_action=(
                            m.RepairAction.OPENEVO_CAN_RETRY
                            if self._registry is not None
                            else m.RepairAction.USER_ACTION_REQUIRED
                        ),
                    )
                )
            elif scope is m.DiagnosticScope.STORAGE:
                self._store.verify_maintenance_authority()
                checks.append(
                    m.DiagnosticCheckV1(
                        id="storage-authority",
                        scope=scope,
                        status=m.CheckStatus.OK,
                        message="Core durable state passed integrity validation.",
                        repair_action=m.RepairAction.OPENEVO_CAN_RETRY,
                    )
                )
            elif scope is m.DiagnosticScope.PROJECT:
                assert isinstance(target, m.ProjectDiagnosticTargetV1)
                project = self._store.get_project(target.project_id)
                checks.append(
                    m.DiagnosticCheckV1(
                        id="project-authority",
                        scope=scope,
                        status=(
                            m.CheckStatus.OK
                            if project.status is m.ProjectStatus.READY
                            else m.CheckStatus.WARNING
                        ),
                        message=(
                            "The project authority is ready."
                            if project.status is m.ProjectStatus.READY
                            else "The project authority exists but is not ready."
                        ),
                        repair_action=m.RepairAction.OPENEVO_CAN_RECONFIGURE,
                    )
                )
            else:
                assert isinstance(target, m.RunDiagnosticTargetV1)
                self._validate_diagnostic_target(target)
                checks.append(
                    m.DiagnosticCheckV1(
                        id="run-authority",
                        scope=scope,
                        status=m.CheckStatus.OK,
                        message="The run is owned by the requested project.",
                        repair_action=m.RepairAction.OPENEVO_CAN_RETRY,
                    )
                )
        return checks

    def _service_diagnostic_checks(self) -> list[m.DiagnosticCheckV1]:
        if not self._service_control_complete:
            return [
                m.DiagnosticCheckV1(
                    id="services-owner",
                    scope=m.DiagnosticScope.SERVICES,
                    status=m.CheckStatus.UNAVAILABLE,
                    message="The managed service owner is unavailable.",
                    repair_action=m.RepairAction.USER_ACTION_REQUIRED,
                )
            ]
        checks = [
            m.DiagnosticCheckV1(
                id="service-core-control",
                scope=m.DiagnosticScope.SERVICES,
                status=m.CheckStatus.OK,
                message="Core Control is serving the diagnostic request.",
                repair_action=m.RepairAction.OPENEVO_CAN_RETRY,
            )
        ]
        for service in self._require_service_control().list():
            summary = service.to_contract()
            checks.append(
                m.DiagnosticCheckV1(
                    id=f"service-{summary.id}",
                    scope=m.DiagnosticScope.SERVICES,
                    status=(
                        m.CheckStatus.OK
                        if summary.status is m.ServiceStatus.RUNNING
                        else m.CheckStatus.BLOCKING
                    ),
                    message=(
                        "The managed service is running."
                        if summary.status is m.ServiceStatus.RUNNING
                        else "The managed service is not ready."
                    ),
                    repair_action=(
                        m.RepairAction.OPENEVO_CAN_RETRY
                        if summary.restartable
                        else m.RepairAction.USER_ACTION_REQUIRED
                    ),
                )
            )
        return checks

    def _service(self, service_id: str) -> m.ServiceSummaryV1:
        service_control = self._require_service_control()
        try:
            service = service_control.get(service_id).to_contract()
        except CoreServiceControlError:
            known = {item.id: item.to_contract() for item in service_control.list()}
            if service_id not in known:
                raise ResourceNotFoundError("service", service_id) from None
            raise
        if service.id != service_id:
            raise CoreServiceControlError("service owner returned a different service identity")
        return service

    def _require_service_control(self) -> CoreServiceControl:
        if not self._service_control_complete or self._service_control is None:
            raise CoreServiceControlError("managed service owner is unavailable")
        return self._service_control

    def _cancelled_result(self, operation_id: str) -> StoredResult:
        current = self._store.get_maintenance_operation(operation_id)
        if current.status is not m.OperationStatus.CANCELLING:
            raise ResourceConflictError(
                "operation_state_changed",
                "The maintenance operation did not enter cancellation.",
            )
        terminal = self._store.complete_cancelled_maintenance_operation(operation_id)
        return StoredResult(202, terminal, terminal.etag)

    def _fail_operation(
        self,
        operation_id: str,
        *,
        code: str,
        message: str,
        category: m.ErrorCategory,
        retryable: bool,
    ) -> m.OperationV1:
        current = self._store.get_maintenance_operation(operation_id)
        if current.status is m.OperationStatus.CANCELLING:
            return self._store.complete_cancelled_maintenance_operation(operation_id)
        if _operation_terminal(current):
            return current
        return self._store.fail_maintenance_operation(
            operation_id,
            _operation_error(
                operation_id,
                code=code,
                message=message,
                category=category,
                retryable=retryable,
            ),
        )

    def _maintenance_busy_error(self, operation_id: str) -> m.ApiErrorV1 | None:
        if not self._run_control_complete:
            return _operation_error(
                operation_id,
                code="run_owner_unavailable",
                message="Core cannot prove that run admission is quiescent.",
                category=m.ErrorCategory.RUN,
                retryable=True,
            )
        assert self._run_control is not None
        try:
            active, queued = self._run_control.counts()
        except Exception:
            return _operation_error(
                operation_id,
                code="run_owner_unavailable",
                message="Core could not prove that run admission is quiescent.",
                category=m.ErrorCategory.RUN,
                retryable=True,
            )
        if active or queued:
            return _operation_error(
                operation_id,
                code="maintenance_blocked_by_runs",
                message="Core maintenance is blocked while managed runs are active or queued.",
                category=m.ErrorCategory.RUN,
                retryable=True,
            )
        return None

    def _timestamp(self) -> str:
        return (
            self._clock()
            .astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )


def _environment_check(
    kind: m.EnvironmentCheckKind,
    status: m.CheckStatus,
    message: str,
    repair_action: m.RepairAction,
    next_action: str | None = None,
    *,
    model_preparation: m.ModelPreparationV1 | None = None,
) -> m.EnvironmentCheckV1:
    return m.EnvironmentCheckV1(
        id=f"environment-{kind.value}",
        kind=kind,
        status=status,
        message=message,
        repair_action=repair_action,
        next_action=next_action,
        model_preparation=model_preparation,
    )


def _default_environment_checks(
    execution_mode: m.ExecutionMode,
) -> list[m.EnvironmentCheckKind]:
    checks = [
        m.EnvironmentCheckKind.PYTHON,
        m.EnvironmentCheckKind.CONTAINER_RUNTIME,
        m.EnvironmentCheckKind.NETWORK,
        m.EnvironmentCheckKind.STORAGE,
        m.EnvironmentCheckKind.REGISTRY,
    ]
    if execution_mode is m.ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT:
        checks.insert(2, m.EnvironmentCheckKind.CODEX_SUBSCRIPTION)
    else:
        checks.insert(2, m.EnvironmentCheckKind.MODEL_SERVICE)
    return checks


def _doctor_status(statuses) -> m.DoctorStatus:
    values = list(statuses)
    if any(status in {m.CheckStatus.BLOCKING, m.CheckStatus.UNAVAILABLE} for status in values):
        return m.DoctorStatus.NEEDS_USER_ACTION
    if any(status is m.CheckStatus.WARNING for status in values):
        return m.DoctorStatus.DEGRADED
    return m.DoctorStatus.OK


def _operation_error(
    resource_id: str,
    *,
    code: str,
    message: str,
    category: m.ErrorCategory,
    retryable: bool,
) -> m.ApiErrorV1:
    return m.ApiErrorV1(
        request_id=f"{resource_id}-error",
        code=code,
        http_status=503,
        message=message,
        severity=m.ErrorSeverity.BLOCKING,
        category=category,
        retryable=retryable,
        repair_action=(
            m.RepairAction.OPENEVO_CAN_RETRY if retryable else m.RepairAction.USER_ACTION_REQUIRED
        ),
        next_action=(
            "Retry after Core restores the required maintenance owner."
            if retryable
            else "Inspect the diagnostic result and complete the required user action."
        ),
    )


def _service_restart_recovery_error(
    operation_id: str,
    *,
    code: str,
    message: str,
    retryable: bool,
) -> m.ApiErrorV1:
    return _operation_error(
        operation_id,
        code=code,
        message=message,
        category=m.ErrorCategory.SERVICE,
        retryable=retryable,
    )


def _operation_terminal(operation: m.OperationV1) -> bool:
    return operation.status in {
        m.OperationStatus.SUCCEEDED,
        m.OperationStatus.FAILED,
        m.OperationStatus.CANCELLED,
    }


def _diagnostic_terminal(diagnostic: m.DiagnosticV1) -> bool:
    return diagnostic.status in {
        m.DiagnosticStatus.SUCCEEDED,
        m.DiagnosticStatus.FAILED,
    }


__all__ = ["CoreMaintenanceOwnerV1"]
