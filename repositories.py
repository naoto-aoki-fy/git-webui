"""Persistent catalog of local Git working folders (files stay on disk)."""
from contextlib import contextmanager
import sqlite3
import uuid
from pathlib import Path

from filesystem import resolve_location


class RepositoryStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS repositories (
                id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '',
                branch TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )''')

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def list(self):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                'SELECT * FROM repositories ORDER BY created_at DESC, rowid DESC')]

    def add(self, path, name=None, owner='', branch=''):
        with self.connect() as connection:
            connection.execute('INSERT INTO repositories (id, path, name, owner, branch) VALUES (?, ?, ?, ?, ?)',
                               (uuid.uuid4().hex, str(path), name or Path(path).name, owner, branch))
            return dict(connection.execute('SELECT * FROM repositories WHERE path = ?', (str(path),)).fetchone())

    def delete(self, identifier):
        with self.connect() as connection:
            return connection.execute('DELETE FROM repositories WHERE id = ?', (identifier,)).rowcount > 0


def existing_repository(value, default):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Enter an existing Git folder.')
    path = resolve_location(value, default)
    # Require the selected working tree itself, rather than a child of a repo.
    if not path.is_dir() or not (path / '.git').exists():
        raise ValueError('Select a Git working folder containing .git.')
    return path
