#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""环境自检：一条命令告诉你卡在哪一步、下一步做什么

    python doctor.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from adb_env import adb, adb_devices, adb_version, ensure_online, setup_adb

BASE = Path(__file__).resolve().parent
OK = "[OK]  "
BAD = "[FAIL]"
WARN = "[WARN]"

results: list[tuple[str, str, str]] = []  # (level, title, hint)


def check(title: str, level: str, hint: str = "") -> None:
    results.append((level, title, hint))
    print("{} {}".format(level, title))
    if hint and level != OK:
        for line in hint.splitlines():
            print("        " + line)


def main() -> int:
    print("=" * 64)
    print("小米钱包自动化 - 环境自检")
    print("=" * 64)

    # 1. Python 与依赖
    print("\n[1/7] Python 环境")
    check("Python {}".format(sys.version.split()[0]), OK)
    try:
        from importlib.metadata import version as _ver
        import uiautomator2 as u2
        check("uiautomator2 {}".format(_ver("uiautomator2")), OK)
    except Exception as exc:
        check("uiautomator2 未安装 ({})".format(exc), BAD,
              "在该目录执行:\n"
              "  .venv\\Scripts\\python.exe -m pip install -r requirements.txt")
        return summary()

    if sys.executable.lower().endswith("python.exe") and ".venv" not in sys.executable:
        check("当前解释器不是项目虚拟环境", WARN,
              "建议用 .venv\\Scripts\\python.exe 运行，避免依赖装到别的 Python 上")

    # 2. adb
    print("\n[2/7] adb")
    exe = setup_adb()
    if not exe:
        check("找不到 adb", BAD,
              "重新解压 platform-tools 到 {} 或安装到 PATH".format(BASE / "tools"))
        return summary()
    check("adb: {}".format(adb_version()), OK)
    print("        {}".format(exe))

    # 3. 设备连接
    print("\n[3/7] 设备连接")
    target = ""
    try:
        target = (json.loads((BASE / "config.json").read_text(encoding="utf-8")) or {}).get(
            "serial", "")
    except Exception:
        pass
    if target and ":" in str(target):
        print("        config 指向无线设备: {}".format(target))
        ensure_online(target)
    devs = adb_devices()
    if not devs:
        check("没有检测到任何设备", BAD,
              "1) 换一根能传数据的 USB 线，别用纯充电线\n"
              "2) 手机: 设置 - 更多设置 - 开发者选项 - 打开「USB 调试」\n"
              "3) 手机弹出「允许 USB 调试吗」时勾选「一律允许」并确定\n"
              "4) 还不行就执行: {} kill-server 然后重插".format(exe))
        return summary()

    serial, state = devs[0]
    if state == "unauthorized":
        check("设备未授权 ({})".format(serial), BAD,
              "解锁手机，在弹窗里点「允许」；看不到弹窗就拔插数据线或撤销 USB 调试授权后重试")
        return summary()
    if state == "offline":
        check("设备 offline ({})".format(serial), BAD,
              "执行: {} kill-server，然后重插数据线".format(exe))
        return summary()
    check("设备在线: {}".format(serial), OK)

    # 4. 设备信息
    print("\n[4/7] 设备信息")
    model = adb("-s", serial, "shell", "getprop", "ro.product.model").strip()
    brand = adb("-s", serial, "shell", "getprop", "ro.product.brand").strip()
    ver = adb("-s", serial, "shell", "getprop", "ro.build.version.release").strip()
    sdk = adb("-s", serial, "shell", "getprop", "ro.build.version.sdk").strip()
    print("        {} / {} / Android {} (API {})".format(brand, model, ver, sdk))
    check("设备可读", OK)
    root = adb("-s", serial, "shell", "which", "su").strip()
    check("root: {}".format("可用" if root else "未检测到 su"),
          OK if root else WARN,
          "若无 root 但不影响本流程，只看红包/奖励是否到账")

    # 5. 小米钱包
    print("\n[5/7] 小米钱包")
    pkgs = adb("-s", serial, "shell", "pm", "list", "packages")
    wallet = [p.split(":")[-1].strip() for p in pkgs.splitlines()
              if "mipay" in p or "wallet" in p.lower()]
    if wallet:
        check("已安装: {}".format(", ".join(wallet)), OK)
        if "com.mipay.wallet" not in wallet:
            check("config.json 里 package 需要改成上面列出的包名", WARN)
    else:
        check("未找到小米钱包", BAD, "确认手机上装了小米钱包并已登录")

    # 6. uiautomator2 连通
    print("\n[6/7] uiautomator2 连通性")
    try:
        d = u2.connect(serial)
        info = d.info or {}
        check("已连接 {}x{}".format(info.get("displayWidth"), info.get("displayHeight")), OK)
    except Exception as exc:
        check("uiautomator2 连接失败", BAD,
              "{}\n首次连接会自动往手机装 atx-agent，需要打开「USB 安装」".format(exc))
        return summary()

    # 7. 解锁与截图
    print("\n[7/7] 唤醒与截图")
    try:
        d.screen_on()
    except Exception:
        pass
    try:
        if not (d.info or {}).get("screenOn", True):
            d.wakeup()
        d.unlock()
        import time
        time.sleep(1.2)
        shot = BASE / "shots" / "doctor.png"
        shot.parent.mkdir(exist_ok=True)
        d.screenshot(str(shot))
        on = (d.info or {}).get("screenOn")
        check("屏幕已点亮 (screenOn={})".format(on), OK if on else WARN)
        check("截图成功: shots/doctor.png", OK)
        cur = d.app_current()
        print("        当前前台: {} / {}".format(cur.get("package"), cur.get("activity")))
        if "lock" in str(cur.get("package", "")).lower():
            check("仍停在锁屏", BAD, "去掉锁屏密码，或改成滑动解锁")
    except Exception as exc:
        check("截图失败", WARN, str(exc))

    print("\n打开 shots/doctor.png 看一眼：如果画面是手机当前桌面，说明整条链路已经通了。")
    return summary()


def summary() -> int:
    print("\n" + "=" * 64)
    bad = [r for r in results if r[0] == BAD]
    warn = [r for r in results if r[0] == WARN]
    if bad:
        print("结论: 有 {} 项阻塞，按上面提示处理后重跑 python doctor.py".format(len(bad)))
        return 2
    if warn:
        print("结论: 基本可用，有 {} 项提醒。下一步: python ui_probe.py --watch 3".format(len(warn)))
        return 0
    print("结论: 全部通过。下一步: python ui_probe.py --watch 3 取真实按钮文案")
    return 0


if __name__ == "__main__":
    sys.exit(main())
