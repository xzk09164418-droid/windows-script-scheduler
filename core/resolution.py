"""Bounded startup inspection and best-effort window correction for games."""
from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import logging
import math
import os
import time

import psutil

log = logging.getLogger("resolution")


def validate(config):
    if config is None:
        return
    if not isinstance(config, dict):
        raise ValueError("resolution_check 必须是对象")
    for name in ("enabled", "require_1080p", "restart_with_args"):
        if name in config and not isinstance(config[name], bool):
            raise ValueError(f"resolution_check.{name} 必须为 true/false")
    for name, default, minimum in (("inspect_seconds", 180, 1), ("restart_grace_seconds", 15, 0)):
        value = config.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
            raise ValueError(f"resolution_check.{name} 无效")
    if config.get("inspect_seconds", 180) > 180:
        raise ValueError("resolution_check.inspect_seconds 不能超过 180")
    if config.get("engine", "auto") not in ("auto", "unity", "ue", "ue4", "ue5"):
        raise ValueError("resolution_check.engine 必须为 auto/unity/ue/ue4/ue5")
    args = config.get("launch_args", {})
    if not isinstance(args, dict):
        raise ValueError("resolution_check.launch_args 必须是引擎到参数列表的映射")
    for engine, values in args.items():
        if engine not in ("unity", "ue", "ue4", "ue5") or not isinstance(values, list) or not values or any(not isinstance(v, str) or not v for v in values):
            raise ValueError("launch_args 的 unity/ue 参数必须为非空字符串列表")
    if config.get("require_1080p") and config.get("enabled", True) and config.get("restart_with_args", True):
        engines = ("unity", "ue") if config.get("engine", "auto") == "auto" else (config["engine"],)
        if any(e not in args for e in engines):
            raise ValueError("1080p 游戏必须在 launch_args 中配置对应引擎启动参数")


def window_state(pid, window_classes=("UnityWndClass", "UnrealWindow")):
    """Largest visible Unity/Unreal client area, in physical pixels; None if unknown.

    Hidden/minimized windows, dialogs and inaccessible measurements never count
    as resolution failures. No activation, resize, display or input APIs are used.
    """
    if os.name != "nt":
        return None
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    for name in ("IsWindowVisible", "IsIconic"):
        getattr(user32, name).argtypes = [wintypes.HWND]
        getattr(user32, name).restype = wintypes.BOOL
    user32.SetThreadDpiAwarenessContext.argtypes = [wintypes.HANDLE]
    user32.SetThreadDpiAwarenessContext.restype = wintypes.HANDLE
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = wintypes.LONG
    sizes = []

    @callback_type
    def visit(hwnd, unused):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, name, len(name))
        if name.value not in window_classes:
            return True
        rect = wintypes.RECT()
        if user32.GetClientRect(hwnd, ctypes.byref(rect)):
            width, height = rect.right - rect.left, rect.bottom - rect.top
            if width > 0 and height > 0:
                sizes.append(Window(hwnd, (width, height), (user32.GetWindowLongW(hwnd, -16) & 0x00C00000) == 0x00C00000, "unity" if name.value == "UnityWndClass" else "ue"))
        return True

    previous = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    if not previous:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not user32.EnumWindows(visit, 0):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        user32.SetThreadDpiAwarenessContext(previous)
    return max(sizes, key=lambda s: s.size[0] * s.size[1]) if sizes else None


@dataclass(frozen=True)
class Window:
    hwnd: int
    size: tuple
    windowed: bool
    engine: str


def client_size(pid, window_classes=("UnityWndClass", "UnrealWindow")):
    state = window_state(pid, window_classes)
    return state.size if state else None


def force_windowed(pid, state):
    """Apply a framed 1920x1080 physical client area to this game's window only."""
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    u.GetWindowLongW.restype = wintypes.LONG
    u.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
    u.SetWindowLongW.restype = wintypes.LONG
    u.SetThreadDpiAwarenessContext.argtypes = [wintypes.HANDLE]
    u.SetThreadDpiAwarenessContext.restype = wintypes.HANDLE
    u.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    u.GetMenu.argtypes = [wintypes.HWND]
    u.GetMenu.restype = wintypes.HMENU
    u.AdjustWindowRectEx.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    owner = wintypes.DWORD()
    u.GetWindowThreadProcessId(state.hwnd, ctypes.byref(owner))
    if owner.value != pid:
        return False
    old = u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    if not old:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        style = (u.GetWindowLongW(state.hwnd, -16) & ~0x80000000) | 0x00CF0000
        exstyle = u.GetWindowLongW(state.hwnd, -20) & ~0x00000008
        u.ShowWindowAsync(state.hwnd, 9)
        for index, value in ((-16, style), (-20, exstyle)):
            ctypes.set_last_error(0)
            if not u.SetWindowLongW(state.hwnd, index, value) and ctypes.get_last_error():
                raise ctypes.WinError(ctypes.get_last_error())
        rect = wintypes.RECT(0, 0, 1920, 1080)
        if not u.AdjustWindowRectEx(ctypes.byref(rect), style, bool(u.GetMenu(state.hwnd)), exstyle):
            raise ctypes.WinError(ctypes.get_last_error())
        if not u.SetWindowPos(state.hwnd, ctypes.c_void_p(-2), 0, 0, rect.right-rect.left, rect.bottom-rect.top, 0x0030):
            raise ctypes.WinError(ctypes.get_last_error())
        result = window_state(pid)
        return bool(result and result.windowed and result.size == (1920, 1080))
    finally:
        u.SetThreadDpiAwarenessContext(old)


def launch_arguments(original, extra):
    """Preserve authentication/launcher arguments, replacing display switches."""
    value_flags = {"-screen-width", "-screen-height", "-screen-fullscreen", "-window-mode", "-resx", "-resy"}
    flags = {"-windowed", "-fullscreen", "-full-screen", "-popupwindow", "-borderless"}
    result = []
    it = iter(original)
    for arg in it:
        key = arg.lower().split("=", 1)[0]
        if key in value_flags:
            if "=" not in arg:
                next(it, None)
        elif key not in flags:
            result.append(arg)
    return result + list(extra)


class RestartRequested(Exception):
    """A launch plan only; the owning task must stop and restart the entire group."""
    def __init__(self, launch):
        super().__init__("需要整组参数重启")
        self.launch = launch


class Watcher:
    def __init__(self, config=None):
        validate(config)
        self.config = config or {}
        self.enabled = self.config.get("enabled", True) and self.config.get("require_1080p", False)
        self._runs = {}
        self._ignored_exits = set()
        self._children = []

    def consume_counted_exits(self, exited):
        """Only our deliberate game restart is exempt from normal exit counting."""
        ignored = self._ignored_exits.intersection(exited)
        self._ignored_exits.difference_update(exited)
        return ignored

    def _restart(self, process, state, run):
        engine = self.config.get("engine", "auto")
        if engine == "auto":
            engine = state.engine
        # Windows may allow reading the command line while denying access to
        # the working directory (which requires reading process memory).
        stage = "读取启动信息"
        try:
            exe = process.exe()
            original = process.cmdline()
            if not original:
                raise OSError("原始命令行为空，无法安全保留启动器参数")
            args = launch_arguments(original[1:], self.config["launch_args"][engine])
            stage = "读取工作目录"
            try:
                cwd = process.cwd()
            except psutil.AccessDenied:
                cwd = os.path.dirname(exe)
                log.info("pid=%s 工作目录读取被拒绝，使用游戏程序目录", process.pid)
            cwd = cwd or os.path.dirname(exe)
        except (psutil.Error, OSError, KeyError) as exc:
            log.warning("pid=%s 参数重启准备失败，步骤=%s，异常=%s", process.pid, stage, type(exc).__name__)
            raise
        run["done"] = True
        raise RestartRequested({"exe": exe, "cwd": cwd, "args": args, "role": "game"})

    def poll(self, pids):
        if not self.enabled:
            return
        for pid in pids:
            run = None
            try:
                process = psutil.Process(pid)
                identity = (pid, process.create_time())
                now = time.time()
                if identity not in self._runs and now >= identity[1] + self.config.get("inspect_seconds", 180):
                    continue  # Only new processes are inspected.
                run = self._runs.setdefault(identity, {"deadline": identity[1] + self.config.get("inspect_seconds", 180), "restarted": False, "done": False, "ready": 0})
                if run["done"]:
                    continue
                state = window_state(pid)
                if not process.is_running() or psutil.Process(pid).create_time() != identity[1]:
                    continue
                expired = now >= run["deadline"]
                if state and state.size == (1920, 1080) and state.windowed:
                    run["done"] = expired
                    continue  # Continue inspecting healthy games for the entire startup interval.
                if expired:
                    run["done"] = True
                    if state:
                        self._force(pid, state)
                    else:
                        log.info("pid=%s 三分钟巡检结束，窗口不可测；交由原重试机制，不计失败", pid)
                    continue
                if not state or now < run["ready"]:
                    continue
                if not run["restarted"] and self.config.get("restart_with_args", True):
                    try:
                        self._restart(process, state, run)
                        run["restarted"] = True
                        continue
                    except (psutil.Error, OSError, KeyError) as exc:
                        log.info("pid=%s 参数重启未完成：%s: %s", pid, type(exc).__name__, exc)
                        if not process.is_running():
                            run["done"] = True
                            continue  # Never resize the destroyed window.
                        # A temporary access failure during startup must not
                        # permanently consume this process's restart attempt.
                        run["ready"] = now + max(1, self.config.get("restart_grace_seconds", 15))
                        continue
                run["done"] = True
                self._force(pid, state)
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except (psutil.AccessDenied, OSError) as exc:
                if run is not None and time.time() >= run["deadline"]:
                    run["done"] = True
                log.info("pid=%s 窗口巡检暂不可用：%s，不计失败", pid, exc)

    @staticmethod
    def _force(pid, state):
        try:
            applied = force_windowed(pid, state)
            log.info("pid=%s 强制1080p窗口化结果=%s，退出巡检，不计失败", pid, applied)
        except OSError as exc:
            log.info("pid=%s 强制窗口调整未完成：%s；退出巡检，不计失败", pid, exc)
