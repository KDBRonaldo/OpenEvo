from __future__ import annotations

import json
from pathlib import Path
import threading
from types import SimpleNamespace
import urllib.request

from openevo.daemon import product_app


def test_product_composition_initializes_all_authoritative_services(
    tmp_path: Path,
    monkeypatch,
) -> None:
    observed: dict[str, object] = {}

    class Runner:
        def __init__(self, binary: str, timeout: int, model: str | None) -> None:
            observed["runner"] = (binary, timeout, model)

        def check_cli_ready(self) -> None:
            observed["runner_cli_ready"] = True

    class Store:
        def __init__(self, path: Path) -> None:
            observed["state_path"] = path

        def state_v2(self) -> SimpleNamespace:
            return SimpleNamespace(active_project_id=None)

    class Evolution:
        def __init__(self, **kwargs: object) -> None:
            observed["evolution"] = kwargs

        def check_ready(self) -> None:
            observed["evolution_ready"] = True

        def seal_completed_session_datasets(self, store: object) -> list[str]:
            observed["sealed_store"] = store
            return ["legacy evidence fixture"]

    class Models:
        def __init__(self, **kwargs: object) -> None:
            observed["models"] = kwargs

    class Server:
        def __init__(self, address, token, runner, store, evolution, models) -> None:
            observed["server"] = (address, token, runner, store, evolution, models)

    monkeypatch.setattr(product_app.shutil, "which", lambda _: "/usr/bin/codex")
    monkeypatch.setattr(product_app, "CodexRunner", Runner)
    monkeypatch.setattr(product_app, "DevelopmentStateStore", Store)
    monkeypatch.setattr(product_app, "DocumentEvolutionRunner", Evolution)
    monkeypatch.setattr(product_app, "HuggingFaceModelManager", Models)
    monkeypatch.setattr(product_app, "DevelopmentAgentServer", Server)

    composition = product_app.create_product_daemon(
        port=8787,
        token="t" * 32,
        codex_binary="codex",
        timeout_seconds=300,
        state_path=tmp_path / "state.sqlite3",
        model="codex-model",
        evolution_model="evolution-model",
    )

    assert observed["runner"] == ("/usr/bin/codex", 300, "codex-model")
    assert observed["runner_cli_ready"] is True
    assert observed["evolution_ready"] is True
    assert observed["state_path"] == (tmp_path / "state.sqlite3").resolve()
    assert observed["models"] == {
        "state_path": (tmp_path / "state.sqlite3").resolve(),
        "root": tmp_path / "models",
        "runtime_setup_script": None,
    }
    assert composition.evolution_model == "evolution-model"
    assert composition.evidence_failures == ("legacy evidence fixture",)


def test_product_composition_defers_model_runtime_and_forwards_setup_script(
    tmp_path: Path,
    monkeypatch,
) -> None:
    observed: dict[str, object] = {}

    class Runner:
        def __init__(self, *_args: object) -> None:
            pass

        def check_cli_ready(self) -> None:
            pass

    class Store:
        def __init__(self, _path: Path) -> None:
            pass

        def state_v2(self) -> SimpleNamespace:
            raise AssertionError("daemon startup must not inspect an active model project")

    class Evolution:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def check_ready(self) -> None:
            pass

        def seal_completed_session_datasets(self, _store: object) -> list[str]:
            return []

    class Server:
        def __init__(self, _address, _token, _runner, _store, _evolution, models) -> None:
            observed["models"] = models

        def warm_project_model(self, _config: object) -> None:
            raise AssertionError("daemon startup must not prewarm a historical model")

    class Models:
        def __init__(self, **kwargs: object) -> None:
            observed["model_args"] = kwargs

    monkeypatch.setattr(product_app.shutil, "which", lambda _: "/usr/bin/codex")
    monkeypatch.setattr(product_app, "CodexRunner", Runner)
    monkeypatch.setattr(product_app, "DevelopmentStateStore", Store)
    monkeypatch.setattr(product_app, "DocumentEvolutionRunner", Evolution)
    monkeypatch.setattr(product_app, "HuggingFaceModelManager", Models)
    monkeypatch.setattr(product_app, "DevelopmentAgentServer", Server)

    setup_script = tmp_path / "model-runtime-setup.sh"
    product_app.create_product_daemon(
        port=8787,
        token="t" * 32,
        codex_binary="codex",
        timeout_seconds=300,
        state_path=tmp_path / "state.sqlite3",
        model_runtime_setup_script=setup_script,
    )

    assert observed["models"] is not None
    assert observed["model_args"] == {
        "state_path": (tmp_path / "state.sqlite3").resolve(),
        "root": tmp_path / "models",
        "runtime_setup_script": setup_script,
    }


def test_product_daemon_exposes_public_loopback_health() -> None:
    server = product_app.DevelopmentAgentServer(
        ("127.0.0.1", 0),
        "t" * 32,
        object(),
        object(),
        None,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_address[1]}/health",
            timeout=5,
        ) as response:
            payload = json.loads(response.read())
        assert payload == {
            "schema_version": "1",
            "service": "openevo-daemon",
            "status": "ok",
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
