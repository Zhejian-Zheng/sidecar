"""Task-scoped scrcpy recording and live preview; never a completion oracle.

Use scrcpy CLI for timestamped MKV recording and its matching standalone server
for raw H.264 frames. No custom control protocol or input injection is used.
Final verification deliberately takes an independent fresh device capture.
"""

import hashlib
import io
import json
import os
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path
from uuid import uuid4

MAX_FRAME = 8_000_000


def atomic_write(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(data)
    os.replace(temp, path)


def read_png(stream):
    """Read one complete PNG, bounding corrupt or unexpectedly large frames."""
    def exact(size):
        data = bytearray()
        while len(data) < size:
            chunk = stream.read(size - len(data))
            if not chunk:
                raise EOFError("Video decoder ended")
            data.extend(chunk)
        return bytes(data)

    data = bytearray(exact(8))
    if data != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Invalid video frame")
    while True:
        header = exact(8)
        size = struct.unpack(">I", header[:4])[0]
        if len(data) + size + 12 > MAX_FRAME:
            raise ValueError("Video frame too large")
        data.extend(header)
        data.extend(exact(size + 4))
        if header[4:] == b"IEND":
            return bytes(data)


def latest_frame(directory, *, after=None, timeout=0):
    """Return only a live, recent frame, optionally received after a barrier.

Receipt time is a host decoder timestamp, not proof of device capture time.
Callers needing final evidence must use an independent device capture.
"""
    directory = Path(directory)
    deadline = time.monotonic() + timeout
    while True:
        try:
            meta = json.loads((directory / "stream.json").read_text())
            now = time.monotonic()
            if (meta["status"] == "live" and 0 <= now - meta["received_at"] < 3
                    and (after is None or meta["received_at"] > after)):
                # Retry if the writer advances between reading metadata and the image.
                data = (directory / "latest.png").read_bytes()
                if (hashlib.sha256(data).hexdigest() == meta.get("sha256")
                        and (directory / "stream.json").read_text() == json.dumps(meta)):
                    return data, meta
        except (FileNotFoundError, KeyError, ValueError):
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError("No fresh live frame; inspect the recording or reconnect")
        time.sleep(0.05)


def session_directory(home, task_id):
    root = Path(home) / "recordings" / task_id
    if not root.exists():
        return None
    attempts = sorted(p for p in root.iterdir() if p.is_dir() and (p / "stream.json").exists())
    return attempts[-1] if attempts else None


class VideoSession:
    """Own recorder, decoder and files for one execution attempt."""

    def __init__(self, home, task_id, serial, binding):
        self.binding = binding
        self.serial = serial
        self.directory = (Path(home) / "recordings" / task_id
                          / f"{time.time_ns()}-{uuid4().hex[:8]}")
        self.stop = threading.Event()
        self.recorder = self.decoder = self.server = None
        self.connection = None
        self.port = None
        self.threads = []
        self.lock = threading.Lock()
        self.meta = {"session_id": self.directory.name, "display": binding.model_dump(),
                     "status": "starting", "sequence": 0, "received_at": 0,
                     "timestamp_source": "host_decoder_receipt", "recording_complete": False,
                     "recording_finalized": False}

    def _publish(self, **values):
        with self.lock:
            self.meta.update(values)
            atomic_write(self.directory / "stream.json", json.dumps(self.meta).encode())

    def __enter__(self):
        from sidecar.displays import run, verify

        verify(self.serial, self.binding)
        binary = shutil.which("scrcpy")
        if not binary:
            raise RuntimeError("scrcpy is required")
        version = re.search(
            r"scrcpy ([0-9]+\.[0-9]+(?:\.[0-9]+)?)", run([binary, "--version"]).decode()
        )
        if not version:
            raise RuntimeError("Cannot determine scrcpy version")
        version = version[1]
        configured = os.environ.get("SCRCPY_SERVER_PATH")
        candidates = ([Path(configured)] if configured else [
            Path(binary).resolve().parent.parent / "share/scrcpy/scrcpy-server",
            Path(binary).parent.parent / "share/scrcpy/scrcpy-server",
            Path("/usr/share/scrcpy/scrcpy-server"),
        ])
        server_file = next((p for p in candidates if p.is_file()), None)
        if server_file is None:
            raise RuntimeError("Set SCRCPY_SERVER_PATH to the installed matching scrcpy-server")
        self.directory.mkdir(parents=True, mode=0o700)
        self._publish(scrcpy_version=version)
        try:
            self.decoder = subprocess.Popen(
                ["ffmpeg", "-v", "error", "-threads", "1", "-flags", "low_delay",
                 "-probesize", "32", "-analyzeduration", "1",
                 "-f", "h264", "-i", "pipe:0", "-an", "-vf", "scale=min(960\\,iw):-2",
                 "-fps_mode", "passthrough", "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            self.recorder = subprocess.Popen(
                ["scrcpy", "--serial", self.serial, f"--display-id={self.binding.id}",
                 "--no-window", "--no-audio", "--no-control", "--no-clipboard-autosync",
                 "--max-size=960", "--max-fps=5", "--video-codec=h264",
                 "--record=" + str(self.directory / "recording.mkv")],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            digest = hashlib.sha256(server_file.read_bytes()).hexdigest()[:20]
            remote = f"/data/local/tmp/sidecar-video-{digest}.jar"
            run(["adb", "-s", self.serial, "push", str(server_file), remote])
            scid = f"{int(uuid4().hex[:8], 16) & 0x7fffffff:08x}"
            self.port = int(run(["adb", "-s", self.serial, "forward", "tcp:0",
                                 f"localabstract:scrcpy_{scid}"]).decode().strip())
            command = [f"CLASSPATH={remote}", "app_process", "/",
                       "com.genymobile.scrcpy.Server", version, f"scid={scid}",
                       f"display_id={self.binding.id}", "tunnel_forward=true", "audio=false",
                       "control=false", "raw_stream=true", "cleanup=false", "power_on=false",
                       "max_size=960", "max_fps=5", "video_codec=h264"]
            self.server = subprocess.Popen(
                ["adb", "-s", self.serial, "shell", shlex.join(command)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self.connection = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            self.connection.settimeout(1)
            for target in (self._feed, self._decode):
                thread = threading.Thread(target=target, daemon=True)
                self.threads.append(thread)
                thread.start()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _feed(self):
        received = False
        deadline = time.monotonic() + 15
        try:
            while not self.stop.is_set():
                try:
                    chunk = self.connection.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    if received or time.monotonic() >= deadline or self.server.poll() is not None:
                        return
                    self.connection.close()
                    if self.stop.wait(0.1):
                        return
                    self.connection = socket.create_connection(("127.0.0.1", self.port), timeout=1)
                    self.connection.settimeout(1)
                    continue
                received = True
                self.decoder.stdin.write(chunk)
                self.decoder.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                self.decoder.stdin.close()
            except (OSError, ValueError):
                pass

    def _decode(self):
        try:
            while not self.stop.is_set():
                data = read_png(self.decoder.stdout)
                # Publish image before metadata. A digest prevents mixed generations.
                atomic_write(self.directory / "latest.png", data)
                self._publish(status="live", sequence=self.meta["sequence"] + 1,
                              received_at=time.monotonic(), sha256=hashlib.sha256(data).hexdigest())
        except (EOFError, ValueError, OSError):
            pass
        finally:
            self._publish(status="stopped")

    def __exit__(self, *exc):
        if self.recorder and self.recorder.poll() is None:
            self.recorder.send_signal(signal.SIGINT)
            try:
                self.recorder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.recorder.kill()
                self.recorder.wait(timeout=5)
        self.stop.set()
        if self.connection:
            self.connection.close()
        if self.server and self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait(timeout=3)
        if self.port:
            from sidecar.displays import run

            try:
                run(["adb", "-s", self.serial, "forward", "--remove", f"tcp:{self.port}"])
            except (RuntimeError, subprocess.TimeoutExpired, OSError):
                pass
        if self.decoder and self.decoder.poll() is None:
            self.decoder.terminate()
            try:
                self.decoder.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.decoder.kill()
                self.decoder.wait(timeout=3)
        for thread in self.threads:
            thread.join(timeout=2)
        for pipe in ((self.decoder.stdin, self.decoder.stdout) if self.decoder else ()):
            pipe.close()
        self._publish(status="stopped", recording_finalized=True, recording_complete=bool(
            self.recorder and self.recorder.returncode == 0
            and (self.directory / "recording.mkv").exists()))


def image_from_stream(directory, binding):
    from PIL import Image

    data, meta = latest_frame(directory, after=time.monotonic(), timeout=8)
    if meta["display"] != binding.model_dump():
        raise RuntimeError("Stream belongs to another display session")
    image = Image.open(io.BytesIO(data))
    image.load()
    return image
