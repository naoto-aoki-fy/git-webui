import asyncio
import html
import json
import os
import shlex
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from pathlib import PureWindowsPath
from typing import Dict, List, Optional

from aiohttp import web


CONFIG_PATH = Path(os.environ.get("GIT_WEBUI_CONFIG", "config.json"))


def _load_config() -> Dict[str, List[Dict[str, str]]]:
    if not CONFIG_PATH.exists():
        return {"ssh_keys": [], "git_users": []}

    with CONFIG_PATH.open("r", encoding="utf-8") as config_file:
        try:
            data = json.load(config_file)
        except json.JSONDecodeError as exc:  # noqa: BLE001
            raise RuntimeError(f"Failed to parse configuration file {CONFIG_PATH}: {exc}") from exc

    ssh_keys = data.get("ssh_keys", [])
    git_users = data.get("git_users", [])
    if not isinstance(ssh_keys, list) or not isinstance(git_users, list):
        raise RuntimeError("Configuration file must define 'ssh_keys' and 'git_users' as lists")

    return {"ssh_keys": ssh_keys, "git_users": git_users}


APP_CONFIG = _load_config()


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


async def run_command(
    *cmd: str,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    log: Optional[List[str]] = None,
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


def _log_debug(logs: Optional[List[str]], message: str) -> None:
    if logs is None:
        return
    logs.append(_timestamped(f"DEBUG: {message}"))


def _format_ssh_key_arg(raw_path: str, resolved_path: Path) -> str:
    if "\\" in raw_path or ":" in raw_path:
        return shlex.quote(PureWindowsPath(raw_path).as_posix())
    return shlex.quote(str(resolved_path))


def render_page(form_values: Dict[str, str], logs: Optional[List[str]] = None, success: Optional[bool] = None) -> str:
    log_section = ""
    if logs:
        escaped_logs = "\n".join(html.escape(entry) for entry in logs)
        status_class = "success" if success else "failure"
        status_label = "Success" if success else "Failure"
        if success is None:
            status_class = "neutral"
            status_label = "Logs"
        log_section = f"""
        <section class=\"logs {status_class}\">
            <h2>{status_label}</h2>
            <pre>{escaped_logs}</pre>
        </section>
        """
    escaped_form = {key: html.escape(value) for key, value in form_values.items()}

    ssh_key_options = "\n".join(
        (
            "            "
            + f"<option value=\"{idx}\""
            + (" selected" if str(idx) == escaped_form.get("ssh_key_selection", "") else "")
            + f">{html.escape(option.get('label', option.get('path', 'Unknown Key')))}</option>"
        )
        for idx, option in enumerate(APP_CONFIG["ssh_keys"])
    )
    if ssh_key_options:
        ssh_key_options = "            <option value=\"\"></option>\n" + ssh_key_options
    else:
        ssh_key_options = "            <option value=\"\">(No SSH keys configured)</option>"

    git_user_options = "\n".join(
        (
            "            "
            + f"<option value=\"{idx}\""
            + (" selected" if str(idx) == escaped_form.get("git_user_selection", "") else "")
            + f">{html.escape(option.get('label', option.get('name', 'Unknown User')))}</option>"
        )
        for idx, option in enumerate(APP_CONFIG["git_users"])
    )
    if git_user_options:
        git_user_options = "            <option value=\"\"></option>\n" + git_user_options
    else:
        git_user_options = "            <option value=\"\">(No Git users configured)</option>"
    return f"""<!DOCTYPE html>
<html lang=\"ja\">
<head>
    <meta charset=\"utf-8\">
    <title>git apply web ui</title>
    <style>
        body {{
            font-family: system-ui, sans-serif;
            margin: 2rem;
            background: #f5f5f5;
            color: #222;
        }}
        main {{
            max-width: 960px;
            margin: 0 auto;
            background: #fff;
            padding: 2rem;
            border-radius: 12px;
            box-shadow: 0 4px 18px rgba(0,0,0,0.1);
        }}
        form {{
            display: grid;
            grid-template-columns: 1fr;
            gap: 1.5rem;
        }}
        label {{
            font-weight: 600;
            display: block;
            margin-bottom: 0.5rem;
        }}
        input[type=text], textarea, select {{
            width: 100%;
            padding: 0.75rem;
            border: 1px solid #ccc;
            border-radius: 8px;
            font-family: monospace;
            background: #fafafa;
        }}
        textarea {{
            min-height: 220px;
        }}
        button {{
            padding: 0.75rem 1.5rem;
            background: #2b6cb0;
            color: #fff;
            border: none;
            border-radius: 8px;
            font-size: 1rem;
            cursor: pointer;
        }}
        button:hover {{
            background: #2c5282;
        }}
        .logs {{
            margin-top: 2rem;
            padding: 1.5rem;
            border-radius: 10px;
            background: #1a202c;
            color: #edf2f7;
            box-shadow: inset 0 0 8px rgba(0,0,0,0.4);
        }}
        .logs.success {{ border: 2px solid #48bb78; }}
        .logs.failure {{ border: 2px solid #f56565; }}
        .logs.neutral {{ border: 2px solid #a0aec0; }}
        .logs pre {{
            margin: 0;
            white-space: pre-wrap;
        }}
        .field-group {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: 1rem;
        }}
    </style>
</head>
<body>
<main>
    <h1>git apply --3way Web UI</h1>
    <p>GitHubリポジトリに対してパッチを適用し、コミットしてpushします。</p>
    <form method=\"post\" action=\"/\">
        <div class=\"field-group\">
            <div>
                <label for=\"repository_url\">Repository URL (SSH推奨)</label>
                <input type=\"text\" id=\"repository_url\" name=\"repository_url\" required value=\"{escaped_form.get('repository_url', '')}\">
            </div>
            <div>
                <label for=\"branch\">Branch (省略可: 現在のブランチを使用)</label>
                <input type=\"text\" id=\"branch\" name=\"branch\" value=\"{escaped_form.get('branch', '')}\">
            </div>
        </div>
        <div>
            <label for=\"git_user\">Git User (Name &amp; Email)</label>
            <select id=\"git_user\" name=\"git_user\">
{git_user_options}
            </select>
        </div>
        <div>
            <label for=\"commit_message\">Commit Message</label>
            <textarea id=\"commit_message\" name=\"commit_message\" placeholder=\"例: Apply patch from Web UI\">{escaped_form.get('commit_message', '')}</textarea>
        </div>
        <div>
            <label for=\"ssh_key_path\">SSH Private Key</label>
            <select id=\"ssh_key_path\" name=\"ssh_key_path\">
{ssh_key_options}
            </select>
        </div>
        <div>
            <label for=\"patch\">Patch (git apply --3way -v で適用されます)</label>
            <textarea id=\"patch\" name=\"patch\" required placeholder=\"diff --git a/...\n\"></textarea>
        </div>
        <button type=\"submit\">Apply Patch &amp; Push</button>
    </form>
    {log_section}
</main>
</body>
</html>
"""


async def index(request: web.Request) -> web.Response:
    if request.method == "GET":
        return web.Response(text=render_page({}, None), content_type="text/html")

    logs: List[str] = []
    _log_debug(logs, "Received POST request.")
    form = await request.post()
    repository_url = form.get("repository_url", "").strip()
    branch = form.get("branch", "").strip()
    git_user_selection = form.get("git_user", "").strip()
    ssh_key_selection = form.get("ssh_key_path", "").strip()
    commit_message = form.get("commit_message", "").replace("\r\n", "\n")
    commit_message = commit_message.strip("\n")
    patch_content = form.get("patch", "").replace("\r\n", "\n")

    _log_debug(logs, f"Parsed repository_url='{repository_url}'.")
    _log_debug(logs, f"Parsed branch='{branch or '(default)'}'.")
    _log_debug(logs, f"Parsed git_user selection='{git_user_selection or '(none)'}'.")
    _log_debug(logs, f"Parsed ssh_key selection='{ssh_key_selection or '(none)'}'.")
    _log_debug(logs, f"Commit message length={len(commit_message)}.")
    _log_debug(logs, f"Patch length={len(patch_content)}.")

    form_values = {
        "repository_url": repository_url,
        "branch": branch,
        "commit_message": commit_message,
        "git_user_selection": git_user_selection,
        "ssh_key_selection": ssh_key_selection,
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
            logs = [_timestamped("Invalid Git user selection.")]
            return web.Response(
                text=render_page(form_values, logs, False),
                content_type="text/html",
            )

    _log_debug(logs, "Validated git user selection.")
    success = False

    if not repository_url:
        _log_debug(logs, "Repository URL missing.")
        logs.append(_timestamped("Repository URL is required."))
        return web.Response(text=render_page(form_values, logs, False), content_type="text/html")

    if not patch_content.strip():
        _log_debug(logs, "Patch content missing or whitespace.")
        logs.append(_timestamped("Patch content is required."))
        return web.Response(text=render_page(form_values, logs, False), content_type="text/html")

    ssh_key_path: Optional[Path] = None

    try:
        with tempfile.TemporaryDirectory(prefix="git-webui-") as tmpdir:
            workdir = Path(tmpdir)
            repo_dir = workdir / "repo"
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

            logs.append(_timestamped(f"Cloning repository {repository_url}"))
            _log_debug(logs, "Starting git clone.")
            clone_result = await run_command(
                "git",
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

            if branch:
                _log_debug(logs, f"Checking out branch '{branch}'.")
                checkout_result = await run_command(
                    "git",
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
                _log_debug(logs, "No branch specified; using default branch.")
                await run_command(
                    "git",
                    "status",
                    "-sb",
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )

            patch_path = workdir / "patch.diff"
            logs.append(_timestamped(repr(patch_content)))
            patch_path.write_text(patch_content, encoding="utf-8")
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
                commit_file.write_text(commit_message, encoding="utf-8")
                _log_debug(logs, f"Commit message file saved to {commit_file}.")
                _log_debug(logs, "Creating git commit.")
                commit_result = await run_command(
                    "git",
                    "commit",
                    "-F",
                    str(commit_file),
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
                    "push",
                    "origin",
                    f"HEAD:{branch}" if branch else "HEAD",
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
        logs.append(_timestamped(f"ERROR: {exc}"))
        _log_debug(logs, "Request failed with exception.")
        success = False

    return web.Response(text=render_page(form_values, logs, success), content_type="text/html")


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_route("GET", "/", index)
    app.router.add_route("POST", "/", index)
    return app


if __name__ == "__main__":
    web.run_app(create_app())
