# LANStream v0.4.5

Bugfix release for NVENC detection.

Fixed:
- Hardware capability tests previously used a 128x72 synthetic frame.
- Current NVIDIA NVENC drivers reject frame dimensions that small.
- Capability tests now use 640x360, which remains lightweight while satisfying
  normal hardware encoder minimum-dimension requirements.

This affects detection only. Actual transcoded video dimensions are unchanged.

All v0.4.4 functionality remains unchanged.
