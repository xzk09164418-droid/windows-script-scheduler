"""Loopback-only configuration API. The Node front end is a separate process."""
import argparse
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import threading

from core.configuration import ConfigDocument, dump, parse, validate_config
from config_editor_schema import (at_path, put_path, LAUNCH_FIELDS, MATCH_FIELDS,
                                  RESOLUTION_FIELDS, RESOLUTION_ARGS, WW_FIELDS)

ROOT = Path(__file__).resolve().parent


class EditConflict(ValueError):
    pass


def apply_operations(data, operations):
    """Replay precise edits on round-trip YAML rather than reserializing browser JSON."""
    if not isinstance(operations, list) or len(operations) > 5000:
        raise ValueError("修改列表无效或过长，请保存后继续编辑")
    result = deepcopy(data)
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError("修改项必须是对象")
        path = operation.get("path")
        if not isinstance(path, list) or len(path) > 32 or any(type(p) not in (str, int) or isinstance(p, int) and p < 0 for p in path):
            raise ValueError("修改路径无效")
        action = operation.get("op")
        if action == "set" and path:
            put_path(result, path, parse(dump(operation.get("value"))))
        elif action == "delete" and path:
            parent = at_path(result, path[:-1])
            if isinstance(parent, list):
                parent.pop(path[-1])
            else:
                parent.pop(path[-1], None)
        elif action == "append" and path:
            try:
                items = at_path(result, path)
            except KeyError:
                put_path(result, path, [])
                items = at_path(result, path)
            if items is None:
                put_path(result, path, [])
                items = at_path(result, path)
            if not isinstance(items, list):
                raise ValueError("新增目标必须是列表")
            items.append(parse(dump(operation.get("value"))))
        elif action == "move" and path:
            items = at_path(result, path)
            source, target = operation.get("from"), operation.get("to")
            if not isinstance(items, list) or type(source) is not int or type(target) is not int or not 0 <= source < len(items) or not 0 <= target < len(items):
                raise ValueError("移动位置无效")
            item = items.pop(source)
            items.insert(target, item)
        elif action == "yaml":
            text = operation.get("text")
            if not isinstance(text, str):
                raise ValueError("YAML 内容必须是文本")
            value = parse(text)
            if not isinstance(value, dict):
                raise ValueError("当前节点必须是 YAML 对象")
            if path:
                put_path(result, path, value)
            else:
                value["queues"] = result.get("queues", [])
                result = value
        else:
            raise ValueError("不支持的修改操作")
    return result


def json_value(data):
    # Reject YAML-only types with a useful error instead of a traceback in the UI.
    try:
        return json.loads(json.dumps(data, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("配置包含浏览器不支持的 YAML 类型，请使用桌面版高级 YAML 编辑") from exc


class ConfigStore:
    def __init__(self, path):
        self.document = ConfigDocument(path)
        self.lock = threading.RLock()

    @property
    def revision(self):
        return sha256(self.document.original).hexdigest()

    def snapshot(self, reload=False):
        with self.lock:
            if reload:
                self.document = ConfigDocument(self.document.path)
            return {"data": json_value(self.document.data), "revision": self.revision,
                    "path": str(self.document.path), "schemas": {
                        "launch": [asdict(f) for f in LAUNCH_FIELDS],
                        "matcher": [asdict(f) for f in MATCH_FIELDS],
                        "resolution": [asdict(f) for f in RESOLUTION_FIELDS],
                        "resolution_args": [asdict(f) for f in RESOLUTION_ARGS],
                        "ww": [asdict(f) for f in WW_FIELDS],
                    }}

    def candidate(self, body):
        if body.get("revision") != self.revision or self.document.path.read_bytes() != self.document.original:
            raise EditConflict("配置已被其他窗口或程序修改，请重新载入后再编辑；当前修改尚未保存")
        return apply_operations(self.document.data, body.get("operations", []))

    def process(self, action, body):
        with self.lock:
            value = self.candidate(body)
            if action == "node":
                path = body.get("path", [])
                node = at_path(value, path)
                if not path:
                    node = deepcopy(node)
                    node.pop("queues", None)
                return {"text": dump(node)}
            if action == "preview":
                if any(op.get("op") == "yaml" for op in body.get("operations", [])):
                    validate_config(value)
                return {"data": json_value(value)}
            validate_config(value)
            if action == "validate":
                return {"message": "校验通过：配置结构、任务参数和触发时间有效。"}
            if action != "save":
                raise ValueError("未知操作")
            self.document.save(value)
            return {**self.snapshot(), "message": "保存成功，原文件已备份为 .bak；重启调度器后生效。"}


def make_server(path, port=8765, token=None):
    store = ConfigStore(path)
    token = token or secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Do not log YAML contents, local paths or credentials.
            pass

        def reply(self, status, data):
            payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def authorized(self):
            supplied = self.headers.get("Authorization", "")
            if not secrets.compare_digest(supplied, "Bearer " + token):
                self.reply(403, {"error": "请通过本地前端访问配置服务"})
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            if self.path != "/api/config":
                self.reply(404, {"error": "接口不存在"})
                return
            try:
                self.reply(200, store.snapshot(reload=True))
            except (ValueError, OSError) as exc:
                self.reply(400, {"error": str(exc)})

        def do_POST(self):
            if not self.authorized():
                return
            action = self.path.removeprefix("/api/")
            if action not in ("save", "validate", "node", "preview"):
                self.reply(404, {"error": "接口不存在"})
                return
            try:
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("请求必须使用 JSON")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 5*1024*1024:
                    raise ValueError("请求内容为空或超过 5 MB")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("请求必须是对象")
                self.reply(200, store.process(action, body))
            except EditConflict as exc:
                self.reply(409, {"error": str(exc)})
            except Exception as exc:
                self.reply(400, {"error": str(exc)})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.store = store
    server.token = token
    return server


def main():
    parser = argparse.ArgumentParser(description="浏览器配置编辑器 · Python 后端")
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    token = os.environ.get("CONFIG_EDITOR_TOKEN")
    if not token:
        parser.error("请设置 CONFIG_EDITOR_TOKEN，或使用 config_editor_web.py 一键启动")
    server = make_server(args.config, args.port, token)
    print(f"配置服务：http://127.0.0.1:{server.server_port}（Ctrl+C 退出）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
