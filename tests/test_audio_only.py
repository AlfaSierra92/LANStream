import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import app as server


class AudioOnlyTests(unittest.TestCase):
    def test_hdr_video_copy_skips_all_video_processing_and_hardware_checks(self):
        info = {'streams': [
            {'index': 0, 'codec_type': 'video', 'codec_name': 'hevc', 'color_transfer': 'smpte2084',
             'side_data_list': [{'dv_profile': 5}]},
            {'index': 1, 'codec_type': 'audio', 'codec_name': 'eac3'},
            {'index': 2, 'codec_type': 'audio', 'codec_name': 'ac3'}]}
        for audio, expected in [('', '0:1'), ('&audio=2', '0:2')]:
            process = Mock(stdout=io.BytesIO(b'mp4'), stderr=io.BytesIO(), returncode=0)
            process.poll.return_value = 0
            with patch.object(server, 'authentication_required', return_value=False), \
                 patch.object(server, 'get_root', return_value=Path('/media')), \
                 patch.object(server, 'safe_path', return_value=Path('/media/hdr.mkv')), \
                 patch.object(server, 'is_video', return_value=True), \
                 patch.object(server, 'probe', return_value=info), \
                 patch.object(server, 'selected_transcoder', side_effect=AssertionError('No video checks')), \
                 patch.object(server, 'tone_mapping_test', side_effect=AssertionError('No HDR conversion')), \
                 patch.object(server, 'codec_encoder_test', return_value=(True, '')) as check, \
                 patch.object(server.media_runtime, 'popen', return_value=process) as popen, \
                 patch.object(server.stream_tracker, 'start', return_value='audio-only') as tracker:
                response = server.app.test_client().get('/transcode/0/hdr.mkv?quality=audio_only&start=30' + audio, buffered=True)
                self.assertEqual(response.status_code, 200)
                check.assert_called_once_with('software', 'aac', True)
                command = popen.call_args.args[0]
                self.assertEqual(command[command.index('-c:v') + 1], 'copy')
                self.assertIn(expected, command)
                self.assertEqual(command[command.index('-c:a') + 1], 'aac')
                self.assertIn('hvc1', command)
                for flag in ('-vf', '-hwaccel', '-init_hw_device', '-b:v', '-color_trc', '-map_metadata'):
                    self.assertNotIn(flag, command)
                technical = tracker.call_args.kwargs['technical']
                self.assertTrue(technical['video_passthrough'])
                self.assertEqual(technical['video_codec'], 'hevc')
                self.assertEqual(technical['encoder'], 'copy')
                self.assertEqual(technical['tone_mapping'], 'Off · video passthrough')

    def test_audio_capabilities_do_not_require_video_encoders(self):
        with patch.object(server, 'authentication_required', return_value=False), \
             patch.object(server, 'selected_transcoder', side_effect=AssertionError('No video checks')), \
             patch.object(server, 'codec_encoder_test', return_value=(True, '')):
            response = server.app.test_client().get('/api/transcode-codecs?audio_only=1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['video'], [])
        self.assertEqual(len(response.json['audio']), len(server.AUDIO_CODECS))

    def test_silent_source_and_seek_are_remuxed_without_audio_encoder(self):
        command = server.transcode_command('copy', Path('silent.mp4'), None, 18,
                                           server.QUALITY_PROFILES['audio_only'])
        self.assertIn('-an', command)
        self.assertNotIn('-c:a', command)
        self.assertEqual(command[command.index('-ss') + 1], '18.000')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_real_audio_conversion_preserves_video_packet_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.mp4'
            subprocess.run([server.FFMPEG, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=12',
                            '-f', 'lavfi', '-i', 'sine=sample_rate=44100', '-t', '0.5',
                            '-c:v', 'libx264', '-c:a', 'ac3', str(source)], check=True, capture_output=True, timeout=15)
            command = server.transcode_command('copy', source, 1, 0, server.QUALITY_PROFILES['audio_only'])
            result = subprocess.run(command, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            output = Path(directory) / 'output.mp4'
            output.write_bytes(result.stdout)
            def inspect(path):
                return json.loads(subprocess.check_output([server.FFPROBE, '-v', 'error', '-show_streams',
                    '-show_packets', '-show_data_hash', 'sha256', '-of', 'json', str(path)], timeout=10))
            before, after = inspect(source), inspect(output)
            self.assertEqual([p['data_hash'] for p in before['packets'] if p['codec_type'] == 'video'],
                             [p['data_hash'] for p in after['packets'] if p['codec_type'] == 'video'])
            audio = next(s for s in after['streams'] if s['codec_type'] == 'audio')
            self.assertEqual((audio['codec_name'], audio['channels'], audio['sample_rate']), ('aac', 2, '48000'))


if __name__ == '__main__':
    unittest.main()
