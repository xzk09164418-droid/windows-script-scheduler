"""A desktop configuration editor. Run with pythonw config_editor.py."""
import argparse
import json
import math
from copy import deepcopy
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import font as tkfont
from weakref import WeakKeyDictionary
from tkinter import filedialog, messagebox, simpledialog, ttk

from core import adb
from core.configuration import ConfigDocument, dump, parse, validate_config
from config_editor_forms import StructuredPanel

ROOT = Path(__file__).resolve().parent
BG, PANEL, INK, MUTED, BLUE = "#f1f5f9", "#ffffff", "#17263c", "#64748b", "#2563eb"
MODES = {"继承默认": None, "前台 · 检测键鼠活动": "foreground", "后台 · 忽略键鼠活动": "background"}
TYPE_NAMES = {"launch": "启动程序", "launch_wait": "启动并等待", "retry_group": "启动与重试",
              "monitor": "进程监控", "sliding_window": "滑动监控组", "kill": "清理进程"}


def enable_dpi_awareness():
    # Keep Windows from bitmap-scaling the editor into blurry text.
    try:
        import ctypes
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass


def integer_scale(dpi):
    """96/192/288 DPI → 100/200/300%；非整数倍向下取整。"""
    return max(1, math.floor(float(dpi) / 96 + 1e-6))


def window_dpi(root):
    try:
        import ctypes
        get_dpi = ctypes.windll.user32.GetDpiForWindow
        get_dpi.argtypes = [ctypes.c_void_p]
        get_dpi.restype = ctypes.c_uint
        dpi = get_dpi(root.winfo_id())
        if dpi:
            return dpi
    except (AttributeError, OSError):
        pass
    return root.winfo_fpixels("1i")


def task_template(kind, name):
    base = {"name": name, "type": kind, "enabled": True, "execution_mode": "foreground"}
    matcher = {"names": ["script.exe"]}
    if kind in ("launch", "launch_wait"):
        base.update(exe="C:/path/script.exe", args=[])
    elif kind == "retry_group":
        base.update(launch=[{"exe": "C:/path/script.exe"}], watch=[matcher], cleanup=[deepcopy(matcher)], attempts=2)
    elif kind == "monitor":
        base.update(script={"matchers": [matcher]}, timeout=3600)
    elif kind == "sliding_window":
        base.update(window_size=1, monitors=[task_template("monitor", "子监控 1")])
    else:
        base["targets"] = [matcher]
    return base


class ConfigEditor:
    def __init__(self, root, path):
        self.root = root
        self.system_scale = integer_scale(window_dpi(root))
        self.scale = self.system_scale
        self.manual_zoom = False
        self._wheel_delta = 0
        self._widget_metrics = WeakKeyDictionary()
        self._check_images = {}
        self._named_fonts = {name: tkfont.nametofont(name, root=root).actual()
                             for name in tkfont.names(root)}
        self.root.tk.call("tk", "scaling", 96 / 72)
        self.document = ConfigDocument(path)
        self.data = deepcopy(self.document.data)
        self.selected = None
        self.nodes = {}
        self.vars = {}
        self.form_source = None
        self.last_tab = 0
        self.loading = False
        self.jobs = queue.Queue()
        self.root.title("任务调度器 · 配置工作台")
        self.root.configure(bg=BG)
        self._style()
        self._layout()
        self._rebuild("global")
        self.set_scale(self.scale, manual=False)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Control-s>", lambda _: self.save())
        self.root.bind("<Control-o>", lambda _: self.open_file())
        self.root.bind("<Control-0>", lambda _: self.reset_zoom())
        # Bind before widget classes so Ctrl+wheel cannot also scroll text or change a combo.
        self.root.bind_class("EditorZoom", "<Control-MouseWheel>", self._zoom_wheel)
        self.poll_job = self.root.after(120, self._poll_jobs)
        self.root.after_idle(lambda: self.canvas.yview_moveto(0))

    def _style(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure(".", font=self._font(10), background=BG, foreground=INK)
        s.configure("Card.TFrame", background=PANEL)
        s.configure("Card.TLabel", background=PANEL, foreground=INK)
        s.configure("Hint.TLabel", background=PANEL, foreground=MUTED, font=self._font(9))
        s.configure("Title.TLabel", background=BG, font=self._font(23, "bold"))
        s.configure("TButton", padding=(12*self.scale, 8*self.scale), borderwidth=0)
        s.configure("Accent.TButton", background=BLUE, foreground="white")
        s.map("Accent.TButton", background=[("active", "#1d4ed8")])
        s.configure("TEntry", padding=7*self.scale, fieldbackground="white")
        s.configure("TCombobox", padding=6*self.scale, arrowsize=14*self.scale, fieldbackground="white")
        s.configure("TCheckbutton", background=PANEL, padding=4*self.scale, indicatorsize=12*self.scale)
        self._style_solid_checkbox(s)
        s.configure("TScrollbar", arrowsize=14*self.scale)
        s.configure("Treeview", rowheight=35*self.scale, indent=20*self.scale, background="#14243b", fieldbackground="#14243b", foreground="#dbe7f5", borderwidth=0)
        s.map("Treeview", background=[("selected", "#285598")], foreground=[("selected", "white")])
        s.configure("TNotebook", background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", padding=(22*self.scale, 11*self.scale))
        s.map("TNotebook.Tab", background=[("selected", PANEL)], foreground=[("selected", BLUE)])

    def _style_solid_checkbox(self, style):
        """Render the selected box as a solid square instead of clam's cross."""
        element = f"SolidCheck{self.scale}.indicator"
        if self.scale not in self._check_images:
            size, border = 16*self.scale, self.scale
            off = tk.PhotoImage(master=self.root, width=20*self.scale, height=size)
            on = tk.PhotoImage(master=self.root, width=20*self.scale, height=size)
            for bitmap in (off, on):
                bitmap.put("#111111", to=(0, 0, size, size))
            off.put(PANEL, to=(border, border, size-border, size-border))
            self._check_images[self.scale] = (off, on)
            style.element_create(element, "image", off, ("selected", on), sticky="w")
        style.configure("Solid.TCheckbutton", background=PANEL, padding=4*self.scale)
        style.layout("Solid.TCheckbutton", [("Checkbutton.padding", {
            "sticky": "nswe", "children": [
                (element, {"side": "left", "sticky": "w"}),
                ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                    ("Checkbutton.label", {"sticky": "nswe"})]})]})])

    def _style_combobox_popup(self, widget):
        # Tk creates this listbox outside tkinter's widget tree, so the ordinary
        # recursive scaling pass never reaches it. Configure it explicitly.
        popdown = self.root.tk.call("ttk::combobox::PopdownWindow", str(widget))
        self.root.tk.call(f"{popdown}.f.l", "configure",
                          "-font", widget.cget("font"),
                          "-background", PANEL, "-foreground", INK,
                          "-selectbackground", "#111111", "-selectforeground", "#ffffff",
                          "-selectborderwidth", 0)

    def _layout(self):
        header = ttk.Frame(self.root, padding=(26, 20, 26, 12))
        header.pack(fill="x")
        ttk.Label(header, text="配置工作台", style="Title.TLabel").pack(side="left")
        ttk.Button(header, text="保存配置  Ctrl+S", style="Accent.TButton", command=self.save).pack(side="right", padx=(10, 0))
        ttk.Button(header, text="校验配置", command=self.validate).pack(side="right", padx=6)
        ttk.Button(header, text="打开文件", command=self.open_file).pack(side="right", padx=6)
        self.path_label = ttk.Label(self.root, text=str(self.document.path), foreground=MUTED)
        self.path_label.pack(anchor="w", padx=28, pady=(0, 18))
        self.zoom_label = ttk.Button(header, text="", command=self.reset_zoom)
        self.zoom_label.pack(side="right", padx=6)
        body = ttk.Frame(self.root, padding=(20, 0, 20, 0))
        body.pack(fill="both", expand=True)
        sidebar = tk.Frame(body, bg="#14243b", width=290)
        sidebar.pack(side="left", fill="y", padx=(0, 18))
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="TASK SCHEDULER", bg="#14243b", fg="#7ca8e8", font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x", padx=18, pady=(18, 4))
        self.stats = tk.Label(sidebar, text="", bg="#14243b", fg="white", anchor="w", font=("Microsoft YaHei UI", 11))
        self.stats.pack(fill="x", padx=18, pady=(0, 12))
        self.tree = ttk.Treeview(sidebar, show="tree", selectmode="browse", height=1)
        self.tree.column("#0", width=430, minwidth=280, stretch=False)
        self.tree.pack(fill="both", expand=True, padx=6)
        tree_scroll = ttk.Scrollbar(sidebar, orient="horizontal", command=self.tree.xview)
        self.tree.configure(xscrollcommand=tree_scroll.set)
        tree_scroll.pack(fill="x", padx=6)
        self.tree.bind("<<TreeviewSelect>>", self._select)
        bar = tk.Frame(sidebar, bg="#14243b")
        bar.pack(fill="x", padx=10, pady=12)
        for text, command in (("＋ 队列", self.add_queue), ("＋ 任务", self.add_task), ("复制", self.duplicate), ("删除", self.delete)):
            ttk.Button(bar, text=text, command=command).pack(fill="x", pady=3)
        order = tk.Frame(sidebar, bg="#14243b")
        order.pack(fill="x", padx=10, pady=(0, 14))
        ttk.Button(order, text="↑ 上移", command=lambda: self.move(-1)).pack(side="left", expand=True, fill="x", padx=(0, 3))
        ttk.Button(order, text="↓ 下移", command=lambda: self.move(1)).pack(side="left", expand=True, fill="x")
        content = ttk.Frame(body)
        content.pack(side="left", fill="both", expand=True)
        self.heading = ttk.Label(content, text="", font=("Microsoft YaHei UI", 17, "bold"))
        self.heading.pack(anchor="w", pady=(4, 5))
        self.subtitle = ttk.Label(content, text="", foreground=MUTED)
        self.subtitle.pack(anchor="w", pady=(0, 16))
        self.tabs = ttk.Notebook(content)
        self.tabs.pack(fill="both", expand=True)
        form_host = ttk.Frame(self.tabs, style="Card.TFrame")
        self.tabs.add(form_host, text="常用设置")
        self.canvas = tk.Canvas(form_host, bg=PANEL, highlightthickness=0)
        scroll = ttk.Scrollbar(form_host, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.form = ttk.Frame(self.canvas, style="Card.TFrame", padding=22)
        self.form_window = self.canvas.create_window((0, 0), anchor="nw", window=self.form)
        self.form.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.form_window, width=e.width))
        details = ttk.Frame(self.tabs, style="Card.TFrame")
        self.tabs.add(details, text="启动与进程")
        self.structured = StructuredPanel(self, details)
        self.root.bind_all("<MouseWheel>", self._wheel)
        raw = ttk.Frame(self.tabs, style="Card.TFrame", padding=16)
        self.tabs.add(raw, text="高级 YAML")
        ttk.Label(raw, text="编辑当前节点的全部字段；支持注释、进程匹配、启动参数和嵌套子任务。", style="Hint.TLabel").pack(anchor="w", pady=(0, 10))
        self.raw = tk.Text(raw, wrap="none", undo=True, font=("Consolas", 11), bg="#f8fafc", fg=INK, insertbackground=BLUE, relief="flat", padx=16, pady=14)
        raw_scroll = ttk.Scrollbar(raw, command=self.raw.yview)
        raw_scroll.pack(side="right", fill="y")
        self.raw.configure(yscrollcommand=raw_scroll.set)
        self.raw.pack(fill="both", expand=True)
        self.tabs.bind("<<NotebookTabChanged>>", self._tab_changed)
        self.status = tk.StringVar(value="就绪 · 保存前自动校验，原配置备份为 .bak。修改将在调度器重启后生效。")
        self.status_label = ttk.Label(self.root, textvariable=self.status, foreground=MUTED, padding=(26, 14))
        self.status_label.pack(side="bottom", fill="x", before=body)

    def _wheel(self, event):
        if event.state & 0x4:
            return "break"
        widget = event.widget
        while widget:
            if widget == self.structured.body or widget == self.structured.canvas:
                self.structured.canvas.yview_scroll(-int(event.delta / 120), "units")
                return
            if widget == self.form or widget == self.canvas:
                self.canvas.yview_scroll(-int(event.delta / 120), "units")
                return
            widget = getattr(widget, "master", None)

    def _font(self, points, weight="normal"):
        return ("Microsoft YaHei UI", -round(points * 96 / 72 * self.scale), weight)

    def _scale_widgets(self, widget):
        """Scale pixel metrics from their original values; preserve all live editor contents."""
        if widget is not self.root and widget not in self._widget_metrics:
            metrics = {"options": {}, "layout": {}}
            for key in ("padding", "padx", "pady", "wraplength"):
                if key in widget.keys() and str(widget.cget(key)):
                    metrics["options"][key] = widget.cget(key)
            if isinstance(widget, tk.Frame) and not isinstance(widget, ttk.Frame):
                metrics["options"]["width"] = widget.cget("width")
            if "font" in widget.keys() and str(widget.cget("font")):
                spec = widget.cget("font")
                metrics["font"] = self._named_fonts.get(str(spec)) or tkfont.Font(root=self.root, font=spec).actual()
            manager = widget.winfo_manager()
            if manager in ("pack", "grid"):
                info = widget.pack_info() if manager == "pack" else widget.grid_info()
                metrics["manager"] = manager
                metrics["layout"] = {key: info[key] for key in ("padx", "pady", "ipadx", "ipady") if key in info}
            self._widget_metrics[widget] = metrics
            if isinstance(widget, ttk.Combobox):
                widget.configure(postcommand=lambda w=widget: self._style_combobox_popup(w))
        metrics = self._widget_metrics.get(widget)
        if metrics:
            def scaled(value):
                values = self.root.tk.splitlist(value) if isinstance(value, (str, tuple)) else (value,)
                output = tuple(round(float(str(v)) * self.scale) for v in values)
                return output[0] if len(output) == 1 else output
            widget.configure(**{key: scaled(value) for key, value in metrics["options"].items()})
            if "font" in metrics:
                font = dict(metrics["font"])
                size = font["size"]
                font["size"] = -round((size * 96 / 72 if size > 0 else -size) * self.scale)
                metrics["scaled_font"] = tkfont.Font(root=self.root, **font)
                widget.configure(font=metrics["scaled_font"])
            if isinstance(widget, ttk.Combobox):
                self._style_combobox_popup(widget)
            if metrics["layout"]:
                options = {key: scaled(value) for key, value in metrics["layout"].items()}
                (widget.pack_configure if metrics["manager"] == "pack" else widget.grid_configure)(**options)
        tags = widget.bindtags()
        if "EditorZoom" not in tags:
            widget.bindtags(("EditorZoom",) + tags)
        for child in widget.winfo_children():
            self._scale_widgets(child)

    def set_scale(self, scale, manual=True):
        self.scale = max(1, int(scale))
        self.manual_zoom = manual
        self.root.tk.call("tk", "scaling", 96 / 72 * self.scale)
        self._style()
        self._scale_widgets(self.root)
        self.tree.column("#0", width=430*self.scale, minwidth=280*self.scale)
        self.zoom_label.configure(text=f"{self.scale*100}% · Ctrl+滚轮")
        # Preserve readable controls while keeping the window within the screen.
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        width, height = min(1220*self.scale, sw-40), min(860*self.scale, sh-100)
        self.root.minsize(min(1040*self.scale, sw-40), min(740*self.scale, sh-100))
        self.root.geometry(f"{max(400,width)}x{max(300,height)}")
        self.root.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _zoom_wheel(self, event):
        self._wheel_delta += event.delta
        steps = int(self._wheel_delta / 120)
        if steps:
            self._wheel_delta -= steps * 120
            self.set_scale(self.scale + steps)
        return "break"

    def reset_zoom(self):
        self.system_scale = integer_scale(window_dpi(self.root))
        self.set_scale(self.system_scale, manual=False)

    def _rebuild(self, select=None):
        self.loading = True
        self.tree.delete(*self.tree.get_children())
        self.nodes = {"global": ("global", self.data, None, None)}
        self.tree.insert("", "end", iid="global", text="  全局设置", open=True)
        count = 0
        def tasks(parent, values):
            nonlocal count
            for index, task in enumerate(values):
                iid = f"{parent}/t{index}"
                self.nodes[iid] = ("task", task, values, index)
                mode = "后台" if task.get("execution_mode") == "background" else "前台" if task.get("execution_mode") == "foreground" else "继承"
                self.tree.insert(parent, "end", iid=iid, text=f"{'●' if task.get('enabled', True) else '○'} {task['name']} · {mode}", open=True)
                count += 1
                tasks(iid, task.get("monitors", []))
        for index, q in enumerate(self.data.get("queues", [])):
            iid = f"q{index}"
            self.nodes[iid] = ("queue", q, self.data["queues"], index)
            self.tree.insert("", "end", iid=iid, text=f"  {q['name']}", open=True)
            tasks(iid, q.get("tasks", []))
        self.stats.configure(text=f"{len(self.data.get('queues', []))} 个队列  /  {count} 个任务")
        iid = select if select in self.nodes else "global"
        self.selected = iid
        self.tree.selection_set(iid)
        self.tree.see(iid)
        self.loading = False
        self._load()

    def _node_data(self):
        kind, node, _, _ = self.nodes[self.selected]
        return deepcopy({k: v for k, v in node.items() if k != "queues"}) if kind == "global" else deepcopy(node)

    def _load(self):
        self.loading = True
        self.form_source = self._node_data()
        kind, node, _, _ = self.nodes[self.selected]
        self.heading.configure(text="全局设置" if kind == "global" else node["name"])
        self.subtitle.configure(text={"global": "设置默认策略、活动检测与结果通知。", "queue": "管理每天的触发时间与执行顺序。", "task": "前后台属性控制活动检测；手动控制热键始终保留。"}[kind])
        self._render_form()
        self._scale_widgets(self.form)
        self._set_raw(self.form_source)
        self.last_tab = self.tabs.index(self.tabs.select())
        self.canvas.yview_moveto(0)
        self.structured.canvas.yview_moveto(0)
        self.loading = False

    def _set_raw(self, value):
        self.raw.delete("1.0", "end")
        self.raw.insert("1.0", dump(value))
        self.raw.edit_reset()

    def _section(self, title, hint=""):
        ttk.Label(self.form, text=title, style="Card.TLabel", font=("Microsoft YaHei UI", 12, "bold")).grid(row=self.row, column=0, columnspan=3, sticky="w", pady=(14, 8))
        self.row += 1
        if hint:
            ttk.Label(self.form, text=hint, style="Hint.TLabel", wraplength=610).grid(row=self.row, column=0, columnspan=3, sticky="w", pady=(0, 10))
            self.row += 1

    def _field(self, label, path, default="", kind="str", choices=None, browse=False, secret=False):
        value = self.form_source
        for key in path:
            value = value.get(key, {}) if isinstance(value, dict) else {}
        if isinstance(value, dict):
            value = default
        if kind == "mode":
            value = next((label for label, mode in MODES.items() if mode == value), "继承默认")
        elif kind == "args":
            value = json.dumps(value or [], ensure_ascii=False)
        elif kind == "list":
            value = ", ".join(value) if isinstance(value, list) else value
        var = tk.BooleanVar(value=value) if kind == "bool" else tk.StringVar(value=str(value))
        self.vars[path] = (var, kind, var.get())
        ttk.Label(self.form, text=label, style="Card.TLabel").grid(row=self.row, column=0, sticky="w", padx=(0, 20), pady=6)
        if kind == "bool":
            widget = ttk.Checkbutton(self.form, variable=var, text="启用", style="Solid.TCheckbutton")
        elif choices or kind == "mode":
            widget = ttk.Combobox(self.form, textvariable=var, values=choices or list(MODES), state="readonly")
        else:
            widget = ttk.Entry(self.form, textvariable=var, show="●" if secret else "")
        widget.grid(row=self.row, column=1, sticky="ew", pady=5)
        if browse:
            def pick():
                result = filedialog.askopenfilename(parent=self.root, title="选择程序", filetypes=[("可执行程序", "*.exe"), ("所有文件", "*.*")])
                if result:
                    var.set(result)
            ttk.Button(self.form, text="浏览", command=pick).grid(row=self.row, column=2, padx=(8, 0))
        self.row += 1

    def _render_form(self):
        for child in self.form.winfo_children():
            child.destroy()
        self.vars = {}
        self.row = 0
        self.form.columnconfigure(1, weight=1)
        kind = self.nodes[self.selected][0]
        f = self._field
        if kind == "global":
            self._section("默认运行策略", "任务未单独填写时使用这些数值，时间单位为秒。")
            f("默认执行模式", ("defaults", "execution_mode"), "foreground", "mode")
            for key, title, default in (("poll_interval", "巡检间隔", 5), ("stable_dead", "稳定退出时长", 20), ("heartbeat", "心跳间隔", 60), ("kill_timeout", "强杀最长等待", 600)):
                f(title, ("defaults", key), default, "number")
            f("日志保留天数", ("log_retention_days",), 30, "int")
            self._section("活动检测与热键", "后台任务忽略真实键鼠活动；手动暂停、跳过仍然有效。")
            f("真实输入检测", ("activity_pause", "enabled"), False, "bool")
            f("空闲后恢复（秒）", ("activity_pause", "idle_resume_seconds"), 300, "int")
            for key, title in (("pause_resume", "暂停 / 恢复"), ("prev_task", "上一个任务"), ("next_task", "下一个任务"), ("reset_progress", "进度清零")):
                from core.activity import DEFAULT_HOTKEYS
                f(title, ("activity_pause", "control_hotkeys", key), DEFAULT_HOTKEYS[key])
            self._section("结果通知")
            f("Server酱推送", ("notify", "enabled"), False, "bool")
            f("SENDKEY", ("notify", "sendkey"), secret=True)
        elif kind == "queue":
            self._section("队列设置")
            f("队列名称", ("name",))
            f("参与调度", ("enabled",), True, "bool")
            f("触发时间", ("times",), "06:00", "list")
            self._section("时间格式", "使用 24 小时制，多个时间用逗号分隔，例如 06:10, 14:10, 22:10。\n左侧选择任务进行配置；选择本队列后点击“＋ 任务”新增。")
        else:
            task = self.form_source
            task_type = task.get("type", "monitor")
            self._section("任务属性", f"任务类型：{TYPE_NAMES.get(task_type, task_type)}")
            f("任务名称", ("name",))
            f("参与执行", ("enabled",), True, "bool")
            f("前台 / 后台", ("execution_mode",), None, "mode")
            f("并行组名称", ("parallel",))
            if task_type in ("launch", "launch_wait"):
                f("启动程序", ("exe",), browse=True)
                f("工作目录", ("cwd",))
            self._section("运行参数", "空白表示使用默认值。启动参数、进程匹配与清理名单在“启动与进程”中编辑。")
            keys = {"launch": [("delay_after", "启动后等待")],
                    "launch_wait": [("timeout", "总超时")],
                    "retry_group": [("attempts", "最多尝试次数"), ("attempt_timeout", "单次超时"), ("appear_timeout", "等待启动时限")],
                    "monitor": [("timeout", "总超时"), ("appear_timeout", "等待启动时限")],
                    "sliding_window": [("window_size", "同时监控数量")]}.get(task_type, [])
            for key, title in keys + [("poll_interval", "巡检间隔"), ("stable_dead", "稳定退出时长")]:
                f(title, (key,), "", "int" if key in ("attempts", "window_size") else "number")
            if task_type in ("monitor", "retry_group"):
                self._section("游戏 1080p 窗口化", "新进程前三分钟巡检。启动参数与游戏进程匹配在“启动与进程”中编辑；异环需关闭参数重启。")
                f("需要 1080p 窗口化", ("resolution_check", "require_1080p"), False, "bool")
                f("允许启动参数重启（异环关闭）", ("resolution_check", "restart_with_args"), True, "bool")
                f("启用窗口巡检", ("resolution_check", "enabled"), True, "bool")
                f("巡检期限（最多180秒）", ("resolution_check", "inspect_seconds"), 180, "number")
                f("重启后窗口等待（秒）", ("resolution_check", "restart_grace_seconds"), 15, "number")
            if task_type == "monitor":
                self._section("参数补跑启动设置", "补跑固定最多两次：第一次只启动脚本，第二次游戏→脚本。也可在“启动与进程”中逐行编辑参数。")
                f("补跑脚本程序", ("recovery", "script", "exe"), browse=True)
                f("补跑工作目录", ("recovery", "script", "cwd"))
                f("补跑启动参数（JSON列表）", ("recovery", "script", "args"), [], "args")
                f("关联 Python 根目录", ("recovery", "python_paths"), "", "list")
            if task_type in ("retry_group", "monitor", "launch_wait"):
                self._section("模拟器 · ADB 连接", "先启动模拟器，再获取端口或自动连接；多开时选择实例。桥接与后台保活开关是兼容选项，请与 MuMu 内的设置保持一致。")
                f("ADB 程序路径", ("adb", "path"), browse=True)
                f("连接地址 IP:端口 / serial", ("adb", "serial"))
                f("运行时自动获取端口", ("adb", "auto_detect"), False, "bool")
                f("MuMu 安装目录", ("adb", "mumu_path"))
                f("MuMu 实例编号（桥接必填）", ("adb", "mumu_index"), "", "int")
                f("MuMu 后台保活兼容", ("adb", "mumu_keep_alive"), False, "bool")
                f("MuMu 网络桥接兼容", ("adb", "mumu_bridge"), False, "bool")
                f("命令超时（秒）", ("adb", "timeout"), 15, "number")
                buttons = ttk.Frame(self.form, style="Card.TFrame")
                buttons.grid(row=self.row, column=0, columnspan=3, sticky="w", pady=8)
                for label, action in (("获取当前模拟器端口", "discover"), ("自动连接", "connect"), ("检测前台 App", "test")):
                    ttk.Button(buttons, text=label, command=lambda a=action: self.test_adb(a)).pack(side="left", padx=(0, 8))
                self.row += 1
                self._section("安卓后台清理", "本项脚本稳定退出后暂不关闭 App。等下一项脚本与安卓 App 启动稳定，再清理本项遗留的后台程序；保护当前 App、桌面和系统应用。")
                f("启用延后后台清理", ("adb", "enabled"), False, "bool")
                f("本项启动稳定时长（秒）", ("adb", "startup_stable"), 10, "number")
                f("本项待清理包名（可选）", ("adb", "packages"), "", "list")
                ttk.Label(self.form, text="包名留空：清理运行中的其他第三方 App。填写包名：只清理指定 App（多个以逗号分隔）。启动稳定时长使用新启动任务的设置。", style="Hint.TLabel", wraplength=610).grid(row=self.row, column=0, columnspan=3, sticky="w", pady=8)
                self.row += 1
                ttk.Button(self.form, text="获取当前 App 包名并填写", command=lambda: self.test_adb("package")).grid(row=self.row, column=1, sticky="w", pady=8)
        self.structured.render(self.form_source, kind)

    def _structured_change(self, change):
        """Apply a list/dialog change together with pending edits in the common form."""
        try:
            value = self._current()
            change(value)
            self.form_source = value
            self._set_raw(value)
            self._render_form()
            self._scale_widgets(self.form)
            self.status.set("修改已暂存 · 点击“保存配置”写入文件。")
            return True
        except Exception as exc:
            messagebox.showerror("设置有误", str(exc), parent=self.root)
            return False

    def _from_form(self):
        result = deepcopy(self.form_source)
        for path, (var, kind, initial) in self.vars.items():
            value = var.get()
            if value == initial:
                continue
            if kind == "mode":
                value = MODES[value]
            elif kind in ("int", "number"):
                value = None if not value.strip() else int(value) if kind == "int" else float(value)
            elif kind == "args":
                value = json.loads(value)
                if not isinstance(value, list) or any(not isinstance(arg, str) for arg in value):
                    raise ValueError("启动参数必须为 JSON 字符串列表")
            elif kind == "list":
                value = [x.strip() for x in value.replace("，", ",").split(",") if x.strip()]
            node = result
            for key in path[:-1]:
                node = node.setdefault(key, {})
            if value is None or (value == "" and path[-1] in ("parallel", "cwd")):
                node.pop(path[-1], None)
            else:
                node[path[-1]] = value
        return result

    def _current(self):
        value = parse(self.raw.get("1.0", "end-1c")) if self.last_tab == 2 else self._from_form()
        if not isinstance(value, dict):
            raise ValueError("当前节点必须是 YAML 对象")
        if self.nodes[self.selected][0] != "global" and not value.get("name"):
            raise ValueError("名称不能为空")
        return value

    def _commit(self):
        if not self.selected:
            return
        value = self._current()
        kind, node, parent, index = self.nodes[self.selected]
        if kind == "global":
            value["queues"] = self.data.get("queues", [])
            node.clear()
            node.update(value)
        else:
            node.clear()
            node.update(value)
        self.form_source = deepcopy(value)

    def _select(self, _=None):
        if self.loading or not self.tree.selection():
            return
        selected = self.tree.selection()[0]
        if selected == self.selected:
            return
        try:
            self._commit()
        except Exception as exc:
            self.tree.selection_set(self.selected)
            messagebox.showerror("无法切换", str(exc), parent=self.root)
            return
        self._rebuild(selected)

    def _tab_changed(self, _=None):
        if self.loading or self.form_source is None:
            return
        current = self.tabs.index(self.tabs.select())
        if current == self.last_tab:
            return
        try:
            value = self._current()
            self.form_source = value
            if current == 2:
                self._set_raw(value)
            else:
                self._render_form()
                self._scale_widgets(self.form)
            self.last_tab = current
        except Exception as exc:
            self.tabs.select(self.last_tab)
            messagebox.showerror("内容有误", str(exc), parent=self.root)

    def validate(self):
        try:
            self._commit()
            validate_config(self.data)
            self.status.set("校验通过 · 配置结构、任务参数、热键、触发时间和 ADB 设置均有效。")
            self._rebuild(self.selected)
            return True
        except Exception as exc:
            messagebox.showerror("配置校验失败", str(exc), parent=self.root)
            return False

    def save(self):
        if not self.validate():
            return False
        try:
            self.document.save(self.data)
            self.status.set("保存成功 · 原文件已备份为 .bak；重启调度器后生效。")
            return True
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.root)
            return False

    def _can_leave(self):
        try:
            self._commit()
            changed = dump(self.data) != dump(self.document.data)
        except Exception:
            changed = True
        if not changed:
            return True
        answer = messagebox.askyesnocancel("未保存的更改", "是否保存当前修改？", parent=self.root)
        return self.save() if answer else answer is False

    def close(self):
        if self._can_leave():
            self.root.after_cancel(self.poll_job)
            self.root.destroy()

    def open_file(self):
        if not self._can_leave():
            return
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("YAML 配置", "*.yaml *.yml")])
        if path:
            try:
                document = ConfigDocument(path)
                self.document, self.data = document, deepcopy(document.data)
                self.path_label.configure(text=str(document.path))
                self._rebuild("global")
            except Exception as exc:
                messagebox.showerror("打开失败", str(exc), parent=self.root)

    def _edit(self, action):
        try:
            self._commit()
            selected = action()
            self._rebuild(selected or self.selected)
            self.status.set("修改已暂存 · 点击“保存配置”写入文件。")
        except Exception as exc:
            messagebox.showerror("操作失败", str(exc), parent=self.root)

    def add_queue(self):
        def action():
            queues = self.data.setdefault("queues", [])
            name = self._unique("新队列", queues)
            queues.append({"name": name, "enabled": True, "times": ["06:00"], "tasks": []})
            return f"q{len(queues)-1}"
        self._edit(action)

    @staticmethod
    def _unique(name, items):
        names = {t["name"] for t in items}
        result, count = name, 2
        while result in names:
            result, count = f"{name} {count}", count + 1
        return result

    def add_task(self):
        kind, node, parent, index = self.nodes[self.selected]
        if kind == "global":
            messagebox.showinfo("选择队列", "请先选择一个队列或监控组。", parent=self.root)
            return
        child = kind == "task" and node.get("type") == "sliding_window"
        if child:
            task_kind = "monitor"
        else:
            dialog = tk.Toplevel(self.root)
            dialog.title("新增任务")
            dialog.geometry(f"{360*self.scale}x{185*self.scale}")
            dialog.resizable(False, False)
            dialog.transient(self.root)
            ttk.Label(dialog, text="选择任务类型", padding=18).pack(anchor="w")
            choice = tk.StringVar(value="启动与重试")
            ttk.Combobox(dialog, textvariable=choice, values=list(TYPE_NAMES.values()), state="readonly").pack(fill="x", padx=20)
            result = []
            ttk.Button(dialog, text="创建任务", style="Accent.TButton", command=lambda: (result.append(choice.get()), dialog.destroy())).pack(pady=18)
            self._scale_widgets(dialog)
            dialog.grab_set()
            self.root.wait_window(dialog)
            if not result:
                return
            task_kind = next(k for k, v in TYPE_NAMES.items() if v == result[0])
        def action():
            kind, node, parent, index = self.nodes[self.selected]
            if child:
                values, prefix = node.setdefault("monitors", []), self.selected
            elif kind == "queue":
                values, prefix = node.setdefault("tasks", []), self.selected
            else:
                values, prefix = parent, self.selected.rsplit("/", 1)[0]
            if "/t" in prefix and not child:
                actual_kind = "monitor"
            else:
                actual_kind = task_kind
            values.append(task_template(actual_kind, self._unique("新任务", values)))
            return f"{prefix}/t{len(values)-1}"
        self._edit(action)

    def duplicate(self):
        def action():
            _, node, parent, index = self.nodes[self.selected]
            if parent is None:
                return
            item = deepcopy(node)
            item["name"] = self._unique(node["name"] + " 副本", parent)
            parent.insert(index + 1, item)
        self._edit(action)

    def delete(self):
        if self.selected == "global":
            return
        if not messagebox.askyesno("删除项目", "删除当前项目及其子任务？保存前不会改动文件。", parent=self.root):
            return
        def action():
            _, node, parent, index = self.nodes[self.selected]
            parent.pop(index)
            return self.selected.rsplit("/", 1)[0] if "/" in self.selected else "global"
        self._edit(action)

    def move(self, delta):
        def action():
            _, node, parent, index = self.nodes[self.selected]
            if parent is not None and 0 <= index + delta < len(parent):
                parent[index], parent[index + delta] = parent[index + delta], parent[index]
                return self.selected.rsplit("/t", 1)[0] + f"/t{index+delta}" if "/" in self.selected else f"q{index+delta}"
        self._edit(action)

    def test_adb(self, action="test"):
        try:
            snapshot = deepcopy(self._current())
            target = self.nodes[self.selected][1]
            cfg = deepcopy(self.data.get("defaults", {}).get("adb", {}))
            cfg.update(snapshot.get("adb") or {})
            adb.validate(cfg)
        except Exception as exc:
            messagebox.showerror("ADB 配置有误", str(exc), parent=self.root)
            return
        self.status.set("正在查询模拟器，请稍候…")
        def worker():
            try:
                client = adb.AdbClient(cfg)
                if action == "discover":
                    result = {"candidates": client.discover(), "path": client.path}
                else:
                    serial = client.auto_connect()
                    result = {"serial": serial, "path": client.path}
                    if action != "connect":
                        result["package"] = client.foreground()
                self.jobs.put(lambda: self._adb_result(action, target, snapshot, result))
            except Exception as exc:
                detail = str(exc)
                self.jobs.put(lambda: messagebox.showerror("ADB 查询失败", detail, parent=self.root))
        threading.Thread(target=worker, daemon=True).start()

    def _adb_result(self, action, target, snapshot, result):
        if action == "discover":
            candidates = result["candidates"]
            index = 0
            if len(candidates) > 1:
                labels = "\n".join(f"{i + 1}. {c.get('name', '')}  实例 {c.get('index', '—')}  {c['serial']}" for i, c in enumerate(candidates))
                choice = simpledialog.askinteger("选择模拟器", labels + "\n\n输入列表序号：", minvalue=1, maxvalue=len(candidates), parent=self.root)
                if choice is None:
                    return
                index = choice - 1
            result.update(candidates[index])
        detail = f"设备：{result['serial']}\nADB：{result['path']}"
        if "package" in result:
            detail += f"\n当前 App：{result['package']}"
        if action in ("discover", "connect", "package"):
            # A response must never overwrite another task or edits made while querying.
            try:
                unchanged = self.nodes[self.selected][1] is target and self._current() == snapshot
            except Exception:
                unchanged = False
            if unchanged:
                value = deepcopy(snapshot)
                cfg = value.setdefault("adb", {})
                cfg.update(serial=result["serial"], path=result["path"])
                if "index" in result:
                    cfg["mumu_index"] = result["index"]
                if action == "package":
                    cfg["packages"] = list(dict.fromkeys([*cfg.get("packages", []), result["package"]]))
                self.form_source = value
                self._set_raw(value)
                self._render_form()
                self._scale_widgets(self.form)
                detail += "\n\n已填写，请保存配置。"
            else:
                detail += "\n\n查询期间任务或配置有变动，未自动填写；请重新获取。"
        self.status.set("ADB 查询完成")
        messagebox.showinfo("ADB 查询", detail, parent=self.root)

    def _poll_jobs(self):
        system_scale = integer_scale(window_dpi(self.root))
        if system_scale != self.system_scale:
            self.system_scale = system_scale
            if not self.manual_zoom:
                self.set_scale(system_scale, manual=False)
        try:
            while True:
                self.jobs.get_nowait()()
        except queue.Empty:
            pass
        self.poll_job = self.root.after(120, self._poll_jobs)


def main():
    parser = argparse.ArgumentParser(description="任务调度器 GUI 配置编辑器")
    parser.add_argument("--config", default=ROOT / "config.yaml")
    args = parser.parse_args()
    enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    try:
        ConfigEditor(root, args.config)
    except Exception as exc:
        messagebox.showerror("无法打开配置", str(exc), parent=root)
        root.destroy()
        return
    root.deiconify()
    root.mainloop()


if __name__ == "__main__":
    main()
