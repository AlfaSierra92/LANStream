import io
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import Mock, patch

import app as server
from tone_mapping import HDR10_PROBE, HDR10_VIDEO, ToneMappingError, tone_mapping_plan


class ToneMappingTests(unittest.TestCase):
    def setUp(self):
        server.ffmpeg_filters.cache_clear()
        server.tone_mapping_test.cache_clear()
        self.addCleanup(server.ffmpeg_filters.cache_clear)
        self.addCleanup(server.tone_mapping_test.cache_clear)

    def test_sdr_and_bit_depth_alone_do_not_enable_tone_mapping(self):
        for metadata in [{}, {'pix_fmt': 'yuv420p10le'}, {'color_transfer': 'bt709'}]:
            self.assertFalse(tone_mapping_plan(metadata, 'vaapi').active)
            command = server.transcode_command('vaapi', Path('sdr.mkv'), None, 10, source_video=metadata)
            self.assertNotIn('-color_trc', command)
            self.assertNotIn('tonemap_vaapi', command[command.index('-vf') + 1])

    def test_pq_stays_on_gpu_and_removes_hdr_tags_after_color_conversion(self):
        for codec in server.VIDEO_CODECS:
            command = server.transcode_command('vaapi', Path('hdr.mkv'), 1, 559.094,
                                               server.QUALITY_PROFILES['720p'], codec, source_video=HDR10_VIDEO)
            graph = command[command.index('-vf') + 1]
            self.assertLess(graph.index('tonemap_vaapi='), graph.index('scale_vaapi='))
            self.assertLess(graph.index('scale_vaapi='), graph.index('sidedata=mode=delete'))
            for unwanted in ('hwupload', 'hwdownload', 'zscale', ',format='):
                self.assertNotIn(unwanted, graph)
            for option in ('-color_trc', '-colorspace', '-color_primaries'):
                self.assertEqual(command[command.index(option) + 1], 'bt709')
            self.assertEqual(command[command.index('-map_metadata') + 1], '-1')
            self.assertLess(command.index('-hwaccel'), command.index('-i'))

    def test_hlg_and_dolby_vision_profiles_are_not_misidentified(self):
        hlg = {**HDR10_VIDEO, 'color_transfer': 'arib-std-b67'}
        self.assertIn('HLG', tone_mapping_plan(hlg, 'software').label)
        with self.assertRaises(ToneMappingError):
            tone_mapping_plan(hlg, 'vaapi')
        for profile, compatible in [(7, 6), (8, 1)]:
            video = {**HDR10_VIDEO, 'side_data_list': [{'dv_profile': profile, 'dv_bl_signal_compatibility_id': compatible}]}
            self.assertIn('base layer', tone_mapping_plan(video, 'vaapi').label)
        for profile, compatible in [(5, 0), (8, 0)]:
            with self.assertRaises(ToneMappingError):
                tone_mapping_plan({**HDR10_VIDEO, 'side_data_list': [{'dv_profile': profile, 'dv_bl_signal_compatibility_id': compatible}]}, 'vaapi')

    def test_cpu_linearizes_and_converts_gamut_before_encoding(self):
        for backend in ['software', 'qsv', 'nvenc', 'amf']:
            command = server.transcode_command(backend, Path('hdr.mkv'), None, 0, source_video=HDR10_VIDEO)
            graph = command[command.index('-vf') + 1]
            self.assertLess(graph.index('t=linear'), graph.index('tonemap=tonemap=hable'))
            self.assertLess(graph.index('tonemap=tonemap=hable'), graph.index('zscale=t=bt709'))
            self.assertTrue(graph.endswith('sidedata=mode=delete'))

    def test_missing_filters_fail_preflight_without_opening_a_source(self):
        with patch.object(server, 'ffmpeg_filters', return_value=frozenset()), patch.object(server, 'run_media_command') as run:
            supported, detail = server.tone_mapping_test('vaapi')
        self.assertFalse(supported)
        self.assertIn('tonemap_vaapi', detail)
        run.assert_not_called()

    def test_capability_test_uses_compressed_fixture_in_ram_and_is_cached(self):
        with patch.object(server, 'ffmpeg_filters', return_value=tone_mapping_plan(HDR10_VIDEO, 'vaapi').required), patch.object(server, 'run_media_command', return_value=Mock(returncode=0, stdout=b'mp4', stderr=b'')) as run:
            self.assertTrue(server.tone_mapping_test('vaapi')[0])
            self.assertTrue(server.tone_mapping_test('vaapi')[0])
            run.assert_called_once()
            command = run.call_args.args[0]
            self.assertEqual(command[command.index('-i') + 1], 'pipe:0')
            self.assertEqual(run.call_args.kwargs['input'], HDR10_PROBE)
            self.assertIn('-hwaccel_output_format', command)
            self.assertIn('tonemap_vaapi', command[command.index('-vf') + 1])

    def test_filter_inventory_parses_real_listing(self):
        for flags in ('..', '...', '..C', '....'):
            for channel in ('stdout', 'stderr'):
                with self.subTest(flags=flags, channel=channel):
                    server.ffmpeg_filters.cache_clear()
                    listing = (f' {flags} tonemap_vaapi V->V Tone map\n'
                               f' {flags} scale_vaapi V->V Scale\n'
                               f' {flags} setparams V->V Tags\n'
                               f' {flags} sidedata V->V Metadata\n'
                               ' T. = Timeline support\n ... fake Not-a-signature\n')
                    result = Mock(returncode=0, stdout='', stderr='')
                    setattr(result, channel, listing)
                    with patch.object(server, 'run_media_command', return_value=result):
                        self.assertEqual(server.ffmpeg_filters(), {'tonemap_vaapi', 'scale_vaapi', 'setparams', 'sidedata'})

    def test_two_column_flags_reach_real_capability_probe(self):
        listing = '\n'.join(f' .. {name} V->V Filter' for name in tone_mapping_plan(HDR10_VIDEO, 'vaapi').required)
        with patch.object(server, 'run_media_command', side_effect=[
            Mock(returncode=0, stdout=listing, stderr=''),
            Mock(returncode=0, stdout=b'mp4', stderr=b'')]) as run:
            self.assertTrue(server.tone_mapping_test('vaapi')[0])
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args.kwargs['input'], HDR10_PROBE)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required')
    def test_installed_ffmpeg_inventory_finds_standard_metadata_filters(self):
        self.assertTrue({'setparams', 'sidedata'} <= server.ffmpeg_filters())

    def test_endpoint_reports_mapping_and_rejects_unavailable_pipeline(self):
        info = {'streams': [{'codec_type': 'video', **HDR10_VIDEO}]}
        process = Mock(stdout=io.BytesIO(b'mp4'), stderr=io.BytesIO(), returncode=0)
        process.poll.return_value = 0
        patches = [patch.object(server, 'authentication_required', return_value=False),
                   patch.object(server, 'get_root', return_value=Path('/media')),
                   patch.object(server, 'safe_path', return_value=Path('/media/hdr.mkv')),
                   patch.object(server, 'is_video', return_value=True),
                   patch.object(server, 'probe', return_value=info),
                   patch.object(server, 'selected_transcoder', return_value={'ok': True, 'key': 'vaapi'})]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        client = server.app.test_client()
        with client.session_transaction() as session:
            session['playback_owner'] = 'hdr-test-owner'
        with patch.object(server, 'tone_mapping_test', return_value=(False, 'driver lacks HDR support')), patch.object(server.media_runtime, 'popen') as popen:
            with self.assertLogs(server.app.logger, level='ERROR') as logs:
                response = client.get('/transcode/0/hdr.mkv?playback_id=hdr-failed')
            self.assertEqual(response.status_code, 503)
            self.assertIn(b'driver lacks HDR support', response.data)
            self.assertIn('driver lacks HDR support', logs.output[0])
            metrics = client.get('/api/playback-info/0/hdr.mkv?playback_id=hdr-failed').json['stream']
            self.assertIn('driver lacks HDR support', metrics['technical']['error'])
            self.assertFalse(metrics['active'])
            self.assertIsNone(server.app.test_client().get('/api/playback-info/0/hdr.mkv?playback_id=hdr-failed').json['stream'])
            popen.assert_not_called()
        with patch.object(server, 'tone_mapping_test', return_value=(True, '')), patch.object(server.media_runtime, 'popen', return_value=process), patch.object(server.stream_tracker, 'start', return_value='id') as start:
            response = server.app.test_client().get('/transcode/0/hdr.mkv', buffered=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn('VAAPI', start.call_args.kwargs['technical']['tone_mapping'])

    def test_ffmpeg_failure_is_retained_for_player_and_redacts_gateway_token(self):
        process = Mock(stdout=io.BytesIO(), stderr=io.BytesIO(b'Failed to configure tonemap_vaapi http://127.0.0.1:9999/SECRET/movie'), returncode=1)
        process.poll.return_value = 1
        patches = [patch.object(server, 'authentication_required', return_value=False),
                   patch.object(server, 'get_root', return_value=Path('/media')),
                   patch.object(server, 'safe_path', return_value=Path('/media/hdr.mkv')),
                   patch.object(server, 'is_video', return_value=True),
                   patch.object(server, 'probe', return_value={'streams': [{'codec_type': 'video', **HDR10_VIDEO}]}),
                   patch.object(server, 'selected_transcoder', return_value={'ok': True, 'key': 'vaapi'}),
                   patch.object(server, 'tone_mapping_test', return_value=(True, '')),
                   patch.object(server.media_runtime, 'popen', return_value=process)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        client = server.app.test_client()
        with client.session_transaction() as session:
            session['playback_owner'] = 'hdr-runtime-owner'
        with self.assertLogs(server.app.logger, level='ERROR') as logs:
            client.get('/transcode/0/hdr.mkv?playback_id=runtime-failed', buffered=True)
        metrics = client.get('/api/playback-info/0/hdr.mkv?playback_id=runtime-failed').json['stream']
        self.assertIn('Failed to configure tonemap_vaapi', metrics['technical']['error'])
        self.assertNotIn('SECRET', metrics['technical']['error'])
        self.assertNotIn('SECRET', '\n'.join(logs.output))

    @unittest.skipUnless(shutil.which('ffprobe'), 'FFprobe required')
    def test_bundled_probe_is_real_hdr10_with_mastering_metadata(self):
        info = json.loads(subprocess.check_output([server.FFPROBE, '-v', 'error', '-show_streams', '-show_frames', '-of', 'json', '-f', 'hevc', '-i', 'pipe:0'], input=HDR10_PROBE, timeout=10))
        video = info['streams'][0]
        self.assertEqual(video['color_transfer'], 'smpte2084')
        self.assertEqual(video['color_primaries'], 'bt2020')
        self.assertIn('Mastering display metadata', [data['side_data_type'] for frame in info['frames'] for data in frame.get('side_data_list', [])])

    @unittest.skipUnless(Path(server.VAAPI_DEVICE).exists(), 'Linux VAAPI device required')
    def test_real_vaapi_tone_mapping_outputs_sdr_without_hdr_side_data(self):
        supported, detail = server.tone_mapping_test('vaapi')
        self.assertTrue(supported, detail)
        self.check_real_output('vaapi')

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required')
    def test_real_cpu_tone_mapping_outputs_sdr_without_hdr_side_data(self):
        if not tone_mapping_plan(HDR10_VIDEO, 'software').required <= server.ffmpeg_filters():
            self.skipTest('FFmpeg requires zscale/libzimg and tonemap')
        encoders = subprocess.check_output([server.FFMPEG, '-hide_banner', '-encoders'], stderr=subprocess.DEVNULL).decode()
        for codec, encoder in server.VIDEO_ENCODERS['software'].items():
            if encoder in encoders:
                with self.subTest(codec=codec):
                    self.check_real_output('software', codec)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required')
    def test_real_hlg_cpu_output(self):
        hlg = {**HDR10_VIDEO, 'color_transfer': 'arib-std-b67'}
        if not tone_mapping_plan(hlg, 'software').required <= server.ffmpeg_filters():
            self.skipTest('FFmpeg requires zscale/libzimg and tonemap')
        fixture = subprocess.check_output([server.FFMPEG, '-v', 'error', '-f', 'hevc', '-i', 'pipe:0',
                                           '-c:v', 'copy', '-bsf:v', 'hevc_metadata=transfer_characteristics=18',
                                           '-f', 'hevc', 'pipe:1'], input=HDR10_PROBE, timeout=10)
        self.check_real_output('software', source_video=hlg, fixture=fixture)

    def check_real_output(self, backend, video_codec='h264', source_video=None, fixture=HDR10_PROBE):
        command = server.transcode_command(backend, Path('fixture'), None, 0, video_codec=video_codec,
                                           source_video=source_video or HDR10_VIDEO)
        before, after = command.index('-skip_estimate_duration_from_pts'), command.index('-map')
        command[before:after] = ['-f', 'hevc', '-i', 'pipe:0', '-frames:v', '1']
        result = subprocess.run(command, input=fixture, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        info = json.loads(subprocess.check_output([server.FFPROBE, '-v', 'error', '-show_streams', '-show_frames', '-of', 'json', '-i', 'pipe:0'], input=result.stdout, timeout=10))
        video = info['streams'][0]
        self.assertEqual(video['codec_name'], video_codec)
        for field in ['color_transfer', 'color_space', 'color_primaries']:
            self.assertEqual(video[field], 'bt709')
        self.assertEqual(video['pix_fmt'], 'yuv420p')
        for data in video.get('side_data_list', []) + [side for frame in info.get('frames', []) for side in frame.get('side_data_list', [])]:
            self.assertFalse(any(word in data['side_data_type'].lower() for word in ['mastering', 'content light', 'dovi', 'dolby']))


if __name__ == '__main__':
    unittest.main()
