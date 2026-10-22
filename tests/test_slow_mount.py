import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import app as server


class SlowMountTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'posix', 'POSIX shell required')
    def test_mount_script_uses_ram_only_without_starting_rclone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_rclone = root / 'rclone'
            fake_rclone.write_text(f'#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n')
            fake_rclone.chmod(0o700)
            env = {**os.environ, 'RCLONE_BIN': str(fake_rclone),
                   'RCLONE_RC_PASS': 'test-only-placeholder', 'RCLONE_RC_USER': 'test-user',
                   'RCLONE_RC_ADDR': '127.0.0.1:5572', 'RCLONE_CONFIG_FILE': str(root / 'rclone.conf'),
                   'NZBDAV_MOUNT': str(root / 'mount'), 'NZBDAV_BUFFER_SIZE': '128M'}
            script = Path(__file__).resolve().parents[1] / 'tools' / 'mount_nzbdav.sh'
            result = subprocess.run(['sh', str(script)], env=env, capture_output=True, text=True,
                                    check=True, timeout=5)
            args = json.loads(result.stdout)
            self.assertEqual(args[:3], ['mount', 'nzbdav:', str(root / 'mount')])
            for flag in ('--vfs-cache-mode=off', '--buffer-size=128M',
                         '--use-cookies', '--read-only', '--vfs-read-chunk-streams=0'):
                self.assertIn(flag, args)
            for prefix in ('--cache-dir', '--vfs-cache-max', '--vfs-cache-min', '--vfs-read-ahead'):
                self.assertFalse(any(arg.startswith(prefix) for arg in args))
            self.assertEqual(set(root.iterdir()), {fake_rclone})
            env['NZBDAV_BUFFER_SIZE'] = '64M'
            result = subprocess.run(['sh', str(script)], env=env, capture_output=True, text=True,
                                    check=True, timeout=5)
            self.assertIn('--buffer-size=64M', json.loads(result.stdout))
            del env['RCLONE_RC_PASS']
            result = subprocess.run(['sh', str(script)], env=env, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '')

    def test_startup_preserves_probe_packets_and_seeks_stay_before_input(self):
        for backend in server.VIDEO_ENCODERS:
            for start in (0, 42.5):
                with self.subTest(backend=backend, start=start):
                    command = server.transcode_command(backend, Path('/mnt/nzbdav/movie.mkv'), 2, start)
                    self.assertLess(command.index('-skip_estimate_duration_from_pts'), command.index('-i'))
                    if start:
                        self.assertEqual(command[command.index('-ss') + 1], '42.500')
                        self.assertLess(command.index('-ss'), command.index('-i'))
                    else:
                        self.assertNotIn('-ss', command)
                    # Keep full stream discovery and accurate keyframe seeking.
                    for flag in ('-analyzeduration', '-probesize', '-noaccurate_seek', '-seek2any'):
                        self.assertNotIn(flag, command)

    def test_ffprobe_still_estimates_duration_and_encoder_self_test_uses_lavfi(self):
        with patch.object(server, 'run_media_command', return_value=Mock(returncode=0, stdout='{}')) as run:
            server.probe_media_target(Path('movie.ts'), Path('movie.ts'))
            self.assertNotIn('-skip_estimate_duration_from_pts', run.call_args.args[0])
        server.codec_encoder_test.cache_clear()
        self.addCleanup(server.codec_encoder_test.cache_clear)
        with patch.object(server, 'run_media_command', return_value=Mock(returncode=0)) as run:
            self.assertTrue(server.codec_encoder_test('software', 'hevc')[0])
            command = run.call_args.args[0]
            self.assertIn('color=size=320x180:rate=24', command)
            self.assertNotIn('-skip_estimate_duration_from_pts', command)
            self.assertNotIn('codec-test', command)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_real_mp4_mkv_and_transport_stream_start_and_seek(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / 'source.mkv'
            subprocess.run([
                server.FFMPEG, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=12',
                '-f', 'lavfi', '-i', 'sine=frequency=440',
                '-f', 'lavfi', '-i', 'sine=frequency=880',
                '-map', '0:v', '-map', '1:a', '-map', '2:a', '-t', '3',
                '-c:v', 'libx264', '-g', '12', '-c:a', 'aac', str(original),
            ], capture_output=True, check=True, timeout=20)
            for suffix in ('mp4', 'ts'):
                subprocess.run([server.FFMPEG, '-v', 'error', '-i', str(original),
                                '-map', '0', '-c', 'copy', str(root / f'source.{suffix}')],
                               capture_output=True, check=True, timeout=20)
            for suffix in ('mkv', 'mp4', 'ts'):
                for start in (0, 1):
                    with self.subTest(container=suffix, start=start):
                        command = server.transcode_command('software', root / f'source.{suffix}', 2,
                                                           start, server.QUALITY_PROFILES['360p'])
                        result = subprocess.run(command, capture_output=True, timeout=20)
                        self.assertEqual(result.returncode, 0, result.stderr.decode())
                        output = root / 'output.mp4'
                        output.write_bytes(result.stdout)
                        info = json.loads(subprocess.check_output([
                            server.FFPROBE, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(output),
                        ], timeout=10))
                        self.assertEqual([stream['codec_type'] for stream in info['streams']], ['video', 'audio'])
                        self.assertAlmostEqual(float(info['format']['duration']), 3 - start, delta=0.3)
                        subprocess.run([server.FFMPEG, '-v', 'error', '-i', str(output), '-f', 'null', '-'],
                                       capture_output=True, check=True, timeout=20)


if __name__ == '__main__':
    unittest.main()
