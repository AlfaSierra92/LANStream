import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

import app as server
from nzbdav import BlockCache, MediaService, RemoteError, Resource, WebDAVPath, byte_range


class BlockCacheTests(unittest.TestCase):
    def test_concurrent_sources_stay_within_the_shared_memory_budget(self):
        cache = BlockCache(64, block_size=16)
        def consume(source):
            for index in range(40):
                key = (source, index)
                with cache.block(key, lambda: bytes([index]) * 16) as block:
                    self.assertEqual(bytes(block), bytes([index]) * 16)
                    state = cache.snapshot()
                    self.assertLessEqual(state['stored_bytes'] + state['reserved_bytes'], 64)
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(consume, source) for source in range(8)]
            for future in futures:
                future.result(5)

    def test_paused_consumer_does_not_pin_blocks_or_stop_other_streams(self):
        service = MediaService('http://example.invalid', cache_mb=8)
        self.addCleanup(service.close)
        service.cache = BlockCache(32, block_size=16)
        resource = Resource('long-video', 1600)
        service.client.read = lambda resource, offset, length: bytes([offset // 16]) * length
        slow_reader = service.chunks(resource, 0, 1599)
        self.addCleanup(slow_reader.close)
        self.assertEqual(next(slow_reader), b'\x00' * 16)
        # Leave the first generator suspended, simulating a stalled socket.
        fast_reader = service.chunks(resource, 160, 319)
        self.assertEqual(len(b''.join(fast_reader)), 160)
        self.assertGreater(service.cache.snapshot()['evictions'], 0)
        self.assertLessEqual(service.cache.snapshot()['stored_bytes'], 32)
        self.assertEqual(next(slow_reader), b'\x01' * 16)

    def test_long_open_source_evicts_old_blocks_and_can_seek_back(self):
        cache = BlockCache(32, block_size=16)
        loads = []
        for index in list(range(100)) + [0]:
            def load(index=index):
                loads.append(index)
                self.assertLessEqual(cache.snapshot()['stored_bytes'] + cache.snapshot()['reserved_bytes'], 32)
                return bytes([index]) * 16
            with cache.block(('same-open-video', index), load) as block:
                self.assertEqual(bytes(block), bytes([index]) * 16)
            self.assertLessEqual(cache.snapshot()['stored_bytes'], 32)
        self.assertEqual(loads.count(0), 2)
        self.assertEqual(cache.snapshot()['evictions'], 99)

    def test_concurrent_readers_share_a_single_fetch(self):
        cache = BlockCache(32, block_size=16)
        entered, finish = threading.Event(), threading.Event()
        loads = []
        def load():
            loads.append(1)
            entered.set()
            self.assertTrue(finish.wait(3))
            return b'x' * 16
        def read():
            with cache.block('one', load) as block:
                return bytes(block)
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(read) for _ in range(8)]
            self.assertTrue(entered.wait(3))
            self.assertEqual(cache.snapshot()['reserved_bytes'], 16)
            finish.set()
            self.assertTrue(all(future.result(3) == b'x' * 16 for future in futures))
        self.assertEqual(len(loads), 1)

    def test_active_blocks_backpressure_and_shutdown_wake_waiters(self):
        cache = BlockCache(32, block_size=16)
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        for key in ('a', 'b'):
            contexts.enter_context(cache.block(key, lambda: b'x' * 16))
        entered = threading.Event()
        def waiting():
            entered.set()
            with cache.block('c', lambda: b'y' * 16):
                pass
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(waiting)
            self.assertTrue(entered.wait(3))
            self.assertFalse(future.done())
            cache.close()
            with self.assertRaises(RemoteError):
                future.result(3)

    def test_failure_releases_reserved_memory(self):
        cache = BlockCache(32, block_size=16)
        def fail():
            raise RemoteError('failed')
        with self.assertRaises(RemoteError), cache.block('a', fail):
            pass
        self.assertEqual(cache.snapshot()['reserved_bytes'], 0)
        with cache.block('a', lambda: b'ok') as block:
            self.assertEqual(bytes(block), b'ok')


class DAVFixture(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def handle(self):
        try:
            super().handle()
        except ConnectionResetError:
            pass
    def log_message(self, *args):
        pass

    def authorized(self):
        expected = 'Basic ' + base64.b64encode(b'viewer:private-password').decode()
        self.server.calls.append((self.command, self.path, self.headers.get('Range'), self.headers.get('Cookie')))
        self.server.ports.add(self.client_address[1])
        if self.headers.get('Authorization') != expected:
            self.send_error(401)
            return False
        return True

    def do_PROPFIND(self):
        if not self.authorized():
            return
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        path = unquote(urlsplit(self.path).path).strip('/')
        if path not in self.server.directories:
            self.send_error(404)
            return
        names = [path]
        if self.headers.get('Depth') == '1':
            names += [name for name in [*self.server.directories, *self.server.files]
                      if name != path and name.rpartition('/')[0] == path]
        rows = []
        for name in names:
            directory = name in self.server.directories
            data = self.server.files.get(name, b'')
            rows.append(f'<d:response><d:href>/{quote(name)}{ "/" if directory else ""}</d:href>'
                        f'<d:propstat><d:prop><d:resourcetype>{"<d:collection/>" if directory else ""}</d:resourcetype>'
                        f'<d:getcontentlength>{len(data)}</d:getcontentlength><d:getetag>"v1"</d:getetag>'
                        '</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')
        body = ('<d:multistatus xmlns:d="DAV:">' + ''.join(rows) + '</d:multistatus>').encode()
        self.send_response(207)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Set-Cookie', 'session=remembered; Path=/')
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_media(head=True)

    def do_GET(self):
        self.send_media()

    def send_media(self, head=False):
        if not self.authorized():
            return
        path = unquote(urlsplit(self.path).path).strip('/')
        if path not in self.server.files:
            self.send_error(404)
            return
        data = self.server.files[path]
        if self.headers.get('If-Match') and self.headers['If-Match'] != '"v1"':
            self.send_error(412)
            return
        requested = self.headers.get('Range') if not self.server.ignore_range else None
        try:
            start, end, status = byte_range(requested, len(data))
        except ValueError:
            self.send_error(416)
            return
        self.send_response(status)
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('ETag', '"v1"')
        self.send_header('Set-Cookie', 'session=remembered; Path=/')
        if status == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{len(data)}')
        self.end_headers()
        if not head:
            if path == self.server.stall_path:
                self.wfile.flush()
                self.server.read_started.set()
                self.server.release_read.wait(3)
            try:
                self.wfile.write(data[start:end + 1])
            except (BrokenPipeError, ConnectionResetError):
                pass


class IntegratedDAVTests(unittest.TestCase):
    def setUp(self):
        server.stop_nzbdav()
        self.addCleanup(server.stop_nzbdav)
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), DAVFixture)
        self.http.daemon_threads = True
        self.http.files = {'content/Folder/video name.mp4': b'0123456789' * 100,
                           'content/Folder/ignore.txt': b'text'}
        self.http.directories = {'content', 'content/Folder'}
        self.http.calls = []
        self.http.ignore_range = False
        self.http.ports = set()
        self.http.stall_path = None
        self.http.read_started = threading.Event()
        self.http.release_read = threading.Event()
        self.addCleanup(self.http.release_read.set)
        threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.url = f'http://127.0.0.1:{self.http.server_port}/'
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = self.root / 'config.txt'
        self.config.write_text(f'nzbdav_url={self.url}\nnzbdav_user=viewer\nnzbdav_password=private-password\n'
                               'nzbdav_root=content\nnzbdav_ram_cache_mb=8\n')
        for item in (patch.object(server, 'CONFIG_FILE', self.config),
                     patch.object(server, 'USE_MASTER_PASSWORD', False), patch.object(server, 'APP_PASSWORD', '')):
            item.start()
            self.addCleanup(item.stop)
        self.client = server.app.test_client()
        self.index = server.NZBDAV_ROOT_INDEX

    def test_browse_nested_folders_ranges_cookies_and_shared_cache(self):
        self.assertIn(b'NZBDAV', self.client.get('/').data)
        self.assertIn(b'Folder', self.client.get(f'/browse/{self.index}/').data)
        response = self.client.get(f'/browse/{self.index}/Folder')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn(b'video name.mp4', response.data)
        self.assertNotIn(b'ignore.txt', response.data)
        route = f'/media/{self.index}/Folder/video%20name.mp4'
        for _ in range(2):
            response = self.client.get(route, headers={'Range': 'bytes=10-19'}, buffered=True)
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.data, b'0123456789')
            self.assertEqual(response.headers['Content-Range'], 'bytes 10-19/1000')
        self.assertEqual(len([call for call in self.http.calls if call[0] == 'GET']), 1)
        self.assertTrue(any(call[3] == 'session=remembered' for call in self.http.calls))
        self.assertEqual(self.client.head(route).status_code, 200)
        self.assertEqual(self.client.get(route, headers={'Range': 'bytes=9999-'}).status_code, 416)
        self.assertEqual(set(self.root.iterdir()), {self.config})

    def test_file_larger_than_ram_cache_streams_to_end_then_refetches_old_range(self):
        payload = bytes(range(256)) * (20 * 1024 * 1024 // 256)
        self.http.files['content/large.mp4'] = payload
        route = f'/media/{self.index}/large.mp4'
        response = self.client.get(route, buffered=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, payload)
        cache = server.nzbdav_service().cache.snapshot()
        self.assertLessEqual(cache['stored_bytes'] + cache['reserved_bytes'], 8 * 1024 * 1024)
        self.assertGreaterEqual(cache['evictions'], 3)
        reads = len([call for call in self.http.calls if call[0] == 'GET'])
        response = self.client.get(route, headers={'Range': 'bytes=0-127'}, buffered=True)
        self.assertEqual(response.data, payload[:128])
        self.assertEqual(len([call for call in self.http.calls if call[0] == 'GET']), reads + 1)
        self.assertEqual(set(self.root.iterdir()), {self.config})
        self.assertEqual(len(self.http.ports), 1, 'Reuse one TCP connection across sequential block reads')

    def test_shutdown_stops_private_gateway_and_closes_cache(self):
        service = server.nzbdav_service()
        row = service.client.stat('content/Folder/video name.mp4')
        url = service.input_url(row)
        with urlopen(url) as response:
            self.assertEqual(len(response.read()), 1000)
        service.close()
        self.assertEqual(service.cache.snapshot()['stored_bytes'], 0)
        with self.assertRaises(RemoteError):
            with service.block(row, 0):
                pass

    def test_metadata_refresh_invalidates_sources_without_strong_validators(self):
        service = server.nzbdav_service()
        first = service.client.remember(Resource('content/unversioned.mp4', 100))
        second = service.client.remember(Resource('content/unversioned.mp4', 100))
        self.assertNotEqual(first.version, second.version)
        first = service.client.remember(Resource('content/versioned.mp4', 100, '"stable"'))
        second = service.client.remember(Resource('content/versioned.mp4', 100, '"stable"'))
        self.assertEqual(first.version, second.version)

    def test_shutdown_interrupts_blocked_upstream_read(self):
        service = server.nzbdav_service()
        self.http.stall_path = 'content/Folder/video name.mp4'
        row = service.client.stat(self.http.stall_path)
        def read():
            with service.block(row, 0):
                pass
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(read)
            self.assertTrue(self.http.read_started.wait(3))
            service.close()
            with self.assertRaises(RemoteError):
                future.result(2)
            self.http.release_read.set()


    def test_profile_restrictions_traversal_and_no_credentials_in_pages(self):
        self.config.write_text(self.config.read_text() + '<admin>\n<restricted>\n')
        self.client.post('/login', data={'password': 'restricted'})
        self.assertNotIn(b'NZBDAV', self.client.get('/').data)
        self.assertEqual(self.client.get(f'/browse/{self.index}/').status_code, 403)
        self.client.get('/logout')
        self.client.post('/login', data={'password': 'admin'})
        self.assertEqual(self.client.get(f'/browse/{self.index}/../outside').status_code, 403)
        response = self.client.get(f'/browse/{self.index}/Folder')
        self.assertNotIn(b'private-password', response.data)
        self.assertNotIn(self.url.encode(), response.data)

    def test_private_gateway_head_ranges_tokens_and_no_full_download_fallback(self):
        service = server.nzbdav_service()
        row = service.client.stat('content/Folder/video name.mp4')
        url = service.input_url(row)
        with urlopen(Request(url, method='HEAD')) as response:
            self.assertEqual(response.headers['Content-Length'], '1000')
        for interval, expected in [('bytes=4-8', b'45678'), ('bytes=-3', b'789')]:
            with urlopen(Request(url, headers={'Range': interval})) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), expected)
        bad = url.replace(url.split('/')[3], 'not-authorized')
        with self.assertRaises(HTTPError) as caught:
            urlopen(bad)
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()
        self.http.ignore_range = True
        row = Resource('content/Folder/video name.mp4', 1000)
        with self.assertRaisesRegex(RemoteError, 'byte ranges'):
            service.client.read(row, 100, 100)

    def test_invalid_config_and_preservation_on_settings_save(self):
        data = self.client.get('/settings')
        self.assertNotIn(b'private-password', data.data)
        with self.client.session_transaction() as session:
            csrf = session['settings_csrf']
        response = self.client.post('/settings', data={'csrf': csrf, 'version': server.configuration_version(),
                                    'port': '8060', 'transcoder': 'software', 'mode': 'none', 'public_paths': ''})
        self.assertEqual(response.status_code, 302)
        self.assertIn('nzbdav_password=private-password', self.config.read_text())
        self.config.write_text(self.config.read_text().replace('nzbdav_ram_cache_mb=8', 'nzbdav_ram_cache_mb=0'))
        self.assertEqual(self.client.get(f'/browse/{self.index}/').status_code, 503)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_real_ffprobe_transcode_and_subtitles_use_one_ram_cache(self):
        subtitle = self.root / 'captions.srt'
        subtitle.write_text('1\n00:00:00,000 --> 00:00:01,000\nHello from NZBDAV\n')
        source = self.root / 'fixture.mkv'
        subprocess.run([server.FFMPEG, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=12',
                        '-f', 'lavfi', '-i', 'sine=frequency=440', '-i', str(subtitle),
                        '-map', '0:v', '-map', '1:a', '-map', '2:s', '-t', '2',
                        '-c:v', 'libx264', '-g', '12', '-c:a', 'aac', '-c:s', 'srt', str(source)],
                       capture_output=True, check=True, timeout=20)
        self.http.files['content/fixture.mkv'] = source.read_bytes()
        response = self.client.get(f'/play/{self.index}/fixture.mkv')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn(b'private-password', response.data)
        self.assertNotIn(self.url.encode(), response.data)
        with patch.object(server, 'selected_transcoder', return_value={'ok': True, 'key': 'software', 'label': 'Software'}):
            response = self.client.get(f'/transcode/{self.index}/fixture.mkv?quality=360p&audio=1&start=0.5', buffered=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'ftyp', response.data[:32])
        response = self.client.get(f'/subtitle/{self.index}/fixture.mkv?stream=2')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Hello from NZBDAV', response.data)
        self.assertEqual(len([call for call in self.http.calls if call[0] == 'GET']), 1)
        self.assertFalse(server.stream_tracker.snapshot())


if __name__ == '__main__':
    unittest.main()
