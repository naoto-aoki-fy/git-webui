# git-webui

A small web UI for applying a unified diff patch to a Git repository with `git apply --3way`, then optionally committing and pushing the result.

This repository currently contains:

- **Backend** (`backend/app.py`): `aiohttp` server + SSE log stream and HTTP API that performs Git operations.
- **Frontend** (`frontend/index.html`): single-file static UI that connects to the backend over Server-Sent Events (SSE) and HTTP.
- **Bookmarklet** (`bookmarklet/codex.js`): helper script to open the hosted frontend with pre-filled data from a Codex task page.

## Current capabilities

The UI supports these main workflows:

1. **Default mode**
   - Clone/fetch a repository into a local workspace cache directory.
   - Optionally checkout/pull a specified branch.
   - Apply patch content with `git apply --3way -v`.
   - Stage (`git add -A`), optionally commit, and optionally push.

2. **Create branch from commit**
   - Create a new branch from a specific commit (or `HEAD`).

3. **Create orphan branch**
   - Create an orphan branch and apply/commit there.

4. **Revert branch to commit**
   - Reset a branch to a commit and force-push.

Additional behavior:

- Configures all SSH keys from config in `GIT_SSH_COMMAND` with repeated `-o IdentityFile=` options.
- Git author identity selection from config (`user.name`, `user.email`).
- Live operation logs streamed over SSE.
- Frontend-side local storage for draft form values/history and backend URL.

## Repository structure

```text
backend/app.py              # aiohttp backend server and git orchestration
frontend/index.html         # static single-page UI
bookmarklet/codex.js        # readable source bookmarklet script
bookmarklet/codex-js-url.txt# URL-encoded bookmarklet payload
config-sample.toml          # sample ssh_keys / git_users config
requirements.txt            # Python dependency list
```

## Requirements

- Python 3.11+ (uses `tomllib` from the standard library).
- Git installed and available on `PATH`.
- SSH client available if using SSH remotes.

Install backend dependency:

```bash
pip install -r requirements.txt
```

## Configuration

Copy the sample config and edit values for your machine:

```bash
cp config-sample.toml config.toml
```

`config.toml` supports two lists:

- `[[ssh_keys]]`: SSH private keys to pass to `ssh` with repeated `-o IdentityFile=` options.
  - `label` (display name, used only for config readability/API labels)
  - `path` (private key path)
- `[[git_users]]`: selectable Git commit identities.
  - `label` (display name)
  - `name`
  - `email`
  - `default` (optional, boolean)
  - `default_repositories` (optional list used by frontend defaults)

## Running locally

From the repository root:

```bash
python backend/app.py --config config.toml --repo-root repos --bind 0.0.0.0 --port 8080
```

Then open:

- `http://localhost:8080/` (backend serving frontend)

The frontend can also be hosted separately (for example via GitHub Pages) and pointed at a backend base URL like `http://localhost:8080` (or a subpath base such as `https://host/path`).

If you host frontend and backend on different domains, configure CORS on the backend with `CORS_ALLOW_ORIGIN`:

```bash
CORS_ALLOW_ORIGIN="https://naoto-aoki-fy.github.io,https://example.com" python backend/app.py --config config.toml --repo-root repos --bind 0.0.0.0 --port 8080
```

- Use `*` (default) to allow any origin (i.e. arbitrary domains).
- Use a comma-separated list to allow specific frontend origins.
- Requests from non-allowed origins are rejected at CORS preflight (`OPTIONS`) with HTTP 403.

## Backend CLI options

`backend/app.py` currently supports:

- `--bind` (server bind address)
- `--port` (server port)
- `--config` (path to TOML config)
- `--repo-root` (persistent cache/workspace root for repositories)
- `--keep-temp` (keep temporary workspaces for debugging)
- `--serve-frontend` / `--no-serve-frontend`

## Frontend notes

- The backend endpoint is configured in the UI as a backend base URL. The frontend calls `${base}/events` for SSE and `${base}/submit`, `${base}/config`, `${base}/health` for HTTP.
- Query/hash parameters are supported for pre-filling fields.
- “Open on GitHub” appears for `git@github.com:owner/repo(.git)` format repository URLs.
- Includes helper actions for clipboard paste and commit-message prompt generation.

## Bookmarklet

- `bookmarklet/codex.js` is the readable source.
- `bookmarklet/codex-js-url.txt` is the URL-encoded `javascript:` URL form.
- The script currently targets `https://naoto-aoki-fy.github.io/git-webui/` as frontend base URL.

## GitHub Pages deployment

A GitHub Actions workflow deploys `frontend/` to GitHub Pages on pushes to `main` that change frontend files (or the workflow file).

## Limitations and caveats

- No authentication layer is built into the backend; run only in trusted environments.
- Git operations are executed on the server host, so filesystem/SSH permissions of that host apply.
- The backend keeps per-repository cached clones under `--repo-root`.
- This is currently a lightweight single-file frontend + single Python backend, without a packaged release process.
