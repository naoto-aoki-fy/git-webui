import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clone_jobs import Job, git_command, run_clone


class CloneTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.git('init', '-b', 'main')
        (self.source / 'README.md').write_text('main branch\n')
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-m', 'initial')
        self.git('checkout', '-b', 'feature/demo')
        (self.source / 'feature.txt').write_text('feature content\n')
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-m', 'feature')
        self.git('checkout', 'main')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.source), *args], check=True,
                              capture_output=True, text=True).stdout

    def job(self, branch=''):
        return Job('test', 'octocat', 'demo', branch, self.root / 'destination')

    def local_command(self, job):
        command = git_command(job)
        command[-2] = self.source.as_uri()
        return command

    async def test_default_branch_and_output(self):
        job = self.job()
        with patch('clone_jobs.git_command', self.local_command):
            await asyncio.wait_for(run_clone(job), 10)
        self.assertEqual(job.status, 'succeeded')
        self.assertEqual(job.exit_code, 0)
        self.assertEqual((job.destination / 'README.md').read_text(), 'main branch\n')
        self.assertFalse((job.destination / 'feature.txt').exists())
        self.assertTrue(any('Cloning into' in data for _, kind, data in job.events if kind == 'output'))
        self.assertEqual(job.events[-1][1], 'done')
        # Every output event can be encoded as a single SSE JSON data line.
        for _, _, data in job.events:
            self.assertNotIn('\n', json.dumps(data))

    async def test_selected_branch(self):
        job = self.job('feature/demo')
        with patch('clone_jobs.git_command', self.local_command):
            await asyncio.wait_for(run_clone(job), 10)
        self.assertEqual(job.status, 'succeeded')
        self.assertTrue((job.destination / 'feature.txt').exists())
        result = subprocess.run(['git', '-C', str(job.destination), 'branch', '--show-current'],
                                check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), 'feature/demo')

    async def test_missing_branch_failure(self):
        job = self.job('missing')
        with patch('clone_jobs.git_command', self.local_command):
            await asyncio.wait_for(run_clone(job), 10)
        self.assertEqual(job.status, 'failed')
        self.assertNotEqual(job.exit_code, 0)
        self.assertIn('missing', ''.join(data for _, kind, data in job.events if kind == 'output'))

    async def test_output_arrives_before_completion_and_cancel(self):
        job = self.job()
        command = [sys.executable, '-u', '-c',
                   "import time; print('live progress', flush=True); time.sleep(60)"]
        with patch('clone_jobs.git_command', return_value=command):
            task = asyncio.create_task(run_clone(job))
            async def wait_for_output():
                while not any(kind == 'output' and data == 'live progress\n' for _, kind, data in job.events):
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(wait_for_output(), 5)
            self.assertEqual(job.status, 'running')
            self.assertFalse(task.done())
            task.cancel()
            await asyncio.wait_for(task, 6)
        self.assertEqual(job.status, 'cancelled')
        self.assertIsNotNone(job.exit_code)
        self.assertEqual(job.events[-1][1], 'done')

    async def test_spawn_failure_is_terminal(self):
        job = self.job()
        with patch('clone_jobs.git_command', return_value=['/no/such/git']):
            await run_clone(job)
        self.assertEqual(job.status, 'failed')
        self.assertEqual(job.events[-1][1], 'done')

    async def test_history_is_bounded(self):
        job = self.job()
        for i in range(2010):
            await job.publish('output', str(i))
        self.assertEqual(len(job.events), 2000)
        self.assertEqual(job.events[0][0], 11)
        self.assertEqual(job.events[-1][0], 2010)

    def test_github_command_arguments(self):
        job = self.job('feature/demo')
        command = git_command(job)
        self.assertIn('--progress', command)
        self.assertIn('--single-branch', command)
        self.assertEqual(command[-3:], ['--', 'https://github.com/octocat/demo.git', str(job.destination)])
        self.assertEqual(command[command.index('--branch') + 1], 'feature/demo')
