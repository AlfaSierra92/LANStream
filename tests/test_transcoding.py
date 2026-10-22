import json
import io
from fractions import Fraction
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import app as server
from werkzeug.exceptions import BadRequest


class QualityTests(unittest.TestCase):
    def setUp(self):
        probe = patch.object(server, 'probe', return_value={})
        probe.start()
        self.addCleanup(probe.stop)

    def test_presets_legacy_and_custom_validation(self):
        for query, expected in [('quality=360p', 700), ('low=1', 1500), ('quality=normal', 0),
                                ('quality=custom&height=1080&bitrate=6500&audio_bitrate=192', 6500)]:
            with server.app.test_request_context('/?' + query):
                self.assertEqual(server.requested_quality()['video_kbps'], expected)
        for query in ['quality=unknown', 'quality=custom&height=999', 'quality=custom&bitrate=299',
                      'quality=custom&bitrate=40001', 'quality=custom&audio_bitrate=999', 'quality=custom&bitrate=nan']:
            with server.app.test_request_context('/?' + query), self.assertRaises(BadRequest):
                server.requested_quality()

    def test_rate_control_for_every_backend(self):
        for backend in ['software', 'vaapi', 'qsv', 'nvenc', 'amf']:
            with self.subTest(backend=backend):
                command = server.transcode_command(backend, Path('movie.mkv'), 1, 15, server.QUALITY_PROFILES['720p'])
                for flag, value in [('-b:v', '4000k'), ('-maxrate', '4000k'), ('-bufsize', '8000k'), ('-b:a', '128k')]:
                    self.assertEqual(command[command.index(flag) + 1], value)
                video_filter = command[command.index('-vf') + 1]
                self.assertIn('min(iw,1280)', video_filter)
                self.assertEqual('scale_vaapi=' in video_filter, backend == 'vaapi')
                self.assertNotIn('hwupload', video_filter)
                for conflicting_flag in ['-crf', '-cq', '-qp', '-global_quality', '-qp_i']:
                    self.assertNotIn(conflicting_flag, command)

    def test_player_quality_available_without_audio(self):
        info = {'streams': [{'codec_type': 'video', 'codec_name': 'h264', 'width': 1280, 'height': 720}], 'format': {'duration': '30'}}
        with patch.object(server, 'authentication_required', return_value=False), patch.object(server, 'get_root', return_value=Path('/media')), patch.object(server, 'safe_path', return_value=Path('/media/movie.mp4')), patch.object(server, 'is_video', return_value=True), patch.object(server, 'probe', return_value=info):
            response = server.app.test_client().get('/play/0/movie.mp4?quality=480p')
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'id="qualitySelect"', response.data)
            self.assertIn(b'value="480p" selected', response.data)
            self.assertNotIn(b'id="audioSelect"', response.data)

    def test_invalid_stream_request_does_not_start_ffmpeg(self):
        with patch.object(server, 'authentication_required', return_value=False), patch.object(server, 'get_root', return_value=Path('/media')), patch.object(server, 'safe_path', return_value=Path('/media/movie.mp4')), patch.object(server, 'is_video', return_value=True), patch.object(server, 'selected_transcoder', return_value={'ok': True, 'key': 'software', 'label': 'Software'}), patch.object(server.subprocess, 'Popen') as process:
            for query in ['quality=invalid', 'start=nan', 'start=inf', 'start=-3', 'start=bad', 'audio=bad']:
                self.assertEqual(server.app.test_client().get('/transcode/0/movie.mp4?' + query).status_code, 400)
            process.assert_not_called()

    def test_original_quality_blocks_transcoding_before_encoder_detection(self):
        with patch.object(server, 'authentication_required', return_value=False), patch.object(server, 'get_root', return_value=Path('/media')), patch.object(server, 'safe_path', return_value=Path('/media/movie.mp4')), patch.object(server, 'is_video', return_value=True), patch.object(server, 'selected_transcoder') as detect, patch.object(server.subprocess, 'Popen') as process:
            response = server.app.test_client().get('/transcode/0/movie.mp4?quality=source')
            self.assertEqual(response.status_code, 409)
            detect.assert_not_called()
            process.assert_not_called()
        with self.assertRaises(ValueError):
            server.transcode_command('software', Path('movie.mp4'), 1, 0, server.QUALITY_PROFILES['source'])

    def test_original_quality_label_and_explicit_source_resolution_transcode(self):
        self.assertEqual(server.QUALITY_PROFILES['source']['label'], 'Original · no transcoding')
        command = server.transcode_command('software', Path('movie.mp4'), 1, 0, server.QUALITY_PROFILES['transcode'])
        self.assertEqual(command[command.index('-c:v') + 1], 'libx264')
        self.assertIn('-crf', command)

    def test_stream_completes_and_releases_tracker(self):
        process = Mock(stdout=io.BytesIO(b'test-mp4'), stderr=io.BytesIO(), returncode=0)
        process.poll.return_value = 0
        with patch.object(server, 'authentication_required', return_value=False), patch.object(server, 'get_root', return_value=Path('/media')), patch.object(server, 'safe_path', return_value=Path('/media/movie.mp4')), patch.object(server, 'is_video', return_value=True), patch.object(server, 'selected_transcoder', return_value={'ok': True, 'key': 'software', 'label': 'Software'}), patch.object(server.subprocess, 'Popen', return_value=process):
            response = server.app.test_client().get('/transcode/0/movie.mp4?quality=360p', buffered=True)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b'test-mp4')
            self.assertEqual(server.stream_tracker.snapshot(), [])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_real_software_output_scaling_audio_and_no_upscale(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.mp4'
            subprocess.run([server.FFMPEG, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=960x540:rate=12', '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '1', '-c:v', 'libx264', '-c:a', 'aac', str(source)], check=True, capture_output=True, timeout=20)
            for preset, expected in [('480p', (854, 480)), ('1080p', (960, 540))]:
                command = server.transcode_command('software', source, 1, 0, server.QUALITY_PROFILES[preset])
                result = subprocess.run(command, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                output = Path(directory) / (preset + '.mp4')
                output.write_bytes(result.stdout)
                info = json.loads(subprocess.check_output([server.FFPROBE, '-v', 'error', '-show_streams', '-of', 'json', str(output)], timeout=10))
                video, audio = info['streams']
                self.assertEqual((video['width'], video['height']), expected)
                self.assertEqual(audio['codec_name'], 'aac')
                self.assertEqual(audio['channels'], 2)
                self.assertEqual(video['codec_name'], 'h264')
                sar = Fraction(video.get('sample_aspect_ratio', '1:1').replace(':', '/'))
                self.assertAlmostEqual(float(Fraction(video['width'], video['height']) * sar), 16 / 9, places=2)
                self.assertLessEqual(video['width'], 960)
                self.assertLessEqual(video['height'], 540)
                subprocess.run([server.FFMPEG, '-v', 'error', '-i', str(output), '-f', 'null', '-'], check=True, capture_output=True, timeout=20)


if __name__ == '__main__':
    unittest.main()
