"""Exercise destination API handlers without requiring a listening socket."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    from app import cleanup, create_app, create_clone, errors, list_directories
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
