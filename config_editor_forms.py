"""Structured desktop dialogs for scheduler configuration."""
from copy import deepcopy
from pathlib import PureWindowsPath
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from config_editor_schema import (Field, field_text, update_fields, at_path, put_path,
                                  LAUNCH_FIELDS, MATCH_FIELDS, RESOLUTION_FIELDS, RESOLUTION_ARGS, WW_FIELDS)


class StructuredPanel:
    def __init__(self, editor, host):
        self.editor = editor
        self.root = editor.root
        self.canvas = tk.Canvas(host, bg="#ffffff", highlightthickness=0)
        scroll = ttk.Scrollbar(host, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = ttk.Frame(self.canvas, style="Card.TFrame", padding=20)
        self.window = self.canvas.create_window((0, 0), anchor="nw", window=self.body)
        self.body.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.window, width=e.width))

    def render(self, value, kind):
        self.value = value
        for widget in self.body.winfo_children():
            widget.destroy()
        self.body.columnconfigure(0, weight=1)
        self.row = 0
        if kind == "global":
            self._header("鸣潮启动器", "启动器路径、游戏目录和按钮识别规则。")
            self._object_row("启动器设置", ("ww_launcher",), WW_FIELDS)
        elif kind == "queue":
            self._header("队列执行顺序", "从左侧选择任务；使用上移、下移调整执行顺序。相邻同名并行组同时执行。")
            for i, task in enumerate(value.get("tasks", [])):
                mode = {"foreground": "前台", "background": "后台"}.get(task.get("execution_mode"), "继承默认")
                text = f"{i+1}. {task['name']}  ·  {mode}"
                if task.get("parallel"):
                    text += f"  ·  并行组：{task['parallel']}"
                self._label(text)
        else:
            task_type = value.get("type", "monitor")
            if task_type in ("launch", "launch_wait"):
                self._header("启动程序", "参数一行一个，路径内的空格无需额外加引号。")
                self._object_row(self._launch_summary(value), (), LAUNCH_FIELDS)
            if task_type == "retry_group":
                fields = LAUNCH_FIELDS + (
                    Field("wait_for_exit", "等待此程序退出后再启动下一项", "bool", False),
                    Field("timeout", "等待退出超时（秒）", "number"),
                )
                self._list("启动顺序", ("launch",), fields, {"exe": "C:/path/script.exe", "role": "script"},
                           self._launch_summary, "按列表顺序启动。game = 游戏，script = 脚本；上下箭头调整顺序。")
            if task_type in ("monitor", "retry_group"):
                self._object_row("完成条件", (), (
                    Field("success_when", "完成条件", choices=("", "all_dead", "script_dead") if task_type == "monitor" else ("", "any_exit", "all_dead")),
                ), "all_dead：全部稳定退出；script_dead：仅脚本退出；空白：继承任务默认行为。")
            match_sections = {
                "retry_group": (("watch", "等待与监控进程"), ("cleanup", "重试清理名单")),
                "launch_wait": (("kill", "退出后清理名单"),),
                "kill": (("targets", "清理目标"),),
                "monitor": (("kill", "失败时清理名单"),),
            }
            for key, title in match_sections.get(task_type, ()):
                hint = "留空时使用全部监控进程。" if task_type == "monitor" else "同一条规则中的名称与路径需同时匹配；不同规则分别匹配。"
                self._matchers(title, (key,), hint)
            if task_type == "monitor":
                self._header("进程类别", "每类独立计算退出次数。game = 游戏，script = 脚本；类别模式存在时优先于简单游戏/脚本模式。")
                categories = value.get("categories") or {}
                for name, category in categories.items():
                    path = ("categories", name)
                    self._object_row(f"{name} · {category.get('role', 'script')} · 退出阈值 {category.get('max_exits', '默认')}", path,
                                     (Field("role", "角色", default="script", choices=("game", "script")), Field("max_exits", "退出次数阈值", "int")))
                    self._matchers(f"{name} · 匹配规则", (*path, "matchers"))
                    self._remove_button("删除此类别", path)
                self._button("＋ 新增进程类别", self._add_category)
                for key, title in (("game", "游戏进程"), ("script", "脚本进程")):
                    if categories and key not in value:
                        continue
                    self._object_row(f"{title} · 退出阈值", (key,), (Field("max_exits", "退出次数阈值", "int"),))
                    self._matchers(title, (key, "matchers"))
            if task_type in ("monitor", "retry_group"):
                self._header("分辨率与补跑", "异环需关闭参数重启；重启功能使用下面的启动参数与脚本设置。")
                self._object_row("1080p 巡检与启动参数", ("resolution_check",), RESOLUTION_FIELDS)
                self._object_row("各引擎的分辨率启动参数", ("resolution_check", "launch_args"), RESOLUTION_ARGS,
                                 "参数一行一个。auto 模式需同时填写 unity 和 ue；固定引擎只需填写对应项。")
                if task_type == "retry_group":
                    self._matchers("游戏窗口匹配", ("resolution_check", "matchers"))
                self._object_row("补跑脚本", ("recovery", "script"), LAUNCH_FIELDS)
                self._object_row("补跑 Python 目录", ("recovery",), (Field("python_paths", "Python 根目录 · 一行一个", "lines", []),))
            if task_type == "retry_group":
                self._header("脚本日志错误检测", "匹配到新日志中的错误后重启脚本；清空日志路径可关闭检测。")
                self._object_row("日志文件与匹配规则", ("error_log",), (
                    Field("path", "日志路径", browse="file"), Field("pattern", "错误正则表达式", default="ERROR"),
                    Field("max_restarts", "最多错误重启次数", "int", 10),
                ))
                if value.get("error_log"):
                    self._remove_button("关闭日志错误检测", ("error_log",))
            self._matchers("打断时最小化窗口", ("minimize_matchers",))
            self._object_row("打断与收尾策略", (), (
                Field("pause_kill", "暂停时清理进程", "bool", task_type in ("retry_group", "monitor", "launch_wait")),
                Field("master_close_task", "关联主控关闭任务"),
                Field("kill_timeout", "清理最长等待（秒）", "number"),
                Field("linger", "失败后后台清场（秒）", "number"),
                Field("heartbeat", "心跳间隔（秒）", "number"),
                Field("launch_interval", "启动项间隔（秒）", "number"),
            ))
        self._label("修改先暂存，点击顶部“保存配置”后写入文件。高级 YAML 可编辑扩展字段。")
        self.editor._scale_widgets(self.body)

    def _label(self, text):
        ttk.Label(self.body, text=text, style="Hint.TLabel", wraplength=610).grid(row=self.row, column=0, sticky="ew", pady=6)
        self.row += 1

    def _header(self, title, hint=""):
        ttk.Label(self.body, text=title, style="Card.TLabel", font=("Microsoft YaHei UI", 12, "bold")).grid(row=self.row, column=0, sticky="w", pady=(14, 6))
        self.row += 1
        if hint:
            self._label(hint)

    def _button(self, text, command):
        ttk.Button(self.body, text=text, command=command).grid(row=self.row, column=0, sticky="w", pady=5)
        self.row += 1

    def _object_row(self, title, path, fields, hint=""):
        frame = ttk.Frame(self.body, style="Card.TFrame")
        frame.grid(row=self.row, column=0, sticky="ew", pady=5)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text=title, style="Card.TLabel", wraplength=430).grid(row=0, column=0, sticky="w")
        ttk.Button(frame, text="编辑", command=lambda: self._dialog(title, path, fields, hint)).grid(row=0, column=1, padx=(10, 0))
        self.row += 1

    @staticmethod
    def _launch_summary(spec):
        name = PureWindowsPath(spec.get("exe") or "未设置程序").name
        args = spec.get("args") or []
        text = f"{name} · {spec.get('role', '默认角色')} · {len(args)} 个参数"
        if spec.get("wait_for_exit"):
            text += " · 等待退出"
        return text

    @staticmethod
    def _matcher_summary(matcher):
        parts = [", ".join(matcher.get("names") or []) or "任意进程名"]
        if matcher.get("path_contains"):
            parts.append(f"路径：{matcher['path_contains']}")
        if matcher.get("python_root"):
            parts.append(f"Python：{matcher['python_root']}")
        if matcher.get("exclude_names"):
            parts.append("排除：" + ", ".join(matcher["exclude_names"]))
        return " · ".join(parts)

    def _matchers(self, title, path, hint=""):
        self._list(title, path, MATCH_FIELDS, {"names": ["script.exe"]}, self._matcher_summary,
                   hint or "进程名支持 * 通配符。指定路径可区分不同目录的 python.exe。")

    def _list(self, title, path, fields, template, summary, hint):
        self._header(title, hint)
        try:
            items = at_path(self.value, path) or []
        except KeyError:
            items = []
        for index, item in enumerate(items):
            item_path = (*path, index)
            self._object_row(f"{index+1}. {summary(item)}", item_path, fields, hint)
            bar = ttk.Frame(self.body, style="Card.TFrame")
            bar.grid(row=self.row, column=0, sticky="w", pady=(0, 8))
            for label, delta in (("↑", -1), ("↓", 1)):
                button = ttk.Button(bar, text=label, width=3, command=lambda p=path, i=index, d=delta: self._move(p, i, d))
                button.pack(side="left", padx=(0, 6))
                if not 0 <= index + delta < len(items):
                    button.state(["disabled"])
            ttk.Button(bar, text="删除", command=lambda p=item_path: self._remove(p)).pack(side="left")
            self.row += 1
        if not items:
            self._label("尚未设置")
        self._button("＋ 新增", lambda: self._append(path, template))

    def _append(self, path, template):
        def change(value):
            try:
                items = at_path(value, path)
            except KeyError:
                put_path(value, path, [])
                items = at_path(value, path)
            if items is None:
                put_path(value, path, [])
                items = at_path(value, path)
            items.append(deepcopy(template))
        self.editor._structured_change(change)

    def _move(self, path, index, delta):
        def change(value):
            items = at_path(value, path)
            if 0 <= index + delta < len(items):
                item = items.pop(index)
                items.insert(index + delta, item)
        self.editor._structured_change(change)

    def _remove_button(self, title, path):
        self._button(title, lambda: self._remove(path))

    def _remove(self, path):
        if not messagebox.askyesno("删除设置", "删除这项设置？保存前不会修改文件。", parent=self.root):
            return
        def change(value):
            at_path(value, path[:-1]).pop(path[-1])
        self.editor._structured_change(change)

    def _add_category(self):
        name = simpledialog.askstring("新增进程类别", "类别名称，例如：游戏本体、脚本主体、Python", parent=self.root)
        if not name or not name.strip():
            return
        name = name.strip()
        def change(value):
            categories = value.setdefault("categories", {})
            if name in categories:
                raise ValueError("类别名称已存在")
            categories[name] = {"role": "script", "max_exits": 3, "matchers": [{"names": ["script.exe"]}]}
        self.editor._structured_change(change)

    def _dialog(self, title, path, fields, hint=""):
        try:
            current = self.editor._current()
            try:
                source = deepcopy(at_path(current, path))
            except KeyError:
                source = {}
            if source is None:
                source = {}
            if not isinstance(source, dict):
                raise ValueError("此节点必须是 YAML 对象，请在高级 YAML 中修正")
        except Exception as exc:
            messagebox.showerror("无法编辑", str(exc), parent=self.root)
            return
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title(title)
        dialog.transient(self.root)
        dialog.configure(bg="#ffffff")
        # Pin the action buttons outside the scroll area at every zoom level.
        footer = ttk.Frame(dialog, padding=12)
        footer.pack(side="bottom", fill="x")
        host = ttk.Frame(dialog, style="Card.TFrame")
        host.pack(fill="both", expand=True)
        canvas = tk.Canvas(host, bg="#ffffff", highlightthickness=0)
        scroll = ttk.Scrollbar(host, command=canvas.yview)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        form = ttk.Frame(canvas, style="Card.TFrame", padding=18)
        window = canvas.create_window((0, 0), anchor="nw", window=form)
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        form.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        def wheel(event):
            if event.state & 0x4 or isinstance(event.widget, tk.Text):
                return
            canvas.yview_scroll(-int(event.delta / 120), "units")
        dialog.bind("<MouseWheel>", wheel)
        form.columnconfigure(1, weight=1)
        row = 0
        if hint:
            ttk.Label(form, text=hint, style="Hint.TLabel", wraplength=540).grid(row=row, column=0, columnspan=3, sticky="ew", pady=(0, 12))
            row += 1
        controls, initial = {}, {}
        for field in fields:
            value = source.get(field.key, field.default)
            ttk.Label(form, text=field.label, style="Card.TLabel").grid(row=row, column=0, sticky="nw", padx=(0, 12), pady=8)
            if field.kind == "bool":
                var = tk.BooleanVar(master=dialog, value=value)
                widget = ttk.Checkbutton(form, variable=var, text="启用", style="Solid.TCheckbutton")
                getter = var.get
            elif field.kind in ("lines", "optional_lines"):
                widget = tk.Text(form, height=5, width=25, wrap="none", undo=True, font=("Consolas", 10), relief="solid", borderwidth=1)
                widget.insert("1.0", field_text(value, field.kind))
                getter = lambda w=widget: w.get("1.0", "end-1c")
                widget.bind("<Tab>", lambda event: (event.widget.tk_focusNext().focus_set(), "break")[1])
            else:
                var = tk.StringVar(master=dialog, value=field_text(value, field.kind))
                if field.choices:
                    choices = list(field.choices)
                    if var.get() not in choices:
                        choices.append(var.get())
                    widget = ttk.Combobox(form, textvariable=var, values=choices, state="readonly", width=25)
                else:
                    widget = ttk.Entry(form, textvariable=var, width=25)
                getter = var.get
            widget.grid(row=row, column=1, sticky="ew", pady=5)
            # Retain variables as well as bound getters until the dialog closes.
            controls[field.key] = getter
            initial[field.key] = getter()
            if field.browse:
                def pick(v=var, mode=field.browse):
                    result = filedialog.askdirectory(parent=dialog) if mode == "directory" else filedialog.askopenfilename(parent=dialog)
                    if result:
                        v.set(result)
                ttk.Button(form, text="浏览", command=pick).grid(row=row, column=2, padx=(8, 0))
            row += 1
        ttk.Label(form, text="空白数值继承默认值。未编辑的字段与注释会保留。", style="Hint.TLabel").grid(row=row, column=0, columnspan=3, sticky="w", pady=12)
        def accept():
            try:
                result = update_fields(source, fields, {key: getter() for key, getter in controls.items()}, initial)
                if result == source:
                    dialog.destroy()
                    return
                def change(value):
                    if path:
                        put_path(value, path, result)
                    else:
                        value.clear()
                        value.update(result)
                if self.editor._structured_change(change):
                    dialog.destroy()
            except Exception as exc:
                messagebox.showerror("设置有误", str(exc), parent=dialog)
        ttk.Button(footer, text="应用到配置", style="Accent.TButton", command=accept).pack(side="right")
        ttk.Button(footer, text="取消", command=dialog.destroy).pack(side="right", padx=8)
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        self.editor._scale_widgets(dialog)
        scale = self.editor.scale
        width = min(800*scale, self.root.winfo_screenwidth()-60)
        height = min((180 + 55*len(fields))*scale, self.root.winfo_screenheight()-120)
        x = max(0, min(self.root.winfo_rootx() + (self.root.winfo_width()-width)//2,
                       self.root.winfo_screenwidth()-width))
        y = max(0, min(self.root.winfo_rooty() + (self.root.winfo_height()-height)//2,
                       self.root.winfo_screenheight()-height-60))
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        dialog.deiconify()
        dialog.grab_set()
        dialog.wait_window()
