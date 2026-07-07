# OpenEvo Desktop Project Config Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let OpenEvo Desktop validate and persist a local Science Project plus Remote Profile from the UI so ordinary users do not need to hand-write `science.yaml` and `remote.yaml`.

**Architecture:** Add a small sidecar config-draft contract that converts a Desktop form payload into existing `ScienceProjectConfig` and `RemoteProfileConfig` models, then writes deterministic YAML files under a Desktop config root. Expose it through a mutation-token protected local sidecar endpoint that can turn a fixture sidecar into a config-backed session in-process. Wire a compact setup form into the `/openevo` route and keep secrets reference-only.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, PyYAML, existing OpenEvo Science/Sidecar models, React 19, Vite, Vitest, happy-dom.

---

### Task 1: Config Draft Contract

**Files:**
- Create: `src/openevo/sidecar/config.py`
- Modify: `src/openevo/sidecar/__init__.py`
- Test: `tests/openevo/sidecar/test_config.py`

- [ ] **Step 1: Write failing config tests**

Create `tests/openevo/sidecar/test_config.py` with tests for:

- `DesktopProjectConfigDraft.model_validate(valid_payload)` builds a subscription Science project and remote profile.
- `save_desktop_project_config()` writes deterministic YAML files under `<root>/projects/<project-slug>/science.yaml` and `<root>/profiles/<profile-id>.yaml`.
- extra raw secret fields such as `password` are rejected by Pydantic.
- invalid source combinations such as `source_type="git_repository"` without `source_url` fail validation.

Use this valid payload in tests:

```python
VALID_DRAFT = {
    "project_name": "Protein Design",
    "task_id": "folding-baseline",
    "objective": "Improve the folding baseline.",
    "source_type": "remote_path",
    "source_path": "/datasets/folding-baseline",
    "remote_profile_id": "science-team",
    "remote_host": "gpu.example.edu",
    "remote_port": 22,
    "remote_user": "alice",
    "auth_method": "ssh_agent",
    "https_proxy": "http://127.0.0.1:7890",
    "huggingface_endpoint": "https://hf-mirror.com",
    "codex_model": "gpt-5.1-codex-mini",
    "text_memory": True,
    "skill_bundle": True,
    "agent_system": True,
}
```

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/sidecar/test_config.py -q
```

Expected: FAIL because `openevo.sidecar.config` does not exist.

- [ ] **Step 2: Implement config draft models**

Create `src/openevo/sidecar/config.py` with:

- `DesktopProjectConfigDraft` strict frozen Pydantic model.
- `DesktopProjectConfigPaths` strict frozen model with `science_config_path` and `remote_profile_path`.
- `build_desktop_project_configs(draft) -> tuple[ScienceProjectConfig, RemoteProfileConfig]`.
- `save_desktop_project_config(draft, config_root) -> tuple[ScienceProjectConfig, RemoteProfileConfig, DesktopProjectConfigPaths]`.

The draft fields are:

```python
project_name: str
task_id: str
objective: str
source_type: Literal["local_folder", "git_repository", "remote_path", "scratch"] = "remote_path"
source_path: str | None = None
source_url: str | None = None
source_branch: str | None = None
remote_profile_id: str = "default"
remote_host: str
remote_port: int = Field(default=22, ge=1, le=65535)
remote_user: str
auth_method: Literal["ssh_agent", "private_key", "password_ref"] = "ssh_agent"
private_key_path: str | None = None
password_ref: str | None = None
passphrase_ref: str | None = None
workspace_root: str | None = None
http_proxy: str | None = None
https_proxy: str | None = None
no_proxy: str | None = None
pip_index_url: str | None = None
huggingface_endpoint: str | None = None
hf_home: str | None = None
codex_model: str = "gpt-5.1-codex-mini"
text_memory: bool = True
skill_bundle: bool = True
agent_system: bool = True
```

Validation rules:

- strip all string fields and reject empty strings;
- reject `source_path`, `source_url`, or `source_branch` fields that do not match the selected source type by delegating to `ScienceProjectConfig`;
- reject raw secret extras through `extra="forbid"`;
- rely on `RemoteProfileConfig` for auth-mode compatibility.

Write YAML using `yaml.safe_dump(model.model_dump(mode="json", exclude_none=True, exclude={"path"}), sort_keys=True)`.

Export `DesktopProjectConfigDraft`, `DesktopProjectConfigPaths`, `build_desktop_project_configs`, and `save_desktop_project_config` from `src/openevo/sidecar/__init__.py`.

- [ ] **Step 3: Verify green**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/sidecar/test_config.py -q
/home/ziyi/ProRL-Agent-Server/.venv/bin/ruff check src/openevo/sidecar/config.py tests/openevo/sidecar/test_config.py
```

Expected: tests pass and ruff reports `All checks passed!`.

- [ ] **Step 4: Commit**

```bash
git add src/openevo/sidecar/config.py src/openevo/sidecar/__init__.py tests/openevo/sidecar/test_config.py
GIT_AUTHOR_NAME='ivowang' GIT_AUTHOR_EMAIL='ziyiwang@ieee.org' GIT_COMMITTER_NAME='ivowang' GIT_COMMITTER_EMAIL='ziyiwang@ieee.org' git commit -m "feat: add desktop project config draft contract"
```

### Task 2: Sidecar Config Endpoint

**Files:**
- Modify: `src/openevo/sidecar/api.py`
- Modify: `src/openevo/cli.py`
- Modify: `tests/openevo/sidecar/test_api.py`
- Modify: `tests/openevo/test_cli.py`

- [ ] **Step 1: Write failing API and CLI tests**

Add sidecar API tests:

- `POST /openevo-api/desktop/project-config` without token returns 403.
- valid draft on a no-config app saves files, returns paths and refreshed status, and makes the session config-backed so `/workspace` no longer returns the no-session 409.
- invalid draft with empty `remote_host` returns 422 and does not write files.
- payload with raw `password` extra returns 422.

Use `create_sidecar_app(config_root=tmp_path, transport_factory=lambda _profile: _ApiDryRunTransport())` for the valid no-config test.

Add CLI tests:

- `openevo sidecar serve --desktop-config-root <tmp>` passes `config_root` into `create_sidecar_app`.
- `openevo sidecar serve --transport ssh` without config is allowed and passes a lazy SSH transport factory to `create_sidecar_app`, because the project-config endpoint can create the session later.

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/sidecar/test_api.py tests/openevo/test_cli.py -q
```

Expected: FAIL because the endpoint, app args, and CLI flag do not exist.

- [ ] **Step 2: Implement endpoint and app session mutation**

In `src/openevo/sidecar/api.py`:

- import `Path`, `DesktopProjectConfigDraft`, `DesktopProjectConfigPaths`, and `save_desktop_project_config`;
- add `OpenEvoDesktopProjectConfigResponse` with `config: DesktopProjectConfigPaths` and `status: OpenEvoDesktopShellStatus`;
- extend `create_sidecar_app()` with keyword-only args:

```python
config_root: Path | None = None
transport_factory: Callable[[RemoteProfileConfig], RemoteExecutorTransport] | None = None
```

- add `POST /openevo-api/desktop/project-config`, validate token first, require both `config_root` and `transport_factory` or return 409 `"Desktop project config requires a writable config root and transport factory."`;
- call `save_desktop_project_config()`;
- create a new `OpenEvoSidecarSession(project, profile, transport_factory, build_desktop_shell_status(project, profile))`;
- assign it with `nonlocal session`;
- return saved paths and status with mutation token.

Keep existing `/shell`, `/workspace`, `/bootstrap`, and `/run` semantics unchanged.

In `src/openevo/cli.py`:

- add `--desktop-config-root` to `sidecar serve`;
- when no `--config/--remote-profile` is given, call `create_sidecar_app(config_root=Path(args.desktop_config_root).expanduser() if set else _default_desktop_config_root(), transport_factory=_sidecar_transport_factory(args.transport))`;
- remove the rejection of `--transport ssh` without config;
- add `_default_desktop_config_root() -> Path` using `OPENEVO_DESKTOP_CONFIG_DIR` or `Path.home() / ".openevo" / "desktop"`.

- [ ] **Step 3: Verify green**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/sidecar/test_api.py tests/openevo/test_cli.py -q
/home/ziyi/ProRL-Agent-Server/.venv/bin/ruff check src/openevo/sidecar src/openevo/cli.py tests/openevo/sidecar tests/openevo/test_cli.py
```

Expected: tests pass and ruff reports `All checks passed!`.

- [ ] **Step 4: Commit**

```bash
git add src/openevo/sidecar/api.py src/openevo/cli.py tests/openevo/sidecar/test_api.py tests/openevo/test_cli.py
GIT_AUTHOR_NAME='ivowang' GIT_AUTHOR_EMAIL='ziyiwang@ieee.org' GIT_COMMITTER_NAME='ivowang' GIT_COMMITTER_EMAIL='ziyiwang@ieee.org' git commit -m "feat: add desktop project config endpoint"
```

### Task 3: Web Setup Form

**Files:**
- Modify: `web/src/api/openevo.ts`
- Modify: `web/src/api/openevo.test.ts`
- Modify: `web/src/routes/OpenEvoDesktop.tsx`
- Modify: `web/src/routes/OpenEvoDesktop.test.tsx`

- [ ] **Step 1: Write failing web tests**

In `web/src/api/openevo.test.ts`, add a test that:

- fetches shell to remember the mutation token;
- calls `saveOpenEvoProjectConfig(validDraft)`;
- asserts the request goes to `/openevo-api/desktop/project-config`;
- asserts `X-OpenEvo-Sidecar-Token` is sent;
- asserts returned `config.science_config_path` and `status.project.name`.

In `web/src/routes/OpenEvoDesktop.test.tsx`, add tests that:

- render sidecar-loaded fixture state and find editable fields for Project, Task ID, Objective, Host, User, HTTPS proxy, and Codex model;
- click `Save Config`, assert pending text, mock success, and assert visible project/host update;
- mock rejection and assert the error is rendered.

Run:

```bash
cd web && npm test -- --run
```

Expected: FAIL because the client function and form behavior do not exist.

- [ ] **Step 2: Implement web client**

In `web/src/api/openevo.ts`, add:

```ts
export interface OpenEvoProjectConfigDraft {
  project_name: string;
  task_id: string;
  objective: string;
  source_type: "local_folder" | "git_repository" | "remote_path" | "scratch";
  source_path?: string | null;
  source_url?: string | null;
  source_branch?: string | null;
  remote_profile_id: string;
  remote_host: string;
  remote_port: number;
  remote_user: string;
  auth_method: "ssh_agent" | "private_key" | "password_ref";
  private_key_path?: string | null;
  password_ref?: string | null;
  passphrase_ref?: string | null;
  workspace_root?: string | null;
  http_proxy?: string | null;
  https_proxy?: string | null;
  no_proxy?: string | null;
  pip_index_url?: string | null;
  huggingface_endpoint?: string | null;
  hf_home?: string | null;
  codex_model: string;
  text_memory: boolean;
  skill_bundle: boolean;
  agent_system: boolean;
}
```

Add `OpenEvoProjectConfigResponsePayload`, `OpenEvoProjectConfigResponse`, and `saveOpenEvoProjectConfig(draft)` posting to `/openevo-api/desktop/project-config` with the remembered mutation token and returning typed paths plus `toOpenEvoDesktopShellModel(payload.status)`.

- [ ] **Step 3: Implement route form**

In `OpenEvoDesktop.tsx`:

- add controlled `configDraft` state initialized from the current model;
- update draft fields after shell fetch succeeds;
- add `handleSaveConfig()` with `configSaving` and `configError`;
- render a compact `Project Setup` panel before `Science Project`;
- use existing `CommandButton` for `Save Config`;
- disable workspace/bootstrap/run while `configSaving` is true;
- on success set `model=response.status` and `sidecarConnected=true`.

Keep the form focused: project/task/objective, source type and path/url, remote host/user/port, HTTPS proxy, Hugging Face endpoint, Codex model, and evolution checkboxes.

- [ ] **Step 4: Verify web**

Run:

```bash
cd web && npm test -- --run
cd web && npm run build
cd web && npm audit --omit=dev --audit-level=low
```

Expected: tests/build pass and production audit reports 0 vulnerabilities.

- [ ] **Step 5: Commit**

```bash
git add web/src/api/openevo.ts web/src/api/openevo.test.ts web/src/routes/OpenEvoDesktop.tsx web/src/routes/OpenEvoDesktop.test.tsx
GIT_AUTHOR_NAME='ivowang' GIT_AUTHOR_EMAIL='ziyiwang@ieee.org' GIT_COMMITTER_NAME='ivowang' GIT_COMMITTER_EMAIL='ziyiwang@ieee.org' git commit -m "feat: add openevo desktop setup form"
```

### Task 4: Docs, Verification, Review, PR

**Files:**
- Modify: `docs/architecture/openevo-desktop-science-foundation.md`

- [ ] **Step 1: Document config flow**

Update the Local Sidecar API section with:

- `POST /openevo-api/desktop/project-config`;
- mutation token requirement;
- saved file locations under Desktop config root;
- secret-reference-only boundary;
- no vault/process restart/Electron packaging in this slice;
- note that saving config creates a config-backed session in the current sidecar process.

- [ ] **Step 2: Full verification**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py tests/openevo/sidecar tests/openevo/remote tests/openevo/science tests/evolution/test_models.py -q
/home/ziyi/ProRL-Agent-Server/.venv/bin/ruff check src/openevo/sidecar src/openevo/cli.py tests/openevo/sidecar tests/openevo/test_cli.py
cd web && npm test -- --run
cd web && npm run build
cd web && npm audit --omit=dev --audit-level=low
git diff --check openevo/stable...HEAD
```

- [ ] **Step 3: HTTP smoke**

Start a no-config dry-run sidecar with `OPENEVO_DESKTOP_CONFIG_DIR=<tmp>`, then:

- `GET /openevo-api/desktop/shell` to read the token;
- `POST /openevo-api/desktop/project-config` with a valid draft;
- assert config files exist;
- `POST /workspace`;
- `POST /bootstrap`;
- `POST /run`;
- assert run report ready and refreshed shell status.

- [ ] **Step 4: Commit docs**

```bash
git add docs/architecture/openevo-desktop-science-foundation.md
GIT_AUTHOR_NAME='ivowang' GIT_AUTHOR_EMAIL='ziyiwang@ieee.org' GIT_COMMITTER_NAME='ivowang' GIT_COMMITTER_EMAIL='ziyiwang@ieee.org' git commit -m "docs: document openevo desktop config flow"
```

- [ ] **Step 5: gpt-5.5 high-effort review**

Request a read-only review for `openevo/stable...HEAD`. Fix any Critical or Important findings, rerun affected tests, and re-review fixes.

- [ ] **Step 6: Push, PR, CI, merge**

Push `codex/openevo-desktop-project-config`, create a PR with `Fixes #59`, wait for checks, squash merge, delete the remote branch, and fetch/prune `openevo/stable`.

---

## Self-Review

- Spec coverage: Issue #59 acceptance criteria map to Tasks 1-4.
- Placeholder scan: no TBD/TODO placeholders remain.
- Type consistency: Python `DesktopProjectConfigDraft` maps directly to TypeScript `OpenEvoProjectConfigDraft`; response paths are `science_config_path` and `remote_profile_path`.
- Scope control: no vault, Electron packaging, background process supervisor, raw secret storage, or local inference lifecycle is introduced in this slice.
