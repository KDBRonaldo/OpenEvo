"""Evolution orchestration for the self-hosted OpenEvo daemon.

This module preserves the proven transcript-dataset and development Evolution
SQLite workflow while moving capability resolution, fixed-input job execution,
artifact validation, retry, and publication into the daemon package.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Protocol

from openevo.backend.harness_adapter import HarnessCancellation, HarnessRunCancelled
from openevo.daemon.errors import EvolutionRunError


MAX_EVOLUTION_CAPTURE_BYTES = 2 * 1024 * 1024
EVOLUTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class EvolutionStore(Protocol):
    def project_config(self, project_id: str) -> dict[str, Any]: ...

    def dataset_artifacts(self, project_id: str) -> list[dict[str, str]]: ...

    def record_dataset_artifact(self, **kwargs: Any) -> None: ...

    def completed_sessions(self) -> list[dict[str, Any]]: ...

    def project(self, project_id: str) -> dict[str, Any]: ...

    def latest_artifact(self, project_id: str, target_id: str) -> dict[str, Any] | None: ...

    def start_evolution_job(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_evolution_job(self, job_id: str) -> dict[str, Any]: ...

    def get_session(self, session_id: str) -> dict[str, Any]: ...

    def dataset_artifact(self, artifact_id: str) -> dict[str, str]: ...

    def artifact(self, artifact_id: str) -> dict[str, Any]: ...

    def update_evolution_attempt(self, attempt_id: str, *, stage: str, message: str) -> None: ...

    def record_evolution_artifact(self, **kwargs: Any) -> dict[str, Any]: ...

    def finish_evolution_job(self, job_id: str, **kwargs: Any) -> None: ...


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def development_registry_snapshot() -> Any:
    """Build the explicit unverified catalog used only by this development bridge."""

    from openevo.evolution.framework.builtins import (
        ImplementationDistributionIdentity,
        build_builtin_registry,
    )

    identity = ImplementationDistributionIdentity(
        distribution="openevo",
        distribution_version="0.1.10.dev0",
        distribution_digest=hashlib.sha256(b"openevo-development-catalog-v1").hexdigest(),
    )
    return build_builtin_registry(identity)


def selected_document_evolution(config: object) -> list[dict[str, Any]]:
    if not isinstance(config, dict):
        return []
    evolution = config.get("evolution")
    targets = evolution.get("targets") if isinstance(evolution, dict) else None
    if not isinstance(targets, dict):
        return []
    selected: list[dict[str, Any]] = []
    for target_id, selection in sorted(targets.items()):
        if not isinstance(target_id, str) or not EVOLUTION_ID_PATTERN.fullmatch(target_id):
            continue
        if not isinstance(selection, dict) or selection.get("enabled") is not True:
            continue
        method = selection.get("method")
        method_config = selection.get("config", {})
        if not isinstance(method, str) or not EVOLUTION_ID_PATTERN.fullmatch(method):
            continue
        if not isinstance(method_config, dict):
            continue
        selected.append({"target_id": target_id, "method": method, "config": method_config})
    return selected


class EvolutionOrchestrator:
    """Development adapter driven by Core framework descriptors instead of target switches."""

    def __init__(
        self,
        *,
        state_root: Path,
        codex_binary: str,
        model: str,
        timeout_seconds: int,
    ) -> None:
        self._artifact_root = state_root / "evolution-artifacts"
        self._artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._codex_binary = codex_binary
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._registry: Any | None = None
        self._capabilities: dict[str, Any] | None = None

    def check_ready(self) -> None:
        if sys.version_info < (3, 11):
            raise EvolutionRunError(
                "real document evolution requires Python 3.11 or newer; use `uv run python`"
            )
        try:
            from openevo.evolution.framework.builtins import (  # noqa: F401
                ImplementationDistributionIdentity,
                build_builtin_registry,
            )
            from openevo.evolution.framework.capabilities import (  # noqa: F401
                build_evolution_capabilities,
            )
            from openevo.evolution.methods import METHOD_REGISTRY  # noqa: F401
            from openevo.evolution.models import WorkerClaimedJob  # noqa: F401
        except (ImportError, ModuleNotFoundError) as exc:
            raise EvolutionRunError(
                "OpenEvo Python dependencies are unavailable; run this daemon with `uv run python`"
            ) from exc
        self._load_catalog()

    def _load_catalog(self) -> None:
        from openevo.evolution.framework.capabilities import build_evolution_capabilities
        from openevo.evolution.framework.contracts import EvolutionExecutionProfile

        self._registry = development_registry_snapshot()
        capability = build_evolution_capabilities(
            self._registry,
            profile=EvolutionExecutionProfile(
                execution_mode="subscription",
                capture_mode="transcript",
                harness_id="codex",
            ),
            audience="maintainer",
            core_version="development-catalog-unverified",
        ).model_dump(mode="json")
        # The development bridge currently executes the legacy worker ABI. Context-v1 methods
        # stay registered in Core but are not advertised as runnable by this bridge yet.
        for target in capability["targets"]:
            target["methods"] = [
                method
                for method in target["methods"]
                if self._registry.methods[method["method_id"]].invocation_abi.value
                == "legacy_worker_job_v1"
            ]
        self._capabilities = capability

    def capabilities(self) -> dict[str, Any]:
        if self._capabilities is None:
            self._load_catalog()
        return {
            "schema_version": "1",
            "authority": "development_catalog_unverified",
            "capabilities": self._capabilities,
        }

    def _descriptor(self, target_id: str, method_id: str) -> tuple[Any, Any, Any]:
        if self._registry is None:
            self._load_catalog()
        try:
            target = self._registry.targets[target_id]
            method = self._registry.methods[method_id]
            handler = self._registry.target_handlers[target.handler_id]
        except KeyError as exc:
            raise EvolutionRunError(
                f"unknown evolution selection {target_id}/{method_id}"
            ) from exc
        if method.target_id != target.id:
            raise EvolutionRunError(f"{method_id} does not belong to target {target_id}")
        if method.invocation_abi.value != "legacy_worker_job_v1":
            raise EvolutionRunError(
                f"{method_id} uses {method.invocation_abi.value}, which this development bridge "
                "does not execute yet"
            )
        return target, method, handler

    def _method_config(self, method: Any, requested: dict[str, Any]) -> dict[str, Any]:
        if self._registry is None:
            raise EvolutionRunError("evolution catalog is unavailable")
        config = dict(requested)
        for injection in method.project_config_injections:
            if injection.source.value == "reflector_llm":
                config[injection.field_name] = {
                    "provider": "codex_cli",
                    "model": self._model,
                    "timeout_seconds": self._timeout_seconds,
                }
            else:
                raise EvolutionRunError(
                    f"development bridge cannot provide {injection.source.value}"
                )
        normalized = self._registry.normalize_method_config(method.id, config)
        normalized.update({"promoted": True})
        return normalized

    @staticmethod
    def _materialize_previous(previous: dict[str, Any], root: Path) -> str:
        documents = previous.get("documents", [])
        if previous.get("renderer_kind") == "file_bundle":
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            for document in documents:
                destination = root / document["path"]
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                destination.write_text(document["content"], encoding="utf-8")
            return root.resolve().as_uri()
        root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        content = documents[0]["content"] if documents else ""
        root.write_text(content, encoding="utf-8")
        return root.resolve().as_uri()

    @staticmethod
    def _read_documents(uri: str, renderer_kind: str) -> list[dict[str, str]]:
        from urllib.parse import unquote, urlparse

        parsed = urlparse(uri)
        if parsed.scheme != "file":
            return []
        path = Path(unquote(parsed.path))
        if renderer_kind == "adapter":
            return []
        candidates = (
            [path]
            if path.is_file()
            else sorted(candidate for candidate in path.rglob("*") if candidate.is_file())
        )
        documents: list[dict[str, str]] = []
        for candidate in candidates[:128]:
            if candidate.stat().st_size > MAX_EVOLUTION_CAPTURE_BYTES:
                continue
            try:
                content = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            relative = candidate.name if path.is_file() else candidate.relative_to(path).as_posix()
            documents.append(
                {
                    "path": relative,
                    "media_type": "text/markdown"
                    if relative.lower().endswith(".md")
                    else "text/plain",
                    "content": content,
                }
            )
        return documents

    def evolve(
        self,
        *,
        session_id: str,
        request: dict[str, str],
        result: dict[str, Any],
        store: EvolutionStore,
        cancellation: HarnessCancellation | None = None,
    ) -> dict[str, Any]:
        if cancellation is not None:
            cancellation.raise_if_requested()
        config = store.project_config(request["project_id"])
        selected = selected_document_evolution(config)

        self.capture_session_dataset(
            session_id=session_id,
            request=request,
            result=result,
            store=store,
        )

        if not selected:
            return {"artifacts": [], "errors": []}
        prior_dataset_ids = [
            dataset["artifact_id"]
            for dataset in store.dataset_artifacts(request["project_id"])
            if dataset["session_id"] != session_id
        ]
        return self._run_selections(
            run_id=None,
            project_id=request["project_id"],
            project_name=request["project_name"],
            current_session_id=session_id,
            prior_dataset_ids=prior_dataset_ids,
            selections=selected,
            store=store,
            cancellation=cancellation,
            promote_outputs=True,
        )

    def capture_session_dataset(
        self,
        *,
        session_id: str,
        request: dict[str, str],
        result: dict[str, Any],
        store: EvolutionStore,
    ) -> dict[str, Any]:
        """Seal one completed Session transcript without running Evolution."""

        dataset_dir = self._artifact_root / "datasets" / session_id
        dataset_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        records_path = dataset_dir / "records.jsonl"
        record = {
            "event_id": f"{session_id}-completed",
            "task_id": session_id,
            "session_id": session_id,
            "status": "COMPLETED",
            "reward": 1.0,
            "traces": [
                {
                    "prompt_messages": [{"role": "user", "content": request["instruction"]}],
                    "response_messages": [{"role": "assistant", "content": result["response"]}],
                    "metadata": {
                        "capture_mode": "transcript",
                        "token_level_metrics_available": False,
                    },
                }
            ],
        }
        records_path.write_text(canonical_json(record) + "\n", encoding="utf-8")
        manifest_path = dataset_dir / "manifest.json"
        manifest_path.write_text(
            canonical_json(
                {
                    "dataset_id": f"dataset-{session_id}",
                    "name": f"{request['task_title']} transcript",
                    "records_path": records_path.name,
                    "records_uri": records_path.resolve().as_uri(),
                    "event_count": 1,
                    "capture_mode": "transcript",
                    "token_level_metrics_available": False,
                }
            ),
            encoding="utf-8",
        )

        dataset_input: dict[str, Any] = {
            "artifact_id": f"dataset-{session_id}",
            "type": "dataset",
            "uri": manifest_path.resolve().as_uri(),
            "name": f"{request['task_title']} transcript",
        }
        store.record_dataset_artifact(
            artifact_id=dataset_input["artifact_id"],
            project_id=request["project_id"],
            session_id=session_id,
            uri=dataset_input["uri"],
            name=dataset_input["name"],
            manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        )
        return dataset_input

    def seal_completed_session_datasets(
        self,
        store: EvolutionStore,
    ) -> list[str]:
        """Rebuild durable transcript datasets for completed current or legacy Sessions."""

        failures: list[str] = []
        for session in store.completed_sessions():
            try:
                self.capture_session_dataset(
                    session_id=session["session_id"],
                    request={
                        "project_id": session["project_id"],
                        "task_title": session["task_title"],
                        "instruction": session["instruction"],
                    },
                    result={"response": session["response"]},
                    store=store,
                )
            except Exception as exc:
                failures.append(f"{session['session_id']}: {exc}")
        return failures

    def evolve_run(
        self,
        *,
        run: dict[str, Any],
        store: EvolutionStore,
    ) -> dict[str, Any]:
        """Build unapplied candidates from an explicit, multi-Session evidence set."""

        session_ids = list(run["source_session_ids"])
        current_session_id = session_ids[-1]
        project = store.project(run["project_id"])
        return self._run_selections(
            run_id=run["run_id"],
            project_id=run["project_id"],
            project_name=project["display_name"],
            current_session_id=current_session_id,
            prior_dataset_ids=[f"dataset-{session_id}" for session_id in session_ids[:-1]],
            selections=run["selections"],
            store=store,
            cancellation=None,
            promote_outputs=False,
        )

    def _run_selections(
        self,
        *,
        run_id: str | None,
        project_id: str,
        project_name: str,
        current_session_id: str,
        prior_dataset_ids: list[str],
        selections: list[dict[str, Any]],
        store: EvolutionStore,
        cancellation: HarnessCancellation | None,
        promote_outputs: bool,
    ) -> dict[str, Any]:
        try:
            from openevo.evolution.framework.resolution import resolve_evolution_method
        except (ImportError, ModuleNotFoundError) as exc:
            raise EvolutionRunError(
                "OpenEvo Python dependencies are unavailable; run this daemon with `uv run python`"
            ) from exc

        persisted: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for item in selections:
            if cancellation is not None:
                cancellation.raise_if_requested()
            target_id = item["target_id"]
            requested_method_id = item["method"]
            method_id = resolve_evolution_method(
                target_id=target_id,
                requested_method=requested_method_id,
                prior_dataset_artifact_ids=prior_dataset_ids,
            )
            job_suffix = current_session_id if run_id is None else run_id
            job_id = f"job-{target_id.replace('_', '-')}-{job_suffix}"
            previous = store.latest_artifact(project_id, target_id)
            attempt = store.start_evolution_job(
                job_id=job_id,
                session_id=current_session_id,
                run_id=run_id,
                target_id=target_id,
                method_id=method_id,
                requested_method_id=requested_method_id,
                resolver_input_artifact_ids=prior_dataset_ids,
                previous_artifact_id=None if previous is None else previous["artifact_id"],
                config=item["config"],
            )
            try:
                job = store.get_evolution_job(job_id)
                persisted.extend(
                    self._execute_fixed_job(
                        job=job,
                        attempt=attempt,
                        request={
                            "project_id": project_id,
                            "project_name": project_name,
                        },
                        store=store,
                        cancellation=cancellation,
                        promote_outputs=promote_outputs,
                    )
                )
            except HarnessRunCancelled:
                raise
            except Exception as exc:
                errors.append(
                    {
                        "target_id": target_id,
                        "method": requested_method_id,
                        "message": str(exc),
                    }
                )
        return {"artifacts": persisted, "errors": errors}

    def retry(
        self,
        *,
        job: dict[str, Any],
        attempt: dict[str, Any],
        store: EvolutionStore,
    ) -> list[dict[str, Any]]:
        session = store.get_session(job["session_id"])
        project = store.project(session["project_id"])
        request = {
            "project_id": project["project_id"],
            "project_name": project["display_name"],
            "task_title": session["task_title"],
            "instruction": session["instruction"],
        }
        return self._execute_fixed_job(
            job=job,
            attempt=attempt,
            request=request,
            store=store,
            cancellation=None,
        )

    def _execute_fixed_job(
        self,
        *,
        job: dict[str, Any],
        attempt: dict[str, Any],
        request: dict[str, str],
        store: EvolutionStore,
        cancellation: HarnessCancellation | None,
        promote_outputs: bool | None = None,
    ) -> list[dict[str, Any]]:
        from openevo.evolution.framework.execution import (
            InputBindingSource,
            resolve_method_inputs,
        )
        from openevo.evolution.methods import METHOD_REGISTRY
        from openevo.evolution.models import WorkerClaimInputArtifact, WorkerClaimedJob

        job_id = job["job_id"]
        attempt_id = attempt["attempt_id"]
        attempt_ordinal = attempt["ordinal"]
        target_id = job["target_id"]
        method_id = job["method_id"]
        if promote_outputs is None:
            promote_outputs = job.get("run_id") is None
        stage = "input_resolution"
        try:
            if cancellation is not None:
                cancellation.raise_if_requested()
            target, method, handler = self._descriptor(target_id, method_id)
            method_config = self._method_config(method, job["config"])
            current_record = store.dataset_artifact(f"dataset-{job['session_id']}")
            current_dataset = WorkerClaimInputArtifact(
                artifact_id=current_record["artifact_id"],
                type="dataset",
                uri=current_record["uri"],
                name=current_record["name"],
            )
            prior_datasets = []
            for artifact_id in job["resolver_input_artifact_ids"]:
                record = store.dataset_artifact(artifact_id)
                prior_datasets.append(
                    WorkerClaimInputArtifact(
                        artifact_id=record["artifact_id"],
                        type="dataset",
                        uri=record["uri"],
                        name=record["name"],
                    )
                )
            previous = (
                None
                if job["previous_artifact_id"] is None
                else store.artifact(job["previous_artifact_id"])
            )
            previous_input = None
            if previous is not None:
                attempt_input_root = self._artifact_root / "attempt-inputs" / attempt_id
                attempt_input_root.mkdir(mode=0o700, parents=True, exist_ok=False)
                previous_uri = self._materialize_previous(
                    previous,
                    attempt_input_root / f"previous-{target_id}",
                )
                previous_input = WorkerClaimInputArtifact(
                    artifact_id=previous["artifact_id"],
                    type=target.artifact_type,
                    uri=previous_uri,
                    name=f"previous evolved {target.display_name}",
                )
            ordered_datasets = [*prior_datasets, current_dataset]
            candidates: dict[str, list[Any]] = {}
            for binding in method.input_bindings:
                if binding.source is InputBindingSource.CURRENT_DATASET:
                    candidates[binding.binding_id] = [current_dataset]
                elif binding.source is InputBindingSource.HISTORY_DATASETS:
                    candidates[binding.binding_id] = prior_datasets
                elif (
                    binding.source is InputBindingSource.EXPLICIT_INPUTS
                    and binding.artifact_type == "dataset"
                ):
                    candidates[binding.binding_id] = ordered_datasets
                elif binding.source is InputBindingSource.CURRENT_TARGET_ARTIFACTS:
                    candidates[binding.binding_id] = (
                        [] if previous_input is None else [previous_input]
                    )
                else:
                    candidates[binding.binding_id] = []
            resolved = resolve_method_inputs(method.input_bindings, candidates)
            store.update_evolution_attempt(
                attempt_id,
                stage="method_execution",
                message=f"Running {method_id} with the original fixed inputs.",
            )
            stage = "method_execution"
            method_handle = METHOD_REGISTRY.get(method_id)
            if method_handle is None:
                raise EvolutionRunError(f"{method_id} has no installed legacy worker handle")
            artifacts = method_handle(
                WorkerClaimedJob(
                    job_id=job_id,
                    lease_id=f"lease-{attempt_id}",
                    job_type="development_catalog",
                    method=method_id,
                    target_id=target_id,
                    registry_snapshot_digest=self._registry.registry_digest,
                    method_identity_digest=self._registry.identity_digest_for("method", method_id),
                    input_artifacts=list(resolved.input_artifacts),
                    config={
                        **method_config,
                        "name": f"{request['project_name']} evolved {target.display_name}",
                    },
                ),
                artifact_root=self._artifact_root,
            )
            if cancellation is not None:
                cancellation.raise_if_requested()
            stage = "output_validation"
            store.update_evolution_attempt(
                attempt_id,
                stage=stage,
                message="Validating the declared Evolution outputs.",
            )
            output_records: list[tuple[Any, str, list[dict[str, str]], str]] = []
            for output_index, artifact in enumerate(artifacts):
                artifact_type = artifact.type.value
                if artifact_type not in method.output_artifact_types:
                    raise EvolutionRunError(
                        f"{method_id} returned undeclared artifact type {artifact_type}"
                    )
                renderer_kind = (
                    "structured_summary"
                    if artifact_type == "report"
                    else handler.renderer_kind.value
                )
                documents = self._read_documents(artifact.uri, renderer_kind)
                output_suffix = "" if output_index == 0 else f"-{output_index + 1}"
                retry_suffix = "" if attempt_ordinal == 1 else f"-attempt-{attempt_ordinal}"
                identity_suffix = (
                    job["session_id"].removeprefix("dev-session-")
                    if job.get("run_id") is None
                    else job["run_id"].removeprefix("evolution-run-")
                )
                artifact_id = (
                    f"dev-{artifact_type.replace('_', '-')}-{identity_suffix}"
                    f"{retry_suffix}{output_suffix}"
                )
                output_records.append((artifact, artifact_type, documents, artifact_id))
            stage = "artifact_persistence"
            store.update_evolution_attempt(
                attempt_id,
                stage=stage,
                message="Persisting the validated Evolution artifacts.",
            )
            persisted: list[dict[str, Any]] = []
            artifact_ids: list[str] = []
            for artifact, artifact_type, documents, artifact_id in output_records:
                artifact_ids.append(artifact_id)
                persisted.append(
                    store.record_evolution_artifact(
                        artifact_id=artifact_id,
                        project_id=request["project_id"],
                        session_id=job["session_id"],
                        run_id=job.get("run_id"),
                        target_id=target_id,
                        artifact_type=artifact_type,
                        method_id=method_id,
                        renderer_kind=(
                            "structured_summary"
                            if artifact_type == "report"
                            else handler.renderer_kind.value
                        ),
                        documents=documents,
                        manifest=artifact.manifest,
                        previous_artifact_id=(
                            previous["artifact_id"]
                            if previous is not None and artifact_type == target.artifact_type
                            else None
                        ),
                        promoted=bool(artifact.promoted) and promote_outputs,
                    )
                )
            store.finish_evolution_job(
                job_id,
                attempt_id=attempt_id,
                artifact_ids=artifact_ids,
            )
            return persisted
        except HarnessRunCancelled:
            store.finish_evolution_job(
                job_id,
                attempt_id=attempt_id,
                error="Session cancelled by user",
                error_stage=stage,
                error_code="cancelled",
            )
            raise
        except Exception as exc:
            store.finish_evolution_job(
                job_id,
                attempt_id=attempt_id,
                error=str(exc),
                error_stage=stage,
                error_code=f"{stage}_failed",
            )
            raise
