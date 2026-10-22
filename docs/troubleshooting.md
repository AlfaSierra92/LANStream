# Troubleshooting

[Documentation home](../README.md) · [Manual](../manual.md) ·
[Configuration](configuration.md)

Identify the failing stage first: folder browsing, metadata probing, player startup,
continuous playback, subtitle loading or seeking. They can have different causes.

## Cannot reach the server

Verify the process/container is running, then try its local address on the server.
On another device use the server's LAN IP, not 127.0.0.1.
Check firewall and published ports.

Launcher/Flask defaults to 8060.
Included Gunicorn binds to 8080.
With -p 8060:8080, clients connect to 8060.

```bash
docker ps --filter name=lanstream
docker logs --tail 100 lanstream
```

Review logs before sharing: paths, file names and source URLs can contain personal
information or access tokens.

## Missing folders or empty libraries

Check the active LANSTREAM_CONFIG_FILE and the process account's read permissions.
Docker roots must use container paths and match actual mounts.
Unrecognized extensions are not displayed.

Clear any current-directory filter.
In profile mode, check the password section and exclusions.
Roots above the first profile are not inherited by all profiles.
Symbolic links cannot bypass root confinement/exclusions.

## Settings cannot save

The configuration directory must be writable.
Mount its directory, not only the individual file, because saving replaces the
file atomically.

Reload Settings if another editor/tab changed the configuration after it opened.
Environment overrides continue to win after saving.
Changing port= does not alter Gunicorn's bind or Docker's mapping.

## Unexpected password behavior

Profile sections activate authentication independently of use_master_password.
Without sections, a shared password needs both an enabled switch and nonempty value.
LANSTREAM_PASSWORD/LANSTREAM_USE_MASTER_PASSWORD can override the file.

Restart after manual edits to those runtime settings.
Changing/removing profiles can invalidate existing sessions.
The default random signing secret normally requires login again after restart.

## NZBDAV is missing or cannot list files

Check nzbdav_url and use the administrator profile.
The integrated library is not available to restricted profiles.

Use the final WebDAV endpoint, not the UI or rclone RC endpoint.
Verify root relative to that endpoint; an empty root exposes its root.
The server must support PROPFIND, HEAD, Content-Length and byte ranges.
The client does not follow redirects or environment proxies.

Test reachability from the LANStream process/container environment.
Browser reachability alone proves little about container networking.
Changing NZBDAV settings recreates its cache/connections at the next access.

## Slow startup

A delay before the player page can be FFprobe reading container metadata.
A delay after opening it can involve FFmpeg, hardware setup, source reads or the
browser.

First HDR startup on a backend also performs a cached, in-memory capability test.
This test does not read a user's remote file.
Later HDR requests reuse its result until retest/restart.

NZBDAV cache misses require actual upstream reads.
A larger cache cannot remove latency for bytes never fetched before.
STRM remote URLs do not automatically use the NZBDAV cache.

Do not indiscriminately shrink FFmpeg discovery limits: track detection can break.
The current playback path already avoids redundant zero seeks and unnecessary
end-of-file duration estimation.

## Confirm hardware processing

Playback info shows the selected encoder and tone-mapping path.
On Linux inspect the complete running command:

```bash
pgrep -a ffmpeg
```

VAAPI playback should contain:

```text
-hwaccel vaapi
-hwaccel_output_format vaapi
-vf ...scale_vaapi=...format=nv12...
-c:v h264_vaapi
```

HEVC/AV1 outputs use their corresponding VAAPI encoder names.
HDR10 conversion additionally contains tonemap_vaapi, or libplacebo plus hardware
mapping when an optional Vulkan curve is selected.
It should not download video for CPU filters.

format=nv12,hwupload alone identifies the old CPU-decode/upload pipeline,
although upload can legitimately appear in short synthetic encoder tests.
A process already running before an update retains its old command:
update/restart the server and reopen the player.

### Headless device checks

With vainfo on Linux:

```bash
vainfo --display drm --device /dev/dri/renderD128
```

Inside the supplied Docker image with device access:

```bash
docker exec lanstream vainfo --display drm --device /dev/dri/renderD128
docker exec lanstream ffmpeg -hide_banner -hwaccels
docker exec lanstream ffmpeg -hide_banner -h filter=scale_vaapi
docker exec lanstream ffmpeg -hide_banner -h filter=tonemap_vaapi
```

hwaccels lists build support, not successful device access.
Test with the same account/environment as LANStream.

A normal vainfo invocation may fail Wayland/X11 before succeeding through DRM.
An initial X error does not invalidate a successful DRM result.
HEVC Main 10 with VLD indicates decode capability; encoding and HDR processing
are separate capabilities.

### Low GPU utilization

On Intel, when installed and permitted:

```bash
sudo intel_gpu_top
```

Inspect relevant video engines, not only 3D/render percentage.
Low utilization can mean spare capacity, upstream waiting or browser backpressure.
There is no requirement for 100% GPU use.
Compare command, buffer, dropped frames and playback continuity.

CPU activity remains for audio/network/containers.
QSV/NVENC/AMF also retain CPU video decoding/filters in the current implementation.

## VAAPI unavailable

Check device node, permissions, driver and FFmpeg.
Expose /dev/dri to Docker and grant the process access.
With multiple GPUs, renderD128 may not be the intended device.

Activity contains backend test errors.
Retest after correcting the installation.
H.264 encoding success does not certify every input decoder or HDR filter.

Run real tests in the target source/dependency environment:

```bash
python -m unittest discover -s tests -p test_vaapi.py -v
python -m unittest discover -s tests -p test_tone_mapping.py -v
```

A hardware test skipped on a Mac is not validation of an Intel server.

## HDR conversion fails or colors look wrong

To preserve the original video and bypass HDR filters, select **Original video ·
convert audio only** in the player or Settings → Default quality
(default_quality=audio_only). This still converts audio and remuxes into MP4.
The browser must support the source H.264/HEVC/AV1 video and its HDR rendition;
it is not a way to make unsupported video decodable. Check Playback info for
video passthrough and tone mapping off. No video encoder/GPU test is required.

For a dark SDR rendition, compare alternative GPU curves with
tone_mapping=libplacebo-bt2390, libplacebo-mobius or libplacebo-reinhard in config.txt.
Restart and reopen playback. Playback info identifies the active algorithm.
All require Vulkan/VAAPI interoperability on the selected GPU, not just a listed
libplacebo filter. A failed capability check stops playback with its error;
tone_mapping=vaapi restores the original mapper. Do not change color tags to
compensate for brightness: tags describe a conversion, they do not perform it.

If libplacebo fails with only "Error initializing filters" and "Invalid argument",
update app.py and restart (or retest in Activity) to clear the cached failure.
The Vulkan capability probe uses verbose initialization logs to retain library
version, selected GPU and the final errors. It still uses only the bundled RAM
fixture and does not open a movie. Normal playback logging is unchanged.

FFmpeg 9 has a compatibility guard: libplacebo API versions below 365 cannot
import Vulkan devices created with internally synchronized queue flags. The
guard can return EINVAL without a specific error message. If the diagnostic
shows both an older libplacebo API and the enabled
VK_KHR_internally_synchronized_queues extension, use a consistent FFmpeg build
linked against libplacebo API 365 or newer. Installing a newer runtime library
alone may not change the API used to compile FFmpeg. Do not remove the guard.
See the [FFmpeg 9 source](https://github.com/FFmpeg/FFmpeg/blob/n9.0.2/libavfilter/vf_libplacebo.c).

To avoid this Vulkan/libplacebo dependency, update app.py and tone_mapping.py
together and use tone_mapping=opencl-mobius (alternatives: opencl-reinhard,
opencl-hable). The actual GPU/OpenCL/VAAPI pipeline is checked before playback.
This needs the Intel GPU OpenCL runtime with VAAPI sharing support; FFmpeg's
tonemap_opencl build flag alone does not install the runtime. "No matching
devices", OpenCL platform error -1001, or failed VAAPI/OpenCL derivation indicate
runtime/device-sharing setup issues. CPU OpenCL and software video fallbacks
are not used. Test from the application's actual environment/container:

```bash
python -c "import app; print(app.tone_mapping_test('vaapi', 'opencl-mobius'))"
```

Run the exact synthetic test from the project folder using the server's Python
environment, and share its sanitized diagnostic if the error persists:

```bash
python -c "import app; print(app.tone_mapping_test('vaapi', 'libplacebo-bt2390'))"
```

This diagnosis is conditional; a generic EINVAL alone does not prove this cause.

If the message names Parsed_anull, an audio format_out, and different channel
layouts such as 5.1.2 → stereo, update app.py. Older HDR commands disabled implicit
audio converters along with video conversion without providing an explicit audio
resampler. The current command explicitly downmixes/resamples to the chosen audio
encoder, while keeping the HDR video graph on VAAPI.

For an error containing "Impossible to convert", a long pixel-format list and
"Function not implemented", deploy current app.py and tone_mapping.py together.
Older graphs did not declare the SDR boundary after tonemap_vaapi to FFmpeg's
color negotiation. Restart to clear the cached capability failure. This differs
from a driver explicitly reporting that HDR processing is unsupported.
Long format lists are now shortened without discarding the first error line.

If the error lists several standard filters as missing at once, including
setparams and sidedata, update LANStream: older builds incorrectly assumed a
three-character flag column in FFmpeg's filter list. Newer FFmpeg versions can
use two characters. Restart, or retest in Activity after updating, to clear the
cached failure. This parser issue does not establish a driver limitation.

Playback info should show HDR10/PQ → SDR BT.709 with VAAPI, or the CPU Hable path.
Source color transfer remains the original metadata, so it can still say smpte2084
while the output is correctly SDR.

Check the selected path and the response/log error:

| Error/condition | Meaning/action |
| --- | --- |
| Missing tonemap_vaapi | The selected FFmpeg build lacks the hardware filter. |
| Driver lacks HDR-to-SDR processing | HEVC decode support alone is insufficient; verify driver/GPU VPP support. |
| No mastering-display data | VAAPI needs that metadata on decoded input frames; the fixture passing does not repair an unsuitable source. |
| Missing zscale/tonemap | CPU HDR conversion needs a build with libzimg/zscale and tonemap. |
| HLG on VAAPI | Currently rejected instead of falling back to CPU. |
| Dolby Vision Profile 5/unsupported profile | No supported ordinary base layer; use an HDR10 source or Original on a compatible client. |
| Stale player/server | Deploy tone_mapping.py, app.py, templates and player.js together; rebuild Docker and reload. |

A failed preflight returns HTTP 503 before a playback encoder starts.
Unsupported source color/Dolby combinations return HTTP 415.
The player retrieves the retained error for its own request and shows the reason
in the playback message and Playback info. Preflight rejection is also written to
the server log. FFmpeg failures after headers were sent retain stderr for that
same session/request; internal source URLs/tokens are removed from these messages.
If the player still shows only the old generic failure, deploy app.py and player.js
together and reload the page. Browser-only codec failures may have no encoder error.

The implementation does not reconstruct Dolby Vision dynamic metadata or enhancement
layers. Compatible base-layer mapping is not full Dolby Vision rendering.
Original mode bypasses mapping and depends on the client's HDR support.

After changing tools/drivers, Activity's retest clears filter/mapping test caches.

## NVENC, QSV or AMF errors

These require actual working encoders and matching drivers.
The runtime uses ffmpeg.exe beside the Windows application, then PATH.
It does not automatically select ffmpeg-nvenc.exe or try historical
modern/legacy/minimal NVENC profiles.

For NVENC API mismatch, align the driver and the build actually used.
For QSV/AMF verify the driver and test under the server account.
An encoder listed by FFmpeg does not prove a successful hardware encode.

## Buffering or dropped frames

Observe several samples over time:

| Observation | Check |
| --- | --- |
| Shrinking buffer with few drops | Source/server delivery may be insufficient. |
| Available buffer with many drops | Client decoding/rendering may be overloaded. |
| Delay after remote seek | New ranges and a new encoder may be required. |
| Low GPU activity and frequent cache misses | Inspect remote supply before assuming encoder failure. |
| Multiple simultaneous streams | GPU, CPU, threads, cache and network are shared. |

These are diagnostic clues, not proof of a single bottleneck.
Compare a less demanding quality on the same source.
Reducing output bitrate does not necessarily reduce the compressed input bytes
the decoder needs to read.

A few seconds of buffer gives little room for stalls but does not establish why
it is low. Browser buffer and server block-cache occupancy are different measures.

## Transfer Mbps exceed the selected target

Video target is an encoder setting.
Transfer rate measures the current HTTP response, including faster-than-realtime
buffer filling. Audio/container overhead also contributes.

Unrestricted source-resolution transcoding has no explicit video ceiling.
Original preserves the file bitrate.
The player's transfer rate does not measure NZBDAV upstream throughput.

## Subtitle errors or HTTP 504

Select a supported embedded text track.
PGS/VobSub have no OCR conversion and external SRT upload is not implemented.

A 504 indicates the request deadline expired; the player warns and retries later.
Video continues independently. A silent interval can correctly produce an empty
subtitle window.

If conversion still opens the entire film with FFmpeg, check that Python modules,
template, player.js and subtitles.js are all updated.
Do not solve obsolete full-film extraction by increasing timeouts indefinitely.

ASS layout/animation can change in WebVTT; MOV_TEXT retains text without all styles.

## RAM growth or rereading old ranges

The NZBDAV budget covers stored blocks and in-flight reservations, not total RSS.
Python, FFmpeg, metadata, sockets and delivery chunks use additional memory.
Additional workers multiply cache budgets.

Evicted blocks must be read again, including for still-open files.
This is expected bounded-cache behavior.
Activity reports stored/reserved/capacity, hits, misses and evictions.

Without strong remote validators, metadata refresh can invalidate prior blocks
instead of trusting path/size indefinitely.

## Disk usage despite RAM video caching

LANSTREAM_CACHE_DIR contains persistent graphics derivatives without a global
automatic size limit. It is separate from the video block cache.

Inspect logs, Docker storage, external rclone caches and operating-system swap
separately. LANStream's RAM cache does not configure those components.
Stop the service before removing affected graphics derivatives; do not remove
original media to clear cache.

## Document/thumbnail failures

Check PyMuPDF and PDF integrity.
CBZ must contain supported images; CBR needs 7zz/7z and, where applicable, 7z.dll.
Verify graphics-cache write permissions.

Video thumbnails/posters are not generated by design.
Replacing a source under the same name can leave stale graphics derivatives:
remove the affected cached file and reopen the source.

## HTTP status reference

| Status | Typical causes |
| --- | --- |
| 400 | Invalid request parameters, codec/quality or malformed STRM. |
| 403 | Unauthorized path or restricted Settings/NZBDAV. |
| 404 | Missing/excluded content. |
| 409 | Transcoding requested in Original mode. |
| 415 | Unsupported subtitles/document or unsupported HDR/Dolby conversion plan. |
| 502 | WebDAV/upstream/probe error. |
| 503 | Encoder unavailable, missing HDR filters or failed tone-mapping capability test. |
| 504 | Remote probing/subtitle deadline exceeded. |

An error after response headers have been sent can appear as interrupted playback
even if the initial HTTP status was successful. Inspect server logs as well.

## Useful information for a bug report

Include server OS/startup method, actual backend, source type, codec/profile/pixel
format, selected quality and the failing stage.
Add Playback info and the exact error where available.

Remove passwords, cookies, tokens, private URLs and personal names before sharing.
The whole configuration file is not needed.
