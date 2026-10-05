# -*- coding: utf-8 -*-
"""
Server酱³ 推送：任务最终失败时即时发送，队列运行结束后发送一条汇总消息，
内容按 成功 / 超时 / 未按时启动 / 其他失败 / 跳过 五个分类分节展示，
空分类不出现在消息里；tags 取本次出现的分类名（逗号连接），便于筛选。

SENDKEY 来源（按优先级）：
  1. config.yaml 的 notify.sendkey
  2. 配置文件同目录或脚本根目录下 .env 文件里的 SENDKEY=xxxx
"""
import logging
import os
import re
import time

log = logging.getLogger("notify")

try:
    from serverchan_sdk import sc_send as _sdk_sc_send
except ImportError:                     # 没装 SDK 时只记日志，不影响调度
    _sdk_sc_send = None

# 任务状态 → 报告里的中文描述
STATUS_LABELS = {
    "deferred":    "已转入参数补跑",
    "adb_error":   "脚本已退出，ADB 关闭 App 失败",
    "success":     "成功",
    "timeout":     "超时",
    "not_started": "未按时启动",
    "threshold":   "退出次数达到阈值",
    "resolution":  "分辨率未达标，失败次数达到阈值",
    "error_limit": "错误日志重启达到上限",
    "kill_giveup": "强杀超时放弃",
    "interrupted": "被手动中断",
    "skipped":     "手动跳过",
    "fail":        "失败",
}


def sc_send(sendkey, title, desp="", options=None):
    """使用 SDK 相同的 Server酱接口，并为连接/读取设置超时。"""
    if _sdk_sc_send is None:
        raise RuntimeError("未安装 serverchan-sdk，请先运行: pip install serverchan-sdk")
    import requests
    if sendkey.startswith("sctp"):
        match = re.match(r"^sctp(\d+)t", sendkey)
        if not match:
            raise ValueError("Server酱³ SENDKEY 格式无效")
        url = f"https://{match.group(1)}.push.ft07.com/send/{sendkey}.send"
    else:
        url = f"https://sctapi.ftqq.com/{sendkey}.send"
    response = requests.post(url, json={"title": title, "desp": desp, **(options or {})},
                             headers={"Content-Type": "application/json;charset=utf-8"}, timeout=(5, 15))
    response.raise_for_status()
    return response.json()


def _load_sendkey(notify_cfg, config_path):
    """优先级：config.yaml → .env（配置文件目录 → 脚本根目录）。"""
    key = (notify_cfg.get("sendkey") or "").strip()
    if key:
        return key
    dirs = []
    if config_path:
        dirs.append(os.path.dirname(os.path.abspath(config_path)))
    dirs.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for d in dirs:
        env_path = os.path.join(d, ".env")
        if not os.path.isfile(env_path):
            continue
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("SENDKEY") and "=" in line:
                        return line.split("=", 1)[1].strip().strip("'\"")
        except OSError:
            continue
    return ""


def _fmt_duration(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}小时{m}分"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"


def build_report(queue_name, results, skipped, started_at, finished_at):
    """
    汇总一个队列的运行结果为一条消息，返回 (title, desp, tags)。
    results: {任务名: (ok, status)}   skipped: [未启用/手动跳过的任务名]
    """
    success, timeout_list, not_started, other_fail, skipped_all = [], [], [], [], list(skipped)
    for name, (ok, status) in results.items():
        # 按状态分类（优先于 ok）：monitor 进程未出现会"放行但记 not_started"，
        # 应归入未按时启动而不是成功
        if status == "timeout":
            timeout_list.append(name)
        elif status == "not_started":
            not_started.append(name)
        elif status == "skipped":
            skipped_all.append(name)
        elif ok or status == "success":
            success.append(name)
        else:
            label = STATUS_LABELS.get(status, "失败")
            other_fail.append(f"{name}（{label}）")

    fail_total = len(timeout_list) + len(not_started) + len(other_fail)
    title = (f"【游戏调度】{queue_name}：成功{len(success)} 失败{fail_total}"
             + (f" 跳过{len(skipped_all)}" if skipped_all else ""))

    # (分类名, 图标, 条目)——空分类不进消息、不进 tags
    sections = [("成功", "✅", success), ("超时", "⏰", timeout_list),
                ("未按时启动", "🚫", not_started), ("其他失败", "❌", other_fail),
                ("跳过", "⏭️", skipped_all)]
    parts = [
        f"## 队列「{queue_name}」运行报告",
        "",
        f"- 开始：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started_at))}",
        f"- 结束：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(finished_at))}",
        f"- 耗时：{_fmt_duration(finished_at - started_at)}",
        "",
    ]
    tags = []
    for cat, icon, items in sections:
        if not items:
            continue
        tags.append(cat)
        parts.append(f"### {icon} {cat}（{len(items)}）")
        parts += [f"- {x}" for x in items]
        parts.append("")
    return title, "\n".join(parts), ",".join(tags)


def send_queue_report(queue_name, results, skipped, started_at, finished_at, config):
    """队列结束后由调度器调用。任何推送异常都只记日志，绝不影响调度主流程。"""
    title, desp, tags = build_report(queue_name, results, skipped,
                                   started_at, finished_at)
    _send_message(title, desp, tags or "游戏调度", config)


def send_task_failure(queue_name, task_name, status, config):
    """任务最终失败时即时发送；中断、跳过和 ADB 收尾警告不是任务失败。"""
    if status in ("success", "interrupted", "skipped", "adb_error"):
        return
    label = STATUS_LABELS.get(status, "失败")
    title = f"【任务失败】{task_name}：{label}"
    desp = (f"## 任务运行失败\n\n- 队列：{queue_name}\n- 任务：{task_name}\n"
            f"- 原因：{label}\n- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            "调度器将完成必要清理并继续后续任务。")
    _send_message(title, desp, "任务失败", config)


def _send_message(title, desp, tags, config):
    notify_cfg = (config or {}).get("notify") or {}
    if not notify_cfg.get("enabled", False):
        log.info("通知功能未启用（notify.enabled=false），跳过推送")
        return
    if _sdk_sc_send is None:
        log.error("未安装 serverchan-sdk，无法推送。请运行: pip install serverchan-sdk")
        return
    sendkey = _load_sendkey(notify_cfg, (config or {}).get("_config_path"))
    if not sendkey:
        log.error("notify.enabled=true 但未配置 sendkey（config.yaml 的 notify.sendkey 或 .env 的 SENDKEY）")
        return
    try:
        resp = sc_send(sendkey, title, desp, {"tags": tags})
        if resp.get("code") == 0:
            log.info("Server酱推送成功: %s", title)
        else:
            log.error("Server酱推送失败: %s", resp)
    except Exception as exc:
        # 网络异常可能带含 SENDKEY 的 URL，日志只保留异常类型。
        log.error("Server酱推送异常（%s），后续调度继续", type(exc).__name__)
