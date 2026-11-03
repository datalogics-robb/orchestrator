"""The Windows code paths, exercised on any platform: they are pure functions of their inputs."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

from orchestrator import oscompat
from orchestrator.agents.hooks import claude_pretool
from orchestrator.agents.runners.claude_code import hook_command
from orchestrator.config.schema import ShareConfig, platform_key
from orchestrator.environment import build_env

MODERN_SHIM = r"""@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\node.exe" (
  SET "_prog=%dp0%\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\node_modules\@anthropic-ai\claude-code\cli.js" %*
"""

OLD_SHIM = r"""@IF EXIST "%~dp0\node.exe" (
  "%~dp0\node.exe"  "%~dp0\node_modules\@openai\codex\bin\codex.js" %*
) ELSE (
  @SETLOCAL
  @SET PATHEXT=%PATHEXT:;.JS;=;%
  node  "%~dp0\node_modules\@openai\codex\bin\codex.js" %*
)
"""


def _executable(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _npm_prefix(tmp_path: Path, shim_text: str, script: str) -> Path:
    prefix = tmp_path / "npm"
    _executable(prefix / script.replace("\\", "/"))
    return _executable(prefix / "tool.cmd", shim_text)


# --- finding programs ----------------------------------------------------------------


def test_posix_leaves_argv_alone(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX behaviour")
    assert oscompat.resolve_argv(["claude", "-p"], path="/nowhere", cwd=tmp_path) == ["claude", "-p"]


def test_npm_shim_runs_its_script_with_node_from_path(tmp_path: Path) -> None:
    shim = _npm_prefix(tmp_path, MODERN_SHIM, r"node_modules\@anthropic-ai\claude-code\cli.js")
    node = _executable(tmp_path / "nodejs" / "node")
    wrapped = oscompat.unwrap_npm_shim(shim, path=str(node.parent))
    assert wrapped == [str(node), str(shim.parent / "node_modules/@anthropic-ai/claude-code/cli.js")]


def test_npm_shim_prefers_the_node_beside_it(tmp_path: Path) -> None:
    shim = _npm_prefix(tmp_path, OLD_SHIM, r"node_modules\@openai\codex\bin\codex.js")
    bundled = _executable(shim.parent / "node.exe")
    wrapped = oscompat.unwrap_npm_shim(shim, path=None)
    assert wrapped and wrapped[0] == str(bundled) and wrapped[1].endswith("codex.js")


def test_npm_shim_without_node_or_target_is_not_unwrapped(tmp_path: Path) -> None:
    shim = _executable(tmp_path / "tool.cmd", MODERN_SHIM)
    assert oscompat.unwrap_npm_shim(shim, path=str(tmp_path)) is None  # the script is missing
    _executable(tmp_path / r"node_modules\@anthropic-ai\claude-code\cli.js".replace("\\", "/"))
    assert oscompat.unwrap_npm_shim(shim, path=str(tmp_path / "empty")) is None  # no node
    plain = _executable(tmp_path / "build.cmd", "@echo off\r\ncmake --build . %*\r\n")
    assert oscompat.unwrap_npm_shim(plain, path=None) is None


def test_windows_resolution_searches_the_child_path(tmp_path: Path) -> None:
    exe = _executable(tmp_path / "venv" / "Scripts" / "python.exe")
    argv = oscompat._resolve_windows(["python.exe", "-m", "pytest"], path=str(exe.parent), cwd=tmp_path)
    assert argv == [str(exe), "-m", "pytest"]
    # a relative program is relative to the child's cwd, not the orchestrator's
    local = _executable(tmp_path / "wt" / "tools" / "gen.exe")
    assert oscompat._resolve_windows(["tools/gen.exe"], path="", cwd=tmp_path / "wt")[0] == str(local)
    # an unknown program is left for CreateProcess to report
    assert oscompat._resolve_windows(["nope.exe"], path=str(tmp_path), cwd=tmp_path) == ["nope.exe"]


def test_windows_resolution_unwraps_npm_shims(tmp_path: Path) -> None:
    shim = _npm_prefix(tmp_path, MODERN_SHIM, r"node_modules\@anthropic-ai\claude-code\cli.js")
    node = _executable(shim.parent / "node.exe")
    schema = json.dumps({"description": "a & b | <c> 100%"})
    argv = oscompat._resolve_windows(
        ["tool.cmd", "--json-schema", schema], path=str(shim.parent), cwd=tmp_path
    )
    assert argv[0] == str(node) and argv[1].endswith("cli.js") and argv[2:] == ["--json-schema", schema]


def test_other_batch_files_refuse_arguments_cmd_would_reinterpret(tmp_path: Path) -> None:
    bat = _executable(tmp_path / "build.cmd", "@echo off\r\ncmake --build . %*\r\n")
    assert oscompat._resolve_windows(
        ["build.cmd", "--config", "Release"], path=str(tmp_path), cwd=tmp_path
    ) == [
        str(bat),
        "--config",
        "Release",
    ]
    for arg in ('{"a": 1}', "a&b", "50%", "x|y", "line\nbreak"):
        with pytest.raises(oscompat.BatchArgumentError):
            oscompat._resolve_windows(["build.cmd", arg], path=str(tmp_path), cwd=tmp_path)


# --- configuration and environment ------------------------------------------------------


def test_share_paths_have_a_windows_key() -> None:
    assert platform_key("win32") == "windows"
    assert platform_key("linux") == "linux" and platform_key("darwin") == "darwin"
    assert platform_key("freebsd14") is None
    share = ShareConfig.model_validate({"paths": {"linux": "/support", "windows": r"\\server\support"}})
    assert share.path_for("win32") == Path(r"\\server\support")
    assert ShareConfig.model_validate({"paths": {"linux": "/support"}}).path_for("win32") is None


def test_agent_env_keeps_what_windows_programs_need(monkeypatch: pytest.MonkeyPatch) -> None:
    base = {
        "PATH": "C:\\Windows\\System32",
        "SYSTEMROOT": "C:\\Windows",
        "USERPROFILE": "C:\\Users\\dev",
        "APPDATA": "C:\\Users\\dev\\AppData\\Roaming",
        "PROGRAMFILES(X86)": "C:\\Program Files (x86)",
        "GITHUB_TOKEN": "secret",
    }
    monkeypatch.setattr(sys, "platform", "win32")
    env = build_env(base)
    assert env["SYSTEMROOT"] == "C:\\Windows" and env["USERPROFILE"] == "C:\\Users\\dev"
    assert "PROGRAMFILES(X86)" in env and "APPDATA" in env
    assert "GITHUB_TOKEN" not in env
    monkeypatch.setattr(sys, "platform", "linux")
    assert "SYSTEMROOT" not in build_env(base)


# --- the Claude Code hook -----------------------------------------------------------------


def test_hook_command_quotes_paths_with_spaces(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    script = tmp_path / "my hooks" / "claude_pretool.py"
    rules = tmp_path / "run dir" / "hook-rules.json"
    posix = hook_command(script, rules)
    assert f"'{script}'" in posix and f"'{rules}'" in posix
    monkeypatch.setattr(sys, "platform", "win32")
    windows = hook_command(Path("C:/Users/A B/hook.py"), Path("C:/runs/rules.json"))
    assert windows.endswith('"C:/Users/A B/hook.py" "C:/runs/rules.json"')
    assert "\\" not in windows.split('" "', 1)[1]


def test_hook_treats_powershell_like_bash() -> None:
    rules = {"deny_commands": ["git push"], "write_patterns": [], "read_only_paths": []}
    payload = {"tool_name": "PowerShell", "tool_input": {"command": "git push origin HEAD"}}
    assert claude_pretool.decide(payload, rules)


def test_hook_sees_git_bash_spellings_of_a_windows_share(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_pretool.os, "name", "nt")
    assert claude_pretool._mentions("rm -rf /s/Support/x", "S:\\support")
    assert claude_pretool._mentions("cp a S:/Support/x", "S:\\support")
    assert claude_pretool._mentions("cp a //server/support/x", "\\\\server\\support")
    assert not claude_pretool._mentions("cp a /tmp/x", "S:\\support")


# --- text files are UTF-8 ------------------------------------------------------------------


def _text_io_without_encoding(tree: ast.AST) -> list[int]:
    """Lines that open, read, or write text without naming an encoding (Windows would use the code page)."""

    def binary(call: ast.Call, mode_index: int) -> bool:
        mode = next((kw.value for kw in call.keywords if kw.arg == "mode"), None)
        if mode is None and len(call.args) > mode_index:
            mode = call.args[mode_index]
        return isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value

    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or any(kw.arg == "encoding" for kw in node.keywords):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in ("read_text", "write_text"):
            lines.append(node.lineno)
        elif isinstance(f, ast.Name) and f.id == "open" and not binary(node, 1):
            lines.append(node.lineno)
        elif (
            isinstance(f, ast.Attribute)
            and f.attr == "open"
            and not (isinstance(f.value, ast.Name) and f.value.id == "os")
            and not binary(node, 0)
        ):
            lines.append(node.lineno)
        elif any(kw.arg == "text" for kw in node.keywords):
            lines.append(node.lineno)
    return lines


def test_package_text_io_names_its_encoding() -> None:
    package = Path(__file__).resolve().parents[1] / "orchestrator"
    offenders = [
        f"{path.relative_to(package.parent)}:{line}"
        for path in sorted(package.rglob("*.py"))
        for line in _text_io_without_encoding(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert not offenders, "add encoding='utf-8': " + ", ".join(offenders)
