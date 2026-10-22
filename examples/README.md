# Example configurations

These files are examples, not the active configuration of your server.
Copy the appropriate file to your chosen location and replace folders, endpoints
and passwords before use.

| File | Purpose |
| --- | --- |
| [config-local.txt](config-local.txt) | Local Linux/Docker folders, passwordless access. |
| [config-profiles.txt](config-profiles.txt) | Two profiles with separate libraries and exclusions. |
| [config-nzbdav.txt](config-nzbdav.txt) | NZBDAV-only library with a 256 MiB RAM budget. |
| [config-windows.txt](config-windows.txt) | Windows folders and a UNC share. |

Docker paths must match locations **inside the container**.
Passwords are placeholders, not suitable credentials to retain.
Anyone who can reach a passwordless server can also administer it.

Global NZBDAV settings can be added to local/profile examples.
The remote library remains available only to the administrator.
Tone mapping is automatic for recognized HDR input during transcoding;
Original delivery remains unchanged.

See the [configuration reference](../docs/configuration.md) for all rules.
