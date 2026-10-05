"""Shared field descriptions and lossless patches for both configuration editors."""
from copy import deepcopy
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str = "str"
    default: object = ""
    choices: tuple = ()
    browse: str = ""


def field_text(value, kind):
    if kind in ("lines", "optional_lines"):
        return "\n".join(value) if isinstance(value, list) else str(value or "")
    if kind == "list":
        return ", ".join(value) if isinstance(value, list) else str(value or "")
    return str(value) if value is not None else ""


def update_fields(source, fields, values, initial):
    """Patch changed controls only, so implicit defaults and extension keys survive."""
    result = deepcopy(source)
    for field in fields:
        value = values[field.key]
        if value == initial[field.key]:
            continue
        try:
            if field.kind == "bool":
                value = bool(value)
            elif field.kind in ("int", "number"):
                value = None if not value.strip() else int(value) if field.kind == "int" else float(value)
                if value is not None and not math.isfinite(value):
                    raise ValueError("请输入有限数值")
            elif field.kind in ("lines", "optional_lines"):
                # Spaces and commas belong to arguments; do not split or trim them.
                value = value.split("\n") if value else None if field.kind == "optional_lines" else []
            elif field.kind == "list":
                value = [part.strip() for part in value.replace("，", ",").split(",") if part.strip()]
            elif not value.strip():
                value = None
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{field.label}：{exc}") from exc
        if value is None:
            result.pop(field.key, None)
        else:
            result[field.key] = value
    return result


LAUNCH_FIELDS = (
    Field("exe", "启动程序", browse="file"),
    Field("cwd", "工作目录", browse="directory"),
    Field("args", "启动参数 · 一行一个", "lines", []),
    Field("role", "程序角色", choices=("", "game", "script")),
    Field("delay_after", "启动后等待（秒）", "number"),
)
MATCH_FIELDS = (
    Field("names", "进程名 · 逗号分隔", "list", []),
    Field("path_contains", "路径包含"),
    Field("exclude_names", "排除进程名", "list", []),
    Field("python_root", "Python 根目录", browse="directory"),
)
RESOLUTION_FIELDS = (
    Field("enabled", "启用窗口巡检", "bool", True),
    Field("require_1080p", "要求 1080p 窗口化", "bool", False),
    Field("engine", "游戏引擎", default="auto", choices=("auto", "unity", "ue", "ue4", "ue5")),
    Field("restart_with_args", "允许启动参数重启", "bool", True),
    Field("inspect_seconds", "巡检期限（秒，最多180）", "number", 180),
    Field("restart_grace_seconds", "重启后窗口等待（秒）", "number", 15),
)
RESOLUTION_ARGS = tuple(Field(key, f"{key} 参数 · 一行一个", "optional_lines", []) for key in ("unity", "ue", "ue4", "ue5"))
WW_FIELDS = (
    Field("launcher_exe", "鸣潮启动器", browse="file"),
    Field("game_dir", "游戏安装目录", browse="directory"),
    Field("launcher_names", "启动器进程名", "list", []),
    Field("window_keywords", "窗口标题关键词", "list", []),
    Field("start_texts", "开始按钮文字", "list", []),
    Field("game_names", "游戏进程名", "list", []),
    Field("timeout", "启动超时（秒）", "number", 180),
    Field("poll_interval", "检测间隔（秒）", "number", 2),
    Field("ready_seconds", "游戏稳定时长（秒）", "number", 5),
    Field("click_interval", "点击间隔（秒）", "number", 20),
    Field("max_clicks", "最大点击次数", "int", 3),
    Field("prefer_uia", "优先识别界面控件", "bool", True),
)


def at_path(value, path):
    for key in path:
        value = value[key]
    return value


def put_path(value, path, item):
    for key in path[:-1]:
        if isinstance(value, dict) and value.get(key) is None:
            value[key] = {}
        value = value.setdefault(key, {}) if isinstance(value, dict) else value[key]
    value[path[-1]] = item
