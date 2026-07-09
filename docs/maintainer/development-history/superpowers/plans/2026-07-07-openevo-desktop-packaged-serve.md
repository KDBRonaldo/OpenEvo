# OpenEvo Desktop Packaged Serve Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `openevo desktop serve`, a release-shaped single-process local server that serves the OpenEvo Desktop SPA and the existing sidecar API.

**Architecture:** Add a focused `openevo.desktop` package that locates packaged static assets and appends Desktop static routes to an existing sidecar FastAPI app. Refactor CLI sidecar serve app construction into a shared helper, then add a `desktop serve` command that wraps the same sidecar app with static routes.

**Tech Stack:** Python 3.11, FastAPI/Starlette `StaticFiles` and `FileResponse`, argparse CLI, setuptools package data, pytest `TestClient`.

---

## File Structure

- Create `src/openevo/desktop/__init__.py`: public exports for Desktop serve helpers.
- Create `src/openevo/desktop/app.py`: static root discovery, validation, FastAPI static route attachment, packaged Desktop app creation.
- Create `src/openevo/desktop/web/index.html`: small packaged fallback asset used by editable installs and tests when release assets have not been copied from `web/dist`.
- Create `src/openevo/desktop/web/assets/openevo-desktop.css`: small packaged fallback stylesheet.
- Create `tests/openevo/desktop/test_app.py`: static asset discovery, missing asset failure, route behavior.
- Modify `src/openevo/cli.py`: import Desktop helpers, add `desktop serve`, share sidecar app construction with `sidecar serve`.
- Modify `tests/openevo/test_cli.py`: Desktop CLI tests and pyproject package-data regression.
- Modify `pyproject.toml`: include `openevo/desktop/web/**/*` as package data.
- Modify `docs/architecture/openevo-desktop-science-foundation.md`: document packaged local serve entrypoint.
- Sync `web/dist/` into `src/openevo/desktop/web/` after `npm run build`: packaged default UI assets.

## Task 1: Desktop Static App Helper

**Files:**
- Create: `src/openevo/desktop/__init__.py`
- Create: `src/openevo/desktop/app.py`
- Create: `src/openevo/desktop/web/index.html`
- Create: `src/openevo/desktop/web/assets/openevo-desktop.css`
- Test: `tests/openevo/desktop/test_app.py`

- [ ] **Step 1: Write the failing app tests**

Add `tests/openevo/desktop/test_app.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openevo.desktop import (
    DesktopStaticAssetsMissingError,
    create_desktop_app,
    packaged_desktop_static_root,
    resolve_desktop_static_root,
)
from openevo.sidecar import create_sidecar_app


def _static_root(tmp_path: Path) -> Path:
    root = tmp_path / "desktop-web"
    assets = root / "assets"
    assets.mkdir(parents=True)
    (root / "index.html").write_text(
        "<!doctype html><title>OpenEvo Test Desktop</title><div id='root'></div>",
        encoding="utf-8",
    )
    (assets / "app.js").write_text("window.__openevoTest = true;", encoding="utf-8")
    return root


def test_resolve_desktop_static_root_accepts_override(tmp_path: Path) -> None:
    root = _static_root(tmp_path)

    assert resolve_desktop_static_root(root) == root


def test_resolve_desktop_static_root_reports_missing_index(tmp_path: Path) -> None:
    with pytest.raises(DesktopStaticAssetsMissingError) as exc_info:
        resolve_desktop_static_root(tmp_path / "missing")

    assert "OpenEvo Desktop static assets were not found" in str(exc_info.value)
    assert "--static-root" in str(exc_info.value)


def test_packaged_desktop_static_root_points_at_bundled_assets() -> None:
    root = packaged_desktop_static_root()

    assert root.name == "web"
    assert (root / "index.html").is_file()


def test_create_desktop_app_serves_spa_and_sidecar_api(tmp_path: Path) -> None:
    app = create_desktop_app(create_sidecar_app(), static_root=_static_root(tmp_path))
    client = TestClient(app)

    shell_response = client.get("/openevo-api/desktop/shell")
    assert shell_response.status_code == 200
    assert shell_response.json()["execution"]["mode"] == "codex_subscription_transcript"

    index_response = client.get("/openevo")
    assert index_response.status_code == 200
    assert "OpenEvo Test Desktop" in index_response.text

    nested_response = client.get("/openevo/projects/folding-baseline")
    assert nested_response.status_code == 200
    assert "OpenEvo Test Desktop" in nested_response.text

    asset_response = client.get("/assets/app.js")
    assert asset_response.status_code == 200
    assert "window.__openevoTest" in asset_response.text


def test_create_desktop_app_redirects_root_to_openevo(tmp_path: Path) -> None:
    app = create_desktop_app(create_sidecar_app(), static_root=_static_root(tmp_path))
    client = TestClient(app, follow_redirects=False)

    response = client.get("/")

    assert response.status_code == 307
    assert response.headers["location"] == "/openevo"
```

- [ ] **Step 2: Run the failing app tests**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/desktop/test_app.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'openevo.desktop'`.

- [ ] **Step 3: Implement the Desktop helper package**

Create `src/openevo/desktop/__init__.py`:

```python
"""OpenEvo Desktop packaged app helpers."""

from __future__ import annotations

from openevo.desktop.app import (
    DesktopStaticAssetsMissingError,
    create_desktop_app,
    packaged_desktop_static_root,
    resolve_desktop_static_root,
)

__all__ = [
    "DesktopStaticAssetsMissingError",
    "create_desktop_app",
    "packaged_desktop_static_root",
    "resolve_desktop_static_root",
]
```

Create `src/openevo/desktop/app.py`:

```python
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles


class DesktopStaticAssetsMissingError(FileNotFoundError):
    """Raised when packaged OpenEvo Desktop web assets are unavailable."""


def packaged_desktop_static_root() -> Path:
    return Path(__file__).with_name("web")


def resolve_desktop_static_root(static_root: Path | str | None = None) -> Path:
    root = Path(static_root).expanduser() if static_root is not None else packaged_desktop_static_root()
    index_path = root / "index.html"
    if not index_path.is_file():
        raise DesktopStaticAssetsMissingError(
            "OpenEvo Desktop static assets were not found. Run `cd web && npm run "
            "build` and copy `web/dist` into `src/openevo/desktop/web` before "
            "packaging, or pass `--static-root`."
        )
    return root


def create_desktop_app(
    sidecar_app: FastAPI,
    *,
    static_root: Path | str | None = None,
) -> FastAPI:
    root = resolve_desktop_static_root(static_root)
    index_path = root / "index.html"
    assets_path = root / "assets"

    if assets_path.is_dir():
        sidecar_app.mount(
            "/assets",
            StaticFiles(directory=str(assets_path)),
            name="openevo-assets",
        )

    @sidecar_app.get("/", include_in_schema=False)
    def redirect_to_desktop() -> RedirectResponse:
        return RedirectResponse("/openevo")

    @sidecar_app.get("/openevo", include_in_schema=False)
    def serve_desktop_index() -> FileResponse:
        return FileResponse(index_path)

    @sidecar_app.get("/openevo/{path:path}", include_in_schema=False)
    def serve_desktop_spa(path: str) -> FileResponse:
        candidate = root / path
        if candidate.is_file() and candidate.resolve().is_relative_to(root.resolve()):
            return FileResponse(candidate)
        return FileResponse(index_path)

    return sidecar_app
```

Create `src/openevo/desktop/web/index.html`:

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>OpenEvo Desktop</title>
    <link rel="stylesheet" href="/assets/openevo-desktop.css" />
  </head>
  <body>
    <main>
      <h1>OpenEvo Desktop</h1>
      <p>Packaged Desktop assets are available.</p>
    </main>
  </body>
</html>
```

Create `src/openevo/desktop/web/assets/openevo-desktop.css`:

```css
body {
  margin: 0;
  font-family: system-ui, sans-serif;
  background: #f8fafc;
  color: #0f172a;
}

main {
  max-width: 48rem;
  margin: 4rem auto;
  padding: 0 1.5rem;
}
```

- [ ] **Step 4: Run the app tests again**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/desktop/test_app.py -q
```

Expected: all tests in `tests/openevo/desktop/test_app.py` pass.

- [ ] **Step 5: Commit Task 1**

Run:

```bash
git add src/openevo/desktop tests/openevo/desktop/test_app.py
GIT_AUTHOR_NAME='ivowang' GIT_AUTHOR_EMAIL='ziyiwang@ieee.org' GIT_COMMITTER_NAME='ivowang' GIT_COMMITTER_EMAIL='ziyiwang@ieee.org' git commit -m "feat: add packaged desktop app wrapper"
```

## Task 2: Desktop Serve CLI

**Files:**
- Modify: `src/openevo/cli.py`
- Modify: `tests/openevo/test_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Append these tests near the existing `sidecar serve` tests in `tests/openevo/test_cli.py`:

```python
def _desktop_static_root(tmp_path: Path) -> Path:
    root = tmp_path / "desktop-web"
    assets = root / "assets"
    assets.mkdir(parents=True)
    (root / "index.html").write_text(
        "<!doctype html><title>OpenEvo CLI Desktop</title><div id='root'></div>",
        encoding="utf-8",
    )
    (assets / "app.js").write_text("window.__openevoCliTest = true;", encoding="utf-8")
    return root


def test_cli_desktop_serve_invokes_runner_with_wrapped_app(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    calls = []

    def fake_runner(app, *, host: str, port: int) -> None:
        calls.append((app, host, port))

    monkeypatch.setattr("openevo.cli._run_sidecar_server", fake_runner)

    exit_code = main(
        [
            "desktop",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            "3766",
            "--static-root",
            str(_desktop_static_root(tmp_path)),
        ]
    )

    assert exit_code == 0
    assert calls[0][1:] == ("127.0.0.1", 3766)
    assert calls[0][0].title == "OpenEvo Desktop Sidecar"
    assert "http://127.0.0.1:3766/openevo" in capsys.readouterr().err


def test_cli_desktop_serve_passes_desktop_config_root_and_static_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app_calls = []
    desktop_calls = []

    class FakeApp:
        title = "OpenEvo Desktop Sidecar"

    def fake_create_sidecar_app(**kwargs):
        app_calls.append(kwargs)
        return FakeApp()

    def fake_create_desktop_app(app, *, static_root=None):
        desktop_calls.append((app, static_root))
        return app

    monkeypatch.setattr("openevo.cli.create_sidecar_app", fake_create_sidecar_app)
    monkeypatch.setattr("openevo.cli.create_desktop_app", fake_create_desktop_app)
    monkeypatch.setattr("openevo.cli._run_sidecar_server", lambda app, *, host, port: None)

    exit_code = main(
        [
            "desktop",
            "serve",
            "--desktop-config-root",
            str(tmp_path / "configs"),
            "--static-root",
            str(tmp_path / "web"),
        ]
    )

    assert exit_code == 0
    assert app_calls[0]["config_root"] == tmp_path / "configs"
    assert app_calls[0]["transport_factory"] is not None
    assert desktop_calls == [(desktop_calls[0][0], tmp_path / "web")]


def test_cli_desktop_serve_loads_config_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app_calls = []
    desktop_calls = []

    class FakeApp:
        title = "OpenEvo Desktop Sidecar"

    def fake_create_project_app(project, profile, *, transport_factory):
        app_calls.append((project, profile, transport_factory))
        return FakeApp()

    def fake_create_desktop_app(app, *, static_root=None):
        desktop_calls.append((app, static_root))
        return app

    monkeypatch.setattr("openevo.cli.create_sidecar_app_for_project", fake_create_project_app)
    monkeypatch.setattr("openevo.cli.create_desktop_app", fake_create_desktop_app)
    monkeypatch.setattr("openevo.cli._run_sidecar_server", lambda app, *, host, port: None)
    science_path = _write_config(tmp_path / "science.yaml", _minimal_science_payload())
    profile_path = _write_config(
        tmp_path / "remote.yaml",
        {
            "version": 1,
            "id": "science-team",
            "host": "gpu.example.edu",
            "user": "alice",
        },
    )

    exit_code = main(
        [
            "desktop",
            "serve",
            "--config",
            str(science_path),
            "--remote-profile",
            str(profile_path),
            "--static-root",
            str(tmp_path / "web"),
        ]
    )

    assert exit_code == 0
    project, profile, transport_factory = app_calls[0]
    assert project.task.id == "folding-baseline"
    assert profile.id == "science-team"
    assert transport_factory(profile).__class__.__name__ == "_CliDryRunTransport"
    assert desktop_calls[0][1] == tmp_path / "web"


def test_cli_desktop_serve_requires_config_and_profile_together(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    monkeypatch.setattr("openevo.cli._run_sidecar_server", lambda app, *, host, port: None)
    science_path = _write_config(tmp_path / "science.yaml", _minimal_science_payload())

    exit_code = main(["desktop", "serve", "--config", str(science_path)])

    assert exit_code == 1
    assert "desktop serve --config and --remote-profile must be used together" in (
        capsys.readouterr().err
    )
```

- [ ] **Step 2: Run the failing CLI tests**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py -q
```

Expected: the new Desktop tests fail because argparse rejects `desktop`.

- [ ] **Step 3: Implement CLI parsing and handlers**

Modify `src/openevo/cli.py`:

```python
from openevo.desktop import create_desktop_app
```

Add a helper after the sidecar parser setup in `build_parser()`:

```python
    desktop_parser = subparsers.add_parser(
        "desktop",
        help="Run the packaged OpenEvo Desktop app.",
    )
    desktop_subparsers = desktop_parser.add_subparsers(
        dest="desktop_command",
        required=True,
    )
    desktop_serve_parser = desktop_subparsers.add_parser(
        "serve",
        help="Serve the packaged OpenEvo Desktop UI and local sidecar API.",
    )
    _add_desktop_serve_arguments(desktop_serve_parser)
```

Extract sidecar serve arguments from the existing `serve_parser` block into:

```python
def _add_desktop_serve_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    parser.add_argument("--port", type=int, default=3766, help="Port to bind.")
    parser.add_argument(
        "--config",
        help="Optional Science Project YAML used to derive Desktop shell status.",
    )
    parser.add_argument(
        "--remote-profile",
        help="Optional remote profile YAML used with --config.",
    )
    parser.add_argument(
        "--transport",
        choices=("dry-run", "ssh"),
        default="dry-run",
        help="Remote executor transport used by sidecar mutating endpoints.",
    )
    parser.add_argument(
        "--desktop-config-root",
        help=(
            "Writable local directory for Desktop-created Science Project and "
            "remote profile configs."
        ),
    )
```

Add only this extra argument to `desktop_serve_parser` after calling the helper:

```python
    desktop_serve_parser.add_argument(
        "--static-root",
        help="Override the packaged OpenEvo Desktop static asset directory.",
    )
```

Update `main()`:

```python
        if args.command == "desktop":
            return _handle_desktop(args)
```

Add handlers:

```python
def _handle_desktop(args: argparse.Namespace) -> int:
    if args.desktop_command == "serve":
        return _handle_desktop_serve(args)
    raise ValueError(f"Unknown desktop command: {args.desktop_command}")


def _handle_desktop_serve(args: argparse.Namespace) -> int:
    app = _build_sidecar_serve_app(args, command_name="desktop serve")
    static_root = Path(args.static_root).expanduser() if args.static_root else None
    app = create_desktop_app(app, static_root=static_root)
    print(f"OpenEvo Desktop: http://{args.host}:{args.port}/openevo", file=sys.stderr)
    _run_sidecar_server(app, host=args.host, port=args.port)
    return 0
```

Refactor `_handle_sidecar_serve()` to:

```python
def _handle_sidecar_serve(args: argparse.Namespace) -> int:
    app = _build_sidecar_serve_app(args, command_name="sidecar serve")
    _run_sidecar_server(app, host=args.host, port=args.port)
    return 0
```

Add shared helper:

```python
def _build_sidecar_serve_app(args: argparse.Namespace, *, command_name: str):
    if bool(args.config) != bool(args.remote_profile):
        raise ValueError(
            f"{command_name} --config and --remote-profile must be used together"
        )
    if args.config and args.remote_profile:
        project = load_science_project_config(Path(args.config))
        profile = load_remote_profile_config(Path(args.remote_profile))
        return create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=_sidecar_transport_factory(args.transport),
        )
    config_root = (
        Path(args.desktop_config_root).expanduser()
        if args.desktop_config_root
        else _default_desktop_config_root()
    )
    return create_sidecar_app(
        config_root=config_root,
        transport_factory=_sidecar_transport_factory(args.transport),
    )
```

- [ ] **Step 4: Run the CLI tests again**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py -q
```

Expected: all CLI tests pass.

- [ ] **Step 5: Commit Task 2**

Run:

```bash
git add src/openevo/cli.py tests/openevo/test_cli.py
GIT_AUTHOR_NAME='ivowang' GIT_AUTHOR_EMAIL='ziyiwang@ieee.org' GIT_COMMITTER_NAME='ivowang' GIT_COMMITTER_EMAIL='ziyiwang@ieee.org' git commit -m "feat: add desktop serve cli"
```

## Task 3: Package Data and Documentation

**Files:**
- Modify: `pyproject.toml`
- Modify: `tests/openevo/test_cli.py`
- Modify: `docs/architecture/openevo-desktop-science-foundation.md`

- [ ] **Step 1: Write the failing package-data test**

Add imports to `tests/openevo/test_cli.py`:

```python
import tomllib
```

Append:

```python
def test_pyproject_packages_openevo_desktop_web_assets() -> None:
    payload = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    package_data = payload["tool"]["setuptools"]["package-data"]

    assert "openevo" in package_data
    assert "desktop/web/**/*" in package_data["openevo"]
```

- [ ] **Step 2: Run the failing package-data test**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py::test_pyproject_packages_openevo_desktop_web_assets -q
```

Expected: fail because `openevo` is absent from `[tool.setuptools.package-data]`.

- [ ] **Step 3: Add package data**

Modify `pyproject.toml`:

```toml
[tool.setuptools.package-data]
openevo = [
    "desktop/web/**/*",
]
polar = [
    "**/README.md",
    "platform/web/dist/**/*",
]
slime_bridge = ["README.md"]
```

- [ ] **Step 4: Document packaged Desktop serve**

In `docs/architecture/openevo-desktop-science-foundation.md`, replace the start of the "Local Sidecar API" subsection with:

```markdown
### Local Desktop Serve

The release-shaped local entrypoint is:

```bash
openevo desktop serve --host 127.0.0.1 --port 3766
```

This starts one local FastAPI/uvicorn process. The same process serves the
packaged Desktop SPA at `/openevo` and the sidecar API at `/openevo-api/*`.
The root path `/` redirects to `/openevo`.

For development or custom packages, `--static-root` can point at a Vite build
output directory:

```bash
openevo desktop serve --static-root web/dist
```

If static assets are missing, the command fails before starting the API server
and tells the caller to build and package Desktop assets or pass
`--static-root`.

`openevo sidecar serve --host 127.0.0.1 --port 3766` remains available as an
API-only entrypoint for integration tests and power users.

For a user project, Desktop can start the packaged server with local config
paths:

```bash
openevo desktop serve \
  --config science.yaml \
  --remote-profile remote.yaml \
  --host 127.0.0.1 \
  --port 3766
```
```

Keep the remainder of the existing subsection beginning with "Desktop-created projects can start from a no-config sidecar."

- [ ] **Step 5: Refresh packaged Desktop web assets**

Run:

```bash
cd web && npm run build
```

Expected: Vite build exits 0.

Run:

```bash
rsync -a --delete web/dist/ src/openevo/desktop/web/
```

Expected: `src/openevo/desktop/web/index.html` and `src/openevo/desktop/web/assets/*` match the current Vite build output.

- [ ] **Step 6: Run the package-data test and docs-free focused tests**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py::test_pyproject_packages_openevo_desktop_web_assets -q
```

Expected: package-data test passes.

- [ ] **Step 7: Commit Task 3**

Run:

```bash
git add pyproject.toml tests/openevo/test_cli.py docs/architecture/openevo-desktop-science-foundation.md web/index.html src/openevo/desktop/web docs/superpowers/specs/2026-07-07-openevo-desktop-packaged-serve-design.md docs/superpowers/plans/2026-07-07-openevo-desktop-packaged-serve.md
GIT_AUTHOR_NAME='ivowang' GIT_AUTHOR_EMAIL='ziyiwang@ieee.org' GIT_COMMITTER_NAME='ivowang' GIT_COMMITTER_EMAIL='ziyiwang@ieee.org' git commit -m "chore: package desktop web assets"
```

## Task 4: Focused Verification and Branch Review

**Files:**
- Review all changed files.

- [ ] **Step 1: Run focused Python tests**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py tests/openevo/desktop/test_app.py tests/openevo/sidecar -q
```

Expected: tests pass.

- [ ] **Step 2: Run lint for changed Python files**

Run:

```bash
/home/ziyi/ProRL-Agent-Server/.venv/bin/ruff check src/openevo/desktop src/openevo/cli.py tests/openevo/desktop tests/openevo/test_cli.py
```

Expected: `All checks passed!`

- [ ] **Step 3: Run web tests and build**

Run:

```bash
cd web && npm test -- --run
```

Expected: all Vitest tests pass.

Run:

```bash
cd web && npm run build
```

Expected: Vite build exits 0.

- [ ] **Step 4: Run package-data smoke check**

Run:

```bash
/home/ziyi/ProRL-Agent-Server/.venv/bin/python -m build --wheel
```

Expected: wheel build exits 0. If the build output path is printed, inspect the wheel file list with:

```bash
/home/ziyi/ProRL-Agent-Server/.venv/bin/python - <<'PY'
from pathlib import Path
from zipfile import ZipFile

wheel = sorted(Path("dist").glob("*.whl"))[-1]
with ZipFile(wheel) as zf:
    names = set(zf.namelist())
print("\n".join(name for name in sorted(names) if "openevo/desktop/web" in name))
assert "openevo/desktop/web/index.html" in names
PY
```

Expected: output lists `openevo/desktop/web/index.html` and the assertion passes.

- [ ] **Step 5: Run diff checks and review**

Run:

```bash
git status --short
git diff --check openevo/stable...HEAD
git diff openevo/stable...HEAD
```

Expected: status shows only intended branch changes, diff check has no output, and manual diff review confirms public CLI behavior, package data, docs, and route ordering match #63.

- [ ] **Step 6: Push branch and open PR**

Run:

```bash
git push -u openevo codex/openevo-desktop-packaged-serve
```

Open a draft PR against `stable` with body containing:

```markdown
Resolves #63

## Summary
- add packaged `openevo.desktop` static asset discovery and FastAPI wrapper
- add `openevo desktop serve` for one-process UI + sidecar serving
- package Desktop web assets and document the local entrypoint

## Tests
- `PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py tests/openevo/desktop/test_app.py tests/openevo/sidecar -q`
- `/home/ziyi/ProRL-Agent-Server/.venv/bin/ruff check src/openevo/desktop src/openevo/cli.py tests/openevo/desktop tests/openevo/test_cli.py`
- `cd web && npm test -- --run`
- `cd web && npm run build`
- `/home/ziyi/ProRL-Agent-Server/.venv/bin/python -m build --wheel`
- `git diff --check openevo/stable...HEAD`

## Docs
- `docs/architecture/openevo-desktop-science-foundation.md`
- `docs/superpowers/specs/2026-07-07-openevo-desktop-packaged-serve-design.md`
- `docs/superpowers/plans/2026-07-07-openevo-desktop-packaged-serve.md`
```

## Self-Review

- Spec coverage: Task 1 covers static root discovery, missing asset errors, static routes, root redirect, and sidecar API preservation. Task 2 covers `openevo desktop serve`, config-backed options, `--static-root`, and runner integration. Task 3 covers package data and architecture docs. Task 4 covers verification and PR linkage.
- Placeholder scan: no step uses unfilled placeholders or unspecified tests.
- Type consistency: `DesktopStaticAssetsMissingError`, `packaged_desktop_static_root`, `resolve_desktop_static_root`, and `create_desktop_app` are defined in Task 1 and reused consistently in later tasks.
