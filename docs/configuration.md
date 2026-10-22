# Configuration reference

[Documentation home](../README.md) · [User manual](../manual.md) ·
[Troubleshooting](troubleshooting.md)

This reference lists the options implemented by the current source.
Copy a suitable file from [examples](../examples/) and replace its placeholders.

## Selecting the configuration file

By default, config.txt lives beside app.py, or beside LANStream.exe in a built
Windows package. LANSTREAM_CONFIG_FILE overrides that path.
For Docker, explicitly set it to the mounted configuration location.

The file is UTF-8. Each line is a key=value setting, a folder, a profile header or
an exclusion. Blank lines and lines beginning with # are ignored.
Use separate comment lines: trailing inline comments are not stripped from values.
Do not quote values or paths; quotes are not part of the file syntax.
Folder names may contain spaces.

Known setting keys are case-insensitive. Outer whitespace is stripped.
If a known key appears repeatedly, the last value wins.
Place global settings before library/profile lines for readability.
Unknown settings are not automatically implemented merely because they are present.

## Global settings

| Key | Code default | Purpose |
| --- | --- | --- |
| port | 8060 | Listening port for launcher.py and the Flask development entry point. |
| use_master_password | false | Enables shared-password access when there are no profile sections and the password is nonempty. |
| master_password | empty | Shared password. |
| transcoder | auto | Preferred backend: auto, vaapi, qsv, nvenc, amf or software. |
| default_quality | auto | Initial quality for newly opened players. |
| tone_mapping | vaapi | VAAPI HDR conversion: vaapi; libplacebo-bt2390, libplacebo-mobius, libplacebo-reinhard; opencl-mobius, opencl-reinhard, opencl-hable. |
| nzbdav_url | empty | HTTP/HTTPS WebDAV endpoint; empty disables the integration. |
| nzbdav_user | empty | WebDAV username; empty means no Basic Auth header. |
| nzbdav_password | empty | WebDAV password. |
| nzbdav_root | empty | Subdirectory relative to the WebDAV endpoint. |
| nzbdav_ram_cache_mb | 256 | RAM block-cache budget per worker, in MiB. |

The NZBDAV keys can also be edited in **Settings → Access & media → NZBDAV ·
remote media**. A blank password field preserves the saved password; an explicit
removal checkbox clears it. The saved password is never rendered in the page.
Clear the URL to disable the integration. Saving validates syntax, control
characters, remote-root boundaries and RAM limits without contacting WebDAV.
Do not put credentials in the URL; use the dedicated fields. Invalid URL
credentials/query tokens are not reflected back into a failed form.

### Settings UI and password behavior

| Web UI location | Stored keys |
| --- | --- |
| Server | port, transcoder |
| Playback quality → Default quality | default_quality |
| Access & media | use_master_password, master_password; library/profile lines |
| Access & media → NZBDAV · remote media | nzbdav_url, nzbdav_user, nzbdav_password, nzbdav_root, nzbdav_ram_cache_mb |

The curve selector `tone_mapping` remains file-only and is preserved when the UI
saves other settings. An omitted NZBDAV form field preserves its existing value.
For the NZBDAV password, an empty input also preserves the existing secret;
**Remove saved WebDAV password** explicitly writes an empty value. Entering a
replacement password while selecting removal is rejected. Clearing the URL
disables remote access without erasing username, password, root or cache budget.

Save failures keep nonsecret edits on screen. New passwords must be entered again.
A changed file revision rejects the save rather than overwriting another editor's
changes. Only administrators receive this form: all users in passwordless/shared
password modes, or the first profile in profile mode.

The supplied configuration may explicitly set different values, such as
nzbdav_root=content. Code defaults apply when a key is absent.

### Port and booleans

Settings accepts ports from 1 to 65535. The launcher/Flask port changes after a
restart. The included Gunicorn configuration binds to 8080 independently.

Docker mapping `-p 9090:8080` exposes port 9090 on the host.
Setting port=9090 alone does not change that mapping or Gunicorn.

Boolean true values are 1, true, yes and on, case-insensitively.
Other values are false. Prefer true/false in configuration files.

### Default quality

| Value | Behavior |
| --- | --- |
| auto | Use original media where compatible; otherwise transcode. |
| source | Original media; no transcoding fallback. |
| audio_only | Copy original H.264/HEVC/AV1 video into fragmented MP4; convert selected audio to AAC stereo, 48 kHz, 192 Kbps by default. No video encoding or tone mapping. |
| transcode | Convert at source resolution without an explicit video bitrate ceiling. |
| 360p, 480p, 720p, 1080p, 1440p, 2160p | Limited resolution/bitrate presets. |

Custom quality belongs to the player controls, not default_quality.
Changing this setting does not alter an already-open player.
To preserve video while converting audio, select **Original video · convert
audio only** in Settings → Default quality, or set default_quality=audio_only.
The player also offers this mode and lets you change audio track/codec. The
browser must decode the original video and handle HDR itself. It never falls
back to video encoding in this mode; GPU encoder/tone-mapping availability is
irrelevant. Unsupported video codecs require a different quality profile.
This is fragmented-MP4 remuxing with `-c:v copy`, not byte-for-byte delivery of the
source container. Audio track/codec controls remain available; video controls are
locked to passthrough. No audio track means video-only remuxing. Seeking can align
to source keyframes. The mode is explicit; Auto does not automatically choose it.
See the [quality table](../manual.md#quality-and-codecs).

### Transcoder selection

Auto tests Windows backends in this order: QSV, NVENC, AMF, software.
On Linux the order is VAAPI when its device exists, NVENC, software.
The startup test performs a short real H.264 encode; HEVC/AV1 have separate checks.

An explicit backend neither installs drivers nor adds FFmpeg support.
Select a backend that the platform actually tests. QSV/AMF are tested by this
application on Windows; VAAPI is tested on the non-Windows path.

An explicitly selected backend that is present in the test list but fails is
reported as unavailable. If a backend is not in that platform's list at all,
including VAAPI when its device does not exist, the current selector can proceed
with automatic selection. Always inspect Activity for the actual result.

VAAPI's unavailable HEVC/AV1 outputs do not fall back to software.
The video decoder must independently support the input codec/profile.

### Tone mapping

Recognized HDR input is converted to SDR when video transcoding runs.
Audio-only conversion copies the video and bypasses tone mapping entirely.
SDR input and original-byte delivery are unchanged.

The default tone_mapping=vaapi uses tonemap_vaapi for PQ/HDR10 and rejects HLG.
For alternative GPU curves, add one of these global settings to config.txt:

```ini
tone_mapping=libplacebo-bt2390
```

Other choices are libplacebo-mobius and libplacebo-reinhard. These modes also
accept HLG, use Vulkan for tone mapping and VAAPI for decoding/scaling/encoding.
Vulkan is derived from the selected VAAPI adapter. FFmpeg must include libplacebo,
hwmap and Vulkan/VAAPI interoperability; a filter listing alone is insufficient.

An alternative that avoids Vulkan/libplacebo is:

```ini
tone_mapping=opencl-mobius
```

Other OpenCL choices are opencl-reinhard and opencl-hable. These modes require
tonemap_opencl plus a GPU OpenCL runtime supporting VAAPI surface sharing on the
selected adapter (on Intel, the appropriate Intel compute runtime). VAAPI
decodes, converts surfaces to P010, and shares them with OpenCL for PQ/HLG tone
mapping. Reverse mapping allocates VAAPI-compatible output surfaces for GPU
scaling and encoding. No CPU video processing or disk video cache is introduced.
Only BT.2020 non-constant-luminance or BT.709 input gamuts/matrices are supported
by this OpenCL kernel. Unsupported combinations fail before encoding.

There is no automatic CPU or VAAPI tone-mapper fallback on failure. To restore
the original curve, set tone_mapping=vaapi. Settings preserves this file-only key.
Reopen playback after editing the file. Retest in Activity or restart after
changing FFmpeg/drivers to clear cached capability failures. Compare the same scene;
another curve is not guaranteed to brighten a particular movie or display.
Other backends use zscale and the CPU Hable operator for PQ/HLG.

A capability test uses a tiny compressed HDR10 frame held in memory.
Results are cached per backend and selected mapping mode until restart or Activity's retest.
The test does not fetch or store any user's movie.
Missing filters or a failed test produce HTTP 503 before the stream starts.
Unsupported input color/Dolby Vision combinations produce HTTP 415.

Dolby Vision Profile 7 and Profile 8 with an HDR10-compatible base layer can use
that base layer. Profile 8 HLG-compatible input can use libplacebo, OpenCL or the CPU path.
Profile 5 and other unsupported proprietary combinations are rejected.
This is not full Dolby Vision processing.

Original mode delivers the complete source unchanged. Audio-only conversion
preserves video/HDR while remuxing the container and replacing audio. There is
no HDR-preserving video re-encoding mode. Curve selection applies to VAAPI video
re-encoding only; other video backends retain the CPU Hable path.

## Libraries without profile sections

### Passwordless

```ini
port=8060
use_master_password=false
master_password=
transcoder=auto
default_quality=auto

/media/Movies
/media/TV
```

Anyone who can reach the service can browse and manage Settings and configured
NZBDAV. A leftover master_password alone does not enable authentication.

### Shared password

```ini
use_master_password=true
master_password=CHANGE_THIS_EXAMPLE_PASSWORD
transcoder=auto

/media/Movies
/media/TV
```

All authenticated users have the same library and administrative rights.
Both the switch and a nonempty password are required.

Exclusion lines are implemented for profile sections. Do not put negative lines
in an unsectioned library: they are not interpreted as exclusions there.

## Profiles and exclusions

Each profile begins with its password in angle brackets.
The first profile is the administrator. There are no separate usernames.

```text
<CHANGE_THIS_ADMIN_PASSWORD>
/media/Movies
/media/TV

<CHANGE_THIS_GUEST_PASSWORD>
/media/Movies
-/Private
-/Not shared
/media/TV
-/Drafts
```

The guest sees Movies without Private/Not shared and TV without Drafts.
An exclusion covers the complete subtree.

1. A positive path adds a root and becomes the reference for subsequent relative
   exclusions.
2. -/Private normally refers to Private under the last positive root.
3. If the path after the minus sign is already an absolute path beneath a root
   listed for that profile, it is treated as absolute.
4. -Private is also resolved relative to the last root.
5. An absolute Windows exclusion can be -D:\Movies\Private.
6. Place each exclusion immediately after its intended root for clarity.

For example, -/media/Movies/Private after /media/Movies is recognized as an
absolute exclusion. -/Private is simpler for ordinary cases.

In profile mode, roots before the first section are not inherited as common
libraries. Repeat the appropriate roots in each profile.
The presence of sections enables profile authentication even when
use_master_password=false.

Settings validates distinct, nonempty passwords without surrounding whitespace,
angle brackets or control characters. Its folder fields require absolute server
paths and reject configuration directives or equals signs.
Passwords are stored in plain text; protect the file and its backups.

## Integrated NZBDAV

```ini
nzbdav_url=http://nzbdav.example:3000/
nzbdav_user=WEBDAV_USER
nzbdav_password=WEBDAV_PASSWORD
nzbdav_root=content
nzbdav_ram_cache_mb=256
```

| Field | Rules |
| --- | --- |
| URL | HTTP/HTTPS with hostname; no embedded credentials, query or fragment. |
| Credentials | WebDAV credentials, not rclone RC credentials. |
| Root | Relative endpoint path using /; no dot segments or backslashes. |
| Cache | Integer 8–16384 MiB, rounded down to complete 4 MiB blocks. |

Use the NZBDAV subsection under Access & media, not the local Media folders text
area. The remote root is relative to the URL; a local absolute media path belongs
in the local library fields instead. Values cannot contain control characters or
surrounding whitespace. The UI validates numeric ports and accepts offline
endpoints; actual WebDAV access is checked when opening the remote library.

A 258 MiB setting provides 256 MiB of block capacity.
Stored blocks and in-flight reservations count toward the budget; total process
memory and swap do not.

Credentials are used between the server and WebDAV, not inserted into FFmpeg
arguments or player pages. HTTP does not encrypt that connection.
Use an appropriate HTTPS endpoint when transport encryption is needed.

The backend must support PROPFIND, HEAD, Content-Length and byte ranges.
Use its final URL: the client follows neither redirects nor environment proxies.
These settings do not create a mount or start rclone.

The library is administrator-only in profile mode. In passwordless/shared-password
mode all users have administrative access. There is no separate list of NZBDAV
roots per restricted profile.

## Environment variables

Set these before starting the process:

| Variable | Purpose |
| --- | --- |
| LANSTREAM_CONFIG_FILE | Configuration file to read and save. |
| LANSTREAM_CACHE_DIR | Graphics cache for thumbnails, document pages and TIFF derivatives. |
| LANSTREAM_SESSION_SECRET | Cookie-signing secret; randomly generated per process when absent. |
| LANSTREAM_PORT | Overrides port for launcher/Flask, not Gunicorn's bind. |
| LANSTREAM_PASSWORD | Overrides master_password in the unsectioned access model. |
| LANSTREAM_USE_MASTER_PASSWORD | Overrides the shared-password switch. |
| LANSTREAM_TRANSCODER | Overrides transcoder. |
| LANSTREAM_VAAPI_DEVICE | VAAPI render node, default /dev/dri/renderD128. |

There are no LANSTREAM_NZBDAV_* variables in the current application.
NZBDAV settings come from config.txt, including values saved there by the web UI.

A stable session secret preserves the cookie signature across restarts.
It is not a login password. Multiple processes need the same secret to validate
each other's cookies, but caches, metrics and local services still remain separate.
The supplied deployment uses one worker.

FFmpeg/FFprobe are found beside the application first, then on PATH.
There is no LANSTREAM_FFMPEG override or automatic ffmpeg-nvenc.exe selection.

## When changes take effect

| Change | Application |
| --- | --- |
| Roots, sections and exclusions in the file | Read on subsequent requests; removed/changed profiles can invalidate access. |
| default_quality | New player pages only. |
| tone_mapping | New video-conversion requests; capability tests are cached by backend/mode. Existing streams keep their running command. |
| NZBDAV settings | Checked on the next NZBDAV access; changes recreate the service/cache and interrupt its streams. |
| Shared password/switch/backend saved in Settings | Also updates the saving process's runtime state. |
| Those same settings edited manually | Restart to update values initialized at process startup. |
| Launcher/Flask port | Restart required. |
| Environment or Gunicorn configuration | Restart/recreate the service. |
| Python/templates/player scripts in Docker | Rebuild and recreate the container, then reload open players. |

An environment override continues to win after a Settings save.
Settings preserves unknown options/comments and updates NZBDAV values from its form,
but rewrites the library layout for the selected access mode.
It does not guarantee preservation of the original file formatting.

## Optional rclone mount

Use this only when retaining FUSE as a local library. Direct WebDAV integration
requires none of these steps.

tools/mount_nzbdav.sh runs a read-only mount of an already-configured nzbdav: remote.
It does not create the remote, stop services, unmount existing mounts or store RC
credentials in the script. Stop an old mount through its service manager first.

Provide RCLONE_RC_PASS in the process environment. Choose the RC binding deliberately;
RCLONE_RC_ADDR=127.0.0.1:5572 is suitable when only local access is needed.

| Script variable | Default |
| --- | --- |
| RCLONE_BIN | /usr/bin/rclone |
| RCLONE_CONFIG_FILE | $HOME/.config/rclone/rclone.conf |
| NZBDAV_MOUNT | /mnt/nzbdav |
| NZBDAV_BUFFER_SIZE | 128M |
| RCLONE_RC_ADDR | 0.0.0.0:5572 |
| RCLONE_RC_USER | admin |
| RCLONE_RC_PASS | Required; no default. |

Run `sh tools/mount_nzbdav.sh` in the configured environment.
RC and WebDAV credentials are separate.

The script sets VFS cache off, a 128M RAM buffer per open file, sequential chunks
starting at 64M and limited to 512M, cookies, a one-hour directory cache,
--links, --allow-other and authenticated remote control.
Check `rclone mount --help` for supported flags and FUSE allow-other permissions.

Directory caching contains listings, not movie bytes.
Rclone's per-file buffers are not shared between FFmpeg, FFprobe and subtitles.
Without external listing invalidation, the one-hour directory cache can delay new
files appearing; adapt your mount command if needed.

A VFS cache on tmpfs is not required and is not the same bounded LRU as LANStream's
integrated video cache.

## Historical options and external editors

low_bandwidth_resolution and low_bandwidth_bitrate no longer control current
quality profiles. The legacy low=1 playback query maps to 480p.

The bundled LEC module predates newer settings. Its save reconstructs only known
keys and can remove default_quality, tone_mapping and NZBDAV settings.
Read [INSTALL-LEC.txt](../INSTALL-LEC.txt) before editing current configurations
through that module.
