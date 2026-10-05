"""Launch independent Node front end and Python API, then open the user's browser."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import threading
import webbrowser

from config_editor_server import make_server, ROOT


def main():
    parser = argparse.ArgumentParser(description="浏览器配置编辑器 · 一键启动")
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--port", type=int, default=5173, help="前端端口")
    parser.add_argument("--api-port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    node = shutil.which("node")
    if not node:
        parser.error("未找到 Node.js，请安装后重新打开启动器")
    try:
        server = make_server(args.config, args.api_port)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    env = dict(os.environ, CONFIG_EDITOR_TOKEN=server.token,
               CONFIG_EDITOR_BACKEND=f"http://127.0.0.1:{server.server_port}",
               CONFIG_EDITOR_FRONTEND_PORT=str(args.port))
    frontend = None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("浏览器配置工作台 · Ctrl+C 或关闭此窗口退出")
    print(f"配置：{server.store.document.path}")
    try:
        frontend = subprocess.Popen([node, str(ROOT / "web_editor" / "server.mjs")], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        for line in frontend.stdout:
            print(line.rstrip(), flush=True)
            if line.startswith("READY ") and not args.no_browser:
                webbrowser.open(line.strip().removeprefix("READY "))
        frontend.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if frontend and frontend.poll() is None:
            frontend.terminate()
            try:
                frontend.wait(timeout=5)
            except subprocess.TimeoutExpired:
                frontend.kill()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
