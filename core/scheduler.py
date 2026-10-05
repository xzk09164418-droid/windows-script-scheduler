# -*- coding: utf-8 -*-
"""队列执行器 + 每日定时调度器（不依赖第三方调度库）。

队列执行支持进度控制（由 control 模块/热键驱动）：
  pause   暂停并保存进度，恢复后从保存处继续（monitor 回到其主控 launch 任务）
  next    跳过当前任务，直接运行下一个
  prev    从上一个 start 类任务（launch / launch_wait / retry_group）开始运行
  restart 进度清零，从队列头重新运行
打断时的清理按任务性质分类：
  - AUTO-MAS 旗下的 monitor：按主控 launch 任务的 master_close_task 指向的
    kill 任务清单清理（主控 + 全部下属进程），恢复后从主控 launch 重新运行；
  - retry_group / launch_wait：按其 cleanup / kill 清单清理（adb 模拟器类
    只杀脚本，不杀模拟器）；activity_pause.minimize_matchers 命中的窗口
    （如模拟器）只最小化不关闭。

定时冲突：某队列运行期间错过的触发时间不会丢弃——队列跑完后立即补跑
（多个队列同时到期则按触发时间先后依次执行，绝不并发跑两个队列）。
"""
import logging
import os
import threading
import time
from datetime import datetime, timedelta

from . import activity, adb, control, notify, proc
from .tasks import build_task, RecoveryBatch

log = logging.getLogger("scheduler")

_last_log_cleanup = None        # 上次日志清理的 (年, 月)，用于每月清理一次

START_TYPES = ("launch", "launch_wait", "retry_group")   # 用户口径的 start 类任务


# ---------------------------------------------------------------- 日志清理

def cleanup_old_logs(log_dir, keep_days=30):
    """删除 log_dir 下 keep_days 天之前的 run_*.log，返回删除数量。"""
    if keep_days <= 0 or not os.path.isdir(log_dir):
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for fn in os.listdir(log_dir):
        if not (fn.startswith("run_") and fn.endswith(".log")):
            continue
        path = os.path.join(log_dir, fn)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
                log.info("清理旧日志: %s", fn)
        except OSError:
            continue
    if removed:
        log.info("日志清理完成，共删除 %d 个超过 %d 天的日志文件", removed, keep_days)
    else:
        log.info("日志清理完成，没有超过 %d 天的日志文件", keep_days)
    return removed


def maybe_cleanup_logs_monthly(config, force=False):
    """
    每月自动清理一次日志：调度器启动时（force=True）执行一次，
    之后每次队列运行结束检查是否跨月，跨月则再清理。
    保留天数由 config 的 log_retention_days 控制（默认 30 天，<=0 表示不清理）。
    """
    global _last_log_cleanup
    keep_days = (config or {}).get("log_retention_days", 30)
    now = datetime.now()
    month_key = (now.year, now.month)
    if not force and _last_log_cleanup == month_key:
        return
    log_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "logs")
    log.info("执行每月日志清理（保留最近 %d 天）……", keep_days)
    cleanup_old_logs(log_dir, keep_days)
    _last_log_cleanup = month_key


# ---------------------------------------------------------------- 进度控制辅助

def _build_master_map(cfgs):
    """
    找出 AUTO-MAS 类主控结构：launch 任务上配 master_close_task: <kill任务名>，
    其后的 monitor 被打断时，按该 kill 任务的 targets 清理（主控+全部下属），
    并回到主控 launch 任务重新运行。
    返回 {monitor 索引: (主控任务索引, 关闭清单 targets)}。
    """
    name_to_targets = {}
    for c in cfgs:
        if c.get("type") == "kill" and c.get("name"):
            name_to_targets[c["name"]] = c.get("targets") or []
    masters = {}
    last_master = None                   # (idx, close_task_name)
    for idx, c in enumerate(cfgs):
        if c.get("master_close_task"):
            last_master = (idx, c["master_close_task"])
            continue
        if (last_master and c.get("type") == "kill"
                and c.get("name") == last_master[1]):
            last_master = None           # 主控段落结束（遇到了它的关闭任务）
            continue
        if c.get("type") in ("monitor", "sliding_window") and last_master:
            masters[idx] = (last_master[0],
                            name_to_targets.get(last_master[1], []))
    return masters


def _prev_start_index(tasks, i):
    """i 之前最近的 start 类任务索引；没有则返回 0。"""
    for j in range(i - 1, -1, -1):
        if tasks[j].TYPE in START_TYPES:
            return j
    return 0


def _apply_action(act, i, tasks, masters):
    """根据控制动作计算新的执行位置。"""
    if act in ("restart", "prev") or (act == "pause" and i in masters):
        for task in tasks:
            task.deferred.clear()
            if isinstance(task, RecoveryBatch):
                task.pending.clear()
                task._sub_results.clear()
    if act == "next" and isinstance(tasks[i], RecoveryBatch):
        tasks[i].pending.clear()
    if act == "restart":
        log.warning("控制：任务进度清零，从队列头重新运行")
        return 0
    if act == "prev":
        j = _prev_start_index(tasks, i)
        log.warning("控制：从上一个 start 类任务「%s」开始运行", tasks[j].name)
        return j
    if act == "next":
        # 如果当前任务是并行组的一部分，跳过整个并行组
        if tasks[i].parallel:
            pg = tasks[i].parallel
            j = i
            while j < len(tasks) and tasks[j].enabled and tasks[j].parallel == pg:
                j += 1
            log.warning("控制：跳过当前并行组（%d 个任务），直接运行后续任务", j - i)
            return j
        log.warning("控制：跳过当前任务，直接运行下一个")
        return i + 1
    # pause：monitor 回到主控 launch 任务；其他任务原地重跑
    if i in masters:
        log.warning("恢复后将从主控任务「%s」重新运行",
                    tasks[masters[i][0]].name)
        return masters[i][0]
    return i


def _interrupt_cleanup(tasks, i, masters, config):
    """任务被打断后的分类清理（pause/next/prev/restart 共用）。"""
    ap = (config or {}).get("activity_pause") or {}
    mm = ap.get("minimize_matchers") or []
    if mm:
        try:
            proc.minimize_windows(mm)      # 模拟器等窗口只最小化，进程保留
        except Exception:
            log.exception("最小化窗口失败")
    t = tasks[i]
    try:
        if i in masters:
            m_idx, targets = masters[i]
            log.warning("打断 monitor：按主控关闭清单清理 AUTO-MAS 及其全部下属进程")
            for mt in targets:
                proc.kill_matched([mt])
        elif t.TYPE == "sliding_window":      # ← 新增
            # SlidingWindowTask 内部线程会自行结束（检测到 control 动作后抛出异常）
            # 这里不需要额外清理，因为 monitor 只监控不启动进程
            log.info("打断滑动窗口监控组：子监控线程将在下次轮询中自行退出")
            for m in t.all_kill_matchers:
                proc.kill_matched([m])
        elif t.TYPE == "retry_group":
            t._cleanup()                   # 只杀脚本（cleanup 名单不含模拟器）
        elif t.TYPE == "launch_wait":
            t._kill_mine()
        elif t.TYPE == "monitor":
            proc.kill_matched(t.kill)
    except Exception:
        log.exception("打断清理失败: %s", t.name)


# ---------------------------------------------------------------- 任务执行

def _run_one(t, results):
    t.status = None
    if t.TYPE == "sliding_window":
        t._sub_results = {}
    try:
        with activity.get().task_scope(t.foreground):
            return _run_one_scoped(t, results)
    except Exception:
        log.exception("[%s] 任务活动检测启动失败，继续后续任务", t.name)
        t.report_failure("fail")
        results[t.name] = (False, "fail")
        return None


def _run_one_scoped(t, results):
    """
    执行单个任务，结果写入 results[task.name] = (成功与否, 状态码)。
    被控制动作打断时返回动作名（pause/next/prev/restart），正常结束返回 None。
    """
    # 用户正在用电脑（或手动暂停中）时，等离开后再开始新任务
    log.info("--- 任务开始: %s", t.name)
    interrupted = None
    try:
        activity.get().wait_until_idle(t.name, 1, 60, foreground=t.foreground, stop_event=t._stop_event)
        t._check_interrupt()
        ok = bool(t.run())
    except control.TaskInterrupted as e:
        t._stop_event.set()
        ok, interrupted = False, e.action
    except Exception:
        ok = False
        t.report_failure("fail")
        log.exception("--- 任务异常（继续后续任务）: %s", t.name)
    if interrupted:
        # 记录为中断/跳过；若稍后重跑本任务，结果会被覆盖
        results[t.name] = (interrupted == "next",
                           "skipped" if interrupted == "next" else "interrupted")
        log.warning("--- 任务被打断（%s）: %s", interrupted, t.name)
        return interrupted
    results[t.name] = (ok, t.status or ("success" if ok else "fail"))
    if not ok and not t.get_sub_results():
        t.report_failure(results[t.name][1])
    if ok:
        log.info("--- 任务结束: %s（状态 %s）", t.name, results[t.name][1])
    else:
        log.warning("--- 任务失败（继续后续任务）: %s（状态 %s）",
                    t.name, results[t.name][1])
    return None


def _run_parallel_group(group, results):
    """并行执行一组任务（各自一个线程），全部完成后才返回。
    组内任一任务被打断即视为整组被打断，返回该动作。"""
    names = [t.name for t in group]
    log.info("===== 并行启动 %d 个任务: %s", len(group), " | ".join(names))
    outcomes = {}
    threads = []
    # 组内共享停止事件
    group_event = threading.Event()
    for t in group:
        t._stop_event = group_event
        th = threading.Thread(
            target=lambda x=t: outcomes.__setitem__(x.name, _run_one(x, results)),
            name=f"task-{t.name}", daemon=True)
        th.start()
        threads.append(th)
    for th in threads:
        th.join()
    log.info("===== 并行组完成: %s", " | ".join(names))
    for t in group:
        if outcomes.get(t.name):
            return outcomes[t.name]
    for t in group:
        if outcomes.get(t.name) is None:  # 该任务没有被中断（正常结束）
            results.pop(t.name, None)
            _flatten_sub_results(results, t.get_sub_results())
    return None


def _flatten_sub_results(results, sub):
    """
    把子任务结果展平进 results（原位替换，键保持一致）。
    参数补跑（RecoveryBatch）的最终结果要覆盖登记时记为 deferred 的旧条目——
    旧条目键带组名前缀（如 "5.滑动监控组 → 5.1 …"），补跑结果键是
    "5.1 …（参数补跑）"，二者本不会碰撞，导致同一件事在结果里出现两次：
    调度日志计数比通知多一，且 deferred 条目被报告过滤后成功数再少一。
    """
    if not sub:
        return
    suffix = "（参数补跑）"
    for new_key, val in list(sub.items()):
        base = new_key[:-len(suffix)] if new_key.endswith(suffix) else new_key
        for old_key, old_val in list(results.items()):
            if old_val[1] != "deferred":
                continue
            if old_key.split(" → ")[-1] == base:
                results[old_key] = val          # 原位覆盖，保持组名前缀键
                del sub[new_key]
                break
    results.update(sub)


def run_queue(queue, defaults, config=None):
    """
    顺序执行一个队列中的所有任务；单个任务失败不中断队列。
    支持进度控制热键（见模块 docstring）。队列结束后按 notify 配置推送结果。
    """
    started_at = time.time()
    log.info("=" * 60)
    log.info("开始执行队列【%s】", queue.get("name"))
    cfgs = list(queue.get("tasks", []))
    tasks = [build_task(c, defaults) for c in cfgs]
    original_masters = _build_master_map(cfgs)
    slots = {}
    for index, task in enumerate(tasks):
        if task.TYPE not in ("monitor", "sliding_window"):
            continue
        boundary = index
        if index in original_masters:
            master = cfgs[original_masters[index][0]]
            boundary = next(j for j in range(index + 1, len(cfgs))
                            if cfgs[j].get("type") == "kill" and
                            cfgs[j].get("name") == master["master_close_task"])
        elif task.parallel:
            while boundary + 1 < len(tasks) and tasks[boundary + 1].parallel == task.parallel:
                boundary += 1
        slots.setdefault(boundary, []).append(task)
    for boundary in sorted(slots, reverse=True):
        batch = RecoveryBatch(slots[boundary], defaults)
        tasks.insert(boundary + 1, batch)
        cfgs.insert(boundary + 1, {"name": batch.name, "type": "retry_group"})
    adb_session = adb.CleanupSession()
    for task in tasks:
        task.adb_session = adb_session
        task.failure_callback = lambda name, status: notify.send_task_failure(
            queue.get("name", "未命名队列"), name, status, config or {})
    masters = _build_master_map(cfgs)
    results = {}            # {任务名: (ok, status)}
    skipped = []            # 未启用的任务名
    control.clear()         # 丢弃队列开始前的遗留控制动作
    i = 0
    while i < len(tasks):
        # 任务间隙也可能积累了控制动作（如暂停期间按的跳过/上一个/清零）
        pending = control.consume_action()
        if pending:
            i = _apply_action(pending, i, tasks, masters)
            continue
        t = tasks[i]
        if isinstance(t, RecoveryBatch) and not t.collect():
            i += 1
            continue
        if not t.enabled:
            log.info("--- 跳过（已禁用）: %s", t.name)
            skipped.append(t.name)
            i += 1
            continue
        if t.parallel:
            group = []
            pg = t.parallel
            start_i = i
            while (i < len(tasks) and tasks[i].enabled
                   and tasks[i].parallel == pg):
                group.append(tasks[i])
                i += 1
            interrupted = _run_parallel_group(group, results)
            if interrupted:
                for g in group:                 # 整组按各自类型清理
                    _interrupt_cleanup(tasks, tasks.index(g), masters, config)
                i = start_i
                if interrupted == "pause":
                    with activity.get().task_scope(any(g.foreground for g in group)):
                        activity.get().wait_until_idle(t.name, 1, 60, foreground=any(g.foreground for g in group))
                # 取出触发中断的动作（pause 无暂存则为 None），暂停期间按的键也算
                nxt = control.consume_action()
                if nxt or interrupted != "pause":
                    i = _apply_action(nxt or interrupted, start_i, tasks, masters)
                # 否则 i 保持 start_i：整组重跑
            continue
        t.clear_stop_event()
        interrupted = _run_one(t, results)
        if interrupted:
            _interrupt_cleanup(tasks, i, masters, config)
            if interrupted == "pause":
                # 暂停：等用户离开；期间可能按了其他控制键
                with activity.get().task_scope(t.foreground):
                    activity.get().wait_until_idle(t.name, 1, 60, foreground=t.foreground)
            # 取出触发中断的动作并清除（peek 只读不取，否则会被应用两次）；
            # 暂停期间按下的新动作优先
            nxt = control.consume_action()
            act = nxt or interrupted
            i = _apply_action(act, i, tasks, masters)
            continue
        # ---- 新增：将子任务结果注入最终报告 ----
        sub = t.get_sub_results()
        if sub:
            log.info("展平任务 %s 的子结果（%d 个子任务）", t.name, len(sub))
            results.pop(t.name, None)
            _flatten_sub_results(results, sub)
        i += 1
    ok = sum(1 for v in results.values() if v[0])
    fail = sum(1 for v in results.values() if not v[0])
    log.info("队列【%s】执行完毕：成功 %d，失败 %d，跳过 %d",
             queue.get("name"), ok, fail, len(skipped))
    log.info("=" * 60)
    # 每月日志清理（跨月后的第一次队列结束时触发）
    try:
        maybe_cleanup_logs_monthly(config or {})
    except Exception:
        log.exception("日志清理失败（不影响调度）")
    # Server酱 推送结果报告
    try:
        # Deferred recovery is intermediate; all actual failures remain in the report.
        report_results = {name: result for name, result in results.items()
                          if result[1] != "deferred"}
        notify.send_queue_report(queue.get("name", "未命名队列"),
                                 report_results, skipped, started_at,
                                 time.time(), config or {})
    except Exception:
        log.exception("结果通知失败（不影响调度）")
    return results


# ---------------------------------------------------------------- 定时调度

def _next_run(times, now):
    """times: ["06:00", ...]，返回最近一次未来触发时间。"""
    cands = []
    for t in times:
        t = str(t)                  # 防御：配置里时间若被解析成非字符串也不崩
        hh, mm = int(t[:2]), int(t[3:5])
        cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        cands.append(cand)
    return min(cands)


def _missed_trigger(times, started, finished):
    """
    队列在 started 开跑、finished 结束，返回 (started, finished] 区间内
    最早被错过的触发时间；没有错过返回 None（用于跑完立即补跑）。
    """
    cands = []
    day = started.date()
    while day <= finished.date():
        base = datetime.combine(day, datetime.min.time())
        for t in times:
            t = str(t)
            cand = base.replace(hour=int(t[:2]), minute=int(t[3:5]))
            if started < cand <= finished:
                cands.append(cand)
        day += timedelta(days=1)
    return min(cands) if cands else None


def run_forever(config, initial_queue=None):
    defaults = config.get("defaults", {})
    queues = [q for q in config.get("queues", []) if q.get("enabled", True)]
    if not queues and initial_queue is None:
        raise RuntimeError("config 中没有启用的队列")
    log.info("调度器启动，共 %d 个队列", len(queues))
    maybe_cleanup_logs_monthly(config, force=True)      # 启动时清理一次旧日志
    # 每个队列独立跟踪下次触发时间：运行期间错过的触发会补跑而不是丢弃
    scheduled_from = datetime.now()
    next_at = {id(q): _next_run(q["times"], scheduled_from) for q in queues}
    if initial_queue is not None:
        log.info("先执行手动队列【%s】，期间到期的定时队列将在其完成后串行补跑", initial_queue.get("name"))
        try:
            run_queue(initial_queue, defaults, config)
        except Exception:
            log.exception("手动队列执行异常，仍继续进入定时调度")
        log.info("手动队列执行结束，进入常驻定时调度")
    if not queues:
        log.warning("没有启用的定时队列，手动执行完成后退出")
        return
    while True:
        now = datetime.now()
        # 找全局最近的 (队列, 触发时间)；已到期的（<= now）立即执行，
        # 因此前一队列运行期间错过触发时间的队列会紧接着补跑，绝不并发
        nxt_q = min(queues, key=lambda q: next_at[id(q)])
        nxt_t = next_at[id(nxt_q)]
        log.info("下次执行：队列【%s】 @ %s", nxt_q.get("name"),
                 nxt_t.strftime("%Y-%m-%d %H:%M:%S"))
        # 分段睡眠，便于响应中断、对系统休眠更稳健
        while datetime.now() < nxt_t:
            time.sleep(10)
        began = datetime.now()
        try:
            run_queue(nxt_q, defaults, config)
        except Exception:
            log.exception("队列执行出现异常，调度器继续运行")
        finished = datetime.now()
        # 本队列运行期间若错过了自己的后续触发时间，排入补跑
        missed = _missed_trigger(nxt_q["times"], nxt_t, finished)
        if missed:
            log.warning("队列【%s】运行期间错过了 %s 的触发，稍后立即补跑",
                        nxt_q.get("name"), missed.strftime("%Y-%m-%d %H:%M"))
            next_at[id(nxt_q)] = missed
        else:
            next_at[id(nxt_q)] = _next_run(nxt_q["times"], finished)
        time.sleep(1)
