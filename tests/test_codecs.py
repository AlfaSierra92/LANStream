import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from werkzeug.exceptions import BadRequest

import app as server


class CodecTests(unittest.TestCase):
    def test_codec_names_are_validated(self):
        with server.app.test_request_context('/?video_codec=hevc&audio_codec=opus'):
            self.assertEqual(server.requested_codecs(), ('hevc', 'opus'))
        with server.app.test_request_context('/'):
            self.assertEqual(server.requested_codecs(), ('h264', 'aac'))
        for query in ['video_codec=libx264', 'audio_codec=copy', 'video_codec=bad', 'audio_codec=bad']:
            with server.app.test_request_context('/?' + query), self.assertRaises(BadRequest):
                server.requested_codecs()

    def test_codec_falls_back_to_software(self):
        preferred = {'ok': True, 'key': 'nvenc'}
        with patch.object(server, 'codec_encoder_test', side_effect=[(False, 'No AV1 hardware'), (True, '')]) as probe:
            backend, note = server.codec_transcoder(preferred, 'av1')
            self.assertEqual(backend, 'software')
            self.assertIn('software', note)
            self.assertEqual(probe.call_count, 2)
        with patch.object(server, 'codec_encoder_test', return_value=(False, 'Missing encoder')):
            self.assertIsNone(server.codec_transcoder(preferred, 'hevc')[0])

    def test_capabilities_and_audio_availability(self):
        with patch.object(server, 'authentication_required', return_value=False), patch.object(server, 'selected_transcoder', return_value={'ok': True, 'key': 'software'}), patch.object(server, 'codec_encoder_test', side_effect=lambda backend, codec, *args: (codec != 'opus', 'missing' if codec == 'opus' else '')):
            response = server.app.test_client().get('/api/transcode-codecs')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertEqual([item['id'] for item in response.json['video']], ['h264', 'hevc', 'av1'])
            opus = next(item for item in response.json['audio'] if item['id'] == 'opus')
            self.assertFalse(opus['available'])
            self.assertEqual(response.json['video'][1]['encoder'], 'libx265')

    def test_unavailable_codec_never_starts_stream(self):
        with patch.object(server, 'authentication_required', return_value=False), patch.object(server, 'get_root', return_value=Path('/media')), patch.object(server, 'safe_path', return_value=Path('/media/movie.mp4')), patch.object(server, 'is_video', return_value=True), patch.object(server, 'selected_transcoder', return_value={'ok': True, 'key': 'software'}), patch.object(server, 'codec_encoder_test', return_value=(False, 'encoder missing')), patch.object(server.subprocess, 'Popen') as process:
            response = server.app.test_client().get('/transcode/0/movie.mp4?video_codec=hevc')
            self.assertEqual(response.status_code, 503)
            self.assertIn(b'No encoder available', response.data)
            response = server.app.test_client().get('/transcode/0/movie.mp4?video_codec=bad')
            self.assertEqual(response.status_code, 400)
            process.assert_not_called()

    def test_stream_reports_actual_codec_and_encoder(self):
        info = {'streams': [{'codec_type': 'audio', 'index': 1}]}
        process = Mock(stdout=io.BytesIO(b'mp4'), stderr=io.BytesIO(), returncode=0)
        process.poll.return_value = 0
        with patch.object(server, 'authentication_required', return_value=False), patch.object(server, 'get_root', return_value=Path('/media')), patch.object(server, 'safe_path', return_value=Path('/media/movie.mp4')), patch.object(server, 'is_video', return_value=True), patch.object(server, 'probe', return_value=info), patch.object(server, 'selected_transcoder', return_value={'ok': True, 'key': 'software', 'label': 'Software'}), patch.object(server, 'codec_encoder_test', return_value=(True, '')), patch.object(server.subprocess, 'Popen', return_value=process) as popen, patch.object(server.stream_tracker, 'start', return_value='stream-id') as start:
            response = server.app.test_client().get('/transcode/0/movie.mp4?video_codec=hevc&audio_codec=opus&audio=1', buffered=True)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content_type, 'video/mp4')
            technical = start.call_args.kwargs['technical']
            self.assertEqual(technical['video_codec'], 'hevc')
            self.assertEqual(technical['audio_codec'], 'opus')
            self.assertEqual(technical['encoder'], 'libx265')
            command = popen.call_args.args[0]
            self.assertEqual(command[command.index('-c:a') + 1], 'libopus')
            self.assertEqual(command[command.index('-tag:v') + 1], 'hvc1')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_real_video_audio_combinations(self):
        encoder_list = subprocess.check_output([server.FFMPEG, '-hide_banner', '-encoders'], stderr=subprocess.DEVNULL).decode()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.mp4'
            subprocess.run([server.FFMPEG, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=12', '-f', 'lavfi', '-i', 'sine=sample_rate=48000', '-t', '0.5', '-c:v', 'libx264', '-c:a', 'aac', str(source)], check=True, capture_output=True, timeout=15)
            for video, encoder in server.VIDEO_ENCODERS['software'].items():
                if encoder not in encoder_list:
                    continue
                for audio, metadata in server.AUDIO_CODECS.items():
                    if metadata['encoder'] not in encoder_list:
                        continue
                    with self.subTest(video=video, audio=audio):
                        command = server.transcode_command('software', source, 1, 0, server.QUALITY_PROFILES['480p'], video, audio)
                        result = subprocess.run(command, capture_output=True, timeout=20)
                        self.assertEqual(result.returncode, 0, result.stderr.decode())
                        output = Path(directory) / f'{video}-{audio}.mp4'
                        output.write_bytes(result.stdout)
                        info = json.loads(subprocess.check_output([server.FFPROBE, '-v', 'error', '-show_streams', '-of', 'json', str(output)], timeout=10))
                        self.assertEqual(info['streams'][0]['codec_name'], video)
                        self.assertEqual(info['streams'][1]['codec_name'], audio)
                        self.assertEqual(info['streams'][1]['sample_rate'], '48000')
                        subprocess.run([server.FFMPEG, '-v', 'error', '-i', str(output), '-f', 'null', '-'], check=True, capture_output=True, timeout=15)


if __name__ == '__main__':
    unittest.main()
