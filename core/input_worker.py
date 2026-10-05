"""Dedicated low-load hook host. Invoked only by InputHookProcess."""
import argparse
import ctypes
from ctypes import wintypes
import json
import mmap
import threading
import time

from .activity import ActivityMonitor
from .input_process import (SIZE, LAST_INPUT, HEARTBEAT, STATE, STOP, THREAD_ID,
                            ERROR, READY, FAILED, EXITED, WM_HEARTBEAT,
                            MAX_MESSAGE_LAG, MESSAGE_COUNT,
                            read_int, write_int, read_double, write_double)


class HookHost(ActivityMonitor):
    def __init__(self, memory, hotkeys):
        super().__init__({"enabled": True, "control_hotkeys": hotkeys})
        self.memory = memory
        self._foreground_users = 1

    def _on_real_input(self):
        write_double(self.memory, LAST_INPUT, time.time())

    def _on_hook_message(self, message, wparam=0):
        if message == WM_HEARTBEAT:
            now = time.monotonic()
            write_double(self.memory, HEARTBEAT, now)
            if wparam:
                lag = max(0, now * 1000 - wparam)
                write_double(self.memory, MAX_MESSAGE_LAG, max(lag, read_double(self.memory, MAX_MESSAGE_LAG)))
                write_int(self.memory, MESSAGE_COUNT, read_int(self.memory, MESSAGE_COUNT) + 1)


def run(mapping, parent_pid, hotkeys):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    user.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.PostThreadMessageW.restype = wintypes.BOOL
    memory = mmap.mmap(-1, SIZE, tagname=mapping)
    parent = kernel.OpenProcess(0x00100000, False, parent_pid)  # SYNCHRONIZE only
    watcher = None
    finished = threading.Event()
    try:
        if not parent:
            raise OSError(ctypes.get_last_error(), "无法监视调度器进程")
        host = HookHost(memory, hotkeys)
        thread_id = threading.get_native_id()
        write_int(memory, THREAD_ID, thread_id)

        def watch_parent():
            while not finished.wait(.2):
                if read_int(memory, STOP) or kernel.WaitForSingleObject(parent, 0) != 0x102:
                    host._stop_event.set()
                    user.PostThreadMessageW(thread_id, 0x0012, 0, 0)
                    return
                user.PostThreadMessageW(thread_id, WM_HEARTBEAT, int(time.monotonic() * 1000), 0)

        class ReadySignal:
            def set(self):
                if not (host._h_kbd and host._h_mouse):
                    raise RuntimeError("鼠标或键盘钩子安装失败")
                write_double(memory, HEARTBEAT, time.monotonic())
                write_int(memory, STATE, READY)

        host._hooks_ready = ReadySignal()
        watcher = threading.Thread(target=watch_parent, name="input-parent-watch", daemon=True)
        watcher.start()
        host._hook_loop()
        write_int(memory, STATE, EXITED)
    except BaseException as exc:
        message = str(exc).encode("utf-8")[:SIZE - ERROR - 1]
        memory[ERROR:ERROR + len(message)] = message
        write_int(memory, STATE, FAILED)
    finally:
        finished.set()
        if watcher:
            watcher.join(timeout=1)
        if parent:
            kernel.CloseHandle(parent)
        memory.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mapping", required=True)
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("--hotkeys", required=True)
    args = parser.parse_args()
    run(args.mapping, args.parent, json.loads(args.hotkeys))
