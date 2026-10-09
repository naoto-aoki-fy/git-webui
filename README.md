# Branch

A local GitHub clone dashboard with an asyncio/aiohttp backend and an HTML, CSS, and vanilla JavaScript frontend. Git runs directly through `asyncio.create_subprocess_exec`; Server-Sent Events stream stdout and stderr, including Git progress, to the browser.

## Run

Requires Python 3.10+ and Git on PATH.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

Open **http://127.0.0.1:8080**. Enter a GitHub owner and repository name; optionally enter a branch. An empty branch uses the default branch. Specifying a branch clones it with `--single-branch`.

Use the **Clone location** textbox to enter a server-side parent folder, or **Browse…** to choose one with the folder picker. The picker supports subfolders, parent navigation, and typed paths. Each clone gets a unique repository folder inside your chosen location; missing typed folders are created automatically. Relative paths resolve from the server’s working directory, and `~` expands to the server user’s home directory. The picker shows the server’s folders, even when your browser runs on a different machine.

Leave the location empty to save under `./clones/`. Override this default with `--clone-root /path/to/clones` or the `CLONE_ROOT` environment variable. `--host` and `--port` configure the listening address.

The UI provides live output, cancellation, automatic scrolling, log copying, and recent jobs. Closing or refreshing the browser does not stop a clone. Jobs and logs live in memory: restarting the server clears history but keeps cloned files. The server retains the latest 100 jobs and 2,000 output events per job; the browser displays up to 500,000 log characters. Cancelled or failed clones may leave partial files at the displayed destination. At most three jobs run at once. Shutdown stops active Git processes.

Public repositories work without authentication. Private repositories require credentials already configured on the server (for example, through a Git credential helper or a server-side `.netrc` file). Git uses configured credential helpers; interactive prompts remain disabled. The UI does not accept GitHub tokens. Git failures appear in the console.

The default binding is local. The app has no user authentication; use an authenticated reverse proxy before making it accessible to other users. Visitors share the server's jobs and filesystem. URLs are restricted to GitHub, inputs are validated, and subprocess arguments are passed without a shell.

## API

- `POST /api/clones`: JSON `{ "owner": "octocat", "repository": "Hello-World", "branch": "", "location": "" }`; returns the job with HTTP 202.
- `GET /api/directories?path=/path/to/folder`: lists server-side subfolders, the parent path, and the configured default clone location. Omit `path` to browse the default folder (or its nearest existing parent).
- `GET /api/clones`: retained jobs, newest first.
- `GET /api/clones/{id}/events`: SSE `status`, `output`, and terminal `done` events. Supports reconnection through `Last-Event-ID`.
- `POST /api/clones/{id}/cancel`: requests cancellation.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests use temporary local Git repositories and require no GitHub access. The Git process tests run with the Python standard library; API tests require aiohttp and skip when it is missing.
