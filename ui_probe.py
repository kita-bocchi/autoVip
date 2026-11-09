#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""界面探测器：把手机当前界面的所有文字节点打出来，用于配置 config.json

用法：
    python ui_probe.py                 # 打印一次，并保存 xml+png
    python ui_probe.py --watch 3       # 每 3 秒刷新，只输出变化的文本（手动点一遍流程时用）
    python ui_probe.py --tap 540 1200  # 按屏幕坐标点一下，验证坐标是否准确
    python ui_probe.py --apps          # 查看当前前台应用包名/Activity
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT_DIR = BASE / "probe"

sys.path.insert(0, str(BASE))


def connect():
    from adb_env import adb_devices, set_serial, setup_adb

    if not setup_adb():
        raise SystemExit("找不到 adb，请确认 tools/platform-tools/adb.exe 存在")
    devs = [(s, st) for s, st in adb_devices() if st == "device"]
    if not devs:
        raise SystemExit("设备未就绪，先跑: .venv\\Scripts\\python.exe doctor.py")

    set_serial(devs[0][0])
    import uiautomator2 as u2
    return u2.connect(devs[0][0])


def snapshot(d, save: bool = True) -> list[str]:
    xml = d.dump_hierarchy()
    lines: list[str] = []
    try:
        root = ET.fromstring(xml)
    except Exception as exc:
        return ["XML 解析失败: {}".format(exc)]

    for node in root.iter("node"):
        text = (node.get("text") or "").strip()
        desc = (node.get("content-desc") or "").strip()
        if not text and not desc:
            continue
        cls = (node.get("class") or "").split(".")[-1]
        bounds = node.get("bounds") or ""
        clickable = "可点" if node.get("clickable") == "true" else "  "
        rid = node.get("resource-id") or ""
        rid = rid.split("/")[-1] if rid else ""
        lines.append("[{}] {:<10} {:<22} {} {!r} desc={!r} id={}".format(
            clickable, cls, bounds, "", text, desc, rid))

    if save:
        OUT_DIR.mkdir(exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        (OUT_DIR / "{}.xml".format(stamp)).write_text(xml, encoding="utf-8")
        try:
            d.screenshot(str(OUT_DIR / "{}.png".format(stamp)))
        except Exception:
            pass
    return lines


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=float, default=0, help="轮询间隔秒数")
    ap.add_argument("--tap", nargs=2, type=int, metavar=("X", "Y"))
    ap.add_argument("--apps", action="store_true", help="仅打印前台应用")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()

    d = connect()
    cur = d.app_current()
    print("前台: {} / {}".format(cur.get("package"), cur.get("activity")))
    print("屏幕: {}".format(d.window_size()))
    if args.apps:
        return

    if args.tap:
        x, y = args.tap
        from adb_env import has_root, shell, shell_root
        cmd = "input tap {} {}".format(x, y)
        out = shell_root(cmd) if has_root() else shell(cmd)
        print("点击 ({}, {}) -> {}".format(x, y, "root" if has_root() else "shell"))
        if out.strip():
            print(out.strip()[:300])
        return

    if not args.watch:
        for line in snapshot(d, save=not args.no_save):
            print(line)
        return

    print("监听中，Ctrl+C 结束。现在手动操作手机走一遍流程。")
    last = None
    try:
        while True:
            lines = snapshot(d, save=False)
            cur_text = "\n".join(lines)
            if cur_text != last:
                cur = d.app_current()
                print("\n{} --- {} / {}".format(
                    dt.datetime.now().strftime("%H:%M:%S"),
                    cur.get("package"), cur.get("activity")))
                for line in lines:
                    print("  " + line)
                last = cur_text
            import time
            time.sleep(args.watch)
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
