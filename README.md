# LANStream

LANStream is a web server for browsing folders and playing media on a local
network. The server reads your files and delivers them to a browser on a computer,
phone or tablet. Client devices do not need a dedicated LANStream application.

Your existing folder structure is the library. There is no media database,
initial import, collection-wide scan or metadata matching service.

## What it does

| Feature | Behavior |
| --- | --- |
| Local libraries | Exposes multiple server folders, external drives and shares already accessible to the operating system. |
| Integrated NZBDAV | Browses remote video folders over WebDAV without rclone or FUSE. |
| Video playback | Delivers original bytes, remuxes original video with converted audio, or re-encodes video with FFmpeg while it plays. |
| Quality controls | Auto, Original, original video with audio-only conversion, source-resolution transcoding, 360p–4K presets and custom targets. |
| Hardware processing | VAAPI keeps video decoding, resizing, pixel conversion, HDR10 tone mapping and encoding on the GPU. |
| Audio | Selects embedded tracks; transcoding outputs stereo at 48 kHz in AAC, Opus or MP3. |
| Subtitles | Converts supported embedded text tracks to WebVTT in overlapping time windows. |
| STRM pointers | Opens one authorized local video path or HTTP/HTTPS URL. |
| Other viewers | Displays images, PDF, CBZ/CBR comics and plain text/Markdown files. |
| Access control | Supports passwordless access, a shared password or password profiles with separate folders and exclusions. |
| Web settings | Edits local libraries, access, playback defaults and NZBDAV URL/credentials/root/RAM budget; saves to config.txt. |
| Diagnostics | Shows encoders, active requests, transfer statistics, browser buffer and NZBDAV cache counters. |

Media sources are read-only. LANStream can write its configuration and generated
image/document derivatives to its own directories.

## Documentation

- [Complete installation and user manual](manual.md).
- [Configuration keys, profiles and environment variables](docs/configuration.md).
- [Architecture, data flow and cache behavior](docs/architecture.md).
- [Troubleshooting GPU, HDR, buffering and subtitles](docs/troubleshooting.md).
- [Windows package and build instructions](README-WINDOWS.txt).
- [Linux Easy Config integration](INSTALL-LEC.txt).
- [Configuration examples without personal data](examples/).

The documentation describes the current source tree. Older packages and the
bundled LEC module may expose different controls.

## Quick start: Docker

From the project directory:

```bash
docker build -t lanstream:local .
mkdir -p runtime/config runtime/cache
cp examples/config-local.txt runtime/config/config.txt
```

Edit the copied file. Its paths must match paths **inside the container**.
For example, mounting host directory /srv/media at /media makes
/media/Movies refer to /srv/media/Movies on the host.

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

Ensure /srv/media/Movies and /srv/media/TV exist, or adjust the example.
Open http://SERVER-IP:8060. The example starts without authentication; choose
your access mode in **Settings**.

Mount the configuration **directory**, rather than just the file, so Settings can
replace config.txt atomically. Keep the configuration directory writable.

For VAAPI, add `--device /dev/dri:/dev/dri` before the image name and verify device
permissions. Select `transcoder=vaapi` and check the actual backend in Activity.
The default render device is /dev/dri/renderD128.

Gunicorn inside the image listens on **8080**. The published port above is **8060**.
Changing `port=` in config.txt does not change Gunicorn's bind or Docker's mapping.

## Quick start: Windows

1. Extract the complete release into a writable directory.
2. Start LANStream.exe and leave its console open.
3. Open the displayed address.
4. Use Settings to configure server folders, access, playback defaults and NZBDAV.

A built package requires neither Python nor Docker. Paths belong to the Windows
server, even when the browser runs on another device.
See [README-WINDOWS.txt](README-WINDOWS.txt).

## Choose a playback mode

| Quality choice | Video | Audio | Client requirement |
| --- | --- | --- | --- |
| Auto · original when possible | Original delivery where compatible; video conversion on fallback. | Original or converted with the stream. | Depends on the selected path. |
| Original · no transcoding | Original file bytes. | Original file bytes. | Supports the source container, video, audio and HDR. |
| Original video · convert audio only | H.264/HEVC/AV1 packets copied into MP4; no tone mapping. | Selected track converted; default AAC stereo, 48 kHz, 192 Kbps. | Supports the original video and HDR in MP4. |
| Source resolution / resolution preset / Custom | Re-encoded video; recognized HDR converted to SDR. | Selected track converted to stereo at 48 kHz. | Supports the chosen output codecs. |

To keep video unchanged and convert audio, open **Settings → Playback quality →
Default quality**, select **Original video · convert audio only**, and save.
Open a new player, or choose that same mode from an existing player's gear menu.
The file equivalent is `default_quality=audio_only`.

This mode does not need a video encoder or HDR-processing backend. It never
automatically switches to video encoding if the client rejects the video.
See [playback and seeking](manual.md#video-playback).

## NZBDAV without a disk video cache

Open **Settings → Access & media → NZBDAV · remote media**. Enter the WebDAV
URL, username/password, remote root and RAM cache budget, then save.
The equivalent fields in the server's active config.txt are:

```ini
nzbdav_url=http://nzbdav.example:3000/
nzbdav_user=WEBDAV_USER
nzbdav_password=WEBDAV_PASSWORD
nzbdav_root=content
nzbdav_ram_cache_mb=256
```

Use the real WebDAV endpoint and credentials, not rclone's RC endpoint.
The NZBDAV library is available to the administrator in profile mode; passwordless
and shared-password users also have administrative access. No rclone, FUSE or
tmpfs is required. A blank password field keeps the saved password; the removal
checkbox clears it. A blank URL disables the integration. Saving checks syntax
without contacting WebDAV; changes take effect at the next remote access and
can interrupt existing remote streams.

The RAM cache stores 4 MiB blocks, evicts least recently used blocks while files
remain open, and fetches evicted ranges again when needed. A movie can be much
larger than the cache. The per-worker budget includes in-flight block reservations,
not all Python, FFmpeg, socket or operating-system memory.

Image thumbnails, rendered document pages and TIFF conversions use a separate
persistent graphics cache. No video posters or video thumbnails are generated.
See [integrated NZBDAV](manual.md#integrated-nzbdav).

## HDR-to-SDR tone mapping

Video transcoding automatically converts recognized HDR input to SDR BT.709:

- **VAAPI:** HDR10/PQ uses tonemap_vaapi before GPU resizing and encoding.
  Optional libplacebo/Vulkan modes offer BT.2390, Mobius and Reinhard curves;
  OpenCL offers Mobius, Reinhard and Hable without requiring libplacebo/Vulkan;
  see [configuration](docs/configuration.md#tone-mapping).
- **Other backends:** PQ/HLG uses CPU zscale and Hable tone mapping; FFmpeg must
  include zscale/libzimg and tonemap.
- **Dolby Vision:** supported HDR10/HLG-compatible base layers can be used;
  Dolby Vision dynamic metadata and enhancement layers are not reconstructed.
  Unsupported profiles, including Profile 5, are rejected for conversion.
- **Original/direct playback:** bytes are unchanged; the client handles HDR.
- **Original video · convert audio only:** copies video without tone mapping;
  converts selected audio to AAC stereo by default. Save this as Default quality
  in Settings (default_quality=audio_only). The browser must support the source video/HDR.

SDR and 10-bit SDR sources are not tone-mapped. Playback info shows the selected
tone-mapping path. A cached, in-memory HDR test checks the required pipeline before
an HDR stream starts. VAAPI never silently downloads frames for CPU tone mapping.
HLG requires libplacebo or OpenCL when using VAAPI decoding/encoding; the default
mapper rejects it. The `tone_mapping` curve selector is currently file-only.
Vulkan/OpenCL require their own drivers and interoperability support; listing a
filter does not prove that the full pipeline works. See the
[HDR troubleshooting guide](docs/troubleshooting.md#hdr-conversion-fails-or-colors-look-wrong).

HDR processing support depends on the driver as well as FFmpeg. A successful
HEVC Main 10 decode test or a vainfo VLD entry does not prove tone-mapping support.

## Playback and operational limits

Transcoding produces fragmented MP4 directly to the HTTP response, without a
persistent converted movie. With VAAPI, video stays on the GPU. Audio, networking
and container handling still use CPU. QSV/NVENC/AMF currently accelerate encoding;
their decoding and video filters, including tone mapping, remain software.

Startup and seeking depend on the source, network, GPU, driver and browser.
There is no fixed latency guarantee. Increasing RAM cannot repair insufficient
upstream throughput.

LANStream is intended for a trusted LAN. Passwords do not provide TLS or complete
user isolation. Activity exposes server-wide stream information to authenticated
users; configuration passwords are stored in plain text. The application does not
include automatic cataloging, persistent watch history, recommendations or a
download manager.

## License

LANStream uses [The Unlicense](LICENSE). Dependencies retain their own licenses;
see [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).

