"""Own external media processes and release them when a worker stops."""
from __future__ import annotations

import atexit
import signal
import subprocess
import threading
import time


class MediaRuntime:
    def __init__(self):
        self._lock = threading.Lock()
        self._processes = set()
        self._cleanups = {}
        self.stopping = threading.Event()
        self._finished = threading.Event()
        self._shutting_down = False

    def popen(self, command, **kwargs):
        with self._lock:
            if self.stopping.is_set():
                raise RuntimeError("LANStream is shutting down")
            kwargs.setdefault('stdin', subprocess.DEVNULL)
            process = subprocess.Popen(command, **kwargs)
            self._processes.add(process)
        return process

    def release(self, process):
        with self._lock:
            self._processes.discard(process)

    @staticmethod
    def stop_process(process):
        try:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        except ProcessLookupError:
            pass

    def run(self, command, *, capture_output=False, timeout=None, check=False, input=None, **kwargs):
        if capture_output:
            kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if input is not None:
            kwargs['stdin'] = subprocess.PIPE
        process = self.popen(command, **kwargs)
        try:
            try:
                stdout, stderr = process.communicate(input=input, timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
                raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
            result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            if check:
                result.check_returncode()
            return result
        finally:
            try:
                self.stop_process(process)
            finally:
                self.release(process)
                for pipe in (process.stdout, process.stderr):
                    if pipe:
                        pipe.close()

    def add_cleanup(self, key, callback):
        with self._lock:
            if not self.stopping.is_set():
                self._cleanups[key] = callback
                return
        callback()

    def remove_cleanup(self, key):
        with self._lock:
            self._cleanups.pop(key, None)

    def shutdown(self):
        self.stopping.set()
        with self._lock:
            if self._shutting_down:
                owner = False
            else:
                self._shutting_down = True
                owner = True
                processes = list(self._processes)
                cleanups = list(self._cleanups.values())
                self._cleanups.clear()
        if not owner:
            self._finished.wait(4)
            return
        try:
            # Signal every child first so the timeout is shared across streams.
            for process in processes:
                try:
                    if process.poll() is None:
                        process.terminate()
                except OSError:
                    pass
            for callback in cleanups:
                try:
                    callback()
                except Exception:
                    pass
            deadline = time.monotonic() + 2
            for process in processes:
                try:
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except OSError:
                        pass
            deadline = time.monotonic() + 1
            for process in processes:
                try:
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except (OSError, subprocess.TimeoutExpired):
                    pass
                self.release(process)
        finally:
            self._finished.set()

    def install_signal_handlers(self):
        # Cleanup runs outside the signal handler: it can safely acquire locks
        # even if SIGTERM interrupted a request while registering a process.
        threading.Thread(target=self._wait_for_shutdown, name='media-shutdown', daemon=True).start()
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous = signal.getsignal(signum)

            def handle(sig, frame, previous=previous):
                self.stopping.set()
                if callable(previous):
                    previous(sig, frame)
                elif previous == signal.SIG_DFL:
                    raise SystemExit(0)

            signal.signal(signum, handle)

    def _wait_for_shutdown(self):
        self.stopping.wait()
        self.shutdown()


media_runtime = MediaRuntime()
atexit.register(media_runtime.shutdown)
