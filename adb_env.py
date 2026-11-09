#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""adb 环境自举 + 输入注入

优先使用项目自带的 tools/platform-tools，免装 PATH。
本机（HyperOS）权限说明：普通 shell 没有 INJECT_EVENTS，`input tap` 会报
SecurityException；有 root 时用 su 转发即可正常注入。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOCAL_ADB = BASE / "tools" / "platform-tools"

_SERIAL: str | None = None


def setup_adb() -> str | None:
    """把项目内 adb 目录塞进 PATH，返回 adb 可执行文件路径"""
    exe = LOCAL_ADB / ("adb.exe" if os.name == "nt" else "adb")
    if exe.exists():
        os.environ["PATH"] = str(LOCAL_ADB) + os.pathsep + os.environ.get("PATH", "")
        os.environ.setdefault("ADBUTILS_ADB_PATH", str(exe))
        os.environ.setdefault("ADB_PATH", str(exe))
    return shutil.which("adb")


def set_serial(serial: str | None) -> None:
    """后续所有 adb 命令都针对这台设备"""
    global _SERIAL
    _SERIAL = serial or None


def adb(*args: str, timeout: int = 25) -> str:
    exe = setup_adb()
    if not exe:
        return ""
    cmd = [exe]
    if _SERIAL:
        cmd += ["-s", _SERIAL]
    cmd += list(args)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout)
        return (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:
        return "ERROR: {}".format(exc)


def shell(cmd: str, timeout: int = 25) -> str:
    """执行一条完整的 shell 命令（整串传给设备的 sh，避免引号被本地吃掉）"""
    return adb("shell", cmd, timeout=timeout)


def shell_root(cmd: str) -> str:
    """以 root 身份执行，用于需要 INJECT_EVENTS 的输入注入"""
    inner = cmd.replace("'", "'\\''")
    return adb("shell", "su -c '{}'".format(inner))


def has_root() -> bool:
    out = shell_root("id")
    return "uid=0" in out


def adb_version() -> str:
    out = adb("version")
    for line in out.splitlines():
        if "Android Debug Bridge version" in line:
            return line.strip()
    return out.strip()


def adb_devices() -> list[tuple[str, str]]:
    """返回 [(serial, state)]，state 可能是 device / unauthorized / offline"""
    out = adb("devices")
    result: list[tuple[str, str]] = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            result.append((parts[0], parts[1]))
    return result


def ensure_online(serial: str | None, retries: int = 3) -> bool:
    """无线设备掉线后自动 adb connect 拉回来"""
    if not serial:
        return bool([s for s, st in adb_devices() if st == "device"])
    for _ in range(max(1, retries)):
        if any(s == serial and st == "device" for s, st in adb_devices()):
            return True
        if ":" in serial:
            adb("connect", serial)
            time.sleep(1.5)
            if any(s == serial and st == "device" for s, st in adb_devices()):
                return True
        else:
            time.sleep(1.0)
    return False
