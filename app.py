from __future__ import annotations

import json
import math
import hashlib
import mimetypes
import os
import queue
import shutil
import sys
import socket
import re
import secrets
import subprocess
import threading
import time
import io
import zipfile
import tempfile
from functools import lru_cache, wraps
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import (
    Flask, Response, abort, jsonify, redirect, render_template, request,
    send_file, session, url_for, make_response, g, has_request_context,
)
from PIL import Image, ImageOps
import fitz

from media_runtime import media_runtime
from media_cache import MetadataCache
from nzbdav import MediaService, WebDAVPath, RemoteError
from subtitle_windows import capture_packets, matroska_payload, subtitle_container
from tone_mapping import tone_mapping_plan, ToneMappingError, HDR10_PROBE, HDR10_VIDEO

run_media_command = media_runtime.run


def request_cached(function):
    """Reuse filesystem/configuration work within one request only."""
    @wraps(function)
    def cached(*args):
        if not has_request_context():
            return function(*args)
        cache = g.setdefault("filesystem_cache", {})
        key = (function, args)
        if key not in cache:
            cache[key] = function(*args)
        return cache[key]
    return cached


def application_dir() -> Path:
    # For a PyInstaller onedir build, editable files belong beside LANStream.exe.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    # PyInstaller stores bundled templates/static under _MEIPASS.
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


APP_DIR = application_dir()
RESOURCE_DIR = resource_dir()
CONFIG_FILE = Path(os.environ.get("LANSTREAM_CONFIG_FILE", str(APP_DIR / "config.txt")))
CACHE_DIR = Path(os.environ.get("LANSTREAM_CACHE_DIR", str(APP_DIR / "cache")))
SESSION_SECRET = os.environ.get("LANSTREAM_SESSION_SECRET") or secrets.token_hex(32)
VAAPI_DEVICE = os.environ.get("LANSTREAM_VAAPI_DEVICE", "/dev/dri/renderD128")


def find_media_tool(name: str) -> str:
    suffix = ".exe" if os.name == "nt" else ""
    local = APP_DIR / f"{name}{suffix}"
    if local.exists():
        return str(local)
    found = shutil.which(name) or shutil.which(f"{name}{suffix}")
    return found or f"{name}{suffix}"


FFMPEG = find_media_tool("ffmpeg")
FFPROBE = find_media_tool("ffprobe")


@request_cached
def read_config_lines() -> list[str]:
    if not CONFIG_FILE.exists():
        return []
    return CONFIG_FILE.read_text(encoding="utf-8").splitlines()


def load_settings() -> dict[str, str]:
    settings = {
        "port": "8060",
        "use_master_password": "false",
        "master_password": "",
        "transcoder": "auto",
        "default_quality": "auto",
        "tone_mapping": "vaapi",
        "nzbdav_url": "",
        "nzbdav_user": "",
        "nzbdav_password": "",
        "nzbdav_root": "",
        "nzbdav_ram_cache_mb": "256",
    }
    for raw in read_config_lines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("<") or line.startswith("-"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lower()
        if key in settings:
            settings[key] = value.strip()
    return settings


def bool_setting(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


SETTINGS = load_settings()
SERVER_PORT = int(os.environ.get("LANSTREAM_PORT", SETTINGS.get("port", "8060")))
APP_PASSWORD = os.environ.get("LANSTREAM_PASSWORD", SETTINGS.get("master_password", ""))
USE_MASTER_PASSWORD = bool_setting(
    os.environ.get("LANSTREAM_USE_MASTER_PASSWORD", SETTINGS.get("use_master_password", "false"))
)
TRANSCODER_PREFERENCE = os.environ.get(
    "LANSTREAM_TRANSCODER", SETTINGS.get("transcoder", "auto")
).strip().lower()


VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".mpg", ".mpeg",
    ".m2ts", ".mts", ".ts", ".wmv", ".flv", ".ogv", ".strm",
}
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif",
}
PDF_EXTENSIONS = {".pdf"}
COMIC_EXTENSIONS = {".cbz", ".cbr"}
TEXT_EXTENSIONS = {".txt", ".md", ".markdown"}
COMIC_IMAGE_EXTENSIONS = IMAGE_EXTENSIONS - {".avif"}
TEXT_SUBTITLE_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}

app = Flask(
    __name__,
    template_folder=str(RESOURCE_DIR / "templates"),
    static_folder=str(RESOURCE_DIR / "static"),
)
app.secret_key = SESSION_SECRET

NZBDAV_ROOT_INDEX = 2147483647
_nzbdav_lock = threading.Lock()
_nzbdav_service = None
_nzbdav_config = None


def stop_nzbdav():
    global _nzbdav_service, _nzbdav_config
    with _nzbdav_lock:
        service = _nzbdav_service
        _nzbdav_service = _nzbdav_config = None
    if service:
        service.close()


media_runtime.add_cleanup("nzbdav", stop_nzbdav)


@request_cached
def nzbdav_service():
    global _nzbdav_service, _nzbdav_config
    if media_runtime.stopping.is_set():
        abort(503, description="LANStream is stopping.")
    settings = load_settings()
    config = tuple(settings[key] for key in ("nzbdav_url", "nzbdav_user", "nzbdav_password",
                                             "nzbdav_root", "nzbdav_ram_cache_mb"))
    with _nzbdav_lock:
        if config == _nzbdav_config:
            return _nzbdav_service
        try:
            replacement = MediaService(*config[:4], cache_mb=int(config[4])) if config[0] else None
        except ValueError:
            abort(503, description="Invalid NZBDAV configuration. Check its URL, root and RAM limit in config.txt.")
        if _nzbdav_service:
            _nzbdav_service.close()
        _nzbdav_service, _nzbdav_config = replacement, config
        return replacement


def media_input_target(target):
    return target.input_url() if isinstance(target, WebDAVPath) else str(target)


class StreamTracker:
    """In-memory live stream metrics. No database or persistent state."""

    def __init__(self):
        self._lock = threading.Lock()
        self._streams: dict[str, dict[str, Any]] = {}
        self._recent: dict[str, dict[str, Any]] = {}

    def start(
        self,
        mode: str,
        source: Path,
        *,
        start_offset: float = 0.0,
        detail: str = "",
        technical: dict | None = None,
    ) -> str:
        stream_id = secrets.token_hex(5)
        now = time.monotonic()
        with self._lock:
            self._streams[stream_id] = {
                "id": stream_id,
                "mode": mode,
                "filename": source.name,
                "path": str(source),
                "client": request.remote_addr or "unknown",
                "started_monotonic": now,
                "started_epoch": time.time(),
                "last_sample_monotonic": now,
                "last_sample_bytes": 0,
                "bytes_sent": 0,
                "bandwidth_mbps": 0.0,
                "start_offset": float(start_offset),
                "detail": detail,
                "technical": technical or {},
                "playback_id": request.args.get("playback_id", "")[:64],
                "owner": session.get("playback_owner"),
            }
        return stream_id

    def add_bytes(self, stream_id: str, amount: int) -> None:
        if amount <= 0:
            return
        now = time.monotonic()
        with self._lock:
            item = self._streams.get(stream_id)
            if not item:
                return
            item["bytes_sent"] += amount

            elapsed = now - item["last_sample_monotonic"]
            if elapsed >= 0.5:
                delta = item["bytes_sent"] - item["last_sample_bytes"]
                instant_mbps = (delta * 8.0) / elapsed / 1_000_000
                previous = item["bandwidth_mbps"]
                item["bandwidth_mbps"] = (
                    instant_mbps if previous <= 0 else (previous * 0.65 + instant_mbps * 0.35)
                )
                item["last_sample_monotonic"] = now
                item["last_sample_bytes"] = item["bytes_sent"]

    def stop(self, stream_id: str) -> None:
        with self._lock:
            item = self._streams.pop(stream_id, None)
            if item and item["playback_id"] and item["owner"]:
                item["stopped_monotonic"] = time.monotonic()
                self._recent[stream_id] = item
                while len(self._recent) > 128:
                    self._recent.pop(next(iter(self._recent)))

    def fail(self, stream_id: str, message: str) -> None:
        with self._lock:
            item = self._streams.get(stream_id) or self._recent.get(stream_id)
            if item:
                item['technical'] = {**item['technical'], 'error': message}

    def playback_metrics(self, playback_id: str, source: Path, owner: str | None) -> dict | None:
        if not playback_id or not owner:
            return None
        now = time.monotonic()
        with self._lock:
            self._recent = {key: item for key, item in self._recent.items()
                            if now - item["stopped_monotonic"] < 600}
            matches = [item for item in [*self._recent.values(), *self._streams.values()]
                       if item["playback_id"] == playback_id and item["path"] == str(source) and item["owner"] == owner]
            if not matches:
                return None
            item = max(matches, key=lambda row: row["started_monotonic"])
            active = "stopped_monotonic" not in item
            elapsed = max(0.001, now - item["started_monotonic"])
            rate = item["bandwidth_mbps"] or item["bytes_sent"] * 8 / elapsed / 1_000_000
            if not active or now - item["last_sample_monotonic"] > 2:
                rate = 0
            return {"backend": item["mode"], "detail": item["detail"], "active": active,
                    "bytes_sent": item["bytes_sent"], "bandwidth_mbps": round(rate, 2),
                    "technical": item["technical"]}

    def snapshot(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            result = []
            for item in self._streams.values():
                elapsed = max(0.001, now - item["started_monotonic"])
                average_mbps = (item["bytes_sent"] * 8.0) / elapsed / 1_000_000
                current_mbps = item["bandwidth_mbps"] or average_mbps
                result.append({
                    "id": item["id"],
                    "mode": item["mode"],
                    "filename": item["filename"],
                    "path": item["path"],
                    "client": item["client"],
                    "started_at": time.strftime(
                        "%Y-%m-%d %H:%M:%S",
                        time.localtime(item["started_epoch"]),
                    ),
                    "elapsed_seconds": round(elapsed, 1),
                    "bytes_sent": item["bytes_sent"],
                    "bandwidth_mbps": round(current_mbps, 2),
                    "average_mbps": round(average_mbps, 2),
                    "start_offset_seconds": round(item["start_offset"], 1),
                    "detail": item["detail"],
                })
            result.sort(key=lambda row: row["started_at"])
            return result


stream_tracker = StreamTracker()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


class ImageScheduler:
    def __init__(self, workers: int = 2):
        self._queue = queue.PriorityQueue()
        self._lock = threading.Lock()
        self._generation = 0
        self._sequence = 0
        self._queued: set[str] = set()
        self._failed: set[str] = set()
        self._stopping = threading.Event()
        self._workers = []
        for i in range(workers):
            worker = threading.Thread(
                target=self._worker, name=f"image-worker-{i}", daemon=True
            )
            self._workers.append(worker)
            worker.start()

    def shutdown(self):
        self._stopping.set()

    def new_generation(self) -> int:
        with self._lock:
            self._generation += 1
            self._failed.clear()
            return self._generation

    def current_generation(self) -> int:
        with self._lock:
            return self._generation

    def mark_failed(self, generation: int, output: Path) -> None:
        key = str(output)
        with self._lock:
            if generation == self._generation:
                self._failed.add(key)

    def failed(self, generation: int, output: Path) -> bool:
        key = str(output)
        with self._lock:
            return generation == self._generation and key in self._failed

    def submit(self, generation: int, priority: int, source: Path, output: Path, width: int) -> None:
        if output.exists():
            return
        key = str(output)
        with self._lock:
            if (
                self._stopping.is_set()
                or generation != self._generation
                or key in self._queued
                or key in self._failed
            ):
                return
            self._sequence += 1
            seq = self._sequence
            self._queued.add(key)
        self._queue.put((priority, seq, generation, key, source, output, width))

    def _worker(self):
        while not self._stopping.is_set():
            try:
                priority, seq, generation, key, source, output, width = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if generation == self.current_generation():
                    generate_image(source, output, width, generation, self)
            except Exception:
                self.mark_failed(generation, output)
                app.logger.exception("Image worker failed for %s", source)
            finally:
                with self._lock:
                    self._queued.discard(key)
                self._queue.task_done()


image_scheduler = ImageScheduler(2)
media_runtime.add_cleanup("image-scheduler", image_scheduler.shutdown)


def profile_id_for_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


@request_cached
def config_has_password_sections() -> bool:
    if not CONFIG_FILE.exists():
        return False
    for raw in read_config_lines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("<") and line.endswith(">"):
            return True
    return False


def authentication_required() -> bool:
    if config_has_password_sections():
        return True

    # Explicit switch prevents stale deployment secrets from silently
    # re-enabling authentication.
    return USE_MASTER_PASSWORD and bool(APP_PASSWORD)


@request_cached
def load_profiles() -> list[dict]:
    """
    Parse config.txt.

    New format:
        <PasswordOne>
        /mnt/media/Movies
        /mnt/media/TV
        -/Blocked Show
        -/Blocked Folder/Nested

    A -/ exclusion is relative to the most recently listed positive root.
    A line beginning with - followed by an absolute path excludes that absolute
    subtree.

    Legacy format without <password> sections remains supported and uses the
    LANSTREAM_PASSWORD environment variable.
    """
    if not CONFIG_FILE.exists():
        return []

    lines = read_config_lines()
    has_sections = config_has_password_sections()

    if not has_sections:
        roots = []
        for raw in lines:
            line = raw.strip()
            if (
                line
                and not line.startswith("#")
                and "=" not in line
                and not (line.startswith("<") and line.endswith(">"))
            ):
                roots.append(Path(line).expanduser())
        return [{
            "id": profile_id_for_password(APP_PASSWORD),
            "password": APP_PASSWORD,
            "roots": roots,
            "excluded": [],
        }]

    profiles = []
    current = None
    last_root = None

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("<") and line.endswith(">"):
            password = line[1:-1]
            if not password:
                continue
            current = {
                "id": profile_id_for_password(password),
                "password": password,
                "roots": [],
                "excluded": [],
            }
            profiles.append(current)
            last_root = None
            continue

        if "=" in line and not line.startswith("-"):
            # Global config setting, not a media path.
            continue

        if current is None:
            continue

        if line.startswith("-"):
            value = line[1:].strip()
            if not value:
                continue

            # -/something is relative to the most recently listed positive root.
            if value.startswith("/") and last_root is not None:
                # If the exact value is itself underneath a configured root,
                # treat it as absolute; otherwise treat it as relative.
                candidate = Path(value).expanduser()
                is_absolute_config_path = any(
                    str(candidate).startswith(str(root).rstrip("/") + "/")
                    or candidate == root
                    for root in current["roots"]
                )
                if is_absolute_config_path:
                    excluded = candidate
                else:
                    excluded = last_root / value.lstrip("/")
            else:
                candidate = Path(value).expanduser()
                excluded = candidate if candidate.is_absolute() else (
                    last_root / candidate if last_root is not None else candidate
                )

            current["excluded"].append(excluded)
            continue

        root = Path(line).expanduser()
        current["roots"].append(root)
        last_root = root

    return profiles


def active_profile() -> dict | None:
    profiles = load_profiles()

    # In legacy passwordless mode there is exactly one implicit profile and no
    # login session. Use it directly.
    if not authentication_required():
        return profiles[0] if profiles else None

    profile_id = session.get("profile_id")
    if not profile_id:
        return None

    for profile in profiles:
        if profile["id"] == profile_id:
            return profile
    return None


def load_roots() -> list[Path]:
    profile = active_profile()
    return profile["roots"] if profile else []


@request_cached
def resolved_exclusions(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    resolved = []
    for path in paths:
        try:
            resolved.append(path.resolve(strict=False))
        except RuntimeError:
            continue
    return tuple(resolved)


def path_is_excluded(path: Path) -> bool:
    if isinstance(path, WebDAVPath):
        return not can_manage_settings()
    profile = active_profile()
    if not profile:
        return True

    try:
        resolved = path.resolve(strict=False)
    except RuntimeError:
        return True

    for excluded_resolved in resolved_exclusions(tuple(profile["excluded"])):
        try:
            if resolved == excluded_resolved:
                return True
            resolved.relative_to(excluded_resolved)
            return True
        except ValueError:
            continue
        except RuntimeError:
            continue

    return False


def root_label(path: Path, index: int) -> str:
    if isinstance(path, WebDAVPath):
        return "NZBDAV"
    return path.name.strip() or f"Media {index + 1}"


def get_root(index: int) -> Path:
    if index == NZBDAV_ROOT_INDEX:
        if not can_manage_settings():
            abort(403)
        service = nzbdav_service()
        if service is None:
            abort(404)
        return WebDAVPath(service, service.root)
    roots = load_roots()
    if index < 0 or index >= len(roots):
        abort(404)
    try:
        root = roots[index].resolve(strict=False)
    except (OSError, RuntimeError):
        abort(404)
    if path_is_excluded(root):
        abort(404)
    return root


def safe_path(root: Path, relative: str) -> Path:
    try:
        rr = root.resolve(strict=False)
        p = (root / relative).resolve(strict=False)
        p.relative_to(rr)
    except (ValueError, RuntimeError):
        abort(403)

    if path_is_excluded(p):
        abort(404)

    return p


def media_kind(path: Path) -> str | None:
    if isinstance(path, WebDAVPath) and path.suffix.lower() not in VIDEO_EXTENSIONS - {'.strm'}:
        return None
    kind = media_kind_for_suffix(path.suffix.lower())
    return kind if kind and path.is_file() else None


def media_kind_for_suffix(suffix: str) -> str | None:
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in PDF_EXTENSIONS:
        return "pdf"
    if suffix in COMIC_EXTENSIONS:
        return "comic"
    if suffix in TEXT_EXTENSIONS:
        return "text"
    return None


def is_video(path: Path) -> bool:
    return media_kind(path) == "video"


def resolve_media_source(source: Path) -> Path | str:
    """Resolve a STRM pointer without allowing access outside the active profile."""
    if isinstance(source, WebDAVPath) or source.suffix.lower() != ".strm":
        return source
    try:
        with source.open("r", encoding="utf-8-sig") as handle:
            content = handle.read(65537)
    except (OSError, UnicodeError):
        abort(400, description="Unable to read STRM file as UTF-8.")
    if len(content) > 65536:
        abort(400, description="STRM file is too large.")
    lines = [line.strip() for line in content.splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) != 1 or any(ord(char) < 32 or ord(char) == 127 for char in lines[0]):
        abort(400, description="STRM files must contain a single URL or local video path.")
    target = lines[0]
    try:
        url = urlsplit(target)
        if url.scheme.lower() in {"http", "https"}:
            if not url.hostname or url.port == 0 or "|" in target or any(char.isspace() for char in target):
                raise ValueError("Invalid HTTP URL")
            return target
    except ValueError:
        abort(400, description="Invalid STRM URL.")
    # Only native absolute paths and relative paths are accepted for local files.
    # Reject FFmpeg protocols (file:, concat:, etc.) and plugin-specific URLs.
    candidate = Path(target)
    if url.scheme and not candidate.is_absolute():
        abort(400, description="Unsupported STRM protocol. Use HTTP, HTTPS or a local video path.")
    try:
        candidate = (source.parent / candidate).resolve()
        allowed = any(candidate.is_relative_to(root.resolve()) for root in load_roots())
    except (OSError, RuntimeError, ValueError):
        abort(400, description="Invalid STRM path.")
    if not allowed:
        abort(403)
    if path_is_excluded(candidate):
        abort(404)
    if candidate.suffix.lower() == ".strm":
        abort(400, description="Nested STRM files are not supported.")
    if not is_video(candidate):
        abort(404)
    return candidate


def natural_sort_key(value: str) -> list[Any]:
    return [int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"(\d+)", value)]


def image_siblings(root: Path, source: Path) -> tuple[str | None, str | None]:
    try:
        items = [
            p for p in source.parent.iterdir()
            if media_kind(p) == "image" and not path_is_excluded(p)
        ]
    except OSError:
        return None, None
    items.sort(key=lambda p: natural_sort_key(p.name))
    try:
        index = next(i for i, item in enumerate(items) if item.resolve(strict=False) == source.resolve(strict=False))
    except (StopIteration, OSError, RuntimeError):
        return None, None
    previous = items[index - 1] if index > 0 else None
    following = items[index + 1] if index + 1 < len(items) else None
    return (
        previous.relative_to(root).as_posix() if previous else None,
        following.relative_to(root).as_posix() if following else None,
    )


@request_cached
def cache_root_key(root: Path) -> str:
    try:
        resolved = root.resolve(strict=False)
    except RuntimeError:
        resolved = root.absolute()

    normalized = os.path.normcase(os.path.normpath(str(resolved)))
    digest = hashlib.sha256(
        normalized.encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:16]
    return f"root_{digest}"


def cache_root_dir(kind: str, root: Path) -> Path:
    return CACHE_DIR / kind / cache_root_key(root)


def cache_path(kind: str, root_index: int, root: Path, source: Path) -> Path:
    rel = source.relative_to(root)
    return cache_root_dir(kind, root) / rel.parent / f"{rel.name}.jpg"


probe_cache = MetadataCache(maxsize=128)


def probe(path: Path) -> dict[str, Any]:
    target = resolve_media_source(path)
    # Resolve permissions and STRM targets on every call, before consulting the
    # cache. Live/remote sources deliberately retain fresh probe semantics.
    if isinstance(target, str):
        return probe_media_target(path, target)
    stat = target.stat()
    key = (FFPROBE, str(target), stat.st_dev, stat.st_ino,
           stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
    return probe_cache.get(key, lambda: probe_media_target(path, target))


def probe_media_target(path: Path, target: Path | str) -> dict[str, Any]:
    cmd = [
        FFPROBE, "-v", "error",
        "-show_entries",
        "format=duration,size,format_name,bit_rate,start_time:"
        "stream=index,codec_type,codec_name,profile,level,width,height,channels,channel_layout,sample_rate,bit_rate,avg_frame_rate,pix_fmt,color_space,color_transfer,color_primaries,color_range,extradata:"
        "stream_tags=language,title:stream_side_data=side_data_type,dv_profile,dv_bl_signal_compatibility_id",
        '-show_data', "-of", "json", media_input_target(target),
    ]
    try:
        proc = run_media_command(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        if isinstance(path, WebDAVPath):
            abort(504, description="NZBDAV source did not respond in time.")
        if path.suffix.lower() == ".strm":
            abort(504, description="STRM source did not respond in time.")
        raise
    if proc.returncode:
        if isinstance(path, WebDAVPath):
            abort(502, description="Unable to probe NZBDAV media. Check connection and HTTP Range support.")
        if path.suffix.lower() == ".strm":
            abort(502, description="Unable to open the STRM media source.")
        raise RuntimeError(proc.stderr.strip() or "ffprobe failed")
    return json.loads(proc.stdout)


def duration_seconds(info: dict[str, Any]) -> float:
    try:
        duration = float(info.get("format", {}).get("duration", 0) or 0)
        return max(0.0, duration) if math.isfinite(duration) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _save_pillow_jpeg(image: Image.Image, output: Path, width: int) -> None:
    image = ImageOps.exif_transpose(image)
    image.thumbnail((width, max(width * 2, 240)), Image.Resampling.LANCZOS)
    if image.mode not in {"RGB", "L"}:
        background = Image.new("RGB", image.size, "black")
        if "A" in image.getbands():
            background.paste(image, mask=image.getchannel("A"))
        else:
            background.paste(image)
        image = background
    elif image.mode == "L":
        image = image.convert("RGB")
    image.save(output, "JPEG", quality=84, optimize=True)


def find_7zip() -> str | None:
    candidates: list[str] = []
    if os.name == "nt":
        candidates.extend(["7zz.exe", "7z.exe", "7za.exe"])
    else:
        candidates.extend(["7zz", "7z", "7za"])

    for name in candidates:
        local = APP_DIR / name
        if local.is_file():
            return str(local)

    for name in candidates:
        found = shutil.which(name)
        if found:
            return found

    return None


SEVEN_ZIP = find_7zip()


def _seven_zip_names(source: Path) -> list[str]:
    if not SEVEN_ZIP:
        raise RuntimeError(
            "7-Zip is required for CBR viewing. Install 7zip/7z "
            "or place the 7-Zip executable beside LANStream."
        )

    proc = run_media_command(
        [SEVEN_ZIP, "l", "-slt", "-ba", str(source)],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(
            "7-Zip could not read this CBR"
            + (f": {detail}" if detail else ".")
        )

    names: list[str] = []
    for line in proc.stdout.splitlines():
        if not line.startswith("Path = "):
            continue
        name = line[7:].strip()
        if name and Path(name).suffix.lower() in COMIC_IMAGE_EXTENSIONS:
            names.append(name)

    names.sort(key=natural_sort_key)
    return names


def _seven_zip_read(source: Path, member: str) -> bytes:
    if not SEVEN_ZIP:
        raise RuntimeError(
            "7-Zip is required for CBR viewing. Install 7zip/7z "
            "or place the 7-Zip executable beside LANStream."
        )

    proc = run_media_command(
        [SEVEN_ZIP, "x", "-so", "-y", str(source), member],
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            "7-Zip could not extract a CBR page"
            + (f": {detail}" if detail else ".")
        )
    return proc.stdout


@lru_cache(maxsize=128)
def _comic_names_cached(path_text: str, mtime_ns: int, size: int) -> tuple[str, ...]:
    source = Path(path_text)
    names: list[str] = []
    suffix = source.suffix.lower()
    if suffix == ".cbz":
        with zipfile.ZipFile(source) as archive:
            names = [
                name for name in archive.namelist()
                if not name.endswith("/")
                and Path(name).suffix.lower() in COMIC_IMAGE_EXTENSIONS
            ]
    elif suffix == ".cbr":
        names = _seven_zip_names(source)
    names.sort(key=natural_sort_key)
    return tuple(names)


def _comic_names(source: Path) -> list[str]:
    stat = source.stat()
    return list(_comic_names_cached(str(source), stat.st_mtime_ns, stat.st_size))


def _comic_image_bytes(source: Path, page: int) -> bytes:
    names = _comic_names(source)
    if page < 0 or page >= len(names):
        raise IndexError(page)
    name = names[page]
    if source.suffix.lower() == ".cbz":
        with zipfile.ZipFile(source) as archive:
            return archive.read(name)
    if source.suffix.lower() == ".cbr":
        return _seven_zip_read(source, name)
    raise ValueError(f"Unsupported comic archive: {source.suffix}")


def _generate_nonvideo_thumbnail(source: Path, output: Path, width: int) -> bool:
    kind = media_kind(source)
    if kind == "image":
        with Image.open(source) as image:
            _save_pillow_jpeg(image, output, width)
        return True
    if kind == "pdf":
        with fitz.open(source) as document:
            if document.page_count < 1:
                return False
            page = document.load_page(0)
            scale = max(1.0, width / max(1.0, page.rect.width))
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            with Image.open(io.BytesIO(pix.tobytes("png"))) as image:
                _save_pillow_jpeg(image, output, width)
        return True
    if kind == "comic":
        data = _comic_image_bytes(source, 0)
        with Image.open(io.BytesIO(data)) as image:
            _save_pillow_jpeg(image, output, width)
        return True
    return False


def generate_image(source: Path, output: Path, width: int, generation: int, scheduler: ImageScheduler):
    if generation != scheduler.current_generation() or media_kind(source) == "video":
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(".tmp.jpg")
    try:
        if _generate_nonvideo_thumbnail(source, tmp, width) and generation == scheduler.current_generation():
            tmp.replace(output)
    except Exception as exc:
        scheduler.mark_failed(generation, output)
        app.logger.warning("Thumbnail generation failed for %s: %s", source, exc)
    finally:
        tmp.unlink(missing_ok=True)


def fmt_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return str(size)


def fmt_duration(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def stream_lists(info):
    videos, audios, subtitles = [], [], []
    for stream in info.get("streams", []):
        tags = stream.get("tags") or {}
        item = {
            "index": stream.get("index"),
            "codec": stream.get("codec_name") or "unknown",
            "language": tags.get("language") or "und",
            "title": tags.get("title") or "",
            "width": stream.get("width"),
            "height": stream.get("height"),
        }
        kind = stream.get("codec_type")
        if kind == "video":
            videos.append(item)
        elif kind == "audio":
            audios.append(item)
        elif kind == "subtitle":
            item["text_supported"] = item["codec"] in TEXT_SUBTITLE_CODECS
            subtitles.append(item)
    return videos, audios, subtitles


def direct_playback_mimetype(path: Path, info, audios) -> str | None:
    target = resolve_media_source(path)
    remote = isinstance(target, str)
    ext = Path(urlsplit(target).path).suffix.lower() if remote else target.suffix.lower()
    formats = set(info.get("format", {}).get("format_name", "").split(","))
    videos, _, _ = stream_lists(info)
    if not videos:
        return None
    v = videos[0]["codec"]
    a = audios[0]["codec"] if audios else None
    if remote and ("hls" in formats or ext == ".m3u8"):
        return "application/vnd.apple.mpegurl" if v == "h264" and a in {None, "aac", "mp3"} else None
    if ext in {".mp4", ".m4v", ".mov"} or (remote and formats & {"mov", "mp4"}):
        return "video/mp4" if v == "h264" and a in {None, "aac", "mp3"} else None
    if ext == ".webm" or (remote and formats == {"webm"}):
        return "video/webm" if v in {"vp8", "vp9", "av1"} and a in {None, "opus", "vorbis"} else None
    return None


def direct_playback_ok(path: Path, info, audios) -> bool:
    return direct_playback_mimetype(path, info, audios) is not None


def parent_relpath(relpath: str) -> str:
    parent = Path(relpath).parent
    return "" if str(parent) == "." else parent.as_posix()


def run_encoder_test(encoder: str, extra_before_input=None, video_filter=None) -> tuple[bool, str]:
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error"]
    if extra_before_input:
        cmd += extra_before_input
    cmd += ["-f", "lavfi", "-i", "color=size=640x360:rate=1", "-frames:v", "1"]
    if video_filter:
        cmd += ["-vf", video_filter]
    cmd += ["-c:v", encoder, "-f", "null", "-"]
    try:
        proc = run_media_command(cmd, capture_output=True, text=True, timeout=20)
        return proc.returncode == 0, (proc.stderr or "").strip()
    except Exception as exc:
        return False, str(exc)


def available_transcoder_tests() -> list[tuple[str, str, tuple[bool, str]]]:
    tests = []

    if os.name == "nt":
        tests.append(("qsv", "Intel QSV", run_encoder_test("h264_qsv", video_filter="format=nv12")))
        tests.append(("nvenc", "NVIDIA NVENC", run_encoder_test("h264_nvenc", video_filter="format=nv12")))
        tests.append(("amf", "AMD AMF", run_encoder_test("h264_amf", video_filter="format=nv12")))
    else:
        if Path(VAAPI_DEVICE).exists():
            tests.append((
                "vaapi",
                "VAAPI",
                run_encoder_test(
                    "h264_vaapi",
                    extra_before_input=["-vaapi_device", VAAPI_DEVICE],
                    video_filter="format=nv12,hwupload,scale_vaapi=format=nv12",
                ),
            ))
        tests.append(("nvenc", "NVIDIA NVENC", run_encoder_test("h264_nvenc", video_filter="format=nv12")))

    tests.append(("software", "Software libx264", run_encoder_test("libx264")))
    return tests


_transcoder_cache = None


def selected_transcoder(force_refresh: bool = False) -> dict:
    global _transcoder_cache
    if force_refresh:
        codec_encoder_test.cache_clear()
        ffmpeg_filters.cache_clear()
        tone_mapping_test.cache_clear()
    if _transcoder_cache is not None and not force_refresh:
        return _transcoder_cache

    tests = available_transcoder_tests()
    preference = TRANSCODER_PREFERENCE

    if preference != "auto":
        for key, label, result in tests:
            if key == preference:
                ok, detail = result
                _transcoder_cache = {
                    "key": key if ok else None,
                    "label": label if ok else "Unavailable",
                    "ok": ok,
                    "detail": detail,
                    "tests": [(k, l, r[0], r[1]) for k, l, r in tests],
                }
                return _transcoder_cache

    for key, label, (ok, detail) in tests:
        if ok:
            _transcoder_cache = {
                "key": key,
                "label": label,
                "ok": True,
                "detail": detail,
                "tests": [(k, l, r[0], r[1]) for k, l, r in tests],
            }
            return _transcoder_cache

    _transcoder_cache = {
        "key": None,
        "label": "No usable transcoder",
        "ok": False,
        "detail": "\n".join(f"{label}: {result[1]}" for _, label, result in tests),
        "tests": [(k, l, r[0], r[1]) for k, l, r in tests],
    }
    return _transcoder_cache


@app.before_request
def password_gate():
    if request.endpoint in {"login", "static"}:
        return

    if not authentication_required():
        # Passwordless LAN mode. No session is needed.
        return

    if not session.get("authenticated") or active_profile() is None:
        session.clear()
        return redirect(url_for("login", next=request.path))


# The first configured profile owns server administration. Restricted profiles
# must never receive the configuration or other profiles' passwords.
_config_lock = threading.Lock()
TRANSCODER_OPTIONS = ("auto", "vaapi", "qsv", "nvenc", "amf", "software")


def can_manage_settings() -> bool:
    if not authentication_required():
        return True
    profiles = load_profiles()
    return bool(profiles and session.get("authenticated") and
                session.get("profile_id") == profiles[0]["id"])


@app.context_processor
def settings_navigation():
    return {"can_manage_settings": can_manage_settings()}


def configuration_version() -> str:
    data = CONFIG_FILE.read_bytes() if CONFIG_FILE.exists() else b""
    return hashlib.sha256(data).hexdigest()


def editable_libraries() -> tuple[str, list[dict]]:
    public = []
    profiles = []
    current = public
    for raw in read_config_lines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("<") and line.endswith(">"):
            profiles.append({"original": str(len(profiles)), "password": line[1:-1], "paths": []})
            current = profiles[-1]["paths"]
        elif "=" not in line or line.startswith("-"):
            current.append(line)
    for profile in profiles:
        profile["paths"] = "\n".join(profile["paths"])
    return "\n".join(public), profiles


def validate_library(text: str, *, exclusions: bool) -> list[str]:
    lines = []
    has_root = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if any(ord(c) < 32 for c in line) or "=" in line or line.startswith(("#", "<")):
            raise ValueError("Enter one absolute folder path per line, without config directives.")
        if line.startswith("-"):
            if not exclusions or not has_root or not line[1:].strip():
                raise ValueError("Exclusions require a profile and must follow a media folder.")
        else:
            if not Path(line).expanduser().is_absolute():
                raise ValueError("Media folders must use absolute paths on the server.")
            has_root = True
        lines.append(line)
    return lines


def validate_password(value: str) -> None:
    if not value or value != value.strip() or any(c in value for c in "<>\r\n") or any(ord(c) < 32 for c in value):
        raise ValueError("Passwords must be nonempty, without angle brackets, line breaks or outer spaces.")


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    global SETTINGS, APP_PASSWORD, USE_MASTER_PASSWORD, TRANSCODER_PREFERENCE, _transcoder_cache
    if not can_manage_settings():
        abort(403)
    session.setdefault("settings_csrf", secrets.token_hex(32))
    error = None
    status = 200
    with _config_lock:
        values = load_settings()
        public_paths, profiles = editable_libraries()
        version = configuration_version()
        mode = "profiles" if profiles else ("master" if bool_setting(values["use_master_password"]) else "none")
        if request.method == "POST":
            if not secrets.compare_digest(session["settings_csrf"].encode(), request.form.get("csrf", "").encode()):
                abort(400)
            try:
                if request.form.get("version") != version:
                    raise ValueError("Configuration changed since this page was opened. Reload before saving.")
                port = request.form.get("port", "")
                if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
                    raise ValueError("Port must be a number between 1 and 65535.")
                transcoder = request.form.get("transcoder", "")
                if transcoder not in TRANSCODER_OPTIONS:
                    raise ValueError("Select a supported transcoder.")
                default_quality = request.form.get("default_quality", values["default_quality"])
                if default_quality not in QUALITY_PROFILES:
                    raise ValueError("Select a supported default quality.")
                mode = request.form.get("mode", "")
                if mode not in {"none", "master", "profiles"}:
                    raise ValueError("Select an access mode.")
                master = request.form.get("master_password") or values["master_password"]
                if master:
                    validate_password(master)
                if mode == "master":
                    validate_password(os.environ.get("LANSTREAM_PASSWORD", master))
                libraries = []
                if mode == "profiles":
                    passwords = set()
                    for row in request.form.getlist("profile_row"):
                        original = request.form.get(f"original_{row}", "")
                        password = request.form.get(f"password_{row}", "")
                        if not password and original.isdecimal() and int(original) < len(profiles):
                            password = profiles[int(original)]["password"]
                        validate_password(password)
                        if password in passwords:
                            raise ValueError("Each profile must have a different password.")
                        passwords.add(password)
                        libraries.append((password, validate_library(request.form.get(f"paths_{row}", ""), exclusions=True)))
                    if not libraries:
                        raise ValueError("Add at least one password profile.")
                else:
                    libraries.append((None, validate_library(request.form.get("public_paths", ""), exclusions=False)))
                # Preserve user comments and settings not owned by this form.
                retained = []
                for raw in read_config_lines():
                    line = raw.strip()
                    if line.startswith("#") or ("=" in line and not line.startswith(("-", "<")) and line.split("=", 1)[0].strip().lower() not in values):
                        retained.append(raw)
                updated = {**values, "port": str(int(port)), "use_master_password": str(mode == "master").lower(),
                           "master_password": master, "transcoder": transcoder, "default_quality": default_quality}
                content = retained + [f"{key}={value}" for key, value in updated.items()] + [""]
                for password, paths in libraries:
                    if password is not None:
                        content.append(f"<{password}>")
                    content.extend(paths)
                    content.append("")
                temporary = None
                try:
                    # Same-directory replacement leaves the old file intact on failure.
                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=CONFIG_FILE.parent, delete=False) as output:
                        temporary = output.name
                        output.write("\n".join(content))
                        output.flush()
                        os.fsync(output.fileno())
                    if CONFIG_FILE.exists():
                        os.chmod(temporary, CONFIG_FILE.stat().st_mode & 0o777)
                    os.replace(temporary, CONFIG_FILE)
                    # The rest of this request must see the newly saved profiles.
                    g.pop("filesystem_cache", None)
                finally:
                    if temporary and os.path.exists(temporary):
                        os.unlink(temporary)
                SETTINGS = updated
                APP_PASSWORD = os.environ.get("LANSTREAM_PASSWORD", updated["master_password"])
                USE_MASTER_PASSWORD = bool_setting(os.environ.get("LANSTREAM_USE_MASTER_PASSWORD", updated["use_master_password"]))
                TRANSCODER_PREFERENCE = os.environ.get("LANSTREAM_TRANSCODER", transcoder).strip().lower()
                _transcoder_cache = None
                session.clear()
                if authentication_required():
                    session["authenticated"] = True
                    session["profile_id"] = load_profiles()[0]["id"]
                session["settings_saved"] = True
                return redirect(url_for("settings_page"))
            except ValueError as exc:
                error, status = str(exc), 400
            except OSError:
                error, status = "Could not save config.txt. Check write permissions; in Docker, mount its parent directory writable rather than mounting only the file.", 500
            # Keep the original revision so a stale form cannot overwrite external edits.
            version = request.form.get("version", "")
            # Keep the user's edits on validation/save errors; never render passwords.
            values.update({key: request.form.get(key, values[key]) for key in ("port", "transcoder", "default_quality")})
            public_paths = request.form.get("public_paths", public_paths)
            profiles = [{"original": request.form.get(f"original_{row}", ""), "paths": request.form.get(f"paths_{row}", "")} for row in request.form.getlist("profile_row")]
    response = make_response(render_template(
        "settings.html", values=values, mode=mode, public_paths=public_paths,
        profiles=profiles, version=version, error=error,
        saved=session.pop("settings_saved", False), transcoders=TRANSCODER_OPTIONS,
        overrides=[name for name in ("LANSTREAM_PORT", "LANSTREAM_PASSWORD", "LANSTREAM_USE_MASTER_PASSWORD", "LANSTREAM_TRANSCODER") if name in os.environ],
        running_port=SERVER_PORT, quality_profiles=QUALITY_PROFILES,
    ), status)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/login", methods=["GET", "POST"])
def login():
    if not authentication_required():
        return redirect(url_for("home"))

    error = None
    if request.method == "POST":
        supplied = request.form.get("password", "")
        matched = None
        for profile in load_profiles():
            if secrets.compare_digest(supplied.encode("utf-8"), profile["password"].encode("utf-8")):
                matched = profile
                break

        if matched:
            session.clear()
            session["authenticated"] = True
            session["profile_id"] = matched["id"]
            dest = request.args.get("next") or url_for("home")
            if not dest.startswith("/"):
                dest = url_for("home")
            return redirect(dest)

        error = "Incorrect password."

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def home():
    image_scheduler.new_generation()
    roots = load_roots()
    items = [
        {
            "index": i,
            "label": root_label(p, i),
            "exists": p.is_dir(),
        }
        for i, p in enumerate(roots)
        if not path_is_excluded(p)
    ]
    items.sort(key=lambda item: item["label"].casefold())
    if can_manage_settings() and load_settings()["nzbdav_url"]:
        items.append({"index": NZBDAV_ROOT_INDEX, "label": "NZBDAV", "exists": True})
    return render_template("home.html", roots=items)


@app.route("/browse/<int:root_index>/", defaults={"relpath": ""})
@app.route("/browse/<int:root_index>/<path:relpath>")
def browse(root_index, relpath):
    generation = image_scheduler.new_generation()
    root = get_root(root_index)
    current = safe_path(root, relpath)
    if not current.is_dir():
        abort(404)

    folders: list[dict[str, Any]] = []
    media: dict[str, list[dict[str, Any]]] = {
        "video": [], "image": [], "pdf": [], "comic": [], "text": [],
    }
    try:
        if isinstance(current, WebDAVPath):
            entries = current.iterdir()
        else:
            with os.scandir(current) as directory:
                entries = list(directory)
    except OSError as exc:
        return render_template("error.html", message=f"Unable to read folder: {exc}"), 500

    for directory_entry in entries:
        try:
            is_directory = directory_entry.is_dir()
            kind = None if is_directory else media_kind_for_suffix(Path(directory_entry.name).suffix.lower())
            if not is_directory and (not kind or not directory_entry.is_file()):
                continue
            entry = directory_entry if isinstance(directory_entry, WebDAVPath) else Path(directory_entry.path)
            if isinstance(entry, WebDAVPath) and not is_directory and kind != 'video':
                continue
            if isinstance(entry, WebDAVPath) and entry.suffix.lower() == '.strm':
                continue
            if path_is_excluded(entry):
                continue
            if is_directory:
                folder_rel = entry.relative_to(root).as_posix()
                folders.append({
                    "name": entry.name,
                    "rel": folder_rel,
                })
                continue

            rel = entry.relative_to(root).as_posix()
            item: dict[str, Any] = {"name": entry.name, "rel": rel, "kind": kind}
            if kind not in {"text", "video"}:
                thumb = cache_path("thumbs", root_index, root, entry)
                item.update({
                    "thumb_ready": thumb.exists(),
                    "_source": entry,
                    "_thumb": thumb,
                })
            media[kind].append(item)
        except OSError:
            continue

    folders.sort(key=lambda x: natural_sort_key(x["name"]))
    for values in media.values():
        values.sort(key=lambda x: natural_sort_key(x["name"]))

    # Queue only the first visible-ish supported media items. Additional
    # thumbnails are requested lazily as cards approach the viewport.
    primed = 0
    for kind in ("image", "pdf", "comic"):
        for item in media[kind]:
            if primed >= 12:
                break
            if not item["thumb_ready"]:
                image_scheduler.submit(generation, 10, item["_source"], item["_thumb"], 360)
            primed += 1
        if primed >= 12:
            break

    for values in media.values():
        for item in values:
            item.pop("_source", None)
            item.pop("_thumb", None)

    crumbs = [{"name": root_label(root, root_index), "url": url_for("browse", root_index=root_index)}]
    acc = Path()
    for part in Path(relpath).parts:
        acc /= part
        crumbs.append({"name": part, "url": url_for("browse", root_index=root_index, relpath=acc.as_posix())})

    if relpath:
        parent = Path(relpath).parent
        up_rel = "" if str(parent) == "." else parent.as_posix()
        up_url = url_for("browse", root_index=root_index, relpath=up_rel)
    else:
        up_url = url_for("home")

    return render_template(
        "browse.html",
        root_index=root_index,
        folders=folders,
        videos=media["video"],
        images=media["image"],
        pdfs=media["pdf"],
        comics=media["comic"],
        texts=media["text"],
        crumbs=crumbs,
        generation=generation,
        up_url=up_url,
    )


@app.route("/image/<int:root_index>/<path:relpath>")
def image_viewer(root_index, relpath):
    image_scheduler.new_generation()
    root = get_root(root_index)
    source = safe_path(root, relpath)
    if media_kind(source) != "image":
        abort(404)
    previous_rel, next_rel = image_siblings(root, source)
    return render_template(
        "image_viewer.html",
        root_index=root_index,
        relpath=relpath,
        filename=source.name,
        parent_rel=parent_relpath(relpath),
        previous_rel=previous_rel,
        next_rel=next_rel,
    )


@app.route("/api/image-navigation/<int:root_index>/<path:relpath>")
def image_navigation(root_index, relpath):
    root = get_root(root_index)
    source = safe_path(root, relpath)
    if media_kind(source) != "image":
        abort(404)
    previous_rel, next_rel = image_siblings(root, source)
    return jsonify({
        "relpath": relpath,
        "filename": source.name,
        "image_url": url_for(
            "image_file",
            root_index=root_index,
            relpath=relpath,
        ),
        "viewer_url": url_for(
            "image_viewer",
            root_index=root_index,
            relpath=relpath,
        ),
        "previous": (
            {
                "relpath": previous_rel,
                "viewer_url": url_for(
                    "image_viewer",
                    root_index=root_index,
                    relpath=previous_rel,
                ),
            }
            if previous_rel else None
        ),
        "next": (
            {
                "relpath": next_rel,
                "viewer_url": url_for(
                    "image_viewer",
                    root_index=root_index,
                    relpath=next_rel,
                ),
            }
            if next_rel else None
        ),
    })


@app.route("/image-file/<int:root_index>/<path:relpath>")
def image_file(root_index, relpath):
    root = get_root(root_index)
    source = safe_path(root, relpath)
    if media_kind(source) != "image":
        abort(404)

    # Most configured image formats can be sent directly to modern browsers.
    # TIFF is converted lazily because browser support is inconsistent.
    if source.suffix.lower() in {".tif", ".tiff"}:
        output = cache_path("image-view", root_index, root, source)
        if not output.exists():
            output.parent.mkdir(parents=True, exist_ok=True)
            tmp = output.with_suffix(".tmp.jpg")
            try:
                with Image.open(source) as image:
                    _save_pillow_jpeg(image, tmp, 2400)
                tmp.replace(output)
            except Exception:
                tmp.unlink(missing_ok=True)
                abort(415)
        return send_file(output, mimetype="image/jpeg", conditional=True, max_age=86400)

    mimetype = mimetypes.guess_type(str(source))[0] or "application/octet-stream"
    return send_file(source, mimetype=mimetype, conditional=True, max_age=3600)


def _page_cache_path(kind: str, root_index: int, root: Path, source: Path, page: int) -> Path:
    base = cache_path(kind, root_index, root, source)
    return base.parent / f"{base.stem}.page-{page + 1:05d}.jpg"


def _render_pdf_page(source: Path, page: int, output: Path, width: int = 1600) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(".tmp.jpg")
    try:
        with fitz.open(source) as document:
            if page < 0 or page >= document.page_count:
                raise IndexError(page)
            p = document.load_page(page)
            scale = max(1.0, width / max(1.0, p.rect.width))
            pix = p.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            pix.save(str(tmp))
        tmp.replace(output)
    finally:
        tmp.unlink(missing_ok=True)


def _render_comic_page(source: Path, page: int, output: Path, width: int = 1800) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(".tmp.jpg")
    try:
        data = _comic_image_bytes(source, page)
        with Image.open(io.BytesIO(data)) as image:
            _save_pillow_jpeg(image, tmp, width)
        tmp.replace(output)
    finally:
        tmp.unlink(missing_ok=True)


@app.route("/document/<int:root_index>/<path:relpath>")
def document_viewer(root_index, relpath):
    image_scheduler.new_generation()
    root = get_root(root_index)
    source = safe_path(root, relpath)
    kind = media_kind(source)
    if kind not in {"pdf", "comic"}:
        abort(404)
    try:
        if kind == "pdf":
            with fitz.open(source) as document:
                page_count = document.page_count
        else:
            page_count = len(_comic_names(source))
    except Exception as exc:
        return render_template("error.html", message=f"Unable to open document: {exc}"), 500
    if page_count < 1:
        return render_template("error.html", message="This document has no viewable pages."), 415
    return render_template(
        "document_viewer.html",
        root_index=root_index,
        relpath=relpath,
        filename=source.name,
        parent_rel=parent_relpath(relpath),
        page_count=page_count,
        kind=kind,
    )


@app.route("/document-page/<int:root_index>/<path:relpath>")
def document_page(root_index, relpath):
    root = get_root(root_index)
    source = safe_path(root, relpath)
    kind = media_kind(source)
    if kind not in {"pdf", "comic"}:
        abort(404)
    try:
        page = int(request.args.get("page", "0"))
    except ValueError:
        abort(400)
    if page < 0:
        abort(404)
    output = _page_cache_path("document-pages", root_index, root, source, page)
    if not output.exists():
        try:
            if kind == "pdf":
                _render_pdf_page(source, page, output)
            else:
                _render_comic_page(source, page, output)
        except (IndexError, zipfile.BadZipFile, fitz.FileDataError):
            abort(404)
        except Exception as exc:
            app.logger.warning("Document page render failed for %s page %s: %s", source, page, exc)
            abort(500)
    return send_file(output, mimetype="image/jpeg", conditional=True, max_age=86400)


@app.route("/text/<int:root_index>/<path:relpath>")
def text_viewer(root_index, relpath):
    image_scheduler.new_generation()
    root = get_root(root_index)
    source = safe_path(root, relpath)
    if media_kind(source) != "text":
        abort(404)
    try:
        raw = source.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return render_template("error.html", message=f"Unable to read text file: {exc}"), 500

    return render_template(
        "text_viewer.html",
        root_index=root_index,
        relpath=relpath,
        filename=source.name,
        parent_rel=parent_relpath(relpath),
        text=raw,
    )


@app.route("/video/<int:root_index>/<path:relpath>")
def video_detail(root_index, relpath):
    root = get_root(root_index)
    source = safe_path(root, relpath)
    if not is_video(source):
        abort(404)

    info = probe(source)
    videos, audios, subtitles = stream_lists(info)
    v = videos[0] if videos else {}
    size = source.stat().st_size
    if source.suffix.lower() == ".strm":
        size = info.get("format", {}).get("size")
        try:
            size = int(size) if size is not None else None
        except (ValueError, TypeError):
            size = None

    return render_template(
        "video.html",
        root_index=root_index, relpath=relpath, filename=source.name,
        parent_rel=parent_relpath(relpath),
        size=fmt_size(size) if size is not None else "Unknown",
        duration_text=fmt_duration(duration_seconds(info)) if duration_seconds(info) else "Live / unknown",
        width=v.get("width"), height=v.get("height"),
        video_codec=v.get("codec", "unknown"),
        audios=audios, subtitles=subtitles,
        native=direct_playback_ok(source, info, audios),
        quality_profiles=QUALITY_PROFILES, default_quality=load_settings().get("default_quality", "auto"),
    )


def playback_media_info(info: dict) -> dict:
    streams = info.get("streams", [])
    return {
        "container": info.get("format", {}).get("format_name", "unknown"),
        "bit_rate": info.get("format", {}).get("bit_rate"),
        "video": next((stream for stream in streams if stream.get("codec_type") == "video"), {}),
        "audios": [stream for stream in streams if stream.get("codec_type") == "audio"],
    }


@app.route("/play/<int:root_index>/<path:relpath>")
def player(root_index, relpath):
    image_scheduler.new_generation()
    root = get_root(root_index)
    source = safe_path(root, relpath)
    if not is_video(source):
        abort(404)
    info = probe(source)
    _, audios, subtitles = stream_lists(info)
    session.setdefault("playback_owner", secrets.token_hex(16))
    d = duration_seconds(info)
    return render_template(
        "player.html", root_index=root_index, relpath=relpath,
        filename=source.name, duration=d, duration_text=fmt_duration(d),
        audios=audios, subtitles=subtitles, media_info=playback_media_info(info),
        native=direct_playback_ok(source, info, audios),
        native_mimetype=direct_playback_mimetype(source, info, audios),
        quality_profiles=QUALITY_PROFILES,
        default_quality=request.args.get("quality") if request.args.get("quality") in QUALITY_PROFILES else load_settings().get("default_quality", "auto"),
    )


def parse_range_header(range_header: str | None, file_size: int) -> tuple[int, int] | None:
    """Return inclusive start/end for a single HTTP byte range."""
    if file_size <= 0 or not range_header or not range_header.startswith("bytes="):
        return None

    value = range_header[6:].strip()
    # Browsers normally request one range. Keep the implementation deliberately
    # small and reject multipart ranges.
    if "," in value:
        return None

    try:
        start_text, end_text = value.split("-", 1)

        if start_text == "":
            # Suffix range, e.g. bytes=-500
            suffix = int(end_text)
            if suffix <= 0:
                return None
            suffix = min(suffix, file_size)
            return file_size - suffix, file_size - 1

        start = int(start_text)
        if start < 0 or start >= file_size:
            return None

        if end_text == "":
            end = file_size - 1
        else:
            end = min(int(end_text), file_size - 1)

        if end < start:
            return None

        return start, end
    except (ValueError, TypeError):
        return None


def ranged_file_response(source: Path, mimetype: str, *, tracked_source: Path | None = None):
    file_size = source.stat().st_size
    requested = request.headers.get("Range")

    # Native playback startup may begin with a normal GET. Stream it in chunks
    # rather than handing the entire file to Flask/Werkzeug.
    if not requested:
        stream_id = stream_tracker.start(
            "direct",
            tracked_source or source,
            detail=f"full file, {file_size} bytes",
        )

        def whole_file():
            try:
                with source.open("rb", buffering=0) as fh:
                    while not media_runtime.stopping.is_set():
                        chunk = fh.read(1024 * 1024)
                        if not chunk:
                            break
                        stream_tracker.add_bytes(stream_id, len(chunk))
                        yield chunk
            finally:
                stream_tracker.stop(stream_id)

        response = Response(whole_file(), status=200, mimetype=mimetype)
        response.call_on_close(lambda: stream_tracker.stop(stream_id))
        response.headers["Content-Length"] = str(file_size)
        response.headers["Accept-Ranges"] = "bytes"
        response.headers["Cache-Control"] = "private, max-age=0"
        return response

    byte_range = parse_range_header(requested, file_size)
    if byte_range is None:
        response = Response(status=416)
        response.headers["Content-Range"] = f"bytes */{file_size}"
        response.headers["Accept-Ranges"] = "bytes"
        return response

    start, end = byte_range
    length = end - start + 1

    app.logger.info(
        "Native range request: %s bytes %d-%d/%d",
        source.name, start, end, file_size
    )

    stream_id = stream_tracker.start(
        "direct",
        tracked_source or source,
        detail=f"bytes {start}-{end}/{file_size}",
    )

    def partial_file():
        remaining = length
        try:
            with source.open("rb", buffering=0) as fh:
                # Jump directly to the requested offset, which is important
                # for NAS-backed files.
                fh.seek(start, os.SEEK_SET)
                while remaining > 0 and not media_runtime.stopping.is_set():
                    chunk = fh.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    stream_tracker.add_bytes(stream_id, len(chunk))
                    yield chunk
        finally:
            stream_tracker.stop(stream_id)

    response = Response(
        partial_file(),
        status=206,
        mimetype=mimetype,
    )
    response.call_on_close(lambda: stream_tracker.stop(stream_id))
    response.headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    response.headers["Content-Length"] = str(length)
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Cache-Control"] = "private, max-age=0"
    return response


def remote_file_response(source: Path, target: str):
    """Relay the original bytes and ranges without invoking FFmpeg."""
    headers = {"Accept-Encoding": "identity"}
    for name in ("Range", "If-Range"):
        if request.headers.get(name):
            headers[name] = request.headers[name]
    try:
        upstream = urlopen(Request(target, headers=headers), timeout=30)
    except HTTPError as exc:
        content_range = exc.headers.get("Content-Range")
        exc.close()
        if exc.code == 416:
            response = Response(status=416)
            if content_range:
                response.headers["Content-Range"] = content_range
            return response
        abort(502, description="Unable to open the STRM media source.")
    except (TimeoutError, URLError):
        abort(502, description="Unable to open the STRM media source.")

    content_type = upstream.headers.get("Content-Type", "")
    if Path(urlsplit(target).path).suffix.lower() == ".m3u8" or "mpegurl" in content_type.lower():
        # Let native HLS players resolve playlists, segments and keys relative
        # to the upstream URL rather than the LANStream /media route.
        location = upstream.geturl()
        upstream.close()
        response = redirect(location)
        response.headers["Cache-Control"] = "no-store"
        return response

    stream_id = stream_tracker.start("direct", source, detail="Original remote stream · no conversion")
    closed = False

    def close():
        nonlocal closed
        if not closed:
            closed = True
            upstream.close()
            stream_tracker.stop(stream_id)
            media_runtime.remove_cleanup(stream_id)

    def abort_upstream():
        # Interrupt a read in another request thread before closing the buffered
        # HTTP response, whose close() otherwise waits for that read to finish.
        try:
            upstream.fp.raw._sock.shutdown(socket.SHUT_RDWR)
        except (AttributeError, OSError):
            pass
        close()

    media_runtime.add_cleanup(stream_id, abort_upstream)

    def generate():
        try:
            while not media_runtime.stopping.is_set():
                chunk = upstream.read(256 * 1024)
                if not chunk:
                    break
                stream_tracker.add_bytes(stream_id, len(chunk))
                yield chunk
        finally:
            close()

    response = Response(generate(), status=upstream.status,
                        content_type=content_type or "application/octet-stream")
    for name in ("Content-Length", "Content-Range", "Accept-Ranges", "Content-Encoding", "ETag", "Last-Modified"):
        if upstream.headers.get(name):
            response.headers[name] = upstream.headers[name]
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Accel-Buffering"] = "no"
    response.call_on_close(close)
    return response


@app.route("/media/<int:root_index>/<path:relpath>")
def media_file(root_index, relpath):
    root = get_root(root_index)
    source = safe_path(root, relpath)
    if not is_video(source):
        abort(404)

    target = resolve_media_source(source)
    if isinstance(target, WebDAVPath):
        return cached_remote_response(target)
    if isinstance(target, str):
        return remote_file_response(source, target)
    mimetype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    return ranged_file_response(target, mimetype, tracked_source=source)


def cached_remote_response(source):
    service = source.service
    resource = service.client.stat(source.path)
    requested = request.headers.get('Range')
    interval = parse_range_header(requested, resource.size) if requested else (0, resource.size - 1)
    if interval is None:
        return Response(status=416, headers={'Content-Range': f'bytes */{resource.size}'})
    start, end = interval
    headers = {'Content-Length': str(max(0, end - start + 1)), 'Accept-Ranges': 'bytes',
               'Cache-Control': 'no-store'}
    if requested:
        headers['Content-Range'] = f'bytes {start}-{end}/{resource.size}'
    if request.method == 'HEAD':
        return Response(status=206 if requested else 200, headers=headers,
                        mimetype=mimetypes.guess_type(source.path)[0] or 'application/octet-stream')
    stream_id = stream_tracker.start('direct', source, detail='NZBDAV · shared RAM cache')

    def generate():
        try:
            for chunk in service.chunks(resource, start, end):
                if media_runtime.stopping.is_set():
                    break
                stream_tracker.add_bytes(stream_id, len(chunk))
                yield bytes(chunk)
        finally:
            stream_tracker.stop(stream_id)

    response = Response(generate(), status=206 if requested else 200, headers=headers,
                        mimetype=mimetypes.guess_type(source.path)[0] or 'application/octet-stream')
    response.call_on_close(lambda: stream_tracker.stop(stream_id))
    return response


def best_cached_image(root_index: int, root: Path, source: Path, requested_kind: str) -> Path | None:
    requested = cache_path(requested_kind, root_index, root, source)
    return requested if requested.exists() else None


@app.route("/thumb/<int:root_index>/<path:relpath>")
def thumbnail(root_index, relpath):
    root = get_root(root_index)
    source = safe_path(root, relpath)
    if media_kind(source) not in {"image", "pdf", "comic"}:
        abort(404)
    out = best_cached_image(root_index, root, source, "thumbs")
    if not out:
        abort(404)
    return send_file(out, mimetype="image/jpeg", conditional=True, max_age=86400)


@app.route("/api/image-ready/<kind>/<int:root_index>/<path:relpath>")
def image_ready(kind, root_index, relpath):
    if kind != "thumbs":
        abort(404)
    try:
        generation = int(request.args.get("generation", "0"))
    except ValueError:
        abort(400)

    root = get_root(root_index)
    source = safe_path(root, relpath)
    source_kind = media_kind(source)
    if source_kind not in {"image", "pdf", "comic"}:
        abort(404)
    found = best_cached_image(root_index, root, source, "thumbs")
    out = cache_path("thumbs", root_index, root, source)
    width, priority = 360, 10

    current = generation == image_scheduler.current_generation()
    failed = image_scheduler.failed(generation, out) if current else False
    if not found and current and not failed:
        image_scheduler.submit(generation, priority, source, out, width)
    return jsonify({
        "ready": bool(found),
        "current": current,
        "failed": failed,
    })


# Fixed targets make quality choices consistent across software and GPU encoders.
QUALITY_PROFILES = {
    "auto": {"label": "Auto · original when possible", "width": 0, "height": 0, "video_kbps": 0, "audio_kbps": 160},
    "source": {"label": "Original · no transcoding", "width": 0, "height": 0, "video_kbps": 0, "audio_kbps": 0},
    "transcode": {"label": "Source resolution · transcode", "width": 0, "height": 0, "video_kbps": 0, "audio_kbps": 192},
    "2160p": {"label": "4K · 20 Mbps", "width": 3840, "height": 2160, "video_kbps": 20000, "audio_kbps": 192},
    "1440p": {"label": "1440p · 12 Mbps", "width": 2560, "height": 1440, "video_kbps": 12000, "audio_kbps": 192},
    "1080p": {"label": "1080p · 8 Mbps", "width": 1920, "height": 1080, "video_kbps": 8000, "audio_kbps": 192},
    "720p": {"label": "720p · 4 Mbps", "width": 1280, "height": 720, "video_kbps": 4000, "audio_kbps": 128},
    "480p": {"label": "480p · 1.5 Mbps", "width": 854, "height": 480, "video_kbps": 1500, "audio_kbps": 96},
    "360p": {"label": "360p · 700 Kbps", "width": 640, "height": 360, "video_kbps": 700, "audio_kbps": 96},
}


def requested_quality() -> dict:
    # Preserve links made by older players, including the previously ignored low flag.
    key = request.args.get("quality") or ("480p" if request.args.get("low") == "1" else "auto")
    if key == "normal":
        key = "auto"
    if key in QUALITY_PROFILES:
        return dict(QUALITY_PROFILES[key])
    if key != "custom":
        abort(400, description="Unknown quality profile")
    try:
        height = int(request.args.get("height", "720"))
        bitrate = int(request.args.get("bitrate", "4000"))
        audio = int(request.args.get("audio_bitrate", "128"))
    except ValueError:
        abort(400, description="Invalid custom quality")
    if height not in {240, 360, 480, 720, 1080, 1440, 2160} or not 300 <= bitrate <= 40000 or audio not in {96, 128, 160, 192, 256}:
        abort(400, description="Custom quality is outside the supported range")
    width = {240: 426, 360: 640, 480: 854, 720: 1280, 1080: 1920, 1440: 2560, 2160: 3840}[height]
    return {"label": f"Custom · {height}p · {bitrate / 1000:g} Mbps", "width": width,
            "height": height, "video_kbps": bitrate, "audio_kbps": audio}


VIDEO_CODECS = {
    "h264": {"label": "H.264 / AVC", "browser_codec": "avc1.42E01E"},
    "hevc": {"label": "H.265 / HEVC", "browser_codec": "hvc1.1.6.L93.B0"},
    "av1": {"label": "AV1", "browser_codec": "av01.0.08M.08"},
}
AUDIO_CODECS = {
    "aac": {"label": "AAC", "encoder": "aac", "browser_codec": "mp4a.40.2"},
    "opus": {"label": "Opus", "encoder": "libopus", "browser_codec": "Opus"},
    "mp3": {"label": "MP3", "encoder": "libmp3lame", "browser_codec": "mp4a.6B"},
}
VIDEO_ENCODERS = {
    "software": {"h264": "libx264", "hevc": "libx265", "av1": "libsvtav1"},
    **{backend: {codec: f"{codec}_{backend}" for codec in VIDEO_CODECS}
       for backend in ("vaapi", "qsv", "nvenc", "amf")},
}


def requested_codecs() -> tuple[str, str]:
    video = request.args.get("video_codec", "auto")
    audio = request.args.get("audio_codec", "auto")
    if video not in {"auto", *VIDEO_CODECS} or audio not in {"auto", *AUDIO_CODECS}:
        abort(400, description="Unknown video or audio codec")
    return ("h264" if video == "auto" else video, "aac" if audio == "auto" else audio)


def playback_input_args(source: Path | str, start: float) -> list[str]:
    # The player already has duration from FFprobe. MPEG-PS/TS duration
    # estimation otherwise reads the tail and seeks back on every new encoder.
    args = ["-skip_estimate_duration_from_pts", "1"]
    # Even -ss 0 triggers an input seek after stream analysis. Keep the
    # demuxer's buffered packets at startup, particularly on uncached FUSE.
    if start > 0:
        args += ["-ss", f"{start:.3f}"]
    return args + ["-i", media_input_target(source)]


def transcode_command(
    backend: str,
    source: Path | str,
    audio_index: int | None,
    start: float,
    quality: dict | None = None,
    video_codec: str = "h264",
    audio_codec: str = "aac",
    source_video: dict | None = None,
    tone_mapper: str | None = None,
) -> list[str]:
    quality = quality or QUALITY_PROFILES["auto"]
    if quality == QUALITY_PROFILES["source"]:
        raise ValueError("Original quality must not be transcoded")
    limited = bool(quality["video_kbps"])
    tone_mapper = tone_mapper if tone_mapper is not None else load_settings()['tone_mapping']
    mapping = tone_mapping_plan(source_video, backend, tone_mapper)
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if backend == "vaapi":
        if mapping.active:
            # A fully specified hardware graph needs no implicit CPU converter.
            cmd += ['-noauto_conversion_filters']
        # Share one device between decoder, VPP and encoder. Keep decoded
        # surfaces on the GPU; software frames must never enter this graph.
        cmd += ["-init_hw_device", f"vaapi=lanstream:{VAAPI_DEVICE}"]
        if mapping.vulkan:
            # Derive Vulkan from the selected VAAPI adapter, never a software
            # Vulkan renderer or an unrelated default GPU.
            cmd += ['-init_hw_device', 'vulkan=lanstream_vk@lanstream']
        cmd += ["-filter_hw_device", "lanstream_vk" if mapping.vulkan else "lanstream",
                "-hwaccel", "vaapi", "-hwaccel_device", "lanstream",
                "-hwaccel_output_format", "vaapi"]
    cmd += playback_input_args(source, start) + ["-map", "0:v:0"]
    cmd += ["-map", f"0:{audio_index}"] if audio_index is not None else ["-an"]
    filters = list(mapping.filters)
    scale = ""
    if quality["width"]:
        # Bound both dimensions, preserve aspect ratio and never enlarge the source.
        scale = f"w='min(iw,{quality['width']})':h='min(ih,{quality['height']})':force_original_aspect_ratio=decrease:force_divisible_by=2"
    if backend == "vaapi":
        # VPP also converts Main 10 / P010 input to the encoder's NV12 format.
        filters.append("scale_vaapi=" + (scale + ":" if scale else "") + "format=nv12" + (":out_range=tv" if mapping.active else ""))
    else:
        if scale:
            filters.append("scale=" + scale)
        filters.append("format=yuv420p" if backend == "software" else "format=nv12")
    if mapping.active:
        # Metadata-only filter: does not download hardware surfaces. Remove
        # stale mastering/light/Dolby side data after the actual color transform.
        filters.append('sidedata=mode=delete')
    cmd += ["-vf", ",".join(filters)]
    if mapping.active:
        cmd += ['-color_primaries', 'bt709', '-color_trc', 'bt709',
                '-colorspace', 'bt709', '-color_range', 'tv', '-map_metadata', '-1']
    encoder = VIDEO_ENCODERS[backend][video_codec]
    options = {
        "vaapi": ["-c:v", "h264_vaapi"] + (["-rc_mode", "VBR"] if limited else ["-qp", "24"]),
        "qsv": ["-c:v", "h264_qsv", "-preset", "veryfast", "-look_ahead", "0"] + ([] if limited else ["-global_quality", "24"]),
        "nvenc": ["-c:v", "h264_nvenc", "-preset", "p3", "-tune", "ll", "-rc", "vbr"] + ([] if limited else ["-cq", "23", "-b:v", "0"]),
        "amf": ["-c:v", "h264_amf", "-quality", "speed"] + (["-rc", "vbr_peak"] if limited else ["-rc", "cqp", "-qp_i", "24", "-qp_p", "24"]),
        "software": ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency"] + ([] if limited else ["-crf", "23"]),
    }
    video_options = options[backend]
    video_options[video_options.index("-c:v") + 1] = encoder
    if backend == "software" and video_codec == "hevc":
        video_options = ["-c:v", encoder, "-preset", "veryfast", "-tune", "zerolatency", "-x265-params", "pools=2:log-level=error"] + ([] if limited else ["-crf", "26"])
    elif backend == "software" and video_codec == "av1":
        video_options = ["-c:v", encoder, "-preset", "8", "-svtav1-params", "lp=2", "-crf", "32"]
    cmd += video_options
    if video_codec == "hevc":
        cmd += ["-tag:v", "hvc1"]
    if limited:
        bitrate = quality["video_kbps"]
        cmd += ["-b:v", "0" if backend == "software" and video_codec == "av1" else f"{bitrate}k", "-maxrate", f"{bitrate}k", "-bufsize", f"{bitrate * 2}k"]
    if video_codec != "av1":
        cmd += ["-bf", "0"]
    cmd += ["-g", "48"]
    if audio_index is not None:
        if backend == 'vaapi' and mapping.active:
            # -noauto_conversion_filters applies to audio as well as video.
            # Explicitly downmix/resample, including encoder sample format,
            # so multichannel/Atmos layouts do not block the HDR video graph.
            sample_format = {'aac': 'fltp', 'opus': 'flt', 'mp3': 'fltp'}[audio_codec]
            cmd += ['-af', f'aresample=48000:ochl=stereo:osf={sample_format}']
        cmd += ["-c:a", AUDIO_CODECS[audio_codec]["encoder"], "-b:a", f"{quality['audio_kbps']}k", "-ac", "2", "-ar", "48000"]
    cmd += ["-movflags", "+frag_keyframe+empty_moov+default_base_moof+omit_tfhd_offset",
            "-frag_duration", "500000", "-flush_packets", "1", "-f", "mp4", "pipe:1"]
    return cmd


@lru_cache(maxsize=1)
def ffmpeg_filters() -> frozenset[str]:
    result = run_media_command([FFMPEG, '-hide_banner', '-filters'], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise ToneMappingError('Unable to inspect FFmpeg tone-mapping filters.')
    # FFmpeg releases differ in their flag-column width (e.g. "..." vs "..").
    # Recognize actual filter rows by their media signature, not a fixed width.
    listing = f'{result.stdout or ""}\n{result.stderr or ""}'
    return frozenset(re.findall(
        r'^\s*[A-Z.]+\s+(\w+)\s+(?:[AVNS]+|\|)->(?:[AVNS]+|\|)(?:\s|$)',
        listing, re.MULTILINE))


@lru_cache(maxsize=16)
def tone_mapping_test(backend: str, tone_mapper: str = 'vaapi') -> tuple[bool, str]:
    """Exercise the HDR pipeline once per backend using a bundled RAM fixture."""
    try:
        plan = tone_mapping_plan(HDR10_VIDEO, backend, tone_mapper)
        missing = plan.required - ffmpeg_filters()
        if missing:
            return False, 'FFmpeg is missing tone-mapping filters: ' + ', '.join(sorted(missing))
        command = transcode_command(backend, Path('hdr-test'), None, 0, source_video=HDR10_VIDEO, tone_mapper=tone_mapper)
        if plan.vulkan:
            # Device/library initialization failures may return EINVAL without
            # an error-level explanation. Keep version/extension context for
            # this small, cached RAM-only test; movie playback stays quiet.
            command[command.index('-loglevel') + 1] = 'verbose'
        before, after = command.index('-skip_estimate_duration_from_pts'), command.index('-map')
        command[before:after] = ['-f', 'hevc', '-i', 'pipe:0', '-frames:v', '1']
        result = run_media_command(command, input=HDR10_PROBE, capture_output=True, timeout=20)
        if result.returncode == 0 and result.stdout:
            return True, ''
        diagnostic = (result.stderr or b'').decode('utf-8', 'replace')
        if plan.vulkan:
            # Retain the failure plus Vulkan/libplacebo initialization context,
            # rather than allowing long extension lists to hide the cause.
            diagnostic = vulkan_playback_error(diagnostic)
        return False, safe_playback_error(diagnostic)
    except (OSError, subprocess.TimeoutExpired, ToneMappingError) as error:
        return False, str(error)


@lru_cache(maxsize=32)
def codec_encoder_test(backend: str, codec: str, audio: bool = False) -> tuple[bool, str]:
    """Probe the actual encoder and fragmented MP4 muxer, once per codec/backend."""
    if audio:
        command = [FFMPEG, "-v", "error", "-nostdin", "-f", "lavfi", "-i", "sine=sample_rate=48000",
                   "-t", "0.1", "-c:a", AUDIO_CODECS[codec]["encoder"], "-b:a", "128k", "-ac", "2",
                   "-movflags", "+frag_keyframe+empty_moov", "-f", "mp4", "pipe:1"]
    else:
        command = transcode_command(backend, Path("codec-test"), None, 0, QUALITY_PROFILES["480p"], codec)
        before = command.index("-hwaccel" if backend == "vaapi" else "-skip_estimate_duration_from_pts")
        after = command.index("-map")
        command[before:after] = ["-f", "lavfi", "-i", "color=size=320x180:rate=24", "-frames:v", "2"]
        if backend == "vaapi":
            # Lavfi produces raw CPU frames, not a compressed video to decode.
            # Only the synthetic encoder capability test needs an upload.
            vf = command.index("-vf") + 1
            command[vf] = "format=nv12,hwupload," + command[vf]
    try:
        result = run_media_command(command, capture_output=True, timeout=15)
        return result.returncode == 0, "" if result.returncode == 0 else result.stderr.decode("utf-8", "replace")[-1500:]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


BACKEND_LABELS = {"software": "Software", "vaapi": "VAAPI", "qsv": "Intel QSV", "nvenc": "NVIDIA NVENC", "amf": "AMD AMF"}


def codec_transcoder(transcoder: dict, codec: str) -> tuple[str | None, str]:
    if not transcoder["ok"]:
        return None, "No usable transcoder"
    preferred = transcoder["key"]
    if codec == "h264":
        return preferred, ""
    ok, detail = codec_encoder_test(preferred, codec)
    if ok:
        return preferred, ""
    if preferred == "vaapi":
        return None, f"{VIDEO_CODECS[codec]['label']} is unavailable on VAAPI; software fallback is disabled. {detail}"
    if preferred != "software":
        ok, detail = codec_encoder_test("software", codec)
        if ok:
            return "software", f"{VIDEO_CODECS[codec]['label']} is unavailable on the selected GPU; using software."
    return None, detail


@app.route("/api/transcode-codecs")
def transcode_codecs():
    transcoder = selected_transcoder()
    videos = []
    for codec, metadata in VIDEO_CODECS.items():
        backend, detail = codec_transcoder(transcoder, codec)
        encoder = VIDEO_ENCODERS[backend][codec] if backend else None
        videos.append({"id": codec, **metadata, "available": bool(backend), "backend": backend,
                       "encoder": encoder, "detail": detail})
    audios = []
    for codec, metadata in AUDIO_CODECS.items():
        available, detail = codec_encoder_test("software", codec, True)
        audios.append({"id": codec, **metadata, "available": available, "detail": detail})
    response = jsonify({"video": videos, "audio": audios})
    response.headers["Cache-Control"] = "no-store"
    return response


def safe_playback_error(message: str) -> str:
    # Encoder errors can echo internal gateway URLs, tokens or STRM credentials.
    text = re.sub(r'https?://[^\s\'"<>]+', '[media source]', str(message), flags=re.IGNORECASE)
    lines = []
    for line in text.splitlines():
        # Format-negotiation dumps can list hundreds of pixel formats. Retain
        # the leading error and link names instead of showing only that list.
        if len(line) > 240 and re.match(r'^\s*(?:\[[^]]+\]\s*)?(?:src|dst):', line):
            line = line[:200] + ' … [format list shortened]'
        lines.append(line)
    text = '\n'.join(lines).strip()
    return text if len(text) <= 2000 else text[:1300] + '\n…\n' + text[-600:]


def vulkan_playback_error(message: str) -> str:
    """Summarize a synthetic test, preserving library and GPU evidence."""
    lines = message.splitlines()
    context = []
    for line in lines:
        if re.search(r'libplacebo|Using device:|Device name:|Driver (?:name|info):|Using device extension VK_KHR_internally_synchronized_queues', line, re.I):
            context.append(line)
    # A source-level compatibility guard in FFmpeg 9 returns EINVAL silently
    # for flagged queues when built with libplacebo API <365. Only identify it
    # when both the library API and enabled extension are present in the log.
    api = re.search(r'libplacebo[^\n]*API\s+v?(\d+)', message, re.I)
    extension_enabled = 'Using device extension VK_KHR_internally_synchronized_queues' in message
    hint = ''
    if api and int(api.group(1)) < 365 and extension_enabled:
        hint = ('Vulkan queue compatibility: this libplacebo API cannot import internally synchronized queues. '
                'Use an FFmpeg build linked against libplacebo API 365 or newer, '
                'or restore tone_mapping=vaapi.\n')
    return hint + '\n'.join(context[:8]) + '\n' + '\n'.join(lines[-12:])


def rejected_playback(source, backend: str, message: str, status: int):
    detail = safe_playback_error(message)
    app.logger.error('Playback rejected (%s, %s): %s', backend, status, detail)
    stream_id = stream_tracker.start(backend, source, detail='Playback rejected')
    stream_tracker.fail(stream_id, detail)
    stream_tracker.stop(stream_id)
    return Response(detail, status=status, mimetype='text/plain')


@app.route("/transcode/<int:root_index>/<path:relpath>")
def transcode(root_index, relpath):
    root = get_root(root_index)
    source = safe_path(root, relpath)
    if not is_video(source):
        abort(404)

    target = resolve_media_source(source)
    quality = requested_quality()
    if request.args.get("quality") == "source":
        abort(409, description="Original quality disables transcoding. Use the original media endpoint.")
    video_codec, audio_codec = requested_codecs()
    transcoder = selected_transcoder()
    if not transcoder["ok"]:
        app.logger.error("No usable transcoder: %s", transcoder["detail"])
        return Response(
            "No usable transcoder is available.\n" + transcoder["detail"],
            status=503, mimetype="text/plain"
        )

    try:
        start = float(request.args.get("start", "0"))
    except ValueError:
        abort(400)
    if not math.isfinite(start) or start < 0:
        abort(400)

    audio_index = None
    if request.args.get("audio"):
        try:
            audio_index = int(request.args["audio"])
        except ValueError:
            abort(400)

    backend, fallback = codec_transcoder(transcoder, video_codec)
    if not backend:
        return Response(f"No encoder available for {VIDEO_CODECS[video_codec]['label']}.\n{fallback}", status=503, mimetype="text/plain")
    info = probe(source)
    source_video = next((track for track in info.get('streams', []) if track.get('codec_type') == 'video'), {})
    if audio_index is not None:
        _, audio_tracks, _ = stream_lists(info)
        if audio_index not in {track['index'] for track in audio_tracks}:
            abort(400)
    try:
        tone_mapper = load_settings()['tone_mapping']
        mapping = tone_mapping_plan(source_video, backend, tone_mapper)
    except ToneMappingError as error:
        return rejected_playback(source, backend, str(error), 415)
    if mapping.active:
        supported, detail = tone_mapping_test(backend, tone_mapper)
        if not supported:
            return rejected_playback(source, backend,
                                     'HDR-to-SDR tone mapping is unavailable on the selected backend.\n' + detail, 503)
    if audio_index is not None and audio_codec != "aac":
        supported, detail = codec_encoder_test("software", audio_codec, True)
        if not supported:
            return Response(f"Audio encoder unavailable: {detail}", status=503, mimetype="text/plain")
    encoder = VIDEO_ENCODERS[backend][video_codec]
    encoder_label = f"{BACKEND_LABELS[backend]} · {encoder}"
    cmd = transcode_command(backend, target, audio_index, start, quality, video_codec, audio_codec, source_video, tone_mapper)
    app.logger.info("Starting %s transcode: %s", encoder_label,
                    source.name if isinstance(source, WebDAVPath) or source.suffix.lower() == ".strm" else " ".join(cmd))
    try:
        proc = media_runtime.popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    except (OSError, RuntimeError):
        abort(503, description="Unable to start the media encoder.")

    stream_id = stream_tracker.start(
        backend, source, start_offset=start,
        detail=f"{encoder_label} · {VIDEO_CODECS[video_codec]['label']} / {AUDIO_CODECS[audio_codec]['label'] if audio_index is not None else 'No audio'} · {quality['label']}",
        technical={"video_codec": video_codec, "audio_codec": audio_codec if audio_index is not None else None,
                   "encoder": encoder, "encoder_label": encoder_label, "fallback": fallback,
                   "tone_mapping": mapping.label},
    )
    stderr_head = bytearray()
    stderr_tail = bytearray()

    def drain_errors():
        try:
            if proc.stderr:
                while chunk := proc.stderr.read(4096):
                    stderr_head.extend(chunk[:max(0, 4000 - len(stderr_head))])
                    stderr_tail.extend(chunk)
                    del stderr_tail[:-4000]
        except (OSError, ValueError):
            pass

    error_thread = threading.Thread(target=drain_errors, name="ffmpeg-stderr", daemon=True)
    error_thread.start()
    close_lock = threading.Lock()
    closed = False

    def close(cancelled=True):
        nonlocal closed
        with close_lock:
            if closed:
                return
            closed = True
        failed_before_close = proc.poll() not in (0, None)
        try:
            if not cancelled and proc.poll() is None:
                try:
                    proc.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    cancelled = True
            media_runtime.stop_process(proc)
            error_thread.join(timeout=1)
            for pipe in (proc.stdout, proc.stderr):
                if pipe:
                    pipe.close()
            if (not cancelled or failed_before_close) and proc.returncode not in (0, None):
                errors = bytes(stderr_head) if stderr_head == stderr_tail else bytes(stderr_head) + b'\n...\n' + bytes(stderr_tail)
                detail = safe_playback_error(errors.decode('utf-8', 'replace').strip()
                                             or f'FFmpeg exited with code {proc.returncode}.')
                stream_tracker.fail(stream_id, detail)
                app.logger.error('FFmpeg transcode exited %s: %s', proc.returncode, detail)
        finally:
            media_runtime.release(proc)
            stream_tracker.stop(stream_id)

    def generate():
        cancelled = False
        try:
            assert proc.stdout is not None
            while not media_runtime.stopping.is_set():
                chunk = proc.stdout.read(256 * 1024)
                if not chunk:
                    break
                stream_tracker.add_bytes(stream_id, len(chunk))
                yield chunk
        except GeneratorExit:
            cancelled = True
            raise
        except (BrokenPipeError, ConnectionResetError):
            cancelled = True
        finally:
            close(cancelled=cancelled or media_runtime.stopping.is_set())

    # The generator uses no request globals. Avoid retaining a Flask request
    # context across cancelled streams when the player switches quality.
    response = Response(generate())
    response.call_on_close(close)
    response.headers["Content-Type"] = "video/mp4"
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@app.route("/api/playback-info/<int:root_index>/<path:relpath>")
def playback_info(root_index, relpath):
    root = get_root(root_index)
    source = safe_path(root, relpath)
    if not is_video(source):
        abort(404)
    metrics = stream_tracker.playback_metrics(
        request.args.get("playback_id", ""), source, session.get("playback_owner"))
    response = jsonify({"stream": metrics})
    response.headers["Cache-Control"] = "no-store"
    return response


def subtitle_bytes(target, position, length, timeout):
    if isinstance(target, WebDAVPath):
        resource = target.service.client.stat(target.path)
        end = min(resource.size, position + length) - 1
        return b''.join(target.service.chunks(resource, position, end))
    if isinstance(target, Path):
        with target.open('rb') as handle:
            handle.seek(position)
            return handle.read(length)
    with urlopen(Request(target, headers={'Range': f'bytes={position}-{position + length - 1}',
                                         'Accept-Encoding': 'identity'}), timeout=timeout) as response:
        if response.status != 206 or not response.headers.get('Content-Range', '').startswith(f'bytes {position}-'):
            raise ValueError('Subtitle source does not support byte ranges')
        return response.read(length)


@app.route("/subtitle/<int:root_index>/<path:relpath>")
def subtitle(root_index, relpath):
    root = get_root(root_index)
    source = safe_path(root, relpath)
    if not is_video(source):
        abort(404)
    target = resolve_media_source(source)
    try:
        idx = int(request.args["stream"])
        start = float(request.args.get("start", "0"))
        duration = float(request.args.get("duration", "20"))
    except (KeyError, ValueError):
        abort(400)

    if (idx < 0 or not math.isfinite(start) or start < 0
            or not math.isfinite(duration) or not 5 <= duration <= 30
            or request.args.get('absolute', '0') not in {'0', '1'}):
        abort(400)

    offset = start if request.args.get('absolute') == '1' else 0
    try:
        deadline = time.monotonic() + 30
        info = probe(source)
        track = next((row for row in info.get('streams', []) if row.get('index') == idx), {})
        if track.get('codec_type') != 'subtitle' or track.get('codec_name') not in TEXT_SUBTITLE_CODECS:
            abort(415, description="Select a supported text subtitle track.")
        try:
            origin = float(info.get('format', {}).get('start_time', 0) or 0)
            if not math.isfinite(origin):
                origin = 0
        except (ValueError, TypeError):
            origin = 0
        absolute_start = max(0, start + origin)
        # Do not use -select_streams here: FFprobe must observe video/audio
        # timestamps too, to stop inside a long gap between subtitle packets.
        packets = run_media_command(
            [FFPROBE, '-v', 'error', '-read_intervals', f'{absolute_start:.3f}%{absolute_start + duration:.3f}',
             '-show_packets', '-show_entries', 'packet=stream_index,pts_time,duration_time,size,pos',
             '-of', 'json', media_input_target(target)],
            capture_output=True, timeout=max(0.01, deadline - time.monotonic()),
        )
        if packets.returncode:
            abort(502, description="Unable to read this subtitle window.")
        selected = []
        for row in json.loads(packets.stdout).get('packets', []):
            if row.get('stream_index') != idx:
                continue
            try:
                pts = float(row['pts_time'])
                length = float(row.get('duration_time', 0) or 0)
            except (KeyError, ValueError, TypeError):
                continue
            if pts < absolute_start + duration and pts + max(0, length) > absolute_start:
                selected.append(row)
        count = len(selected)
        if count == 0:
            return Response(b'WEBVTT\n\n', mimetype='text/vtt', headers={'Cache-Control': 'no-store'})
        # Prefer exact subtitle byte ranges. The generic fallback captures a
        # known number of subtitle packets, then stops before extra lookahead.
        # Video/audio bytes stay out of the JSON and conversion input.
        formats = set(info.get('format', {}).get('format_name', '').split(','))
        matroska = bool(formats & {'matroska', 'webm'})
        raw = None
        if matroska or (track.get('codec_name') == 'mov_text' and formats & {'mov', 'mp4'}):
            try:
                raw = []
                total = 0
                for row in selected:
                    position, size = int(row['pos']), int(row['size'])
                    total += size
                    if position < 0 or size <= 0 or total > 2 * 1024 * 1024:
                        raise ValueError('Invalid subtitle packet range')
                    prefix = min(16, position) if matroska else 0
                    payload = subtitle_bytes(target, position - prefix, size + prefix + 20 if matroska else size,
                                             max(0.01, deadline - time.monotonic()))
                    payload = matroska_payload(payload, size, prefix) if matroska else payload
                    raw.append({**row, 'payload': payload})
            except (ValueError, KeyError):
                raw = None
        if raw is None:
            raw = capture_packets(media_runtime,
                [FFPROBE, '-v', 'error', '-read_intervals', f'{absolute_start:.3f}%+#{count}',
                 '-select_streams', str(idx), '-show_packets', '-show_data',
                 '-show_entries', 'packet=pts_time,duration_time,data',
                 '-of', 'json=compact=1', media_input_target(target)],
                count=count, timeout=max(0.01, deadline - time.monotonic()), start=absolute_start,
            )
        data = subtitle_container(track, raw, start + origin, duration)
        if data is None:
            return Response(b'WEBVTT\n\n', mimetype='text/vtt', headers={'Cache-Control': 'no-store'})
        proc = run_media_command(
            [FFMPEG, "-hide_banner", "-loglevel", "error", '-copyts', '-f', 'matroska', '-i', 'pipe:0']
            + ["-map", '0:s:0', '-output_ts_offset', f'{offset:.3f}', '-flush_packets', '1',
               "-f", "webvtt", "pipe:1"],
            input=data, capture_output=True, timeout=max(0.01, deadline - time.monotonic()),
        )
    except subprocess.TimeoutExpired:
        abort(504, description="Subtitle window timed out. Video playback can continue; subtitles will retry.")
    except (OSError, RuntimeError):
        abort(503, description="Unable to start subtitle extraction.")
    except (ValueError, TypeError):
        abort(415, description="Unable to decode this subtitle window.")
    if proc.returncode:
        abort(415, description="Unable to convert this track to text subtitles.")
    return Response(proc.stdout, mimetype="text/vtt", headers={'Cache-Control': 'no-store'})


def transcoder_status_payload(force_refresh: bool = False) -> dict[str, Any]:
    status = selected_transcoder(force_refresh=force_refresh)
    streams = stream_tracker.snapshot()
    service = _nzbdav_service
    return {
        "nzbdav_ram_cache": service.cache.snapshot() if service and can_manage_settings() else None,
        "transcoder_ok": status["ok"],
        "backend": status["label"],
        "preference": TRANSCODER_PREFERENCE,
        "ffmpeg": FFMPEG,
        "ffprobe": FFPROBE,
        "vaapi_device": VAAPI_DEVICE if os.name != "nt" else None,
        "active_stream_count": len(streams),
        "streams": streams,
        "tests": [
            {"key": key, "label": label, "ok": ok, "detail": detail[-1000:]}
            for key, label, ok, detail in status["tests"]
        ],
    }


@app.route("/transcoder-status")
def transcoder_status_page():
    # Backend tests are cached. The page polls only lightweight in-memory
    # stream metrics unless the user explicitly requests a retest.
    return render_template("status.html")


@app.route("/api/transcoder-status")
def transcoder_status_api():
    refresh = request.args.get("refresh", "").strip().lower() in {"1", "true", "yes"}
    return jsonify(transcoder_status_payload(force_refresh=refresh))


@app.route("/hw-status")
@app.route("/qsv-status")
def legacy_hardware_status():
    return jsonify(transcoder_status_payload(force_refresh=False))


@app.errorhandler(404)
def not_found(_):
    return render_template("error.html", message="That item was not found."), 404


@app.errorhandler(403)
def forbidden(_):
    return render_template("error.html", message="Access outside the configured media roots is not allowed."), 403


@app.errorhandler(RemoteError)
def remote_media_error(error):
    return render_template("error.html", message=str(error)), 502


if __name__ == "__main__":
    media_runtime.install_signal_handlers()
    try:
        app.run(host="0.0.0.0", port=SERVER_PORT, threaded=True)
    finally:
        media_runtime.shutdown()
