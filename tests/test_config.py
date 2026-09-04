from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from orchestrator.config.loader import ConfigError, SecretError, load_config, scan_for_secrets
from orchestrator.config.schema import Config


def test_loads_and_splits_commands(config_dict: dict, tmp_path: Path) -> None:
    cfg = Config.model_validate(config_dict)
    assert cfg.build.commands == [["python3", "-c", "pass"]]
    assert cfg.shares["raid"].path_for("darwin") == Path(config_dict["shares"]["raid"]["paths"]["darwin"])
    assert cfg.runs_dir == tmp_path / "state" / "runs"


def test_unknown_key_is_error(config_dict: dict) -> None:
    config_dict["repo"]["typo"] = 1
    with pytest.raises(ValidationError):
        Config.model_validate(config_dict)


def test_auth_needs_exactly_one_source(config_dict: dict) -> None:
    config_dict["repo"]["auth"] = {"token_env": "A", "netrc_machine": "b"}
    with pytest.raises(Exception, match="exactly one"):
        Config.model_validate(config_dict)


def test_undeclared_share_rejected(config_dict: dict) -> None:
    config_dict["agents"]["worker"]["shares"]["nope"] = "read"
    with pytest.raises(Exception, match="not declared"):
        Config.model_validate(config_dict)


def test_read_write_share_needs_workspace_write(config_dict: dict) -> None:
    config_dict["agents"]["reviewer"]["shares"]["raid"] = "read-write"
    with pytest.raises(Exception, match="workspace-write"):
        Config.model_validate(config_dict)


def test_secret_literal_rejected(config_dict: dict, tmp_path: Path) -> None:
    config_dict["repo"]["branch_template"] = "ghp_abcdefghijklmnopqrstuvwxyz0123"
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(config_dict))
    with pytest.raises(SecretError):
        load_config(p)


def test_scan_reports_path() -> None:
    hits = scan_for_secrets({"a": {"b": ["x", "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"]}})
    assert hits == ["a.b[1]"]


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "none.yaml")


def test_deny_tools_accepts_list_form(config_dict: dict) -> None:
    config_dict["mcp"] = {"deny_tools": [{"jenkins": ["triggerBuild"]}, {"jenkins": ["replayBuild"]}]}
    cfg = Config.model_validate(config_dict)
    assert cfg.mcp.deny_tools == {"jenkins": ["triggerBuild", "replayBuild"]}
