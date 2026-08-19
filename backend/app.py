from __future__ import annotations

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
from datetime import UTC, datetime
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Set
import traceback

from aiohttp import web

SSE_PATH = "/events"

DEFAULT_CONFIG_PATH = Path("config.toml")
DEFAULT_BIND = "0.0.0.0"
DEFAULT_PORT = 8080
MAX_LINE_SIZE = 32 * 1024


@dataclass(frozen=True)
class ServerListenConfig:
    host: Optional[str]
    port: Optional[int]
    path: Optional[str]


DEVNULL = "NUL" if os.name == "nt" else "/dev/null"
GIT_COMMON_OPTIONS = ("-c", "core.hooksPath=" + DEVNULL)
KEEP_TEMP = False
DEFAULT_REPO_ROOT = Path("repos")
REPO_ROOT = DEFAULT_REPO_ROOT.expanduser()
REPO_ROOT.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DEFAULT_CONFIG_PATH

def _load_config(config_path: Path) -> Dict[str, object]:
    if not config_path.exists():
        return {"git_users": []}

    with config_path.open("rb") as config_file:
        try:
            data = tomllib.load(config_file)
        except tomllib.TOMLDecodeError as exc:  # noqa: BLE001
            raise RuntimeError(f"Failed to parse configuration file {config_path}: {exc}") from exc

    git_users = data.get("git_users", [])
    if not isinstance(git_users, list):
        raise RuntimeError("Configuration file must define 'git_users' as a list")

    return {"git_users": git_users}


APP_CONFIG = {"git_users": []}


def _resolve_cors_allow_origin() -> List[str]:
    raw_value = os.environ.get("CORS_ALLOW_ORIGIN", "*").strip()
    if not raw_value:
        return ["*"]
    return [origin.strip() for origin in raw_value.split(",") if origin.strip()] or ["*"]


def _build_cors_headers(request: web.Request) -> Dict[str, str]:
    allowed = _resolve_cors_allow_origin()
    origin = request.headers.get("Origin", "")

    if not origin:
        return {}

    if "*" in allowed:
        allow_origin = "*"
    elif origin in allowed:
        allow_origin = origin
    else:
        return {}

    requested_headers = request.headers.get("Access-Control-Request-Headers", "")
    headers = {
        "Access-Control-Allow-Origin": allow_origin,
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": requested_headers or "Content-Type",
    }
    if allow_origin != "*":
        headers["Vary"] = "Origin"
    return headers


@web.middleware
async def cors_middleware(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> web.StreamResponse:
    cors_headers = _build_cors_headers(request)

    if request.method == "OPTIONS":
        if request.headers.get("Origin", "") and not cors_headers:
            return web.Response(status=403, text="CORS origin denied")
        return web.Response(status=204, headers=cors_headers)

    response = await handler(request)
    if cors_headers:
        response.headers.update(cors_headers)
    return response




@dataclass
class LineEndingSnapshot:
    path: Path
    line_ending: str
    is_text: bool


def _normalize_bytes_to_lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _detect_line_ending(data: bytes) -> str:
    if not data:
        return "none"
    crlf_count = data.count(b"\r\n")
    without_crlf = data.replace(b"\r\n", b"")
    lf_count = without_crlf.count(b"\n")
    cr_count = without_crlf.count(b"\r")
    if crlf_count and not lf_count and not cr_count:
        return "crlf"
    if lf_count and not crlf_count and not cr_count:
        return "lf"
    if cr_count and not crlf_count and not lf_count:
        return "cr"
    if crlf_count or lf_count or cr_count:
        return "mixed"
    return "none"


def _parse_patch_path_token(token: str) -> str:
    if token in {"/dev/null", "NUL"}:
        return ""
    if token.startswith("a/") or token.startswith("b/"):
        return token[2:]
    return token


def _parse_patch_file_header_path(line: str) -> str:
    value = line[4:].strip()
    if not value:
        return ""
    if value == "/dev/null":
        return ""
    if value.startswith('"'):
        try:
            parts = shlex.split(value)
        except ValueError:
            return ""
        return _parse_patch_path_token(parts[0]) if parts else ""
    return _parse_patch_path_token(value.split("\t", 1)[0].split(" ", 1)[0])


def _patch_referenced_paths(patch_content: str) -> Set[str]:
    paths: Set[str] = set()
    for line in patch_content.splitlines():
        if line.startswith("diff --git "):
            try:
                parts = shlex.split(line)
            except ValueError:
                continue
            if len(parts) >= 4:
                for token in parts[2:4]:
                    parsed = _parse_patch_path_token(token)
                    if parsed:
                        paths.add(parsed)
        elif line.startswith("--- ") or line.startswith("+++ "):
            parsed = _parse_patch_file_header_path(line)
            if parsed:
                paths.add(parsed)
    return paths


def _safe_repo_file_path(repo_dir: Path, relative_path: str) -> Optional[Path]:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    resolved_repo = repo_dir.resolve()
    resolved_candidate = (repo_dir / candidate).resolve()
    try:
        resolved_candidate.relative_to(resolved_repo)
    except ValueError:
        return None
    return resolved_candidate


def _line_ending_snapshots(repo_dir: Path, patch_content: str) -> List[LineEndingSnapshot]:
    snapshots: List[LineEndingSnapshot] = []
    seen_paths: Set[Path] = set()
    for relative_path in sorted(_patch_referenced_paths(patch_content)):
        file_path = _safe_repo_file_path(repo_dir, relative_path)
        if file_path is None or file_path in seen_paths or not file_path.is_file():
            continue
        seen_paths.add(file_path)
        data = file_path.read_bytes()
        snapshots.append(
            LineEndingSnapshot(
                path=file_path,
                line_ending=_detect_line_ending(data),
                is_text=b"\0" not in data,
            )
        )
    return snapshots


def _normalize_files_to_lf(snapshots: List[LineEndingSnapshot]) -> List[LineEndingSnapshot]:
    normalized: List[LineEndingSnapshot] = []
    for snapshot in snapshots:
        if not snapshot.is_text or not snapshot.path.exists():
            continue
        original = snapshot.path.read_bytes()
        converted = _normalize_bytes_to_lf(original)
        if converted != original:
            snapshot.path.write_bytes(converted)
        normalized.append(snapshot)
    return normalized


def _restore_original_line_endings(snapshots: List[LineEndingSnapshot]) -> None:
    for snapshot in snapshots:
        if not snapshot.is_text or not snapshot.path.exists():
            continue
        if snapshot.line_ending not in {"crlf", "cr", "mixed"}:
            continue
        lf_data = _normalize_bytes_to_lf(snapshot.path.read_bytes())
        if snapshot.line_ending == "cr":
            restored = lf_data.replace(b"\n", b"\r")
        else:
            restored = lf_data.replace(b"\n", b"\r\n")
        snapshot.path.write_bytes(restored)


async def _apply_patch_with_line_ending_retry(
    repo_dir: Path,
    patch_path: Path,
    patch_content: str,
    env: Dict[str, str],
    logs: LogSink,
) -> None:
    _log_debug(logs, "Checking patch with git apply --3way --check -v.")
    check_result = await run_git_command(
        "apply",
        "--3way",
        "--check",
        "-v",
        str(patch_path),
        cwd=repo_dir,
        env=env,
        log=logs,
    )

    if check_result.returncode == 0:
        _log_debug(logs, "Applying patch with git apply --3way -v.")
        apply_result = await run_git_command(
            "apply",
            "--3way",
            "-v",
            str(patch_path),
            cwd=repo_dir,
            env=env,
            log=logs,
        )
        if apply_result.returncode == 0:
            return
        _log_debug(logs, "git apply failed after a successful check; retrying with LF-normalized inputs.")
    else:
        _log_debug(logs, "git apply check failed; retrying with LF-normalized inputs.")

    snapshots = _line_ending_snapshots(repo_dir, patch_content)
    for snapshot in snapshots:
        relative = snapshot.path.relative_to(repo_dir.resolve())
        _log_debug(
            logs,
            f"Detected {snapshot.line_ending.upper()} line endings in {relative}."
            if snapshot.is_text
            else f"Detected binary file {relative}; skipping line-ending normalization.",
        )

    _log_debug(logs, "Temporarily normalizing referenced files and patch to LF line endings.")
    normalized_snapshots = _normalize_files_to_lf(snapshots)
    patch_path.write_bytes(_normalize_bytes_to_lf(patch_path.read_bytes()))
    try:
        apply_result = await run_git_command(
            "apply",
            "-v",
            str(patch_path),
            cwd=repo_dir,
            env=env,
            log=logs,
        )
    finally:
        _restore_original_line_endings(normalized_snapshots)
        _log_debug(logs, "Restored original line ending format for normalized files.")

    if apply_result.returncode != 0:
        raise RuntimeError("git apply failed")


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass
class LogSink:
    entries: List[str]
    emit: Optional[Callable[[Dict[str, object]], Awaitable[None]]] = None

    def append(self, message: str) -> None:
        self.entries.append(message)
        if self.emit is not None:
            asyncio.create_task(self.emit({"type": "log", "line": message}))


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


_GH_AUTH_LOCK = asyncio.Lock()


def _parse_gh_hosts(output: str) -> tuple[List[str], Optional[str]]:
    try:
        hosts = json.loads(output).get("hosts", {})
    except (json.JSONDecodeError, AttributeError) as exc:
        raise RuntimeError("Unable to parse gh authentication status") from exc
    accounts = hosts.get("github.com", []) if isinstance(hosts, dict) else []
    users = [account.get("login", "") for account in accounts if isinstance(account, dict) and account.get("login")]
    active = next((account.get("login") for account in accounts if isinstance(account, dict) and account.get("active") is True), None)
    return users, active


async def _gh_auth_status(log: Optional[LogSink] = None) -> tuple[List[str], Optional[str]]:
    result = await run_command("gh", "auth", "status", "--json", "hosts", log=log)
    # gh returns a non-zero status when no account is logged in, while still
    # emitting the requested valid JSON (`{"hosts": {}}`).
    return _parse_gh_hosts(result.stdout)


async def _get_github_users() -> List[str]:
    users, _ = await _gh_auth_status()
    return users


async def _run_network_git_command(git_args: List[str], username: str, cwd: Optional[Path], env: Optional[Dict[str, str]], log: Optional[LogSink]) -> CommandResult:
    async with _GH_AUTH_LOCK:
        users, active = await _gh_auth_status(log)
        if username not in users:
            raise RuntimeError(f"GitHub user is not authenticated: {username}")
        switched = active != username
        if switched:
            result = await run_command("gh", "auth", "switch", "--hostname", "github.com", "--user", username, log=log)
            if result.returncode != 0:
                raise RuntimeError(f"Failed to switch GitHub authentication to {username}")
        try:
            return await run_command("git", *GIT_COMMON_OPTIONS, *git_args, cwd=cwd, env=env, log=log)
        finally:
            if switched and active:
                restore = await run_command("gh", "auth", "switch", "--hostname", "github.com", "--user", active, log=log)
                if restore.returncode != 0:
                    raise RuntimeError(f"Failed to restore GitHub authentication to {active}")


async def run_git_command(
    *cmd: str,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    log: Optional[LogSink] = None,
) -> CommandResult:
    git_args = list(cmd)
    if git_args and git_args[0] == "git":
        git_args = git_args[1:]

    idx = 0
    hooks_path_option = "core.hooksPath=" + DEVNULL
    while idx < len(git_args) - 1:
        if git_args[idx] == "-c" and git_args[idx + 1] == hooks_path_option:
            del git_args[idx : idx + 2]
            continue
        idx += 1

    command_env = env.copy() if env is not None else None
    github_username = command_env.pop("GIT_WEBUI_GITHUB_USERNAME", "") if command_env is not None else ""
    if github_username and git_args and git_args[0] in {"clone", "fetch", "pull", "push"}:
        return await _run_network_git_command(git_args, github_username, cwd, command_env, log)
    return await run_command("git", *GIT_COMMON_OPTIONS, *git_args, cwd=cwd, env=command_env, log=log)


def _timestamped(message: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    return f"[{timestamp} UTC] {message}"


def _log_debug(logs: Optional[LogSink], message: str) -> None:
    if logs is None:
        return
    logs.append(_timestamped(f"DEBUG: {message}"))


async def _git_ref_exists(
    repo_dir: Path,
    ref: str,
    env: Dict[str, str],
    logs: Optional[LogSink] = None,
) -> bool:
    result = await run_git_command(
        "show-ref",
        "--verify",
        ref,
        cwd=repo_dir,
        env=env,
        log=logs,
    )
    return result.returncode == 0


async def _remote_has_branches(repo_dir: Path, env: Dict[str, str], logs: Optional[LogSink] = None) -> bool:
    result = await run_git_command(
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/remotes/origin/",
        cwd=repo_dir,
        env=env,
        log=logs,
    )
    if result.returncode != 0:
        raise RuntimeError("Failed to list remote branches")
    return any(
        line.strip() and line.strip() != "origin/HEAD"
        for line in result.stdout.splitlines()
    )


async def _resolve_default_branch(repo_dir: Path, env: Dict[str, str], logs: Optional[LogSink] = None) -> str:
    _log_debug(logs, "Resolving default branch from origin/HEAD.")
    default_branch_result = await run_git_command(
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

    if resolved:
        candidate = resolved
        if candidate.startswith("origin/"):
            candidate = candidate.split("/", 1)[1]
        if not await _git_ref_exists(repo_dir, f"refs/remotes/origin/{candidate}", env, logs):
            _log_debug(logs, f"origin/HEAD points to missing ref '{resolved}'; falling back.")
            resolved = ""

    if not resolved:
        _log_debug(logs, "origin/HEAD not available; checking 'git remote show origin'.")
        remote_show_result = await run_git_command(
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
    _log_debug(logs, "Pre-cleaning cached repository state before switching branches.")
    reset_before_result = await run_git_command(
        "reset",
        "--hard",
        cwd=repo_dir,
        env=env,
        log=logs,
    )
    if reset_before_result.returncode != 0:
        raise RuntimeError("git reset --hard failed before orphan switch")
    clean_before_result = await run_git_command(
        "clean",
        "-fd",
        cwd=repo_dir,
        env=env,
        log=logs,
    )
    if clean_before_result.returncode != 0:
        raise RuntimeError("git clean -fd failed before orphan switch")

    tmp_branch = await _generate_unique_temp_branch(repo_dir, env, logs)
    _log_debug(logs, f"Switching to orphan temporary branch '{tmp_branch}'.")
    orphan_result = await run_git_command(
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
    commit_result = await run_git_command(
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

    branches_result = await run_git_command(
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
        delete_result = await run_git_command(
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
    switch_default_result = await run_git_command(
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
    reset_result = await run_git_command(
        "reset",
        "--hard",
        cwd=repo_dir,
        env=env,
        log=logs,
    )
    if reset_result.returncode != 0:
        raise RuntimeError("git reset --hard failed")
    clean_result = await run_git_command(
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
            target_result = await run_git_command(
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
            create_target_result = await run_git_command(
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
    delete_tmp_result = await run_git_command(
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


def _resolve_server_listen_config(
    bind_override: Optional[str] = None,
    port_override: Optional[int] = None,
    unix_socket: Optional[str] = None,
) -> ServerListenConfig:
    if unix_socket:
        if bind_override is not None or port_override is not None:
            raise RuntimeError("--unix-socket cannot be combined with --bind or --port")
        return ServerListenConfig(host=None, port=None, path=unix_socket)

    bind = bind_override or DEFAULT_BIND
    port_source = port_override if port_override is not None else DEFAULT_PORT
    return ServerListenConfig(host=bind, port=_parse_port(port_source), path=None)


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
        "--unix-socket",
        help="AF_UNIX socket path for the backend server. Cannot be combined with --bind or --port.",
    )
    parser.add_argument(
        "--config",
        help="Path to the config.toml file.",
        default=str(DEFAULT_CONFIG_PATH),
    )
    parser.add_argument(
        "--repo-root",
        help="Path to the repository workspace root.",
        default=str(DEFAULT_REPO_ROOT),
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep temporary workspaces for debugging.",
        default=False,
    )
    parser.add_argument(
        "--serve-frontend",
        dest="serve_frontend",
        action="store_true",
        default=True,
        help="Serve the frontend UI from the backend server (default).",
    )
    parser.add_argument(
        "--no-serve-frontend",
        dest="serve_frontend",
        action="store_false",
        help="Disable serving the frontend UI from the backend server.",
    )
    return parser.parse_args()


def _configure_runtime(config_path: str, repo_root: str, keep_temp: bool) -> None:
    global CONFIG_PATH, REPO_ROOT, KEEP_TEMP, APP_CONFIG
    CONFIG_PATH = Path(config_path)
    REPO_ROOT = Path(repo_root).expanduser()
    REPO_ROOT.mkdir(parents=True, exist_ok=True)
    KEEP_TEMP = keep_temp
    APP_CONFIG = _load_config(CONFIG_PATH)


def _frontend_root() -> Path:
    return Path(__file__).resolve().parent.parent / "frontend"


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


def _find_default_index(entries: List[Dict[str, object]]) -> Optional[int]:
    for idx, entry in enumerate(entries):
        if entry.get("default") is True:
            return idx
    return None


def _normalize_repository_identifier(value: str) -> str:
    trimmed = value.strip()
    if not trimmed:
        return ""
    if "://" in trimmed:
        try:
            from urllib.parse import urlparse

            parsed = urlparse(trimmed)
            trimmed = f"{parsed.hostname or ''}{parsed.path}"
        except ValueError:
            pass
    else:
        match = re.match(r"^(?:[^@]+@)?([^:]+):(.+)$", trimmed)
        if match:
            trimmed = f"{match.group(1)}/{match.group(2)}"
    return trimmed.strip("/").removesuffix(".git").lower()


def _is_supported_repository_url(value: str) -> bool:
    """Accept local paths for testing/self-hosting, but require HTTPS for URLs."""
    if "://" in value:
        return value.lower().startswith("https://")
    return re.match(r"^(?:[^@]+@)?[^:]+:.+$", value) is None


def _display_label(entry: Dict[str, str], fallback: str) -> str:
    label = entry.get("label")
    if isinstance(label, str) and label.strip():
        return label
    return fallback


def _serialize_config(github_users: List[str]) -> Dict[str, object]:
    git_users = []
    for entry in APP_CONFIG["git_users"]:
        name = entry.get("name", "")
        email = entry.get("email", "")
        default_repos = entry.get("default_repositories", [])
        if not isinstance(default_repos, list):
            default_repos = []
        default_repos = [
            repo.strip()
            for repo in default_repos
            if isinstance(repo, str) and repo.strip()
        ]
        fallback = " ".join(part for part in [name, f"<{email}>" if email else ""] if part).strip()
        git_users.append(
            {
                "label": _display_label(entry, fallback or "Unknown User"),
                "name": name,
                "email": email,
                "default": entry.get("default") is True,
                "default_repositories": default_repos,
            }
        )
    return {
        "github_users": github_users,
        "git_users": git_users,
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
    mirror_repository_url = form.get("mirror_repository_url", "").strip()
    branch = form.get("branch", "").strip()
    new_branch = form.get("new_branch", "").strip()
    git_user_selection = form.get("git_user", "").strip()
    github_username = form.get("github_username", "").strip()
    branch_mode = form.get("branch_mode", "default").strip()
    base_commit = form.get("base_commit", "").strip()
    commit_message = form.get("commit_message", "").replace("\r\n", "\n")
    commit_message = commit_message.strip("\n")
    allow_empty_commit = form.get("allow_empty_commit") == "true"
    patch_content = form.get("patch", "").replace("\r\n", "\n")
    if branch_mode in {"from_commit", "revert_to_commit", "mirror_repository"}:
        commit_message = ""
        allow_empty_commit = False
        patch_content = ""

    _log_debug(logs, f"Parsed repository_url='{repository_url}'.")
    _log_debug(logs, f"Parsed mirror_repository_url='{mirror_repository_url or '(none)'}'.")
    _log_debug(logs, f"Parsed branch='{branch or '(default)'}'.")
    _log_debug(logs, f"Parsed new_branch='{new_branch or '(none)'}'.")
    _log_debug(logs, f"Parsed branch_mode='{branch_mode}'.")
    _log_debug(logs, f"Parsed base_commit='{base_commit or '(none)'}'.")
    _log_debug(logs, f"Parsed git_user selection='{git_user_selection or '(none)'}'.")
    _log_debug(logs, f"Parsed GitHub username='{github_username or '(none)'}'.")
    _log_debug(logs, f"Commit message length={len(commit_message)}.")
    _log_debug(logs, f"Allow empty commit={allow_empty_commit}.")
    _log_debug(logs, f"Patch length={len(patch_content)}.")

    target_branch = new_branch if branch_mode in {"from_commit", "orphan"} else (branch if branch_mode in {"default", "merge_branches"} else None)
    form_values = {
        "repository_url": repository_url,
        "mirror_repository_url": mirror_repository_url,
        "branch": branch,
        "new_branch": new_branch,
        "commit_message": commit_message,
        "allow_empty_commit": "true" if allow_empty_commit else "",
        "git_user_selection": git_user_selection,
        "github_username": github_username,
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
    if not _is_supported_repository_url(repository_url):
        logs.append(_timestamped("Repository URL must use HTTPS; SSH remotes are not supported."))
        return {"form_values": form_values, "success": False}
    if mirror_repository_url and not _is_supported_repository_url(mirror_repository_url):
        logs.append(_timestamped("Repository B URL must use HTTPS; SSH remotes are not supported."))
        return {"form_values": form_values, "success": False}

    if branch_mode not in {"from_commit", "revert_to_commit", "merge_branches", "mirror_repository"} and not patch_content.strip() and not allow_empty_commit:
        _log_debug(logs, "Patch content missing or whitespace.")
        logs.append(_timestamped("Patch content is required unless empty commit is allowed."))
        return {"form_values": form_values, "success": False}
    if branch_mode == "mirror_repository" and not mirror_repository_url:
        _log_debug(logs, "Repository B URL missing for mirror mode.")
        logs.append(_timestamped("Repository B URL is required for mirror mode."))
        return {"form_values": form_values, "success": False}
    if branch_mode == "mirror_repository" and repository_url == mirror_repository_url:
        _log_debug(logs, "Repository A and Repository B are identical in mirror mode.")
        logs.append(_timestamped("Repository A and Repository B must be different for mirror mode."))
        return {"form_values": form_values, "success": False}
    if branch_mode == "from_commit" and base_commit and base_commit.upper() == "HEAD":
        _log_debug(logs, "Base commit set to HEAD for branch creation; will resolve to default branch.")
    if branch_mode == "revert_to_commit" and not branch:
        _log_debug(logs, "Branch name missing for revert mode.")
        logs.append(_timestamped("Branch is required for revert mode."))
        return {"form_values": form_values, "success": False}
    if branch_mode == "revert_to_commit" and not base_commit:
        _log_debug(logs, "Commit ID missing for revert mode.")
        logs.append(_timestamped("Commit ID is required for revert mode."))
        return {"form_values": form_values, "success": False}
    if branch_mode in {"from_commit", "orphan"} and not new_branch:
        _log_debug(logs, "New branch name missing for selected branch mode.")
        logs.append(_timestamped("New branch name is required for commit/orphan branch creation modes."))
        return {"form_values": form_values, "success": False}
    if branch_mode == "merge_branches" and not branch:
        _log_debug(logs, "Branch A missing for merge mode.")
        logs.append(_timestamped("Branch A is required for merge mode."))
        return {"form_values": form_values, "success": False}
    if branch_mode == "merge_branches" and not new_branch:
        _log_debug(logs, "Branch B missing for merge mode.")
        logs.append(_timestamped("Branch B is required for merge mode."))
        return {"form_values": form_values, "success": False}
    if branch_mode == "merge_branches" and branch and new_branch and branch == new_branch:
        _log_debug(logs, "Branch A and Branch B are identical in merge mode.")
        logs.append(_timestamped("Branch A and Branch B must be different for merge mode."))
        return {"form_values": form_values, "success": False}

    try:
        with _temporary_workspace(logs) as workdir:
            repo_dir = _repo_workspace_for_url(repository_url)
            env = os.environ.copy()
            if github_username:
                env["GIT_WEBUI_GITHUB_USERNAME"] = github_username
            _log_debug(logs, f"Created temporary workspace at {workdir}.")
            _log_debug(logs, f"Repository directory will be {repo_dir}.")

            if branch_mode == "mirror_repository":
                source_env = env.copy()
                target_env = env.copy()
                mirror_dir = workdir / "mirror.git"
                logs.append(_timestamped(f"Cloning Repository A as a mirror: {repository_url}"))
                clone_result = await run_git_command(
                    "clone",
                    "--mirror",
                    repository_url,
                    str(mirror_dir),
                    env=source_env,
                    log=logs,
                )
                if clone_result.returncode != 0:
                    raise RuntimeError("git clone --mirror failed")
                logs.append(_timestamped(f"Pushing mirrored refs to Repository B: {mirror_repository_url}"))
                push_result = await run_git_command(
                    "push",
                    "--mirror",
                    mirror_repository_url,
                    cwd=mirror_dir,
                    env=target_env,
                    log=logs,
                )
                if push_result.returncode != 0:
                    raise RuntimeError("git push --mirror failed")
                logs.append(_timestamped("Repository A was mirrored to Repository B successfully."))
                success = True
                return {"form_values": form_values, "success": success}

            repo_prepared = False
            default_branch = ""
            if repo_dir.exists():
                if not (repo_dir / ".git").exists():
                    raise RuntimeError(f"Existing repository path is not a git repo: {repo_dir}")
                logs.append(_timestamped(f"Using existing repository at {repo_dir}"))
                _log_debug(logs, "Fetching latest changes from all remotes.")
                fetch_result = await run_git_command(
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
                if branch_mode == "orphan" and not await _remote_has_branches(repo_dir, env, logs):
                    _log_debug(logs, "Origin has no branches; skipping default branch resolution for orphan creation.")
                    _log_debug(logs, "Cleaning cached repository before creating orphan branch.")
                    reset_result = await run_git_command(
                        "reset",
                        "--hard",
                        cwd=repo_dir,
                        env=env,
                        log=logs,
                    )
                    if reset_result.returncode != 0:
                        raise RuntimeError("git reset --hard failed before orphan creation")
                    clean_result = await run_git_command(
                        "clean",
                        "-fd",
                        cwd=repo_dir,
                        env=env,
                        log=logs,
                    )
                    if clean_result.returncode != 0:
                        raise RuntimeError("git clean -fd failed before orphan creation")
                else:
                    default_branch = await _resolve_default_branch(repo_dir, env, logs)
                    if branch_mode == "default" and not branch:
                        branch = default_branch
                    target_branch = branch if branch_mode not in {"from_commit", "orphan"} else None
                    _log_debug(logs, "Resetting cached repository state to match remote default branch.")
                    await _reset_cached_repo_state(repo_dir, env, logs, default_branch, target_branch)
                    repo_prepared = True
            else:
                logs.append(_timestamped(f"Cloning repository {repository_url}"))
                _log_debug(logs, "Starting git clone.")
                repo_dir.parent.mkdir(parents=True, exist_ok=True)
                clone_result = await run_git_command(
                    "clone",
                    repository_url,
                    str(repo_dir),
                    env=env,
                    log=logs,
                )
                if clone_result.returncode != 0:
                    raise RuntimeError("git clone failed")
                _log_debug(logs, "git clone completed.")

            if branch_mode == "merge_branches":
                _log_debug(logs, "Merge mode selected; unsetting local git user.name/user.email.")
                for config_key in ("user.name", "user.email"):
                    unset_result = await run_git_command(
                        "config",
                        "--local",
                        "--unset-all",
                        config_key,
                        cwd=repo_dir,
                        env=env,
                        log=logs,
                    )
                    if unset_result.returncode not in {0, 5}:
                        raise RuntimeError(f"Failed to unset git {config_key}")
            else:
                if user_name:
                    _log_debug(logs, "Configuring git user.name.")
                    config_result = await run_git_command(
                        "config",
                        "--local",
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
                    config_result = await run_git_command(
                        "config",
                        "--local",
                        "user.email",
                        user_email,
                        cwd=repo_dir,
                        env=env,
                        log=logs,
                    )
                    if config_result.returncode != 0:
                        raise RuntimeError("Failed to set git user.email")
                    _log_debug(logs, "git user.email configured.")

            if branch_mode == "default" and not branch:
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
                create_branch_result = await run_git_command(
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
                push_result = await run_git_command(
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
            elif branch_mode == "revert_to_commit":
                logs.append(_timestamped(f"Resetting branch {branch} to commit {base_commit}."))
                if not await _git_ref_exists(repo_dir, f"refs/remotes/origin/{branch}", env, logs):
                    raise RuntimeError(f"Branch '{branch}' does not exist on origin for revert mode")
                _log_debug(logs, f"Checking out existing branch '{branch}' from origin for revert mode.")
                checkout_result = await run_git_command(
                    "switch",
                    "-C",
                    branch,
                    f"origin/{branch}",
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )
                if checkout_result.returncode != 0:
                    raise RuntimeError("Failed to checkout branch for revert mode")
                _log_debug(logs, f"Resetting branch '{branch}' to commit '{base_commit}'.")
                reset_result = await run_git_command(
                    "reset",
                    "--hard",
                    base_commit,
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )
                if reset_result.returncode != 0:
                    raise RuntimeError("git reset --hard failed")
                _log_debug(logs, f"Force-pushing branch '{branch}' to origin.")
                push_result = await run_git_command(
                    "push",
                    "-f",
                    "origin",
                    branch,
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )
                if push_result.returncode != 0:
                    raise RuntimeError("git push failed")
                logs.append(_timestamped("Branch reset and force-pushed successfully."))
                success = True
                return {"form_values": form_values, "success": success}
            elif branch_mode == "orphan":
                logs.append(_timestamped(f"Creating orphan branch {new_branch}."))
                _log_debug(logs, f"Creating orphan branch '{new_branch}'.")
                create_branch_result = await run_git_command(
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
                await run_git_command(
                    "rm",
                    "-rf",
                    ".",
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )
            elif branch_mode == "merge_branches":
                logs.append(_timestamped(f"Merging branch {new_branch} into {branch}."))
                if not await _git_ref_exists(repo_dir, f"refs/remotes/origin/{branch}", env, logs):
                    raise RuntimeError(f"Branch '{branch}' does not exist on origin")
                if not await _git_ref_exists(repo_dir, f"refs/remotes/origin/{new_branch}", env, logs):
                    raise RuntimeError(f"Branch '{new_branch}' does not exist on origin")
                _log_debug(logs, f"Checking whether '{branch}' can be fast-forwarded to '{new_branch}'.")
                ff_check_result = await run_git_command(
                    "merge-base",
                    "--is-ancestor",
                    f"origin/{branch}",
                    f"origin/{new_branch}",
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )
                if ff_check_result.returncode != 0:
                    raise RuntimeError("Fast-forward merge is not possible (branch A is not an ancestor of branch B)")
                _log_debug(logs, f"Fast-forward updating '{branch}' to '{new_branch}' on origin.")
                push_result = await run_git_command(
                    "push",
                    "origin",
                    f"refs/remotes/origin/{new_branch}:refs/heads/{branch}",
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )
                if push_result.returncode != 0:
                    raise RuntimeError("git push failed")
                _log_debug(logs, f"Deleting merged source branch '{new_branch}' on origin.")
                delete_remote_result = await run_git_command(
                    "push",
                    "origin",
                    "--delete",
                    new_branch,
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )
                if delete_remote_result.returncode != 0:
                    raise RuntimeError("Failed to delete source branch on origin")
                _log_debug(logs, f"Verifying source branch '{new_branch}' was deleted from origin.")
                verify_delete_remote_result = await run_git_command(
                    "ls-remote",
                    "--exit-code",
                    "--heads",
                    "origin",
                    new_branch,
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )
                if verify_delete_remote_result.returncode == 0:
                    raise RuntimeError("Source branch still exists on origin after delete attempt")
                if await _git_ref_exists(repo_dir, f"refs/heads/{new_branch}", env, logs):
                    _log_debug(logs, f"Deleting merged source branch '{new_branch}' locally.")
                    delete_local_result = await run_git_command(
                        "branch",
                        "-d",
                        new_branch,
                        cwd=repo_dir,
                        env=env,
                        log=logs,
                    )
                    if delete_local_result.returncode != 0:
                        raise RuntimeError("Failed to delete local source branch")
                logs.append(_timestamped("Branch merge completed, pushed, and source branch deleted."))
                success = True
                return {"form_values": form_values, "success": success}
            elif branch and not repo_prepared:
                _log_debug(logs, f"Checking out branch '{branch}'.")
                checkout_result = await run_git_command(
                    "checkout",
                    branch,
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )
                if checkout_result.returncode != 0:
                    logs.append(_timestamped(f"Branch {branch} not found. Creating new branch."))
                    _log_debug(logs, f"Creating new branch '{branch}'.")
                    create_branch_result = await run_git_command(
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
                    pull_result = await run_git_command(
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
                await run_git_command(
                    "status",
                    "-sb",
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )
                _log_debug(logs, "Pulling latest changes for default branch.")
                pull_result = await run_git_command(
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

                await _apply_patch_with_line_ending_retry(
                    repo_dir,
                    patch_path,
                    patch_content,
                    env,
                    logs,
                )
                _log_debug(logs, "Patch applied successfully.")
            else:
                logs.append(_timestamped("No patch provided; skipping git apply."))
                _log_debug(logs, "Patch skipped because content is empty.")

            _log_debug(logs, "Staging changes with git add -A.")
            await run_git_command(
                "add",
                "-A",
                cwd=repo_dir,
                env=env,
                log=logs,
            )

            _log_debug(logs, "Checking git status after staging.")
            await run_git_command(
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
                    "commit",
                ]
                if allow_empty_commit:
                    commit_command.append("--allow-empty")
                commit_command.extend(["-F", str(commit_file)])
                commit_result = await run_git_command(
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
                push_result = await run_git_command(
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


async def frontend_handler(request: web.Request) -> web.Response:
    frontend_root = request.app["frontend_root"]
    return web.FileResponse(frontend_root / "index.html")


def _sse_payload(message: Dict[str, object]) -> str:
    return f"data: {json.dumps(message, ensure_ascii=False)}\n\n"


async def sse_handler(request: web.Request) -> web.StreamResponse:
    client_id = request.query.get("client_id", "").strip()
    if not client_id:
        raise web.HTTPBadRequest(text="client_id query parameter is required")

    sse_headers = {
        **_build_cors_headers(request),
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
    }

    stream = web.StreamResponse(
        status=200,
        headers=sse_headers,
    )
    await stream.prepare(request)

    queue: asyncio.Queue[Dict[str, object]] = asyncio.Queue()
    clients: Dict[str, asyncio.Queue[Dict[str, object]]] = request.app["sse_clients"]
    clients[client_id] = queue

    try:
        await stream.write(_sse_payload({"type": "connected"}).encode("utf-8"))
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=15)
                await stream.write(_sse_payload(message).encode("utf-8"))
            except asyncio.TimeoutError:
                await stream.write(b": keepalive\n\n")
            except (ConnectionResetError, asyncio.CancelledError):
                break
    except ConnectionResetError:
        pass
    finally:
        if clients.get(client_id) is queue:
            del clients[client_id]
    return stream


async def _emit_to_client(app: web.Application, client_id: str, message: Dict[str, object]) -> None:
    queue: Optional[asyncio.Queue[Dict[str, object]]] = app["sse_clients"].get(client_id)
    if queue is None:
        return
    await queue.put(message)


async def _read_json_payload(request: web.Request) -> Dict[str, object]:
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text="Invalid JSON payload")
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text="Invalid JSON payload")
    return payload


async def submit_handler(request: web.Request) -> web.Response:
    payload = request.get("forced_payload")
    if payload is None:
        payload = await _read_json_payload(request)
    form_data = payload.get("payload", {})
    client_id = str(payload.get("client_id", "")).strip()
    if not isinstance(form_data, dict):
        raise web.HTTPBadRequest(text="Invalid form payload")
    if not client_id:
        raise web.HTTPBadRequest(text="client_id is required")

    logs = LogSink(entries=[], emit=lambda message: _emit_to_client(request.app, client_id, message))
    result = await process_submission(_normalize_form_payload(form_data), logs)
    await _emit_to_client(request.app, client_id, {"type": "complete", "success": result["success"]})
    return web.json_response({"accepted": True})


def _with_branch_mode(payload: Dict[str, object], branch_mode: str) -> Dict[str, object]:
    merged = dict(payload)
    form_payload = merged.get("payload", {})
    if not isinstance(form_payload, dict):
        raise web.HTTPBadRequest(text="Invalid form payload")
    normalized_payload = dict(form_payload)
    normalized_payload["branch_mode"] = branch_mode
    merged["payload"] = normalized_payload
    return merged


async def submit_default_handler(request: web.Request) -> web.Response:
    payload = await _read_json_payload(request)
    request["forced_payload"] = _with_branch_mode(payload, "default")
    return await submit_handler(request)


async def submit_from_commit_handler(request: web.Request) -> web.Response:
    payload = await _read_json_payload(request)
    request["forced_payload"] = _with_branch_mode(payload, "from_commit")
    return await submit_handler(request)


async def submit_orphan_handler(request: web.Request) -> web.Response:
    payload = await _read_json_payload(request)
    request["forced_payload"] = _with_branch_mode(payload, "orphan")
    return await submit_handler(request)


async def submit_revert_to_commit_handler(request: web.Request) -> web.Response:
    payload = await _read_json_payload(request)
    request["forced_payload"] = _with_branch_mode(payload, "revert_to_commit")
    return await submit_handler(request)


async def submit_merge_branches_handler(request: web.Request) -> web.Response:
    payload = await _read_json_payload(request)
    request["forced_payload"] = _with_branch_mode(payload, "merge_branches")
    return await submit_handler(request)


async def submit_mirror_repository_handler(request: web.Request) -> web.Response:
    payload = await _read_json_payload(request)
    request["forced_payload"] = _with_branch_mode(payload, "mirror_repository")
    return await submit_handler(request)


async def config_handler(_: web.Request) -> web.Response:
    return web.json_response({"payload": _serialize_config(await _get_github_users())})


async def health_handler(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def close_sse_clients(app: web.Application) -> None:
    app["sse_clients"].clear()


def create_app(serve_frontend: bool = True) -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app["sse_clients"] = {}
    app.on_shutdown.append(close_sse_clients)
    app.router.add_route("GET", SSE_PATH, sse_handler)
    app.router.add_route("POST", "/submit", submit_default_handler)
    app.router.add_route("POST", "/submit/from_commit", submit_from_commit_handler)
    app.router.add_route("POST", "/submit/orphan", submit_orphan_handler)
    app.router.add_route("POST", "/submit/revert_to_commit", submit_revert_to_commit_handler)
    app.router.add_route("POST", "/submit/merge_branches", submit_merge_branches_handler)
    app.router.add_route("POST", "/submit/mirror_repository", submit_mirror_repository_handler)
    app.router.add_route("GET", "/config", config_handler)
    app.router.add_route("GET", "/health", health_handler)
    if serve_frontend:
        frontend_root = _frontend_root()
        if not frontend_root.exists():
            raise RuntimeError(f"Frontend root not found at {frontend_root}")
        app["frontend_root"] = frontend_root
        app.router.add_route("GET", "/", frontend_handler)
        app.router.add_route("GET", "/index.html", frontend_handler)
    else:
        app.router.add_route("GET", "/", sse_handler)
    return app


if __name__ == "__main__":
    args = _parse_args()
    _configure_runtime(args.config, args.repo_root, args.keep_temp)
    try:
        listen_config = _resolve_server_listen_config(
            bind_override=args.bind,
            port_override=args.port,
            unix_socket=args.unix_socket,
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    web.run_app(
        create_app(serve_frontend=args.serve_frontend),
        host=listen_config.host,
        port=listen_config.port,
        path=listen_config.path,
        max_line_size=MAX_LINE_SIZE,
    )
