"""Harbor adapter that executes Core CodexHarness steps inside one task container."""

from __future__ import annotations

import json
from pathlib import Path
import shlex
from typing import Any

from harbor.agents.installed.codex import Codex as HarborCodexInstaller
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from openevo.runtime.base import RUNTIME_AGENT_LOG_DIR
from openevo_terminal_bench.core_codex import CoreCodexRun, build_core_codex_run
from openevo_terminal_bench.core_codex_payload import (
    CodexPayload,
    build_codex_install_command,
    remote_codex_payload_paths,
    resolve_codex_payload,
)


_MAX_STEP_DIAGNOSTIC_CHARS = 1024 * 1024
_MAX_FAILURE_DETAIL_CHARS = 2000


class _HarborRuntimeAdapter:
    def __init__(self, environment: BaseEnvironment, *, timeout_seconds: int | None) -> None:
        self._environment = environment
        self._timeout_seconds = timeout_seconds

    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> Any:
        return await self._environment.exec(
            command,
            cwd=cwd,
            env=env,
            timeout_sec=self._timeout_seconds,
        )


class OpenEvoCoreCodexAgent(HarborCodexInstaller):
    """Install Codex with Harbor, then run it only through Core CodexHarness."""

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        gateway_url: str | None = None,
        timeout_sec: int | str | None = None,
        reasoning_effort: str = "high",
        version: str | None = None,
        codex_binary_path: str | None = None,
        rg_binary_path: str | None = None,
        **kwargs: Any,
    ) -> None:
        if not model_name:
            raise ValueError("OpenEvo Core Codex Harbor agent requires model_name")
        if not gateway_url:
            raise ValueError("OpenEvo Core Codex Harbor agent requires gateway_url")
        super().__init__(
            logs_dir=logs_dir,
            model_name=model_name,
            version=version,
            **kwargs,
        )
        self._gateway_url = gateway_url
        self._timeout_seconds = int(timeout_sec) if timeout_sec not in (None, "") else None
        self._reasoning_effort = reasoning_effort
        self._codex_binary_path = codex_binary_path
        self._rg_binary_path = rg_binary_path
        self._codex_payload: CodexPayload | None = None
        self._core_run: CoreCodexRun | None = None

    @staticmethod
    def name() -> str:
        return "openevo-core-codex"

    async def install(self, environment: BaseEnvironment) -> None:
        if not self._version:
            raise ValueError("OpenEvo Core Codex Harbor agent requires a pinned version")
        payload = resolve_codex_payload(
            codex_binary_path=self._codex_binary_path,
            rg_binary_path=self._rg_binary_path,
        )
        remote_codex, remote_rg = remote_codex_payload_paths()
        await environment.upload_file(payload.codex_path, remote_codex)
        if payload.rg_path is not None:
            await environment.upload_file(payload.rg_path, remote_rg)
        await self.exec_as_root(
            environment,
            command=build_codex_install_command(payload, version=self._version),
            timeout_sec=self._timeout_seconds,
        )
        self._codex_payload = payload
        (self.logs_dir / "setup" / "codex-payload.json").write_text(
            json.dumps(
                {**payload.manifest(), "version": self._version},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    async def setup(self, environment: BaseEnvironment) -> None:
        await super().setup(environment)
        await environment.exec(
            f"mkdir -p {shlex.quote(RUNTIME_AGENT_LOG_DIR)}",
            timeout_sec=self._timeout_seconds,
        )

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / "instruction.txt").write_text(instruction, encoding="utf-8")
        core_run = build_core_codex_run(
            instruction=instruction,
            model=str(self.model_name),
            gateway_url=self._gateway_url,
            reasoning_effort=self._reasoning_effort,
        )
        self._core_run = core_run
        runtime = _HarborRuntimeAdapter(
            environment,
            timeout_seconds=self._timeout_seconds,
        )
        await core_run.harness.setup(runtime)
        return_code = 0
        try:
            for index, step in enumerate(core_run.steps):
                result = await environment.exec(
                    step.command,
                    cwd=step.cwd,
                    env=step.env,
                    timeout_sec=self._timeout_seconds,
                )
                _write_step_diagnostics(self.logs_dir, index, result)
                return_code = int(result.return_code)
                if return_code != 0:
                    detail = _exec_result_text(result, "stderr") or _exec_result_text(
                        result,
                        "stdout",
                    )
                    detail = detail[-_MAX_FAILURE_DETAIL_CHARS:].strip()
                    suffix = f": {detail}" if detail else ""
                    raise RuntimeError(
                        "OpenEvo Core CodexHarness step "
                        f"{index} failed with exit code {return_code}{suffix}"
                    )
        finally:
            try:
                await environment.download_file(
                    f"{RUNTIME_AGENT_LOG_DIR}/codex.txt",
                    self.logs_dir / "codex.txt",
                )
            except Exception:
                pass

        metadata = {
            "agent": self.name(),
            "capture_mode": "proxy",
            "gateway_url": self._gateway_url,
            "harness": "openevo.harness.presets.codex:CodexHarness",
            "model_name": self.model_name,
            "return_code": return_code,
            "codex_payload": (
                self._codex_payload.manifest()
                if self._codex_payload is not None
                else None
            ),
        }
        (self.logs_dir / "result.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if context.metadata is None:
            context.metadata = {}
        context.metadata["openevo_core_codex"] = metadata


def _exec_result_text(result: Any, field: str) -> str:
    value = getattr(result, field, "")
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _write_step_diagnostics(logs_dir: Path, index: int, result: Any) -> None:
    payload = {
        "return_code": int(result.return_code),
        "stderr": _exec_result_text(result, "stderr")[-_MAX_STEP_DIAGNOSTIC_CHARS:],
        "stdout": _exec_result_text(result, "stdout")[-_MAX_STEP_DIAGNOSTIC_CHARS:],
    }
    (logs_dir / f"core-step-{index}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


__all__ = ["OpenEvoCoreCodexAgent"]
