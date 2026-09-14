"""Only the local Shinobi credentials required by the detector, never gateway tokens."""
import json
import os
from pathlib import Path
import subprocess

RUNTIME_PATH = '/var/lib/gravae-hands-up/runtime.json'


def load_runtime(path=None):
    target = Path(path or os.environ.get('HANDS_UP_RUNTIME', RUNTIME_PATH))
    if not target.exists():
        # Existing LEGACY deployments retain their former configuration source.
        try:
            with open('/etc/gravae/device.json') as handle:
                device = json.load(handle)
        except (OSError, ValueError):
            device = {}
        return {'device': {k: device[k] for k in ('deviceId', 'shinobiGroupKey', 'shinobiApiKey') if k in device}, 'db': {}}
    with target.open() as handle:
        value = json.load(handle)
    if not isinstance(value, dict) or not isinstance(value.get('db'), dict) or not isinstance(value.get('device'), dict):
        raise ValueError('Invalid detector runtime configuration')
    return value


def read_monitors(sql, runtime=None):
    db = (runtime if runtime is not None else load_runtime()).get('db', {})
    cmd = ['mysql', '-u' + str(db.get('user') or 'majesticflame')]
    if db.get('host'):
        cmd.extend(['-h', str(db['host'])])
    if db.get('port'):
        cmd.extend(['-P', str(int(db['port']))])
    cmd.extend([str(db.get('database') or 'ccio'), '-N', '-B', '-e', sql])
    env = os.environ.copy()
    if db.get('password'):
        env['MYSQL_PWD'] = str(db['password'])
    else:
        env.pop('MYSQL_PWD', None)
    result = subprocess.run(cmd, env=env, capture_output=True, timeout=20)
    if result.returncode:
        # Raw stderr may contain server or credential details. Fail explicitly and safely.
        raise RuntimeError('SHINOBI_DATABASE_UNAVAILABLE')
    return result.stdout
