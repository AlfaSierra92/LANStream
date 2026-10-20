import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from media_runtime import MediaRuntime
import app as server


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.runtime = MediaRuntime()
        self.addCleanup(self.runtime.shutdown)

    def test_shutdown_reaps_all_children_and_refuses_new_work(self):
        children = [self.runtime.popen([sys.executable, '-c', 'import time; time.sleep(60)']) for _ in range(3)]
        start = time.monotonic()
        self.runtime.shutdown()
        self.assertLess(time.monotonic() - start, 4)
        self.assertTrue(all(child.poll() is not None for child in children))
        with self.assertRaises(RuntimeError):
            self.runtime.popen([sys.executable, '-c', 'pass'])
        self.runtime.shutdown()

    def test_run_drains_large_error_output_and_reaps_on_timeout(self):
        result = self.runtime.run([sys.executable, '-c', 'import sys; sys.stderr.write("x"*200000); print("done")'], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.stdout, 'done\n')
        self.assertEqual(len(result.stderr), 200000)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.runtime.run([sys.executable, '-c', 'import time; time.sleep(60)'], capture_output=True, timeout=0.1)
        self.assertFalse(self.runtime._processes)

    def test_shutdown_interrupts_inflight_probe(self):
        started = threading.Event()
        result = []
        original_popen = self.runtime.popen

        def popen(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            started.set()
            return process

        with patch.object(self.runtime, 'popen', side_effect=popen):
            thread = threading.Thread(target=lambda: result.append(self.runtime.run([sys.executable, '-c', 'import time; time.sleep(60)'], capture_output=True, timeout=30)))
            thread.start()
            self.assertTrue(started.wait(3))
            self.runtime.shutdown()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertNotEqual(result[0].returncode, 0)

    @unittest.skipUnless(os.name == 'posix', 'POSIX signals required')
    def test_shutdown_kills_unresponsive_children_with_shared_deadline(self):
        children = [self.runtime.popen([sys.executable, '-u', '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print("ready"); time.sleep(60)'], stdout=subprocess.PIPE) for _ in range(2)]
        for process in children:
            self.assertEqual(process.stdout.readline(), b'ready\n')
            self.addCleanup(process.stdout.close)
        started = time.monotonic()
        self.runtime.shutdown()
        self.assertLess(time.monotonic() - started, 4)
        self.assertTrue(all(process.returncode == -9 for process in children))

    def test_unconsumed_native_wsgi_responses_release_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'video.mp4'
            source.write_bytes(b'original-video')
            tracker = server.StreamTracker()
            with patch.object(server, 'stream_tracker', tracker):
                for headers in ({}, {'Range': 'bytes=0-3'}):
                    with server.app.test_request_context('/', headers=headers):
                        response = server.ranged_file_response(source, 'video/mp4')
                        iterable = response(server.request.environ, lambda *args: None)
                    self.assertEqual(len(tracker.snapshot()), 1)
                    iterable.close()
                    self.assertFalse(tracker.snapshot())

    def test_unconsumed_transcode_response_releases_child_and_tracker(self):
        process = Mock(stdout=io.BytesIO(b'mp4'), stderr=io.BytesIO(), returncode=0)
        process.poll.return_value = 0
        tracker = server.StreamTracker()
        with patch.object(server, 'get_root'), patch.object(server, 'safe_path'), patch.object(server, 'is_video', return_value=True), patch.object(server, 'resolve_media_source'), patch.object(server, 'selected_transcoder', return_value={'ok': True, 'key': 'software', 'label': 'Software'}), patch.object(server, 'transcode_command', return_value=['ffmpeg']), patch.object(server.media_runtime, 'popen', return_value=process), patch.object(server.media_runtime, 'release') as release, patch.object(server, 'stream_tracker', tracker):
            with server.app.test_request_context('/transcode/0/movie.mp4'):
                response = server.transcode(0, 'movie.mp4')
            self.assertEqual(len(tracker.snapshot()), 1)
            response.close()
            response.close()
            self.assertFalse(tracker.snapshot())
            self.assertTrue(process.stdout.closed)
            release.assert_called_once_with(process)

    def test_bad_duration_and_subtitle_parameters(self):
        for duration in ['inf', '-inf', 'nan', 'bad']:
            self.assertEqual(server.duration_seconds({'format': {'duration': duration}}), 0)
        self.assertIsNone(server.parse_range_header('bytes=-10', 0))
        with patch.object(server, 'authentication_required', return_value=False), patch.object(server, 'get_root'), patch.object(server, 'safe_path'), patch.object(server, 'is_video', return_value=True), patch.object(server, 'resolve_media_source'), patch.object(server, 'run_media_command') as run:
            for query in ['stream=-1', 'stream=1&start=nan', 'stream=1&start=inf', 'stream=1&start=-1']:
                self.assertEqual(server.app.test_client().get('/subtitle/0/movie.mp4?' + query).status_code, 400)
            run.assert_not_called()

    def test_image_worker_survives_failure_and_stops(self):
        scheduler = server.ImageScheduler(1)
        self.addCleanup(scheduler.shutdown)
        output = Path('/private/tmp/lanstream-test-no-image-output.jpg')
        with patch.object(server, 'generate_image', side_effect=RuntimeError('bad image')), patch.object(server.app.logger, 'exception'):
            scheduler.submit(0, 1, Path('bad.png'), output, 360)
            deadline = time.monotonic() + 2
            while not scheduler.failed(0, output) and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertTrue(scheduler.failed(0, output))
        self.assertTrue(scheduler._workers[0].is_alive())
        scheduler.shutdown()
        scheduler._workers[0].join(1)
        self.assertFalse(scheduler._workers[0].is_alive())


if __name__ == '__main__':
    unittest.main()
