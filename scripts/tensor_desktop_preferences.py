"""Small, local Desktop settings shared by independent GUI features."""

import json
import os
from pathlib import Path
import tempfile

PREFERENCES = "desktop-preferences.json"


def read_json(path):
    try:
        data = Path(path).read_text(encoding="utf-8")
        value = json.loads(data)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path, data):
    """Replace the file atomically, without leaving a partial JSON document."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def load(directory, key):
    """A preference is on by default; only an explicit false turns it off."""
    return read_json(Path(directory) / PREFERENCES).get(key, True) is not False


def save(directory, key, enabled):
    """Preserve the choices for the other Desktop features."""
    path = Path(directory) / PREFERENCES
    data = read_json(path)
    data[key] = bool(enabled)
    write_json(path, data)
