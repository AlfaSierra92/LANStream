from __future__ import annotations

import socket
import sys

from waitress import serve

from app import SERVER_PORT, app, selected_transcoder, media_runtime


def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    media_runtime.install_signal_handlers()
    status = selected_transcoder()
    print("")
    print("LANStream")
    print("=========")
    print(f"Local:   http://127.0.0.1:{SERVER_PORT}")
    print(f"Network: http://{local_ip()}:{SERVER_PORT}")
    print(f"Transcoder: {status['label']}")
    print("")
    print("Leave this window open while LANStream is running.")
    print("Press Ctrl+C to stop.")
    print("")
    try:
        serve(app, host="0.0.0.0", port=SERVER_PORT, threads=8)
    except KeyboardInterrupt:
        pass
    finally:
        media_runtime.shutdown()
