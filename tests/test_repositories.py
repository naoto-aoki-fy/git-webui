"""Catalog persistence and management, without HTTP sockets or GitHub."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import (REPOSITORIES, add_repository, clone_and_register, create_app,
                 delete_repository, errors, list_repositories)
from clone_jobs import Job


class RepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / 'catalog.sqlite3'
        self.app = create_app(self.root, self.database)
        self.folder = self.root / 'existing'
        subprocess.run(['git', 'init', str(self.folder)], check=True, capture_output=True)

    async def request(self, handler, payload=None, identifier='', method='GET', origin=None):
        request = SimpleNamespace(app=self.app, json=AsyncMock(return_value=payload),
            match_info={'id': identifier}, headers={'Origin': origin} if origin else {},
            method=method, scheme='http', host='localhost', path='/api/repositories')
        response = await errors(request, handler)
        return response.status, json.loads(response.body)

    async def test_add_duplicate_restart_delete_preserves_files(self):
        status, entry = await self.request(add_repository, {'path': str(self.folder)}, method='POST')
        self.assertEqual(status, 201)
        status, _ = await self.request(add_repository, {'path': str(self.folder / '..' / 'existing')}, method='POST')
        self.assertEqual(status, 409)
        self.app = create_app(self.root, self.database)
        status, entries = await self.request(list_repositories)
        self.assertEqual(entries, [entry])
        status, _ = await self.request(delete_repository, identifier=entry['id'], method='DELETE', origin='https://other.example')
        self.assertEqual(status, 403)
        status, _ = await self.request(delete_repository, identifier=entry['id'], method='DELETE')
        self.assertEqual(status, 200)
        self.assertTrue((self.folder / '.git').is_dir())
        self.assertEqual(create_app(self.root, self.database)[REPOSITORIES].list(), [])
        status, _ = await self.request(delete_repository, identifier=entry['id'], method='DELETE')
        self.assertEqual(status, 404)

    async def test_invalid_folders_and_payloads(self):
        for payload in [[], {}, {'path': 3}, {'path': ''}, {'path': 'bad\x00path'},
                        {'path': str(self.root)}, {'path': str(self.root / 'missing')}]:
            status, _ = await self.request(add_repository, payload, method='POST')
            self.assertEqual(status, 400)
        fake = self.root / 'fake'
        (fake / '.git').mkdir(parents=True)
        status, _ = await self.request(add_repository, {'path': str(fake)}, method='POST')
        self.assertEqual(status, 400)

    async def test_successful_clones_only_are_registered(self):
        job = Job('test', 'owner', 'repo', 'main', self.folder)
        async def succeed(job):
            job.status = 'succeeded'
        with patch('app.run_clone', succeed):
            await clone_and_register(self.app, job)
            await clone_and_register(self.app, job)
        entries = self.app[REPOSITORIES].list()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['owner'], 'owner')
        self.assertEqual(entries[0]['branch'], 'main')
        async def fail(job):
            job.status = 'failed'
        job.destination = self.root / 'failed'
        with patch('app.run_clone', fail):
            await clone_and_register(self.app, job)
        self.assertEqual(self.app[REPOSITORIES].list(), entries)
