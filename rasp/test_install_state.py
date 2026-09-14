import json
from pathlib import Path
import stat
import tempfile
import unittest
from install_state import prepare


class InstallationState(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.legacy = root/'etc'/'gravae'/'hands-up.json'
        self.legacy.parent.mkdir(parents=True)
        self.destination = root/'state'/'config.json'

    def test_new_install_stays_disabled_and_does_not_change_agent_directory(self):
        before = self.legacy.parent.stat().st_mode
        prepare(self.destination, self.legacy, 'https://worker.test', 'https://ops.test/event')
        config = json.loads(self.destination.read_text())
        self.assertIs(config['ativo'], False)
        self.assertEqual(config['cameras'], {})
        self.assertEqual(self.legacy.parent.stat().st_mode, before)
        self.assertFalse(self.legacy.exists())
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.destination.parent.stat().st_mode), 0o700)

    def test_migration_preserves_switches_and_keeps_legacy_file_untouched(self):
        config = {'ativo': True, 'fps': 0.5, 'cameras': {'quadra01_camera01': True}, 'quadras': {'quadra01': True}, 'custom': 42}
        self.legacy.write_text(json.dumps(config))
        before = self.legacy.read_bytes()
        self.assertEqual(prepare(self.destination, self.legacy), 'migrated')
        self.assertEqual(json.loads(self.destination.read_text()), config)
        self.assertEqual(self.legacy.read_bytes(), before)

    def test_reinstall_prefers_current_state_over_old_migration_source(self):
        self.legacy.write_text('{"ativo":true}')
        prepare(self.destination, self.legacy)
        self.destination.write_text('{"ativo":false,"cameras":{"one":false}}')
        self.assertEqual(prepare(self.destination, self.legacy), 'existing')
        self.assertFalse(json.loads(self.destination.read_text())['ativo'])

    def test_urls_are_json_encoded_and_cannot_inject_config_fields(self):
        url = 'https://worker.test/quote"\n, "ativo":true'
        prepare(self.destination, self.legacy, url)
        self.assertEqual(json.loads(self.destination.read_text())['nuvem'], url)
        self.assertFalse(json.loads(self.destination.read_text())['ativo'])

    def test_malformed_legacy_is_not_silently_replaced(self):
        self.legacy.write_text('not json')
        with self.assertRaises(ValueError): prepare(self.destination, self.legacy)
        self.assertFalse(self.destination.exists())

    def test_symlink_sources_are_rejected(self):
        target = Path(self.temp.name)/'sensitive'
        target.write_text('{"token":"secret"}')
        self.legacy.symlink_to(target)
        with self.assertRaises(OSError): prepare(self.destination, self.legacy)
        self.assertFalse(self.destination.exists())
