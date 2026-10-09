import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from aiohttp.test_utils import TestClient, TestServer
    from app import JOBS, create_app
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, 'Install requirements.txt to run aiohttp API tests')
class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_page_and_assets(self):
        for path in ['/', '/static/app.js', '/static/style.css']:
            response = await self.client.get(path)
            self.assertEqual(response.status, 200)
        response = await self.client.get('/api/clones')
        self.assertEqual(await response.json(), [])

    async def test_invalid_inputs(self):
        for payload in [[], {'owner': '../bad', 'repository': 'demo'},
                        {'owner': 'owner', 'repository': '../bad'},
                        {'owner': 'owner', 'repository': 'demo', 'branch': '--help'},
                        {'owner': 'owner', 'repository': 'demo', 'branch': 'bad..ref'},
                        {'owner': 12, 'repository': 'demo'}]:
            response = await self.client.post('/api/clones', json=payload)
            self.assertEqual(response.status, 400)
            self.assertIn('error', await response.json())
        response = await self.client.post('/api/clones', data='not json')
        self.assertEqual(response.status, 400)

    async def test_cross_origin_and_missing_job(self):
        response = await self.client.post('/api/clones', json={}, headers={'Origin': 'https://other.example'})
        self.assertEqual(response.status, 403)
        response = await self.client.get('/api/clones/missing/events')
        self.assertEqual(response.status, 404)

    async def test_stream_replay_and_unique_destinations(self):
        async def fake_clone(job):
            job.status = 'running'
            await job.publish('output', 'first\nsecond\n')
            job.status = 'succeeded'
            job.exit_code = 0
            await job.publish('done', job.summary())
        with patch('app.run_clone', fake_clone):
            response = await self.client.post('/api/clones', json={'owner': 'octocat', 'repository': 'demo'})
            self.assertEqual(response.status, 202)
            first = await response.json()
            response = await self.client.post('/api/clones', json={'owner': 'octocat', 'repository': 'demo'})
            second = await response.json()
            self.assertNotEqual(first['destination'], second['destination'])
            self.assertEqual(Path(first['destination']).parent, Path(self.temp.name))
            response = await self.client.get(f"/api/clones/{first['id']}/events")
            text = await response.text()
            self.assertIn('event: output', text)
            self.assertIn('event: done', text)
            response = await self.client.get(f"/api/clones/{first['id']}/events", headers={'Last-Event-ID': '1'})
            text = await response.text()
            self.assertNotIn('event: output', text)
            self.assertIn('event: done', text)

    async def test_cancel_and_concurrency_limit(self):
        async def fake_clone(job):
            job.status = 'running'
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                job.status = 'cancelled'
                await job.publish('done', job.summary())
        with patch('app.run_clone', fake_clone):
            for _ in range(3):
                response = await self.client.post('/api/clones', json={'owner': 'octocat', 'repository': 'demo'})
                self.assertEqual(response.status, 202)
            response = await self.client.post('/api/clones', json={'owner': 'octocat', 'repository': 'demo'})
            self.assertEqual(response.status, 429)
            job = next(iter(self.app[JOBS].values()))
            response = await self.client.post(f'/api/clones/{job.id}/cancel')
            self.assertEqual(response.status, 200)
            await asyncio.wait_for(job.task, 2)
            self.assertEqual(job.status, 'cancelled')
