import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as server


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name) / 'config.txt'
        self.config.write_text('# Personal comment\nport=8060\ncustom_option=keep\n/mnt/media\n')
        self.patches = [
            patch.object(server, 'CONFIG_FILE', self.config),
            patch.object(server, 'APP_PASSWORD', ''),
            patch.object(server, 'USE_MASTER_PASSWORD', False),
            patch.object(server, 'TRANSCODER_PREFERENCE', 'auto'),
            patch.object(server, 'SETTINGS', server.load_settings()),
            patch.dict(os.environ, {k: v for k, v in os.environ.items() if not k.startswith('LANSTREAM_')}, clear=True),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        server.app.config['TESTING'] = True
        self.client = server.app.test_client()

    def form(self, **overrides):
        response = self.client.get('/settings')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        with self.client.session_transaction() as session:
            csrf = session['settings_csrf']
        data = dict(csrf=csrf, version=server.configuration_version(), port='8060',
                    transcoder='software', mode='none', public_paths='/mnt/media')
        data.update(overrides)
        return data

    def test_save_and_reload(self):
        response = self.client.post('/settings', data=self.form(port='9080', default_quality='720p', public_paths='/mnt/Movies\n/mnt/TV'), follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Settings saved', response.data)
        self.assertEqual(server.TRANSCODER_PREFERENCE, 'software')
        self.assertEqual(server.load_settings()['port'], '9080')
        self.assertEqual(server.load_settings()['default_quality'], '720p')
        self.assertEqual([str(p) for p in server.load_profiles()[0]['roots']], ['/mnt/Movies', '/mnt/TV'])
        self.assertIn('# Personal comment', self.config.read_text())
        self.assertIn('custom_option=keep', self.config.read_text())

    def test_invalid_values_never_modify_file(self):
        for invalid in [dict(port='0'), dict(port='65536'), dict(transcoder='bad'), dict(default_quality='bad'),
                        dict(public_paths='relative/path'), dict(public_paths='/mnt/a\n<injected>'),
                        dict(master_password='abc\nport=1'), dict(mode='profiles')]:
            with self.subTest(invalid=invalid):
                before = self.config.read_bytes()
                self.assertEqual(self.client.post('/settings', data=self.form(**invalid)).status_code, 400)
                self.assertEqual(self.config.read_bytes(), before)

    def test_audio_only_default_is_saved_and_available(self):
        response = self.client.post('/settings', data=self.form(default_quality='audio_only'), follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(server.load_settings()['default_quality'], 'audio_only')
        self.assertIn(b'Original video', response.data)

    def test_csrf_and_stale_page(self):
        data = self.form()
        before = self.config.read_bytes()
        self.assertEqual(self.client.post('/settings', data={**data, 'csrf': 'wrong'}).status_code, 400)
        self.assertEqual(self.config.read_bytes(), before)
        self.config.write_text(self.config.read_text() + '# External edit\n')
        self.assertEqual(self.client.post('/settings', data=data).status_code, 400)
        self.assertIn('# External edit', self.config.read_text())

    def test_profiles_and_restricted_access(self):
        data = self.form(mode='profiles', profile_row=['0', '1'], password_0='Administrator',
                         paths_0='/mnt/media\n-/Private', password_1='Viewer', paths_1='/mnt/shared')
        self.assertEqual(self.client.post('/settings', data=data).status_code, 302)
        profiles = server.load_profiles()
        self.assertEqual(str(profiles[0]['excluded'][0]), '/mnt/media/Private')
        response = self.client.get('/settings')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'Administrator', response.data)
        self.assertNotIn(b'Viewer', response.data)
        self.client.get('/logout')
        self.assertEqual(self.client.get('/settings').status_code, 302)
        self.client.post('/login', data={'password': 'Viewer'})
        self.assertEqual(self.client.get('/settings').status_code, 403)
        self.assertEqual(self.client.post('/settings', data=data).status_code, 403)
        self.client.get('/logout')
        self.client.post('/login', data={'password': 'Administrator'})
        data = self.form(mode='profiles', profile_row=['0', '1'], original_0='0', original_1='1',
                         paths_0='/mnt/media', paths_1='/mnt/shared')
        self.assertEqual(self.client.post('/settings', data=data).status_code, 302)
        self.assertEqual([p['password'] for p in server.load_profiles()], ['Administrator', 'Viewer'])

    def test_master_password_and_revoked_sessions(self):
        data = self.form(mode='master', master_password='Secret')
        self.assertEqual(self.client.post('/settings', data=data).status_code, 302)
        self.assertTrue(server.USE_MASTER_PASSWORD)
        other = server.app.test_client()
        self.assertEqual(other.get('/settings').status_code, 302)
        other.post('/login', data={'password': 'Secret'})
        self.assertEqual(other.get('/settings').status_code, 200)
        self.client.post('/settings', data=self.form(mode='master', master_password='NewSecret'))
        self.assertEqual(other.get('/settings').status_code, 302)

    def test_unicode_password(self):
        data = self.form(mode='master', master_password='caffè🔑')
        self.assertEqual(self.client.post('/settings', data=data).status_code, 302)
        self.client.get('/logout')
        self.assertEqual(self.client.post('/login', data={'password': 'caffè🔑'}).status_code, 302)
        self.assertEqual(self.client.get('/settings').status_code, 200)

    def test_write_failure_leaves_config_intact(self):
        data = self.form(public_paths='/mnt/updated')
        before = self.config.read_bytes()
        with patch.object(server.os, 'replace', side_effect=OSError('read only')):
            response = self.client.post('/settings', data=data)
        self.assertEqual(response.status_code, 500)
        self.assertIn(b'/mnt/updated', response.data)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(list(self.config.parent.iterdir()), [self.config])

    def test_environment_overrides(self):
        with patch.dict(os.environ, {'LANSTREAM_TRANSCODER': 'nvenc'}):
            data = self.form()
            self.assertEqual(self.client.post('/settings', data=data).status_code, 302)
            self.assertEqual(server.TRANSCODER_PREFERENCE, 'nvenc')
            self.assertIn(b'LANSTREAM_TRANSCODER', self.client.get('/settings').data)


if __name__ == '__main__':
    unittest.main()
