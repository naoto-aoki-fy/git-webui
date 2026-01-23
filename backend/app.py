import argparse
import asyncio
import json
import os
import re
import shlex
import tempfile
import secrets
import tomllib
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from pathlib import PureWindowsPath
from typing import Dict, List, Optional
import traceback

from aiohttp import web

CONFIG_PATH = Path(os.environ.get("GIT_WEBUI_CONFIG", "config.toml"))
DEFAULT_BIND = "0.0.0.0"
DEFAULT_PORT = 8080
MAX_LINE_SIZE = 32 * 1024

DEVNULL = "NUL" if os.name == "nt" else "/dev/null"
KEEP_TEMP = os.environ.get("GIT_WEBUI_KEEP_TEMP", "").lower() in {"1", "true", "yes", "on"}
REPO_ROOT = Path(os.environ.get("GIT_WEBUI_REPO_ROOT", "repos")).expanduser()
REPO_ROOT.mkdir(parents=True, exist_ok=True)

def _load_config() -> Dict[str, object]:
    if not CONFIG_PATH.exists():
        return {"ssh_keys": [], "git_users": [], "server": {}}

    with CONFIG_PATH.open("rb") as config_file:
        try:
            data = tomllib.load(config_file)
        except tomllib.TOMLDecodeError as exc:  # noqa: BLE001
            raise RuntimeError(f"Failed to parse configuration file {CONFIG_PATH}: {exc}") from exc

    ssh_keys = data.get("ssh_keys", [])
    git_users = data.get("git_users", [])
    server = data.get("server", {})
    if not isinstance(ssh_keys, list) or not isinstance(git_users, list):
        raise RuntimeError("Configuration file must define 'ssh_keys' and 'git_users' as lists")
    if server is None:
        server = {}
    if not isinstance(server, dict):
        raise RuntimeError("Configuration file 'server' must be a table if provided")

    return {"ssh_keys": ssh_keys, "git_users": git_users, "server": server}


APP_CONFIG = _load_config()


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass
class LogSink:
    entries: List[str]
    websocket: Optional[web.WebSocketResponse] = None

    def append(self, message: str) -> None:
        self.entries.append(message)
        if self.websocket and not self.websocket.closed:
            asyncio.create_task(self.websocket.send_json({"type": "log", "line": message}))


async def run_command(
    *cmd: str,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    log: Optional[LogSink] = None,
) -> CommandResult:
    """Run a command asynchronously and capture its output."""
    printable_cmd = " ".join(cmd)
    if log is not None:
        log.append(_timestamped(f"$ {printable_cmd}"))
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        env=env,
    )
    stdout, stderr = await process.communicate()
    stdout_text = stdout.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")
    if stdout_text and log is not None:
        log.append(_timestamped(stdout_text.rstrip()))
    if stderr_text and log is not None:
        log.append(_timestamped(stderr_text.rstrip()))
    if log is not None:
        log.append(_timestamped(f"exit code: {process.returncode}"))
    return CommandResult(process.returncode, stdout_text, stderr_text)


def _timestamped(message: str) -> str:
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    return f"[{timestamp} UTC] {message}"


def _log_debug(logs: Optional[LogSink], message: str) -> None:
    if logs is None:
        return
    logs.append(_timestamped(f"DEBUG: {message}"))


def _format_ssh_key_arg(raw_path: str, resolved_path: Path) -> str:
    if "\\" in raw_path or ":" in raw_path:
        return shlex.quote(PureWindowsPath(raw_path).as_posix())
    return shlex.quote(str(resolved_path))


async def _git_ref_exists(
    repo_dir: Path,
    ref: str,
    env: Dict[str, str],
    logs: Optional[LogSink] = None,
) -> bool:
    result = await run_command(
        "git",
        "show-ref",
        "--verify",
        ref,
        cwd=repo_dir,
        env=env,
        log=logs,
    )
    return result.returncode == 0


async def _resolve_default_branch(repo_dir: Path, env: Dict[str, str], logs: Optional[LogSink] = None) -> str:
    _log_debug(logs, "Resolving default branch from origin/HEAD.")
    default_branch_result = await run_command(
        "git",
        "symbolic-ref",
        "--quiet",
        "--short",
        "refs/remotes/origin/HEAD",
        cwd=repo_dir,
        env=env,
        log=logs,
    )
    if default_branch_result.returncode == 0:
        resolved = default_branch_result.stdout.strip()
    else:
        resolved = ""

    if not resolved:
        _log_debug(logs, "origin/HEAD not available; checking 'git remote show origin'.")
        remote_show_result = await run_command(
            "git",
            "remote",
            "show",
            "origin",
            cwd=repo_dir,
            env=env,
            log=logs,
        )
        if remote_show_result.returncode == 0:
            match = re.search(r"HEAD branch:\s*(\S+)", remote_show_result.stdout)
            if match:
                resolved = match.group(1).strip()

    if not resolved:
        _log_debug(logs, "Unable to parse HEAD branch; falling back to common defaults.")
        for candidate in ("main", "master"):
            if await _git_ref_exists(repo_dir, f"refs/remotes/origin/{candidate}", env, logs):
                resolved = candidate
                break

    if not resolved:
        raise RuntimeError("Unable to resolve default branch from origin")

    if resolved.startswith("origin/"):
        resolved = resolved.split("/", 1)[1]
    if not resolved:
        raise RuntimeError("Resolved default branch name is empty")
    if not await _git_ref_exists(repo_dir, f"refs/remotes/origin/{resolved}", env, logs):
        raise RuntimeError(f"origin/{resolved} does not exist")
    _log_debug(logs, f"Resolved default branch '{resolved}'.")
    return resolved


async def _generate_unique_temp_branch(repo_dir: Path, env: Dict[str, str], logs: Optional[LogSink] = None) -> str:
    while True:
        candidate = f"tmp-clean-{secrets.token_hex(4)}"
        if not await _git_ref_exists(repo_dir, f"refs/heads/{candidate}", env, logs):
            _log_debug(logs, f"Generated temporary branch '{candidate}'.")
            return candidate
        _log_debug(logs, f"Temporary branch '{candidate}' already exists; regenerating.")


async def _reset_cached_repo_state(
    repo_dir: Path,
    env: Dict[str, str],
    logs: Optional[LogSink],
    default_branch: str,
    target_branch: Optional[str],
) -> None:
    tmp_branch = await _generate_unique_temp_branch(repo_dir, env, logs)
    _log_debug(logs, f"Switching to orphan temporary branch '{tmp_branch}'.")
    orphan_result = await run_command(
        "git",
        "switch",
        "--orphan",
        tmp_branch,
        cwd=repo_dir,
        env=env,
        log=logs,
    )
    if orphan_result.returncode != 0:
        raise RuntimeError("Failed to create temporary orphan branch")

    _log_debug(logs, "Creating empty commit on temporary branch.")
    commit_result = await run_command(
        "git",
        "-c",
        "user.name=git-webui",
        "-c",
        "user.email=git-webui@localhost",
        "commit",
        "--allow-empty",
        "-m",
        "temporary cleanup branch",
        cwd=repo_dir,
        env=env,
        log=logs,
    )
    if commit_result.returncode != 0:
        raise RuntimeError("Failed to create temporary empty commit")

    branches_result = await run_command(
        "git",
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads/",
        cwd=repo_dir,
        env=env,
        log=logs,
    )
    if branches_result.returncode != 0:
        raise RuntimeError("Failed to list local branches")
    branches = [line.strip() for line in branches_result.stdout.splitlines() if line.strip()]
    for branch in branches:
        if branch == tmp_branch:
            continue
        _log_debug(logs, f"Deleting local branch '{branch}'.")
        delete_result = await run_command(
            "git",
            "branch",
            "-D",
            branch,
            cwd=repo_dir,
            env=env,
            log=logs,
        )
        if delete_result.returncode != 0:
            raise RuntimeError(f"Failed to delete branch {branch}")

    _log_debug(logs, f"Recreating default branch '{default_branch}' from origin/{default_branch}.")
    switch_default_result = await run_command(
        "git",
        "switch",
        "-C",
        default_branch,
        f"origin/{default_branch}",
        cwd=repo_dir,
        env=env,
        log=logs,
    )
    if switch_default_result.returncode != 0:
        raise RuntimeError(f"Failed to reset default branch {default_branch}")

    _log_debug(logs, "Resetting and cleaning working tree.")
    reset_result = await run_command(
        "git",
        "reset",
        "--hard",
        cwd=repo_dir,
        env=env,
        log=logs,
    )
    if reset_result.returncode != 0:
        raise RuntimeError("git reset --hard failed")
    clean_result = await run_command(
        "git",
        "clean",
        "-fd",
        cwd=repo_dir,
        env=env,
        log=logs,
    )
    if clean_result.returncode != 0:
        raise RuntimeError("git clean -fd failed")

    if target_branch:
        if await _git_ref_exists(repo_dir, f"refs/remotes/origin/{target_branch}", env, logs):
            _log_debug(logs, f"Switching to target branch '{target_branch}' from origin/{target_branch}.")
            target_result = await run_command(
                "git",
                "switch",
                "-C",
                target_branch,
                f"origin/{target_branch}",
                cwd=repo_dir,
                env=env,
                log=logs,
            )
            if target_result.returncode != 0:
                raise RuntimeError(f"Failed to switch to origin/{target_branch}")
        else:
            _log_debug(logs, f"Creating new local branch '{target_branch}' from default branch.")
            create_target_result = await run_command(
                "git",
                "switch",
                "-c",
                target_branch,
                cwd=repo_dir,
                env=env,
                log=logs,
            )
            if create_target_result.returncode != 0:
                raise RuntimeError(f"Failed to create local branch {target_branch}")

    _log_debug(logs, f"Deleting temporary branch '{tmp_branch}'.")
    delete_tmp_result = await run_command(
        "git",
        "branch",
        "-D",
        tmp_branch,
        cwd=repo_dir,
        env=env,
        log=logs,
    )
    if delete_tmp_result.returncode != 0:
        raise RuntimeError(f"Failed to delete temporary branch {tmp_branch}")


def _parse_port(value: object) -> int:
    if isinstance(value, bool):
        raise RuntimeError("Server port must be an integer between 1 and 65535")
    if isinstance(value, int):
        port = value
    elif isinstance(value, str) and value.strip():
        try:
            port = int(value.strip())
        except ValueError as exc:
            raise RuntimeError(f"Server port must be an integer, got {value!r}") from exc
    else:
        raise RuntimeError("Server port must be an integer between 1 and 65535")

    if not 1 <= port <= 65535:
        raise RuntimeError("Server port must be between 1 and 65535")
    return port


def _resolve_server_bind(
    bind_override: Optional[str] = None,
    port_override: Optional[int] = None,
) -> tuple[str, int]:
    server_config = APP_CONFIG.get("server", {})
    env_bind = os.environ.get("GIT_WEBUI_BIND", "").strip()
    env_port = os.environ.get("GIT_WEBUI_PORT", "").strip()
    bind = bind_override or env_bind or server_config.get("bind", DEFAULT_BIND)
    port_source = port_override if port_override is not None else env_port or server_config.get("port", DEFAULT_PORT)
    return bind, _parse_port(port_source)


def _parse_port_argument(value: str) -> int:
    try:
        return _parse_port(value)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the git-webui backend server.")
    parser.add_argument("--bind", help="Bind address for the backend server.")
    parser.add_argument("--port", type=_parse_port_argument, help="Port number for the backend server.")
    parser.add_argument(
        "--serve-frontend",
        action="store_true",
        help="Serve the frontend UI from the backend server.",
    )
    return parser.parse_args()


def _frontend_root() -> Path:
    return Path(__file__).resolve().parent.parent / "docs"


def _repo_workspace_for_url(repository_url: str) -> Path:
    repo_name = Path(repository_url.rstrip("/")).name
    repo_name = repo_name[:-4] if repo_name.endswith(".git") else repo_name
    repo_name = re.sub(r"[^A-Za-z0-9._-]+", "-", repo_name).strip("-") or "repo"
    digest = hashlib.sha256(repository_url.encode("utf-8")).hexdigest()[:10]
    return REPO_ROOT / f"{repo_name}-{digest}"


@contextmanager
def _temporary_workspace(logs: Optional[LogSink] = None) -> Path:
    if KEEP_TEMP:
        tmpdir = tempfile.mkdtemp(prefix="git-webui-")
        workdir = Path(tmpdir)
        if logs is not None:
            logs.append(_timestamped(f"Keeping temporary workspace at {workdir}"))
        _log_debug(logs, "Temporary workspace will be preserved for debugging.")
        try:
            yield workdir
        finally:
            pass
    with tempfile.TemporaryDirectory(prefix="git-webui-") as tmpdir:
        yield Path(tmpdir)


def _find_default_index(entries: List[Dict[str, str]]) -> Optional[int]:
    for idx, entry in enumerate(entries):
        if entry.get("default") is True:
            return idx
    return None


def _display_label(entry: Dict[str, str], fallback: str) -> str:
    label = entry.get("label")
    if isinstance(label, str) and label.strip():
        return label
    return fallback


def _serialize_config() -> Dict[str, object]:
    ssh_keys = []
    for entry in APP_CONFIG["ssh_keys"]:
        label = _display_label(entry, entry.get("path", "Unknown Key"))
        ssh_keys.append(
            {
                "label": label,
                "default": entry.get("default") is True,
            }
        )
    git_users = []
    for entry in APP_CONFIG["git_users"]:
        name = entry.get("name", "")
        email = entry.get("email", "")
        fallback = " ".join(part for part in [name, f"<{email}>" if email else ""] if part).strip()
        git_users.append(
            {
                "label": _display_label(entry, fallback or "Unknown User"),
                "name": name,
                "email": email,
                "default": entry.get("default") is True,
            }
        )
    return {
        "ssh_keys": ssh_keys,
        "git_users": git_users,
        "default_ssh_key_index": _find_default_index(APP_CONFIG["ssh_keys"]),
        "default_git_user_index": _find_default_index(APP_CONFIG["git_users"]),
    }




def _normalize_form_payload(form: Dict[str, str]) -> Dict[str, str]:
    normalized = {}
    for key, value in form.items():
        normalized[key] = value if isinstance(value, str) else str(value)
    return normalized


async def process_submission(form: Dict[str, str], logs: LogSink) -> Dict[str, object]:
    _log_debug(logs, "Received submission payload.")
    repository_url = form.get("repository_url", "").strip()
    branch = form.get("branch", "").strip()
    new_branch = form.get("new_branch", "").strip()
    git_user_selection = form.get("git_user", "").strip()
    ssh_key_selection = form.get("ssh_key_path", "").strip()
    branch_mode = form.get("branch_mode", "default").strip()
    base_commit = form.get("base_commit", "").strip()
    commit_message = form.get("commit_message", "").replace("\r\n", "\n")
    commit_message = commit_message.strip("\n")
    allow_empty_commit = form.get("allow_empty_commit") == "true"
    patch_content = form.get("patch", "").replace("\r\n", "\n")
    if branch_mode == "from_commit":
        commit_message = ""
        allow_empty_commit = False
        patch_content = ""

    _log_debug(logs, f"Parsed repository_url='{repository_url}'.")
    _log_debug(logs, f"Parsed branch='{branch or '(default)'}'.")
    _log_debug(logs, f"Parsed new_branch='{new_branch or '(none)'}'.")
    _log_debug(logs, f"Parsed branch_mode='{branch_mode}'.")
    _log_debug(logs, f"Parsed base_commit='{base_commit or '(none)'}'.")
    _log_debug(logs, f"Parsed git_user selection='{git_user_selection or '(none)'}'.")
    _log_debug(logs, f"Parsed ssh_key selection='{ssh_key_selection or '(none)'}'.")
    _log_debug(logs, f"Commit message length={len(commit_message)}.")
    _log_debug(logs, f"Allow empty commit={allow_empty_commit}.")
    _log_debug(logs, f"Patch length={len(patch_content)}.")

    target_branch = new_branch if branch_mode in {"from_commit", "orphan"} else branch
    form_values = {
        "repository_url": repository_url,
        "branch": branch,
        "new_branch": new_branch,
        "commit_message": commit_message,
        "allow_empty_commit": "true" if allow_empty_commit else "",
        "git_user_selection": git_user_selection,
        "ssh_key_selection": ssh_key_selection,
        "branch_mode": branch_mode,
        "base_commit": base_commit,
    }

    user_name = ""
    user_email = ""
    if git_user_selection:
        try:
            user_idx = int(git_user_selection)
            user_entry = APP_CONFIG["git_users"][user_idx]
            user_name = user_entry.get("name", "").strip()
            user_email = user_entry.get("email", "").strip()
            _log_debug(logs, f"Resolved git user index={user_idx} name='{user_name}'.")
        except (ValueError, IndexError):
            logs.append(_timestamped("Invalid Git user selection."))
            return {"form_values": form_values, "success": False}

    _log_debug(logs, "Validated git user selection.")
    success = False

    if not repository_url:
        _log_debug(logs, "Repository URL missing.")
        logs.append(_timestamped("Repository URL is required."))
        return {"form_values": form_values, "success": False}

    if branch_mode != "from_commit" and not patch_content.strip() and not allow_empty_commit:
        _log_debug(logs, "Patch content missing or whitespace.")
        logs.append(_timestamped("Patch content is required unless empty commit is allowed."))
        return {"form_values": form_values, "success": False}
    if branch_mode == "from_commit" and base_commit and base_commit.upper() == "HEAD":
        _log_debug(logs, "Base commit set to HEAD for branch creation; will resolve to default branch.")
    if branch_mode in {"from_commit", "orphan"} and not new_branch:
        _log_debug(logs, "New branch name missing for selected branch mode.")
        logs.append(_timestamped("New branch name is required for commit/orphan branch creation modes."))
        return {"form_values": form_values, "success": False}

    ssh_key_path: Optional[Path] = None

    try:
        with _temporary_workspace(logs) as workdir:
            repo_dir = _repo_workspace_for_url(repository_url)
            env = os.environ.copy()
            _log_debug(logs, f"Created temporary workspace at {workdir}.")
            _log_debug(logs, f"Repository directory will be {repo_dir}.")

            if ssh_key_selection:
                try:
                    key_idx = int(ssh_key_selection)
                    key_entry = APP_CONFIG["ssh_keys"][key_idx]
                    raw_ssh_key_path = key_entry.get("path", "")
                    ssh_key_path = Path(raw_ssh_key_path).expanduser()
                except (ValueError, IndexError):
                    raise RuntimeError("Invalid SSH key selection") from None

                if not ssh_key_path or not ssh_key_path.exists():
                    raise RuntimeError(f"SSH key path not found: {ssh_key_path}")

                ssh_key_arg = _format_ssh_key_arg(raw_ssh_key_path, ssh_key_path)
                env["GIT_SSH_COMMAND"] = f"ssh -i {ssh_key_arg} -o StrictHostKeyChecking=no"
                logs.append(_timestamped(f"Using SSH key: {ssh_key_path}"))
                _log_debug(logs, f"GIT_SSH_COMMAND set to: {env['GIT_SSH_COMMAND']}")
            else:
                _log_debug(logs, "No SSH key selected; using default SSH configuration.")

            repo_prepared = False
            default_branch = ""
            if repo_dir.exists():
                if not (repo_dir / ".git").exists():
                    raise RuntimeError(f"Existing repository path is not a git repo: {repo_dir}")
                logs.append(_timestamped(f"Using existing repository at {repo_dir}"))
                _log_debug(logs, "Fetching latest changes from all remotes.")
                fetch_result = await run_command(
                    "git",
                    "-c",
                    "core.hooksPath=" + DEVNULL,
                    "fetch",
                    "--prune",
                    "--all",
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )
                if fetch_result.returncode != 0:
                    raise RuntimeError("git fetch failed")
                _log_debug(logs, "git fetch completed.")
                default_branch = await _resolve_default_branch(repo_dir, env, logs)
                if branch_mode not in {"from_commit", "orphan"} and not branch:
                    branch = default_branch
                target_branch = branch if branch_mode not in {"from_commit", "orphan"} else None
                _log_debug(logs, "Resetting cached repository state to match remote default branch.")
                await _reset_cached_repo_state(repo_dir, env, logs, default_branch, target_branch)
                repo_prepared = True
            else:
                logs.append(_timestamped(f"Cloning repository {repository_url}"))
                _log_debug(logs, "Starting git clone.")
                repo_dir.parent.mkdir(parents=True, exist_ok=True)
                clone_result = await run_command(
                    "git",
                    "-c", "core.hooksPath=" + DEVNULL,
                    "clone",
                    repository_url,
                    str(repo_dir),
                    env=env,
                    log=logs,
                )
                if clone_result.returncode != 0:
                    raise RuntimeError("git clone failed")
                _log_debug(logs, "git clone completed.")

            if user_name:
                _log_debug(logs, "Configuring git user.name.")
                config_result = await run_command(
                    "git",
                    "config",
                    "user.name",
                    user_name,
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )
                if config_result.returncode != 0:
                    raise RuntimeError("Failed to set git user.name")
                _log_debug(logs, "git user.name configured.")

            if user_email:
                _log_debug(logs, "Configuring git user.email.")
                config_result = await run_command(
                    "git",
                    "config",
                    "user.email",
                    user_email,
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )
                if config_result.returncode != 0:
                    raise RuntimeError("Failed to set git user.email")
                _log_debug(logs, "git user.email configured.")

            if branch_mode not in {"from_commit", "orphan"} and not branch:
                default_branch = default_branch or await _resolve_default_branch(repo_dir, env, logs)
                branch = default_branch

            if branch_mode == "from_commit":
                if not base_commit or base_commit.upper() == "HEAD":
                    default_branch = default_branch or await _resolve_default_branch(repo_dir, env, logs)
                    base_commit = f"origin/{default_branch}"
                    logs.append(_timestamped(f"Using {base_commit} as the base for branch creation."))
                    _log_debug(logs, f"Resolved base commit to '{base_commit}' for branch creation.")
                logs.append(_timestamped(f"Creating branch {new_branch} from commit {base_commit}."))
                _log_debug(logs, f"Creating branch '{new_branch}' from commit '{base_commit}'.")
                create_branch_result = await run_command(
                    "git",
                    "-c", "core.hooksPath=" + DEVNULL,
                    "checkout",
                    "-b",
                    new_branch,
                    base_commit,
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )
                if create_branch_result.returncode != 0:
                    raise RuntimeError("Failed to create branch from commit")
                _log_debug(logs, f"Branch '{new_branch}' created from commit.")
                _log_debug(logs, "Pushing branch created from commit to origin.")
                push_result = await run_command(
                    "git",
                    "-c", "core.hooksPath=" + DEVNULL,
                    "push",
                    "origin",
                    new_branch,
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )
                if push_result.returncode != 0:
                    raise RuntimeError("git push failed")
                logs.append(_timestamped("Branch created from commit and pushed successfully."))
                success = True
                return {"form_values": form_values, "success": success}
            elif branch_mode == "orphan":
                logs.append(_timestamped(f"Creating orphan branch {new_branch}."))
                _log_debug(logs, f"Creating orphan branch '{new_branch}'.")
                create_branch_result = await run_command(
                    "git",
                    "-c", "core.hooksPath=" + DEVNULL,
                    "checkout",
                    "--orphan",
                    new_branch,
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )
                if create_branch_result.returncode != 0:
                    raise RuntimeError("Failed to create orphan branch")
                _log_debug(logs, "Removing working tree files for orphan branch.")
                await run_command(
                    "git",
                    "rm",
                    "-rf",
                    ".",
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )
            elif branch and not repo_prepared:
                _log_debug(logs, f"Checking out branch '{branch}'.")
                checkout_result = await run_command(
                    "git",
                    "-c", "core.hooksPath=" + DEVNULL,
                    "checkout",
                    branch,
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )
                if checkout_result.returncode != 0:
                    logs.append(_timestamped(f"Branch {branch} not found. Creating new branch."))
                    _log_debug(logs, f"Creating new branch '{branch}'.")
                    create_branch_result = await run_command(
                        "git",
                        "-c", "core.hooksPath=" + DEVNULL,
                        "checkout",
                        "-b",
                        branch,
                        cwd=repo_dir,
                        env=env,
                        log=logs,
                    )
                    if create_branch_result.returncode != 0:
                        raise RuntimeError("Failed to create branch")
                    _log_debug(logs, f"Branch '{branch}' created.")
                else:
                    _log_debug(logs, f"Pulling latest changes for branch '{branch}'.")
                    pull_result = await run_command(
                        "git",
                        "-c",
                        "core.hooksPath=" + DEVNULL,
                        "pull",
                        "--ff-only",
                        "origin",
                        branch,
                        cwd=repo_dir,
                        env=env,
                        log=logs,
                    )
                    if pull_result.returncode != 0:
                        raise RuntimeError("git pull failed")
            elif not repo_prepared:
                _log_debug(logs, "No branch specified; using default branch.")
                await run_command(
                    "git",
                    "status",
                    "-sb",
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )
                _log_debug(logs, "Pulling latest changes for default branch.")
                pull_result = await run_command(
                    "git",
                    "-c",
                    "core.hooksPath=" + DEVNULL,
                    "pull",
                    "--ff-only",
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )
                if pull_result.returncode != 0:
                    raise RuntimeError("git pull failed")

            if patch_content.strip():
                patch_path = workdir / "patch.diff"
                logs.append(_timestamped(patch_content))
                patch_path.write_text(patch_content, encoding="utf-8", newline="\n")
                logs.append(_timestamped("Patch written to temporary file."))
                _log_debug(logs, f"Patch file saved to {patch_path}.")

                _log_debug(logs, "Applying patch with git apply --3way -v.")
                apply_result = await run_command(
                    "git",
                    "apply",
                    "--3way",
                    "-v",
                    str(patch_path),
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )
                if apply_result.returncode != 0:
                    raise RuntimeError("git apply failed")
                _log_debug(logs, "Patch applied successfully.")
            else:
                logs.append(_timestamped("No patch provided; skipping git apply."))
                _log_debug(logs, "Patch skipped because content is empty.")

            _log_debug(logs, "Staging changes with git add -A.")
            await run_command(
                "git",
                "add",
                "-A",
                cwd=repo_dir,
                env=env,
                log=logs,
            )

            _log_debug(logs, "Checking git status after staging.")
            await run_command(
                "git",
                "status",
                "-sb",
                cwd=repo_dir,
                env=env,
                log=logs,
            )

            if commit_message:
                commit_file = workdir / "commit_message.txt"
                commit_file.write_text(commit_message, encoding="utf-8", newline="\n")
                _log_debug(logs, f"Commit message file saved to {commit_file}.")
                _log_debug(logs, "Creating git commit.")
                commit_command = [
                    "git",
                    "-c",
                    "core.hooksPath=" + DEVNULL,
                    "commit",
                ]
                if allow_empty_commit:
                    commit_command.append("--allow-empty")
                commit_command.extend(["-F", str(commit_file)])
                commit_result = await run_command(
                    *commit_command,
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )
                if commit_result.returncode != 0:
                    raise RuntimeError("git commit failed")
                _log_debug(logs, "git commit completed.")
            else:
                logs.append(_timestamped("No commit message provided. Skipping commit."))
                _log_debug(logs, "Commit skipped due to empty commit message.")

            if commit_message:
                _log_debug(logs, "Pushing commit to origin.")
                push_result = await run_command(
                    "git",
                    "-c", "core.hooksPath=" + DEVNULL,
                    "push",
                    "origin",
                    f"HEAD:{target_branch}" if target_branch else "HEAD",
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )
                if push_result.returncode != 0:
                    raise RuntimeError("git push failed")
                logs.append(_timestamped("Patch applied, committed, and pushed successfully."))
                _log_debug(logs, "git push completed.")
            else:
                logs.append(_timestamped("Push skipped because no commit was created."))
                _log_debug(logs, "Push skipped due to missing commit.")
            success = True
    except Exception as exc:  # noqa: BLE001
        tb_str = traceback.format_exc()
        logs.append(_timestamped(f"ERROR: {exc}"))
        logs.append(_timestamped(tb_str))
        _log_debug(logs, "Request failed with exception.")
        success = False

    return {"form_values": form_values, "success": success}


def _cors_headers() -> Dict[str, str]:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


async def config_handler(request: web.Request) -> web.Response:
    payload = _serialize_config()
    return web.json_response(payload, headers=_cors_headers())


async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"}, headers=_cors_headers())


async def frontend_handler(request: web.Request) -> web.Response:
    frontend_root = request.app["frontend_root"]
    return web.FileResponse(frontend_root / "index.html")


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    websocket = web.WebSocketResponse()
    await websocket.prepare(request)
    sockets: set[web.WebSocketResponse] = request.app["websockets"]
    sockets.add(websocket)
    try:
        async for msg in websocket:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    await websocket.send_json({"type": "error", "message": "Invalid JSON payload."})
                    continue
                if payload.get("type") == "submit":
                    form_data = payload.get("payload", {})
                    if not isinstance(form_data, dict):
                        await websocket.send_json({"type": "error", "message": "Invalid form payload."})
                        continue
                    logs = LogSink(entries=[], websocket=websocket)
                    result = await process_submission(_normalize_form_payload(form_data), logs)
                    await websocket.send_json({"type": "complete", "success": result["success"]})
            if msg.type == web.WSMsgType.ERROR:
                break
    finally:
        sockets.discard(websocket)
    return websocket


async def close_websockets(app: web.Application) -> None:
    sockets: set[web.WebSocketResponse] = app["websockets"]
    if not sockets:
        return
    await asyncio.gather(*(socket.close() for socket in list(sockets)))


def create_app(serve_frontend: bool = False) -> web.Application:
    app = web.Application()
    app["websockets"] = set()
    app.on_shutdown.append(close_websockets)
    app.router.add_route("GET", "/api/health", health_handler)
    app.router.add_route("GET", "/api/config", config_handler)
    app.router.add_route("GET", "/ws", websocket_handler)
    if serve_frontend:
        frontend_root = _frontend_root()
        if not frontend_root.exists():
            raise RuntimeError(f"Frontend root not found at {frontend_root}")
        app["frontend_root"] = frontend_root
        app.router.add_route("GET", "/", frontend_handler)
        app.router.add_route("GET", "/index.html", frontend_handler)
    else:
        app.router.add_route("GET", "/", health_handler)
    return app


if __name__ == "__main__":
    args = _parse_args()
    bind, port = _resolve_server_bind(bind_override=args.bind, port_override=args.port)
    web.run_app(
        create_app(serve_frontend=args.serve_frontend),
        host=bind,
        port=port,
        max_line_size=MAX_LINE_SIZE,
    )
