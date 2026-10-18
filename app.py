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
from functools import lru_cache
from pathlib import Path
from typing import Any

from flask import (
    Flask, Response, abort, jsonify, redirect, render_template, request,
    send_file, session, stream_with_context, url_for, make_response,
)
from PIL import Image, ImageOps
import fitz

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
    ".m2ts", ".mts", ".ts", ".wmv", ".flv", ".ogv",
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


class StreamTracker:
    """In-memory live stream metrics. No database or persistent state."""

    def __init__(self):
        self._lock = threading.Lock()
        self._streams: dict[str, dict[str, Any]] = {}

    def start(
        self,
        mode: str,
        source: Path,
        *,
        start_offset: float = 0.0,
        detail: str = "",
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
            self._streams.pop(stream_id, None)

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
        for i in range(workers):
            threading.Thread(
                target=self._worker, name=f"image-worker-{i}", daemon=True
            ).start()

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
                generation != self._generation
                or key in self._queued
                or key in self._failed
            ):
                return
            self._sequence += 1
            seq = self._sequence
            self._queued.add(key)
        self._queue.put((priority, seq, generation, key, source, output, width))

    def _worker(self):
        while True:
            priority, seq, generation, key, source, output, width = self._queue.get()
            try:
                if generation == self.current_generation():
                    generate_image(source, output, width, generation, self)
            finally:
                with self._lock:
                    self._queued.discard(key)
                self._queue.task_done()


image_scheduler = ImageScheduler(2)


def profile_id_for_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


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


def path_is_excluded(path: Path) -> bool:
    profile = active_profile()
    if not profile:
        return True

    try:
        resolved = path.resolve(strict=False)
    except RuntimeError:
        return True

    for excluded in profile["excluded"]:
        try:
            excluded_resolved = excluded.resolve(strict=False)
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
    return path.name.strip() or f"Media {index + 1}"


def get_root(index: int) -> Path:
    roots = load_roots()
    if index < 0 or index >= len(roots):
        abort(404)
    root = roots[index]
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
    if not path.is_file():
        return None
    suffix = path.suffix.lower()
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


def probe(path: Path) -> dict[str, Any]:
    cmd = [
        FFPROBE, "-v", "error",
        "-show_entries",
        "format=duration,size,format_name:"
        "stream=index,codec_type,codec_name,width,height,channels:"
        "stream_tags=language,title",
        "-of", "json", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or "ffprobe failed")
    return json.loads(proc.stdout)


def duration_seconds(info: dict[str, Any]) -> float:
    try:
        return max(0.0, float(info.get("format", {}).get("duration", 0) or 0))
    except (TypeError, ValueError):
        return 0.0


def screenshot_candidates(info: dict[str, Any]) -> list[float]:
    """Return useful thumbnail sample times while avoiding intros/credits."""
    d = duration_seconds(info)
    if d <= 2:
        return [0.0]

    candidates = [
        max(2.0, d * 0.10),
        max(2.0, d * 0.20),
        max(2.0, d * 0.35),
        max(2.0, d * 0.50),
        max(2.0, d * 0.70),
        min(30.0, max(2.0, d * 0.05)),
    ]

    result: list[float] = []
    for value in candidates:
        value = min(max(0.0, value), max(0.0, d - 0.5))
        if not any(abs(value - existing) < 0.5 for existing in result):
            result.append(value)
    return result


def frame_black_percent(source: Path, stamp: float) -> float | None:
    """Return the percentage of sampled pixels FFmpeg considers black."""
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "info",
        "-ss", f"{stamp:.3f}", "-i", str(source),
        "-frames:v", "1",
        "-vf", "blackframe=amount=0:threshold=32",
        "-f", "null", "-"
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        match = re.search(r"pblack:(\d+)", proc.stderr or "")
        if match:
            return float(match.group(1))
    except Exception:
        pass
    return None


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

    proc = subprocess.run(
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

    proc = subprocess.run(
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
    if generation != scheduler.current_generation():
        return

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(".tmp.jpg")

    try:
        if media_kind(source) != "video":
            if _generate_nonvideo_thumbnail(source, tmp, width):
                if generation == scheduler.current_generation():
                    tmp.replace(output)
            return

        info = probe(source)
        candidates = screenshot_candidates(info)

        for stamp in candidates:
            if generation != scheduler.current_generation():
                return
            black_percent = frame_black_percent(source, stamp)
            if black_percent is not None and black_percent >= 85:
                continue
            cmd = [
                FFMPEG, "-hide_banner", "-loglevel", "error",
                "-ss", f"{stamp:.3f}", "-i", str(source),
                "-frames:v", "1",
                "-vf", f"scale={width}:-2:force_original_aspect_ratio=decrease",
                "-q:v", "3", "-y", str(tmp),
            ]
            proc = subprocess.run(cmd, capture_output=True, timeout=90)
            if (
                proc.returncode == 0
                and tmp.exists()
                and generation == scheduler.current_generation()
            ):
                tmp.replace(output)
                return
            tmp.unlink(missing_ok=True)

        fallback = candidates[0] if candidates else 0.0
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error",
            "-ss", f"{fallback:.3f}", "-i", str(source),
            "-frames:v", "1",
            "-vf", f"scale={width}:-2:force_original_aspect_ratio=decrease",
            "-q:v", "3", "-y", str(tmp),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=90)
        if (
            proc.returncode == 0
            and tmp.exists()
            and generation == scheduler.current_generation()
        ):
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


def direct_playback_ok(path: Path, info, audios) -> bool:
    videos, _, _ = stream_lists(info)
    if not videos or len(audios) > 1:
        return False
    v = videos[0]["codec"]
    a = audios[0]["codec"] if audios else None
    ext = path.suffix.lower()
    if ext in {".mp4", ".m4v", ".mov"}:
        return v == "h264" and a in {None, "aac", "mp3"}
    if ext == ".webm":
        return v in {"vp8", "vp9", "av1"} and a in {None, "opus", "vorbis"}
    return False


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
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
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
                    video_filter="format=nv12,hwupload",
                ),
            ))
        tests.append(("nvenc", "NVIDIA NVENC", run_encoder_test("h264_nvenc", video_filter="format=nv12")))

    tests.append(("software", "Software libx264", run_encoder_test("libx264")))
    return tests


_transcoder_cache = None


def selected_transcoder(force_refresh: bool = False) -> dict:
    global _transcoder_cache
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
                updated = {"port": str(int(port)), "use_master_password": str(mode == "master").lower(),
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
            "preview": bool(folder_cached_preview(i, "", recursive=True)),
        }
        for i, p in enumerate(roots)
        if not path_is_excluded(p)
    ]
    items.sort(key=lambda item: item["label"].casefold())
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
        entries = list(current.iterdir())
    except OSError as exc:
        return render_template("error.html", message=f"Unable to read folder: {exc}"), 500

    for entry in entries:
        try:
            if path_is_excluded(entry):
                continue
            if entry.is_dir():
                folder_rel = entry.relative_to(root).as_posix()
                folders.append({
                    "name": entry.name,
                    "rel": folder_rel,
                    "preview": bool(folder_cached_preview(root_index, folder_rel)),
                })
                continue

            kind = media_kind(entry)
            if not kind:
                continue

            rel = entry.relative_to(root).as_posix()
            item: dict[str, Any] = {"name": entry.name, "rel": rel, "kind": kind}
            if kind != "text":
                thumb = cache_path("thumbs", root_index, root, entry)
                poster = cache_path("posters", root_index, root, entry) if kind == "video" else None
                item.update({
                    "thumb_ready": thumb.exists() or bool(poster and poster.exists()),
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
    for kind in ("video", "image", "pdf", "comic"):
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
    generation = image_scheduler.new_generation()
    root = get_root(root_index)
    source = safe_path(root, relpath)
    if not is_video(source):
        abort(404)

    info = probe(source)
    videos, audios, subtitles = stream_lists(info)
    poster = cache_path("posters", root_index, root, source)
    image_scheduler.submit(generation, 0, source, poster, 960)
    v = videos[0] if videos else {}

    return render_template(
        "video.html",
        root_index=root_index, relpath=relpath, filename=source.name,
        parent_rel=parent_relpath(relpath),
        size=fmt_size(source.stat().st_size),
        duration_text=fmt_duration(duration_seconds(info)),
        width=v.get("width"), height=v.get("height"),
        video_codec=v.get("codec", "unknown"),
        audios=audios, subtitles=subtitles,
        native=direct_playback_ok(source, info, audios),
        poster_ready=poster.exists(), generation=generation,
        quality_profiles=QUALITY_PROFILES, default_quality=load_settings().get("default_quality", "auto"),
    )


@app.route("/play/<int:root_index>/<path:relpath>")
def player(root_index, relpath):
    image_scheduler.new_generation()
    root = get_root(root_index)
    source = safe_path(root, relpath)
    if not is_video(source):
        abort(404)
    info = probe(source)
    _, audios, subtitles = stream_lists(info)
    d = duration_seconds(info)
    return render_template(
        "player.html", root_index=root_index, relpath=relpath,
        filename=source.name, duration=d, duration_text=fmt_duration(d),
        audios=audios, subtitles=subtitles,
        native=direct_playback_ok(source, info, audios),
        quality_profiles=QUALITY_PROFILES,
        default_quality=request.args.get("quality") if request.args.get("quality") in QUALITY_PROFILES else load_settings().get("default_quality", "auto"),
    )


def parse_range_header(range_header: str | None, file_size: int) -> tuple[int, int] | None:
    """Return inclusive start/end for a single HTTP byte range."""
    if not range_header or not range_header.startswith("bytes="):
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


def ranged_file_response(source: Path, mimetype: str):
    file_size = source.stat().st_size
    requested = request.headers.get("Range")

    # Native playback startup may begin with a normal GET. Stream it in chunks
    # rather than handing the entire file to Flask/Werkzeug.
    if not requested:
        stream_id = stream_tracker.start(
            "direct",
            source,
            detail=f"full file, {file_size} bytes",
        )

        def whole_file():
            try:
                with source.open("rb", buffering=0) as fh:
                    while True:
                        chunk = fh.read(1024 * 1024)
                        if not chunk:
                            break
                        stream_tracker.add_bytes(stream_id, len(chunk))
                        yield chunk
            finally:
                stream_tracker.stop(stream_id)

        response = Response(stream_with_context(whole_file()), status=200, mimetype=mimetype)
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
        source,
        detail=f"bytes {start}-{end}/{file_size}",
    )

    def partial_file():
        remaining = length
        try:
            with source.open("rb", buffering=0) as fh:
                # Jump directly to the requested offset, which is important
                # for NAS-backed files.
                fh.seek(start, os.SEEK_SET)
                while remaining > 0:
                    chunk = fh.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    stream_tracker.add_bytes(stream_id, len(chunk))
                    yield chunk
        finally:
            stream_tracker.stop(stream_id)

    response = Response(
        stream_with_context(partial_file()),
        status=206,
        mimetype=mimetype,
        direct_passthrough=True,
    )
    response.headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    response.headers["Content-Length"] = str(length)
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Cache-Control"] = "private, max-age=0"
    return response


@app.route("/media/<int:root_index>/<path:relpath>")
def media_file(root_index, relpath):
    root = get_root(root_index)
    source = safe_path(root, relpath)
    if not is_video(source):
        abort(404)

    mimetype = mimetypes.guess_type(str(source))[0] or "application/octet-stream"
    return ranged_file_response(source, mimetype)


def best_cached_image(root_index: int, root: Path, source: Path, requested_kind: str) -> Path | None:
    requested = cache_path(requested_kind, root_index, root, source)
    if requested.exists():
        return requested
    if requested_kind == "thumbs":
        poster = cache_path("posters", root_index, root, source)
        if poster.exists():
            return poster
    return None


def folder_cached_preview(
    root_index: int,
    folder_rel: str,
    recursive: bool = False,
) -> Path | None:
    relative = Path(folder_rel)
    for kind in ("thumbs", "posters"):
        root = get_root(root_index)
        cache_dir = cache_root_dir(kind, root) / relative
        try:
            if not cache_dir.is_dir():
                continue

            candidates = [
                entry for entry in cache_dir.iterdir()
                if entry.is_file() and entry.suffix.lower() == ".jpg"
            ]
            if candidates:
                candidates.sort(key=lambda path: natural_sort_key(path.name))
                return candidates[0]

            if recursive:
                for current, dirnames, filenames in os.walk(cache_dir):
                    dirnames.sort(key=natural_sort_key)
                    jpgs = [
                        name for name in filenames
                        if Path(name).suffix.lower() == ".jpg"
                    ]
                    if jpgs:
                        jpgs.sort(key=natural_sort_key)
                        return Path(current) / jpgs[0]
        except OSError:
            continue

    return None


@app.route(
    "/folder-preview/<int:root_index>/",
    defaults={"relpath": ""},
)
@app.route("/folder-preview/<int:root_index>/<path:relpath>")
def folder_preview(root_index, relpath):
    root = get_root(root_index)
    folder = root if not relpath else safe_path(root, relpath)
    if not folder.is_dir():
        abort(404)

    preview = folder_cached_preview(
        root_index,
        relpath,
        recursive=not bool(relpath),
    )
    if not preview:
        abort(404)

    return send_file(
        preview,
        mimetype="image/jpeg",
        conditional=True,
        max_age=86400,
    )


@app.route("/thumb/<int:root_index>/<path:relpath>")
def thumbnail(root_index, relpath):
    root = get_root(root_index)
    source = safe_path(root, relpath)
    if media_kind(source) not in {"video", "image", "pdf", "comic"}:
        abort(404)
    out = best_cached_image(root_index, root, source, "thumbs")
    if not out:
        abort(404)
    return send_file(out, mimetype="image/jpeg", conditional=True, max_age=86400)


@app.route("/poster/<int:root_index>/<path:relpath>")
def poster(root_index, relpath):
    root = get_root(root_index)
    source = safe_path(root, relpath)
    if not is_video(source):
        abort(404)
    out = cache_path("posters", root_index, root, source)
    if not out.exists():
        abort(404)
    return send_file(out, mimetype="image/jpeg", conditional=True, max_age=86400)


@app.route("/api/image-ready/<kind>/<int:root_index>/<path:relpath>")
def image_ready(kind, root_index, relpath):
    if kind not in {"thumbs", "posters"}:
        abort(404)
    try:
        generation = int(request.args.get("generation", "0"))
    except ValueError:
        abort(400)

    root = get_root(root_index)
    source = safe_path(root, relpath)
    source_kind = media_kind(source)
    if kind == "thumbs":
        if source_kind not in {"video", "image", "pdf", "comic"}:
            abort(404)
        found = best_cached_image(root_index, root, source, "thumbs")
        out = cache_path("thumbs", root_index, root, source)
        width, priority = 360, 10
    else:
        if source_kind != "video":
            abort(404)
        out = cache_path("posters", root_index, root, source)
        found = out if out.exists() else None
        width, priority = 960, 0

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
    "source": {"label": "Original · transcode", "width": 0, "height": 0, "video_kbps": 0, "audio_kbps": 192},
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


def transcode_command(
    backend: str,
    source: Path,
    audio_index: int | None,
    start: float,
    quality: dict | None = None,
) -> list[str]:
    quality = quality or QUALITY_PROFILES["auto"]
    limited = bool(quality["video_kbps"])
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if backend == "vaapi":
        cmd += ["-vaapi_device", VAAPI_DEVICE]
    cmd += ["-ss", f"{max(0.0, start):.3f}", "-i", str(source), "-map", "0:v:0"]
    cmd += ["-map", f"0:{audio_index}"] if audio_index is not None else ["-an"]
    filters = []
    if quality["width"]:
        # Bound both dimensions, preserve aspect ratio and never enlarge the source.
        filters.append(f"scale=w='min(iw,{quality['width']})':h='min(ih,{quality['height']})':force_original_aspect_ratio=decrease:force_divisible_by=2")
    filters.append("format=yuv420p" if backend == "software" else "format=nv12")
    if backend == "vaapi":
        filters.append("hwupload")
    cmd += ["-vf", ",".join(filters)]
    options = {
        "vaapi": ["-c:v", "h264_vaapi"] + (["-rc_mode", "VBR"] if limited else ["-qp", "24"]),
        "qsv": ["-c:v", "h264_qsv", "-preset", "veryfast", "-look_ahead", "0"] + ([] if limited else ["-global_quality", "24"]),
        "nvenc": ["-c:v", "h264_nvenc", "-preset", "p3", "-tune", "ll", "-rc", "vbr"] + ([] if limited else ["-cq", "23", "-b:v", "0"]),
        "amf": ["-c:v", "h264_amf", "-quality", "speed"] + (["-rc", "vbr_peak"] if limited else ["-rc", "cqp", "-qp_i", "24", "-qp_p", "24"]),
        "software": ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency"] + ([] if limited else ["-crf", "23"]),
    }
    cmd += options[backend]
    if limited:
        bitrate = quality["video_kbps"]
        cmd += ["-b:v", f"{bitrate}k", "-maxrate", f"{bitrate}k", "-bufsize", f"{bitrate * 2}k"]
    cmd += ["-bf", "0", "-g", "48"]
    if audio_index is not None:
        cmd += ["-c:a", "aac", "-b:a", f"{quality['audio_kbps']}k", "-ac", "2"]
    cmd += ["-movflags", "+frag_keyframe+empty_moov+default_base_moof+omit_tfhd_offset",
            "-frag_duration", "500000", "-flush_packets", "1", "-f", "mp4", "pipe:1"]
    return cmd


@app.route("/transcode/<int:root_index>/<path:relpath>")
def transcode(root_index, relpath):
    root = get_root(root_index)
    source = safe_path(root, relpath)
    if not is_video(source):
        abort(404)

    quality = requested_quality()
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

    if audio_index is not None:
        _, audio_tracks, _ = stream_lists(probe(source))
        if audio_index not in {track["index"] for track in audio_tracks}:
            abort(400)

    cmd = transcode_command(transcoder["key"], source, audio_index, start, quality)
    app.logger.info("Starting %s transcode: %s", transcoder["label"], " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)

    stream_id = stream_tracker.start(
        transcoder["key"],
        source,
        start_offset=start,
        detail=f"{transcoder['label']} · {quality['label']}",
    )

    def generate():
        cancelled = False
        stderr = b""
        try:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(256 * 1024)
                if not chunk:
                    break
                stream_tracker.add_bytes(stream_id, len(chunk))
                yield chunk
        except GeneratorExit:
            # Normal browser behavior: seeking, changing tracks, navigating
            # away, or closing playback abandons the old HTTP stream.
            cancelled = True
            raise
        except (BrokenPipeError, ConnectionResetError):
            cancelled = True
        finally:
            stream_tracker.stop(stream_id)

            if proc.poll() is None:
                cancelled = True
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass

            if proc.stderr:
                try:
                    stderr = proc.stderr.read()
                except Exception:
                    pass

            # A terminated FFmpeg process is expected when the browser stops
            # consuming a stream. Only report genuine unexpected failures.
            if not cancelled and proc.returncode not in (0, None):
                app.logger.error(
                    "FFmpeg transcode exited %s: %s",
                    proc.returncode, stderr.decode("utf-8", "replace")[-4000:]
                )

    # The generator uses no request globals. Avoid retaining a Flask request
    # context across cancelled streams when the player switches quality.
    response = Response(generate())
    response.headers["Content-Type"] = 'video/mp4; codecs="avc1.42E01E, mp4a.40.2"'
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@app.route("/subtitle/<int:root_index>/<path:relpath>")
def subtitle(root_index, relpath):
    root = get_root(root_index)
    source = safe_path(root, relpath)
    if not is_video(source):
        abort(404)
    try:
        idx = int(request.args["stream"])
        start = float(request.args.get("start", "0"))
    except (KeyError, ValueError):
        abort(400)

    proc = subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, start):.3f}",
        "-i", str(source), "-map", f"0:{idx}", "-f", "webvtt", "pipe:1"
    ], capture_output=True, timeout=30)
    if proc.returncode:
        abort(415)
    return Response(proc.stdout, mimetype="text/vtt")


def transcoder_status_payload(force_refresh: bool = False) -> dict[str, Any]:
    status = selected_transcoder(force_refresh=force_refresh)
    streams = stream_tracker.snapshot()
    return {
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=SERVER_PORT, threaded=True)
