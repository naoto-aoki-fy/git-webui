"""Background Git process execution and bounded event history."""
import asyncio
import codecs
import os
import shlex
import signal
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Job:
    id: str
    owner: str
    repository: str
    branch: str
    destination: Path
    status: str = 'queued'
    exit_code: int | None = None
    events: deque = field(default_factory=lambda: deque(maxlen=2000))
    sequence: int = 0
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    task: asyncio.Task | None = None
    cancel_requested: bool = False

    def summary(self):
        return dict(id=self.id, owner=self.owner, repository=self.repository,
                    branch=self.branch, destination=str(self.destination),
                    status=self.status, exit_code=self.exit_code)

    async def publish(self, kind, data):
        async with self.condition:
            self.sequence += 1
            self.events.append((self.sequence, kind, data))
            self.condition.notify_all()


def git_command(job):
    command = ['git', 'clone', '--progress']
    if job.branch:
        command += ['--branch', job.branch, '--single-branch']
    return command + ['--', f'https://github.com/{job.owner}/{job.repository}.git', str(job.destination)]


async def stop_process(process):
    if process.returncode is not None:
        return
    # Drain the pipe while stopping: a full stdout buffer can delay process.wait().
    async def drain():
        if process.stdout:
            while await process.stdout.read(4096):
                pass
    drain_task = asyncio.create_task(drain())
    try:
        if os.name == 'posix':
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        await drain_task
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        try:
            if os.name == 'posix':
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
    finally:
        await drain_task


async def run_clone(job):
    process = None
    job.status = 'running'
    try:
        await job.publish('status', job.summary())
        await job.publish('output', '$ ' + shlex.join(git_command(job)) + '\n')
        env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never'}
        # Shield process creation so cancellation cannot orphan a newly spawned Git.
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            *git_command(job), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=env,
            start_new_session=(os.name == 'posix')))
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            process = await spawn
            raise
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        while chunk := await process.stdout.read(4096):
            await job.publish('output', decoder.decode(chunk).replace('\r', '\n'))
        tail = decoder.decode(b'', final=True)
        if tail:
            await job.publish('output', tail)
        job.exit_code = await process.wait()
        job.status = 'succeeded' if job.exit_code == 0 else 'failed'
    except asyncio.CancelledError:
        if process:
            await stop_process(process)
            job.exit_code = process.returncode
        job.status = 'cancelled'
        await job.publish('output', '\nClone cancelled.\n')
    except Exception as exc:
        if process:
            await stop_process(process)
        job.status = 'failed'
        await job.publish('output', f'\nUnable to clone: {exc}\n')
    finally:
        await job.publish('done', job.summary())


