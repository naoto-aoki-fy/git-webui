import asyncio
import html
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from aiohttp import web


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
        input[type=text], textarea {{
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
        <div class=\"field-group\">
            <div>
                <label for=\"user_name\">Git User Name</label>
                <input type=\"text\" id=\"user_name\" name=\"user_name\" value=\"{escaped_form.get('user_name', '')}\">
            </div>
            <div>
                <label for=\"user_email\">Git User Email</label>
                <input type=\"text\" id=\"user_email\" name=\"user_email\" value=\"{escaped_form.get('user_email', '')}\">
            </div>
        </div>
        <div>
            <label for=\"commit_message\">Commit Message</label>
            <input type=\"text\" id=\"commit_message\" name=\"commit_message\" placeholder=\"例: Apply patch from Web UI\" value=\"{escaped_form.get('commit_message', '')}\">
        </div>
        <div>
            <label for=\"ssh_key\">SSH Private Key (必要に応じて。保存はされません)</label>
            <textarea id=\"ssh_key\" name=\"ssh_key\" placeholder=\"-----BEGIN OPENSSH PRIVATE KEY-----\n...\n-----END OPENSSH PRIVATE KEY-----\"></textarea>
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

    form = await request.post()
    repository_url = form.get("repository_url", "").strip()
    branch = form.get("branch", "").strip()
    user_name = form.get("user_name", "").strip()
    user_email = form.get("user_email", "").strip()
    commit_message = form.get("commit_message", "").strip()
    patch_content = form.get("patch", "")
    ssh_key = form.get("ssh_key", "")

    form_values = {
        "repository_url": repository_url,
        "branch": branch,
        "user_name": user_name,
        "user_email": user_email,
        "commit_message": commit_message,
    }

    logs: List[str] = []
    success = False

    if not repository_url:
        logs.append(_timestamped("Repository URL is required."))
        return web.Response(text=render_page(form_values, logs, False), content_type="text/html")

    if not patch_content.strip():
        logs.append(_timestamped("Patch content is required."))
        return web.Response(text=render_page(form_values, logs, False), content_type="text/html")

    ssh_key_path: Optional[Path] = None

    try:
        with tempfile.TemporaryDirectory(prefix="git-webui-") as tmpdir:
            workdir = Path(tmpdir)
            repo_dir = workdir / "repo"
            env = os.environ.copy()

            if ssh_key.strip():
                fd, tmp_path = tempfile.mkstemp(prefix="git-webui-key-")
                os.close(fd)
                ssh_key_path = Path(tmp_path)
                ssh_key_path.write_text(ssh_key, encoding="utf-8")
                os.chmod(ssh_key_path, 0o600)
                env["GIT_SSH_COMMAND"] = f"ssh -i {ssh_key_path} -o StrictHostKeyChecking=no"
                logs.append(_timestamped("SSH key written to temporary file."))

            logs.append(_timestamped(f"Cloning repository {repository_url}"))
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

            if user_name:
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

            if user_email:
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

            if branch:
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
            else:
                await run_command(
                    "git",
                    "status",
                    "-sb",
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )

            patch_path = workdir / "patch.diff"
            patch_path.write_text(patch_content)
            logs.append(_timestamped("Patch written to temporary file."))

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

            await run_command(
                "git",
                "add",
                "-A",
                cwd=repo_dir,
                env=env,
                log=logs,
            )

            await run_command(
                "git",
                "status",
                "-sb",
                cwd=repo_dir,
                env=env,
                log=logs,
            )

            if commit_message:
                commit_result = await run_command(
                    "git",
                    "commit",
                    "-m",
                    commit_message,
                    cwd=repo_dir,
                    env=env,
                    log=logs,
                )
                if commit_result.returncode != 0:
                    raise RuntimeError("git commit failed")
            else:
                logs.append(_timestamped("No commit message provided. Skipping commit."))

            if commit_message:
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
            else:
                logs.append(_timestamped("Push skipped because no commit was created."))
            success = True
    except Exception as exc:  # noqa: BLE001
        logs.append(_timestamped(f"ERROR: {exc}"))
        success = False
    finally:
        if ssh_key_path and ssh_key_path.exists():
            try:
                ssh_key_path.unlink()
                logs.append(_timestamped("Temporary SSH key removed."))
            except OSError as cleanup_error:
                logs.append(_timestamped(f"Failed to remove temporary SSH key: {cleanup_error}"))

    return web.Response(text=render_page(form_values, logs, success), content_type="text/html")


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_route("GET", "/", index)
    app.router.add_route("POST", "/", index)
    return app


if __name__ == "__main__":
    web.run_app(create_app())
