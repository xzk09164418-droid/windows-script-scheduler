# -*- coding: utf-8 -*-
"""
进程管理原语：
- 启动程序（可带参数、工作目录、新控制台窗口）
- 按 "进程名通配符 / 路径子串" 查找进程（复刻 taskkill /im 与 PowerShell 路径过滤）
- 杀进程树（等价 taskkill /f /t）
"""
import fnmatch
import logging
import os
import subprocess
import time

import psutil

log = logging.getLogger("proc")


def start_process(exe, args=None, cwd=None, new_console=True):
    """启动外部程序，返回 Popen 句柄。Windows 下默认弹出新控制台窗口。"""
    cmd = [exe] + list(args or [])
    creationflags = 0
    if os.name == "nt" and new_console:
        creationflags = subprocess.CREATE_NEW_CONSOLE
    log.info("启动程序: %s (cwd=%s)", " ".join(cmd), cwd or os.getcwd())
    return subprocess.Popen(cmd, cwd=cwd or None, creationflags=creationflags)


def _norm(p):
    return (p or "").lower().replace("/", "\\")


def _match_one(name, exe, matcher):
    """matcher: {"names": [...], "path_contains": "...", "exclude_names": [...]}
    names 与 path_contains 同时给出时取与；exclude_names 命中则直接排除
    （用于把 MuMu 的系统服务进程从 MuMu* 通配中剔除）。"""
    lname = name.lower()
    exclude = matcher.get("exclude_names")
    if exclude and any(fnmatch.fnmatch(lname, n.lower()) for n in exclude):
        return False
    names = matcher.get("names")
    if names:
        if not any(fnmatch.fnmatch(lname, n.lower()) for n in names):
            return False
    sub = matcher.get("path_contains")
    if sub:
        if _norm(sub) not in _norm(exe):
            return False
    if not names and not sub:
        return False
    return True


def _match_process(process, name, exe, matcher):
    root = matcher.get("python_root")
    if not root:
        return _match_one(name, exe, matcher)
    if name.lower() not in ("python.exe", "pythonw.exe"):
        return False
    root = _norm(os.path.abspath(root)).rstrip("\\")
    def inside(value):
        path = _norm(value).strip('"').rstrip("\\")
        return path == root or path.startswith(root + "\\")
    if inside(exe):
        return True
    try:
        if inside(process.cwd()):
            return True
        return any(inside(arg) for arg in process.cmdline()[1:])
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except psutil.AccessDenied:
        # Unknown ownership must not be interpreted as three minutes of quiet.
        raise


def find_processes(matchers):
    """matchers: matcher 列表（列表间是"或"）。返回 psutil.Process 列表。"""
    #进程查询
    result = []
    for p in psutil.process_iter(["name", "exe", "status"]):
        try:
            # 跳过僵尸进程：类 Unix 下子进程退出后若父进程未 wait 会残留为僵尸，
            # 不剔除会导致"进程消失"判定永远不成立（Windows 无此问题，但无害）
            if p.info.get("status") == psutil.STATUS_ZOMBIE:
                continue
            name = p.info["name"] or ""
            exe = p.info["exe"] or ""
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if any(_match_process(p, name, exe, m) for m in matchers):
            result.append(p)
    return result


def find_processes_multi(matchers_list):
    """
    一次性遍历系统进程，返回每个子列表对应的 PID 集合。
    matchers_list: [[matcher1, matcher2], [matcher3], ...]
    """
    results = [set() for _ in matchers_list]
    for p in psutil.process_iter(["name", "exe", "status"]):
        try:
            if p.info.get("status") == psutil.STATUS_ZOMBIE:
                continue
            name = p.info["name"] or ""
            exe = p.info["exe"] or ""
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        pid = p.pid
        for idx, matchers in enumerate(matchers_list):
            for m in matchers:
                if _match_process(p, name, exe, m):
                    results[idx].add(pid)
                    break   # 一个进程只匹配本组一次
    return results

def any_alive(matchers):
    return bool(find_processes(matchers))


def kill_tree(proc_obj):
    """先杀全部子进程，再杀自身（等价 taskkill /f /t）。
    返回 False 表示权限不足/受保护杀不动；进程本就不在了算成功。"""
    try:
        children = proc_obj.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        children = []
    for c in children:
        try:
            c.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    try:
        proc_obj.kill()
        return True
    except psutil.NoSuchProcess:
        return True
    except psutil.AccessDenied:
        return False


def kill_matched(matchers):
    """按 matcher 查找并杀掉进程树，返回杀掉的进程数。杀不动的会报警。"""
    killed = 0
    for p in find_processes(matchers):
        try:
            pid, name = p.pid, p.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if kill_tree(p):
            log.info("杀死进程: pid=%s name=%s", pid, name)
            killed += 1
        else:
            log.warning("杀死进程失败（权限不足或受保护的系统进程）: pid=%s name=%s",
                        pid, name)
    if killed:
        # 等系统真正回收
        time.sleep(1)
    return killed



def minimize_windows(matchers):
    """
    把匹配进程的可见顶层窗口全部最小化（仅 Windows；其他平台返回 0 空操作）。
    用于用户活动暂停时：adb 模拟器类程序不关进程，只把模拟器窗口收起来。
    """
    if os.name != "nt":
        return 0
    import ctypes
    from ctypes import wintypes

    pids = set()
    for p in find_processes(matchers):
        try:
            pids.add(p.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if not pids:
        return 0

    user32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                     wintypes.LPARAM)
    found = []

    def _enum_cb(hwnd, _lp):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    cb = WNDENUMPROC(_enum_cb)          # 保持引用防止 GC
    user32.EnumWindows(cb, 0)
    minimized = 0
    for hwnd in found:
        if user32.ShowWindow(hwnd, 6):  # SW_MINIMIZE = 6
            minimized += 1
        else:
            # ShowWindow 返回"之前是否可见"，为 0 不代表失败；兜底再发一次
            user32.ShowWindow(hwnd, 6)
            minimized += 1
    if minimized:
        log.info("已最小化 %d 个窗口（进程保持运行）", minimized)
    return minimized
