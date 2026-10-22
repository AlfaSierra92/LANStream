import importlib.util
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest


@unittest.skipUnless(os.name == 'posix' and importlib.util.find_spec('gunicorn'), 'Gunicorn on POSIX required')
class GunicornShutdownTests(unittest.TestCase):
    def test_sigterm_stops_stream_and_probe_without_orphan_processes(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='lanstream-stop-') as directory:
            root = Path(directory)
            socket_path = root / 'server.sock'
            (root / 'video.mp4').touch()
            (root / 'shutdown_fixture.py').write_text(r'''
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import app as server
root = Path(os.environ['LANSTREAM_TEST_ROOT'])
server.authentication_required = lambda: False
server.get_root = lambda index: root
server.path_is_excluded = lambda path: False
server.selected_transcoder = lambda: {'ok': True, 'key': 'software', 'label': 'Software'}
server.probe = lambda source: {}
code = "import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); sys.stderr.write('x'*200000); sys.stderr.flush(); exec('while True: sys.stdout.buffer.write(b\"video-data\"*1000); sys.stdout.flush(); time.sleep(.05)')"
server.transcode_command = lambda *args: [sys.executable, '-u', '-c', code, str(root / 'encoder.pid')]
class Upstream(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'video/mp4')
        self.end_headers()
        self.wfile.flush()
        (root / 'remote-open').touch()
        import time
        time.sleep(60)
    def log_message(self, *args):
        pass
upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
threading.Thread(target=upstream.serve_forever, daemon=True).start()
(root / 'remote.strm').write_text('http://127.0.0.1:%d/movie.mp4' % upstream.server_port)
@server.app.route('/blocked-probe')
def blocked_probe():
    server.run_media_command([sys.executable, '-c', "import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)", str(root / 'probe.pid')], capture_output=True, timeout=60)
    return 'done'
application = server.app
''')
            env = {**os.environ, 'LANSTREAM_TEST_ROOT': str(root), 'PYTHONDONTWRITEBYTECODE': '1'}
            with (root / 'server.log').open('w+') as log:
                process = subprocess.Popen([sys.executable, '-m', 'gunicorn', '--config', str(repo / 'gunicorn.conf.py'), '--bind', 'unix:' + str(socket_path), '--pythonpath', str(root), 'shutdown_fixture:application'], cwd=repo, env=env, stdout=log, stderr=log, start_new_session=True)
                connections = []
                children = []
                try:
                    deadline = time.monotonic() + 8
                    while not socket_path.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(socket_path.exists(), self._log(log))
                    for route, pidfile in [('/transcode/0/video.mp4', 'encoder.pid'), ('/blocked-probe', 'probe.pid'), ('/media/0/remote.strm', 'remote-open')]:
                        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        connections.append(connection)
                        connection.settimeout(5)
                        connection.connect(str(socket_path))
                        connection.sendall(f'GET {route} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n'.encode())
                        deadline = time.monotonic() + 5
                        while not (root / pidfile).exists() and process.poll() is None and time.monotonic() < deadline:
                            time.sleep(0.02)
                        self.assertTrue((root / pidfile).exists(), self._log(log))
                        if pidfile.endswith('.pid'):
                            children.append(int((root / pidfile).read_text()))
                        if pidfile == 'encoder.pid':
                            # stderr is much larger than a pipe buffer: reaching
                            # stdout proves the stream cannot deadlock on stderr.
                            self.assertIn(b'200 OK', connection.recv(65536))
                    started = time.monotonic()
                    process.send_signal(signal.SIGTERM)
                    process.wait(timeout=8)
                    self.assertEqual(process.returncode, 0, self._log(log))
                    self.assertLess(time.monotonic() - started, 8)
                    self.assertNotIn('SIGKILL', self._log(log))
                    for pid in children:
                        with self.assertRaises(ProcessLookupError):
                            os.kill(pid, 0)
                finally:
                    for connection in connections:
                        connection.close()
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)

    @staticmethod
    def _log(log):
        log.flush()
        log.seek(0)
        return log.read()


if __name__ == '__main__':
    unittest.main()
