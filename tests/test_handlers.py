"""Exercise destination API handlers without requiring a listening socket."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from datetime import datetime

try:
    from app import JOBS, cleanup, create_app, create_clone, errors, list_directories
except ImportError:
    create_app = None


@unittest.skipIf(create_app is None, 'Install requirements.txt to run API handler tests')
class DestinationHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = create_app(self.root)
        # Test handler behavior separately from worker-thread scheduling.
        async def inline_worker(function, *args):
            return function(*args)
        self.worker = patch('app.asyncio.to_thread', side_effect=inline_worker)
        self.worker.start()
        self.addCleanup(self.worker.stop)

    async def asyncTearDown(self):
        await cleanup(self.app)
        self.temp.cleanup()

    async def request(self, handler, *, payload=None, query=None):
        request = SimpleNamespace(app=self.app, json=AsyncMock(return_value=payload),
                                  query=query or {}, headers={}, method='POST' if payload is not None else 'GET',
                                  scheme='http', host='localhost', path='/api/test')
        response = await errors(request, handler)
        return response.status, json.loads(response.body)

    async def test_clone_uses_selected_location(self):
        custom = self.root / 'new folder' / 'nested'
        async def fake_clone(job):
            job.status = 'succeeded'
            await job.publish('done', job.summary())
        with patch('app.run_clone', fake_clone):
            status, data = await self.request(create_clone, payload={
                'owner': 'octocat', 'repository': 'demo', 'location': str(custom)})
        self.assertEqual(status, 202)
        self.assertEqual(Path(data['destination']).parent, custom)
        self.assertTrue(custom.is_dir())

    async def test_folder_formats_and_collisions(self):
        async def fake_clone(job):
            job.status = 'succeeded'
        with patch('app.run_clone', fake_clone), patch('app.datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 9)
            for value in [None, '', '{owner}--{repo}--{id}', '{date}_{owner}_{repo}_{id}']:
                payload = {'owner': 'octocat', 'repository': 'demo'}
                if value is not None:
                    payload['folder_format'] = value
                status, data = await self.request(create_clone, payload=payload)
                self.assertEqual(status, 202)
                await self.app[JOBS][data['id']].task
                name = Path(data['destination']).name
                if value == '{owner}--{repo}--{id}':
                    self.assertEqual(name, f"octocat--demo--{data['id']}")
                elif value == '{date}_{owner}_{repo}_{id}':
                    self.assertEqual(name, f"20261009_octocat_demo_{data['id']}")
                else:
                    self.assertEqual(name, '20261009-demo' if value is None else f"20261009-demo-{data['id']}")
            (self.root / 'existing').mkdir()
            status, data = await self.request(create_clone, payload={
                'owner': 'octocat', 'repository': 'demo', 'folder_format': 'existing'})
            self.assertEqual(status, 202)
            self.assertEqual(Path(data['destination']).name, f"existing-{data['id']}")

    async def test_invalid_folder_formats(self):
        for value in [12, None, '../{repo}', '/tmp/{repo}', r'folder\{repo}',
                      '.', '..', '{unknown}', '{repo', '{repo.__class__}',
                      '{id:8}', 'bad\x00name', 'a' * 201, 'あ' * 67]:
            with self.subTest(value=value):
                status, data = await self.request(create_clone, payload={
                    'owner': 'octocat', 'repository': 'demo', 'folder_format': value})
                self.assertEqual(status, 400)
                self.assertIn('error', data)

    async def test_browse_returns_folders_and_parent(self):
        (self.root / 'folder').mkdir()
        (self.root / 'file.txt').touch()
        status, data = await self.request(list_directories)
        self.assertEqual(status, 200)
        self.assertEqual(data['directories'], [{'name': 'folder', 'path': str(self.root / 'folder')}])
        self.assertEqual(data['parent'], str(self.root.parent))
        status, data = await self.request(list_directories, query={'path': str(self.root / 'folder')})
        self.assertEqual(status, 200)
        self.assertEqual(data['path'], str(self.root / 'folder'))
        self.assertEqual(data['directories'], [])

    async def test_invalid_location_and_permissions_are_json_errors(self):
        for value in [12, 'bad\x00path']:
            status, data = await self.request(create_clone, payload={
                'owner': 'octocat', 'repository': 'demo', 'location': value})
            self.assertEqual(status, 400)
            self.assertIn('error', data)
        for value in [str(self.root / 'missing'), 'bad\x00path']:
            status, data = await self.request(list_directories, query={'path': value})
            self.assertEqual(status, 400)
            self.assertIn('error', data)
        with patch('app.prepare_location', side_effect=PermissionError):
            status, data = await self.request(create_clone, payload={
                'owner': 'octocat', 'repository': 'demo', 'location': str(self.root)})
            self.assertEqual(status, 403)
            self.assertIn('error', data)
        with patch('app.browse_directories', side_effect=PermissionError):
            status, data = await self.request(list_directories)
            self.assertEqual(status, 403)
            self.assertIn('error', data)
