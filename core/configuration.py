"""Shared validation and comment-preserving, atomic configuration saves."""
from copy import deepcopy
from io import StringIO
from pathlib import Path
import math
import os
import re
import tempfile

from ruamel.yaml import YAML

from . import adb, resolution
from .tasks import build_task
from .hotkey import parse_hotkey


def yaml_codec():
    codec = YAML()
    codec.preserve_quotes = True
    codec.width = 120
    codec.indent(mapping=2, sequence=2, offset=0)
    return codec


def dump(value):
    stream = StringIO()
    yaml_codec().dump(value, stream)
    return stream.getvalue()


def parse(text):
    return yaml_codec().load(text)


def _number(obj, key, minimum=0, integer=False):
    if key not in obj:
        return
    value = obj[key]
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < minimum or (integer and not isinstance(value, int))):
        raise ValueError(f"{key} 必须是 >= {minimum} 的{'整数' if integer else '数值'}")


def _matchers(value, label):
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} 必须是非空进程匹配列表")
    for matcher in value:
        if not isinstance(matcher, dict) or not (matcher.get("names") or matcher.get("path_contains")):
            raise ValueError(f"{label} 的匹配项需要 names 或 path_contains")
        for key in ("names", "exclude_names"):
            if key in matcher and (not isinstance(matcher[key], list) or any(not isinstance(n, str) or not n for n in matcher[key])):
                raise ValueError(f"{label}.{key} 必须是进程名列表")
        if "path_contains" in matcher and not isinstance(matcher["path_contains"], str):
            raise ValueError(f"{label}.path_contains 必须是字符串")


def validate_task(task, defaults):
    if not isinstance(task, dict) or not isinstance(task.get("name"), str) or not task["name"].strip():
        raise ValueError("任务必须有名称")
    if task.get("resolution_check") is not None:
        if task.get("type") not in ("monitor", "retry_group"):
            raise ValueError("resolution_check 仅用于 monitor / retry_group 任务")
        resolution.validate(task["resolution_check"])
        categories = task.get("categories") or {}
        if task.get("type") == "retry_group":
            _matchers(task["resolution_check"].get("matchers"), "resolution_check.matchers")
        elif not task.get("game") and not any(c.get("role") == "game" for c in categories.values()):
            raise ValueError("分辨率检测需要 game 或 role=game 的进程类别")
    for key in ("enabled", "pause_kill", "check_exit_code"):
        if key in task and not isinstance(task[key], bool):
            raise ValueError(f"{task['name']}.{key} 必须为 true/false")
    if "check_exit_code" in task and task.get("type") != "launch_wait":
        raise ValueError("check_exit_code 仅用于 launch_wait 任务")
    for key in ("poll_interval", "heartbeat"):
        _number(task, key, 0.01)
    for key in ("stable_dead", "timeout", "attempt_timeout", "appear_timeout", "kill_timeout", "delay_after", "launch_interval", "linger"):
        _number(task, key)
    _number(task, "attempts", 1, True)
    _number(task, "window_size", 1, True)
    for key in ("watch", "cleanup", "kill", "targets"):
        if task.get(key):
            _matchers(task[key], key)
    specs = [task] if task.get("type") in ("launch", "launch_wait") else task.get("launch", [])
    if not isinstance(specs, list):
        raise ValueError("launch 必须是列表")
    recovery = task.get("recovery") or {}
    if not isinstance(recovery, dict):
        raise ValueError("recovery 必须是对象")
    _number(recovery, "attempts", 1, True)  # Legacy field accepted; runtime budget is fixed at two.
    roots = recovery.get("python_paths", [])
    if not isinstance(roots, list) or any(not isinstance(r, str) or not r.strip() for r in roots):
        raise ValueError("recovery.python_paths 必须是非空路径字符串列表")
    specs_to_check = specs + ([recovery["script"]] if "script" in recovery else [])
    for spec in specs_to_check:
        if not isinstance(spec, dict) or not isinstance(spec.get("exe"), str) or not spec["exe"].strip():
            raise ValueError("启动项 exe 不能为空")
        if "args" in spec and (not isinstance(spec["args"], list) or any(not isinstance(a, str) for a in spec["args"])):
            raise ValueError("args 必须是字符串列表")
        if "cwd" in spec and (not isinstance(spec["cwd"], str) or not spec["cwd"].strip()):
            raise ValueError("cwd 必须是非空工作目录")
        if "role" in spec and spec["role"] not in ("game", "script"):
            raise ValueError("启动项 role 必须是 game/script")
        _number(spec, "delay_after")
        if "wait_for_exit" in spec:
            if not isinstance(spec["wait_for_exit"], bool):
                raise ValueError("启动项 wait_for_exit 必须为 true/false")
            if spec["wait_for_exit"]:
                if task.get("type") != "retry_group":
                    raise ValueError("wait_for_exit 仅支持 retry_group 的 launch 启动项")
                if "timeout" not in spec:
                    raise ValueError("wait_for_exit 启动项必须配置 timeout")
                _number(spec, "timeout", 0.01)
    if task.get("type") == "retry_group":
        if not specs:
            raise ValueError("retry_group.launch 不能为空")
        _matchers(task.get("watch"), "watch")
    check = task.get("resolution_check") or {}
    if check.get("enabled", True) and check.get("require_1080p") and check.get("restart_with_args", True):
        if task.get("type") == "monitor" and not recovery.get("script"):
            raise ValueError("monitor 参数补跑必须配置 recovery.script 的 exe/cwd/args")
        if task.get("type") == "retry_group" and not recovery.get("script") and not any(s.get("role") == "script" for s in specs):
            raise ValueError("retry_group 参数重启需要在 launch 中标记 role: script 或配置 recovery.script")
    for spec in list((task.get("categories") or {}).values()) + [task[k] for k in ("game", "script") if task.get(k)]:
        _matchers(spec.get("matchers"), "matchers")
        _number(spec, "max_exits", 1, True)
        if spec.get("role", "script") not in ("script", "game"):
            raise ValueError("role 只能为 game/script")
    if task.get("error_log"):
        re.compile(task["error_log"].get("pattern", "ERROR"))
        _number(task["error_log"], "max_restarts", 0, True)
    build_task(task, defaults)
    children = task.get("monitors", [])
    names = set()
    for child in children:
        inherited = dict(defaults, execution_mode=task.get("execution_mode", defaults.get("execution_mode", "foreground")))
        validate_task(dict(child, type="monitor"), inherited)
        if child["name"] in names:
            raise ValueError("子任务名称不能重复")
        names.add(child["name"])


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError("配置根节点必须是对象")
    defaults = config.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ValueError("defaults 必须是对象")
    for key in ("poll_interval", "heartbeat"):
        _number(defaults, key, 0.01)
    for key in ("stable_dead", "kill_timeout"):
        _number(defaults, key)
    if defaults.get("execution_mode", "foreground") not in ("foreground", "background"):
        raise ValueError("defaults.execution_mode 无效")
    adb.validate(defaults.get("adb") or {})
    _number(config, "log_retention_days", 0, True)
    ap = config.get("activity_pause") or {}
    _number(ap, "idle_resume_seconds", 0, True)
    combinations = set()
    for combo in (ap.get("control_hotkeys") or {}).values():
        parsed = parse_hotkey(combo)
        if not parsed or parsed in combinations:
            raise ValueError(f"热键无效或重复：{combo}")
        combinations.add(parsed)
    for section in (ap, config.get("notify") or {}):
        if "enabled" in section and not isinstance(section["enabled"], bool):
            raise ValueError("enabled 必须为 true/false")
    queues = config.get("queues", [])
    if not isinstance(queues, list):
        raise ValueError("queues 必须为列表")
    queue_names = set()
    for queue in queues:
        if not isinstance(queue, dict) or not isinstance(queue.get("name"), str) or not queue["name"].strip() or queue["name"] in queue_names:
            raise ValueError("队列名称不能为空或重复")
        queue_names.add(queue["name"])
        if not isinstance(queue.get("enabled", True), bool):
            raise ValueError("队列 enabled 必须为 true/false")
        times = queue.get("times", [])
        if not isinstance(times, list) or not times or any(not isinstance(t, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", t) for t in times):
            raise ValueError(f"{queue['name']}：触发时间应为 HH:MM 列表")
        names = set()
        if not isinstance(queue.get("tasks", []), list):
            raise ValueError("tasks 必须为列表")
        for task in queue.get("tasks", []):
            validate_task(task, defaults)
            if task["name"] in names:
                raise ValueError(f"任务名称重复：{task['name']}")
            names.add(task["name"])
        for task in queue.get("tasks", []):
            if task.get("master_close_task") and not any(t.get("type") == "kill" and t["name"] == task["master_close_task"] for t in queue["tasks"]):
                raise ValueError(f"{task['name']} 的主控关闭任务不存在")


class ConfigDocument:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.original = self.path.read_bytes()
        self.data = parse(self.original.decode("utf-8-sig"))
        validate_config(self.data)

    def save(self, data):
        validate_config(data)
        encoded = dump(data).encode("utf-8")
        if self.path.read_bytes() != self.original:
            raise ValueError("配置文件已被其他程序修改，请重新打开后再编辑")
        self.path.with_suffix(self.path.suffix + ".bak").write_bytes(self.original)
        fd, name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        self.original = encoded
        self.data = deepcopy(data)
