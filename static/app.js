const $ = (id) => document.getElementById(id);
const form = $('clone-form');
let selectedId = null;
let stream = null;
let jobs = [];
let outputText = '';
let refreshPromise = null;
const labels = { queued: 'Queued', running: 'Cloning', succeeded: 'Complete', failed: 'Failed', cancelled: 'Cancelled' };

async function api(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'The server could not complete the request.');
  return data;
}

function showError(message) {
  $('form-error').textContent = message;
  $('form-error').hidden = !message;
}

function applyStatus(job) {
  $('status').textContent = labels[job.status] || job.status;
  $('status').className = `status ${job.status}`;
  const active = ['queued', 'running'].includes(job.status);
  $('cancel-button').hidden = !active;
  $('cancel-button').disabled = false;
  $('output-note').textContent = active ? 'Streaming command output…' :
    job.exit_code !== null ? `Process exited with code ${job.exit_code}` : labels[job.status];
  $('destination').hidden = active;
  $('destination').textContent = job.status === 'succeeded' ? `Saved to ${job.destination}` :
    `Destination: ${job.destination} · ${job.status === 'failed' ? 'Clone failed; inspect the output above.' : 'Clone cancelled; partial files may remain.'}`;
}

function appendOutput(chunk) {
  outputText = (outputText + chunk).slice(-500000);
  $('output').textContent = outputText;
  if ($('auto-scroll').checked) $('terminal').scrollTop = $('terminal').scrollHeight;
}

function selectJob(job) {
  if (stream) stream.close();
  selectedId = job.id;
  outputText = '';
  $('output').textContent = '';
  $('output').hidden = false;
  $('empty-output').hidden = true;
  applyStatus(job);
  const source = new EventSource(`/api/clones/${job.id}/events`);
  stream = source;
  source.addEventListener('output', (event) => appendOutput(JSON.parse(event.data)));
  source.addEventListener('status', (event) => applyStatus(JSON.parse(event.data)));
  source.addEventListener('done', (event) => {
    applyStatus(JSON.parse(event.data));
    source.close();
    refreshJobs().catch((error) => showError(error.message));
  });
  source.onerror = () => {
    $('output-note').textContent = 'Connection interrupted. Reconnecting…';
  };
}

function renderHistory() {
  const list = $('history-list');
  list.replaceChildren();
  $('history-count').textContent = `${jobs.length} ${jobs.length === 1 ? 'REPOSITORY' : 'REPOSITORIES'}`;
  if (!jobs.length) {
    const empty = document.createElement('p');
    empty.className = 'history-empty';
    empty.textContent = 'Your cloned repositories will appear here.';
    list.append(empty);
  }
  for (const job of jobs) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'history-row';
    row.setAttribute('aria-label', `View output for ${job.owner}/${job.repository}`);
    const details = document.createElement('span');
    const title = document.createElement('strong');
    title.textContent = `${job.owner} / ${job.repository}`;
    const branch = document.createElement('small');
    branch.textContent = `⑂ ${job.branch || 'default branch'} · ${job.id.slice(0, 8)}`;
    details.append(title, branch);
    const status = document.createElement('span');
    status.className = `status ${job.status}`;
    status.textContent = labels[job.status];
    row.append(details, status);
    row.addEventListener('click', () => selectJob(job));
    list.append(row);
  }
}

function refreshJobs() {
  if (refreshPromise) return refreshPromise;
  refreshPromise = api('/api/clones').then((data) => {
    jobs = data;
    renderHistory();
    if (!selectedId && jobs.length) selectJob(jobs[0]);
  }).finally(() => { refreshPromise = null; });
  return refreshPromise;
}

for (const id of ['owner', 'repository']) {
  $(id).addEventListener('input', () => {
    $('repo-preview').textContent = `github.com / ${$('owner').value.trim() || 'owner'} / ${$('repository').value.trim() || 'repository'}`;
  });
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  showError('');
  $('clone-button').disabled = true;
  try {
    const job = await api('/api/clones', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.fromEntries(new FormData(form)))
    });
    selectJob(job);
    await refreshJobs();
  } catch (error) { showError(error.message); }
  finally { $('clone-button').disabled = false; }
});

$('cancel-button').addEventListener('click', async () => {
  $('cancel-button').disabled = true;
  try { await api(`/api/clones/${selectedId}/cancel`, { method: 'POST' }); }
  catch (error) { showError(error.message); $('cancel-button').disabled = false; }
});

$('copy-output').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(outputText);
    $('copy-output').textContent = 'Copied!';
    setTimeout(() => { $('copy-output').textContent = 'Copy log'; }, 1500);
  } catch { showError('Clipboard access is unavailable. Select the console text to copy it.'); }
});

$('auto-scroll').addEventListener('change', () => {
  if ($('auto-scroll').checked) $('terminal').scrollTop = $('terminal').scrollHeight;
});
refreshJobs().catch((error) => showError(`Unable to load clones: ${error.message}`));
setInterval(() => refreshJobs().catch(() => {}), 5000);

let folderTarget = 'location';
let folderPath = null;
let parentPath = null;
let folderRequest = 0;

async function loadFolders(path = '') {
  const requestId = ++folderRequest;
  folderPath = null;
  parentPath = null;
  $('select-folder').disabled = true;
  $('folder-up').disabled = true;
  $('folder-error').hidden = true;
  $('folder-current').textContent = 'Loading folders…';
  $('folder-list').replaceChildren();
  try {
    const data = await api(`/api/directories?path=${encodeURIComponent(path)}`);
    if (requestId !== folderRequest) return;
    folderPath = data.path;
    parentPath = data.parent;
    $('folder-path').value = data.path;
    $('folder-current').textContent = data.path;
    $('location').placeholder = data.default_location;
    $('select-folder').disabled = false;
    $('folder-up').disabled = !data.parent;
    for (const folder of data.directories) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = `▸ ${folder.name}`;
      button.setAttribute('aria-label', `Open folder ${folder.name}`);
      button.addEventListener('click', () => loadFolders(folder.path));
      $('folder-list').append(button);
    }
    if (!data.directories.length) {
      const empty = document.createElement('p');
      empty.textContent = 'No subfolders. You can select this folder.';
      $('folder-list').append(empty);
    }
  } catch (error) {
    if (requestId !== folderRequest) return;
    $('folder-current').textContent = '';
    $('folder-error').textContent = error.message;
    $('folder-error').hidden = false;
  }
}

$('browse-location').addEventListener('click', () => {
  folderTarget = 'location';
  $('folder-title').textContent = 'Choose clone location';
  $('folder-dialog').showModal();
  $('folder-path').value = $('location').value.trim();
  loadFolders($('location').value.trim());
});
$('folder-path-form').addEventListener('submit', (event) => {
  event.preventDefault();
  loadFolders($('folder-path').value.trim());
});
$('folder-path').addEventListener('input', () => {
  $('select-folder').disabled = $('folder-path').value.trim() !== folderPath;
});
$('folder-up').addEventListener('click', () => {
  if (parentPath) loadFolders(parentPath);
});
$('folder-default').addEventListener('click', () => loadFolders());
$('select-folder').addEventListener('click', () => {
  if (!folderPath) return;
  $(folderTarget).value = folderPath;
  $('folder-dialog').close();
});
for (const id of ['close-folder', 'cancel-folder']) {
  $(id).addEventListener('click', () => $('folder-dialog').close());
}
$('folder-dialog').addEventListener('close', () => { folderRequest += 1; });


function repositoryError(message = '') {
  $('repository-error').textContent = message;
  $('repository-error').hidden = !message;
}

async function refreshRepositories() {
  const entries = await api('/api/repositories');
  const list = $('repository-list');
  list.replaceChildren();
  if (!entries.length) {
    const empty = document.createElement('p');
    empty.className = 'history-empty';
    empty.textContent = 'No saved folders. Clone a repository or add an existing Git folder.';
    list.append(empty);
  }
  for (const entry of entries) {
    const row = document.createElement('div');
    row.className = 'history-row repository-row';
    const details = document.createElement('span');
    const title = document.createElement('strong');
    title.textContent = entry.owner ? `${entry.owner} / ${entry.name}` : entry.name;
    const path = document.createElement('small');
    path.textContent = entry.path;
    details.append(title, path);
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'secondary';
    remove.textContent = 'Delete entry';
    remove.setAttribute('aria-label', `Delete catalog entry for ${entry.name}`);
    remove.addEventListener('click', async () => {
      remove.disabled = true;
      repositoryError();
      try {
        await api(`/api/repositories/${entry.id}`, { method: 'DELETE' });
        await refreshRepositories();
      } catch (error) { repositoryError(error.message); }
      finally { remove.disabled = false; }
    });
    row.append(details, remove);
    list.append(row);
  }
}

$('repository-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  repositoryError();
  $('add-repository').disabled = true;
  try {
    await api('/api/repositories', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: $('existing-path').value.trim() })
    });
    $('existing-path').value = '';
    await refreshRepositories();
  } catch (error) { repositoryError(error.message); }
  finally { $('add-repository').disabled = false; }
});
$('browse-existing').addEventListener('click', () => {
  folderTarget = 'existing-path';
  $('folder-title').textContent = 'Choose an existing Git folder';
  $('folder-dialog').showModal();
  loadFolders($('existing-path').value.trim());
});
$('refresh-repositories').addEventListener('click', () => {
  repositoryError();
  refreshRepositories().catch((error) => repositoryError(error.message));
});
refreshRepositories().catch((error) => repositoryError(error.message));
setInterval(() => refreshRepositories().catch((error) => repositoryError(error.message)), 5000);
