import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app as server


INFO = {'streams': [{'index': 0, 'codec_type': 'video', 'codec_name': 'h264'},
                    {'index': 1, 'codec_type': 'audio', 'codec_name': 'aac'}],
        'format': {'duration': '60'}}


class StrmTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.source = self.root / 'movie.STRM'
        self.video = self.root / 'video.mp4'
        self.video.write_bytes(b'0123456789')
        self.tracker = server.StreamTracker()
        for item in [patch.object(server, 'authentication_required', return_value=False),
                     patch.object(server, 'get_root', return_value=self.root),
                     patch.object(server, 'load_roots', return_value=[self.root]),
                     patch.object(server, 'path_is_excluded', return_value=False),
                     patch.object(server, 'stream_tracker', self.tracker)]:
            item.start()
            self.addCleanup(item.stop)
        self.client = server.app.test_client()

    def write(self, content):
        self.source.write_text(content, encoding='utf-8')

    def test_browse_and_pages_show_strm_as_video(self):
        self.write('https://example.com/movie.mp4?token=secret')
        with patch.object(server, 'probe', return_value=INFO):
            browse = self.client.get('/browse/0/')
            self.assertIn(b'movie.STRM', browse.data)
            for route in ['video', 'play']:
                response = self.client.get(f'/{route}/0/movie.STRM')
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(b'token=secret', response.data)
            self.assertIn(b'"native": false', response.data)

    def test_probe_preserves_remote_url_and_supports_bom_comments(self):
        url = 'https://example.com/movie.m3u8?token=a%2Fb&x=2'
        self.write('\ufeff#EXTM3U\n\n' + url + '\n')
        with patch.object(server.subprocess, 'run', return_value=Mock(returncode=0, stdout=json.dumps(INFO))) as run:
            self.assertEqual(server.probe(self.source), INFO)
            self.assertEqual(run.call_args.args[0][-1], url)

    def test_unavailable_remote_source_returns_clear_error(self):
        self.write('https://example.com/expired')
        with patch.object(server.subprocess, 'run', return_value=Mock(returncode=1, stderr='secret upstream error')):
            response = self.client.get('/play/0/movie.STRM')
            self.assertEqual(response.status_code, 502)
            self.assertNotIn(b'secret upstream error', response.data)
        with patch.object(server.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ffprobe', 30)):
            self.assertEqual(self.client.get('/video/0/movie.STRM').status_code, 504)

    def test_unknown_duration_and_size(self):
        self.write('https://example.com/live.m3u8')
        with patch.object(server, 'probe', return_value={'streams': INFO['streams']}):
            response = self.client.get('/video/0/movie.STRM')
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'Live / unknown', response.data)
            self.assertIn(b'<dt>Size</dt><dd>Unknown</dd>', response.data)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_real_local_pointer_probe_and_transcoding(self):
        subprocess.run([server.FFMPEG, '-v', 'error', '-f', 'lavfi', '-i',
                        'testsrc2=size=320x180:rate=12', '-t', '0.5', '-c:v', 'libx264',
                        '-y', str(self.video)], check=True, capture_output=True, timeout=15)
        self.write('video.mp4')
        response = self.client.get('/play/0/movie.STRM')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"native": true', response.data)
        with patch.object(server, 'selected_transcoder', return_value={'ok': True, 'key': 'software', 'label': 'Software'}), patch.object(server, 'codec_transcoder', return_value=('software', '')):
            response = self.client.get('/transcode/0/movie.STRM?quality=360p', buffered=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'ftyp', response.data[:32])
        self.assertEqual(self.tracker.snapshot(), [])

    def test_local_direct_ranges_and_metrics_use_pointer_identity(self):
        self.write('video.mp4')
        with server.app.test_request_context('/'):
            self.assertTrue(server.direct_playback_ok(self.source, INFO, [{'codec': 'aac'}]))
        with self.client.session_transaction() as session:
            session['playback_owner'] = 'owner'
        response = self.client.get('/media/0/movie.STRM?playback_id=x', headers={'Range': 'bytes=2-5'}, buffered=True)
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, b'2345')
        self.assertEqual(response.mimetype, 'video/mp4')
        self.assertEqual(response.headers['Content-Range'], 'bytes 2-5/10')
        metrics = self.client.get('/api/playback-info/0/movie.STRM?playback_id=x').json['stream']
        self.assertEqual(metrics['bytes_sent'], 4)
        self.assertFalse(metrics['active'])
        self.write(str(self.video))
        self.assertEqual(self.client.get('/media/0/movie.STRM', buffered=True).data, b'0123456789')

    def test_remote_transcoding_subtitles_and_no_raw_descriptor_delivery(self):
        url = 'https://example.com/stream.m3u8?token=abc'
        self.write(url)
        self.assertEqual(self.client.get('/media/0/movie.STRM').status_code, 415)
        process = Mock(stdout=io.BytesIO(b'mp4-data'), stderr=io.BytesIO(), returncode=0)
        process.poll.return_value = 0
        with patch.object(server, 'selected_transcoder', return_value={'ok': True, 'key': 'software', 'label': 'Software'}), patch.object(server, 'codec_transcoder', return_value=('software', '')), patch.object(server, 'probe', return_value=INFO), patch.object(server.subprocess, 'Popen', return_value=process) as popen:
            response = self.client.get('/transcode/0/movie.STRM?audio=1&start=12&quality=480p', buffered=True)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b'mp4-data')
            command = popen.call_args.args[0]
            self.assertEqual(command[command.index('-i') + 1], url)
            self.assertEqual(command[command.index('-ss') + 1], '12.000')
            self.assertEqual(self.tracker.snapshot(), [])
        with patch.object(server.subprocess, 'run', return_value=Mock(returncode=0, stdout=b'WEBVTT')) as run:
            response = self.client.get('/subtitle/0/movie.STRM?stream=2')
            self.assertEqual(response.data, b'WEBVTT')
            command = run.call_args.args[0]
            self.assertEqual(command[command.index('-i') + 1], url)

    def test_invalid_pointers_fail_before_launching_media_tools(self):
        for content in ['', '# comment', 'one.mp4\ntwo.mp4', 'plugin://video/123',
                        'file:///etc/passwd', 'concat:a|b', 'http://', 'https://host:bad/a',
                        'https://host/a|User-Agent=x', 'https://host/a\x00', 'x' * 65537,
                        'movie.STRM']:
            with self.subTest(content=content[:60]), patch.object(server.subprocess, 'Popen') as process:
                self.write(content)
                self.assertEqual(self.client.get('/transcode/0/movie.STRM').status_code, 400)
                process.assert_not_called()
        self.source.write_bytes(b'\xff')
        self.assertEqual(self.client.get('/media/0/movie.STRM').status_code, 400)

    def test_local_paths_cannot_escape_roots_or_exclusions(self):
        for target in ['../outside.mp4', '/etc/passwd']:
            self.write(target)
            self.assertEqual(self.client.get('/media/0/movie.STRM').status_code, 403)
        self.write('missing.mp4')
        self.assertEqual(self.client.get('/media/0/movie.STRM').status_code, 404)
        self.write('video.mp4')
        with patch.object(server, 'path_is_excluded', side_effect=lambda path: path == self.video):
            self.assertEqual(self.client.get('/media/0/movie.STRM').status_code, 404)
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / 'secret.mp4'
            target.touch()
            (self.root / 'link.mp4').symlink_to(target)
            self.write('link.mp4')
            self.assertEqual(self.client.get('/media/0/movie.STRM').status_code, 403)


if __name__ == '__main__':
    unittest.main()
