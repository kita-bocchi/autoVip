#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 USB adb 切换成无线 adb，并写进 config.json

前置：手机先用能传数据的 USB 线连着，`adb devices` 显示 device
用法：
    .venv\\Scripts\\python.exe wifi_adb.py
    .venv\\Scripts\\python.exe wifi_adb.py --port 5555 --no-boot-script

做完就可以把线拔了，之后电脑用 Wi-Fi 连手机。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from adb_env import adb, adb_devices, setup_adb  # noqa: E402

BASE = Path(__file__).resolve().parent
CFG_FILE = BASE / "config.json"


def log(msg: str) -> None:
    print(msg, flush=True)


def pick_usb_serial(explicit: str | None) -> str | None:
    devs = [(s, st) for s, st in adb_devices() if st == "device" and ":" not in s]
    if explicit:
        return explicit if any(s == explicit for s, _ in devs) else None
    return devs[0][0] if devs else None


def phone_ip(serial: str) -> str:
    out = adb("-s", serial, "shell", "ip route")
    for line in out.splitlines():
        m = re.search(r"src\s+(\d+\.\d+\.\d+\.\d+)", line)
        if m and not m.group(1).startswith("127."):
            return m.group(1)
    out = adb("-s", serial, "shell", "ip -4 addr show wlan0")
    m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else ""


def install_boot_script(serial: str, port: int) -> bool:
    """写 Magisk 开机脚本，重启后自动回到无线 adb"""
    content = ("#!/system/bin/sh\n"
               "setprop service.adb.tcp.port {}\n"
               "stop adbd\n"
               "start adbd\n").format(port)
    tmp = BASE / "_wifi_adb.sh"
    tmp.write_text(content, encoding="utf-8", newline="\n")
    targets = ["/data/adb/service.d/99-wifi-adb.sh"]
    ok = False
    for target in targets:
        adb("-s", serial, "push", str(tmp), "/data/local/tmp/99-wifi-adb.sh")
        adb("-s", serial, "shell",
            "su -c 'mkdir -p /data/adb/service.d && cp /data/local/tmp/99-wifi-adb.sh "
            "{} && chmod 755 {}'".format(target, target))
        check = adb("-s", serial, "shell", "su -c 'ls -l {}'".format(target))
        if "99-wifi-adb.sh" in check:
            ok = True
            log("  已写入开机脚本: {}".format(target))
            break
    try:
        tmp.unlink()
    except Exception:
        pass
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5555)
    ap.add_argument("--usb-serial", default=None)
    ap.add_argument("--no-boot-script", action="store_true")
    args = ap.parse_args()

    if not setup_adb():
        log("找不到 adb，检查 tools/platform-tools")
        return 1

    serial = pick_usb_serial(args.usb_serial)
    if not serial:
        log("没有检测到 USB 连接的设备。")
        log("无线 adb 必须先插一次线完成握手：")
        log("  1) 用能传数据的 USB 线连电脑")
        log("  2) 手机点「允许 USB 调试」")
        log("  3) 再跑这条命令")
        return 2
    log("USB 设备: {}".format(serial))

    ip = phone_ip(serial)
    if not ip:
        log("读不到手机 Wi-Fi IP —— 手机没连 Wi-Fi？或 Wi-Fi 没分配地址。")
        log("请先让手机连上同一个局域网，再重试。")
        return 3
    log("手机 IP: {}".format(ip))

    log("切换 adbd 到 tcp:{} ...".format(args.port))
    adb("-s", serial, "tcpip", str(args.port))
    time.sleep(2.5)

    target = "{}:{}".format(ip, args.port)
    connected = False
    for attempt in range(1, 6):
        out = adb("connect", target)
        if "connected" in out:
            connected = True
            log("  第 {} 次连接: {}".format(attempt, out.strip().splitlines()[0]))
            break
        log("  第 {} 次连接失败: {}".format(attempt, out.strip()))
        time.sleep(2.0)
        if attempt == 2:
            # adbd 可能还没就绪，再触发一次
            adb("-s", serial, "tcpip", str(args.port))

    devs = dict(adb_devices())
    if devs.get(target) != "device":
        log("无线连接未建立，当前设备列表: {}".format(devs))
        log("排查: 手机和电脑是否同一网段 / 路由器是否开了 AP 隔离")
        return 4

    log("无线 adb 已就绪: {}".format(target))

    if not args.no_boot_script:
        log("安装开机自动启用无线 adb 的脚本 ...")
        if install_boot_script(serial, args.port):
            log("  重启后仍可用（依赖 Magisk service.d）")
        else:
            log("  开机脚本写入失败，重启后需要重新插线跑一次本脚本")

    try:
        cfg = json.loads(CFG_FILE.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    cfg["serial"] = target
    CFG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    log("已写入 config.json: serial = {}".format(target))

    log("")
    log("现在可以把 USB 线拔掉了。验证: .venv\\Scripts\\python.exe doctor.py")
    log("注意: 手机 IP 由路由器 DHCP 分配会变，建议在路由器里给手机绑定固定 IP，")
    log("      否则换 IP 后脚本连不上，需要重跑本脚本。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
