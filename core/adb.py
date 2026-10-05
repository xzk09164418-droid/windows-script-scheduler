"""Bounded ADB operations, explicitly scoped to one emulator."""
import logging
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading

log = logging.getLogger("adb")
ROOT = Path(__file__).resolve().parents[1]
PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+\Z")
COMPONENT = r"([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)/[A-Za-z0-9_.$]+"


class AdbError(RuntimeError):
    pass


def validate(cfg):
    if not isinstance(cfg, dict):
        raise ValueError("adb 必须是配置对象")
    for key in ("enabled", "auto_detect", "mumu_keep_alive", "mumu_bridge"):
        if not isinstance(cfg.get(key, False), bool):
            raise ValueError(f"adb.{key} 必须为 true/false")
    for key in ("path", "serial", "mumu_path"):
        if not isinstance(cfg.get(key, ""), str):
            raise ValueError(f"adb.{key} 必须是字符串")
    if not 0 < float(cfg.get("timeout", 15)) <= 120:
        raise ValueError("adb.timeout 必须在 0~120 秒之间")
    if not 1 <= float(cfg.get("startup_stable", 10)) <= 600:
        raise ValueError("adb.startup_stable 必须在 1~600 秒之间")
    index = cfg.get("mumu_index")
    if index is not None and (isinstance(index, bool) or not isinstance(index, int) or index < 0):
        raise ValueError("adb.mumu_index 必须是非负整数或留空")
    if cfg.get("mumu_bridge") and index is None:
        raise ValueError("MuMu 桥接模式必须指定实例编号 mumu_index")
    packages = cfg.get("packages", [])
    if not isinstance(packages, list) or any(not isinstance(p, str) or not PACKAGE.fullmatch(p) for p in packages):
        raise ValueError("adb.packages 必须是合法 Android 包名列表")


def find_adb(configured=""):
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        if not path.is_file():
            raise AdbError(f"找不到 ADB：{path}")
        return str(path)
    candidates = [ROOT / "tools/platform-tools/adb.exe"]
    base = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Netease/MuMu"
    candidates += [base / "nx_main/adb.exe"]
    candidates += sorted(base.glob("nx_device/*/shell/adb.exe"), reverse=True)
    for path in candidates:
        if path.is_file():
            return str(path)
    found = shutil.which("adb")
    if found:
        return found
    raise AdbError("未找到 ADB，请在配置编辑器中选择 adb.exe 或安装 Android Platform Tools")


def foreground_package(output):
    # Only use explicit current/resumed fields; never match historical task entries.
    for marker in ("mCurrentFocus", "mFocusedApp", "topResumedActivity", "mResumedActivity"):
        matches = re.findall(r"(?m)^.*" + marker + r"[^\r\n]*?" + COMPONENT, output)
        if matches:
            unique = set(matches)
            if len(unique) != 1:
                raise AdbError("模拟器存在多个前台窗口，无法确定目标 App")
            return matches[0]
    return None


def focused_display_package(output):
    """MuMu 保活会让多个 Display 同时 resumed，只读取实际获焦 Display。"""
    focus = re.search(r"mTopFocusedDisplayId\s*[=:]\s*(\d+)", output)
    if not focus:
        raise AdbError("保活模式未找到获焦 Display，无法安全确定当前 App")
    blocks = re.split(r"(?m)^\s*Display:\s*mDisplayId=(\d+)\b", output)
    for i in range(1, len(blocks), 2):
        if blocks[i] == focus[1]:
            display = re.split(r"(?m)^WINDOW MANAGER\b", blocks[i + 1], maxsplit=1)[0]
            package = foreground_package(display)
            if package:
                return package
    raise AdbError("保活模式无法解析获焦 Display 的 App；请检查 MuMu 版本和窗口焦点")


def endpoint(host, port):
    try:
        ip = ipaddress.ip_address(host)
        port = int(port)
        if not 1 <= port <= 65535 or ip.is_unspecified or ip.is_multicast:
            raise ValueError()
    except (ValueError, TypeError):
        raise AdbError("模拟器返回了无效的 ADB 地址/端口") from None
    return f"[{ip}]:{port}" if ip.version == 6 else f"{ip}:{port}"


class CleanupSession:
    """仅在同一队列内交接；并行任务不进入自动清理。"""
    def __init__(self):
        self.pending = {}
        self.lock = threading.Lock()

    def completed(self, client, package):
        with self.lock:
            self.pending[client.serial] = (dict(client.cfg), package)

    def previous(self, serial):
        with self.lock:
            return self.pending.get(serial)

    def clean(self, client, package, check_interrupt):
        with self.lock:
            previous = self.pending.get(client.serial)
            if previous is None:
                return []
            closed = client.clean_background(package, previous[0].get("packages", []), check_interrupt)
            del self.pending[client.serial]
            return closed


class AdbClient:
    def __init__(self, cfg):
        validate(cfg)
        self.cfg = cfg
        self.path = find_adb(cfg.get("path", ""))
        self.serial = cfg.get("serial", "").strip()
        self.timeout = float(cfg.get("timeout", 15))

    def manager_info(self):
        root = Path(self.cfg.get("mumu_path") or Path(self.path).parent)
        candidates = [root / "MuMuManager.exe", root / "nx_main/MuMuManager.exe",
                      root / "shell/MuMuManager.exe"]
        manager = next((p for p in candidates if p.is_file()), None)
        if manager is None:
            if self.cfg.get("mumu_bridge") or self.cfg.get("mumu_index") is not None:
                raise AdbError("找不到 MuMuManager.exe，请填写 MuMu 安装目录")
            return []
        try:
            result = subprocess.run([str(manager), "info", "-v", "all"], capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=self.timeout,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            data = json.loads(result.stdout)
            if result.returncode or not isinstance(data, dict):
                raise ValueError("管理工具查询失败")
            return [dict(v, index=int(v.get("index", k))) for k, v in data.items()
                    if isinstance(v, dict) and str(v.get("index", k)).isdigit()]
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise AdbError(f"MuMu 实例查询失败：{exc}") from exc

    def discover(self):
        """读取真实端口；不扫描局域网，不启动已关闭的模拟器。"""
        infos = self.manager_info()
        index = self.cfg.get("mumu_index")
        candidates = []
        for info in infos:
            if index is not None and info["index"] != index:
                continue
            if info.get("is_android_started") is not True or not info.get("adb_port"):
                continue
            host = info.get("adb_host_ip") or info.get("adb_host")
            if self.cfg.get("mumu_bridge") and not host:
                raise AdbError("MuMu 未报告桥接 ADB 地址；请从模拟器填写实际 IP:端口后点击自动连接")
            serial = endpoint(host or "127.0.0.1", info["adb_port"])
            candidates.append({"serial": serial, "index": info["index"], "name": info.get("name", "MuMu")})
        if index is not None and not candidates:
            raise AdbError(f"MuMu 实例 {index} 尚未启动安卓或未提供 ADB 端口")
        if not candidates:
            output = self.command("devices", device=False)
            candidates = [{"serial": line.split()[0], "name": "在线模拟器"}
                          for line in output.splitlines() if len(line.split()) >= 2
                          and line.split()[1] == "device" and re.fullmatch(
                              r"emulator-\d+|(?:127\.0\.0\.1|localhost|\[::1\]):\d+", line.split()[0])]
        if not candidates:
            raise AdbError("没有运行中的模拟器，请先启动模拟器并启用 ADB")
        return candidates

    def auto_connect(self):
        if not self.serial:
            candidates = self.discover()
            if len(candidates) != 1:
                raise AdbError("检测到多个模拟器，请先获取端口并选择目标实例")
            self.serial = candidates[0]["serial"]
        return self.select_device()

    def command(self, *args, device=True):
        cmd = [self.path]
        if device:
            if not self.serial:
                raise AdbError("必须先选择模拟器设备")
            cmd += ["-s", self.serial]
        cmd.extend(args)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=self.timeout,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AdbError(f"ADB 命令失败：{exc}") from exc
        if result.returncode or re.search(r"(?im)^\s*(error:|exception|java\..*exception)", result.stdout):
            raise AdbError((result.stderr or result.stdout).strip() or "ADB 命令失败")
        return result.stdout

    def select_device(self):
        if not self.serial and self.cfg.get("auto_detect"):
            return self.auto_connect()
        if self.serial and re.fullmatch(r"(?:[A-Za-z0-9.-]+|\[[0-9a-fA-F:]+\]):\d+", self.serial):
            host = self.serial.rsplit(":", 1)[0].strip("[]")
            if host not in ("127.0.0.1", "localhost", "::1"):
                if not self.cfg.get("mumu_bridge"):
                    raise AdbError("非本机 ADB 地址需要开启 MuMu 网络桥接兼容开关")
                endpoint(host, self.serial.rsplit(":", 1)[1])
            self.command("connect", self.serial, device=False)
        output = self.command("devices", device=False)
        ready = [line.split()[0] for line in output.splitlines()
                 if len(line.split()) >= 2 and line.split()[1] == "device"]
        if self.serial:
            if self.serial not in ready:
                raise AdbError(f"模拟器 {self.serial} 离线或未授权")
        else:
            emulators = [s for s in ready if re.fullmatch(r"emulator-\d+|(?:127\.0\.0\.1|localhost|\[::1\]):\d+", s)]
            if len(emulators) != 1:
                raise AdbError("未找到唯一在线模拟器，请填写 adb.serial（多开必须分别指定）")
            self.serial = emulators[0]
        return self.serial

    def foreground(self):
        if self.cfg.get("mumu_keep_alive"):
            # AOSP prints mTopFocusedDisplayId in windows, not in displays-only output.
            return focused_display_package(self.command("shell", "dumpsys", "window"))
        package = foreground_package(self.command("shell", "dumpsys", "window", "windows"))
        if not package:
            package = foreground_package(self.command("shell", "dumpsys", "activity", "activities"))
        if not package:
            raise AdbError("无法识别模拟器当前前台 App")
        return package

    def protected_packages(self):
        homes = self.command("shell", "cmd", "package", "resolve-activity", "--brief",
                             "-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME")
        system = self.command("shell", "pm", "list", "packages", "-s")
        systems = set(re.findall(r"(?m)^package:([^\s]+)", system))
        launchers = set(re.findall(COMPONENT, homes))
        if not systems or not launchers:
            raise AdbError("无法确认系统应用/桌面名单，跳过后台清理")
        return (systems | launchers
                | {"com.android.systemui", "com.android.settings", "android"})

    def clean_background(self, current, packages, check_interrupt=lambda: None):
        """清理旧第三方进程，包括保活 Display；每次强停前复核当前 App。"""
        check_interrupt()
        protected = self.protected_packages()
        if current in protected:
            raise AdbError("当前 App 为系统应用，暂不清理")
        protected.add(current)
        third_party = set(re.findall(r"(?m)^package:([^\s]+)",
                                    self.command("shell", "pm", "list", "packages", "-3")))
        # ps includes :service processes. Match exact package boundaries, never substrings.
        processes = self.command("shell", "ps", "-A", "-o", "NAME")
        running = {line.strip().split(":", 1)[0] for line in processes.splitlines()}
        targets = (set(packages) if packages else third_party) & third_party & running - protected
        closed = []
        for package in sorted(targets):
            if not PACKAGE.fullmatch(package):
                continue
            check_interrupt()
            if self.foreground() != current:
                raise AdbError("前台 App 发生变化，已停止本轮后台清理")
            check_interrupt()
            self.command("shell", "am", "force-stop", "--user", "current", package)
            closed.append(package)
            log.info("[%s] 下一项 %s 已稳定，清理后台 App：%s", self.serial, current, package)
        return closed
