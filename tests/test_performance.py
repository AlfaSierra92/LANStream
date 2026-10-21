import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import app as server
from media_cache import MetadataCache


class MetadataCacheTests(unittest.TestCase):
    def test_concurrent_requests_share_one_probe_and_independent_results(self):
        cache = MetadataCache()
        started, finish = threading.Event(), threading.Event()

        def load():
            started.set()
            self.assertTrue(finish.wait(3))
            return {'streams': [{'codec_name': 'h264'}]}

        loader = Mock(side_effect=load)
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(cache.get, 'movie', loader) for _ in range(8)]
            self.assertTrue(started.wait(3))
            finish.set()
            results = [future.result(timeout=3) for future in futures]
        loader.assert_called_once()
        results[0]['streams'][0]['codec_name'] = 'changed'
        self.assertTrue(all(item['streams'][0]['codec_name'] == 'h264' for item in results[1:]))
        self.assertEqual(cache.get('movie', loader)['streams'][0]['codec_name'], 'h264')

    def test_expiration_eviction_and_failures_allow_retry(self):
        cache = MetadataCache(maxsize=2, ttl=10)
        loader = Mock(return_value={})
        with patch('media_cache.time.monotonic', return_value=0):
            for key in ('a', 'b', 'a', 'c'):
                cache.get(key, loader)
            self.assertEqual(loader.call_count, 3)
            self.assertEqual(list(cache._entries), ['a', 'c'])
        with patch('media_cache.time.monotonic', return_value=11):
            cache.get('a', loader)
            self.assertEqual(loader.call_count, 4)
        failing = Mock(side_effect=[RuntimeError('unavailable'), {'recovered': True}])
        with self.assertRaises(RuntimeError):
            cache.get('failed', failing)
        self.assertEqual(cache.get('failed', failing), {'recovered': True})
        self.assertFalse(cache._pending)

    def test_failed_probe_wakes_all_waiters(self):
        cache = MetadataCache()
        entered, finish, waiting = threading.Event(), threading.Event(), threading.Event()

        def load():
            entered.set()
            self.assertTrue(finish.wait(3))
            raise RuntimeError('probe failed')

        with ThreadPoolExecutor(max_workers=2) as pool:
            owner = pool.submit(cache.get, 'video', load)
            self.assertTrue(entered.wait(3))
            future = cache._pending['video']
            original_result = future.result

            def wait():
                waiting.set()
                return original_result(timeout=3)

            with patch.object(future, 'result', side_effect=wait):
                follower = pool.submit(cache.get, 'video', load)
                self.assertTrue(waiting.wait(3))
                finish.set()
                for task in (owner, follower):
                    with self.assertRaisesRegex(RuntimeError, 'probe failed'):
                        task.result(timeout=3)


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / 'config.txt'
        self.config.write_text(str(self.root) + '\n')
        for item in (patch.object(server, 'CONFIG_FILE', self.config),
                     patch.object(server, 'USE_MASTER_PASSWORD', False),
                     patch.object(server, 'APP_PASSWORD', ''),
                     patch.object(server, 'probe_cache', MetadataCache())):
            item.start()
            self.addCleanup(item.stop)

    def test_large_folder_reads_configuration_once_and_reloads_next_request(self):
        for index in range(120):
            (self.root / f'video{index}.mp4').touch()
        original_read = Path.read_text
        reads = []

        def read(path, *args, **kwargs):
            if path == self.config:
                reads.append(path)
            return original_read(path, *args, **kwargs)

        client = server.app.test_client()
        with patch.object(Path, 'read_text', read):
            response = client.get('/browse/0/')
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'video119.mp4', response.data)
            self.assertEqual(len(reads), 1)
            self.config.write_text('<new-password>\n' + str(self.root) + '\n')
            self.assertEqual(client.get('/browse/0/').status_code, 302)
            self.assertEqual(len(reads), 2)

    def test_exclusions_symlinks_and_natural_sort_survive_fast_listing(self):
        self.config.write_text('<viewer>\n' + str(self.root) + '\n-/hidden\n')
        (self.root / 'hidden').mkdir()
        (self.root / 'hidden' / 'secret.mp4').touch()
        (self.root / 'alias').symlink_to(self.root / 'hidden', target_is_directory=True)
        for name in ('video10.mp4', 'video2.mp4', 'unsupported.bin'):
            (self.root / name).touch()
        client = server.app.test_client()
        client.post('/login', data={'password': 'viewer'})
        response = client.get('/browse/0/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'>hidden<', response.data)
        self.assertNotIn(b'>alias<', response.data)
        self.assertNotIn(b'unsupported.bin', response.data)
        self.assertLess(response.data.index(b'video2.mp4'), response.data.index(b'video10.mp4'))
        self.assertEqual(client.get('/media/0/alias/secret.mp4').status_code, 404)

    def test_symlink_root_works_for_browsing_and_original_media(self):
        library = self.root / 'library'
        library.mkdir()
        alias = self.root / 'shortcut'
        alias.symlink_to(library, target_is_directory=True)
        self.config.write_text(str(alias) + '\n')
        (library / 'video.mp4').write_bytes(b'original-video')
        client = server.app.test_client()
        response = client.get('/browse/0/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'video.mp4', response.data)
        response = client.get('/media/0/video.mp4', buffered=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b'original-video')

    def test_local_probe_reused_until_file_changes(self):
        source = self.root / 'video.mp4'
        source.write_bytes(b'video')
        info = {'format': {'duration': '10'}}
        with patch.object(server, 'run_media_command', return_value=Mock(returncode=0, stdout=json.dumps(info))) as run:
            self.assertEqual(server.probe(source), info)
            self.assertEqual(server.probe(source), info)
            self.assertEqual(run.call_count, 1)
            source.write_bytes(b'updated video')
            server.probe(source)
            self.assertEqual(run.call_count, 2)
            replacement = self.root / 'replacement.mp4'
            replacement.write_bytes(b'updated video')
            os.replace(replacement, source)
            server.probe(source)
            self.assertEqual(run.call_count, 3)

    def test_strm_access_rechecked_before_cached_metadata_and_target_change(self):
        source = self.root / 'movie.strm'
        (self.root / 'first.mp4').touch()
        (self.root / 'second.mp4').touch()
        source.write_text('first.mp4')
        with server.app.test_request_context('/'), patch.object(server, 'run_media_command', return_value=Mock(returncode=0, stdout='{}')) as run:
            server.probe(source)
            server.probe(source)
            self.assertEqual(run.call_count, 1)
            source.write_text('second.mp4')
            server.probe(source)
            self.assertEqual(run.call_count, 2)
            with patch.object(server, 'load_roots', return_value=[]):
                from werkzeug.exceptions import Forbidden
                with self.assertRaises(Forbidden):
                    server.probe(source)
            self.assertEqual(run.call_count, 2)

    def test_remote_metadata_is_always_fresh(self):
        source = self.root / 'live.strm'
        source.write_text('https://example.com/live.m3u8')
        with patch.object(server, 'run_media_command', return_value=Mock(returncode=0, stdout='{}')) as run:
            server.probe(source)
            server.probe(source)
            self.assertEqual(run.call_count, 2)


if __name__ == '__main__':
    unittest.main()
