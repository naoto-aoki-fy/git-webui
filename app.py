"""Local GitHub clone dashboard. Run with python3 app.py."""
import argparse
import asyncio
import json
import os
import re
import shutil
import uuid
from pathlib import Path

from aiohttp import web
from clone_jobs import Job, run_clone
from filesystem import browse_directories, prepare_location

STATIC = Path(__file__).parent / 'static'
MAX_JOBS = 100
MAX_ACTIVE = 3
JOBS = web.AppKey('jobs', dict)
ROOT = web.AppKey('root', Path)


@web.middleware
async def errors(request, handler):
    origin = request.headers.get('Origin')
    if request.method == 'POST' and origin and origin != f'{request.scheme}://{request.host}':
        return web.json_response({'error': 'Cross-origin requests are not allowed.'}, status=403)
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        if not request.path.startswith('/api/'):
            raise
        response = web.json_response({'error': exc.reason}, status=exc.status)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    return response


async def index(request):
    return web.FileResponse(STATIC / 'index.html')


async def filesystem_call(function, *args):
    try:
        return await asyncio.to_thread(function, *args)
    except (ValueError, RuntimeError):
        raise web.HTTPBadRequest(reason='Enter a valid folder path.')
    except PermissionError:
        raise web.HTTPForbidden(reason='The server does not have permission to access this folder.')
    except OSError:
        raise web.HTTPBadRequest(reason='The folder could not be accessed. Check the path and server permissions.')


async def list_directories(request):
    result = await filesystem_call(
        browse_directories, request.query.get('path', ''), request.app[ROOT])
    return web.json_response(result)


async def create_clone(request):
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise web.HTTPBadRequest(reason='Send a JSON object with owner and repository.')
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(reason='Expected a JSON object.')
    values = [payload.get(key, '') for key in ('owner', 'repository', 'branch')]
    if any(not isinstance(value, str) for value in values):
        raise web.HTTPBadRequest(reason='Owner, repository, and branch must be strings.')
    owner, repository, branch = [value.strip() for value in values]
    if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?', owner):
        raise web.HTTPBadRequest(reason='Enter a valid GitHub owner (1–39 letters, numbers, or hyphens).')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', repository) or repository in {'.', '..'}:
        raise web.HTTPBadRequest(reason='Enter a valid repository name (letters, numbers, dots, hyphens, or underscores).')
    if branch:
        if len(branch) > 255 or branch.startswith('-') or '@{' in branch or any(ord(c) < 32 for c in branch):
            raise web.HTTPBadRequest(reason='Enter a valid Git branch name.')
        check = await asyncio.create_subprocess_exec(
            'git', 'check-ref-format', '--branch', branch,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        if await check.wait() != 0:
            raise web.HTTPBadRequest(reason='Enter a valid Git branch name.')
    location = payload.get('location', '')
    if not isinstance(location, str):
        raise web.HTTPBadRequest(reason='Clone location must be a string.')
    jobs = request.app[JOBS]
    if sum(job.status in {'queued', 'running'} for job in jobs.values()) >= MAX_ACTIVE:
        raise web.HTTPTooManyRequests(reason='Three clones are already running. Wait for one to finish.')
    root = await filesystem_call(prepare_location, location, request.app[ROOT])
    if sum(job.status in {'queued', 'running'} for job in jobs.values()) >= MAX_ACTIVE:
        raise web.HTTPTooManyRequests(reason='Three clones are already running. Wait for one to finish.')
    while len(jobs) >= MAX_JOBS:
        completed = next((key for key, job in jobs.items() if job.status not in {'queued', 'running'}), None)
        if completed is None:
            break
        del jobs[completed]
    identifier = uuid.uuid4().hex
    job = Job(identifier, owner, repository, branch, root / f'{owner}--{repository}--{identifier}')
    jobs[job.id] = job
    job.task = asyncio.create_task(run_clone(job))
    return web.json_response(job.summary(), status=202)


async def list_clones(request):
    return web.json_response([job.summary() for job in reversed(list(request.app[JOBS].values()))])


def get_job(request):
    job = request.app[JOBS].get(request.match_info['id'])
    if job is None:
        raise web.HTTPNotFound(reason='Clone job not found.')
    return job


async def cancel_clone(request):
    job = get_job(request)
    if job.task and not job.task.done() and not job.cancel_requested:
        job.cancel_requested = True
        await asyncio.sleep(0)
        job.task.cancel()
    return web.json_response(job.summary())


async def stream_events(request):
    job = get_job(request)
    try:
        cursor = max(0, int(request.headers.get('Last-Event-ID', '0')))
    except ValueError:
        raise web.HTTPBadRequest(reason='Invalid event ID.')
    response = web.StreamResponse(headers={'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
    await response.prepare(request)
    try:
        while True:
            async with job.condition:
                pending = [event for event in job.events if event[0] > cursor]
                terminal = job.status not in {'queued', 'running'}
                if not pending and not terminal:
                    try:
                        await asyncio.wait_for(job.condition.wait(), 15)
                    except asyncio.TimeoutError:
                        pass
                    pending = [event for event in job.events if event[0] > cursor]
                    terminal = job.status not in {'queued', 'running'}
            for number, kind, data in pending:
                await response.write(f'id: {number}\nevent: {kind}\ndata: {json.dumps(data)}\n\n'.encode())
                cursor = number
            if terminal:
                break
            if not pending:
                await response.write(b': heartbeat\n\n')
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return response


async def cleanup(app):
    tasks = [job.task for job in app[JOBS].values() if job.task and not job.task.done()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def create_app(clone_root=None):
    app = web.Application(middlewares=[errors], client_max_size=4096)
    app[JOBS] = {}
    app[ROOT] = Path(clone_root or os.environ.get('CLONE_ROOT', 'clones')).resolve()
    app.router.add_get('/', index)
    app.router.add_get('/api/directories', list_directories)
    app.router.add_get('/api/clones', list_clones)
    app.router.add_post('/api/clones', create_clone)
    app.router.add_get('/api/clones/{id}/events', stream_events)
    app.router.add_post('/api/clones/{id}/cancel', cancel_clone)
    app.router.add_static('/static/', STATIC)
    app.on_shutdown.append(cleanup)
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--clone-root', default=None)
    args = parser.parse_args()
    if not shutil.which('git'):
        parser.error('Git must be installed and available on PATH.')
    web.run_app(create_app(args.clone_root), host=args.host, port=args.port)
