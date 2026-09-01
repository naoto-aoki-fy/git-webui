from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import shutil
import tempfile
import secrets
import tomllib
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Set, TypedDict
import traceback

from aiohttp import web

from .settings import Settings
from .web.keys import FORCED_PAYLOAD_KEY, FRONTEND_ROOT_KEY, GITHUB_REFRESH_TASK_KEY, SSE_CLIENTS_KEY, SUBMISSIONS_KEY

SSE_PATH = "/events"

DEFAULT_CONFIG_PATH = Path("config.toml")
DEFAULT_BIND = "0.0.0.0"
DEFAULT_PORT = 8080
MAX_LINE_SIZE = 32 * 1024
GITHUB_CACHE_FILENAME = ".github-list-cache.json"


@dataclass(frozen=True)
class ServerListenConfig:
    host: Optional[str]
    port: Optional[int]
    path: Optional[str]


class AddFileEntry(TypedDict):
    path: str
    content: str
    overwrite: bool


DEVNULL = "NUL" if os.name == "nt" else "/dev/null"
GIT_COMMON_OPTIONS = ("-c", "core.hooksPath=" + DEVNULL)
KEEP_TEMP = False
DEFAULT_REPO_ROOT = Path("repos")
REPO_ROOT = DEFAULT_REPO_ROOT.expanduser()
CONFIG_PATH = DEFAULT_CONFIG_PATH
RUNTIME_SETTINGS: Settings | None = None

def _load_config(config_path: Path) -> Dict[str, object]:
    if not config_path.exists():
        return {"repository_prefix_destinations": [], "git_user_defaults": []}

    with config_path.open("rb") as config_file:
        try:
            data = tomllib.load(config_file)
        except tomllib.TOMLDecodeError as exc:  # noqa: BLE001
            raise RuntimeError(f"Failed to parse configuration file {config_path}: {exc}") from exc

    git_user_defaults = data.get("git_user_defaults", [])
    if not isinstance(git_user_defaults, list):
        raise RuntimeError("Configuration file must define 'git_user_defaults' as a list")
    for entry in git_user_defaults:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("repository"), str)
            or not isinstance(entry.get("github_user"), str)
        ):
            raise RuntimeError("Each git_user_defaults entry must define repository and github_user")

    destinations = data.get("repository_prefix_destinations", [])
    if not isinstance(destinations, list):
        raise RuntimeError("Configuration file must define 'repository_prefix_destinations' as an array of pairs")
    for entry in destinations:
        if (
            not isinstance(entry, list)
            or len(entry) != 2
            or not all(isinstance(value, str) and value.strip() for value in entry)
        ):
            raise RuntimeError("Each repository_prefix_destinations entry must be a [prefix, destination_owner] pair")

    return {
        "repository_prefix_destinations": destinations,
        # Kept in the response during the deprecation period for old clients.
        "git_user_defaults": git_user_defaults,
    }


APP_CONFIG = {"repository_prefix_destinations": [], "git_user_defaults": []}


def configure(settings: Settings) -> None:
    """Install settings for legacy workflow functions during staged extraction."""
    global APP_CONFIG, CONFIG_PATH, KEEP_TEMP, REPO_ROOT, RUNTIME_SETTINGS
    RUNTIME_SETTINGS = settings
    CONFIG_PATH = settings.config_path
    REPO_ROOT = settings.repository_root
    KEEP_TEMP = settings.keep_temporary_workspaces
    APP_CONFIG = {
        "repository_prefix_destinations": [list(item) for item in settings.repository_prefix_destinations],
        "git_user_defaults": [
            {"repository": repository, "github_user": user}
            for repository, user in settings.git_user_defaults
        ],
    }


def _resolve_cors_allow_origin() -> List[str]:
    if RUNTIME_SETTINGS is not None:
        return list(RUNTIME_SETTINGS.cors_allowed_origins)
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


def _write_repo_file(
    repo_dir: Path,
    relative_path: str,
    content: str,
    overwrite: bool,
) -> Path:
    """Write a UTF-8 file inside a repository, optionally replacing it."""
    file_path = _safe_repo_file_path(repo_dir, relative_path)
    if file_path is None or not relative_path.strip() or Path(relative_path) == Path("."):
        raise RuntimeError("File path must be a relative path inside the repository.")
    if file_path.exists() and not overwrite:
        raise RuntimeError(f"File already exists and overwrite is disabled: {relative_path}")
    if file_path.exists() and not file_path.is_file():
        raise RuntimeError(f"File path does not identify a regular file: {relative_path}")
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8", newline="")
    return file_path


def _parse_add_files(form: Dict[str, str]) -> List[AddFileEntry]:
    """Return non-empty add-file entries, accepting the legacy single-file fields."""
    raw_files = form.get("files", "")
    if raw_files:
        try:
            decoded = json.loads(raw_files)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Files must be a valid JSON array.") from exc
        if not isinstance(decoded, list):
            raise RuntimeError("Files must be a valid JSON array.")
        files: List[AddFileEntry] = []
        for entry in decoded:
            if not isinstance(entry, dict):
                raise RuntimeError("Each file must define a path, content, and overwrite option.")
            path = entry.get("path", "")
            content = entry.get("content", "")
            overwrite = entry.get("overwrite", False)
            if not isinstance(path, str) or not isinstance(content, str) or not isinstance(overwrite, bool):
                raise RuntimeError("Each file must define a path, content, and overwrite option.")
            path = path.strip()
            content = content.replace("\r\n", "\n")
            if path and content:
                files.append({"path": path, "content": content, "overwrite": overwrite})
        return files

    path = form.get("file_path", "").strip()
    content = form.get("file_content", "").replace("\r\n", "\n")
    if not path or not content:
        return []
    return [{"path": path, "content": content, "overwrite": form.get("overwrite") == "true"}]


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


class GitCommandError(RuntimeError):
    """Raised when a Git command that must succeed returns a nonzero status."""

    def __init__(self, args: tuple[str, ...], result: CommandResult) -> None:
        self.command_args = args
        self.result = result
        command = shlex.join(("git", *args))
        detail = result.stderr.strip() or result.stdout.strip()
        message = f"{command} failed with exit code {result.returncode}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


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
    try:
        stdout, stderr = await process.communicate()
    except asyncio.CancelledError:
        # Cancelling a submission must also stop the command it is currently
        # running; otherwise git could continue through a push after the UI
        # reports that the operation was stopped.
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.communicate(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
        raise
    stdout_text = stdout.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")
    if stdout_text and log is not None:
        log.append(_timestamped(stdout_text.rstrip()))
    if stderr_text and log is not None:
        log.append(_timestamped(stderr_text.rstrip()))
    if log is not None:
        log.append(_timestamped(f"exit code: {process.returncode}"))
    return CommandResult(process.returncode, stdout_text, stderr_text)


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

    return await run_command("git", *GIT_COMMON_OPTIONS, *git_args, cwd=cwd, env=env, log=log)


async def run_git_checked(
    *cmd: str,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    log: Optional[LogSink] = None,
) -> CommandResult:
    """Run a Git command and raise a diagnostic error if it fails."""
    result = await run_git_command(*cmd, cwd=cwd, env=env, log=log)
    if result.returncode != 0:
        raise GitCommandError(cmd, result)
    return result


async def _clean_orphan_worktree(
    repo_dir: Path,
    env: Dict[str, str],
    logs: Optional[LogSink] = None,
) -> None:
    """Remove tracked and untracked files and verify an orphan tree is empty."""
    await run_git_checked(
        "rm", "-rf", "--ignore-unmatch", ".", cwd=repo_dir, env=env, log=logs
    )
    await run_git_checked("clean", "-fd", cwd=repo_dir, env=env, log=logs)
    status = await run_git_checked(
        "status", "--porcelain", "--untracked-files=all", cwd=repo_dir, env=env, log=logs
    )
    if status.stdout.strip():
        raise RuntimeError("Orphan worktree is not empty after cleanup")


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


async def _resolve_branch_creation_base(
    repo_dir: Path,
    base: str,
    env: Dict[str, str],
    logs: Optional[LogSink] = None,
) -> str:
    """Resolve a remote branch name while leaving commit-ish values unchanged."""
    remote_ref = f"refs/remotes/origin/{base}"
    if await _git_ref_exists(repo_dir, remote_ref, env, logs):
        resolved = f"origin/{base}"
        _log_debug(logs, f"Resolved base branch '{base}' to '{resolved}'.")
        return resolved
    return base


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


async def _delete_all_local_branches(
    repo_dir: Path,
    env: Dict[str, str],
    logs: Optional[LogSink] = None,
) -> None:
    _log_debug(logs, "Deleting all local branches before orphan branch creation.")
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
    if not branches:
        return

    detach_result = await run_git_command(
        "checkout",
        "--detach",
        cwd=repo_dir,
        env=env,
        log=logs,
    )
    if detach_result.returncode != 0:
        raise RuntimeError("Failed to detach HEAD before deleting local branches")

    for branch in branches:
        _log_debug(logs, f"Deleting local branch '{branch}' before orphan creation.")
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
    return Path(__file__).resolve().parents[2] / "frontend"


def _repo_workspace_for_url(repository_url: str) -> Path:
    repo_name = Path(repository_url.rstrip("/")).name
    repo_name = repo_name[:-4] if repo_name.endswith(".git") else repo_name
    repo_name = re.sub(r"[^A-Za-z0-9._-]+", "-", repo_name).strip("-") or "repo"
    digest = hashlib.sha256(repository_url.encode("utf-8")).hexdigest()[:10]
    return REPO_ROOT / f"{repo_name}-{digest}.git"


async def _ensure_mirror_cache(
    repository_url: str,
    env: Dict[str, str],
    logs: Optional[LogSink] = None,
) -> Path:
    """Create an immutable bare mirror used only as a clone seed."""
    mirror_dir = _repo_workspace_for_url(repository_url)
    if mirror_dir.exists():
        bare_result = await run_git_command(
            "rev-parse", "--is-bare-repository", cwd=mirror_dir, env=env, log=logs
        )
        if bare_result.returncode != 0 or bare_result.stdout.strip() != "true":
            raise RuntimeError(f"Existing repository cache is not a bare mirror: {mirror_dir}")
        _log_debug(logs, f"Using bare mirror cache at {mirror_dir}.")
        return mirror_dir

    mirror_dir.parent.mkdir(parents=True, exist_ok=True)
    candidate = Path(tempfile.mkdtemp(prefix=f".{mirror_dir.name}-", dir=mirror_dir.parent))
    # git clone requires the destination not to exist.
    candidate.rmdir()
    try:
        if logs is not None:
            logs.append(_timestamped(f"Creating bare mirror cache for {repository_url}"))
        clone_result = await run_git_command(
            "clone", "--mirror", repository_url, str(candidate), env=env, log=logs
        )
        if clone_result.returncode != 0:
            raise RuntimeError("git clone --mirror failed while creating repository cache")
        try:
            candidate.rename(mirror_dir)
        except OSError:
            # Another process may have won the atomic publication race. Its
            # completed mirror is equivalent; never mutate either cache.
            if not mirror_dir.exists():
                raise
        _log_debug(logs, f"Bare mirror cache is ready at {mirror_dir}.")
        return mirror_dir
    finally:
        if candidate.exists():
            shutil.rmtree(candidate)


async def _clone_isolated_repository(
    repository_url: str,
    destination: Path,
    env: Dict[str, str],
    logs: Optional[LogSink] = None,
    *,
    mirror: bool = False,
) -> None:
    """Clone a cache seed and refresh only the operation-local repository."""
    cache_dir = await _ensure_mirror_cache(repository_url, env, logs)
    clone_args = ["clone"]
    if mirror:
        clone_args.append("--mirror")
    clone_args.extend([str(cache_dir), str(destination)])
    clone_result = await run_git_command(*clone_args, env=env, log=logs)
    if clone_result.returncode != 0:
        raise RuntimeError("Failed to create isolated repository clone")

    set_url_result = await run_git_command(
        "remote", "set-url", "origin", repository_url,
        cwd=destination, env=env, log=logs,
    )
    if set_url_result.returncode != 0:
        raise RuntimeError("Failed to configure isolated repository origin")
    fetch_result = await run_git_command(
        "remote", "update", "--prune", cwd=destination, env=env, log=logs
    )
    if fetch_result.returncode != 0:
        raise RuntimeError("Failed to refresh isolated repository clone")


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
        return
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


def _repository_owner(value: str) -> str:
    normalized = _normalize_repository_identifier(value)
    if not normalized:
        return ""
    parts = [part for part in normalized.split("/") if part]
    if len(parts) >= 3:
        return parts[1]
    if len(parts) >= 2:
        return parts[0]
    return parts[0] if parts else ""


async def _github_accounts(logs: Optional[LogSink] = None) -> tuple[List[str], Optional[str]]:
    """Return authenticated GitHub logins and the currently active login."""
    try:
        result = await run_command("gh", "auth", "status", "--json", "hosts", log=logs)
    except OSError as exc:
        raise RuntimeError("Unable to run gh auth status; is GitHub CLI installed?") from exc
    if result.returncode != 0:
        raise RuntimeError("Unable to retrieve GitHub users with gh auth status")
    try:
        hosts = json.loads(result.stdout).get("hosts", {})
        entries = hosts.get("github.com", [])
    except (json.JSONDecodeError, AttributeError):
        raise RuntimeError("Invalid response from gh auth status") from None
    users: List[str] = []
    active: Optional[str] = None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("login"), str):
            continue
        login = entry["login"]
        if login not in users:
            users.append(login)
        if entry.get("active") is True:
            active = login
    return users, active


async def _github_repositories(
    github_users: List[str],
    logs: Optional[LogSink] = None,
) -> List[Dict[str, str]]:
    """List repositories for each account without changing gh's active account."""
    repositories: List[Dict[str, str]] = []
    seen: Set[str] = set()
    for username in github_users:
        token_result = await run_command("gh", "auth", "token", "--user", username)
        if token_result.returncode != 0 or not token_result.stdout.strip():
            raise RuntimeError(f"Unable to retrieve GitHub token for {username}")
        env = os.environ.copy()
        env["GH_TOKEN"] = token_result.stdout.strip()
        result = await run_command(
            "gh", "repo", "list", "--limit", "1000", "--json", "nameWithOwner", env=env, log=logs
        )
        if result.returncode != 0:
            raise RuntimeError(f"Unable to list GitHub repositories for {username}")
        try:
            entries = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"Invalid repository list returned for {username}") from None
        if not isinstance(entries, list):
            raise RuntimeError(f"Invalid repository list returned for {username}")
        for entry in entries:
            full_name = entry.get("nameWithOwner") if isinstance(entry, dict) else None
            if not isinstance(full_name, str) or full_name.count("/") != 1:
                continue
            owner, name = full_name.split("/", 1)
            normalized = full_name.lower()
            if owner and name and normalized not in seen:
                seen.add(normalized)
                repositories.append({"owner": owner, "name": name})
    return repositories


async def _github_user_profiles(
    github_users: List[str], logs: Optional[LogSink] = None
) -> List[Dict[str, object]]:
    """Retrieve commit identities from GitHub for authenticated CLI accounts."""
    profiles: List[Dict[str, object]] = []
    for username in github_users:
        token_result = await run_command("gh", "auth", "token", "--user", username)
        if token_result.returncode != 0 or not token_result.stdout.strip():
            raise RuntimeError(f"Unable to retrieve GitHub token for {username}")
        env = os.environ.copy()
        env["GH_TOKEN"] = token_result.stdout.strip()
        result = await run_command(
            "gh", "api", "user", "--jq", "{id: .id, login: .login, name: .name}",
            env=env, log=logs,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Unable to retrieve GitHub profile for {username}")
        try:
            profile = json.loads(result.stdout)
            user_id = profile["id"]
            login = profile["login"]
        except (json.JSONDecodeError, KeyError, TypeError):
            raise RuntimeError(f"Invalid GitHub profile returned for {username}") from None
        if not isinstance(user_id, int) or not isinstance(login, str):
            raise RuntimeError(f"Invalid GitHub profile returned for {username}")
        name = profile.get("name")
        profiles.append(
            {
                "id": user_id,
                "login": login,
                "name": name.strip() if isinstance(name, str) and name.strip() else login,
                "email": f"{user_id}+{login}@users.noreply.github.com",
            }
        )
    return profiles


def _github_cache_path() -> Path:
    return REPO_ROOT / GITHUB_CACHE_FILENAME


def _read_github_cache() -> Optional[Dict[str, object]]:
    """Return a valid GitHub listing cache, or None when it must be refreshed."""
    try:
        cache = json.loads(_github_cache_path().read_text(encoding="utf-8"))
        refreshed_at = datetime.fromisoformat(cache["refreshed_at"])
        if refreshed_at.tzinfo is None:
            return None
        users = cache["github_users"]
        repositories = cache["github_repositories"]
        active_user = cache.get("active_github_user")
        if not isinstance(users, list) or not all(
            isinstance(user, dict)
            and isinstance(user.get("id"), int)
            and isinstance(user.get("login"), str)
            and isinstance(user.get("name"), str)
            and isinstance(user.get("email"), str)
            for user in users
        ):
            return None
        if active_user is not None and not isinstance(active_user, str):
            return None
        if not isinstance(repositories, list) or not all(
            isinstance(repo, dict)
            and isinstance(repo.get("owner"), str)
            and isinstance(repo.get("name"), str)
            for repo in repositories
        ):
            return None
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    return {
        "github_users": users,
        "active_github_user": active_user,
        "github_repositories": repositories,
        "github_cache_refreshed_at": refreshed_at.isoformat(),
    }


def _write_github_cache(payload: Dict[str, object], now: Optional[datetime] = None) -> None:
    cache_path = _github_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache = {
        "refreshed_at": (now or datetime.now(UTC)).isoformat(),
        "github_users": payload["github_users"],
        "active_github_user": payload["active_github_user"],
        "github_repositories": payload["github_repositories"],
    }
    temporary_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    temporary_path.replace(cache_path)


async def _github_listing(force_refresh: bool = False) -> Dict[str, object]:
    if not force_refresh:
        cached = _read_github_cache()
        if cached is not None:
            return cached

    github_accounts, active_user = await _github_accounts()
    github_users = await _github_user_profiles(github_accounts)
    repositories = await _github_repositories(github_accounts)
    refreshed_at = datetime.now(UTC)
    payload: Dict[str, object] = {
        "github_users": github_users,
        "active_github_user": active_user,
        "github_repositories": repositories,
        "github_cache_refreshed_at": refreshed_at.isoformat(),
    }
    _write_github_cache(payload, refreshed_at)
    return payload


def _display_label(entry: Dict[str, str], fallback: str) -> str:
    label = entry.get("label")
    if isinstance(label, str) and label.strip():
        return label
    return fallback


def _serialize_config() -> Dict[str, object]:
    return {
        "repository_prefix_destinations": APP_CONFIG.get("repository_prefix_destinations", []),
        "git_user_defaults": APP_CONFIG.get("git_user_defaults", []),
    }




def _normalize_form_payload(form: Dict[str, str]) -> Dict[str, str]:
    normalized = {}
    for key, value in form.items():
        normalized[key] = value if isinstance(value, str) else str(value)
    return normalized


def _github_repository_url(owner: str, name: str) -> str:
    """Build a GitHub HTTPS clone URL from separately supplied identifiers."""
    owner = owner.strip()
    name = name.strip()
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", owner):
        raise ValueError("Repository owner contains invalid characters.")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ValueError("Repository name contains invalid characters.")
    return f"https://github.com/{owner}/{name}.git"


def _additional_repository(repository_name: str) -> Optional[tuple[str, str]]:
    """Return the first configured destination owner/name for a repository."""
    normalized_name = repository_name.lower()
    for prefix, owner in APP_CONFIG.get("repository_prefix_destinations", []):
        if normalized_name.startswith(prefix.strip().lower()):
            destination_name = repository_name[len(prefix.strip()):]
            if not destination_name:
                raise ValueError("A repository prefix mapping cannot produce an empty destination repository name.")
            return owner.strip(), destination_name
    return None


async def _push_to_remotes(
    repo_dir: Path,
    env: Dict[str, str],
    logs: LogSink,
    repository_name: str,
    *arguments: str,
) -> None:
    destinations = [("origin", "origin")]
    additional = _additional_repository(repository_name)
    if additional:
        owner, name = additional
        destinations.append((f"{owner}/{name}", _github_repository_url(owner, name)))

    if len(destinations) > 1:
        for label, remote in destinations[1:]:
            _log_debug(logs, f"Dry-running push to {label}.")
            result = await run_git_command(
                "push", "--dry-run", remote, *arguments, cwd=repo_dir, env=env, log=logs
            )
            if result.returncode != 0:
                raise RuntimeError(f"git push --dry-run to {label} failed")

    for label, remote in destinations:
        _log_debug(logs, f"Pushing to {label}.")
        result = await run_git_command("push", remote, *arguments, cwd=repo_dir, env=env, log=logs)
        if result.returncode != 0:
            raise RuntimeError(f"git push to {label} failed")


async def _verify_reset_commit_on_branch(
    repo_dir: Path,
    env: Dict[str, str],
    logs: LogSink,
    commit_id: str,
    branch: str,
    skip_check: bool = False,
) -> None:
    """Ensure a hard-reset target is already reachable from the remote branch."""
    if skip_check:
        _log_debug(logs, "Skipping the reset commit containment check at the user's request.")
        return
    remote_branch = f"origin/{branch}"
    _log_debug(
        logs,
        f"Checking whether commit '{commit_id}' is contained in branch '{remote_branch}'.",
    )
    ancestor_result = await run_git_command(
        "merge-base",
        "--is-ancestor",
        commit_id,
        remote_branch,
        cwd=repo_dir,
        env=env,
        log=logs,
    )
    if ancestor_result.returncode != 0:
        raise RuntimeError(
            f"Commit '{commit_id}' is not contained in branch '{branch}'; hard reset aborted"
        )


async def _format_reset_backup_branch(
    repo_dir: Path,
    env: Dict[str, str],
    logs: LogSink,
    branch: str,
    template: str,
) -> str:
    """Expand a reset-backup branch template using the original remote tip."""
    remote_branch = f"origin/{branch}"
    commit_result = await run_git_command(
        "rev-parse", "--short=7", remote_branch, cwd=repo_dir, env=env, log=logs
    )
    date_result = await run_git_command(
        "show", "-s", "--format=%cI", remote_branch, cwd=repo_dir, env=env, log=logs
    )
    if commit_result.returncode != 0 or date_result.returncode != 0:
        raise RuntimeError("Failed to read reset backup branch metadata")

    try:
        committer_date = datetime.fromisoformat(date_result.stdout.strip())
        backup_branch = template.format(
            commit_id=commit_result.stdout.strip(),
            branch=branch,
            date=committer_date.strftime("%Y-%m%d-%H%M"),
        )
    except (KeyError, ValueError) as exc:
        raise RuntimeError(f"Invalid backup branch name format: {exc}") from exc

    check_result = await run_git_command(
        "check-ref-format", "--branch", backup_branch, cwd=repo_dir, env=env, log=logs
    )
    if check_result.returncode != 0:
        raise RuntimeError(f"Invalid backup branch name '{backup_branch}'")
    return backup_branch


async def process_submission(form: Dict[str, str], logs: LogSink) -> Dict[str, object]:
    _log_debug(logs, "Received submission payload.")
    repository_owner = form.get("repository_owner", "").strip()
    repository_name = form.get("repository_name", "").strip()
    mirror_repository_owner = form.get("mirror_repository_owner", "").strip()
    mirror_repository_name = form.get("mirror_repository_name", "").strip()
    try:
        repository_url = _github_repository_url(repository_owner, repository_name) if repository_owner or repository_name else form.get("repository_url", "").strip()
        mirror_repository_url = _github_repository_url(mirror_repository_owner, mirror_repository_name) if mirror_repository_owner or mirror_repository_name else form.get("mirror_repository_url", "").strip()
    except ValueError as exc:
        logs.append(_timestamped(str(exc)))
        return {"form_values": dict(form), "success": False}
    if not repository_name and repository_url:
        repository_name = Path(repository_url.removesuffix(".git").rstrip("/")).name
    branch = form.get("branch", "").strip()
    new_branch = form.get("new_branch", "").strip()
    git_user_selection = form.get("git_user", "").strip()
    branch_mode = form.get("branch_mode", "default").strip()
    sync_push_option = form.get("sync_push_option", "all").strip()
    base_commit = form.get("base_commit", "").strip()
    skip_reset_commit_check = form.get("skip_reset_commit_check") == "true"
    backup_before_reset = form.get("backup_before_reset") == "true"
    backup_branch_format = form.get(
        "backup_branch_format", "archive/{branch}-{date}-{commit_id}"
    ).strip()
    commit_message = form.get("commit_message", "").replace("\r\n", "\n")
    commit_message = commit_message.strip("\n")
    allow_empty_commit = form.get("allow_empty_commit") == "true"
    patch_content = form.get("patch", "").replace("\r\n", "\n")
    try:
        add_files = _parse_add_files(form)
    except RuntimeError as exc:
        logs.append(_timestamped(str(exc)))
        return {"form_values": dict(form), "success": False}
    if branch_mode in {"from_commit", "revert_to_commit", "mirror_repository"}:
        commit_message = ""
        allow_empty_commit = False
        patch_content = ""

    _log_debug(logs, f"Parsed repository_url='{repository_url}'.")
    _log_debug(logs, f"Parsed mirror_repository_url='{mirror_repository_url or '(none)'}'.")
    _log_debug(logs, f"Parsed branch='{branch or '(default)'}'.")
    _log_debug(logs, f"Parsed new_branch='{new_branch or '(none)'}'.")
    _log_debug(logs, f"Parsed branch_mode='{branch_mode}'.")
    _log_debug(logs, f"Parsed sync_push_option='{sync_push_option}'.")
    _log_debug(logs, f"Parsed base_commit='{base_commit or '(none)'}'.")
    _log_debug(logs, f"Parsed git_user selection='{git_user_selection or '(none)'}'.")
    _log_debug(logs, f"Commit message length={len(commit_message)}.")
    _log_debug(logs, f"Allow empty commit={allow_empty_commit}.")
    _log_debug(logs, f"Patch length={len(patch_content)}.")
    _log_debug(logs, f"Parsed {len(add_files)} non-empty file(s).")

    target_branch = new_branch if branch_mode in {"from_commit", "orphan"} else (branch if branch_mode in {"default", "add_file", "merge_branches"} else None)
    form_values = {
        "repository_url": repository_url,
        "mirror_repository_url": mirror_repository_url,
        "branch": branch,
        "new_branch": new_branch,
        "commit_message": commit_message,
        "allow_empty_commit": "true" if allow_empty_commit else "",
        "git_user_selection": git_user_selection,
        "branch_mode": branch_mode,
        "sync_push_option": sync_push_option,
        "base_commit": base_commit,
        "skip_reset_commit_check": "true" if skip_reset_commit_check else "",
        "backup_before_reset": "true" if backup_before_reset else "",
        "backup_branch_format": backup_branch_format,
        "files": json.dumps(add_files),
    }

    user_name = ""
    user_email = ""
    if git_user_selection:
        listing = await _github_listing()
        user_entry = next(
            (
                entry for entry in listing["github_users"]
                if entry["login"].lower() == git_user_selection.lower()
            ),
            None,
        )
        if user_entry is None:
            logs.append(_timestamped("Invalid Git user selection."))
            return {"form_values": form_values, "success": False}
        user_name = str(user_entry["name"]).strip()
        user_email = str(user_entry["email"]).strip()
        _log_debug(logs, f"Resolved GitHub user '{git_user_selection}' as '{user_name}'.")

    _log_debug(logs, "Validated git user selection.")
    success = False

    if not repository_url:
        _log_debug(logs, "Repository URL missing.")
        logs.append(_timestamped("Repository URL is required."))
        return {"form_values": form_values, "success": False}
    if repository_url.startswith(("git@", "ssh://")) or mirror_repository_url.startswith(("git@", "ssh://")):
        logs.append(_timestamped("SSH repository URLs are not supported. Use an HTTPS URL."))
        return {"form_values": form_values, "success": False}
    repository_urls = [repository_url]
    if mirror_repository_url:
        repository_urls.append(mirror_repository_url)
    if any("://" in url and not url.lower().startswith("https://") for url in repository_urls):
        logs.append(_timestamped("Remote repository URLs must use HTTPS."))
        return {"form_values": form_values, "success": False}

    if branch_mode == "add_file" and not add_files:
        logs.append(_timestamped("At least one file with a non-empty path and content is required in add-file mode."))
        return {"form_values": form_values, "success": False}
    if branch_mode not in {"add_file", "from_commit", "revert_to_commit", "merge_branches", "mirror_repository"} and not patch_content.strip() and not allow_empty_commit:
        _log_debug(logs, "Patch content missing or whitespace.")
        logs.append(_timestamped("Patch content is required unless empty commit is allowed."))
        return {"form_values": form_values, "success": False}
    if branch_mode == "mirror_repository" and not mirror_repository_url:
        _log_debug(logs, "Repository B URL missing for sync mode.")
        logs.append(_timestamped("Repository B URL is required for sync mode."))
        return {"form_values": form_values, "success": False}
    if branch_mode == "mirror_repository" and repository_url == mirror_repository_url:
        _log_debug(logs, "Repository A and Repository B are identical in sync mode.")
        logs.append(_timestamped("Repository A and Repository B must be different for sync mode."))
        return {"form_values": form_values, "success": False}
    if branch_mode == "mirror_repository" and sync_push_option not in {"all", "branch", "mirror"}:
        _log_debug(logs, "Invalid sync push option.")
        logs.append(_timestamped("Sync method must be all branches, a single branch, or mirror all refs."))
        return {"form_values": form_values, "success": False}
    if branch_mode == "mirror_repository" and sync_push_option == "branch" and not branch:
        _log_debug(logs, "Branch name missing for single-branch sync.")
        logs.append(_timestamped("Branch is required for single-branch sync."))
        return {"form_values": form_values, "success": False}
    if branch_mode == "from_commit" and base_commit and base_commit.upper() == "HEAD":
        _log_debug(logs, "Base commit set to HEAD for branch creation; will resolve to default branch.")
    if branch_mode in {"default", "add_file"} and not branch:
        _log_debug(logs, "Branch name missing.")
        logs.append(_timestamped("Branch is required."))
        return {"form_values": form_values, "success": False}
    if branch_mode == "revert_to_commit" and not branch:
        _log_debug(logs, "Branch name missing for revert mode.")
        logs.append(_timestamped("Branch is required for revert mode."))
        return {"form_values": form_values, "success": False}
    if branch_mode == "revert_to_commit" and not base_commit:
        _log_debug(logs, "Commit ID missing for revert mode.")
        logs.append(_timestamped("Commit ID is required for revert mode."))
        return {"form_values": form_values, "success": False}
    if branch_mode == "revert_to_commit" and backup_before_reset and not backup_branch_format:
        logs.append(_timestamped("Backup branch name format is required when backup is enabled."))
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
            repo_dir = workdir / "repository"
            env = os.environ.copy()
            _log_debug(logs, f"Created temporary workspace at {workdir}.")
            _log_debug(logs, f"Isolated repository directory will be {repo_dir}.")

            if branch_mode == "mirror_repository":
                mirror_dir = workdir / "mirror.git"
                logs.append(
                    _timestamped(f"Creating an isolated mirror of Repository A: {repository_url}")
                )
                await _clone_isolated_repository(
                    repository_url, mirror_dir, env, logs, mirror=True
                )
                push_arguments = {
                    "all": ["--all", mirror_repository_url],
                    "branch": [mirror_repository_url, f"refs/heads/{branch}:refs/heads/{branch}"],
                    "mirror": ["--mirror", mirror_repository_url],
                }[sync_push_option]
                push_description = " ".join(push_arguments)
                logs.append(_timestamped(f"Syncing Repository A to Repository B with git push {push_description}"))
                push_result = await run_git_command(
                    "push",
                    *push_arguments,
                    cwd=mirror_dir,
                    env=env,
                    log=logs,
                )
                if push_result.returncode != 0:
                    raise RuntimeError(f"git push {push_description} failed")
                logs.append(_timestamped("Repository A was synced to Repository B successfully."))
                success = True
                return {"form_values": form_values, "success": success}

            default_branch = ""
            logs.append(_timestamped(f"Creating isolated repository clone for {repository_url}"))
            await _clone_isolated_repository(repository_url, repo_dir, env, logs)
            if branch_mode == "orphan" and not await _remote_has_branches(repo_dir, env, logs):
                _log_debug(logs, "Origin has no branches; skipping default branch resolution for orphan creation.")

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

            if branch_mode in {"default", "add_file"} and not branch:
                default_branch = default_branch or await _resolve_default_branch(repo_dir, env, logs)
                branch = default_branch

            if branch_mode == "from_commit":
                if not base_commit or base_commit.upper() == "HEAD":
                    default_branch = default_branch or await _resolve_default_branch(repo_dir, env, logs)
                    base_commit = f"origin/{default_branch}"
                    logs.append(_timestamped(f"Using {base_commit} as the base for branch creation."))
                    _log_debug(logs, f"Resolved base commit to '{base_commit}' for branch creation.")
                else:
                    base_commit = await _resolve_branch_creation_base(
                        repo_dir, base_commit, env, logs
                    )
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
                await _push_to_remotes(repo_dir, env, logs, repository_name, new_branch)
                logs.append(_timestamped("Branch created from commit and pushed successfully."))
                success = True
                return {"form_values": form_values, "success": success}
            elif branch_mode == "revert_to_commit":
                logs.append(_timestamped(f"Resetting branch {branch} to commit {base_commit}."))
                if not await _git_ref_exists(repo_dir, f"refs/remotes/origin/{branch}", env, logs):
                    raise RuntimeError(f"Branch '{branch}' does not exist on origin for revert mode")
                await _verify_reset_commit_on_branch(
                    repo_dir,
                    env,
                    logs,
                    base_commit,
                    branch,
                    skip_reset_commit_check,
                )
                if backup_before_reset:
                    backup_branch = await _format_reset_backup_branch(
                        repo_dir, env, logs, branch, backup_branch_format
                    )
                    logs.append(_timestamped(f"Backing up {branch} as {backup_branch}."))
                    await _push_to_remotes(
                        repo_dir,
                        env,
                        logs,
                        repository_name,
                        f"origin/{branch}:refs/heads/{backup_branch}",
                    )
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
                await _push_to_remotes(repo_dir, env, logs, repository_name, "--force", branch)
                logs.append(_timestamped("Branch reset and force-pushed successfully."))
                success = True
                return {"form_values": form_values, "success": success}
            elif branch_mode == "orphan":
                await _delete_all_local_branches(repo_dir, env, logs)
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
                await _clean_orphan_worktree(repo_dir, env, logs)
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
                await _push_to_remotes(
                    repo_dir, env, logs, repository_name,
                    f"refs/remotes/origin/{new_branch}:refs/heads/{branch}",
                )
                _log_debug(logs, f"Deleting merged source branch '{new_branch}' on origin.")
                await _push_to_remotes(repo_dir, env, logs, repository_name, "--delete", new_branch)
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
            elif branch:
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
            else:
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

            if branch_mode == "add_file":
                for add_file in add_files:
                    written_path = _write_repo_file(
                        repo_dir, add_file["path"], add_file["content"], add_file["overwrite"]
                    )
                    logs.append(_timestamped(f"Wrote file: {written_path.relative_to(repo_dir.resolve())}"))
                _log_debug(logs, "File contents written successfully.")
            elif patch_content.strip():
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
            await run_git_checked(
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
                await _push_to_remotes(
                    repo_dir, env, logs, repository_name,
                    f"HEAD:{target_branch}" if target_branch else "HEAD",
                )
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
    frontend_root = request.app[FRONTEND_ROOT_KEY]
    return web.FileResponse(frontend_root / "index.html")


async def frontend_script_handler(request: web.Request) -> web.Response:
    frontend_root = request.app[FRONTEND_ROOT_KEY]
    allowed = {
        "openai-request-settings.js", "candidate-contract.js", "openai-candidate-provider.js",
        "codex-app-server-client.js", "codex-candidate-provider.js", "commit-prompt.js",
    }
    script = request.match_info["script"]
    if script not in allowed:
        raise web.HTTPNotFound()
    return web.FileResponse(frontend_root / script)


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
    clients: Dict[str, asyncio.Queue[Dict[str, object]]] = request.app[SSE_CLIENTS_KEY]
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
    queue: Optional[asyncio.Queue[Dict[str, object]]] = app[SSE_CLIENTS_KEY].get(client_id)
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
    payload = request.get(FORCED_PAYLOAD_KEY)
    if payload is None:
        payload = await _read_json_payload(request)
    form_data = payload.get("payload", {})
    client_id = str(payload.get("client_id", "")).strip()
    if not isinstance(form_data, dict):
        raise web.HTTPBadRequest(text="Invalid form payload")
    if not client_id:
        raise web.HTTPBadRequest(text="client_id is required")

    submissions: Dict[str, asyncio.Task[Dict[str, object]]] = request.app[SUBMISSIONS_KEY]
    existing = submissions.get(client_id)
    if existing is not None and not existing.done():
        raise web.HTTPConflict(text="A submission is already running for this client")

    logs = LogSink(entries=[], emit=lambda message: _emit_to_client(request.app, client_id, message))
    task = asyncio.create_task(process_submission(_normalize_form_payload(form_data), logs))
    submissions[client_id] = task
    try:
        result = await task
    except asyncio.CancelledError:
        logs.append(_timestamped("Operation stopped by user."))
        await _emit_to_client(
            request.app, client_id, {"type": "complete", "success": False, "cancelled": True}
        )
        return web.json_response({"accepted": True, "cancelled": True})
    finally:
        if submissions.get(client_id) is task:
            del submissions[client_id]
    await _emit_to_client(request.app, client_id, {"type": "complete", "success": result["success"]})
    return web.json_response({"accepted": True})


async def cancel_submission_handler(request: web.Request) -> web.Response:
    payload = await _read_json_payload(request)
    client_id = str(payload.get("client_id", "")).strip()
    if not client_id:
        raise web.HTTPBadRequest(text="client_id is required")
    task: Optional[asyncio.Task[Dict[str, object]]] = request.app[SUBMISSIONS_KEY].get(client_id)
    if task is None or task.done():
        return web.json_response({"cancelled": False})
    task.cancel()
    return web.json_response({"cancelled": True})


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
    request[FORCED_PAYLOAD_KEY] = _with_branch_mode(payload, "default")
    return await submit_handler(request)


async def submit_from_commit_handler(request: web.Request) -> web.Response:
    payload = await _read_json_payload(request)
    request[FORCED_PAYLOAD_KEY] = _with_branch_mode(payload, "from_commit")
    return await submit_handler(request)


async def submit_add_file_handler(request: web.Request) -> web.Response:
    payload = await _read_json_payload(request)
    request[FORCED_PAYLOAD_KEY] = _with_branch_mode(payload, "add_file")
    return await submit_handler(request)


async def submit_orphan_handler(request: web.Request) -> web.Response:
    payload = await _read_json_payload(request)
    request[FORCED_PAYLOAD_KEY] = _with_branch_mode(payload, "orphan")
    return await submit_handler(request)


async def submit_revert_to_commit_handler(request: web.Request) -> web.Response:
    payload = await _read_json_payload(request)
    request[FORCED_PAYLOAD_KEY] = _with_branch_mode(payload, "revert_to_commit")
    return await submit_handler(request)


async def submit_merge_branches_handler(request: web.Request) -> web.Response:
    payload = await _read_json_payload(request)
    request[FORCED_PAYLOAD_KEY] = _with_branch_mode(payload, "merge_branches")
    return await submit_handler(request)


async def submit_mirror_repository_handler(request: web.Request) -> web.Response:
    payload = await _read_json_payload(request)
    request[FORCED_PAYLOAD_KEY] = _with_branch_mode(payload, "mirror_repository")
    return await submit_handler(request)


async def config_handler(request: web.Request) -> web.Response:
    payload = _serialize_config()
    try:
        force_refresh = request.query.get("refresh", "").lower() in {"1", "true", "yes"}
        refresh_state = request.app[GITHUB_REFRESH_TASK_KEY]
        startup_refresh = refresh_state.get("task")
        waited_for_startup_refresh = startup_refresh is not None and not startup_refresh.done()
        if waited_for_startup_refresh:
            await startup_refresh

        refresh_error = refresh_state.get("error")
        cached = _read_github_cache() if refresh_error else None
        if refresh_error and cached is None:
            raise RuntimeError(refresh_error)

        payload.update(
            cached
            if cached is not None
            else await _github_listing(
                force_refresh=force_refresh and not waited_for_startup_refresh
            )
        )
        payload["git_users"] = payload["github_users"]
        if refresh_error:
            payload["github_users_error"] = refresh_error
    except RuntimeError as exc:
        payload["github_users"] = []
        payload["active_github_user"] = None
        payload["github_repositories"] = []
        payload["github_users_error"] = str(exc)
    return web.json_response({"payload": payload})


async def health_handler(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def close_sse_clients(app: web.Application) -> None:
    app[SSE_CLIENTS_KEY].clear()


async def refresh_github_listing_after_server_start(app: web.Application) -> None:
    """Populate GitHub data after the backend has begun accepting requests."""
    try:
        await _github_listing(force_refresh=True)
        app[GITHUB_REFRESH_TASK_KEY].pop("error", None)
    except RuntimeError as exc:
        # A missing or unauthenticated GitHub CLI must not prevent the backend
        # from serving requests; /config will expose the actionable error.
        app[GITHUB_REFRESH_TASK_KEY]["error"] = str(exc)


def server_started(app: web.Application, message: str) -> None:
    """Handle aiohttp's post-listen notification and start GitHub discovery."""
    print(message)
    app[GITHUB_REFRESH_TASK_KEY]["task"] = asyncio.create_task(
        refresh_github_listing_after_server_start(app)
    )


async def stop_github_listing_refresh(app: web.Application) -> None:
    """Cancel an outstanding discovery task during graceful shutdown."""
    task = app[GITHUB_REFRESH_TASK_KEY].get("task")
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def create_web_app(serve_frontend: bool = True) -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app[SSE_CLIENTS_KEY] = {}
    app[SUBMISSIONS_KEY] = {}
    app[GITHUB_REFRESH_TASK_KEY] = {}
    app.on_shutdown.append(stop_github_listing_refresh)
    app.on_shutdown.append(close_sse_clients)
    app.router.add_route("GET", SSE_PATH, sse_handler)
    app.router.add_route("POST", "/submit", submit_default_handler)
    app.router.add_route("POST", "/submit/add_file", submit_add_file_handler)
    app.router.add_route("POST", "/submit/from_commit", submit_from_commit_handler)
    app.router.add_route("POST", "/submit/orphan", submit_orphan_handler)
    app.router.add_route("POST", "/submit/revert_to_commit", submit_revert_to_commit_handler)
    app.router.add_route("POST", "/submit/merge_branches", submit_merge_branches_handler)
    app.router.add_route("POST", "/submit/mirror_repository", submit_mirror_repository_handler)
    app.router.add_route("POST", "/submit/cancel", cancel_submission_handler)
    app.router.add_route("GET", "/config", config_handler)
    app.router.add_route("GET", "/health", health_handler)
    if serve_frontend:
        frontend_root = RUNTIME_SETTINGS.frontend_root if RUNTIME_SETTINGS else _frontend_root()
        if not frontend_root.exists():
            raise RuntimeError(f"Frontend root not found at {frontend_root}")
        app[FRONTEND_ROOT_KEY] = frontend_root
        app.router.add_route("GET", "/", frontend_handler)
        app.router.add_route("GET", "/index.html", frontend_handler)
        app.router.add_route("GET", "/{script:.+\\.js}", frontend_script_handler)
    else:
        app.router.add_route("GET", "/", sse_handler)
    return app
