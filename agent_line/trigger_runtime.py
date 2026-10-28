"""Load optional trigger bindings; no scheduling or state initialization."""
import os
from pathlib import Path
import stat

from .triggers import TriggerAPI
from .execution_runtime import _json


def load_trigger_api(path, *, projects, workflows):
    path = Path(path)
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError('Trigger configuration must not use symlinks')
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_size > 1048576):
        raise ValueError('Trigger configuration must be an owner-only regular file')
    value = _json(path)
    if (not isinstance(value, dict) or set(value) != {'schemaVersion', 'triggers'}
            or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1):
        raise ValueError('Unsupported trigger configuration')
    return TriggerAPI(workflows, value['triggers'], projects=projects)
