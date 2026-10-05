# -*- coding: utf-8 -*-
"""
任务原语。所有任务从 config.yaml 构造，run() 返回 True/False 表示成功/失败，
队列执行器只负责顺序调用，不关心细节。

判定模型：
- retry_group：跟踪 watch 匹配到的全部进程，任意一个退出即视为本次尝试成功；
  attempt_timeout 内没有任何退出 → 清理 → 重试。
- monitor（AUTO-MAS 场景）：不启动进程，只监控。游戏类、脚本类进程的退出次数
  分别累加（被 AUTO-MAS 重启一次就累加一次），任一类超过阈值 → 判定失败；
  总时长超过 timeout → 判定失败；进程全部消失且稳定 → 判定成功。
  失败后进入强杀模式：外部会自动重试把进程重新拉起，所以每轮巡检发现活着
  就再杀一次，直到进程持续消失 stable_dead 秒（外部重试耗尽）。
"""
from copy import deepcopy
import logging
import os
import re
import threading
import time

from . import activity, adb, control, proc, resolution

log = logging.getLogger("tasks")

TASK_TYPES = {}

class LaunchPreparationFailed(RuntimeError):
    def __init__(self, status):
        super().__init__(status)
        self.status = status

def _pids_batch(multi_matchers):
    """接收多个 matchers 列表，返回对应的 PID 集合列表（一次遍历）"""
    return proc.find_processes_multi(multi_matchers)

def _register(cls):
    TASK_TYPES[cls.TYPE] = cls
    return cls


def build_task(cfg, defaults):
    t = cfg.get("type")
    if t not in TASK_TYPES:
        raise ValueError(f"未知任务类型: {t!r}（任务 {cfg.get('name')!r}）")
    return TASK_TYPES[t](cfg, defaults)


def _pids(matchers):
    """当前匹配到的进程 PID 集合。"""
    return {p.pid for p in proc.find_processes(matchers)}


class BaseTask:
    TYPE = None
    # 用户活动时是否响应"暂停"的默认值（可用 pause_kill 覆盖）。
    # start 类任务（retry_group / launch_wait）默认 True；monitor 也默认 True——
    # 暂停时由调度器按主控关闭清单清理（AUTO-MAS 及其全部下属进程）并
    # 回到主控 launch 任务重新运行。热键控制动作（跳过/上一个/从头）不受此开关限制。
    PAUSE_KILL_DEFAULT = False

    def __init__(self, cfg, defaults):
        self._cfg = deepcopy(cfg)
        self._defaults = deepcopy(defaults)
        self.deferred = []
        self.name = cfg.get("name", "未命名任务")
        self.enabled = cfg.get("enabled", True)
        self.execution_mode = cfg.get("execution_mode", defaults.get("execution_mode", "foreground"))
        if self.execution_mode not in ("foreground", "background"):
            raise ValueError(f"[{self.name}] execution_mode 只能是 foreground / background")
        self.foreground = self.execution_mode == "foreground"
        self.adb = dict(defaults.get("adb") or {})
        self.adb.update(cfg.get("adb") or {})
        adb.validate(self.adb)
        if self.adb.get("enabled") and self.TYPE not in ("retry_group", "monitor", "launch_wait"):
            raise ValueError(f"[{self.name}] 此任务类型不支持 ADB 退出清理")
        self.poll = cfg.get("poll_interval", defaults.get("poll_interval", 5))
        self.stable_dead = cfg.get("stable_dead", defaults.get("stable_dead", 20))
        # 并行组名：连续的、parallel 相同的任务会被同时启动，全部完成后队列才继续。
        # 不配（None）则保持顺序执行。
        self.parallel = cfg.get("parallel")
        # 运行结果状态（供队列结束后的通知报告分类）：
        # success / timeout / not_started / threshold / error_limit / kill_giveup / fail
        self._status = None
        self.failure_callback = None
        self._failure_reported = False
        # 检测到用户真实键鼠输入时，是否打断本任务（由调度器清理并暂停）
        self.pause_kill = cfg.get("pause_kill", self.PAUSE_KILL_DEFAULT)
        self._stop_event = threading.Event()   # 协同停止标志
        self.adb_session = None
        self._adb_client = None
        self._adb_candidate = None
        self._adb_since = None
        self._adb_package = None
        self._adb_disabled = False

    def run(self):
        raise NotImplementedError

    @property
    def status(self):
        return self._status

    @status.setter
    def status(self, value):
        self._status = value
        if value is None:
            self._failure_reported = False
        elif value not in ("success", "interrupted", "skipped", "deferred") and self.TYPE != "sliding_window":
            self.report_failure(value)

    def take_deferred(self):
        pending, self.deferred = self.deferred, []
        return pending

    def report_failure(self, status="fail"):
        """一次任务执行只即时报告一次，先于耗时的失败清理。"""
        if self._failure_reported or not self.failure_callback:
            return
        self._failure_reported = True
        try:
            self.failure_callback(self.name, status)
        except Exception:
            log.exception("[%s] 失败即时通知异常，不影响任务清理与后续调度", self.name)

    def _check_interrupt(self):
        """
        在轮询循环中调用：有待执行的控制动作（热键）或用户活动需要暂停时，
        抛出 control.TaskInterrupted，由调度器统一清理现场并调整执行位置。
        """
        act = control.peek_action()
        if act:
            self._stop_event.set()      # 广播“有动作发生”
            raise control.TaskInterrupted(act)
        if (self._stop_event.is_set() or control.is_paused()
                or (self.foreground and self.pause_kill and activity.get().user_active())):
            self._stop_event.set()      # 广播“暂停”
            raise control.TaskInterrupted("pause")

    def _dead_stable(self, matchers):
        """目标进程当前是否已持续消失 stable_dead 秒。供轮询循环调用。"""
        return not proc.any_alive(matchers)
    def _interruptible_sleep(self, seconds):
        """分段等待，使手动控制在一秒内生效。"""
        deadline = time.monotonic() + max(0, seconds)
        while time.monotonic() < deadline:
            self._check_interrupt()
            self._stop_event.wait(min(1, max(0, deadline - time.monotonic())))

    def _start_launch_spec(self, spec):
        """Optionally wait for a prerequisite's successful exit before continuing."""
        child = proc.start_process(spec["exe"], spec.get("args"), spec.get("cwd"))
        if not spec.get("wait_for_exit"):
            return child
        deadline = time.monotonic() + spec["timeout"]
        try:
            while True:
                self._check_interrupt()
                code = child.poll()
                if code is not None:
                    if code != 0:
                        log.warning("[%s] 前置启动程序退出码 %s，停止本次启动", self.name, code)
                        raise LaunchPreparationFailed("not_started")
                    return child
                if time.monotonic() >= deadline:
                    log.warning("[%s] 前置启动程序超时，停止本次启动", self.name)
                    raise LaunchPreparationFailed("timeout")
                self._interruptible_sleep(min(1, max(0, deadline - time.monotonic())))
        finally:
            if child.poll() is None:
                try:
                    proc.kill_tree(proc.psutil.Process(child.pid))
                except proc.psutil.NoSuchProcess:
                    pass

    def _finish_adb(self):
        self._check_interrupt()
        if self.adb_session and self._adb_client and self._adb_package:
            self.adb_session.completed(self._adb_client, self._adb_package)
            log.info("[%s] 脚本已稳定退出，后台清理延后至下一模拟器任务启动稳定", self.name)
        self.status = "success"
        return True

    def _adb_tick(self, scripts_alive):
        if (not self.adb.get("enabled") or not self.adb_session or self._adb_disabled):
            return
        if self.parallel or getattr(self, "_adb_parallel", False):
            self._adb_disabled = True
            log.warning("[%s] 并行模拟器任务不执行自动后台清理，避免影响同设备的其他脚本", self.name)
            return
        if not scripts_alive:
            self._adb_candidate = self._adb_since = None
            return
        try:
            self._check_interrupt()
            if self._adb_client is None:
                cfg = dict(self.adb)
                if cfg.get("auto_detect"):
                    cfg["serial"] = ""
                self._adb_client = adb.AdbClient(cfg)
                self._adb_client.select_device()
                self._adb_protected = self._adb_client.protected_packages()
            package = self._adb_client.foreground()
            if (package in self._adb_protected or
                    (self.adb.get("packages") and package not in self.adb["packages"])):
                self._adb_candidate = self._adb_since = None
                return
            if package != self._adb_candidate:
                self._adb_candidate, self._adb_since = package, time.monotonic()
                return
            if time.monotonic() - self._adb_since < float(self.adb.get("startup_stable", 10)):
                return
            self._adb_package = package
            previous = self.adb_session.previous(self._adb_client.serial)
            # Script process appearing alone does not mean it has opened the next game.
            if previous and (package != previous[1] or package in self.adb.get("packages", [])):
                self.adb_session.clean(self._adb_client, package, self._check_interrupt)
        except control.TaskInterrupted:
            raise
        except Exception:
            self._adb_disabled = True
            log.warning("[%s] ADB 启动检查/后台清理失败，本次跳过清理，脚本继续执行", self.name, exc_info=True)

    def clear_stop_event(self):
        """每次开始运行前重置事件（由调度器调用）。"""
        self._stop_event.clear()
        self._adb_client = self._adb_candidate = self._adb_since = self._adb_package = None
        self._adb_disabled = False

    def get_sub_results(self):
        """返回子任务的详细结果字典（名称→(ok, status)）。默认空。"""
        return {}

@_register
class LaunchWaitTask(BaseTask):
    """启动程序并等待其自然退出；timeout>0 时超时强杀。
    用户活动时：先等离开再启动；运行中被真实输入打断则强杀、暂停，
    用户离开后重新启动。"""
    TYPE = "launch_wait"
    PAUSE_KILL_DEFAULT = True

    def __init__(self, cfg, defaults):
        super().__init__(cfg, defaults)
        self.exe = cfg["exe"]
        self.args = cfg.get("args") or []
        self.cwd = cfg.get("cwd")          # python 脚本一般需要起始路径
        self.timeout = cfg.get("timeout") or 0   # 0 = 不限时
        self.kill = cfg.get("kill") or []
        self.check_exit_code = cfg.get("check_exit_code", False)
        self.delay_after = cfg.get("delay_after", 0)

    def _kill_mine(self):
        exe_name = self.exe.split("\\")[-1].split("/")[-1]
        proc.kill_matched(self.kill or [{"names": [exe_name]}])

    def run(self):
        mon = activity.get()
        deadline = (time.time() + self.timeout) if self.timeout else None
        if self.pause_kill:
            mon.wait_until_idle(self.name, self.poll, 60, foreground=self.foreground)
        self._check_interrupt()
        p = proc.start_process(self.exe, self.args, self.cwd)
        dead_since = None
        while True:
            self._check_interrupt()
            rc = p.poll()
            self._adb_tick(rc is None)
            if rc is not None:
                log.info("[%s] 程序自然退出，返回码 %s", self.name, rc)
                if self.check_exit_code and rc != 0:
                    self.status = "not_started"
                    return False
                if self.adb.get("enabled"):
                    matchers = self.kill or [{"names": [os.path.basename(self.exe)],
                                               "path_contains": os.path.dirname(self.exe)}]
                    if proc.any_alive(matchers):
                        dead_since = None
                    elif dead_since is None:
                        dead_since = time.monotonic()
                    if dead_since is not None and time.monotonic() - dead_since >= self.stable_dead:
                        return self._finish_adb()
                else:
                    if self.delay_after:
                        self._interruptible_sleep(self.delay_after)
                    self.status = "success"
                    return True
            if deadline and time.time() >= deadline:
                log.warning("[%s] 超过 %s 秒未退出，强杀", self.name, self.timeout)
                self.status = "timeout"
                self._kill_mine()
                return False
            # 用户活动/热键控制 → 抛出中断，调度器清理后按需重跑本任务
            self._check_interrupt()
            self._interruptible_sleep(self.poll)


@_register
class LaunchTask(BaseTask):
    """只启动，不等待；可选 delay_after 等待若干秒（如等模拟器开机）。"""
    TYPE = "launch"

    def __init__(self, cfg, defaults):
        super().__init__(cfg, defaults)
        self.exe = cfg["exe"]
        self.args = cfg.get("args") or []
        self.cwd = cfg.get("cwd")
        self.delay_after = cfg.get("delay_after", 0)

    def run(self):
        proc.start_process(self.exe, self.args, self.cwd)
        if self.delay_after:
            log.info("[%s] 等待 %s 秒……", self.name, self.delay_after)
            deadline = time.time() + self.delay_after
            while time.time() < deadline:
                self._check_interrupt()          # 响应热键/用户活动
                time.sleep(min(1, deadline - time.time()))
        self.status = "success"
        return True


@_register
class MonitorTask(BaseTask):
    """
    只监控、不启动（程序由 AUTO-MAS 等外部拉起的场景）。
    组间关系：队列顺序执行 → AUTO-MAS 的各组依次监控；
    组内关系：游戏 / 脚本两类进程在同一巡检循环中并行统计退出次数。

    类别配置（两种写法，二选一）：

    1) 简单两分（兼容旧配置）：
        game:   {max_exits: 5, matchers: [...]}
        script: {max_exits: 5, matchers: [...]}

    2) categories 任意多分（python 类脚本建议用）——每个进程单独计数、单独阈值，
       避免某个进程（如被脚本内部反复重启的 python）连累同组其他进程被误判：
        categories:
          游戏本体: {role: game,   max_exits: 3, matchers: [...]}
          脚本主体: {role: script, max_exits: 3, matchers: [...]}
          Python:  {role: script, max_exits: 5, matchers: [...]}
       role 仅用于 success_when=script_dead 的判定（script 类全部消失即成功）。

    判定：
      - 任一类退出次数 >= 该类 max_exits → 失败：
          linger > 0 时，立即唤醒队列里的下一个监控任务，本任务转入后台
          继续清场 linger 秒（外部重试会把进程拉起，后台反复杀），保证进程完全关闭；
          linger = 0 时，原地反复强杀直到死透再返回。
      - 总时长 > timeout                  → 失败：原地反复强杀直到死透再返回。
      - 全部进程消失并稳定（或 script_dead 下脚本类消失并稳定）→ 成功（返回 True）

    用户活动/热键打断：抛出 TaskInterrupted，由调度器按主控关闭清单清理
    （AUTO-MAS 及其全部下属进程），恢复后从主控 launch 任务重新运行，
    本组及后续组的超时与重试计数自然重置（任务对象重新执行）。
    """
    TYPE = "monitor"
    PAUSE_KILL_DEFAULT = True

    def __init__(self, cfg, defaults):
        super().__init__(cfg, defaults)
        self._phase = "idle"  # idle / waiting / monitoring / finished
        self.categories = {}  # {类别名: {"matchers":..., "max_exits":..., "role":...}}
        if cfg.get("categories"):
            for name, spec in cfg["categories"].items():
                self.categories[name] = {
                    "matchers": spec["matchers"],
                    "max_exits": spec.get("max_exits", 999999),
                    "role": spec.get("role", "script"),
                }
        else:
            for cat in ("game", "script"):
                spec = cfg.get(cat)
                if spec:
                    self.categories[cat] = {
                        "matchers": spec["matchers"],
                        "max_exits": spec.get("max_exits", 999999),
                        "role": cat,
                    }
        if not self.categories:
            raise ValueError(f"[{self.name}] monitor 任务至少需要 game/script 或 categories")
        self.all_matchers = [m for c in self.categories.values() for m in c["matchers"]]
        self.kill = cfg.get("kill") or self.all_matchers
        self.timeout = cfg.get("timeout", 3600)
        self.appear_timeout = cfg.get("appear_timeout", 1800)
        self.linger = cfg.get("linger", 0)
        self.kill_timeout = cfg.get("kill_timeout",        # 强杀最长持续秒数
                                    defaults.get("kill_timeout", 600))
        # 成功判定方式：
        #   all_dead（默认）= 游戏和脚本进程全部消失并稳定 → 成功
        #   script_dead     = 只看脚本类进程，脚本消失并稳定 → 成功
        #     （用于游戏侧进程可能合法驻留的场景，如明日方舟的模拟器在
        #       MAA 跑完后仍不退出，等它消失会把监控卡到超时）
        self.success_when = cfg.get("success_when", "all_dead")
        if self.success_when not in ("all_dead", "script_dead"):
            raise ValueError(f"[{self.name}] success_when 只能是 all_dead / script_dead")
        if (self.success_when == "script_dead"
                and not any(c["role"] == "script" for c in self.categories.values())):
            log.warning("[%s] success_when=script_dead 但没有 role=script 的类别，回退为 all_dead",
                        self.name)
            self.success_when = "all_dead"
        self.heartbeat = cfg.get("heartbeat", defaults.get("heartbeat", 60))
        self.resolution_check = cfg.get("resolution_check")
        resolution.validate(self.resolution_check)
        if self.adb.get("enabled"):
            if not any(c["role"] == "script" for c in self.categories.values()):
                raise ValueError(f"[{self.name}] ADB monitor 必须配置 script 类别")
            self.success_when = "script_dead"

    def _force_kill_until_dead(self, reason):
        """
        失败收尾（原地阻塞式）：反复强杀直到进程死透（外部重试会重新拉起，所以要杀多轮）。
        但有 kill_timeout 逃生出口：杀不动的系统服务进程会无限重启，
        绝不能因此卡死队列——超时后报错并放行。
        """
        log.warning("[%s] %s，进入强杀模式", self.name, reason)
        deadline = time.time() + self.kill_timeout
        dead_since = None
        while True:
            if time.time() > deadline:
                left = []
                for p in proc.find_processes(self.kill):
                    try:
                        left.append(p.name())
                    except (proc.psutil.NoSuchProcess, proc.psutil.AccessDenied):
                        pass
                log.error("[%s] 强杀持续 %s 秒仍未死透（残留: %s），放弃并放行队列。"
                          "若是杀不死的系统服务进程，请把它加入 kill 名单的 exclude_names",
                          self.name, self.kill_timeout, left)
                self.status = "kill_giveup"
                return False
            if proc.any_alive(self.kill):
                dead_since = None
                proc.kill_matched(self.kill)
            else:
                if dead_since is None:
                    dead_since = time.time()
                elif time.time() - dead_since >= self.stable_dead:
                    log.info("[%s] 进程已死透，失败收尾完成", self.name)
                    return False
            self._interruptible_sleep(self.poll)

    def _linger_kill_background(self, reason):
        """
        失败收尾（后台延时清场）：立即返回 False 让队列唤醒下一个监控任务，
        同时起后台线程在 linger 秒内持续强杀本组进程，确保外部重试拉起的
        进程也被彻底关闭。
        """
        log.warning("[%s] %s，唤醒下一个监控任务；本任务后台继续清场 %s 秒",
                    self.name, reason, self.linger)

        def worker():
            end = time.time() + self.linger
            while time.time() < end:
                if proc.any_alive(self.kill):
                    proc.kill_matched(self.kill)
                self._interruptible_sleep(self.poll)
            proc.kill_matched(self.kill)      # 收尾再补一轮
            log.info("[%s] 后台清场 %s 秒结束，进程已完全关闭", self.name, self.linger)

        threading.Thread(target=worker, daemon=True,
                         name=f"linger-{self.name}").start()
        return False

    def _quiet(self, prev):
        """当前是否达到"成功静默"状态。"""
        if self.success_when == "script_dead":
            # 所有 role=script 的类别（脚本主体、Python 等）都消失才算静默
            return not any(pids for cat, pids in prev.items()
                           if self.categories[cat]["role"] == "script")
        return not any(prev.values())                # 默认：全部消失

    def run(self):
        self.deferred.clear()
        self._phase = "waiting"
        # 1. 等本组进程出现（AUTO-MAS 顺序执行，上一组结束才会拉起本组）
        log.info("[%s] 等待目标进程出现（最长 %s 秒）……", self.name, self.appear_timeout)
        deadline = time.time() + self.appear_timeout
        last_beat = time.time()
        while time.time() < deadline:
            self._check_interrupt()          # 用户活动/热键 → 中断交给调度器
            if proc.any_alive(self.all_matchers):
                self._phase = "monitoring"   # 已检测到进程
                break
            if time.time() - last_beat >= self.heartbeat:
                log.info("[%s] [心跳] 仍在等待目标进程出现，已等待 %d 秒",
                         self.name, int(time.time() - (deadline - self.appear_timeout)))
                last_beat = time.time()
            self._interruptible_sleep(self.poll)
        else:
            self._phase = "finished"
            log.info("[%s] 目标进程未出现，视为该组被跳过", self.name)
            self.status = "not_started"
            return True

        # 2. 并行监控游戏/脚本两类进程，分别累加退出次数
        log.info("[%s] 检测到进程，开始监控（超时 %s 秒，成功判定：%s）",
                 self.name, self.timeout, self.success_when)
        start = time.time()
        last_beat = start
        # 收集所有类别的 matchers，保持顺序
        cats = list(self.categories.keys())
        matchers_list = [self.categories[cat]["matchers"] for cat in cats]

        # 第一次批量查询
        pids_list = _pids_batch(matchers_list)
        prev = {cat: pids_list[i] for i, cat in enumerate(cats)}
        script_seen = {cat for cat in cats if prev[cat] and self.categories[cat]["role"] == "script"}

        exits = {cat: 0 for cat in self.categories}
        resolution_watchers = {
            cat: resolution.Watcher(self.resolution_check)
            for cat in cats if self.categories[cat]["role"] == "game"
        }
        dead_since = None
        while True:
            # 用户活动/热键控制 → 中断：调度器负责清理（AUTO-MAS 系走主控关闭
            # 清单），恢复后本任务重新执行，计数与超时时钟自然清零
            self._check_interrupt()
            curr_list = _pids_batch(matchers_list)
            self._adb_tick(all(curr_list))
            for i, cat in enumerate(cats):
                curr = curr_list[i]
                if curr and self.categories[cat]["role"] == "script":
                    script_seen.add(cat)
                c = self.categories[cat]
                exited = prev[cat] - curr
                watcher = resolution_watchers.get(cat)
                counted = watcher.consume_counted_exits(exited) if watcher else set()
                n_exit = len(exited - counted)     # 为调整窗口主动重启游戏不计退出失败
                if n_exit:
                    exits[cat] += n_exit
                    log.info("[%s] %s 类进程退出 +%d（累计 %d，阈值 %d）",
                             self.name, cat, n_exit, exits[cat], c["max_exits"])
                    if exits[cat] >= c["max_exits"]:
                        reason = (f"{cat} 类进程退出次数达到阈值 "
                                  f"{c['max_exits']}（累计 {exits[cat]}）")
                        self.status = "threshold"
                        self._phase = "finished"
                        if self.linger > 0:
                            return self._linger_kill_background(reason)
                        return self._force_kill_until_dead(reason)
                if watcher:
                    try:
                        watcher.poll(curr)
                    except resolution.RestartRequested as request:
                        recovery = RecoveryTask.from_monitor(self, request.launch)
                        if not recovery.quiesce():
                            self.status = "kill_giveup"
                            self._phase = "finished"
                            return False
                        self.deferred = [recovery]
                        self.status = "deferred"
                        self._phase = "finished"
                        log.info("[%s] 整组已稳定退出，登记监控组结束后的补跑", self.name)
                        return True
                prev[cat] = curr

            if time.time() - start > self.timeout:
                self.status = "timeout"
                self._phase = "finished"
                return self._force_kill_until_dead(f"超过总超时 {self.timeout} 秒")

            scripts_ready = (not self.adb.get("enabled") or all(
                cat in script_seen for cat in cats if self.categories[cat]["role"] == "script"))
            if scripts_ready and self._quiet(prev):
                if dead_since is None:
                    dead_since = time.time()
                elif time.time() - dead_since >= self.stable_dead:
                    log.info("[%s] %s，本组自然结束", self.name,
                             "脚本进程消失且稳定" if self.success_when == "script_dead"
                             else "进程全部消失且稳定")
                    self.status = "success"
                    self._phase = "finished"
                    return self._finish_adb()
            else:
                dead_since = None

            if time.time() - last_beat >= self.heartbeat:
                alive = {cat: len(p) for cat, p in prev.items()}
                log.info("[%s] [心跳] 已监控 %d 秒，存活进程数 %s，退出累计 %s",
                         self.name, int(time.time() - start), alive, exits)
                last_beat = time.time()
            self._interruptible_sleep(self.poll)


class ErrorLogWatcher:
    """
    跟踪日志文件的增量内容，命中正则即视为脚本出错。
    每次尝试开始时从文件末尾起跟踪（历史 ERROR 不触发）；
    日志被截断/轮转时自动从头重读。
    """

    def __init__(self, path, pattern):
        self.path = path
        self.regex = re.compile(pattern)
        self.offset = self._size()

    def _size(self):
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def poll(self):
        """有新行命中 pattern 返回 True（并记录命中行）。"""
        size = self._size()
        if size < self.offset:
            self.offset = 0
        if size == self.offset:
            return False
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self.offset)
                chunk = f.read(size - self.offset)
        except OSError:
            return False
        self.offset = size
        for line in chunk.splitlines():
            if self.regex.search(line):
                log.warning("错误日志命中 %s: %s", self.path, line.strip()[:200])
                return True
        return False


@_register
class RetryGroupTask(BaseTask):
    """
    启动一组程序，跟踪 watch 匹配到的全部进程。
    watch 是 matcher 列表，每个元素视为一个"进程组"。分两阶段：
      1) 就绪阶段：等【所有组】都有进程出现（如游戏本体和 python 脚本都已启动），
         才开始退出检测——避免启动顺序导致误判；appear_timeout 内未全部就绪，
         本次尝试失败，清理后重试；
      2) 退出检测：任意一个被监控进程退出 → 本次尝试成功 → 清理残留 → 返回 True；
         attempt_timeout 内没有任何退出 → 清理全部 → 重新启动，最多 attempts 次。
    等待期间每 heartbeat 秒输出一次心跳日志，长时间运行可确认任务活着。
    用户活动检测开启时：尝试前先等用户离开；运行中被真实输入打断则
    清理本组、暂停等待，离开后重新开始本次尝试（被打断不计入 attempts）。
    """
    TYPE = "retry_group"
    PAUSE_KILL_DEFAULT = True

    def __init__(self, cfg, defaults):
        super().__init__(cfg, defaults)
        self.launch = cfg["launch"]                  # [{exe,args,cwd,delay_after}]
        self.watch = cfg["watch"]                    # 成功判定的进程 matcher 列表
        self.resolution_check = cfg.get("resolution_check")
        resolution.validate(self.resolution_check)
        self.resolution_matchers = (self.resolution_check or {}).get("matchers", [])
        if self.resolution_check and not self.resolution_matchers:
            raise ValueError("retry_group 的 resolution_check 必须指定游戏 matchers")
        self.success_when = cfg.get("success_when", "all_dead" if self.adb.get("enabled") else "any_exit")
        if self.success_when not in ("any_exit", "all_dead"):
            raise ValueError(f"[{self.name}] retry_group.success_when 只能是 any_exit / all_dead")
        if self.adb.get("enabled") and self.success_when != "all_dead":
            raise ValueError(f"[{self.name}] 启用 ADB 时必须使用 all_dead 稳定退出判定")
        self.attempts = cfg.get("attempts", 1)
        self.attempt_timeout = cfg.get("attempt_timeout", 1800)
        self.appear_timeout = cfg.get("appear_timeout", 120)
        self.cleanup = cfg.get("cleanup") or []      # 按顺序执行的清理 matcher
        self.launch_interval = cfg.get("launch_interval", 5)
        self.heartbeat = cfg.get("heartbeat", defaults.get("heartbeat", 60))
        # 可选：日志错误检测。命中 pattern → 清理并重启脚本，本次不计入 attempts 上限；
        # max_restarts 为安全上限，防止日志持续刷错导致无限重启。
        el = cfg.get("error_log")
        self.error_log = None
        if el:
            self.error_log = {
                "path": el["path"],
                "pattern": el.get("pattern", "ERROR"),
                "max_restarts": el.get("max_restarts", 10),
            }

    def _cleanup(self):
        if getattr(self, "_active_recovery", None):
            self._active_recovery._cleanup()
        for m in self.cleanup:
            proc.kill_matched([m])
        time.sleep(2)

    def run(self):
        try:
            return self._run_attempts()
        except resolution.RestartRequested as request:
            recovery = RecoveryTask.from_retry(self, request.launch)
            recovery._stop_event = self._stop_event
            recovery.failure_callback = None  # The owning retry task reports the final result.
            self._active_recovery = recovery
            try:
                ok = recovery.run()
            except control.TaskInterrupted:
                raise  # Keep the expanded cleanup plan for scheduler cleanup.
            except Exception:
                recovery._cleanup()
                raise
            self.status = recovery.status
            self._active_recovery = None
            return ok

    def _run_attempts(self):
        resolution_watcher = resolution.Watcher(self.resolution_check)
        attempt = 0
        err_restarts = 0
        last_fail = "timeout"                        # 记录最后一次失败原因（通知用）
        while attempt < self.attempts:
            # 用户活动时不启动新尝试，等离开再说（启动自动化脚本会抢键鼠）
            if self.pause_kill:
                activity.get().wait_until_idle(self.name, self.poll, self.heartbeat, foreground=self.foreground)
            self._check_interrupt()
            attempt += 1
            log.info("[%s] 第 %d/%d 次尝试", self.name, attempt, self.attempts)
            watcher = (ErrorLogWatcher(self.error_log["path"], self.error_log["pattern"])
                       if self.error_log else None)
            try:
                for spec in self.launch:
                    self._check_interrupt()
                    self._start_launch_spec(spec)
                    if spec.get("delay_after"):
                        self._interruptible_sleep(spec["delay_after"])
                    self._interruptible_sleep(self.launch_interval)
            except LaunchPreparationFailed as exc:
                last_fail = exc.status
                self._cleanup()
                continue

            # 就绪阶段：等 watch 里的每一组进程都出现（游戏本体、python 脚本等
            # 全部启动完毕），再开始退出检测，避免启动顺序造成误判
            log.info("[%s] 等待全部被监控进程组启动（最长 %s 秒）……",
                     self.name, self.appear_timeout)
            err_hit = False
            appear_deadline = time.time() + self.appear_timeout
            seen = set()
            last_beat = time.time()
            while time.time() < appear_deadline:
                self._check_interrupt()      # 用户活动/热键 → 中断交给调度器
                if watcher and watcher.poll():
                    err_hit = True
                    break
                watch_matchers_list = [[m] for m in self.watch]
                pids_per_group = _pids_batch(watch_matchers_list)
                if resolution_watcher.enabled:
                    resolution_watcher.poll(_pids(self.resolution_matchers))
                if all(pids_per_group):
                    seen = set().union(*pids_per_group)
                    break
                if time.time() - last_beat >= self.heartbeat:
                    missing = [m for m, p in zip(self.watch, pids_per_group) if not p]
                    log.info("[%s] [心跳] 等待进程组启动，未就绪: %s", self.name, missing)
                    last_beat = time.time()
                self._interruptible_sleep(self.poll)
            if not err_hit and not seen:
                log.warning("[%s] %s 秒内被监控进程未全部启动，本次尝试失败",
                            self.name, self.appear_timeout)
                last_fail = "not_started"

            # 退出检测阶段
            if not err_hit and seen:
                log.info("[%s] 全部被监控进程已启动（%d 个），开始退出检测",
                         self.name, len(seen))
                start = time.time()
                last_beat = start
                dead_since = None
                while True:
                    self._check_interrupt()  # 用户活动/热键 → 中断交给调度器
                    if watcher and watcher.poll():
                        err_hit = True
                        break
                    curr = _pids(self.watch)
                    if self.adb.get("enabled") and self.adb_session:
                        self._adb_tick(all(_pids_batch([[m] for m in self.watch])))
                    # 主动调整窗口的重启不能被误判为任务成功；从历史 seen 永久移除。
                    ignored = resolution_watcher.consume_counted_exits(seen - curr)
                    seen.difference_update(ignored)
                    exited = seen - curr             # 自然退出仍沿用原成功/重试机制
                    if resolution_watcher.enabled:
                        resolution_watcher.poll(_pids(self.resolution_matchers))
                    if curr:
                        dead_since = None
                    elif dead_since is None:
                        dead_since = time.monotonic()
                    stable = dead_since is not None and time.monotonic() - dead_since >= self.stable_dead
                    if (self.success_when == "any_exit" and exited) or (self.success_when == "all_dead" and stable):
                        log.info("[%s] 检测到进程退出（%d 个），任务成功",
                                 self.name, len(exited))
                        self._cleanup()
                        return self._finish_adb()
                    if time.time() - start > self.attempt_timeout:
                        log.warning("[%s] 第 %d 次超过 %s 秒无进程退出，清理后重试",
                                    self.name, attempt, self.attempt_timeout)
                        last_fail = "timeout"
                        break
                    seen |= curr                     # 后出现的进程也纳入跟踪
                    if time.time() - last_beat >= self.heartbeat:
                        log.info("[%s] [心跳] 已运行 %d 秒，跟踪进程数 %d",
                                 self.name, int(time.time() - start), len(seen))
                        last_beat = time.time()
                    self._interruptible_sleep(self.poll)

            if err_hit:
                # 日志报错：脚本卡死，清理后重启，本次不计入 attempts 上限
                err_restarts += 1
                if err_restarts > self.error_log["max_restarts"]:
                    log.error("[%s] 错误重启达到安全上限 %d 次，任务失败",
                              self.name, self.error_log["max_restarts"])
                    self.status = "error_limit"
                    self._cleanup()
                    return False
                attempt -= 1
                log.warning("[%s] 检测到脚本错误日志，清理并重启脚本"
                            "（错误重启第 %d 次，不占用尝试次数）",
                            self.name, err_restarts)
            if not err_hit and attempt >= self.attempts:
                self.status = last_fail
            self._cleanup()

        log.error("[%s] 达到最大重试次数 %d，任务失败，继续队列后续任务",
                  self.name, self.attempts)
        self.status = last_fail
        return False


class RecoveryTask(BaseTask):
    """Bounded group restart; monitor recoveries try scripts before game + scripts."""
    TYPE = "retry_group"
    PAUSE_KILL_DEFAULT = True

    def __init__(self, cfg, defaults, categories, scripts, game, cleanup, script_first):
        super().__init__(cfg, defaults)
        self.parallel = None
        self.categories = deepcopy(categories)
        self.scripts = deepcopy(scripts)
        self.game = deepcopy(game)
        self.cleanup = deepcopy(cleanup)
        self.script_first = script_first
        options = cfg.get("recovery") or {}
        self.attempts = 2  # Total budget: one script-only, one game-then-script.
        self._next_attempt = 0
        self._last_fail = "timeout"
        self.timeout = min(1200, cfg.get("attempt_timeout", cfg.get("timeout", 1200)))
        self._recovery_deadline = None
        self.appear_timeout = cfg.get("appear_timeout", 120)
        self.kill_timeout = max(180, cfg.get("kill_timeout", defaults.get("kill_timeout", 600)))
        self.heartbeat = cfg.get("heartbeat", defaults.get("heartbeat", 60))
        self.launch_interval = cfg.get("launch_interval", 5)
        if not scripts:
            raise ValueError(f"[{self.name}] 整组重启需要配置 recovery.script 的 exe/cwd/args")
        roots = list(options.get("python_paths", []))
        roots.extend(spec.get("cwd") or os.path.dirname(spec["exe"]) for spec in scripts)
        python_matchers = [{"names": ["python.exe", "pythonw.exe"], "python_root": root}
                           for root in roots if root]
        self.categories["恢复关联Python"] = {"role": "script", "max_exits": self.attempts,
                                             "matchers": python_matchers}
        for c in self.categories.values():
            for matcher in c["matchers"]:
                if matcher not in self.cleanup:
                    self.cleanup.append(matcher)

    @classmethod
    def from_monitor(cls, task, game):
        cfg = deepcopy(task._cfg)
        cfg["name"] = task.name + "（参数补跑）"
        script = (cfg.get("recovery") or {}).get("script")
        recovery = cls(cfg, task._defaults, task.categories, [script] if script else [],
                       game, task.kill, True)
        recovery._stop_event = task._stop_event
        return recovery

    @classmethod
    def from_retry(cls, task, game):
        cfg = deepcopy(task._cfg)
        scripts = [s for s in task.launch if s.get("role") == "script"]
        if not scripts:
            script = (cfg.get("recovery") or {}).get("script")
            scripts = [script] if script else []
        games = task.resolution_matchers
        categories = {"游戏": {"role": "game", "matchers": games, "max_exits": task.attempts},
                      "脚本": {"role": "script", "matchers": [m for m in task.watch if m not in games],
                               "max_exits": task.attempts}}
        return cls(cfg, task._defaults, categories, scripts, game, task.cleanup, False)

    def _cleanup(self):
        # Script roots are included even if a user's ordinary cleanup omits them.
        scripts = [m for c in self.categories.values() if c["role"] == "script" for m in c["matchers"]]
        for matcher in scripts + self.cleanup:
            proc.kill_matched([matcher])

    def quiesce(self):
        log.info("[%s] 关闭脚本、游戏及关联 Python，等待连续 180 秒无残留", self.name)
        deadline = time.monotonic() + self.kill_timeout
        if self._recovery_deadline is not None:
            deadline = min(deadline, self._recovery_deadline)
        quiet_since = None
        last_beat = time.monotonic()
        while True:
            self._check_interrupt()
            now = time.monotonic()
            try:
                alive = proc.any_alive(self.cleanup)
            except proc.psutil.AccessDenied:
                alive = None
                quiet_since = None
                log.warning("[%s] 关联 Python 信息不可读，不能确认稳定退出", self.name)
            if alive is None:
                pass
            elif alive:
                quiet_since = None
                self._cleanup()
            else:
                if quiet_since is None:
                    quiet_since = now
                if now - quiet_since >= 180:
                    return True
            if now >= deadline:
                log.error("[%s] 整组退出未稳定三分钟，禁止启动", self.name)
                return False
            if now - last_beat >= self.heartbeat:
                log.info("[%s] 等待整组稳定退出，当前静默 %d 秒", self.name,
                         0 if quiet_since is None else now - quiet_since)
                last_beat = now
            self._interruptible_sleep(min(self.poll, 5, max(0, deadline - time.monotonic())))

    def _attempt(self, game_first):
        specs = ([self.game] if game_first else []) + self.scripts
        error = self._cfg.get("error_log")
        log_watcher = ErrorLogWatcher(error["path"], error.get("pattern", "ERROR")) if error else None
        # Recovery windows may be adjusted, but must never request another recovery.
        window_cfg = deepcopy(self._cfg.get("resolution_check") or {})
        window_cfg["restart_with_args"] = False
        window_watcher = resolution.Watcher(window_cfg)
        start = time.monotonic()
        deadline = min(start + self.timeout, self._recovery_deadline or float("inf"))
        handles = []
        for spec in specs:
            self._check_interrupt()
            if time.monotonic() >= deadline:
                return "timeout"
            child = proc.start_process(spec["exe"], spec.get("args"), spec.get("cwd"), new_console=False)
            if spec is not self.game:
                handles.append(child)
            self._interruptible_sleep(min(spec.get("delay_after", self.launch_interval),
                                          max(0, deadline - time.monotonic())))
        previous = {name: set() for name in self.categories}
        exits = dict.fromkeys(self.categories, 0)
        script_seen = bool(handles)
        quiet_since = None
        last_beat = start
        while True:
            self._check_interrupt()
            now = time.monotonic()
            if now >= deadline:
                return "timeout"
            current = dict(zip(self.categories, _pids_batch([c["matchers"] for c in self.categories.values()])))
            scripts_alive = any(current[n] for n, c in self.categories.items() if c["role"] == "script")
            scripts_alive = scripts_alive or any(child.poll() is None for child in handles)
            script_seen |= scripts_alive
            if log_watcher and log_watcher.poll():
                return "error_limit"
            for name, c in self.categories.items():
                exits[name] += len(previous[name] - current[name])
                if exits[name] >= c.get("max_exits", self.attempts):
                    return "threshold"
                if c["role"] == "game":
                    window_watcher.poll(current[name])
            previous = current
            if script_seen and not scripts_alive:
                if quiet_since is None:
                    quiet_since = now
                if now - quiet_since >= self.stable_dead:
                    return "success"
            else:
                quiet_since = None
            if now - start >= self.timeout:
                return "timeout"
            if not script_seen and now - start >= self.appear_timeout:
                return "not_started"
            if now - last_beat >= self.heartbeat:
                log.info("[%s] 补跑已运行 %d 秒，脚本存活=%s", self.name, now-start, scripts_alive)
                last_beat = now
            self._interruptible_sleep(min(self.poll, max(0, deadline - time.monotonic())))

    def _out_of_time(self):
        return self._recovery_deadline is not None and time.monotonic() >= self._recovery_deadline

    def run(self):
        if self._recovery_deadline is None:
            self._recovery_deadline = time.monotonic() + 3000
        while self._next_attempt < self.attempts:
            if self._out_of_time() or not self.quiesce():
                if self._out_of_time():
                    self._cleanup()
                self.status = "timeout" if self._out_of_time() else "kill_giveup"
                return False
            game_first = self._next_attempt == 1
            # Count before launch so pause/resume cannot repeat an already started attempt.
            self._next_attempt += 1
            log.info("[%s] 补跑模式=%s，第 %d/2 次", self.name,
                     "游戏后脚本" if game_first else "仅脚本", self._next_attempt)
            try:
                self._last_fail = self._attempt(game_first)
            except OSError as exc:
                log.warning("[%s] 补跑启动失败：%s", self.name, type(exc).__name__)
                self._last_fail = "not_started"
            # Error-log restarts also consume this fixed budget; never add extra launches.
            if self._last_fail == "success":
                if not self.quiesce():
                    if self._out_of_time():
                        self._cleanup()
                    self.status = "timeout" if self._out_of_time() else "kill_giveup"
                    return False
                self.status = "success"
                return True
        if not self.quiesce():
            if self._out_of_time():
                self._cleanup()
            self._last_fail = "timeout" if self._out_of_time() else "kill_giveup"
        self.status = self._last_fail
        return False


class RecoveryBatch(BaseTask):
    TYPE = "retry_group"
    PAUSE_KILL_DEFAULT = True

    def __init__(self, owners, defaults):
        super().__init__({"name": "监控参数补跑组", "execution_mode": "background"}, defaults)
        self.owners = owners
        self.pending = []
        self.active = None
        self._sub_results = {}

    def collect(self):
        for owner in self.owners:
            self.pending.extend(owner.take_deferred())
        return bool(self.pending)

    def _cleanup(self):
        if self.active:
            self.active._cleanup()

    def run(self):
        while self.pending:
            task = self.active = self.pending[0]
            task._stop_event = self._stop_event
            task.failure_callback = self.failure_callback
            with activity.get().task_scope(task.foreground):
                activity.get().wait_until_idle(task.name, task.poll, task.heartbeat,
                                             foreground=task.foreground, stop_event=self._stop_event)
                try:
                    ok = task.run()
                except control.TaskInterrupted:
                    raise
                except Exception:
                    task._cleanup()
                    log.exception("[%s] 补跑异常", task.name)
                    task.status = "fail"
                    ok = False
            self._sub_results[task.name] = (ok, task.status)
            self.pending.pop(0)
        self.active = None
        return all(v[0] for v in self._sub_results.values())

    def get_sub_results(self):
        return self._sub_results


@_register
class KillTask(BaseTask):
    """纯清理：按顺序杀 targets 中的进程。"""
    TYPE = "kill"

    def __init__(self, cfg, defaults):
        super().__init__(cfg, defaults)
        self.targets = cfg.get("targets") or []

    def run(self):
        for m in self.targets:
            n = proc.kill_matched([m])
            log.info("[%s] 清理 %s → 杀掉 %d 个进程", self.name, m, n)
        self.status = "success"
        return True


@_register
class SlidingWindowTask(BaseTask):
    """
    滑动窗口监控组：对串行执行的多个程序，始终保持 window_size 个 monitor
    并行运行。当窗口最左侧的 monitor 完成后，窗口向右滑动一位。

    暂停（手动热键或用户活动）时立即设置共享停止事件，通知所有子 monitor
    立刻退出，不再等待它们完成。
    """
    TYPE = "sliding_window"
    PAUSE_KILL_DEFAULT = True

    def __init__(self, cfg, defaults):
        super().__init__(cfg, defaults)
        self.window_size = int(cfg.get("window_size", 2))
        if self.window_size < 1:
            raise ValueError("window_size 必须至少为 1")
        raw_monitors = cfg.get("monitors", [])
        if len(raw_monitors) < self.window_size:
            raise ValueError(
                f"[{self.name}] monitors 数量({len(raw_monitors)})不能小于"
                f"窗口大小({self.window_size})")
        self._monitor_cfgs = []
        for idx, mc in enumerate(raw_monitors):
            c = dict(mc)
            c["type"] = "monitor"
            c.setdefault("execution_mode", self.execution_mode)
            c.setdefault("name", f"{self.name}-子监控{idx+1}")
            self._monitor_cfgs.append(c)
        self._defaults = defaults

        # 汇总所有子 monitor 的 kill 目标，供中断清理使用
        self.all_kill_matchers = []
        for mc in self._monitor_cfgs:
            kill = mc.get("kill") or []
            if not kill:
                cat_matchers = []
                if mc.get("categories"):
                    for spec in mc["categories"].values():
                        cat_matchers.extend(spec["matchers"])
                else:
                    for key in ("game", "script"):
                        if mc.get(key):
                            cat_matchers.extend(mc[key].get("matchers", []))
                kill = cat_matchers
            for m in kill:
                if m not in self.all_kill_matchers:
                    self.all_kill_matchers.append(m)

        self._sub_results = {}

    def _run_child_monitor(self, idx, monitor, states, results, lock):
        m = monitor
        with lock:
            states[idx] = 1
        try:
            with activity.get().task_scope(m.foreground):
                ok = m.run()
            with lock:
                results[idx] = (ok, m.status or ("success" if ok else "fail"))
                states[idx] = 2
                self._sub_results[f"{self.name} → {m.name}"] = results[idx]
        except control.TaskInterrupted as e:
            with lock:
                results[idx] = (False, "interrupted")
                states[idx] = 2
                self._sub_results[f"{self.name} → {m.name}"] = results[idx]
        except Exception:
            log.exception("[%s] 子监控异常", m.name)
            m.report_failure("fail")
            with lock:
                results[idx] = (False, "fail")
                states[idx] = 2
                self._sub_results[f"{self.name} → {m.name}"] = results[idx]

    def run(self):
        self.deferred.clear()
        self._sub_results = {}
        n = len(self._monitor_cfgs)
        monitors = [MonitorTask(c, self._defaults) for c in self._monitor_cfgs]
        for mon in monitors:
            mon.adb_session = self.adb_session
            mon._adb_parallel = True
            if self.failure_callback:
                mon.failure_callback = lambda name, status: self.failure_callback(f"{self.name} → {name}", status)

        shared_stop = self._stop_event
        for mon in monitors:
            mon._stop_event = shared_stop
        self._stop_event = shared_stop          # ★ 让 _check_interrupt 能触发共享停止

        states = [0] * n
        results = [None] * n
        lock = threading.Lock()
        threads = []

        next_idx = 0
        for i in range(min(self.window_size, n)):
            states[i] = 1
            t = threading.Thread(
                target=self._run_child_monitor,
                args=(i, monitors[i], states, results, lock),
                name=f"sw-{self.name}-{i}",
            )
            t.start()
            threads.append(t)
            next_idx += 1

        log.info("[%s] 滑动窗口启动，窗口大小=%d，共 %d 个监控任务",
                 self.name, self.window_size, n)

        try:
            while True:
                self._check_interrupt()
                with lock:
                    left = 0
                    while left < n and states[left] == 2:
                        left += 1
                    if left >= n:
                        break
                    running_count = sum(
                        1 for i in range(left, n) if states[i] == 1
                    )
                    while next_idx < n and running_count < self.window_size:
                        t = threading.Thread(
                            target=self._run_child_monitor,
                            args=(next_idx, monitors[next_idx], states, results, lock),
                            name=f"sw-{self.name}-{next_idx}",
                        )
                        states[next_idx] = 1
                        t.start()
                        threads.append(t)
                        next_idx += 1
                        running_count += 1

                self._check_interrupt()
                self._interruptible_sleep(self.poll)

        except control.TaskInterrupted as e:
            log.warning("[%s] 收到中断动作 %s，通知所有子 monitor 立即退出",
                        self.name, e.action)
            shared_stop.set()
            raise
        finally:
            for thread in threads:
                thread.join()

        self.deferred = [task for mon in monitors for task in mon.take_deferred()]
        ok_all = all(v[0] for v in results if v is not None)
        for i in range(n):
            if results[i] and not results[i][0]:
                self.status = results[i][1]
                break
        else:
            self.status = "success"

        success_count = sum(1 for v in results if v and v[0])
        fail_count = sum(1 for v in results if v and not v[0])
        log.info("[%s] 滑动窗口完成：%d 成功 / %d 失败 / %d 总计",
                 self.name, success_count, fail_count, n)
        return ok_all

    def get_sub_results(self):
        return self._sub_results
