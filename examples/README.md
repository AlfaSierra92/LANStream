# Example configurations

These files are examples, not the active configuration of your server.
Copy the appropriate file to your chosen location and replace folders, endpoints
and passwords before use.

| File | Purpose |
| --- | --- |
| [config-local.txt](config-local.txt) | Local Linux/Docker folders, passwordless access. |
| [config-audio-only.txt](config-audio-only.txt) | Original H.264/HEVC/AV1 video with AAC stereo conversion as the default. |
| [config-profiles.txt](config-profiles.txt) | Two profiles with separate libraries and exclusions. |
| [config-nzbdav.txt](config-nzbdav.txt) | NZBDAV-only library with a 256 MiB RAM budget. |
| [config-windows.txt](config-windows.txt) | Windows folders and a UNC share. |

Docker paths must match locations **inside the container**.
Passwords are placeholders, not suitable credentials to retain.
Anyone who can reach a passwordless server can also administer it.

Global NZBDAV settings can be added to local/profile examples, or configured in
**Settings → Access & media → NZBDAV · remote media**. The UI writes the same
keys to the active file. The remote library is administrator-only in profile mode.
Blank WebDAV password inputs retain the saved secret; the removal checkbox
clears it. A blank URL disables integration. Saving checks syntax, not connectivity.

Use `default_quality=audio_only` to preserve video and convert audio; the browser
must support the original video/HDR. This mode bypasses video encoders and tone
mapping. The standard examples retain `default_quality=auto`, which may re-encode
video on fallback. Tone mapping applies to recognized HDR when video is re-encoded;
Original and audio-only video are unchanged.

The tone_mapping selector is currently edited in the file. Keep its default
unless the selected driver/filter/runtime pipeline passes the in-memory check.
Optional Vulkan/OpenCL modes are not guaranteed by an FFmpeg filter listing.

See the [configuration reference](../docs/configuration.md) for all rules.
