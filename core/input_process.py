"""Windows input hook process and nonblocking shared-memory status.

No pipes, queues, file I/O or interprocess locks in the input callback.
Only the helper writes input timestamps; only the parent writes STOP.
"""
import json
import mmap
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
import uuid

SIZE = 1024
LAST_INPUT, HEARTBEAT, STATE, STOP, THREAD_ID, ERROR = 0, 8, 16, 20, 24, 64
MAX_MESSAGE_LAG, MESSAGE_COUNT = 32, 40
READY, FAILED, EXITED = 3, 4, 5
WM_HEARTBEAT = 0x8001


def read_double(memory, offset):
    return struct.unpack_from("<d", memory, offset)[0]


def write_double(memory, offset, value):
    struct.pack_into("<d", memory, offset, value)


def read_int(memory, offset):
    return struct.unpack_from("<I", memory, offset)[0]


def write_int(memory, offset, value):
    struct.pack_into("<I", memory, offset, value)


class InputHookProcess:
    def __init__(self, hotkeys):
        self.hotkeys = hotkeys
        self.memory = None
        self.process = None
        self._last_input = 0.0

    @property
    def last_input(self):
        return read_double(self.memory, LAST_INPUT) if self.memory is not None else self._last_input

    def healthy(self):
        return (self.process is not None and self.process.poll() is None
                and self.memory is not None and read_int(self.memory, STATE) == READY
                and time.monotonic() - read_double(self.memory, HEARTBEAT) < 5)

    def start(self):
        tag = "Local\\scheduler-input-" + uuid.uuid4().hex
        self.memory = mmap.mmap(-1, SIZE, tagname=tag)
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "core.input_worker", "--mapping", tag,
                 "--parent", str(os.getpid()), "--hotkeys", json.dumps(self.hotkeys)],
                cwd=str(Path(__file__).resolve().parents[1]),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.healthy():
                    return
                if self.process.poll() is not None or read_int(self.memory, STATE) == FAILED:
                    break
                time.sleep(.01)
            error = bytes(self.memory[ERROR:]).split(b"\0", 1)[0].decode("utf-8", "replace")
            raise RuntimeError("独立键鼠检测进程未能加载两项钩子" + (f"：{error}" if error else ""))
        except BaseException:
            self.stop()
            raise

    def stop(self):
        try:
            if self.memory is not None:
                write_int(self.memory, STOP, 1)
            if self.process is not None:
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    # Only this dedicated child is terminated; Windows removes its hooks.
                    self.process.kill()
                    self.process.wait(timeout=2)
        finally:
            if self.memory is not None:
                self._last_input = self.last_input
                self.memory.close()
                self.memory = None
            self.process = None
