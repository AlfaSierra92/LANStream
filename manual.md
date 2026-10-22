# LANStream user manual

This manual covers installation, everyday use and maintenance.
The [configuration reference](docs/configuration.md) lists exact settings;
the [architecture guide](docs/architecture.md) explains internal data flow.

## Contents

1. [What LANStream does](#what-lanstream-does)
2. [Requirements](#requirements)
3. [Docker installation](#docker-installation)
4. [Running from source](#running-from-source)
5. [Windows installation](#windows-installation)
6. [Settings and first use](#settings-and-first-use)
7. [Access modes and profiles](#access-modes-and-profiles)
8. [Browsing libraries](#browsing-libraries)
9. [Video playback](#video-playback)
10. [Quality and codecs](#quality-and-codecs)
11. [Hardware acceleration](#hardware-acceleration)
12. [HDR and tone mapping](#hdr-and-tone-mapping)
13. [Audio and subtitles](#audio-and-subtitles)
14. [Integrated NZBDAV](#integrated-nzbdav)
15. [STRM files](#strm-files)
16. [Images, documents, comics and text](#images-documents-comics-and-text)
17. [Caches and storage](#caches-and-storage)
18. [Playback diagnostics](#playback-diagnostics)
19. [Updates, shutdown and backups](#updates-shutdown-and-backups)
20. [Operational limits](#operational-limits)

## What LANStream does

The **server** runs LANStream, reads media and converts it when necessary.
The **client** is the browser displaying folders and playing the result.
A phone or television does not need direct access to the server's filesystem.

Configured folders, called **roots**, appear as libraries on the home page.
Folder names and layout are preserved. LANStream does not download titles,
posters, plots or recommendations and does not require a collection database.

Opening a video runs FFprobe to read duration, container, codecs, resolution and
tracks. The player then requests original delivery or transcoding.
During transcoding, FFmpeg produces output while the browser receives it; a whole
converted movie is not prepared first.

Local libraries also support images, PDF, CBZ/CBR and text.
Integrated NZBDAV supports remote folders and video files.
There is no persistent watch history or background collection-wide indexing.
RAM caches and live metrics disappear when the server process stops.

## Requirements

### Server

A built Windows package includes the compiled application and the external tools
distributed with that release. The Docker image installs Linux dependencies,
FFmpeg/FFprobe and 7-Zip.

Running from source requires Python compatible with the project syntax
(Python 3.10 or later), the appropriate requirements file and accessible
FFmpeg/FFprobe tools. Individual dependency compatibility also depends on the
Python version and platform.

The process account must be able to read every published folder.
SMB/NFS shares and external drives must already be accessible to the server's
operating system. Browser access to a share is not sufficient.

Hardware acceleration requires compatible hardware, drivers, FFmpeg and device
permissions. CBR requires a working 7-Zip backend; CBZ uses Python's ZIP support.
CPU HDR conversion additionally requires FFmpeg zscale/libzimg and tonemap.
VAAPI HDR conversion requires tonemap_vaapi and driver HDR processing support.

### Client

Use a modern browser with HTML5 video and JavaScript enabled.
Codec playback, fullscreen, frame metrics and native HLS vary by browser/device.
LANStream does not bundle a JavaScript HLS engine for universal HLS playback.

The client must reach the server's address and published port.
The server must reach any NZBDAV or STRM source.
Native HLS may require the client to reach the remote URL directly.

## Docker installation

### Prepare persistent directories

From the project directory:

```bash
docker build -t lanstream:local .
mkdir -p runtime/config runtime/cache
cp examples/config-local.txt runtime/config/config.txt
```

Edit the copied file rather than distributing an existing personal config.txt.
The [examples](examples/) contain placeholders only.

The local example lists /media/Movies and /media/TV. If /srv/media is mounted at
/media, create those directories as /srv/media/Movies and /srv/media/TV on the host,
or adjust the example to match existing directories.

### Start the container

```bash
docker run -d --name lanstream \
  --restart unless-stopped \
  -p 8060:8080 \
  -e LANSTREAM_CONFIG_FILE=/config/config.txt \
  -e LANSTREAM_CACHE_DIR=/cache \
  --mount type=bind,src="$(pwd)/runtime/config",dst=/config \
  --mount type=bind,src="$(pwd)/runtime/cache",dst=/cache \
  --mount type=bind,src=/srv/media,dst=/media,readonly \
  lanstream:local
```

Mounting the configuration **directory** is intentional: Settings saves by atomic
file replacement. Binding only the individual config.txt can prevent that operation.
Configuration must be writable; media can remain read-only.

The included Gunicorn configuration binds to 0.0.0.0:8080 with one worker and eight
threads. Mapping 8060:8080 publishes host port 8060.
Open http://SERVER-IP:8060, or http://127.0.0.1:8060 on the server itself.

The Dockerfile does not copy your config.txt or automatically redirect the graphics
cache to /cache. The environment variables and mounts above connect those resources.

### Expose hardware

For VAAPI, add this Docker option before the image name:

```text
--device /dev/dri:/dev/dri
```

The default device is /dev/dri/renderD128. Override it with
LANSTREAM_VAAPI_DEVICE if another node is needed.
If running as a custom user, also verify device permissions and group membership.
Making the device visible does not by itself make it usable.

For NVENC, the Docker host needs NVIDIA container support and the container must
receive the GPU, for example through --gpus all in an already-configured environment.
FFmpeg and the installed driver must agree on the encoder API.

For an NZBDAV-only installation, omit the local media mount and configure WebDAV.
No rclone mount is needed.

### Ports

The port setting in config.txt controls launcher/Flask startup, not Gunicorn.
Changing port= does not change Docker's published port.
Modify the host mapping or Gunicorn bind as appropriate.

## Running from source

Install system FFmpeg/FFprobe and, for CBR, 7-Zip.
On Linux, from the source directory:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-linux.txt
```

Prepare config.txt with real server paths. Start the Waitress launcher:

```bash
python launcher.py
```

The console prints the addresses and selected backend.
The default port is 8060, and the server listens on all interfaces.

python app.py starts Flask's development server instead.
For a continuing service, use the launcher or included Gunicorn configuration.
Starting Gunicorn with its supplied configuration uses port 8080 independently
of port= in config.txt.

Tools are found beside the application first, then on PATH.
Set environment variables before starting the process.
On Windows source installations, use requirements.txt.

## Windows installation

Extract the entire distribution. Keep LANStream.exe, _internal, ffmpeg.exe,
ffprobe.exe and any supplied 7-Zip files in their expected positions.
LANStream.exe is not a standalone single-file application.

Start the executable and leave the console open.
Open the displayed address and allow the appropriate local-network firewall access
when required.

Configure paths belonging to the Windows server, such as D:\Movies or
\\NAS\Media\TV. UNC shares must be readable by the process account.
A service running as another account may not see your desktop's mapped drives.

Editable config.txt lives beside the executable. Bundled templates/scripts remain
inside the application's resources.
For build instructions see [README-WINDOWS.txt](README-WINDOWS.txt).

## Settings and first use

Settings controls the launcher/Flask port, preferred backend, default player
quality, access mode, roots and profile passwords/exclusions.

Paths must be absolute server paths; Docker paths refer to the container.
Do not put URLs or configuration directives into folder fields.

Blank password fields preserve existing passwords.
The first profile administers the server. Removing profiles or switching access
modes replaces the corresponding library layout; inspect the intended result.

Saving uses a concurrent-change check and atomic file replacement.
If another editor changed the file after you opened Settings, reload the page.
Comments, unknown options and existing NZBDAV fields are preserved, although the
file's formatting/layout may change.

Port changes need a restart. Docker mappings must be changed separately.
Environment overrides continue to win over matching file values.
Manual edits to shared authentication/backend settings also require a restart
to update values initialized at startup.
See [when changes apply](docs/configuration.md#when-changes-take-effect).

## Access modes and profiles

### Passwordless access

With no profile sections and use_master_password=false, anyone reaching the server
can access libraries, Settings and configured NZBDAV.

### Shared password

With use_master_password=true and a nonempty master_password, all users enter the
same password and share libraries and administrative rights.
A stored password alone does not activate authentication.

### Password profiles

Each section begins with its password in angle brackets. There are no usernames.

```text
<CHANGE_THIS_ADMIN_PASSWORD>
/media/Movies
/media/TV

<CHANGE_THIS_GUEST_PASSWORD>
/media/Movies
-/Private
```

The first profile administers Settings and NZBDAV.
The guest sees Movies without the Private subtree.
Content routes enforce exclusions even when opened through a direct URL.

Relative exclusions apply to the last positive root, with absolute-path handling
described in the [configuration reference](docs/configuration.md#profiles-and-exclusions).
Use distinct nonempty passwords without angle brackets, control characters or
surrounding whitespace.

Passwords are stored in plain text in the configuration.
Browser sessions use signed cookies. With the default random session secret,
restarting the process normally requires logging in again.
Logout clears that browser's session.

Profiles restrict media access, but Activity is a server-wide view that can expose
stream names, paths and clients to authenticated users.
Profiles are not complete isolation between mutually untrusted users.

## Browsing libraries

The home page lists the current profile's roots and, for administrators, configured
NZBDAV. Open a root, navigate folders and use breadcrumbs to move upward.

Names use natural sorting: item 2 appears before item 10.
The page filter searches the directory already displayed.
It is not a recursive collection search or a database query.

| Type | Recognized extensions |
| --- | --- |
| Video/pointers | .mp4, .m4v, .mov, .mkv, .webm, .avi, .mpg, .mpeg, .m2ts, .mts, .ts, .wmv, .flv, .ogv, .strm |
| Images | .jpg, .jpeg, .png, .webp, .gif, .bmp, .tif, .tiff, .avif |
| Documents | .pdf |
| Comics | .cbz, .cbr |
| Text | .txt, .md, .markdown |

An extension selects a viewer; it does not guarantee valid or decodable contents.
Symbolic links are resolved during access checks. A link cannot grant access
outside the requested root or bypass an excluded subtree.

## Video playback

Opening a video displays technical information and an initial quality choice.
Start playback to open the player. Overlay controls hide after inactivity and
reappear on interaction.

Controls include play/pause, 30-second backward/forward jumps, seek, volume,
fullscreen, settings and technical information.

| Control | Action |
| --- | --- |
| Space | Play/pause outside input fields or buttons handling the key. |
| F | Toggle fullscreen when permitted by the browser. |
| I | Toggle Playback info. |
| Escape | Close open panels; the browser may also exit fullscreen. |
| Gear | Open quality, codec and track controls. |

### Direct playback

Auto uses conservative compatibility rules:

| Source | Video | Audio |
| --- | --- | --- |
| MP4, M4V, MOV | H.264 | AAC, MP3 or no audio |
| WebM | VP8, VP9, AV1 | Opus, Vorbis or no audio |
| Recognized remote HLS | H.264 | AAC, MP3 or no audio; native browser HLS required |

MKV normally triggers conversion even if a particular browser can play some MKV
variants. These rules do not exhaustively identify every client capability.
If an Auto direct attempt fails, the player tries transcoding.

Local delivery supports HTTP byte ranges for seeking.
Compatible remote MP4/WebM STRM targets are relayed through the server.
They do not automatically enter the NZBDAV block cache.

### Transcoded playback and seeking

FFmpeg selects the first video stream and requested audio track, applies output
settings and writes fragmented MP4 directly into the HTTP response.

There is no separate audio-only conversion path that always copies video:
requesting conversion can re-encode video as well.

If a seek destination is already buffered in the browser, the player reuses it.
A seek outside that buffer can start a new FFmpeg request at the new position.
Rapid unbuffered seeks are coalesced for about 200 ms.
Actual latency still depends on reading and decoding the source.

Unknown-duration/live sources have seeking disabled.
Positions are not stored as persistent watch history.

## Quality and codecs

| Mode | Effect |
| --- | --- |
| Auto | Original where compatible, otherwise H.264/AAC unless explicit codecs are selected. |
| Original · no transcoding | Original bytes, with no conversion fallback. |
| Source resolution · transcode | Convert at original resolution without an explicit video bitrate ceiling. |
| 360p–4K preset | Convert within the preset's dimensions/bitrate. |
| Custom | Apply the selected limits after Apply quality. |

Original disables controls requiring audio/codec conversion.
It does not make an incompatible file playable.

| Preset | Maximum dimensions | Video target | Audio target |
| --- | --- | --- | --- |
| 4K | 3840 × 2160 | 20,000 Kbps | 192 Kbps |
| 1440p | 2560 × 1440 | 12,000 Kbps | 192 Kbps |
| 1080p | 1920 × 1080 | 8,000 Kbps | 192 Kbps |
| 720p | 1280 × 720 | 4,000 Kbps | 128 Kbps |
| 480p | 854 × 480 | 1,500 Kbps | 96 Kbps |
| 360p | 640 × 360 | 700 Kbps | 96 Kbps |

Limited scaling preserves display aspect ratio, uses even dimensions and avoids
upscaling. A widescreen movie is not stretched to fill a 16:9 rectangle.

Custom accepts maximum heights 240, 360, 480, 720, 1080, 1440 or 2160,
video 300–40,000 Kbps, and audio 96, 128, 160, 192 or 256 Kbps.
Targets are encoder settings, not exact total network limits:
audio, muxing and delivery bursts also contribute.

Auto transcoding uses 160 Kbps audio; source-resolution conversion uses 192 Kbps.
Original preserves the source bitrate. Save a default in Settings/default_quality.

The video menu offers H.264, HEVC/H.265 and AV1; audio offers AAC, Opus and MP3.
Opening settings checks server encoders and the browser's declared MP4 support.
Unavailable options are disabled.

An explicit codec forces conversion. Switching quality, codec or audio preserves
position and paused state but may open a new transcoding response.

Unavailable VAAPI output codecs do not fall back to CPU.
Other backends may offer software HEVC/AV1 when their hardware test fails.
Playback info reports the selected encoder. Software HEVC/AV1 can be demanding.

Historical low_bandwidth_resolution/low_bandwidth_bitrate settings no longer
control quality. The old low=1 URL query maps to 480p.

## Hardware acceleration

Auto selection tests QSV, NVENC, AMF and software on Windows.
On Linux it tests VAAPI when the device exists, then NVENC and software.
Startup results are cached; HEVC/AV1 are tested separately when needed.

Use a backend appropriate to the platform and verify the result in Activity.
After driver/tool changes, press Retest transcoder backends.
See [selection caveats](docs/configuration.md#transcoder-selection) for explicit
preferences missing from a platform's test list.

The VAAPI video path is:

```text
compressed packets → VAAPI decoder → GPU frames
                   → HDR10 tone mapping when needed
                   → scale_vaapi → VAAPI encoder → fragmented MP4
```

Decoder, filters and encoder share a device.
Main 10/P010 to NV12 conversion and resizing run on the GPU.
The playback path does not use CPU scale/format or hwupload/hwdownload.
Metadata-only filters can adjust/remove frame tags without downloading surfaces.

Audio, demuxing, muxing, networking and application work still use CPU.
Short synthetic encoder tests intentionally upload CPU-generated frames.
The HDR capability test instead decodes a tiny bundled compressed HDR10 frame.

A vainfo VLD entry indicates decoding support; EncSlice indicates encoding.
Neither proves every processing operation.
Check the same device, permissions and FFmpeg environment used by the server.

QSV/NVENC/AMF currently keep decoding and filters in software and accelerate
encoding. Their tone mapping also uses CPU.

## HDR and tone mapping

Transcoding recognized HDR produces **SDR BT.709** automatically.
This applies to H.264, HEVC and AV1 outputs: there is no HDR-preserving transcode mode.
SDR input, including 10-bit SDR, is not identified as HDR just from bit depth.

PQ/smpte2084 identifies HDR10-style input.
HLG/arib-std-b67 is handled by the software processing path.
VAAPI uses tonemap_vaapi for PQ and rejects HLG rather than silently using CPU.
Alternatively, set tone_mapping=libplacebo-bt2390, libplacebo-mobius or
libplacebo-reinhard in config.txt. These modes accept PQ/HLG and map GPU surfaces
through Vulkan for tone mapping, back to VAAPI for scaling and encoding.
The same in-memory capability check verifies the complete selected chain before
playback. Missing Vulkan/VAAPI interoperability causes an error, with no CPU
fallback. See the [configuration reference](docs/configuration.md#tone-mapping).
OpenCL alternatives are opencl-mobius, opencl-reinhard and opencl-hable. These
avoid Vulkan/libplacebo, require an OpenCL GPU runtime with VAAPI surface sharing,
and support PQ/HLG with BT.2020 non-constant-luminance or BT.709 primaries/matrix.
The pipeline converts input surfaces to P010 on VAAPI, tone-maps in OpenCL and
maps back to VAAPI output surfaces for scaling/encoding, without CPU video filters.

The GPU path tone-maps before converting/scaling to NV12 for encoding.
The CPU path linearizes to floating-point RGB with zscale, converts the gamut,
applies Hable and converts to limited-range BT.709.
Output color tags are set explicitly and stale HDR/Dolby frame side data is removed.

Before the first HDR request on a backend, a cached capability check uses a tiny
compressed HDR10 frame with mastering/content-light information, held in RAM.
It validates required filters and performs an actual short encode.
Missing filters or failed driver processing cause an explanatory HTTP 503.
Activity's retest clears this cache.

This synthetic check does not certify every input resolution or metadata variant.
VAAPI tonemap_vaapi requires mastering-display information in the decoded HDR10
frames; unsuitable sources can still fail at runtime.
The GPU's HEVC Main 10 decoder alone is not enough.

Dolby Vision Profile 7 and compatible Profile 8 sources can use an ordinary
HDR10 base layer. HLG-compatible Profile 8 can use libplacebo, OpenCL or the CPU HLG path.
Dynamic Dolby Vision metadata/enhancement layers are not reconstructed.
Unsupported proprietary profiles, including Profile 5, are rejected for conversion.
Do not infer support from a filename containing HDR or DV.

Original/direct delivery retains source bytes and leaves HDR to the client.
A compatible HDR client may therefore be preferable when retaining the source's
HDR rendition matters. Playback info distinguishes original delivery from the
active SDR mapping path.

## Audio and subtitles

### Audio

Embedded tracks are listed with language, codec and title when available.
Choosing another track normally triggers conversion.
Converted audio is stereo at 48 kHz; Atmos, multichannel layout or the original
compressed bitstream is not automatically retained.

Original delivery preserves source audio bytes, subject to client support.

### Text subtitles

Supported embedded text codecs include SubRip/SRT, ASS, SSA, WebVTT, MOV_TEXT
and text. The browser receives WebVTT and overlays captions.
Subtitles are not burned into transcoded video.

There is no OCR for PGS/VobSub and no external SRT upload control.
WebVTT conversion does not preserve all ASS styling/animation.
MOV_TEXT keeps text without optional sample styling extensions.

The browser loads overlapping 20-second windows and prefetches the next one.
Seeking/changing tracks cancels obsolete browser requests and ignores stale results.
Old cues are removed; known end times of long cues are retained.

The reader uses timestamps from other streams to bound silent subtitle gaps.
Common MKV/MOV_TEXT packets are read by byte offset; unusual layouts use a bounded
packet-reader fallback. A small subtitle-only container is built in RAM and sent
to FFmpeg, instead of reopening the whole movie for conversion.

A request has a 30-second deadline. HTTP 504 causes a notice and a delayed retry;
video continues independently. Subtitle selection does not restart video.

## Integrated NZBDAV

### Connect directly

Set these values in the active configuration:

```ini
nzbdav_url=http://nzbdav.example:3000/
nzbdav_user=WEBDAV_USER
nzbdav_password=WEBDAV_PASSWORD
nzbdav_root=content
nzbdav_ram_cache_mb=256
```

Use the final WebDAV endpoint, not a web UI or rclone RC port.
Do not embed credentials in the URL.
Root is relative to the endpoint; empty exposes its root.
The example content directory must match the actual server layout.

An empty URL disables integration.
Only the administrator sees/accesses this library, including in profile mode.
The integration lists folders and videos, not remote STRM pointers or the other viewers.

The backend must support PROPFIND, HEAD, Content-Length and byte ranges.
The client does not follow redirects or use environment proxies.
Use a directly reachable final endpoint.

NZBDAV fields are currently edited in config.txt, not a dedicated Settings form.
Settings preserves them when saving other options.
Changes recreate the remote service/cache at the next NZBDAV access and interrupt
streams using the old service.

### Bounded block cache

The cache stores 4 MiB blocks shared by original delivery, FFprobe, transcoding
and subtitle extraction. Least recently used blocks can be evicted even while
their file is open.

A 60 GiB movie can play through a 256 MiB cache.
As playback progresses, new blocks replace old ones.
Seeking far backward downloads evicted blocks again.

This is not an in-memory recording of the whole movie.
It does not guarantee every seek is a cache hit.
The budget includes stored blocks and reservations for pending reads.

Python, FFmpeg, metadata, sockets and small delivery copies use additional memory.
The limit is per worker: four 256 MiB workers can reserve approximately 1 GiB
for blocks alone. The included deployment uses one worker.

Accepted budgets are 8–16384 MiB, rounded down to whole blocks.
A slow/paused browser holds at most a small delivery copy rather than pinning
a complete block during a network write.

### HTTP and internal gateway

The client reuses HTTP/TLS connections and cookies.
Concurrent readers of the same block share its fetch.
A backend ignoring Range is rejected instead of causing an unbounded full download.

FFmpeg/FFprobe read through an internal loopback HTTP gateway on an ephemeral port,
protected by random tokens. Do not publish that port.
Tokens are not WebDAV passwords. Browser URLs continue through LANStream's
normal access-controlled routes.

Initial probing and cache misses still depend on upstream latency/throughput.
The cache cannot make an insufficiently fast source deliver data faster.

### Keeping an existing rclone mount

Local libraries can still point to FUSE mounts.
LANStream does not start or stop rclone automatically.
tools/mount_nzbdav.sh is a separate optional helper.

It keeps VFS disk cache off and uses per-file RAM buffering.
Those buffers are not the direct integration's shared block cache.
See [optional rclone](docs/configuration.md#optional-rclone-mount).

## STRM files

A .strm file contains exactly one HTTP/HTTPS URL or local video path:

```text
https://media.example/video.mp4
```

UTF-8, BOM, blank lines, # comment lines and Windows line endings are accepted.
Multiple targets, control characters and content over 65,536 characters are rejected.

A local target can be absolute or relative to the STRM file's directory.
It must resolve beneath an authorized root and respect exclusions.
Nested STRM pointers, FFmpeg protocols, plugin URLs and custom HTTP-header suffixes
such as |User-Agent=... are unsupported.

Compatible remote MP4/WebM is relayed with Range support.
Native HLS may redirect the browser to the upstream URL.
Transcoding reads the remote target through FFmpeg.
Expired URLs must be updated in the pointer file.

Only use trusted pointers: the server makes requests to their target addresses.

## Images, documents, comics and text

Images support previous/next navigation within the folder, arrow keys and horizontal
swipes. Fullscreen navigation can keep the view open; the mouse wheel switches
images in that mode.

TIFF is converted to a cached JPEG for compatibility.
Other image formats are normally sent to the browser; decoding support varies.

PDF/comics are rendered as page images.
Use buttons, arrows, PageUp/PageDown or horizontal swipes.
The viewer also prepares the next page.
It is not a PDF editor with selectable text, search or annotation editing.

CBZ is a ZIP archive of supported images; CBR is read using 7-Zip.
Pages use natural ordering. AVIF is excluded from comic page images by this reader.

TXT/MD/Markdown is read as UTF-8 with invalid characters replaced and shown as
escaped plain text. HTML and Markdown are not executed/rendered.
The whole text file is read, so avoid extremely large text documents.

## Caches and storage

| Data | Location | Lifetime |
| --- | --- | --- |
| NZBDAV blocks | Worker RAM | Until eviction, reconfiguration or restart. |
| FFprobe metadata | Worker RAM | Up to 128 entries, five-minute TTL, file-version invalidation. |
| Stream statistics | Worker RAM | Active requests and limited recent history. |
| Subtitle windows | Server/browser RAM | Bounded windows; no temporary movie files. |
| Image/PDF/comic thumbnails | Graphics cache | Persistent until removed. |
| Rendered document pages/TIFF | Graphics cache | Persistent until removed. |
| Configuration | Active config.txt | Persistent. |

The graphics cache defaults to cache beside the application, unless
LANSTREAM_CACHE_DIR overrides it. It has no automatic global size limit/expiration.
nzbdav_ram_cache_mb does not control it.

No video posters/thumbnails or folder preview scans are generated.
Old derivatives are not deleted automatically.
Graphics cache keys use paths; replacing a source under the same name may leave
a stale derivative. Stop the server, remove only the affected cached derivative
and reopen the source to regenerate it. Do not delete original media.

## Playback diagnostics

Playback info (I) describes the current player request.
Activity at /transcoder-status describes the server.

The player shows method/reason, output codecs, displayed dimensions, bitrate
targets, actual encoder, tone-mapping path and subtitles.
It also shows buffer ahead, displayed FPS, dropped frames, transfer rate,
request bytes and original source properties.

Metrics refresh about once per second while the panel is visible.
A dash means a value is unavailable, not necessarily an error.
FPS is displayed frames over wall time, not FFmpeg's maximum encode speed.
Transfer rate measures HTTP delivery, including buffer filling, not source bitrate
or NZBDAV upstream speed.

Activity shows active requests, clients, transferred bytes, starting positions,
elapsed request time and backend tests. Administrators also see NZBDAV cache counters.
A response can finish while the browser keeps playing buffered data.
An empty active list does not necessarily mean no video is playing.

Low GPU usage alone does not prove software encoding.
Inspect the command, video engines, buffer and dropped frames together.
See [troubleshooting](docs/troubleshooting.md).

## Updates, shutdown and backups

Stop the launcher with Ctrl+C or Docker with docker stop lanstream.
Managed FFmpeg/FFprobe children and remote connections are closed.
The included Gunicorn setup has a five-second graceful shutdown period.
Retain its cleanup hooks if changing the service command/configuration.

For Docker updates, rebuild the image from updated source and recreate the container
with the same mounts, ports, environment and GPU access.
Restarting an old image does not incorporate new code.

For source updates, stop the process, update source/dependencies and restart.
On Windows, replace the complete distribution while retaining your configuration.
Reload open players after script/template changes.
Deploy Python, templates and static files together; the image must include
tone_mapping.py as well as the media/subtitle modules.

Back up the active config.txt and deployment details: mounts, ports, environment
and devices. Graphics cache backup is optional because it can be regenerated.
RAM caches need no backup. There is no media database to export.
Protect backups containing passwords/WebDAV credentials.
Original media needs an independent backup policy.

## Operational limits

LANStream targets trusted local networks. It has no built-in TLS, multifactor login,
per-user resource quotas or complete user isolation.
Activity can reveal file names, paths and clients; passwords are plain-text settings.

A RAM-only application video cache does not prevent operating-system swap.
Logs, Python bytecode, configuration, graphics cache and deployment tools may
still write to disk. No temporary movie output is created by the playback path.

Concurrent streams share GPU, CPU, network, threads and cache.
Capacity depends on the machine and sources; there is no fixed stream-count
guarantee. Browser compatibility, supported HDR base layers and driver processing
remain constraints even when the encoder itself is available.
