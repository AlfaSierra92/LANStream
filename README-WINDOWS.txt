LANStream 1.0.0 for Windows
===========================

LANStream runs as a normal Windows application. Docker and Python are not
required after the package has been built.

Release folder
--------------
LANStream\
    LANStream.exe
    config.txt
    ffmpeg.exe
    ffprobe.exe
    cache\
    _internal\

Configuration
-------------
Edit config.txt beside LANStream.exe.

Example:

port=8060
use_master_password=false
master_password=
transcoder=auto
low_bandwidth_resolution=854x480
low_bandwidth_bitrate=2M

D:\Movies
D:\TV
\\NAS\Media\Home Videos

UNC network paths work as long as the Windows account running LANStream can
read them.

Starting LANStream
------------------
Double-click LANStream.exe.

The console displays:
- LANStream version
- localhost URL
- LAN URL
- selected transcoder

Leave the console open while LANStream is running. Windows may request firewall
permission; for normal LAN use, allow access on Private networks.

Transcoding
-----------
With transcoder=auto LANStream tests:
1. Intel QSV
2. NVIDIA NVENC
3. AMD AMF
4. Software libx264

The included FFmpeg build must provide the relevant encoder. Visit
/transcoder-status to see backend tests and active streams.

Building
--------
1. Install Python 3 on Windows.
2. Put ffmpeg.exe and ffprobe.exe in:
       tools\
3. Run:
       build_windows.bat
4. The distributable folder is:
       dist\LANStream\

End users of the built package do not need Python.
