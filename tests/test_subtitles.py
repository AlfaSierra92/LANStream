from pathlib import Path
import json
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import app as server
from media_runtime import MediaRuntime
from subtitle_windows import capture_packets


class SubtitleTests(unittest.TestCase):
    def test_packet_reader_stops_on_json_without_waiting_for_newline_or_eof(self):
        runtime = MediaRuntime()
        self.addCleanup(runtime.shutdown)
        payload = '{"packets": [{"pts_time": "1", "duration_time": "2", "data": "hello"}'
        command = [sys.executable, '-u', '-c', 'import sys,time;sys.stdout.write(sys.argv[1]);sys.stdout.flush();time.sleep(60)', payload]
        rows = capture_packets(runtime, command, count=1, timeout=3)
        self.assertEqual(rows[0]['data'], 'hello')
        self.assertFalse(runtime._processes)

    def test_packet_reader_timeout_reaps_child(self):
        runtime = MediaRuntime()
        self.addCleanup(runtime.shutdown)
        with self.assertRaises(subprocess.TimeoutExpired):
            capture_packets(runtime, [sys.executable, '-c', 'import time;time.sleep(60)'], count=1, timeout=0.1)
        self.assertFalse(runtime._processes)

    def test_runtime_accepts_in_memory_conversion_input(self):
        runtime = MediaRuntime()
        self.addCleanup(runtime.shutdown)
        result = runtime.run([sys.executable, '-c', 'import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())'],
                             input=b'RAM-only subtitle data', capture_output=True, timeout=3)
        self.assertEqual(result.stdout, b'RAM-only subtitle data')
        self.assertFalse(runtime._processes)

    def setUp(self):
        self.client = server.app.test_client()
        for item in (patch.object(server, 'authentication_required', return_value=False),
                     patch.object(server, 'get_root', return_value=Path('/media')),
                     patch.object(server, 'safe_path', return_value=Path('/media/movie.mkv')),
                     patch.object(server, 'is_video', return_value=True),
                     patch.object(server, 'probe', return_value={'streams': [{'index': 3, 'codec_type': 'subtitle', 'codec_name': 'subrip'}]}),
                     patch.object(server, 'resolve_media_source', return_value=Path('/media/movie.mkv'))):
            item.start()
            self.addCleanup(item.stop)

    def test_bounded_input_output_windows_and_absolute_timeline(self):
        packets = Mock(returncode=0, stdout=json.dumps({'packets': [{'stream_index': 3, 'pts_time': '560', 'duration_time': '2', 'size': '20'}]}))
        raw = [{'pts_time': '560', 'duration_time': '2', 'data': '\n00000000: 4865 6c6c 6f                             Hello\n'}]
        with patch.object(server, 'run_media_command', side_effect=[packets, Mock(returncode=0, stdout=b'WEBVTT\n\n')]*2) as run, patch.object(server, 'capture_packets', return_value=raw) as capture:
            response = self.client.get('/subtitle/0/movie.mkv?stream=3&start=555&duration=20&absolute=1')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            command = run.call_args.args[0]
            scan = run.call_args_list[0].args[0]
            self.assertNotIn('-select_streams', scan)
            self.assertEqual(scan[scan.index('-read_intervals') + 1], '555.000%575.000')
            self.assertEqual(capture.call_args.kwargs['count'], 1)
            self.assertEqual(command[command.index('-i') + 1], 'pipe:0')
            self.assertIn(b'Hello', run.call_args.kwargs['input'])
            after = command[command.index('-i') + 2:]
            self.assertEqual(after[after.index('-output_ts_offset') + 1], '555.000')
            self.client.get('/subtitle/0/movie.mkv?stream=3&start=555')
            command = run.call_args.args[0]
            self.assertEqual(command[command.index('-output_ts_offset') + 1], '0.000')

    def test_timeout_is_a_clean_504_without_internal_url_or_traceback(self):
        command = ['ffmpeg', '-i', 'http://127.0.0.1:1234/private-token']
        with patch.object(server, 'run_media_command', side_effect=subprocess.TimeoutExpired(command, 30)):
            response = self.client.get('/subtitle/0/movie.mkv?stream=3&start=559.094')
        self.assertEqual(response.status_code, 504)
        self.assertIn(b'Subtitle window timed out', response.data)
        self.assertNotIn(b'private-token', response.data)
        self.assertNotIn(b'Traceback', response.data)

    def test_invalid_duration_rejected_before_starting_ffmpeg(self):
        with patch.object(server, 'run_media_command') as run:
            for query in ('duration=nan', 'duration=inf', 'duration=0', 'duration=31',
                          'duration=invalid', 'absolute=invalid'):
                self.assertEqual(self.client.get('/subtitle/0/movie.mkv?stream=3&' + query).status_code, 400)
            run.assert_not_called()

    def test_encoder_unavailable_and_unsupported_track(self):
        with patch.object(server, 'run_media_command', side_effect=OSError('private details')):
            self.assertEqual(self.client.get('/subtitle/0/movie.mkv?stream=3').status_code, 503)
        packets = Mock(returncode=0, stdout=json.dumps({'packets': [{'stream_index': 3, 'pts_time': '1', 'duration_time': '2'}]}))
        raw = [{'pts_time': '1', 'duration_time': '2', 'data': '\n00000000: 4869                                     Hi\n'}]
        with patch.object(server, 'run_media_command', side_effect=[packets, Mock(returncode=1)]), patch.object(server, 'capture_packets', return_value=raw):
            self.assertEqual(self.client.get('/subtitle/0/movie.mkv?stream=3').status_code, 415)

    def test_silent_window_never_starts_the_encoder(self):
        with patch.object(server, 'run_media_command', return_value=Mock(returncode=0, stdout=b'{"packets": []}')) as run:
            response = self.client.get('/subtitle/0/movie.mkv?stream=3')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b'WEBVTT\n\n')
        self.assertEqual(run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
