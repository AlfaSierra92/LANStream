from __future__ import annotations

import json
import hashlib
import mimetypes
import os
import queue
import shutil
import sys
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from flask import (
    Flask, Response, abort, jsonify, redirect, render_template, request,
    send_file, session, stream_with_context, url_for,
)

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
VERSION = "1.0.0"


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
        "low_bandwidth_resolution": "854x480",
        "low_bandwidth_bitrate": "2M",
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

LOW_BANDWIDTH_RESOLUTION = SETTINGS.get("low_bandwidth_resolution", "854x480").strip() or "854x480"
LOW_BANDWIDTH_BITRATE = SETTINGS.get("low_bandwidth_bitrate", "2M").strip() or "2M"


def parse_resolution(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", value)
    if not match:
        return 854, 480
    width = max(160, int(match.group(1)))
    height = max(90, int(match.group(2)))
    width -= width % 2
    height -= height % 2
    return width, height


LOW_BANDWIDTH_WIDTH, LOW_BANDWIDTH_HEIGHT = parse_resolution(LOW_BANDWIDTH_RESOLUTION)

def parse_bitrate_bits(value: str) -> int | None:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKmMgG]?)\s*", value)
    if not match:
        return None
    number = float(match.group(1))
    suffix = match.group(2).lower()
    factor = {"": 1, "k": 1_000, "m": 1_000_000, "g": 1_000_000_000}[suffix]
    return int(number * factor)


def human_bitrate(bits_per_second: int | None) -> str | None:
    if bits_per_second is None:
        return None
    if bits_per_second >= 1_000_000:
        return f"{bits_per_second / 1_000_000:.2f} Mbps"
    if bits_per_second >= 1_000:
        return f"{bits_per_second / 1_000:.0f} kbps"
    return f"{bits_per_second} bps"


LOW_BANDWIDTH_VIDEO_BPS = parse_bitrate_bits(LOW_BANDWIDTH_BITRATE)
LOW_BANDWIDTH_AUDIO_BPS = 128_000
LOW_BANDWIDTH_TOTAL_BPS = (
    LOW_BANDWIDTH_VIDEO_BPS + LOW_BANDWIDTH_AUDIO_BPS
    if LOW_BANDWIDTH_VIDEO_BPS is not None
    else None
)


VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".mpg", ".mpeg",
    ".m2ts", ".mts", ".ts", ".wmv", ".flv", ".ogv",
}
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
        target_bitrate: str | None = None,
        quality_mode: str = "normal",
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
                "target_bitrate": target_bitrate,
                "quality_mode": quality_mode,
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
                    "target_bitrate": item.get("target_bitrate"),
                    "quality_mode": item.get("quality_mode", "normal"),
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
        for i in range(workers):
            threading.Thread(
                target=self._worker, name=f"image-worker-{i}", daemon=True
            ).start()

    def new_generation(self) -> int:
        with self._lock:
            self._generation += 1
            return self._generation

    def current_generation(self) -> int:
        with self._lock:
            return self._generation

    def submit(self, generation: int, priority: int, source: Path, output: Path, width: int) -> None:
        if output.exists():
            return
        key = str(output)
        with self._lock:
            if generation != self._generation or key in self._queued:
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

    Without <password> sections, all listed roots use the optional master
    password configuration.
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

    # Passwordless mode uses the implicit profile directly.
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

    # Windows may resolve a mapped drive such as Z:\ to its backing UNC path.
    # Return the resolved root so children discovered through it use the same
    # path namespace for relative-path and cache calculations.
    try:
        return root.resolve(strict=False)
    except (OSError, RuntimeError):
        return root.absolute()


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


def is_video(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS


def cache_path(kind: str, root_index: int, root: Path, source: Path) -> Path:
    try:
        resolved_root = root.resolve(strict=False)
        resolved_source = source.resolve(strict=False)
    except (OSError, RuntimeError):
        resolved_root = root.absolute()
        resolved_source = source.absolute()

    rel = resolved_source.relative_to(resolved_root)
    return CACHE_DIR / kind / f"root_{root_index}" / rel.parent / f"{rel.name}.jpg"


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


def generate_image(source: Path, output: Path, width: int, generation: int, scheduler: ImageScheduler):
    if generation != scheduler.current_generation():
        return

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(".tmp.jpg")

    try:
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

        # If every candidate was mostly black, still create an image rather
        # than leaving the item permanently on its placeholder.
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

    except Exception:
        pass
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
            return

    if not session.get("authenticated"):
        return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not authentication_required():
        return redirect(url_for("home"))

    error = None
    if request.method == "POST":
        supplied = request.form.get("password", "")
        matched = None
        for profile in load_profiles():
            if secrets.compare_digest(supplied, profile["password"]):
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
        {"index": i, "label": root_label(p, i), "exists": p.is_dir()}
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

    folders, videos = [], []
    try:
        entries = list(current.iterdir())
    except OSError as exc:
        return render_template("error.html", message=f"Unable to read folder: {exc}"), 500

    for entry in entries:
        try:
            if path_is_excluded(entry):
                continue
            if entry.is_dir():
                folders.append({"name": entry.name, "rel": entry.relative_to(root).as_posix()})
            elif is_video(entry):
                rel = entry.relative_to(root).as_posix()
                thumb = cache_path("thumbs", root_index, root, entry)
                poster = cache_path("posters", root_index, root, entry)
                videos.append({
                    "name": entry.name, "rel": rel,
                    "thumb_ready": thumb.exists() or poster.exists(),
                    "has_thumb": thumb.exists(),
                    "has_poster": poster.exists(),
                    "_source": entry, "_thumb": thumb,
                })
        except OSError:
            continue

    folders.sort(key=lambda x: x["name"].casefold())
    videos.sort(key=lambda x: x["name"].casefold())

    # Queue the initial visible batch; additional thumbnails are viewport-driven.
    for item in videos[:12]:
        if not item["thumb_ready"]:
            image_scheduler.submit(generation, 10, item["_source"], item["_thumb"], 360)

    for item in videos:
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
        "browse.html", root_index=root_index, folders=folders, videos=videos,
        crumbs=crumbs, generation=generation, up_url=up_url,
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

    if not requested:
        stream_id = stream_tracker.start(
            "direct",
            source,
            detail=f"full file, {file_size} bytes",
        )

        def whole_file():
            with source.open("rb", buffering=0) as fh:
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    stream_tracker.add_bytes(stream_id, len(chunk))
                    yield chunk

        response = Response(
            stream_with_context(whole_file()),
            status=200,
            mimetype=mimetype,
            direct_passthrough=True,
        )
        response.headers["Content-Length"] = str(file_size)
        response.headers["Accept-Ranges"] = "bytes"
        response.headers["Cache-Control"] = "private, max-age=0"
        response.call_on_close(lambda: stream_tracker.stop(stream_id))
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
        with source.open("rb", buffering=0) as fh:
            fh.seek(start, os.SEEK_SET)
            while remaining > 0:
                chunk = fh.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                stream_tracker.add_bytes(stream_id, len(chunk))
                yield chunk

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
    response.call_on_close(lambda: stream_tracker.stop(stream_id))
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


@app.route("/thumb/<int:root_index>/<path:relpath>")
def thumbnail(root_index, relpath):
    root = get_root(root_index)
    source = safe_path(root, relpath)
    if not is_video(source):
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
    if not is_video(source):
        abort(404)

    if kind == "thumbs":
        found = best_cached_image(root_index, root, source, "thumbs")
        out = cache_path("thumbs", root_index, root, source)
        width, priority = 360, 10
    else:
        out = cache_path("posters", root_index, root, source)
        found = out if out.exists() else None
        width, priority = 960, 0

    current = generation == image_scheduler.current_generation()
    if not found and current:
        image_scheduler.submit(generation, priority, source, out, width)
    return jsonify({"ready": bool(found), "current": current})


def transcode_command(
    backend: str,
    source: Path,
    audio_index: int | None,
    start: float,
    low_bandwidth: bool = False,
) -> list[str]:
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error"]

    if backend == "vaapi":
        cmd += ["-vaapi_device", VAAPI_DEVICE]

    cmd += [
        "-ss", f"{max(0.0, start):.3f}",
        "-i", str(source),
        "-map", "0:v:0",
    ]

    if audio_index is not None:
        cmd += ["-map", f"0:{audio_index}"]
    else:
        cmd += ["-an"]

    if low_bandwidth:
        scale = (
            f"scale={LOW_BANDWIDTH_WIDTH}:{LOW_BANDWIDTH_HEIGHT}:"
            "force_original_aspect_ratio=decrease:force_divisible_by=2"
        )
        sw_filter = f"{scale},format=nv12"
        x264_filter = f"{scale},format=yuv420p"
        vaapi_filter = f"{scale},format=nv12,hwupload"
    else:
        sw_filter = "format=nv12"
        x264_filter = "format=yuv420p"
        vaapi_filter = "format=nv12,hwupload"

    if backend == "vaapi":
        cmd += ["-vf", vaapi_filter, "-c:v", "h264_vaapi"]
        if low_bandwidth:
            cmd += [
                "-rc_mode", "CBR",
                "-b:v", LOW_BANDWIDTH_BITRATE,
                "-maxrate", LOW_BANDWIDTH_BITRATE,
                "-bufsize", LOW_BANDWIDTH_BITRATE,
            ]
        else:
            cmd += ["-qp", "24"]
        cmd += ["-bf", "0", "-g", "48"]

    elif backend == "qsv":
        cmd += [
            "-vf", sw_filter,
            "-c:v", "h264_qsv",
            "-preset", "veryfast",
            "-look_ahead", "0",
        ]
        if low_bandwidth:
            cmd += [
                "-b:v", LOW_BANDWIDTH_BITRATE,
                "-maxrate", LOW_BANDWIDTH_BITRATE,
                "-bufsize", LOW_BANDWIDTH_BITRATE,
            ]
        else:
            cmd += ["-global_quality", "24"]
        cmd += ["-bf", "0", "-g", "48"]

    elif backend == "nvenc":
        cmd += [
            "-vf", sw_filter,
            "-c:v", "h264_nvenc",
            "-preset", "p3",
            "-tune", "ll",
        ]
        if low_bandwidth:
            cmd += [
                "-rc", "cbr",
                "-b:v", LOW_BANDWIDTH_BITRATE,
                "-maxrate", LOW_BANDWIDTH_BITRATE,
                "-bufsize", LOW_BANDWIDTH_BITRATE,
                "-multipass", "disabled",
            ]
        else:
            cmd += ["-rc", "vbr", "-cq", "23", "-b:v", "0"]
        cmd += ["-bf", "0", "-g", "48"]

    elif backend == "amf":
        cmd += [
            "-vf", sw_filter,
            "-c:v", "h264_amf",
            "-quality", "speed",
        ]
        if low_bandwidth:
            cmd += [
                "-rc", "cbr",
                "-b:v", LOW_BANDWIDTH_BITRATE,
                "-maxrate", LOW_BANDWIDTH_BITRATE,
                "-bufsize", LOW_BANDWIDTH_BITRATE,
                "-filler_data", "true",
            ]
        else:
            cmd += ["-rc", "cqp", "-qp_i", "24", "-qp_p", "24"]
        cmd += ["-bf", "0", "-g", "48"]

    else:
        cmd += [
            "-vf", x264_filter,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-tune", "zerolatency",
        ]
        if low_bandwidth:
            cmd += [
                "-b:v", LOW_BANDWIDTH_BITRATE,
                "-maxrate", LOW_BANDWIDTH_BITRATE,
                "-bufsize", LOW_BANDWIDTH_BITRATE,
                "-x264-params", "nal-hrd=cbr:force-cfr=1",
            ]
        else:
            cmd += ["-crf", "23"]
        cmd += ["-bf", "0", "-g", "48"]

    if audio_index is not None:
        cmd += [
            "-c:a", "aac",
            "-b:a", "128k" if low_bandwidth else "160k",
            "-ac", "2",
        ]

    cmd += [
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof+omit_tfhd_offset",
        "-frag_duration", "500000",
        "-flush_packets", "1",
        "-f", "mp4", "pipe:1",
    ]
    return cmd


@app.route("/transcode/<int:root_index>/<path:relpath>")
def transcode(root_index, relpath):
    root = get_root(root_index)
    source = safe_path(root, relpath)
    if not is_video(source):
        abort(404)

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
        start = 0.0

    low_bandwidth = request.args.get("low", "").strip().lower() in {"1", "true", "yes", "on"}

    audio_index = None
    if request.args.get("audio"):
        try:
            audio_index = int(request.args["audio"])
        except ValueError:
            abort(400)

    cmd = transcode_command(transcoder["key"], source, audio_index, start, low_bandwidth)
    app.logger.info("Starting %s transcode: %s", transcoder["label"], " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)

    stream_id = stream_tracker.start(
        transcoder["key"],
        source,
        start_offset=start,
        detail=(
            transcoder["label"]
            + (
                f" / Low bandwidth {LOW_BANDWIDTH_WIDTH}x{LOW_BANDWIDTH_HEIGHT}"
                if low_bandwidth
                else ""
            )
        ),
        target_bitrate=(
            human_bitrate(LOW_BANDWIDTH_TOTAL_BPS)
            if low_bandwidth
            else None
        ),
        quality_mode="low" if low_bandwidth else "normal",
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
            # Browser seeks/navigation normally abandon the current HTTP stream.
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

            # Only report failures that were not caused by client cancellation.
            if not cancelled and proc.returncode not in (0, None):
                app.logger.error(
                    "FFmpeg transcode exited %s: %s",
                    proc.returncode, stderr.decode("utf-8", "replace")[-4000:]
                )

    response = Response(stream_with_context(generate()))
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
        "version": VERSION,
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
    return render_template("status.html")


@app.route("/api/transcoder-status")
def transcoder_status_api():
    refresh = request.args.get("refresh", "").strip().lower() in {"1", "true", "yes"}
    return jsonify(transcoder_status_payload(force_refresh=refresh))


@app.errorhandler(404)
def not_found(_):
    return render_template("error.html", message="That item was not found."), 404


@app.errorhandler(403)
def forbidden(_):
    return render_template("error.html", message="Access outside the configured media roots is not allowed."), 403


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=SERVER_PORT, threaded=True)
