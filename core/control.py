# -*- coding: utf-8 -*-
"""
队列运行控制状态：被热键触发的控制动作（跳过/上一个/从头）在这里暂存，
任务的轮询循环检测到后抛出 TaskInterrupted，由调度器统一清理现场并
调整执行位置（保存/恢复任务进度）。

动作定义（用户对"任务"的口径 = config 里的 start 类任务）：
  pause    暂停（保存进度）——由真实输入自动触发，或 pause_resume 热键手动触发；
           恢复后从保存的位置继续（monitor 回到其主控 launch 任务）
  next     跳过当前任务，直接运行下一个
  prev     从上一个 start 类任务开始运行
  restart  进度清零：打断当前任务，从队列头重新运行
"""
import logging
import threading

log = logging.getLogger("control")

ACTIONS = ("next", "prev", "restart")


class TaskInterrupted(Exception):
    """任务被控制动作打断（控制流异常，非错误）。action: pause/next/prev/restart"""

    def __init__(self, action):
        super().__init__(action)
        self.action = action


_lock = threading.Lock()
_pending = None                      # None / "next" / "prev" / "restart"


def _request(action):
    global _pending
    with _lock:
        _pending = action
    log.warning("收到控制动作: %s", action)


def request_skip():
    _request("next")


def request_prev():
    _request("prev")


def request_restart():
    _request("restart")


def peek_action():
    """当前待执行的动作（不清除）。"""
    with _lock:
        return _pending


def consume_action():
    """取出并清除待执行动作。"""
    global _pending
    with _lock:
        a, _pending = _pending, None
    return a


def clear():
    """队列开始时清空遗留动作。"""
    global _pending
    with _lock:
        _pending = None

_paused = False
def request_pause_toggle():
    """切换手动暂停状态；暂停时同时设置 pending 让调度器快速响应。"""
    global _paused, _pending
    with _lock:
        _paused = not _paused
    log.warning("手动暂停: %s", "开启" if _paused else "关闭")

def is_paused():
    with _lock:
        return _paused
