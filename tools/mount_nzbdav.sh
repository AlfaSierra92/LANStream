#!/bin/sh
# Run on the Linux host that owns the mount, after stopping its existing mount.
# Keep RC credentials in the environment, never in this file.
# Media buffering is RAM-only; do not create a VFS disk cache.
set -eu

: "${RCLONE_RC_PASS:?Export RCLONE_RC_PASS before running this script}"
exec "${RCLONE_BIN:-/usr/bin/rclone}" mount nzbdav: "${NZBDAV_MOUNT:-/mnt/nzbdav}" \
    --config="${RCLONE_CONFIG_FILE:-${HOME}/.config/rclone/rclone.conf}" \
    --allow-other \
    --links \
    --use-cookies \
    --dir-cache-time=1h \
    --vfs-cache-mode=off \
    --vfs-read-chunk-size=64M \
    --vfs-read-chunk-streams=0 \
    --vfs-read-chunk-size-limit=512M \
    --buffer-size="${NZBDAV_BUFFER_SIZE:-128M}" \
    --read-only \
    --rc \
    --rc-addr="${RCLONE_RC_ADDR:-0.0.0.0:5572}" \
    --rc-user="${RCLONE_RC_USER:-admin}" \
    --rc-pass="$RCLONE_RC_PASS" \
    "$@"
