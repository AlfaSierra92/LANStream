import io
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import Mock, patch

import app as server
from tone_mapping import HDR10_PROBE, HDR10_VIDEO, SDR_FRAME_PROPERTIES, ToneMappingError, tone_mapping_plan


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
            self.assertLess(graph.index('tonemap_vaapi='), graph.index(SDR_FRAME_PROPERTIES))
            self.assertLess(graph.index(SDR_FRAME_PROPERTIES), graph.index('scale_vaapi='))
            self.assertIn('-noauto_conversion_filters', command)
            self.assertLess(graph.index('scale_vaapi='), graph.index('sidedata=mode=delete'))
            for unwanted in ('hwupload', 'hwdownload', 'zscale', ',format='):
                self.assertNotIn(unwanted, graph)
            for option in ('-color_trc', '-colorspace', '-color_primaries'):
                self.assertEqual(command[command.index(option) + 1], 'bt709')
            self.assertEqual(command[command.index('-map_metadata') + 1], '-1')
            self.assertLess(command.index('-hwaccel'), command.index('-i'))

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg tools required')
    def test_hdr_vaapi_explicit_audio_downmix_with_automatic_conversion_disabled(self):
        encoders = subprocess.check_output([server.FFMPEG, '-hide_banner', '-encoders'], stderr=subprocess.DEVNULL).decode()
        # The explicit height-channel layout also works in versions predating
        # the "5.1.2" alias. It reproduces the layout in the reported error.
        layouts = ['mono', '5.1', '7.1', 'FL+FR+FC+LFE+SL+SR+TFL+TFR']
        for codec, metadata in server.AUDIO_CODECS.items():
            if metadata['encoder'] not in encoders:
                continue
            command = server.transcode_command('vaapi', Path('hdr.mkv'), 1, 0, audio_codec=codec, source_video=HDR10_VIDEO)
            self.assertIn('-noauto_conversion_filters', command)
            audio_filter = command[command.index('-af') + 1]
            for layout in layouts:
                with self.subTest(codec=codec, layout=layout):
                    base = [server.FFMPEG, '-v', 'error', '-noauto_conversion_filters', '-f', 'lavfi',
                            '-i', f'anullsrc=r=44100:cl={layout}', '-t', '0.1']
                    options = ['-c:a', metadata['encoder'], '-b:a', '128k', '-ac', '2', '-ar', '48000',
                               '-movflags', '+frag_keyframe+empty_moov', '-f', 'mp4', 'pipe:1']
                    # Exercise the actual filter/encoder options generated for
                    # VAAPI without requiring a GPU for this audio regression.
                    fixed = subprocess.run(base + ['-af', audio_filter] + options,
                                           capture_output=True, timeout=10)
                    self.assertEqual(fixed.returncode, 0, fixed.stderr.decode('utf-8', 'replace'))
                    info = json.loads(subprocess.check_output(
                        [server.FFPROBE, '-v', 'error', '-show_streams', '-of', 'json', '-i', 'pipe:0'],
                        input=fixed.stdout, timeout=10))
                    audio = info['streams'][0]
                    self.assertEqual(audio['channels'], 2)
                    self.assertEqual(audio['sample_rate'], '48000')
                    self.assertEqual(audio['codec_name'], codec)

    def test_silent_hdr_does_not_add_audio_filter(self):
        command = server.transcode_command('vaapi', Path('hdr.mkv'), None, 0, source_video=HDR10_VIDEO)
        self.assertIn('-an', command)
        self.assertNotIn('-af', command)

    def test_libplacebo_modes_keep_decode_and_encode_on_same_gpu(self):
        for mode, algorithm in [('libplacebo-bt2390', 'bt.2390'),
                                ('libplacebo-mobius', 'mobius'),
                                ('libplacebo-reinhard', 'reinhard')]:
            command = server.transcode_command('vaapi', Path('hdr.mkv'), 1, 0,
                                               source_video=HDR10_VIDEO, tone_mapper=mode)
            graph = command[command.index('-vf') + 1]
            self.assertIn('vulkan=lanstream_vk@lanstream', command)
            self.assertEqual(command[command.index('-hwaccel_device') + 1], 'lanstream')
            self.assertIn('tonemapping=' + algorithm, graph)
            self.assertLess(graph.index('derive_device=vulkan'), graph.index('libplacebo='))
            self.assertLess(graph.index('libplacebo='), graph.index('derive_device=vaapi'))
            self.assertLess(graph.index('derive_device=vaapi'), graph.index('scale_vaapi='))
            self.assertIn('apply_dolbyvision=0', graph)
            for unwanted in ('hwdownload', 'hwupload', 'zscale', 'tonemap_vaapi='):
                self.assertNotIn(unwanted, graph)
            self.assertIn('-af', command)
        with self.assertRaises(ToneMappingError):
            tone_mapping_plan(HDR10_VIDEO, 'vaapi', 'arbitrary:filter=1')

    def test_libplacebo_preflight_is_separate_from_vaapi_and_requires_interop(self):
        filters = tone_mapping_plan(HDR10_VIDEO, 'vaapi').required | tone_mapping_plan(HDR10_VIDEO, 'vaapi', 'libplacebo-bt2390').required
        with patch.object(server, 'ffmpeg_filters', return_value=filters), patch.object(
                server, 'run_media_command', side_effect=[Mock(returncode=0, stdout=b'mp4', stderr=b''),
                                                        Mock(returncode=1, stdout=b'', stderr=b'Vulkan hardware mapping failed')]) as run:
            self.assertTrue(server.tone_mapping_test('vaapi')[0])
            supported, detail = server.tone_mapping_test('vaapi', 'libplacebo-bt2390')
            self.assertFalse(supported)
            self.assertIn('mapping failed', detail)
            self.assertEqual(run.call_args.kwargs['input'], HDR10_PROBE)
            self.assertIn('libplacebo=', run.call_args.args[0][run.call_args.args[0].index('-vf') + 1])
            self.assertEqual(run.call_args.args[0][run.call_args.args[0].index('-loglevel') + 1], 'verbose')
            self.assertTrue(server.tone_mapping_test('vaapi')[0])
            self.assertEqual(run.call_count, 2)

    def test_vulkan_diagnostics_preserve_library_context_and_identify_queue_guard(self):
        extension = '[AVHWDeviceContext] Using device extension VK_KHR_internally_synchronized_queues'
        library = '[libplacebo] Initialized libplacebo v7.360.0 (API v360)'
        failure = '[AVFilterGraph] Error initializing filters\nError opening output files: Invalid argument'
        log = '\n'.join([extension] + ['Unrelated extension'] * 200 + [library] + ['Other startup info'] * 200 + [failure])
        filters = tone_mapping_plan(HDR10_VIDEO, 'vaapi', 'libplacebo-bt2390').required
        with patch.object(server, 'ffmpeg_filters', return_value=filters), patch.object(
                server, 'run_media_command', return_value=Mock(returncode=1, stdout=b'', stderr=log.encode())):
            supported, detail = server.tone_mapping_test('vaapi', 'libplacebo-bt2390')
        self.assertFalse(supported)
        self.assertIn('API 365 or newer', detail)
        self.assertIn(library, detail)
        self.assertIn('Error initializing filters', detail)
        self.assertLessEqual(len(detail), 2000)
        for alternative in (log.replace('API v360', 'API v365'), log.replace(extension, 'Supported extension only')):
            self.assertNotIn('API 365 or newer', server.vulkan_playback_error(alternative))

    def test_successful_vulkan_probe_does_not_return_verbose_startup_output(self):
        filters = tone_mapping_plan(HDR10_VIDEO, 'vaapi', 'libplacebo-bt2390').required
        with patch.object(server, 'ffmpeg_filters', return_value=filters), patch.object(
                server, 'run_media_command', return_value=Mock(returncode=0, stdout=b'mp4', stderr=b'Verbose startup details')):
            self.assertEqual(server.tone_mapping_test('vaapi', 'libplacebo-bt2390'), (True, ''))

    def test_configured_tone_mapper_is_used_without_changing_sdr(self):
        with patch.object(server, 'load_settings', return_value={'tone_mapping': 'libplacebo-mobius'}):
            hdr = server.transcode_command('vaapi', Path('hdr.mkv'), None, 0, source_video=HDR10_VIDEO)
            sdr = server.transcode_command('vaapi', Path('sdr.mkv'), None, 0)
        self.assertIn('tonemapping=mobius', hdr[hdr.index('-vf') + 1])
        self.assertNotIn('lanstream_vk', ' '.join(sdr))

    @unittest.skipUnless(Path('/dev/dri/renderD128').exists(), 'Linux VAAPI/Vulkan GPU required')
    def test_real_libplacebo_pipeline(self):
        supported, detail = server.tone_mapping_test('vaapi', 'libplacebo-bt2390')
        self.assertTrue(supported, detail)

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

    def test_capability_error_keeps_cause_before_large_format_dump(self):
        cause = "Impossible to convert between the formats supported by 'Parsed_sidedata' and 'auto_scale_0'"
        error = cause + '\nLink formats:\n    src: ' + 'yuv420p10le ' * 1000 + '\n    dst: vaapi\nError reinitializing filters!'
        with patch.object(server, 'ffmpeg_filters', return_value=tone_mapping_plan(HDR10_VIDEO, 'vaapi').required), patch.object(server, 'run_media_command', return_value=Mock(returncode=1, stdout=b'', stderr=error.encode())):
            supported, detail = server.tone_mapping_test('vaapi')
        self.assertFalse(supported)
        self.assertTrue(detail.startswith(cause))
        self.assertIn('dst: vaapi', detail)
        self.assertIn('Error reinitializing filters!', detail)
        self.assertLessEqual(len(detail), 2000)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required')
    def test_explicit_sdr_boundary_needs_no_implicit_color_converter(self):
        # A null filter models VPP's pass-through format negotiation, not its
        # pixel processing. This reproduces the graph bug without a VAAPI GPU.
        # FFmpeg 8.1 must not negotiate BT.2020 all the way to a BT.709 encoder.
        prefix = tone_mapping_plan(HDR10_VIDEO, 'vaapi').filters[0] + ',null'
        base = [server.FFMPEG, '-hide_banner', '-loglevel', 'error', '-noauto_conversion_filters',
                '-f', 'lavfi', '-i', 'color=size=64x64:rate=1', '-frames:v', '1', '-vf']
        output = ['-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709',
                  '-color_range', 'tv', '-c:v', 'libx264', '-f', 'null', '-']
        broken = subprocess.run(base + [prefix + ',format=yuv420p,sidedata=mode=delete'] + output,
                                capture_output=True, timeout=10)
        version = subprocess.check_output([server.FFMPEG, '-version'], text=True, timeout=10)
        if 'ffmpeg version 8.1' in version:
            self.assertNotEqual(broken.returncode, 0)
            self.assertIn(b'common format', broken.stderr)
        fixed = subprocess.run(base + [prefix + ',' + SDR_FRAME_PROPERTIES + ',format=yuv420p,sidedata=mode=delete'] + output,
                               capture_output=True, timeout=10)
        self.assertEqual(fixed.returncode, 0, fixed.stderr.decode('utf-8', 'replace'))

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
