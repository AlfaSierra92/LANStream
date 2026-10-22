import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app as server


class VAAPITests(unittest.TestCase):
    def test_gpu_frames_are_kept_on_device_for_all_output_codecs_and_qualities(self):
        for codec in server.VIDEO_CODECS:
            for name, quality in server.QUALITY_PROFILES.items():
                if name in {'source', 'audio_only'}:
                    continue
                for start in (0, 559.094):
                    with self.subTest(codec=codec, quality=name, start=start):
                        command = server.transcode_command('vaapi', Path('main10.mkv'), 1, start, quality, codec)
                        for flag, value in [('-hwaccel', 'vaapi'), ('-hwaccel_device', 'lanstream'),
                                            ('-hwaccel_output_format', 'vaapi')]:
                            self.assertEqual(command[command.index(flag) + 1], value)
                            self.assertLess(command.index(flag), command.index('-i'))
                        self.assertEqual(command[command.index('-init_hw_device') + 1],
                                         f'vaapi=lanstream:{server.VAAPI_DEVICE}')
                        self.assertEqual(command[command.index('-filter_hw_device') + 1], 'lanstream')
                        graph = command[command.index('-vf') + 1]
                        self.assertTrue(graph.startswith('scale_vaapi='))
                        self.assertTrue(graph.endswith('format=nv12'))
                        for cpu_step in ('hwupload', 'hwdownload', ',format=', ',scale='):
                            self.assertNotIn(cpu_step, graph)
                        self.assertEqual(command[command.index('-c:v') + 1], f'{codec}_vaapi')
                        self.assertEqual(command[command.index('-c:a') + 1], 'aac')

    def test_synthetic_capability_probe_uploads_raw_frames_without_hardware_decoder(self):
        server.codec_encoder_test.cache_clear()
        self.addCleanup(server.codec_encoder_test.cache_clear)
        with patch.object(server, 'run_media_command', return_value=Mock(returncode=0)) as run:
            self.assertTrue(server.codec_encoder_test('vaapi', 'hevc')[0])
            command = run.call_args.args[0]
        self.assertNotIn('-hwaccel', command)
        self.assertNotIn('-hwaccel_output_format', command)
        self.assertIn('-init_hw_device', command)
        self.assertIn('color=size=320x180:rate=24', command)
        self.assertNotIn('codec-test', command)
        self.assertTrue(command[command.index('-vf') + 1].startswith('format=nv12,hwupload,scale_vaapi='))

    def test_unavailable_vaapi_output_codec_does_not_switch_to_cpu(self):
        with patch.object(server, 'codec_encoder_test', return_value=(False, 'unsupported codec')) as probe:
            backend, detail = server.codec_transcoder({'ok': True, 'key': 'vaapi'}, 'av1')
        self.assertIsNone(backend)
        self.assertIn('software fallback is disabled', detail)
        probe.assert_called_once_with('vaapi', 'av1')

    @unittest.skipUnless(Path(server.VAAPI_DEVICE).exists() and shutil.which('ffmpeg') and shutil.which('ffprobe'),
                         'A Linux VAAPI render device and FFmpeg are required')
    def test_real_h264_and_main10_decode_convert_scale_and_encode_on_gpu(self):
        encoders = subprocess.check_output([server.FFMPEG, '-hide_banner', '-encoders'], stderr=subprocess.DEVNULL).decode()
        with tempfile.TemporaryDirectory() as directory:
            for encoder, pixel_format in [('libx264', 'yuv420p'), ('libx265', 'yuv420p10le')]:
                if encoder not in encoders:
                    self.skipTest(f'{encoder} is required to generate the compressed test fixture')
                source = Path(directory) / (encoder + '.mkv')
                generate = [server.FFMPEG, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=960x540:rate=12',
                            '-t', '1', '-c:v', encoder, '-pix_fmt', pixel_format]
                if encoder == 'libx265':
                    generate += ['-x265-params', 'pools=2:log-level=error', '-profile:v', 'main10']
                subprocess.run(generate + [str(source)], capture_output=True, check=True, timeout=30)
                for preset, expected in [('auto', (960, 540)), ('480p', (854, 480)), ('1080p', (960, 540))]:
                    with self.subTest(source=encoder, quality=preset):
                        command = server.transcode_command('vaapi', source, None, 0, server.QUALITY_PROFILES[preset])
                        result = subprocess.run(command, capture_output=True, timeout=30)
                        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
                        info = json.loads(subprocess.check_output(
                            [server.FFPROBE, '-v', 'error', '-show_streams', '-of', 'json', '-i', 'pipe:0'],
                            input=result.stdout, timeout=10))
                        video = info['streams'][0]
                        self.assertEqual(video['codec_name'], 'h264')
                        self.assertEqual(video['pix_fmt'], 'yuv420p')
                        self.assertEqual((video['width'], video['height']), expected)


if __name__ == '__main__':
    unittest.main()
