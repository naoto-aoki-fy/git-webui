import asyncio
import html
import json
import os
import shlex
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from pathlib import PureWindowsPath
from typing import Dict, List, Optional
import traceback

from aiohttp import web

CONFIG_PATH = Path(os.environ.get("GIT_WEBUI_CONFIG", "config.json"))

DEVNULL = "NUL" if os.name == "nt" else "/dev/null"
KEEP_TEMP = os.environ.get("GIT_WEBUI_KEEP_TEMP", "").lower() in {"1", "true", "yes", "on"}

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


@contextmanager
def _temporary_workspace(logs: Optional[List[str]] = None) -> Path:
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
    allow_empty_checked = " checked" if escaped_form.get("allow_empty_commit") == "true" else ""
    branch_mode = escaped_form.get("branch_mode", "default")
    new_branch_name = escaped_form.get("new_branch", "")
    branch_mode_default_checked = " checked" if branch_mode == "default" else ""
    branch_mode_commit_checked = " checked" if branch_mode == "from_commit" else ""
    branch_mode_orphan_checked = " checked" if branch_mode == "orphan" else ""
    return f"""<!DOCTYPE html>
<html lang=\"en\">
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
        .toggle-group {{
            display: flex;
            flex-wrap: wrap;
            gap: 1rem;
            align-items: center;
        }}
        .toggle-group label {{
            font-weight: 500;
            margin-bottom: 0;
        }}
        .subtle {{
            color: #4a5568;
            font-size: 0.9rem;
        }}
        .hidden {{
            display: none;
        }}
    </style>
</head>
<body>
<main>
    <h1>git apply --3way Web UI</h1>
    <p>Apply a patch to a GitHub repository, commit it, and push the result.</p>
    <form method=\"post\" action=\"/\">
        <div class=\"field-group\">
            <div>
                <label for=\"repository_url\">Repository URL (SSH recommended)</label>
                <input type=\"text\" id=\"repository_url\" name=\"repository_url\" required value=\"{escaped_form.get('repository_url', '')}\">
            </div>
            <div id=\"branch_group\">
                <label for=\"branch\">Branch (optional: use the current branch)</label>
                <input type=\"text\" id=\"branch\" name=\"branch\" value=\"{escaped_form.get('branch', '')}\">
            </div>
        </div>
        <div>
            <label>Mode</label>
            <div class=\"toggle-group\" role=\"radiogroup\" aria-label=\"Mode\">
                <label>
                    <input type=\"radio\" name=\"branch_mode\" value=\"default\"{branch_mode_default_checked}>
                    Default behavior (checkout when a branch is specified)
                </label>
                <label>
                    <input type=\"radio\" name=\"branch_mode\" value=\"from_commit\"{branch_mode_commit_checked}>
                    Create a new branch from the specified commit
                </label>
                <label>
                    <input type=\"radio\" name=\"branch_mode\" value=\"orphan\"{branch_mode_orphan_checked}>
                    Create an orphan branch
                </label>
            </div>
            <p class=\"subtle\">A new branch name is required for commit-based or orphan modes.</p>
        </div>
        <div id=\"new_branch_group\" class=\"hidden\">
            <label for=\"new_branch\">New Branch Name</label>
            <input type=\"text\" id=\"new_branch\" name=\"new_branch\" value=\"{new_branch_name}\">
        </div>
        <div id=\"commit_id_group\" class=\"hidden\">
            <label for=\"base_commit\">Base Commit ID (e.g., a1b2c3d)</label>
            <input type=\"text\" id=\"base_commit\" name=\"base_commit\" value=\"{escaped_form.get('base_commit', '')}\">
        </div>
        <div id=\"git_user_group\">
            <label for=\"git_user\">Git User (Name &amp; Email)</label>
            <select id=\"git_user\" name=\"git_user\">
{git_user_options}
            </select>
        </div>
        <div id=\"commit_message_group\">
            <label for=\"commit_message\">Commit Message</label>
            <textarea id=\"commit_message\" name=\"commit_message\" placeholder=\"e.g., Apply patch from Web UI\">{escaped_form.get('commit_message', '')}</textarea>
        </div>
        <div id=\"allow_empty_group\">
            <label>
                <input type=\"checkbox\" id=\"allow_empty_commit\" name=\"allow_empty_commit\" value=\"true\"{allow_empty_checked}>
                Allow empty commit
            </label>
        </div>
        <div id=\"ssh_key_group\">
            <label for=\"ssh_key_path\">SSH Private Key</label>
            <select id=\"ssh_key_path\" name=\"ssh_key_path\">
{ssh_key_options}
            </select>
        </div>
        <div id=\"patch_group\">
            <label for=\"patch\">Patch (applied with git apply --3way -v)</label>
            <textarea id=\"patch\" name=\"patch\" placeholder=\"diff --git a/...\n\"></textarea>
        </div>
        <button type=\"submit\">Apply Patch &amp; Push</button>
    </form>
    {log_section}
</main>
<script>
    const allowEmptyCommit = document.getElementById("allow_empty_commit");
    const patchField = document.getElementById("patch");
    const branchModeInputs = document.querySelectorAll("input[name='branch_mode']");
    const commitIdGroup = document.getElementById("commit_id_group");
    const commitIdField = document.getElementById("base_commit");
    const branchGroup = document.getElementById("branch_group");
    const branchField = document.getElementById("branch");
    const newBranchGroup = document.getElementById("new_branch_group");
    const newBranchField = document.getElementById("new_branch");
    const gitUserGroup = document.getElementById("git_user_group");
    const commitMessageGroup = document.getElementById("commit_message_group");
    const allowEmptyGroup = document.getElementById("allow_empty_group");
    const sshKeyGroup = document.getElementById("ssh_key_group");
    const patchGroup = document.getElementById("patch_group");
    const gitUserField = document.getElementById("git_user");
    const commitMessageField = document.getElementById("commit_message");
    const sshKeyField = document.getElementById("ssh_key_path");
    const togglePatchRequired = () => {{
        patchField.required = !allowEmptyCommit.checked;
        patchGroup.classList.toggle("hidden", allowEmptyCommit.checked);
        if (allowEmptyCommit.checked) {{
            patchField.removeAttribute("name");
            patchField.setAttribute("disabled", "");
        }} else {{
            patchField.setAttribute("name", "patch");
            patchField.removeAttribute("disabled", "");
        }}
    }};
    const toggleCommitField = () => {{
        const selectedMode = document.querySelector("input[name='branch_mode']:checked").value;
        const needsCommit = selectedMode === "from_commit";
        const needsNewBranch = selectedMode === "from_commit" || selectedMode === "orphan";
        commitIdGroup.classList.toggle("hidden", !needsCommit);
        if (needsCommit) {{
            commitIdField.removeAttribute("disabled");
        }} else {{
            commitIdField.setAttribute("disabled", "");
        }}
        newBranchGroup.classList.toggle("hidden", !needsNewBranch);
        if (needsNewBranch) {{
            newBranchField.removeAttribute("disabled");
            newBranchField.setAttribute("required", "");
            branchField.setAttribute("disabled", "");
        }} else {{
            newBranchField.setAttribute("disabled", "");
            newBranchField.removeAttribute("required");
            branchField.removeAttribute("disabled");
        }}
        const hideDetails = selectedMode === "from_commit";
        branchGroup.classList.toggle("hidden", selectedMode !== "default");
        if (selectedMode === "default") {{
            branchField.removeAttribute("disabled");
        }} else {{
            branchField.setAttribute("disabled", "");
        }}
        gitUserGroup.classList.toggle("hidden", hideDetails);
        commitMessageGroup.classList.toggle("hidden", hideDetails);
        allowEmptyGroup.classList.toggle("hidden", hideDetails);
        patchGroup.classList.toggle("hidden", hideDetails || allowEmptyCommit.checked);
        if (hideDetails) {{
            gitUserField.setAttribute("disabled", "");
            commitMessageField.setAttribute("disabled", "");
            allowEmptyCommit.setAttribute("disabled", "");
            patchField.setAttribute("disabled", "");
        }} else {{
            gitUserField.removeAttribute("disabled");
            commitMessageField.removeAttribute("disabled");
            allowEmptyCommit.removeAttribute("disabled");
            togglePatchRequired();
        }}
    }};
    togglePatchRequired();
    allowEmptyCommit.addEventListener("change", togglePatchRequired);
    branchModeInputs.forEach((input) => {{
        input.addEventListener("change", toggleCommitField);
    }});
    toggleCommitField();
</script>
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

    if branch_mode != "from_commit" and not patch_content.strip() and not allow_empty_commit:
        _log_debug(logs, "Patch content missing or whitespace.")
        logs.append(_timestamped("Patch content is required unless empty commit is allowed."))
        return web.Response(text=render_page(form_values, logs, False), content_type="text/html")
    if branch_mode == "from_commit" and not base_commit:
        _log_debug(logs, "Base commit missing for branch creation.")
        logs.append(_timestamped("Base commit ID is required when creating a branch from a commit."))
        return web.Response(text=render_page(form_values, logs, False), content_type="text/html")
    if branch_mode in {"from_commit", "orphan"} and not new_branch:
        _log_debug(logs, "New branch name missing for selected branch mode.")
        logs.append(_timestamped("New branch name is required for commit/orphan branch creation modes."))
        return web.Response(text=render_page(form_values, logs, False), content_type="text/html")

    ssh_key_path: Optional[Path] = None

    try:
        with _temporary_workspace(logs) as workdir:
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

            if branch_mode == "from_commit":
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
                return web.Response(text=render_page(form_values, logs, success), content_type="text/html")
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
            elif branch:
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
                _log_debug(logs, "No branch specified; using default branch.")
                await run_command(
                    "git",
                    "status",
                    "-sb",
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )

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

    return web.Response(text=render_page(form_values, logs, success), content_type="text/html")


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_route("GET", "/", index)
    app.router.add_route("POST", "/", index)
    return app


if __name__ == "__main__":
    web.run_app(create_app())
