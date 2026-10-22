import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import app as server


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class AVSyncTests(unittest.TestCase):
    def run_media(self, command):
        result = subprocess.run(command, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
        return result.stdout

    def packets(self, path):
        return json.loads(self.run_media([
            server.FFPROBE, '-v', 'error', '-show_packets', '-show_data_hash', 'sha256',
            '-of', 'json', str(path)]))['packets']

    def test_fragmented_mp4_preserves_audio_offset_and_seek_preroll(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.mkv'
            output = Path(directory) / 'output.mp4'
            # B-frames, sparse keyframes and a delayed audio track reproduce
            # independent track starts both at startup and between keyframes.
            self.run_media([
                server.FFMPEG, '-v', 'error', '-f', 'lavfi', '-i',
                'testsrc2=size=160x90:rate=24', '-itsoffset', '0.6', '-f', 'lavfi',
                '-i', 'sine=sample_rate=48000', '-t', '5', '-c:v', 'libx264',
                '-g', '48', '-c:a', 'ac3', str(source)])
            original = self.packets(source)
            video_times = {p['data_hash']: float(p['pts_time'])
                           for p in original if p['codec_type'] == 'video'}
            audio_start = min(float(p['pts_time']) for p in original if p['codec_type'] == 'audio')
            aac_frame = 1024 / 48000
            for profile in ('transcode', 'audio_only'):
                for start in (0, 1.3, 2.7):
                    with self.subTest(profile=profile, start=start):
                        command = server.transcode_command(
                            'software', source, 1, start, server.QUALITY_PROFILES[profile])
                        output.write_bytes(self.run_media(command))
                        packets = self.packets(output)
                        audio = [p for p in packets if p['codec_type'] == 'audio']
                        self.assertAlmostEqual(float(audio[0]['pts_time']),
                                               max(0, audio_start - start) - aac_frame, delta=0.01)
                        # The old muxer stretched the first AAC packet up to
                        # the original offset/preroll instead of retaining PTS.
                        for previous, current in zip(audio, audio[1:]):
                            self.assertAlmostEqual(float(current['pts_time']) - float(previous['pts_time']),
                                                   aac_frame, delta=0.002)
                        video = [p for p in packets if p['codec_type'] == 'video']
                        if profile == 'audio_only':
                            for packet in video:
                                self.assertAlmostEqual(float(packet['pts_time']),
                                                       video_times[packet['data_hash']] - start, delta=0.002)
                        else:
                            self.assertAlmostEqual(min(float(p['pts_time']) for p in video),
                                                   0, delta=1 / 24 + 0.002)
                        self.run_media([server.FFMPEG, '-v', 'error', '-i', str(output),
                                        '-f', 'null', '-'])


if __name__ == '__main__':
    unittest.main()
