#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单步操作器：用来逐屏验证 wallet_daily 的真实代码路径

    python act.py open            启动小米钱包
    python act.py dump            打印当前界面文字
    python act.py tap 做任务      按文字/描述找到元素并点击（走 root 注入）
    python act.py ad              执行一次广告等待逻辑
    python act.py shot 名字       截图
    python act.py key back|home   按键
    python act.py focus           看前台 Activity
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wallet_daily import WalletBot, init_dirs, load_config  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    args = sys.argv[2:]

    init_dirs()
    cfg = load_config()
    bot = WalletBot(cfg)
    bot.connect()

    try:
        if cmd == "open":
            bot.wake()
            bot.d.app_start(cfg["package"], wait=True)
            time.sleep(3)
            print("已启动", cfg["package"])
        elif cmd == "dump":
            for t in bot.texts():
                print(t)
        elif cmd == "tap":
            if not args:
                print("缺少文字参数")
                return 1
            hit = bot.find_target(args[0])
            if not hit:
                print("未找到:", args[0])
                return 2
            _, x, y = hit
            print("命中「{}」@({},{})".format(args[0], x, y))
            bot.tap(x, y)
        elif cmd == "ad":
            spec = cfg["steps"][0] if False else {"max_seconds": 90}
            print("ad 结果:", bot.action_ad(spec))
        elif cmd == "shot":
            bot.shot(args[0] if args else "manual")
        elif cmd == "key":
            code = {"back": "4", "home": "3", "power": "26", "wake": "224"}.get(
                args[0] if args else "back", "4")
            bot.key(code)
        elif cmd == "focus":
            print(bot.current_focus())
        else:
            print(__doc__)
            return 1
    finally:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
