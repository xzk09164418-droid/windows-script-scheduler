# -*- coding: utf-8 -*-
"""
全局热键管理器 —— 基于 RegisterHotKey API，不依赖 SetWindowsHookEx。
即使 activity_pause.enabled=false，热键仍然生效。
"""
import ctypes
import logging
import threading
from ctypes import wintypes

from . import control

log = logging.getLogger("hotkey")

# ---------- 修饰键映射（RegisterHotKey 的 fsModifiers）----------
_MOD_FS = {
    "ctrl":  0x0002,   # MOD_CONTROL
    "alt":   0x0001,   # MOD_ALT
    "shift": 0x0004,   # MOD_SHIFT
}

# ---------- 特殊键虚拟键码 ----------
_OEM_VK = {
    "=": 0xBB, "equal": 0xBB,
    "+": 0xBB, "plus":  0xBB,
    "-": 0xBD, "minus": 0xBD,
    "_": 0xBD,
    "num+": 0x6B, "add":      0x6B,
    "num-": 0x6D, "subtract": 0x6D,
}

DEFAULT_HOTKEYS = {
    "pause_resume":   "ctrl+alt+=",
    "prev_task":      "ctrl+alt+-",
    "next_task":      "ctrl+alt+shift+=",
    "reset_progress": "ctrl+alt+0",
}


def parse_hotkey(hotkey: str):
    """解析热键字符串 → (fsModifiers, vk)；失败返回 None。"""
    try:
        parts = [p.strip().lower() for p in str(hotkey).split("+") if p.strip()]
        mods, key = 0, None
        for p in parts:
            if p in _MOD_FS:
                mods |= _MOD_FS[p]
            else:
                key = p
        if key is None:
            return None
        # 特殊键
        if key in _OEM_VK:
            return mods, _OEM_VK[key]
        # 单字符键（字母/数字）
        if len(key) == 1:
            ch = key.upper()
            if ch.isalnum():
                return mods, ord(ch)
        # F1–F12
        if key.startswith("f") and key[1:].isdigit():
            n = int(key[1:])
            if 1 <= n <= 12:
                return mods, 0x70 + n - 1
    except Exception:
        pass
    return None


class HotkeyManager:
    """全局热键管理器（RegisterHotKey + 线程消息循环）。"""

    def __init__(self, hotkeys=None, on_pause_toggle=None):
        self._hotkeys = dict(DEFAULT_HOTKEYS)
        if hotkeys:
            for k in self._hotkeys:
                if hotkeys.get(k):
                    self._hotkeys[k] = str(hotkeys[k])
        # 可选回调：暂停切换时通知 ActivityMonitor 重置空闲计时器
        self._on_pause_toggle = on_pause_toggle
        self._thread = None
        self._stop_event = threading.Event()
        self._id_map = {}          # 热键 ID → 动作名
        self._next_id = 1

    # ---------- 生命周期 ----------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="hotkey-thread", daemon=True)
        self._thread.start()
        log.info("全局热键已注册: %s",
                 " | ".join(f"{v}={k}" for k, v in self._hotkeys.items()))

    def stop(self):
        if self._thread and self._thread.is_alive():
            self._stop_event.set()
            # 向热键线程发送一条消息唤醒 GetMessageW
            try:
                ctypes.windll.user32.PostThreadMessageW(
                    self._thread.ident, 0x0400, 0, 0)
            except Exception:
                pass
            self._thread.join(timeout=3.0)
            self._thread = None

    # ---------- 内部分发 ----------
    def _dispatch(self, name):
        if name == "pause_resume":
            control.request_pause_toggle()
            if self._on_pause_toggle:
                self._on_pause_toggle(control.is_paused())
        elif name == "prev_task":
            control.request_prev()
        elif name == "next_task":
            control.request_skip()
        elif name == "reset_progress":
            control.request_restart()

    # ---------- 消息循环线程 ----------
    def _run(self):
        user32 = ctypes.windll.user32

        # 声明 API 签名
        user32.RegisterHotKey.argtypes = [
            wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL

        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL

        user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND,
            wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = wintypes.BOOL

        # 注册全部热键（hWnd=NULL → 消息发到本线程队列）
        for name, combo in self._hotkeys.items():
            parsed = parse_hotkey(combo)
            if parsed is None:
                log.warning("无法解析热键 %s=%r，跳过", name, combo)
                continue
            mods, vk = parsed
            hid = self._next_id
            self._next_id += 1
            if not user32.RegisterHotKey(None, hid, mods, vk):
                err = ctypes.get_last_error()
                log.warning("注册热键失败 %s=%r (err=%d)，可能已被占用",
                            name, combo, err)
            else:
                self._id_map[hid] = name

        if not self._id_map:
            log.error("没有成功注册任何热键，热键功能不可用")

        # 消息循环
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if self._stop_event.is_set():
                break
            if msg.message == 0x0312:          # WM_HOTKEY
                name = self._id_map.get(msg.wParam)
                if name:
                    try:
                        self._dispatch(name)
                    except Exception:
                        log.exception("热键分发异常: %s", name)
            # 正常消息继续派发（实际上没有窗口，派发无操作）
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        # 注销全部热键
        for hid in list(self._id_map):
            user32.UnregisterHotKey(None, hid)
        self._id_map.clear()
        log.info("热键管理器已停止")
