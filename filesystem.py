"""Resolve clone locations and list server-side directories."""
from pathlib import Path


def resolve_location(value, default):
    if not isinstance(value, str):
        raise ValueError('Clone location must be a string.')
    value = value.strip()
    if len(value) > 4096 or any(ord(char) < 32 for char in value):
        raise ValueError('Enter a valid folder path.')
    return Path(value).expanduser().resolve() if value else Path(default).resolve()


def prepare_location(value, default):
    location = resolve_location(value, default)
    location.mkdir(parents=True, exist_ok=True)
    return location


def browse_directories(value, default):
    location = resolve_location(value, default)
    # The default clone directory may not exist until the first clone.
    if not value.strip():
        while not location.exists() and location != location.parent:
            location = location.parent
    if not location.is_dir():
        raise NotADirectoryError('The selected path is not an existing folder.')
    directories = []
    for entry in location.iterdir():
        try:
            if entry.is_dir():
                directories.append({'name': entry.name, 'path': str(entry.resolve())})
        except OSError:
            continue
    directories.sort(key=lambda entry: (entry['name'].casefold(), entry['name']))
    return {
        'path': str(location),
        'parent': str(location.parent) if location.parent != location else None,
        'default_location': str(Path(default).resolve()),
        'directories': directories,
    }
