from __future__ import annotations

from pathlib import Path

import pytest

from openevo.science import ScienceProjectConfig, compile_science_project
from openevo.sidecar import (
    RemoteProfileConfig,
    plan_workspace_preparation,
)


def _profile() -> RemoteProfileConfig:
    return RemoteProfileConfig.model_validate(
        {
            "version": 1,
            "id": "lab-gpu",
            "host": "gpu.example.edu",
            "user": "alice",
        }
    )


def _project(
    source: dict[str, str],
    tmp_path: Path | None = None,
) -> ScienceProjectConfig:
    path = None if tmp_path is None else tmp_path / "science.yaml"
    if path is not None:
        path.write_text("version: 1\n", encoding="utf-8")
    return ScienceProjectConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "Protein Design"},
            "remote_profile": "lab-gpu",
            "task": {
                "id": "folding-baseline",
                "objective": "Improve the folding baseline.",
                "source": source,
            },
            "path": path,
        }
    )


def test_local_folder_plan_uploads_to_deterministic_remote_workspace(
    tmp_path: Path,
) -> None:
    project = _project(
        {"type": "local_folder", "path": "workflows/folding"},
        tmp_path=tmp_path,
    )

    plan = plan_workspace_preparation(project, _profile())

    assert plan.project_name == "Protein Design"
    assert plan.remote_profile_id == "lab-gpu"
    assert plan.workspace_root == "/home/alice/.openevo/workspaces"
    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action.type == "upload_dir"
    assert action.task_id == "folding-baseline"
    assert action.source == str(tmp_path / "workflows/folding")
    assert action.target.startswith(
        "/home/alice/.openevo/workspaces/protein-design/folding-baseline/"
    )
    assert action.source_fingerprint is not None
    assert action.source_fingerprint.startswith("sha256:")
    prepared = plan.to_prepared_workspaces()
    assert prepared["folding-baseline"].path == action.target
    assert (
        prepared["folding-baseline"].source_fingerprint
        == action.source_fingerprint
    )


def test_workspace_plan_actions_are_immutable_after_construction(
    tmp_path: Path,
) -> None:
    project = _project(
        {"type": "local_folder", "path": "workflows/folding"},
        tmp_path=tmp_path,
    )

    plan = plan_workspace_preparation(project, _profile())

    assert isinstance(plan.actions, tuple)
    with pytest.raises(AttributeError):
        plan.actions.append({"type": "use_remote_path"})
    dumped = plan.model_dump(mode="json")
    assert isinstance(dumped["actions"], list)


def test_git_repository_plan_records_clone_command() -> None:
    project = _project(
        {
            "type": "git_repository",
            "url": "https://github.com/example/research.git",
            "branch": "main",
        }
    )

    plan = plan_workspace_preparation(project, _profile())

    action = plan.actions[0]
    assert action.type == "git_clone"
    assert action.source == "https://github.com/example/research.git"
    assert action.branch == "main"
    assert action.command == (
        "git clone --depth 1 --branch main "
        "-- "
        "https://github.com/example/research.git "
        f"{action.target}"
    )
    assert action.target.startswith(
        "/home/alice/.openevo/workspaces/protein-design/folding-baseline/"
    )
    assert action.source_fingerprint is not None
    assert action.source_fingerprint.startswith("sha256:")


def test_git_repository_plan_omits_branch_command_segment_when_absent() -> None:
    project = _project(
        {
            "type": "git_repository",
            "url": "https://github.com/example/research.git",
        }
    )

    plan = plan_workspace_preparation(project, _profile())

    action = plan.actions[0]
    assert action.command == (
        "git clone --depth 1 "
        "-- "
        "https://github.com/example/research.git "
        f"{action.target}"
    )
    assert action.branch is None


def test_git_repository_plan_terminates_options_before_dash_prefixed_url() -> None:
    project = _project(
        {
            "type": "git_repository",
            "url": "--upload-pack=touch /tmp/pwned",
        }
    )

    plan = plan_workspace_preparation(project, _profile())

    action = plan.actions[0]
    assert action.command == (
        "git clone --depth 1 -- "
        "'--upload-pack=touch /tmp/pwned' "
        f"{action.target}"
    )


def test_remote_path_plan_uses_existing_remote_workspace() -> None:
    project = _project({"type": "remote_path", "path": "/datasets/folding"})

    plan = plan_workspace_preparation(project, _profile())

    assert len(plan.actions) == 1
    assert plan.actions[0].type == "use_remote_path"
    assert plan.actions[0].source == "/datasets/folding"
    assert plan.actions[0].target == "/datasets/folding"
    assert (
        plan.to_prepared_workspaces()["folding-baseline"].path
        == "/datasets/folding"
    )


def test_remote_path_plan_rejects_relative_remote_path() -> None:
    project = _project({"type": "remote_path", "path": "relative/path"})

    with pytest.raises(ValueError, match="absolute remote path"):
        plan_workspace_preparation(project, _profile())


def test_scratch_plan_has_no_actions_and_no_prepared_workspace() -> None:
    project = _project({"type": "scratch"})

    plan = plan_workspace_preparation(project, _profile())

    assert plan.actions == ()
    assert plan.to_prepared_workspaces() == {}


def test_workspace_plan_compiles_local_folder_science_project(
    tmp_path: Path,
) -> None:
    project = _project(
        {"type": "local_folder", "path": "workflows/folding"},
        tmp_path=tmp_path,
    )
    plan = plan_workspace_preparation(project, _profile())

    compiled = compile_science_project(
        project,
        prepared_workspaces=plan.to_prepared_workspaces(),
    )

    assert compiled.tasks[0].workspace == plan.actions[0].target
    assert compiled.tasks[0].metadata["openevo"][
        "source_fingerprint"
    ].startswith("sha256:")
