# -*- coding: utf-8 -*-
"""
游戏自动化任务调度器
用法：
    python main.py                    # 常驻，按 config.yaml 中的时间定时执行
    python main.py --run 队列1        # 先手动执行一次，再继续常驻定时调度
    python main.py --config other.yaml
"""
import argparse
import logging
import os
import sys
import time

import yaml

from core import activity, scheduler
from core.configuration import validate_config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def setup_logging():
    # Windows 控制台默认是 GBK，bat 里又 chcp 65001 切到了 UTF-8，
    # 两边不一致会导致中文乱码。这里把标准输出/错误统一强制为 UTF-8，
    # 配合 bat 的 chcp 65001 使用。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)
    fh = logging.FileHandler(
        os.path.join(BASE_DIR, "logs",
                     time.strftime("run_%Y%m%d.log")),
        encoding="utf-8-sig")   # 带 BOM 的 UTF-8，记事本/Excel 直接打开不乱码
    fh.setFormatter(fmt)
    root.addHandler(fh)


def acquire_single_instance_lock():
    """
    单实例锁：防止调度器被同时启动两次（手动 --run 与常驻调度并存、
    误双击 bat 等），否则 AUTO-MAS 会被重复拉起、监控计数互相干扰。
    返回文件句柄（需活到进程结束）；抢锁失败返回 None。
    """
    lock_path = os.path.join(BASE_DIR, "logs", "scheduler.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    f = open(lock_path, "a+")
    try:
        f.seek(0)
        f.write(" ")
        f.flush()
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(BASE_DIR, "config.yaml"))
    ap.add_argument("--run", metavar="队列名", help="先执行指定队列，完成后继续定时调度；期间到期的任务延后补跑")
    ap.add_argument("--check", action="store_true", help="仅校验配置，不启动任务或输入钩子")
    args = ap.parse_args()

    setup_logging()
    with open(args.config, encoding="utf-8-sig") as f:
        config = yaml.safe_load(f)
    validate_config(config)
    if args.check:
        logging.info("配置校验通过")
        return
    _lock = acquire_single_instance_lock()   # 句柄需存活到进程结束，勿删
    if _lock is None:
        logging.error("已有一个调度器实例在运行，本次启动退出。"
                      "如确认没有实例在跑，删除 logs\\scheduler.lock 后重试。")
        sys.exit(2)
    config["_config_path"] = os.path.abspath(args.config)  # 供 notify 查找 .env
    initial_queue = None
    if args.run:
        initial_queue = next((q for q in config.get("queues", []) if q.get("name") == args.run), None)
        if initial_queue is None:
            logging.error("未找到队列: %s", args.run)
            sys.exit(1)
    monitor = activity.init(config.get("activity_pause") or {})
    try:
        scheduler.run_forever(config, initial_queue=initial_queue)
    finally:
        monitor.stop()
        _lock.close()


if __name__ == "__main__":
    main()
