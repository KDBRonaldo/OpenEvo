from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from desktop.sidecar import (
    ProxySettings,
    RemoteProfileConfig,
    SSHAuthConfig,
    load_remote_profile_config,
)


def _minimal_payload() -> dict:
    return {
        "version": 1,
        "id": "research-a100",
        "host": "gpu.example.com",
        "user": "ubuntu",
    }


def _write_yaml(path: Path, payload: dict) -> Path:
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def test_defaults_to_ssh_agent_and_user_workspace_root() -> None:
    config = RemoteProfileConfig.model_validate(_minimal_payload())

    assert config.auth == SSHAuthConfig()
    assert config.proxy == ProxySettings()
    assert config.port == 22
    assert config.min_home_available_kb == 20_000_000
    assert config.workspace_root is None
    assert config.effective_workspace_root == "/home/ubuntu/.openevo/workspaces"


def test_profile_strings_are_trimmed_and_empty_strings_rejected() -> None:
    config = RemoteProfileConfig.model_validate(
        _minimal_payload()
        | {
            "id": "  research-a100  ",
            "name": "  Lab A100  ",
            "host": "  gpu.example.com  ",
            "user": "  ubuntu  ",
            "workspace_root": "  /srv/openevo/workspaces  ",
        }
    )

    assert config.id == "research-a100"
    assert config.name == "Lab A100"
    assert config.host == "gpu.example.com"
    assert config.user == "ubuntu"
    assert config.workspace_root == "/srv/openevo/workspaces"
    assert config.effective_workspace_root == "/srv/openevo/workspaces"

    with pytest.raises(ValidationError, match="id"):
        RemoteProfileConfig.model_validate(_minimal_payload() | {"id": "   "})

    with pytest.raises(ValidationError, match="name"):
        RemoteProfileConfig.model_validate(_minimal_payload() | {"name": "   "})


def test_profile_validates_remote_path_and_numeric_bounds() -> None:
    with pytest.raises(ValidationError, match="workspace_root"):
        RemoteProfileConfig.model_validate(
            _minimal_payload() | {"workspace_root": "relative/workspaces"}
        )

    with pytest.raises(ValidationError, match="less than or equal to 65535"):
        RemoteProfileConfig.model_validate(_minimal_payload() | {"port": 65536})

    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        RemoteProfileConfig.model_validate(_minimal_payload() | {"min_home_available_kb": -1})


def test_models_are_strict_frozen_and_forbid_extra_fields() -> None:
    config = RemoteProfileConfig.model_validate(_minimal_payload())

    with pytest.raises(ValidationError, match="Input should be a valid integer"):
        RemoteProfileConfig.model_validate(_minimal_payload() | {"port": "22"})

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RemoteProfileConfig.model_validate(_minimal_payload() | {"extra": True})

    with pytest.raises(ValidationError, match="frozen"):
        config.port = 2200


def test_raw_secret_fields_are_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SSHAuthConfig.model_validate({"method": "private_key", "private_key": "raw"})

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SSHAuthConfig.model_validate({"method": "password_ref", "password": "raw"})


def test_auth_and_proxy_authority_values_never_enter_repr_or_validation_errors() -> None:
    key_canary = "/private/SECRET_KEY_PATH_CANARY"
    password_canary = "SECRET_PASSWORD_REFERENCE_CANARY"
    proxy_canary = "http://proxy-user:SECRET_PROXY_CANARY@example.test"
    auth = SSHAuthConfig.model_validate(
        {
            "method": "private_key",
            "private_key_path": key_canary,
            "passphrase_ref": password_canary,
        }
    )
    proxy = ProxySettings.model_validate(
        {"https_proxy": proxy_canary, "extra_env": {"SECRET_ENV": password_canary}}
    )

    rendered = repr((auth, proxy))
    assert key_canary not in rendered
    assert password_canary not in rendered
    assert proxy_canary not in rendered

    with pytest.raises(ValidationError) as exc_info:
        SSHAuthConfig.model_validate({"method": "ssh_agent", "password_ref": password_canary})
    assert password_canary not in str(exc_info.value)
    assert password_canary not in repr(exc_info.value)


def test_ssh_agent_auth_forbids_secret_references() -> None:
    with pytest.raises(ValidationError, match="ssh_agent"):
        SSHAuthConfig.model_validate(
            {"method": "ssh_agent", "private_key_path": "/home/ubuntu/.ssh/id_ed25519"}
        )

    with pytest.raises(ValidationError, match="ssh_agent"):
        SSHAuthConfig.model_validate({"method": "ssh_agent", "password_ref": "secret/password"})

    with pytest.raises(ValidationError, match="ssh_agent"):
        SSHAuthConfig.model_validate(
            {"method": "ssh_agent", "passphrase_ref": "secret/passphrase"}
        )


def test_private_key_auth_requires_path_and_allows_passphrase_ref() -> None:
    with pytest.raises(ValidationError, match="private_key_path"):
        SSHAuthConfig.model_validate({"method": "private_key"})

    auth = SSHAuthConfig.model_validate(
        {
            "method": "private_key",
            "private_key_path": "  /home/ubuntu/.ssh/id_ed25519  ",
            "passphrase_ref": "  secret/passphrase  ",
        }
    )

    assert auth.private_key_path == "/home/ubuntu/.ssh/id_ed25519"
    assert auth.passphrase_ref == "secret/passphrase"

    with pytest.raises(ValidationError, match="password_ref"):
        SSHAuthConfig.model_validate(
            {
                "method": "private_key",
                "private_key_path": "/home/ubuntu/.ssh/id_ed25519",
                "password_ref": "secret/password",
            }
        )


def test_password_ref_auth_requires_reference() -> None:
    with pytest.raises(ValidationError, match="password_ref"):
        SSHAuthConfig.model_validate({"method": "password_ref"})

    auth = SSHAuthConfig.model_validate(
        {"method": "password_ref", "password_ref": "  secret/password  "}
    )

    assert auth.password_ref == "secret/password"

    with pytest.raises(ValidationError, match="private_key_path"):
        SSHAuthConfig.model_validate(
            {
                "method": "password_ref",
                "password_ref": "secret/password",
                "private_key_path": "/home/ubuntu/.ssh/id_ed25519",
            }
        )

    with pytest.raises(ValidationError, match="passphrase_ref"):
        SSHAuthConfig.model_validate(
            {
                "method": "password_ref",
                "password_ref": "secret/password",
                "passphrase_ref": "secret/passphrase",
            }
        )


def test_proxy_env_rendering_includes_proxy_hf_and_pip_vars() -> None:
    proxy = ProxySettings.model_validate(
        {
            "http_proxy": "  http://proxy.local:3128  ",
            "https_proxy": "https://secure-proxy.local:3128",
            "no_proxy": "localhost,127.0.0.1",
            "pip_index_url": "https://pypi.example/simple",
            "huggingface_endpoint": "https://hf.example",
            "hf_home": "/data/hf",
            "docker_registry_mirror": "https://registry-mirror.example",
            "extra_env": {
                "HTTP_PROXY": "http://old.example:3128",
                "CUSTOM_ENV": "enabled",
            },
        }
    )

    assert proxy.http_proxy == "http://proxy.local:3128"
    assert proxy.docker_registry_mirror == "https://registry-mirror.example"
    assert proxy.to_env() == {
        "CUSTOM_ENV": "enabled",
        "HTTP_PROXY": "http://proxy.local:3128",
        "http_proxy": "http://proxy.local:3128",
        "HTTPS_PROXY": "https://secure-proxy.local:3128",
        "https_proxy": "https://secure-proxy.local:3128",
        "NO_PROXY": "localhost,127.0.0.1",
        "no_proxy": "localhost,127.0.0.1",
        "PIP_INDEX_URL": "https://pypi.example/simple",
        "HF_ENDPOINT": "https://hf.example",
        "HF_HOME": "/data/hf",
    }


def test_proxy_rejects_empty_string_values() -> None:
    with pytest.raises(ValidationError, match="http_proxy"):
        ProxySettings.model_validate({"http_proxy": "   "})

    with pytest.raises(ValidationError, match="extra_env"):
        ProxySettings.model_validate({"extra_env": {"EMPTY": ""}})


def test_load_remote_profile_config_reads_yaml_and_sets_path(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path / "remote-profile.yaml",
        _minimal_payload()
        | {
            "name": "A100 Pool",
            "auth": {
                "method": "private_key",
                "private_key_path": "/home/ubuntu/.ssh/id_ed25519",
            },
            "proxy": {"pip_index_url": "https://pypi.example/simple"},
        },
    )

    config = load_remote_profile_config(path)

    assert config.path == path
    assert config.name == "A100 Pool"
    assert config.auth.method == "private_key"
    assert config.proxy.to_env()["PIP_INDEX_URL"] == "https://pypi.example/simple"


def test_load_remote_profile_config_requires_file_and_mapping(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="not found"):
        load_remote_profile_config(tmp_path / "missing.yaml")

    with pytest.raises(ValueError, match="not a file"):
        load_remote_profile_config(tmp_path)

    path = tmp_path / "remote-profile.yaml"
    path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    with pytest.raises(ValueError, match="top-level mapping"):
        load_remote_profile_config(path)


def test_load_remote_profile_config_rejects_falsy_non_mapping_yaml(
    tmp_path: Path,
) -> None:
    path = tmp_path / "remote-profile.yaml"
    path.write_text("false\n", encoding="utf-8")

    with pytest.raises(ValueError, match="top-level mapping"):
        load_remote_profile_config(path)


@pytest.mark.parametrize("content", ["null\n", ""])
def test_load_remote_profile_config_rejects_none_yaml(
    tmp_path: Path,
    content: str,
) -> None:
    path = tmp_path / "remote-profile.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="top-level mapping"):
        load_remote_profile_config(path)
