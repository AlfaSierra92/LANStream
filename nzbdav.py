"""Read-only WebDAV media access and a bounded, RAM-only HTTP range cache."""
from __future__ import annotations

import base64
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, replace
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import mimetypes
import posixpath
from pathlib import PurePosixPath
import re
import secrets
import socket
import threading
import time
from types import SimpleNamespace
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request
import xml.etree.ElementTree as ET


class RemoteError(OSError):
    """Safe to show to a user: no URLs, passwords or upstream response bodies."""


class BlockCache:
    """LRU blocks, including reservations for in-flight reads in the capacity.

    Only a block currently being delivered is pinned, never an entire open file.
    Evicted ranges can always be fetched again, even while a video remains open.
    """
    def __init__(self, max_bytes, block_size=4 * 1024 * 1024):
        if max_bytes < block_size * 2:
            raise ValueError("RAM cache must hold at least two blocks")
        self.block_size = block_size
        self.slots = max_bytes // block_size
        self._condition = threading.Condition()
        self._entries = OrderedDict()
        self._pending = set()
        self._closed = False
        self.hits = self.misses = self.evictions = 0

    @contextmanager
    def block(self, key, loader):
        with self._condition:
            while True:
                if self._closed:
                    raise RemoteError("Remote media cache is stopping.")
                entry = self._entries.get(key)
                if entry is not None:
                    self._entries.move_to_end(key)
                    entry[1] += 1
                    self.hits += 1
                    break
                if key in self._pending:
                    self._condition.wait()
                    continue
                if len(self._entries) + len(self._pending) >= self.slots:
                    victim = next((k for k, row in self._entries.items() if row[1] == 0), None)
                    if victim is None:
                        self._condition.wait()
                        continue
                    del self._entries[victim]
                    self.evictions += 1
                self._pending.add(key)
                self.misses += 1
                entry = None
                break
        if entry is None:
            try:
                data = loader()
                if len(data) > self.block_size:
                    raise RemoteError("Remote server exceeded the requested block size.")
                with self._condition:
                    if self._closed:
                        raise RemoteError("Remote media cache is stopping.")
                    entry = [data, 1]
                    self._pending.discard(key)
                    self._entries[key] = entry
            finally:
                with self._condition:
                    self._pending.discard(key)
                    self._condition.notify_all()
        try:
            yield memoryview(entry[0])
        finally:
            with self._condition:
                entry[1] -= 1
                self._condition.notify_all()

    def snapshot(self):
        with self._condition:
            stored = sum(len(row[0]) for row in self._entries.values())
            reserved = len(self._pending) * self.block_size
            return dict(capacity_bytes=self.slots * self.block_size, stored_bytes=stored,
                        reserved_bytes=reserved, blocks=len(self._entries),
                        hits=self.hits, misses=self.misses, evictions=self.evictions)

    def close(self):
        with self._condition:
            self._closed = True
            self._entries.clear()
            self._condition.notify_all()


@dataclass(frozen=True)
class Resource:
    path: str
    size: int
    etag: str = ""
    modified: str = ""
    directory: bool = False
    generation: str = ''

    @property
    def version(self):
        return self.path, self.size, self.etag, self.modified, self.generation


class WebDAV:
    def __init__(self, url, username="", password="", timeout=30):
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("nzbdav_url must be an HTTP(S) WebDAV URL without credentials or query parameters.")
        self.base = url.rstrip('/') + '/'
        self.base_path = unquote(urlsplit(self.base).path)
        self.timeout = timeout
        self.identity = secrets.token_hex(16)
        self._authorization = ("Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()) if username else ""
        self._origin = parsed
        self._cookies = CookieJar()
        self._slots = threading.BoundedSemaphore(8)
        self._lock = threading.Lock()
        self._connections = set()
        self._responses = set()
        self._idle = []
        self._metadata = OrderedDict()
        self._closed = False

    def url(self, path):
        return self.base + quote(path.lstrip('/'), safe='/')

    @contextmanager
    def request(self, path, *, method="GET", headers=None, data=None):
        headers = {"Accept-Encoding": "identity", **(headers or {})}
        if self._authorization:
            headers['Authorization'] = self._authorization
        while not self._slots.acquire(timeout=0.2):
            with self._lock:
                if self._closed:
                    raise RemoteError("NZBDAV connection is stopping.")
        response = None
        connection = None
        try:
            with self._lock:
                if self._closed:
                    raise RemoteError("NZBDAV connection is stopping.")
                if self._idle:
                    connection = self._idle.pop()
                else:
                    cls = HTTPSConnection if self._origin.scheme == 'https' else HTTPConnection
                    connection = cls(self._origin.hostname, self._origin.port, timeout=self.timeout)
                    self._connections.add(connection)
            req = Request(self.url(path), method=method, headers=headers, data=data)
            self._cookies.add_cookie_header(req)
            # HTTPConnection preserves TCP/TLS sessions across block requests.
            # No redirects or environment proxies can leak credentials elsewhere.
            for attempt in range(2):
                try:
                    connection.request(method, req.selector, body=data, headers=dict(req.header_items()))
                    response = connection.getresponse()
                    break
                except (OSError, HTTPException):
                    # Idle keep-alive sockets may have been closed by the
                    # backend. Retry these read-only requests once, before any
                    # response body has been delivered, on a fresh connection.
                    with self._lock:
                        if attempt or self._closed:
                            raise
                        self._connections.discard(connection)
                        connection.close()
                        cls = HTTPSConnection if self._origin.scheme == 'https' else HTTPConnection
                        connection = cls(self._origin.hostname, self._origin.port, timeout=self.timeout)
                        self._connections.add(connection)
            with self._lock:
                if self._closed:
                    raise RemoteError("NZBDAV connection is stopping.")
                self._responses.add(response)
            self._cookies.extract_cookies(response, req)
            if not 200 <= response.status < 300:
                raise RemoteError(f"NZBDAV returned HTTP {response.status}.")
            yield response
        except RemoteError:
            raise
        except (OSError, ValueError, HTTPException):
            raise RemoteError("Unable to read NZBDAV. Check its URL, credentials and connection.") from None
        finally:
            reusable = response is not None and response.length == 0 and not response.will_close
            if response is not None:
                with self._lock:
                    self._responses.discard(response)
                response.close()
            if connection is not None:
                with self._lock:
                    if reusable and not self._closed:
                        self._idle.append(connection)
                    else:
                        self._connections.discard(connection)
                        connection.close()
            self._slots.release()

    def stat(self, path):
        with self._lock:
            row = self._metadata.get(path)
            if row and time.monotonic() < row[0]:
                return row[1]
        with self.request(path, method="HEAD") as response:
            length = response.headers.get('Content-Length', '')
            if not length.isdecimal():
                raise RemoteError("NZBDAV must provide the media Content-Length.")
            resource = Resource(path, int(length), response.headers.get('ETag', ''), response.headers.get('Last-Modified', ''))
        return self.remember(resource)

    def remember(self, resource):
        # Without reliable backend validators, reuse bytes only until metadata
        # is refreshed, rather than indefinitely trusting path and size alone.
        if not resource.modified and (not resource.etag or resource.etag.startswith('W/')):
            resource = replace(resource, generation=secrets.token_hex(8))
        with self._lock:
            self._metadata[resource.path] = (time.monotonic() + 10, resource)
            self._metadata.move_to_end(resource.path)
            while len(self._metadata) > 512:
                self._metadata.popitem(last=False)
        return resource

    def list(self, path, depth=1):
        body = (b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>'
                b'<d:resourcetype/><d:getcontentlength/><d:getetag/><d:getlastmodified/>'
                b'</d:prop></d:propfind>')
        with self.request(path.rstrip('/') + '/', method='PROPFIND',
                          headers={'Depth': str(depth), 'Content-Type': 'application/xml'}, data=body) as response:
            xml = response.read(8 * 1024 * 1024 + 1)
        if len(xml) > 8 * 1024 * 1024 or b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
            raise RemoteError("NZBDAV directory response is too large or unsupported.")
        try:
            tree = ET.fromstring(xml)
        except ET.ParseError:
            raise RemoteError("NZBDAV returned an invalid directory listing.") from None
        resources = {}
        for row in tree.findall('{DAV:}response'):
            href = urlsplit(row.findtext('{DAV:}href', ''))
            if href.netloc and href.netloc != urlsplit(self.base).netloc:
                continue
            full_path = unquote(href.path)
            if not full_path.startswith(self.base_path):
                continue
            relative = full_path[len(self.base_path):].strip('/')
            if any(part in {'.', '..'} for part in relative.split('/')) or '\\' in relative:
                continue
            if depth == 0:
                if relative != path.strip('/'):
                    continue
            elif relative == path.strip('/') or posixpath.dirname(relative) != path.strip('/'):
                continue
            prop = None
            for propstat in row.findall('{DAV:}propstat'):
                if ' 200 ' in propstat.findtext('{DAV:}status', ''):
                    prop = propstat.find('{DAV:}prop')
                    break
            if prop is None:
                continue
            directory = prop.find('{DAV:}resourcetype/{DAV:}collection') is not None
            length = prop.findtext('{DAV:}getcontentlength', '0')
            if not directory and not length.isdecimal():
                continue
            resource = Resource(relative, int(length) if length.isdecimal() else 0,
                                prop.findtext('{DAV:}getetag', ''),
                                prop.findtext('{DAV:}getlastmodified', ''), directory)
            resource = self.remember(resource)
            resources[relative] = resource
        return list(resources.values())

    def read(self, resource, offset, length):
        end = offset + length - 1
        headers = {'Range': f'bytes={offset}-{end}'}
        if resource.etag and not resource.etag.startswith('W/'):
            headers['If-Match'] = resource.etag
        elif resource.modified:
            headers['If-Unmodified-Since'] = resource.modified
        with self.request(resource.path, headers=headers) as response:
            expected = f'bytes {offset}-{end}/{resource.size}'
            # Never accidentally download an entire large file when a server
            # ignores Range. Even a 200 is accepted only for one whole block.
            if response.status == 206:
                if response.headers.get('Content-Range') != expected:
                    raise RemoteError("NZBDAV returned an incorrect byte range.")
            elif not (response.status == 200 and offset == 0 and length == resource.size):
                raise RemoteError("NZBDAV must support HTTP byte ranges.")
            if response.headers.get('Content-Encoding', 'identity') != 'identity':
                raise RemoteError("Compressed byte ranges are not supported.")
            if resource.etag and response.headers.get('ETag', resource.etag) != resource.etag:
                raise RemoteError("NZBDAV media changed during playback. Reopen the video.")
            data = response.read(length + 1)
            if len(data) != length:
                raise RemoteError("NZBDAV returned an incomplete media block.")
            return data

    def close(self):
        with self._lock:
            self._closed = True
            connections = list(self._connections)
            responses = list(self._responses)
            self._idle.clear()
            self._metadata.clear()
        for connection in connections:
            try:
                if connection.sock:
                    connection.sock.shutdown(socket.SHUT_RDWR)
            except (AttributeError, OSError):
                pass
            connection.close()
        for response in responses:
            try:
                response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
            except (AttributeError, OSError):
                pass


class WebDAVPath:
    """A virtual video path. Resolving it never consults the host filesystem."""
    def __init__(self, service, path='', resource=None):
        self.service = service
        self.path = path.strip('/')
        self._resource = resource

    @property
    def name(self):
        return PurePosixPath(self.path).name or 'NZBDAV'

    @property
    def suffix(self):
        return PurePosixPath(self.path).suffix

    def __str__(self):
        return 'nzbdav:/' + self.path

    def __truediv__(self, other):
        if str(other).startswith('/') or '\\' in str(other) or '\x00' in str(other):
            raise ValueError("Invalid remote path")
        return WebDAVPath(self.service, posixpath.join(self.path, str(other)))

    def resolve(self, strict=False):
        if any(part in {'.', '..'} for part in self.path.split('/')):
            raise ValueError("Remote path traversal is not allowed")
        return self

    def relative_to(self, root):
        if not isinstance(root, WebDAVPath) or root.service is not self.service:
            raise ValueError("Different media roots")
        return PurePosixPath(self.path).relative_to(PurePosixPath(root.path))

    def is_dir(self):
        if not self.path or self.path == self.service.root:
            return True
        if self._resource is None:
            rows = self.service.client.list(self.path, depth=0)
            self._resource = rows[0] if rows else Resource(self.path, 0)
        return self._resource.directory

    def is_file(self):
        if self._resource is not None:
            return not self._resource.directory
        return not self.service.client.stat(self.path).directory

    def iterdir(self):
        return [WebDAVPath(self.service, row.path, row) for row in self.service.client.list(self.path)]

    def stat(self):
        row = self.service.client.stat(self.path)
        fingerprint = hashlib.sha256(repr((self.service.client.identity, row.version)).encode()).digest()
        return SimpleNamespace(st_size=row.size, st_dev=0, st_ino=int.from_bytes(fingerprint[:8], 'big'),
                               st_mtime_ns=int.from_bytes(fingerprint[8:16], 'big'), st_ctime_ns=0)

    def input_url(self):
        return self.service.input_url(self.service.client.stat(self.path))


class MediaService:
    def __init__(self, url, username='', password='', root='', cache_mb=256):
        if not 8 <= cache_mb <= 16384:
            raise ValueError("nzbdav_ram_cache_mb must be between 8 and 16384.")
        if '\\' in root or any(part in {'.', '..'} for part in root.strip('/').split('/')):
            raise ValueError("nzbdav_root must stay inside the configured WebDAV endpoint.")
        self.client = WebDAV(url, username, password)
        self.root = root.strip('/')
        self.cache = BlockCache(cache_mb * 1024 * 1024)
        self._lock = threading.Lock()
        self._server = None
        self._sources = OrderedDict()
        self._tokens = {}
        self._closed = False

    @contextmanager
    def block(self, resource, index):
        offset = index * self.cache.block_size
        length = min(self.cache.block_size, resource.size - offset)
        if length <= 0:
            raise RemoteError("Invalid media block offset.")
        key = resource.version, index
        with self.cache.block(key, lambda: self.client.read(resource, offset, length)) as block:
            yield block

    def chunks(self, resource, start, end):
        while start <= end:
            index, within = divmod(start, self.cache.block_size)
            with self.block(resource, index) as block:
                stop = min(len(block), within + 64 * 1024, end - index * self.cache.block_size + 1)
                chunk = bytes(block[within:stop])
                del block
            # Never hold a block lease across network backpressure. A paused
            # browser retains at most this 64 KiB copy, so even a small cache
            # can evict data belonging to that still-open response.
            yield chunk
            start += len(chunk)

    def input_url(self, resource):
        with self._lock:
            if self._closed:
                raise RemoteError("NZBDAV connection is stopping.")
            if self._server is None:
                self._server = Gateway(('127.0.0.1', 0), self)
                threading.Thread(target=self._server.serve_forever,
                                 kwargs={'poll_interval': 0.1}, name='nzbdav-gateway', daemon=True).start()
            token = self._tokens.get(resource.version)
            if token is None:
                token = secrets.token_urlsafe(32)
                self._tokens[resource.version] = token
                self._sources[token] = resource
                while len(self._sources) > 256:
                    old_token, old = self._sources.popitem(last=False)
                    self._tokens.pop(old.version, None)
            else:
                self._sources.move_to_end(token)
            return f'http://127.0.0.1:{self._server.server_port}/{token}/{quote(PurePosixPath(resource.path).name)}'

    def lookup(self, token):
        with self._lock:
            return self._sources.get(token)

    def close(self):
        with self._lock:
            self._closed = True
            server = self._server
        self.cache.close()
        self.client.close()
        if server:
            server.shutdown()
            server.server_close()


def byte_range(value, size):
    if not value:
        return 0, size - 1, 200
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', value.strip())
    if not match or size <= 0:
        raise ValueError("Invalid range")
    a, b = match.groups()
    if not a:
        if not b or int(b) <= 0:
            raise ValueError("Invalid suffix")
        return max(0, size - int(b)), size - 1, 206
    start, end = int(a), min(int(b), size - 1) if b else size - 1
    if start >= size or end < start:
        raise ValueError("Unsatisfiable range")
    return start, end, 206


class Gateway(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16

    def __init__(self, address, service):
        self.service = service
        self._slots = threading.BoundedSemaphore(8)
        super().__init__(address, GatewayHandler)

    def process_request(self, request, address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self._slots.release()


class GatewayHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log capability tokens.

    def do_HEAD(self):
        self.deliver(head=True)

    def do_GET(self):
        self.deliver()

    def deliver(self, head=False):
        self.connection.settimeout(10)
        parts = self.path.split('/')
        resource = self.server.service.lookup(parts[1]) if len(parts) == 3 else None
        if resource is None:
            self.send_error(404)
            return
        try:
            start, end, status = byte_range(self.headers.get('Range'), resource.size)
        except ValueError:
            self.send_response(416)
            self.send_header('Content-Range', f'bytes */{resource.size}')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        self.send_response(status)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(max(0, end - start + 1)))
        self.send_header('Content-Type', mimetypes.guess_type(resource.path)[0] or 'application/octet-stream')
        if status == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{resource.size}')
        self.end_headers()
        if head:
            return
        try:
            for chunk in self.server.service.chunks(resource, start, end):
                self.wfile.write(chunk)
        except (OSError, ValueError):
            self.close_connection = True
