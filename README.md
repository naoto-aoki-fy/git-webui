# git-webui

A small web UI for applying a unified diff patch to a Git repository with `git apply --3way`, then optionally committing and pushing the result.

This repository currently contains:

- **Backend** (`backend/app.py`): `aiohttp` server + SSE log stream and HTTP API that performs Git operations.
- **Frontend** (`frontend/index.html`): single-file static UI that connects to the backend over Server-Sent Events (SSE) and HTTP.
- **Bookmarklet** (`bookmarklet/codex.js`): helper script to open the hosted frontend with pre-filled data from a Codex task page.

## Current capabilities

The UI supports these main workflows:

1. **Default mode**
   - Seed each operation from a persistent bare mirror cache, then fetch and work in an isolated temporary clone.
   - Optionally checkout/pull a specified branch.
   - Apply patch content with `git apply --3way -v`.
   - Stage (`git add -A`), optionally commit, and optionally push.

2. **Create branch from commit**
   - Create a new branch from a specific commit, a remote branch name, or `HEAD`.

3. **Create orphan branch**
   - Create an orphan branch and apply/commit there.

4. **Revert branch to commit**
   - Reset a branch to a commit and force-push.

Additional behavior:

- Repository owner/name completion exclusively from `gh repo list` for every authenticated account; previously entered values are not included. GitHub usernames and repositories are cached under `--repo-root`. The backend retrieves the list after the aiohttp server starts and on demand with the **Update GitHub users** button beside the Git Author field; it does not refresh the list on a timer. Repository discovery uses a per-user `GH_TOKEN` without changing the active GitHub CLI account.
- Git author identity discovery from authenticated GitHub accounts. GitHub supplies each user's ID and name, and commits use the corresponding GitHub pseudo-email address. The repository owner is selected by default, or the configured additional-push destination owner when a prefix matches.
- Live operation logs streamed over SSE.
- Frontend-side local storage for draft form values, branch history, and backend URL.
- Customizable prompt generation service name and URL template, saved in local storage.
- Direct commit-message suggestion generation through a browser-accessible OpenAI-compatible chat completions endpoint, with selectable JSON candidates.

## Repository structure

```text
backend/app.py              # aiohttp backend server and git orchestration
frontend/index.html         # static single-page UI
bookmarklet/codex.js        # readable source bookmarklet script
bookmarklet/codex-js-url.txt# URL-encoded bookmarklet payload
config-sample.toml          # sample Git author default rule
requirements.txt            # Python dependency list
```

## Requirements

- Python 3.11+ (uses `tomllib` from the standard library).
- Git installed and available on `PATH`.
- GitHub CLI (`gh`) authenticated for every GitHub account you want to use.
- A Git credential helper configured to select credentials from each HTTPS repository owner (example below).

Configure Git to obtain the appropriate GitHub token from `gh` based on the repository owner. The application does not select or switch GitHub users during clone and push operations:

```gitconfig
[credential "https://github.com/"]
    useHttpPath = true
    helper =
    helper = "!f() { \
        [ \"$1\" = get ] || exit 0; \
        owner=''; \
        while IFS='=' read -r k v; do \
            [ \"$k\" = path ] && owner=${v%%/*}; \
        done; \
        [ -n \"$owner\" ] || exit 0; \
        token=$(gh auth token --user \"$owner\") || exit 0; \
        printf 'username=%s\\npassword=%s\\n' \"$owner\" \"$token\"; \
    }; f"
```

Install backend dependency:

```bash
pip install -r requirements.txt
```

## Configuration

Copy the sample config and edit values for your machine:

```bash
cp config-sample.toml config.toml
```

Git identities do not need to be stored in `config.toml`: they are fetched from GitHub at startup and by the frontend's **Update GitHub users** button. The backend uses the profile name (or login when no name is set) and the GitHub pseudo-email `<id>+<login>@users.noreply.github.com`.

`config.toml` optionally supports a `repository_prefix_destinations` array of `[prefix, destination_owner]` pairs. Matching is case-insensitive and the first match wins. For example, `["upstream-", "my-user"]` maps `upstream-widget` to `my-user/widget`. Patch commits, newly created branches, orphan branches, hard resets, and branch merges are pushed both to the original repository and to the mapped repository. The destination owner becomes the default Git author and is automatically filled, with the derived repository name, as Repository B in sync mode. `git_user_defaults` remains accepted during its deprecation period.

## Running locally

From the repository root:

```bash
python backend/app.py --config config.toml --repo-root repos --bind 0.0.0.0 --port 8080

# Or listen only on an AF_UNIX socket instead of TCP:
python backend/app.py --config config.toml --repo-root repos --unix-socket /tmp/git-webui.sock
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
- `--unix-socket` (AF_UNIX socket path; cannot be combined with `--bind` or `--port`)
- `--config` (path to TOML config)
- `--repo-root` (persistent bare mirror cache root for repositories)
- `--keep-temp` (keep temporary workspaces for debugging)
- `--serve-frontend` / `--no-serve-frontend`

## Frontend notes

- The backend endpoint is configured in the UI as a backend base URL. The frontend calls `${base}/events` for SSE and `${base}/submit`, `${base}/config`, `${base}/health` for HTTP.
- Query/hash parameters are supported for pre-filling fields.
- Repositories are entered as separate GitHub owner and repository name fields; “Open on GitHub” links to the resulting repository and optional branch. Sync mode provides links for both repositories, can swap Repository A and Repository B before submission, and defaults to `git push --all` while allowing either one named branch or `git push --mirror` to be synchronized.
- Includes helper actions for clipboard paste and commit-message prompt generation.
- The prompt generation link defaults to ChatGPT. Its service name and URL template can be changed in the UI; use `{message}` in the template to insert the URL-encoded prompt.
- The OpenAI-compatible endpoint URL, authorization token, model, and candidate output format are configured in the frontend and saved in that browser's `localStorage`. Candidate generation supports NDJSON, FlatJSON as a single-level object via JSON mode (`json_object`), and JSON Structured Outputs (`json_schema`); the latter two require endpoint support for the corresponding `response_format`. FlatJSON uses `candidate_count`, `recommended_candidate_id`, and `candidate_N_*` scalar properties without arrays or nested objects. The token is sent directly from the browser to the configured endpoint and never passes through the git-webui backend; the endpoint must permit CORS requests from the frontend origin.
- Additional top-level request parameters are stored as raw text in `git-webui.openAISettings` in that browser's `localStorage`. They must be a JSON object, such as `{"reasoning_effort":"none"}`; arrays and other JSON values are rejected.
- Additional parameters cannot use the reserved keys `model`, `messages`, `stream`, or `response_format`, which are required by the candidate-generation and response-parsing contract. Available settings vary by OpenAI-compatible endpoint, and the endpoint may reject unsupported parameters.
- Streaming generation reasoning is shown in a separate, collapsible thinking display only when the endpoint returns reasoning in `choices[0].delta.reasoning_content` or thinking-type (`thinking`, `reasoning`, or `reasoning_content`) content parts. Both fields may be strings, `{text: ...}` objects, or arrays containing those forms. Some endpoints require additional request parameters to enable thinking; consult the endpoint's documentation and add those parameters in the UI when necessary.

## Bookmarklet

- `bookmarklet/codex.js` is the readable source.
- `bookmarklet/codex-js-url.txt` is the URL-encoded `javascript:` URL form.
- The script currently targets `https://naoto-aoki-fy.github.io/git-webui/` as frontend base URL.

## GitHub Pages deployment

A GitHub Actions workflow deploys `frontend/` to GitHub Pages on pushes to `main` that change frontend files (or the workflow file).

## Limitations and caveats

- No authentication layer is built into the backend; run only in trusted environments.
- GitHub operations use HTTPS and rely on the configured Git credential helper to choose credentials by repository owner; SSH remotes are rejected.
- The backend keeps per-repository bare mirror caches under `--repo-root` and refreshes an existing cache from its remote before each operation. Every submission then uses its own temporary clone, so concurrent operations never share a Git index or working tree.
- This is currently a lightweight single-file frontend + single Python backend, without a packaged release process.
