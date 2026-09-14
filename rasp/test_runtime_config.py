import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from install_state import prepare_runtime
from runtime_config import load_runtime, read_monitors


class RuntimeConfigTests(unittest.TestCase):
    def test_projection_keeps_direct_tokens_out_and_sources_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); device=root/'device.json'; shinobi=root/'shinobi.json'; target=root/'runtime.json'
            device.write_text(json.dumps({'deviceId':'serial','shinobiGroupKey':'arena','shinobiApiKey':'local-key','deviceGatewayToken':'forbidden','EXTERNAL_KEY':'forbidden'}))
            shinobi.write_text(json.dumps({'db':{'user':'custom','password':'local-db','database':'ccio','extra':'forbidden'},'passwordSalt':'forbidden'}))
            device.chmod(0o600); before=device.read_bytes()
            prepare_runtime(target,device,[shinobi])
            raw=target.read_text(); self.assertNotIn('forbidden',raw)
            self.assertEqual(device.read_bytes(),before)
            self.assertEqual(device.stat().st_mode & 0o777,0o600)
            self.assertEqual(target.stat().st_mode & 0o777,0o600)
            self.assertEqual(load_runtime(target)['device']['deviceId'],'serial')

    def test_configured_database_and_password_not_in_command(self):
        with patch('runtime_config.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=b'monitor')) as run:
            self.assertEqual(read_monitors('SELECT mid FROM Monitors',{'db':{'user':'custom','password':'hidden','host':'localhost','port':3306,'database':'ccio'}}),b'monitor')
            self.assertNotIn('hidden',str(run.call_args.args))
            self.assertEqual(run.call_args.kwargs['env']['MYSQL_PWD'],'hidden')
            self.assertIn('-ucustom',run.call_args.args[0])

    def test_database_failure_is_explicit_without_leaking_stderr(self):
        with patch('runtime_config.subprocess.run',return_value=SimpleNamespace(returncode=1,stdout=b'',stderr=b'secret')):
            with self.assertRaisesRegex(RuntimeError,'^SHINOBI_DATABASE_UNAVAILABLE$'):
                read_monitors('SELECT mid FROM Monitors',{'db':{}})

    def test_missing_database_does_not_write_partial_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);device=root/'device';device.write_text('{}')
            with self.assertRaises(ValueError):prepare_runtime(root/'runtime',device,[])
            self.assertFalse((root/'runtime').exists())
