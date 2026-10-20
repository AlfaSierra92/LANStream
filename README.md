# LANStream

LANStream is a lightweight, database-free, web-based media browser and player for local networks.

It is designed for people who want to open a browser, peruse a media collection, and play videos without maintaining a proper database, importing a library, matching metadata, running background scans, or really putting any effort into it at all.

LANStream runs on Linux through Docker or as a portable Windows application. It supports direct playback whenever the browser can handle the source file and automatically falls back to hardware-accelerated or software transcoding when necessary.

<img width="1148" height="628" alt="lanstream1" src="https://github.com/user-attachments/assets/48dd5b48-0f33-4bf7-b06f-969c85575cfb" />

<img width="1135" height="510" alt="lanstream2" src="https://github.com/user-attachments/assets/a0907380-0f9b-4fc4-a9c7-f46515c714a5" />

<img width="1145" height="648" alt="lanstream3" src="https://github.com/user-attachments/assets/deb61a2c-8032-498f-adf6-efbf37a8e684" />

<img width="1252" height="798" alt="lanstream4" src="https://github.com/user-attachments/assets/d6109107-5a9f-45f2-b647-8da761586d75" />

## Why LANStream?

Most home media servers are built around a managed library. That is ideal if you want posters, metadata matching, watch history, recommendations, user profiles, and a streaming-service-style interface.

Not everybody needs all that.  If your media is already organized into folders and you simply want to poke around and play the files, a full media-management platform can be more effort than it's worth.

LANStream is

- **Database-free**  
  There is no media database to create, migrate, repair, or back up.
- **Scan-free**  
  LANStream does not need to index your entire collection before it becomes usable. Directories are read when you browse them.
- **Folder-native**  
  Your existing directory structure is the interface. There is no requirement to rename or reorganize files for metadata matching.
- **Read-only toward your media**  
  LANStream does not need to modify files in your media collection.
- **Portable**  
  The Windows build can be extracted and run as a self-contained application. The Linux version can run in Docker with configuration and cache files kept outside the container.
- **Low-maintenance**  
  There are no library refresh schedules, database upgrades, metadata agents, or background cataloging jobs to manage.
- **LAN-focused**  
  LANStream is optimized for simple local-network playback rather than trying to be an all-purpose cloud media ecosystem.

This makes it particularly useful for:

- large collections that are already organized by folder
- archive drives
- home-video collections
- temporary or portable media servers
- off-grid or popup media streaming
- shared household folders
- systems where simplicity and transparency matter more than rich metadata

## Features

- Browse ordinary folders directly from a web browser
- Multiple configurable media roots
- Client-side filtering of the currently displayed directory
- Lazy, cached thumbnails for images, PDFs and comics
- Video browsing without preview generation
- Dedicated browser-based video player
- STRM pointers to HTTP/HTTPS streams and local video files
- Extremely low seek latency and buffering times
- Audio & subtitle track selection
- Playback quality presets from 360p to 4K, plus custom resolution and video/audio bitrate controls

### STRM files

Place a `.strm` file in a configured media folder to show it alongside videos.
Save it as UTF-8 with one HTTP/HTTPS URL or one local video path, for example:

```text
https://media.example.com/movie.mp4?token=example
```

Local paths may be absolute or relative to the STRM file's folder. Targets must
remain inside the current user's configured media roots and respect exclusions,
including through symbolic links. Local videos use direct playback when compatible;
remote sources are transcoded by the server, which must have access to the URL.
Audio tracks, embedded text subtitles and quality selection work through the usual
player. Streams without a known duration play with seeking disabled.

Blank lines, `#` comments, Windows line endings and a UTF-8 BOM are accepted.
Multiple targets, nested STRM pointers, plugin URLs and URL suffixes for custom HTTP
headers (such as `|User-Agent=...`) are not supported. Unavailable or expired remote
URLs must be updated in the STRM file.

### Transcoding

Videos will be streamed directly to the browser when possible.  For formats unsupported by the browser, LANStream automatically detects available encoders and selects an appropriate backend.

**Windows**

- Intel Quick Sync Video (QSV)
- NVIDIA NVENC
- AMD AMF
- software H.264

**Linux / Docker**

- VAAPI
- NVIDIA NVENC
- software H.264

The active backend and capability-test results can be viewed at:

```text
<your LANStream address>/transcoder-status
```


### Settings UI

Open **Settings** in the top bar, or visit `/settings`, to edit the server port,
transcoder, media folders, master password and password profiles, including folder
exclusions. The first password profile is the administrator; other profiles cannot
view or change settings. In master-password mode the authenticated user can manage
settings. In passwordless mode settings are available to anyone with access to the app.

Blank password fields keep existing passwords. The first profile shown after saving
is the administrator. Removing profiles or switching access modes replaces their media
configuration. Changes apply immediately, except for the listening port, which requires
a restart. Deployment environment variables still override values saved in `config.txt`;
Docker port mappings must be changed in the deployment itself.

The configuration location is unchanged (`LANSTREAM_CONFIG_FILE` or the file beside
the application). Saving uses an atomic file replacement. For Docker, mount the
configuration's **parent directory** writable and point `LANSTREAM_CONFIG_FILE` at the
file inside it; a read-only mount or a bind mount of only the file cannot be replaced.
Media mounts can stay read-only. Comments and unknown settings are retained.

### Compact player and playback info

The video fills the player window. A small overlay holds playback, seeking, volume,
settings and fullscreen controls, and fades out during playback. Open the gear menu
to choose quality, video/audio codecs, audio tracks or subtitles without reserving space below the video.

Open **ⓘ Playback info** (keyboard shortcut **I**) for live diagnostics: direct play
or transcoding and its reason, source/output codecs, actual browser video dimensions,
video/audio bitrate targets, encoder, subtitle selection, buffer ahead, displayed FPS,
dropped frames, and request transfer statistics. Source metadata includes frame rate,
pixel format, color transfer, audio channels/sample rate and file bitrate.

Browser metrics refresh once per second while the panel is visible. Unavailable values
show “—”. FPS measures displayed frames over wall-clock time, rather than the file's
nominal frame rate. Transfer rate measures HTTP delivery, including buffering, rather
than encoded media bitrate. Metrics belong to the current browser session and media
request. Completed request statistics are retained briefly so they remain visible
while buffered playback continues. No background probing is needed for polling.

Video posters, video thumbnails, and folder preview scans have been removed. Images,
PDFs and comics keep their thumbnails; existing cache files are not deleted.

### Transcoding codecs

In the player's gear menu, choose **Video codec** (H.264, H.265/HEVC or AV1) and
**Audio codec** (AAC, Opus or MP3). Auto preserves direct playback when possible and
uses H.264/AAC if conversion is needed. Choosing an explicit codec forces transcoding
and preserves your playback position and paused state. Selections apply to that player.

The menu checks the server's encoders and browser MP4 support when opened. Unavailable
or browser-unsupported codecs are disabled. Unsupported video/audio combinations are
rejected before switching. Detection is cached; use the Activity page's retest button
after changing encoders or hardware. The selected GPU backend is used when its codec
probe succeeds; otherwise LANStream tests the software encoder. The menu labels software
encoding and Playback info shows the encoder actually used and any fallback.

Software encoders are libx264 (H.264), libx265 (HEVC), and libsvtav1 (AV1); audio uses
AAC, libopus or libmp3lame. Each must be included in the server's FFmpeg build. Streams
remain fragmented MP4. Audio is converted to stereo at 48 kHz; bitrate follows the
selected quality profile. Software AV1 uses capped CRF for limited profiles, with the
profile's bitrate as its ceiling. HEVC and AV1 software encoding can be demanding.
Browser capability checks cannot guarantee decoding performance on every device.

### Playback quality

Choose quality before starting a video or change it in the player. **Auto** uses direct
playback when possible and transcodes when required. **Original · transcode** always
converts at source resolution with variable bitrate. Neither mode limits bandwidth.

| Profile | Maximum frame size | Video target | Audio |
| --- | --- | --- | --- |
| 4K | 3840 × 2160 | 20 Mbps | 192 Kbps |
| 1440p | 2560 × 1440 | 12 Mbps | 192 Kbps |
| 1080p | 1920 × 1080 | 8 Mbps | 192 Kbps |
| 720p | 1280 × 720 | 4 Mbps | 128 Kbps |
| 480p | 854 × 480 | 1.5 Mbps | 96 Kbps |
| 360p | 640 × 360 | 700 Kbps | 96 Kbps |

**Custom** supports maximum heights from 240p to 2160p, video targets from 300 to
40,000 Kbps and audio from 96 to 256 Kbps. Press **Apply quality** to start the new
stream. Quality changes preserve playback position and paused state. The player
also transcodes when selecting another audio track or if direct playback fails.

All limited profiles transcode, preserve the display aspect ratio and avoid upscaling.
Bitrate figures are encoder targets: audio and container overhead add to network use,
and short bursts are possible. The same targets apply to software and supported GPU
backends. Select a starting preset in **Settings → Playback quality**, saved as
`default_quality=auto` (or `360p`, `480p`, `720p`, `1080p`, `1440p`, `2160p`, `source`).
The Activity page shows the selected quality for each transcoded stream.

### Access Control

LANStream can run completely passwordless on a trusted LAN, use a single master password, or define multiple password profiles.

Profiles can expose different roots and exclude specific subfolders.


## Installation

### Windows

The Windows release is distributed as a portable, self-contained folder.  Download the latest release zip.

1. Extract the release archive.
2. Run `LANStream.exe`.
3. Open the displayed LAN URL in a browser.
4. Open **Settings** to configure media folders, access and transcoding (or edit `config.txt` manually).

See [manual.md](manual.md) for detailed setup instructions.

### Linux / Docker

LANStream can be run in Docker.

The recommended deployment keeps user data outside the container:

```text
config.txt
cache/
```

Media directories are mounted read-only.

This means the container can be rebuilt or replaced without losing configuration or thumbnail cache data.

NVIDIA NVENC in Docker requires NVIDIA Container Toolkit on the host.

A Linux Easy Config preset is provided for deployments using LEC.

## Requirements

### Client

A reasonably modern web browser with HTML5 video support.

### Windows server

- 64-bit Windows 10/11
- Hardware acceleration is optional

### Linux server

- Docker
- `/dev/dri` access for VAAPI when applicable
- NVIDIA Container Toolkit when using NVENC in Docker


## AI & Safety Disclaimer

The code and documentation included in this project is primarily vibeslop. The human writing this sentence in particular can barely code and doesn't really understand how any of this works. It Works On My Machine and hasn't caused my genitals to explode, but your mileage may vary. I make absolutely no guarantee as to the safety or security of the contents of this project. Use at your own risk. Or don't.

LANStream is primarily intended for trusted local networks.  It is not "secure" in any meaningful sense and should never be exposed to the internet at large.  If you choose to ignore this advice and do it anyway, for the love of fuck at least place it behind an appropriately configured reverse proxy and TLS endpoint.


## Documentation

Detailed setup and usage instructions can be found in the [User Manual](manual.md).


## License

LANStream is released into the public domain under [The Unlicense](LICENSE).

Third-party libraries and dependencies remain subject to their respective licenses. See [Third-Party Licenses](THIRD_PARTY_LICENSES.md) for attribution and licensing information.

Copyleft 2026. Do what thou wilt shall be the whole of the law. One step closer to AGI.

