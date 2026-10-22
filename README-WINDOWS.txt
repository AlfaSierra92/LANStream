LANStream for Windows
=====================

LANStream exposes server folders through a local-network web interface.
The server reads media and uses FFmpeg for conversion when needed.
Client devices use a browser and do not install LANStream.

A built package requires neither Python nor Docker.
These instructions describe the current repository implementation.

1. Extract the complete package
-------------------------------

A release directory typically contains:

LANStream\
    LANStream.exe
    config.txt
    ffmpeg.exe
    ffprobe.exe
    7zz.exe
    cache\
    _internal\

The alternative 7-Zip layout is 7z.exe + 7z.dll.
Exact supplied tools depend on the distributed package.

Keep the complete directory, including _internal.
Do not move only LANStream.exe.
Use a writable location for configuration and graphics derivatives.

2. Start
--------

Run LANStream.exe and leave its console open.
The Waitress launcher prints local/LAN addresses and the selected backend.
The default port is 8060.

On the server:
    http://127.0.0.1:8060

On another device, use the actual server LAN address, for example:
    http://192.168.1.50:8060

The IP is illustrative. Allow the appropriate local-network firewall access
when prompted.

3. Configure libraries
----------------------

Use Settings or edit config.txt beside the executable.
A passwordless example:

port=8060
use_master_password=false
master_password=
transcoder=auto
default_quality=auto

D:\Movies
D:\TV
\\NAS\Media\Video

These paths belong to the Windows server, not the browser device.
UNC shares must already be readable by the process account.
Desktop mapped drives may not exist for another account/service.
Use absolute paths without quotes.

Passwordless users can also administer the server.
Settings supports a shared password or password profiles with roots/exclusions.
The first profile is administrator. Passwords remain plain text in config.txt.

Blank password fields preserve existing passwords.
Port changes require restart. Restart after manually editing shared-password or
backend settings to update runtime values initialized at startup.

4. Playback and viewers
-----------------------

Open a library, choose a video and start playback.
Auto uses original delivery where compatible, otherwise conversion.
Original disables conversion and may fail on an incompatible browser.
360p–4K presets and Custom set output size/bitrate targets.

The gear opens quality, codec and track controls.
Playback info (I) shows encoder, tone mapping, buffer, FPS and transfers.
Activity at /transcoder-status shows active requests and backend tests.

Converted movies/subtitles are not written as temporary media files.
Image thumbnails, document pages and TIFF derivatives can use the disk graphics
cache. Video posters/thumbnails are not generated.

Other viewers support images, PDF, CBZ, CBR and plain text.
CBR requires 7-Zip; Markdown is shown as plain text.

5. Windows transcoding and HDR
-----------------------------

Auto tests, in order:
    Intel QSV
    NVIDIA NVENC
    AMD AMF
    software libx264

An actual short encode must succeed; an encoder name in FFmpeg's list is not enough.
HEVC/AV1 output has additional checks and may use software.
Converted audio is stereo at 48 kHz in AAC, Opus or MP3.

QSV/NVENC/AMF currently accelerate encoding only; decoding and video filters remain
software. Full decode/filter/encode VAAPI processing applies to the Linux VAAPI path.

Recognized HDR transcoding automatically produces SDR BT.709.
Windows HDR processing uses CPU zscale/libzimg and Hable tonemap.
The FFmpeg build must include those filters. A cached in-memory compressed HDR10
fixture tests the path before HDR streaming; missing/failed support returns an error.
SDR input is not tone-mapped. Original bytes remain unchanged.

Supported Dolby Vision HDR10/HLG-compatible base layers can be used.
Dynamic metadata/enhancement layers are not reconstructed; unsupported proprietary
profiles such as Profile 5 are rejected for conversion.

The runtime chooses ffmpeg.exe beside the application, then PATH.
It does not automatically choose ffmpeg-nvenc.exe or test historical
modern/legacy/minimal NVENC profiles. The build script can copy that optional file,
but the current application does not select it.

For an NVENC API mismatch, align the installed driver and actual ffmpeg.exe build.
After changes use Activity's Retest transcoder backends, which also clears HDR
capability/filter caches.

6. Integrated NZBDAV
-------------------

Add these settings to the active file:

nzbdav_url=http://nzbdav.example:3000/
nzbdav_user=WEBDAV_USER
nzbdav_password=WEBDAV_PASSWORD
nzbdav_root=content
nzbdav_ram_cache_mb=256

Replace endpoint and credentials with WebDAV values, not rclone RC values.
The administrator receives an NZBDAV library.
No rclone, FUSE or tmpfs is required.

The video cache holds 4 MiB RAM blocks and evicts least recently used blocks while
movies remain open. Movies may exceed cache size. Seeking to evicted data rereads it.
The budget is per process and excludes FFmpeg/Python/socket overhead.
Operating-system swap is separate. Graphics derivatives can still use disk.

7. Stop, update and back up
--------------------------

Use Ctrl+C in the console. Shutdown interrupts streams and cleans up managed
external processes.

Back up config.txt before replacing the complete distribution.
Preserve personal configuration instead of overwriting it with an example.
Graphics cache backup is optional; RAM caches are recreated.
Reload open players after updates.

8. Build on Windows
-------------------

Install Python compatible with dependencies and the py launcher.
Place these Windows tools in the project's tools directory:
    ffmpeg.exe
    ffprobe.exe

For the build script, also provide one alternative:
    7zz.exe
or:
    7z.exe
    7z.dll

Run build_windows.bat.
It creates .venv-build, installs requirements.txt and PyInstaller, uses LANStream.spec,
and produces the complete folder distribution in dist\LANStream\.

The script copies the current config.txt into the output.
Before distributing a package, replace personal paths/passwords/WebDAV secrets with
a suitable example. Do not distribute production credentials.

Retain the notices/licenses relevant to the tools actually distributed.
Compiled packaging does not place dependencies under The Unlicense.

Full documentation in the source tree
-------------------------------------

README.md
manual.md
docs/configuration.md
docs/architecture.md
docs/troubleshooting.md
examples/

See THIRD_PARTY_LICENSES.md for dependency attribution.
