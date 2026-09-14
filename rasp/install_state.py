"""Prepare Hands-up state without granting write access to agent credentials."""
import json
import os
from pathlib import Path
import stat
import uuid


def read_config(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError('Configuration must be a regular file')
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError('Configuration must be an object')
    return value


def prepare(destination, legacy, cloud='', webhook='', owner=None):
    destination, legacy = Path(destination), Path(legacy)
    if destination.parent.is_symlink() or destination.is_symlink():
        raise ValueError('Configuration path must not be a symlink')
    if destination.exists():
        data = read_config(destination)
        source = 'existing'
    elif legacy.exists() or legacy.is_symlink():
        data = read_config(legacy)
        source = 'migrated'
    else:
        data = {'ativo': False, 'fps': 1.0, 'quadras': {}, 'cameras': {}}
        source = 'new'
    if cloud:
        data['nuvem'] = cloud
    if webhook:
        data['webhook'] = webhook
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.parent.chmod(0o700)
    if owner is not None:
        os.chown(destination.parent, *owner)
    temp = destination.with_name('.config-' + uuid.uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            if owner is not None:
                os.fchown(handle.fileno(), *owner)
            os.fsync(handle.fileno())
        os.replace(temp, destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temp.exists():
            temp.unlink()
    return source


if __name__ == '__main__':
    import pwd
    import sys
    user = pwd.getpwnam(sys.argv[1])
    result = prepare('/var/lib/gravae-hands-up/config.json', '/etc/gravae/hands-up.json',
                     sys.argv[2], sys.argv[3], (user.pw_uid, user.pw_gid))
    print('    configuration: ' + result + ' (isolated state directory)')
