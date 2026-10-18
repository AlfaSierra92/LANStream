import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from werkzeug.exceptions import Forbidden

import app as server


class PlaybackInfoTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.video = self.root / 'movie.mp4'
        self.video.touch()
        self.other = self.root / 'other.mp4'
        self.other.touch()
        self.tracker = server.StreamTracker()
        for item in [patch.object(server, 'authentication_required', return_value=False),
                     patch.object(server, 'get_root', return_value=self.root),
                     patch.object(server, 'path_is_excluded', return_value=False),
                     patch.object(server, 'stream_tracker', self.tracker)]:
            item.start()
            self.addCleanup(item.stop)
        self.client = server.app.test_client()
        with self.client.session_transaction() as session:
            session['playback_owner'] = 'owner-a'

    def start_stream(self):
        with server.app.test_request_context('/?playback_id=token-a'):
            server.session['playback_owner'] = 'owner-a'
            stream_id = self.tracker.start('software', self.video, detail='Software · 480p')
        self.tracker.add_bytes(stream_id, 4096)
        return stream_id

    def test_metrics_are_scoped_to_session_file_and_request(self):
        stream_id = self.start_stream()
        url = '/api/playback-info/0/movie.mp4?playback_id=token-a'
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        metrics = response.json['stream']
        self.assertEqual(metrics['bytes_sent'], 4096)
        self.assertEqual(metrics['backend'], 'software')
        self.assertTrue(metrics['active'])
        self.assertNotIn('owner', metrics)
        self.assertNotIn('path', metrics)
        self.assertIsNone(server.app.test_client().get(url).json['stream'])
        self.assertIsNone(self.client.get(url.replace('token-a', 'token-b')).json['stream'])
        self.assertIsNone(self.client.get(url.replace('movie.mp4', 'other.mp4')).json['stream'])
        self.tracker.stop(stream_id)
        self.assertEqual(self.tracker.snapshot(), [])
        metrics = self.client.get(url).json['stream']
        self.assertFalse(metrics['active'])
        self.assertEqual(metrics['bandwidth_mbps'], 0)
        self.assertEqual(metrics['bytes_sent'], 4096)
        with patch.object(server.time, 'monotonic', return_value=time.monotonic() + 601):
            self.assertIsNone(self.client.get(url).json['stream'])

    def test_metrics_obey_media_access_checks(self):
        with patch.object(server, 'get_root', side_effect=Forbidden):
            self.assertEqual(self.client.get('/api/playback-info/0/movie.mp4?playback_id=x').status_code, 403)

    def test_video_pages_do_not_schedule_previews(self):
        info = {'streams': [{'codec_type': 'video', 'codec_name': 'h264', 'width': 1920, 'height': 1080}], 'format': {'duration': '60'}}
        with patch.object(server.image_scheduler, 'submit') as schedule, patch.object(server, 'probe', return_value=info):
            browse = self.client.get('/browse/0/')
            details = self.client.get('/video/0/movie.mp4')
            self.assertEqual(browse.status_code, 200)
            self.assertEqual(details.status_code, 200)
            self.assertNotIn(b'Generating preview', details.data)
            self.assertNotIn(b'data-ready=', browse.data)
            self.assertNotIn(b'/poster/', details.data)
            schedule.assert_not_called()
        with patch.object(server.subprocess, 'run') as process:
            server.generate_image(self.video, self.root / 'thumb.jpg', 360, server.image_scheduler.current_generation(), server.image_scheduler)
            process.assert_not_called()
        self.assertEqual(self.client.get('/thumb/0/movie.mp4').status_code, 404)
        self.assertEqual(self.client.get('/api/image-ready/thumbs/0/movie.mp4').status_code, 404)
        self.assertEqual(self.client.get('/poster/0/movie.mp4').status_code, 404)

    def test_image_thumbnails_still_work(self):
        source = self.root / 'image.png'
        output = self.root / 'image-thumb.jpg'
        server.Image.new('RGB', (800, 400), 'blue').save(source)
        generation = server.image_scheduler.current_generation()
        server.generate_image(source, output, 360, generation, server.image_scheduler)
        with server.Image.open(output) as thumbnail:
            self.assertEqual(thumbnail.size, (360, 180))


if __name__ == '__main__':
    unittest.main()
