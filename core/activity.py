# -*- coding: utf-8 -*-
"""
用户活动检测 + 队列控制热键（仅 Windows 桌面会话）。

真实输入 vs 脚本模拟输入的区分：
- 通过 SetWindowsHookEx 安装 WH_KEYBOARD_LL / WH_MOUSE_LL 低级钩子；
- pyautogui / pydirectinput / SendInput / keybd_event / mouse_event 等
  模拟出来的输入，事件 flags 里带 LLKHF_INJECTED / LLMHF_INJECTED（或
  LOWER_IL_INJECTED）标志，直接过滤，不算用户活动；
- 真实键盘鼠标产生的事件不带注入标志 → 更新"最近真实输入"时间戳；
- 纯修饰键（Ctrl/Alt/Shift 本身）与控制热键组合不算用户活动。

MuMu 远控说明：
- 远控的触摸/按键通过网络直达安卓虚拟机，不经过 Windows 桌面输入栈，
  钩子完全看不到——所以远控【不会】被误判为用户在用电脑（不会误暂停），
  但也【无法】被自动感知。远控期间请用控制热键手动暂停/恢复。
- MAA / M9A / MaaFgo 等走 adb 控制模拟器的脚本同样不产生桌面输入，
  天然不会触发暂停。

已知例外：
- 使用驱动级模拟（Interception、DD 驱动、虚拟 HID 等）的工具产生的输入
  不带注入标志，会被当成真实输入。若你的脚本使用这类方式模拟键鼠，
  请不要开启本功能（会自我触发暂停）。

控制热键（默认）：
  Ctrl+Alt+=        暂停（保存任务进度）/ 从暂停处继续
  Ctrl+Alt+-        从上一个 start 类任务开始运行
  Ctrl+Alt+Shift+=  跳过当前任务，直接运行下一个（即 Ctrl+Alt+加号）
  Ctrl+Alt+0        任务进度清零：打断当前任务，从队列头重新运行
"""
import logging
import os
import threading
import time
from contextlib import contextmanager

from . import control
from .hotkey import HotkeyManager
from .input_process import InputHookProcess


log = logging.getLogger("activity")

# 注入事件标志
_LLKHF_INJECTED = 0x10
_LLKHF_LOWER_IL_INJECTED = 0x02
_LLMHF_INJECTED = 0x01
_LLMHF_LOWER_IL_INJECTED = 0x02

_MOD_VK = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10}
# 修饰键本身（按下不算"使用电脑"）
_MODIFIER_VKS = {0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}
# 标点键的虚拟键码（美式布局）
_OEM_VK = {
    "=": 0xBB, "equal": 0xBB,
    "+": 0xBB, "plus": 0xBB,
    "-": 0xBD, "minus": 0xBD,
    "_": 0xBD,
    "num+": 0x6B, "add": 0x6B,
    "num-": 0x6D, "subtract": 0x6D,
}

DEFAULT_HOTKEYS = {
    "pause_resume":   "ctrl+alt+=",
    "prev_task":      "ctrl+alt+-",
    "next_task":      "ctrl+alt+shift+=",
    "reset_progress": "ctrl+alt+0",
}


def _parse_hotkey(hotkey):
    """"ctrl+alt+=" → ({0x11, 0x12}, 0xBB)。解析失败返回 None。"""
    try:
        parts = [p.strip().lower() for p in str(hotkey).split("+") if p.strip()]
        mods, key = set(), None
        for p in parts:
            if p in _MOD_VK:
                mods.add(_MOD_VK[p])
            else:
                key = p
        if key is None:
            return None
        if key in _OEM_VK:
            return mods, _OEM_VK[key]
        if len(key) == 1:                       # 字母/数字：vk 即大写 ASCII
            ch = key.upper()
            if ch.isalnum():
                return mods, ord(ch)
        if key.startswith("f") and key[1:].isdigit():   # F1~F12
            n = int(key[1:])
            if 1 <= n <= 12:
                return mods, 0x70 + n - 1
    except Exception:
        pass
    return None


class ActivityMonitor:
    """
    后台任务不安装键盘或鼠标活动钩子，控制热键使用独立的 RegisterHotKey。
    """

    def __init__(self, cfg=None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.idle_seconds = int(cfg.get("idle_resume_seconds", 300))
        ch = cfg.get("control_hotkeys") or {}
        self.hotkeys = dict(DEFAULT_HOTKEYS)
        for k in self.hotkeys:
            if ch.get(k):
                self.hotkeys[k] = str(ch[k])
        # 兼容旧字段 manual_hotkey（等价于 pause_resume）
        if cfg.get("manual_hotkey") and not ch.get("pause_resume"):
            self.hotkeys["pause_resume"] = str(cfg["manual_hotkey"])

        self._last_real_input = 0.0             # 最近真实输入时间戳；0 = 启动时视为空闲
        self._hook_process = None
        self._scope_lock = threading.RLock()
        self._foreground_users = 0
        self._stop_event = threading.Event()    # 用于安全退出消息循环
        self._hooks_ready = threading.Event()
        # 必须保持引用，防止被 GC 回收导致钩子崩溃
        self._kbd_proc = None
        self._mouse_proc = None
        self._h_kbd = None
        self._h_mouse = None

        # ===== 热键管理器：不再传递 on_pause_toggle 回调 =====
        self._hotkey_mgr = HotkeyManager(hotkeys=self.hotkeys)



    # ---------------- 生命周期 ----------------

    def start(self):
        # 热键始终可用
        self._hotkey_mgr.start()



        # 如果没启用活动检测，只保留热键
        if not self.enabled:
            log.info("用户活动检测未启用（activity_pause.enabled=false），控制热键仍然可用")
            return
        if os.name != "nt":
            log.warning("用户活动检测仅支持 Windows，当前平台已自动禁用")
            self.enabled = False
            return

        log.info("活动检测就绪，仅前台任务运行/等待期间安装钩子")

    def _start_hooks(self):
        if not self.enabled or self._hook_process is not None:
            return
        worker = InputHookProcess(self.hotkeys)
        worker.start()
        self._hook_process = worker
        log.info("[活动检测] 键盘检测已加载（独立进程 pid=%s）", worker.process.pid)
        log.info("[活动检测] 鼠标检测已加载（独立进程，与监控扫描隔离）")

    def _stop_hooks(self):
        """结束专用进程；前台最后一个使用者离开后不保留全局钩子。"""
        if self._hook_process is not None:
            worker, self._hook_process = self._hook_process, None
            self._last_real_input = max(self._last_real_input, worker.last_input)
            worker.stop()
            self._last_real_input = max(self._last_real_input, worker.last_input)
            log.info("[活动检测] 键盘检测已卸载（不再检测键盘活动）")
            log.info("[活动检测] 鼠标检测已卸载（不再检测鼠标活动）")

    def _refresh_input(self):
        with self._scope_lock:
            worker = self._hook_process
            if worker is not None:
                self._last_real_input = max(self._last_real_input, worker.last_input)
                if not worker.healthy():
                    log.warning("键鼠检测进程退出或消息循环失去响应，重建检测并保守暂停")
                    self._stop_hooks()
                    self._last_real_input = time.time()
                    self._start_hooks()

    def stop(self):
        self._stop_hooks()
        self._hotkey_mgr.stop()

    @contextmanager
    def task_scope(self, foreground=True):
        """引用计数保证混合并行组中后台任务不会卸载前台任务的钩子。"""
        if foreground:
            with self._scope_lock:
                self._foreground_users += 1
                try:
                    if self._foreground_users == 1:
                        self._start_hooks()
                except Exception:
                    self._foreground_users -= 1
                    raise
        try:
            yield
        finally:
            if foreground:
                with self._scope_lock:
                    self._foreground_users -= 1
                    if self._foreground_users == 0:
                        self._stop_hooks()

    # ---------------- 查询 ----------------

    def user_active(self, foreground=True):
        # 手动暂停 → 始终视为活动
        if control.is_paused():
            return True
        if not foreground or not self.enabled:
            return False
        self._refresh_input()
        return (time.time() - self._last_real_input) < self.idle_seconds

    def wait_until_idle(self, name, poll=5, heartbeat=60, foreground=True, stop_event=None):
        """阻塞直到用户离开（自动暂停）或手动暂停被解除。"""
        if not self.user_active(foreground):
            return

        # 第一次提示
        if control.is_paused():
            log.warning("[%s] 手动暂停中，按 %s 从保存的任务进度恢复……",
                        name, self.hotkeys.get("pause_resume", "Ctrl+Alt+="))
        else:
            log.warning("[%s] 检测到用户真实键鼠输入，脚本暂停；"
                        "连续 %d 秒无输入后自动恢复", name, self.idle_seconds)

        last_beat = time.time()
        while self.user_active(foreground):
            if control.peek_action() or (stop_event and stop_event.is_set()):
                return
            time.sleep(poll)
            if time.time() - last_beat >= heartbeat:
                last_beat = time.time()
                if control.is_paused():
                    log.info("[%s] [心跳] 手动暂停中，按 %s 恢复",
                             name, self.hotkeys.get("pause_resume", "Ctrl+Alt+="))
                else:
                    idle_for = int(time.time() - self._last_real_input)
                    log.info("[%s] [心跳] 暂停中，已 %d 秒无真实输入，约 %d 秒后恢复",
                             name, idle_for, max(0, self.idle_seconds - idle_for))

        # 退出循环，提示恢复原因
        if control.is_paused():
            log.warning("[%s] 手动暂停已解除，从保存的任务进度恢复", name)
        else:
            log.warning("[%s] 用户已离开，恢复执行", name)

    # ---------------- 钩子回调 ----------------

    def _on_real_input(self):
        if self._foreground_users:
            self._last_real_input = time.time()

    def _keyboard_input(self, vk, flags, is_hotkey=False):
        if not (flags & (_LLKHF_INJECTED | _LLKHF_LOWER_IL_INJECTED)):
            if not is_hotkey and vk not in _MODIFIER_VKS:
                self._on_real_input()

    def _mouse_input(self, flags):
        if not (flags & (_LLMHF_INJECTED | _LLMHF_LOWER_IL_INJECTED)):
            self._on_real_input()



    # ---------------- 仅供独立进程使用的 GetMessageW 消息循环 ----------------

    def _on_hook_message(self, message, wparam=0):
        """独立钩子进程覆写此方法，报告消息循环存活状态。"""

    def _hook_loop(self):
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        WH_KEYBOARD_LL, WH_MOUSE_LL, HC_ACTION = 13, 14, 0
        WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104

        LRESULT = ctypes.c_ssize_t
        HHOOK = wintypes.HANDLE
        HINSTANCE = ctypes.c_void_p

        HOOKPROC = ctypes.WINFUNCTYPE(
            LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_size_t)]

        class MSLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_size_t)]

        # API 签名
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = HHOOK
        user32.CallNextHookEx.argtypes = [HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = LRESULT
        user32.UnhookWindowsHookEx.argtypes = [HHOOK]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND,
            wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = wintypes.BOOL
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.TranslateMessage.restype = wintypes.BOOL
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.restype = LRESULT

        # ---------- 热键识别表（仅用于过滤用户活动，不执行动作）----------
        hotkey_map = {}
        for name, combo in self.hotkeys.items():
            parsed = _parse_hotkey(combo)
            if parsed:
                mods, vk = parsed
                hotkey_map.setdefault(vk, []).append((mods, name))
        for vk in hotkey_map:
            hotkey_map[vk].sort(key=lambda x: -len(x[0]))

        def _is_hotkey(vk):
            candidates = hotkey_map.get(vk)
            if not candidates:
                return False
            for mods, _ in candidates:
                if all(user32.GetAsyncKeyState(m) & 0x8000 for m in mods):
                    return True
            return False

        # ---------- 回调（仅用于活动检测；回调在系统输入链上，
        # 绝不能写日志/抛异常，必须无条件 CallNextHookEx）----------
        def keyboard_proc(nCode, wParam, lParam):
            try:
                if nCode == HC_ACTION and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    info = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    self._keyboard_input(info.vkCode, info.flags, _is_hotkey(info.vkCode))
            except Exception:
                pass   # 回调在系统输入链上：禁止写日志/抛异常，只能吞掉
            # 无论发生什么，必须调用 CallNextHookEx，否则这一次输入会卡到超时
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        def mouse_proc(nCode, wParam, lParam):
            try:
                if nCode == HC_ACTION:
                    info = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    self._mouse_input(info.flags)
            except Exception:
                pass
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        self._kbd_proc = HOOKPROC(keyboard_proc)
        self._mouse_proc = HOOKPROC(mouse_proc)

        # ---------- 钩子安装/卸载辅助 ----------
        def _install():
            if not self._h_kbd:
                self._h_kbd = user32.SetWindowsHookExW(
                    WH_KEYBOARD_LL, self._kbd_proc, None, 0)
                if self._h_kbd:
                    log.info("[活动检测] 键盘检测已加载（前台任务）")
            if not self._h_mouse:
                self._h_mouse = user32.SetWindowsHookExW(
                    WH_MOUSE_LL, self._mouse_proc, None, 0)
                if self._h_mouse:
                    log.info("[活动检测] 鼠标检测已加载（前台任务）")
            if not self._h_kbd or not self._h_mouse:
                log.warning("钩子安装部分失败：键盘=%s 鼠标=%s",
                            bool(self._h_kbd), bool(self._h_mouse))

        def _unload():
            if self._h_kbd:
                if user32.UnhookWindowsHookEx(self._h_kbd):
                    log.info("[活动检测] 键盘检测已卸载（不再检测键盘活动）")
                else:
                    log.warning("[活动检测] 键盘钩子卸载返回失败或已被系统移除")
                self._h_kbd = None
            if self._h_mouse:
                if user32.UnhookWindowsHookEx(self._h_mouse):
                    log.info("[活动检测] 鼠标检测已卸载（不再检测鼠标活动）")
                else:
                    log.warning("[活动检测] 鼠标钩子卸载返回失败或已被系统移除")
                self._h_mouse = None

        # ==================================================================
        # ★ 修复：LL 钩子是全系统同步钩子，输入要等本线程取出消息并
        #   CallNextHookEx 后才会到达前台程序。因此这里只能阻塞式
        #   GetMessageW 常驻取消息（消息一到立即处理；无消息时线程挂起，
        #   不占 CPU），绝不能 time.sleep 轮询——sleep 多久，全系统
        #   鼠标键盘就延迟多久。
        # ==================================================================
        msg = wintypes.MSG()
        try:
            _install()
            self._hooks_ready.set()
            while not self._stop_event.is_set():
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:
                    break
                self._on_hook_message(msg.message, msg.wParam)
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            _unload()


# ---------------- 模块级单例 ----------------

_monitor = ActivityMonitor()            # 默认未启用，所有调用为空操作


def init(cfg):
    """由 main.py 在加载配置后调用。"""
    global _monitor
    _monitor = ActivityMonitor(cfg)
    _monitor.start()
    return _monitor


def get():
    return _monitor
