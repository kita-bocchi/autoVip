"""USB / adb 链路稳定性压测

用途：换线、换口之后验证链路是否可靠，避免晚间的定时任务跑一半掉线。

测两种故障，分开统计，因为修法完全不同：
  1. 掉设备 —— devices 列表里看不到机器或状态不是 device（线/口/供电问题）
  2. 杀服务 —— adb server 被杀掉重启（安全软件/端口冲突，与线无关）

用法：
    .venv\\Scripts\\python.exe stab.py             # 默认 90 轮 x 2 秒，约 3 分钟
    .venv\\Scripts\\python.exe stab.py -n 300 -i 1  # 300 轮 x 1 秒，约 5 分钟
"""
from __future__ import annotations

import argparse
import datetime as dt
import statistics
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from adb_env import adb, adb_devices, setup_adb  # noqa: E402


def stamp() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--rounds", type=int, default=90, help="轮数")
    ap.add_argument("-i", "--interval", type=float, default=2.0, help="间隔秒")
    ap.add_argument("--serial", default="", help="目标序列号，留空取第一台")
    args = ap.parse_args()

    if not setup_adb():
        raise SystemExit("找不到 adb")

    # 预热：确认起点是好的，否则测出来的数字没意义
    devs = adb_devices()
    ok = [s for s, st in devs if st == "device"]
    if not ok:
        raise SystemExit("起始就没有可用设备，先跑 doctor.py: {}".format(devs))
    serial = args.serial or ok[0]
    print("目标设备: {}".format(serial))
    print("开始压测: {} 轮 x {}s ≈ {:.0f} 秒\n".format(
        args.rounds, args.interval, args.rounds * args.interval))

    # 触发一次 server 启动，这样后面再看到 "daemon not running" 就一定是中途挂的
    adb("start-server")

    drop = 0          # 掉设备次数
    killed = 0        # adb 服务被杀次数
    fails = 0         # 命令失败次数
    lat: list[float] = []
    events: list[str] = []

    for i in range(1, args.rounds + 1):
        t0 = time.time()
        devs = adb_devices()
        states = dict(devs)
        state = states.get(serial)
        alive = (state == "device")

        # 一次真实负载，确认不只是列表里有、实际传不了
        payload = ""
        if alive:
            try:
                payload = adb("-s", serial, "shell", "echo ok").strip()
            except Exception:
                payload = ""
        cost = (time.time() - t0) * 1000

        if alive and payload == "ok":
            lat.append(cost)
        else:
            fails += 1
            if not alive:
                drop += 1
            msg = "第 {} 轮异常: 状态={} 负载={!r} 耗时={:.0f}ms".format(
                i, state or "无设备", payload, cost)
            print("[{}] {}".format(stamp(), msg))
            events.append(msg)

        # adb server 是否中途重启（每轮单独看，避免和掉设备混在一起）
        try:
            out = adb("version")
            if "daemon not running" in out:
                killed += 1
                msg = "第 {} 轮: adb 服务被重启".format(i)
                print("[{}] {}".format(stamp(), msg))
                events.append(msg)
        except Exception:
            pass

        if i % 15 == 0:
            avg = statistics.mean(lat) if lat else 0
            print("[{}] 进度 {}/{}  失败 {}  平均 {:.0f}ms".format(
                stamp(), i, args.rounds, fails, avg))
        time.sleep(max(args.interval, 0))

    print("\n" + "=" * 46)
    print("总轮数      : {}".format(args.rounds))
    print("命令失败    : {}".format(fails))
    print("掉设备      : {}".format(drop))
    print("服务被杀    : {}".format(killed))
    if lat:
        print("响应延迟    : 平均 {:.0f}ms / 中位 {:.0f}ms / 最大 {:.0f}ms".format(
            statistics.mean(lat), statistics.median(lat), max(lat)))
    print("=" * 46)

    if drop == 0 and fails == 0:
        print("结论: USB 链路稳定，可以用于无人值守")
    elif drop > 0:
        print("结论: 出现过掉设备，换口/换线，或关闭 USB 选择性暂停")
    else:
        print("结论: 设备没掉但命令有失败，检查 adb 服务是否被安全软件干扰")


if __name__ == "__main__":
    main()
