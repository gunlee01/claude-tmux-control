"""Claude Code launch command and environment helpers."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
from pathlib import Path
from typing import Callable, Mapping, Sequence


CLAUDE_OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
CLAUDE_DANGEROUS_SKIP_PERMISSIONS_FLAG = "--dangerously-skip-permissions"
CLAUDE_LAUNCH_COMMANDS = {"start", "launch", "chat"}
CLAUDE_EXECUTABLE = "claude"
DEFAULT_ENV_FILE_NAME = ".ctc.env"
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RESERVED_ENV_NAMES = {CLAUDE_OAUTH_TOKEN_ENV}


def _normalize_claude_args_option_values(argv: Sequence[str]) -> list[str]:
    values = list(argv)
    normalized: list[str] = []
    index = 0
    while index < len(values):
        value = values[index]
        if value == "--claude-args" and index + 1 < len(values):
            normalized.append(f"--claude-args={values[index + 1]}")
            index += 2
            continue
        normalized.append(value)
        index += 1
    return normalized


def add_claude_launch_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", help="Claude model for newly launched Claude Code sessions")
    parser.add_argument(
        "--claude-args",
        dest="claude_args_string",
        help="trusted Claude Code CLI arguments, parsed without shell execution",
    )


def add_environment_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--env",
        dest="env_names",
        action="append",
        default=[],
        metavar="NAME",
        help="copy a named environment variable from the ctc process into a newly created tmux session",
    )
    parser.add_argument(
        "--env-file",
        dest="env_files",
        action="append",
        type=Path,
        default=[],
        metavar="PATH",
        help=f"read environment variables from PATH for newly created tmux sessions; defaults to cwd/{DEFAULT_ENV_FILE_NAME}",
    )


def claude_args_from_options(args: argparse.Namespace) -> list[str]:
    try:
        values = shlex.split(getattr(args, "claude_args_string", None) or "")
    except ValueError as error:
        raise ValueError("invalid_claude_args") from error
    model = getattr(args, "model", None)
    if model and _has_model_option(values):
        raise ValueError("duplicate_model")
    if model:
        values.extend(["--model", model])
    return values


def build_claude_command(claude_args: Sequence[str] = ()) -> str:
    args = list(claude_args)
    if not _has_permission_override(args):
        args.append(CLAUDE_DANGEROUS_SKIP_PERMISSIONS_FLAG)
    return _shell_join([CLAUDE_EXECUTABLE, *args])


def build_initial_claude_command(
    claude_args: Sequence[str],
    session_id: str,
    resume: bool,
    prompt: str | None = None,
) -> str:
    session_flag = "--resume" if resume else "--session-id"
    args = [*claude_args, session_flag, session_id]
    if not _has_permission_override(claude_args):
        args.append(CLAUDE_DANGEROUS_SKIP_PERMISSIONS_FLAG)
    command = _shell_join([CLAUDE_EXECUTABLE, *args])
    if prompt is not None:
        command += " -- " + _shell_ansi_c_quote(prompt)
    return command


def claude_environment_from_args(
    args: argparse.Namespace,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    env = os.environ if environ is None else environ
    result: dict[str, str] = {}

    for env_file in _env_files_from_args(args):
        result.update(read_env_file(env_file))

    for name in getattr(args, "env_names", []) or []:
        _validate_env_name(name)
        if name in RESERVED_ENV_NAMES:
            raise ValueError(f"reserved_env: {name}")
        if name not in env:
            raise ValueError(f"missing_env: {name}")
        result[name] = env[name]

    source_env = getattr(args, "oauth_token_env", None)
    if not source_env:
        return result
    token = env.get(source_env)
    if not token:
        return result
    result[CLAUDE_OAUTH_TOKEN_ENV] = token
    return result


def preseed_claude_project_trust(
    cwd: Path,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> None:
    trusted_dir = str(cwd.expanduser().resolve())
    home_dir = home.expanduser() if home is not None else Path.home()
    config_dir_value = (env or {}).get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_CONFIG_DIR")
    config_dir = Path(config_dir_value).expanduser() if config_dir_value else home_dir / ".claude"
    config_dir.mkdir(parents=True, exist_ok=True)

    global_config_path = home_dir / ".claude.json"
    global_config = _read_json_object(global_config_path)
    projects = global_config.get("projects")
    if not isinstance(projects, dict):
        projects = {}
    project = projects.get(trusted_dir)
    if not isinstance(project, dict):
        project = {}
    if not isinstance(project.get("allowedTools"), list):
        project["allowedTools"] = []
    project["hasTrustDialogAccepted"] = True
    project["hasCompletedProjectOnboarding"] = True
    project["projectOnboardingSeenCount"] = max(int(project.get("projectOnboardingSeenCount") or 0), 4)
    projects[trusted_dir] = project
    global_config["hasCompletedOnboarding"] = True
    global_config["projects"] = projects
    _write_json_object(global_config_path, global_config)

    settings_path = config_dir / "settings.json"
    settings = _read_json_object(settings_path)
    settings["skipDangerousModePermissionPrompt"] = True
    _write_json_object(settings_path, settings)


def _env_files_from_args(args: argparse.Namespace) -> list[Path]:
    explicit = list(getattr(args, "env_files", []) or [])
    if explicit:
        return [path.expanduser() for path in explicit]

    cwd = getattr(args, "cwd", None)
    if cwd is None:
        return []
    default_path = Path(cwd).expanduser().resolve() / DEFAULT_ENV_FILE_NAME
    return [default_path] if default_path.exists() else []


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValueError(f"invalid_env_file: {path}") from error

    for index, original_line in enumerate(lines, start=1):
        line = original_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise ValueError(f"invalid_env_file: {path}:{index}")
        name, value = line.split("=", 1)
        name = name.strip()
        if not ENV_NAME_PATTERN.fullmatch(name):
            raise ValueError(f"invalid_env_file: {path}:{index}")
        if name in RESERVED_ENV_NAMES:
            raise ValueError(f"reserved_env: {name}")
        values[name] = _unquote_env_value(value.strip())
    return values


def _shell_join(args: Sequence[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


def _shell_ansi_c_quote(value: str) -> str:
    if "\0" in value:
        raise ValueError("prompt_contains_nul")
    replacements = {
        "\\": "\\\\",
        "'": "\\'",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
    }
    return "$'" + "".join(replacements.get(char, char) for char in value) + "'"


def check_runtime_dependencies(
    args: argparse.Namespace,
    which: Callable[[str], str | None] = shutil.which,
) -> str | None:
    if not which("tmux"):
        return "\n".join(
            [
                "tmux not found in PATH.",
                "Install tmux first, then retry.",
                "Example: sudo yum install -y tmux",
            ]
        )

    if args.command_name not in CLAUDE_LAUNCH_COMMANDS and not (
        args.command_name in {"stream", "ask"} and getattr(args, "cwd", None)
    ):
        return None

    if not which(CLAUDE_EXECUTABLE):
        return "\n".join(
            [
                f"Claude Code executable not found in PATH: {CLAUDE_EXECUTABLE}",
                "Install Claude Code CLI first, then retry.",
                "Example: curl -fsSL https://claude.ai/install.sh | bash",
                "After install, confirm with: claude --version",
            ]
        )
    return None


def _read_json_object(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_object(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _validate_env_name(name: str) -> None:
    if not ENV_NAME_PATTERN.fullmatch(name):
        raise ValueError(f"invalid_env: {name}")


def _unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _has_permission_override(claude_args: Sequence[str]) -> bool:
    return any(
        arg == CLAUDE_DANGEROUS_SKIP_PERMISSIONS_FLAG
        or arg == "--permission-mode"
        or arg.startswith("--permission-mode=")
        for arg in claude_args
    )


def _has_model_option(claude_args: Sequence[str]) -> bool:
    return any(arg == "--model" or arg.startswith("--model=") for arg in claude_args)
