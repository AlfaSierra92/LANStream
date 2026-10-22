import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import app as server


class HlsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.source = self.root / 'movie.mkv'
        self.source.touch()
        self.info = {'format': {'duration': '13', 'format_name': 'matroska,webm'},
                     'streams': [{'index': 0, 'codec_type': 'video', 'codec_name': 'h264'},
                                 {'index': 1, 'codec_type': 'audio', 'codec_name': 'aac'}]}
        for item in [patch.object(server, 'authentication_required', return_value=False),
                     patch.object(server, 'get_root', return_value=self.root),
                     patch.object(server, 'path_is_excluded', return_value=False),
                     patch.object(server, 'probe', side_effect=lambda _: self.info),
                     patch.object(server, 'stream_tracker', server.StreamTracker()),
                     patch.object(server, 'selected_transcoder', return_value={
                         'ok': True, 'key': 'software', 'label': 'Software'}),
                     patch.object(server, 'codec_transcoder', return_value=('software', ''))]:
            item.start()
            self.addCleanup(item.stop)
        self.client = server.app.test_client()

    def playlist(self, query=''):
        return self.client.get('/transcode/0/movie.mkv?format=hls&quality=360p&audio=1' + query)

    def test_playlist_preserves_options_and_bounds_final_segment(self):
        with patch.object(server.media_runtime, 'popen') as popen:
            response = self.playlist('&start=2&playback_id=mine&root_index=99&relpath=bad')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, 'application/vnd.apple.mpegurl')
            text = response.get_data(as_text=True)
            self.assertIn('#EXT-X-TARGETDURATION:6', text)
            self.assertIn('#EXT-X-ENDLIST', text)
            self.assertEqual(text.count('#EXT-X-DISCONTINUITY'), 1)
            self.assertIn('#EXTINF:5.000000,', text)
            urls = [line for line in text.splitlines() if line.startswith('/')]
            self.assertEqual(len(urls), 2)
            for index, url in enumerate(urls):
                self.assertEqual(urlsplit(url).path, '/transcode/0/movie.mkv')
                params = parse_qs(urlsplit(url).query)
                self.assertEqual(params['segment'], [str(index)])
                self.assertEqual(params['format'], ['hls_segment'])
                self.assertEqual(params['start'], ['2'])
                self.assertEqual(params['audio'], ['1'])
                self.assertEqual(params['playback_id'], ['mine'])
            popen.assert_not_called()

    def test_invalid_segments_formats_and_copy_mode_do_not_start_encoder(self):
        with patch.object(server.media_runtime, 'popen') as popen:
            for query, status in [('format=unknown', 400),
                                  ('format=hls&quality=source', 409),
                                  ('format=hls&quality=audio_only', 400),
                                  ('format=hls&video_codec=hevc', 400),
                                  ('format=hls&audio_codec=opus', 400),
                                  ('format=hls&start=13', 416),
                                  ('format=hls_segment&segment=bad', 400),
                                  ('format=hls_segment&segment=-1', 416),
                                  ('format=hls_segment&segment=3', 416)]:
                response = self.client.get('/transcode/0/movie.mkv?' + query)
                self.assertEqual(response.status_code, status, query)
            popen.assert_not_called()

    def test_segment_is_bounded_and_releases_encoder_on_close(self):
        process = Mock(stdout=io.BytesIO(b'transport-stream'), stderr=io.BytesIO(), returncode=0)
        process.poll.return_value = 0
        with patch.object(server.media_runtime, 'popen', return_value=process) as popen:
            response = self.client.get('/transcode/0/movie.mkv?format=hls_segment&segment=1&start=2&audio=1', buffered=True)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, 'video/mp2t')
            self.assertEqual(response.data, b'transport-stream')
            command = popen.call_args.args[0]
            self.assertEqual(command[command.index('-ss') + 1], '8.000')
            self.assertEqual(command[command.index('-t') + 1], '5.000000')
            self.assertEqual(command[command.index('-f') + 1], 'mpegts')
            self.assertNotIn('-movflags', command)
            self.assertEqual(server.stream_tracker.snapshot(), [])
            self.assertTrue(process.stdout.closed)
        with patch.object(server.media_runtime, 'popen') as popen:
            response = self.client.head('/transcode/0/movie.mkv?format=hls_segment&segment=0')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b'')
            popen.assert_not_called()

    def test_segments_recheck_media_permissions(self):
        from werkzeug.exceptions import Forbidden
        with patch.object(server, 'get_root', side_effect=Forbidden), patch.object(server.media_runtime, 'popen') as popen:
            self.assertEqual(self.playlist().status_code, 403)
            self.assertEqual(self.client.get('/transcode/0/movie.mkv?format=hls_segment&segment=0').status_code, 403)
            popen.assert_not_called()

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_real_mkv_segments_decode_as_a_complete_hls_timeline(self):
        subprocess.run([server.FFMPEG, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=12',
                        '-f', 'lavfi', '-i', 'sine=sample_rate=48000', '-t', '13', '-c:v', 'libx264',
                        '-c:a', 'aac', '-y', str(self.source)], check=True, capture_output=True, timeout=15)
        self.info = json.loads(subprocess.run([server.FFPROBE, '-v', 'error', '-show_format', '-show_streams',
                                              '-of', 'json', str(self.source)], check=True,
                                             capture_output=True, timeout=10).stdout)
        # A nonzero seek exercises independent encoder timestamps as well as
        # initial decoding. Segment files exist only in this test fixture.
        text = self.playlist('&start=1').get_data(as_text=True)
        lines = []
        for line in text.splitlines():
            if line.startswith('/'):
                response = self.client.get(line, buffered=True)
                self.assertEqual(response.status_code, 200, response.data[:200])
                self.assertEqual(response.data[0], 0x47)
                filename = f'segment-{len(lines)}.ts'
                (self.root / filename).write_bytes(response.data)
                lines.append(filename)
            else:
                lines.append(line)
        playlist = self.root / 'test.m3u8'
        playlist.write_text('\n'.join(lines) + '\n')
        output = subprocess.run([server.FFMPEG, '-v', 'error', '-i', str(playlist), '-map', '0:v:0',
                                 '-f', 'framemd5', '-'], check=True, capture_output=True, timeout=20)
        frames = [line for line in output.stdout.splitlines() if line and not line.startswith(b'#')]
        self.assertGreaterEqual(len(frames), 143)
        self.assertLessEqual(len(frames), 145)
        self.assertEqual(server.stream_tracker.snapshot(), [])


if __name__ == '__main__':
    unittest.main()
