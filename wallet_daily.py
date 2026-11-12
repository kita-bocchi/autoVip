#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小米钱包每日「看广告领视频会员」自动执行器

运行在常开电脑上，通过 adb 驱动 USB 连接的 HyperOS 手机。
流程由 config.json 的 steps 描述，改界面不改代码。

用法：
    python wallet_daily.py            # 正常执行（当天成功过则跳过）
    python wallet_daily.py --force    # 忽略幂等标记强制跑一次
    python wallet_daily.py --dry      # 只连设备、探界面，不点击
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import re
import sys
import time
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
CFG_FILE = BASE / "config.json"
STATE_FILE = BASE / "state.json"
LOG_DIR = BASE / "logs"
SHOT_DIR = BASE / "shots"

sys.path.insert(0, str(BASE))
from adb_env import (adb, adb_devices, ensure_online, has_root, set_serial,  # noqa: E402
                     setup_adb, shell, shell_root)

COUNTDOWN_RE = re.compile(r"(?:还剩|剩余|倒计时)?\s*(\d{1,3})\s*(?:秒|s|S)\s*(?:后)?\s*$")
BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

_log_fp = None


def log(msg: str) -> None:
    line = "[{}] {}".format(dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    if _log_fp:
        try:
            _log_fp.write(line + "\n")
            _log_fp.flush()
        except Exception:
            pass


# 行为拟人开关：关掉后所有等待与点击恢复成固定值（调试复现用）
HUMANIZE = True


def nap(sec: float, lo: float = 0.90, hi: float = 1.50) -> None:
    """带随机分布的等待。

    脚本原先每步都是 time.sleep(固定值)，整轮耗时精确到秒（两次回归都是 2m47s），
    这是行为分析最容易抓的指纹之一：真人的操作间隔一定有方差。
    默认在基准的 0.90~1.50 倍之间取值（均值约 1.2 倍，整轮会慢 20% 左右）。
    """
    if not HUMANIZE:
        time.sleep(sec)
        return
    time.sleep(max(0.05, sec * random.uniform(lo, hi)))


def ad_dwell(sec: float) -> float:
    """广告/站内浏览的实际停留时长：只在基准上往长了随机，不会短于要求。

    平台要求看满 10 秒，所以下限必须大于基准；上限给到 1.9 倍是让
    "每次都恰好停留 14.0 秒" 这种规律消失。
    """
    if not HUMANIZE:
        return sec
    return sec * random.uniform(1.15, 1.90)


def init_dirs() -> None:
    global _log_fp
    LOG_DIR.mkdir(exist_ok=True)
    SHOT_DIR.mkdir(exist_ok=True)
    _log_fp = open(LOG_DIR / "daily_{}.log".format(dt.date.today().isoformat()),
                   "a", encoding="utf-8")


def load_config() -> dict:
    cfg = json.loads(CFG_FILE.read_text(encoding="utf-8"))
    cfg.setdefault("serial", "")
    cfg.setdefault("package", "com.mipay.wallet")
    cfg.setdefault("max_retry", 2)
    cfg.setdefault("skip_if_done", True)
    cfg.setdefault("notify_webhook", "")
    cfg.setdefault("success_keywords", [])
    cfg.setdefault("steps", [])
    cfg.setdefault("humanize", True)
    return cfg


def read_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def write_state(data: dict) -> None:
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def notify(cfg: dict, title: str, body: str) -> None:
    url = (cfg.get("notify_webhook") or "").strip()
    if not url:
        return
    try:
        if "sctapi.ftqq.com" in url or "pushplus" in url:
            data = urllib.parse.urlencode({"title": title, "desp": body}).encode()
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"})
        else:
            payload = {"msgtype": "markdown",
                       "markdown": {"content": "**{}**\n\n{}".format(title, body)}}
            req = urllib.request.Request(
                url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
        log("通知已发送")
    except Exception as exc:
        log("通知发送失败: {}".format(exc))


class NetworkUnavailable(RuntimeError):
    """手机没有可用网络，且重试重启 WiFi 后仍未恢复"""


class EnvError(RuntimeError):
    """环境问题：没 adb、没设备、未授权、offline、缺依赖。

    这里**必须**是 Exception 的子类，不能再用 SystemExit。
    SystemExit 继承自 BaseException，`except Exception` 接不住，会直接穿透 main()，
    结果是：日志停在半句话、退出码 1、pythonw 下 stderr 被丢弃——
    2026-11-10 就是这么静默失败的（设备 unauthorized，什么都没记下来）。
    """


class WalletBot:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.d = None
        self.shots: list[Path] = []
        self.width, self.height = 1080, 2400
        self.inject = "shell"
        self.has_root = False
        self.ad_tasks_ok: bool | None = None
        self.ads_done = 0          # 本次运行实际完成的广告条数（写回 state 累计）
        self.sweep = False         # 补检模式：不等长冷却，交给下一轮补检
        self.net_ok: bool | None = None
        self._last_wake = 0.0

    # ---------- 连接与唤醒 ----------
    def wait_device(self, timeout: float = 120.0) -> list[tuple[str, str]]:
        """等待设备上线。早上手机深度休眠时 USB 可能还没唤醒，中途重启一次 adb 服务。"""
        timeout = max(timeout, 5.0)
        deadline = time.time() + timeout
        restarted = False
        last: list[tuple[str, str]] = []
        while True:
            last = adb_devices()
            states = dict(last)
            if "device" in states.values():
                if restarted:
                    log("设备已恢复上线: {}".format(last))
                return last
            if "unauthorized" in states.values():
                # 2026-11-10 实测：这种情况下重启 adb 服务就能恢复（授权状态
                # 残留在旧 server 进程里），不是真的需要人去点「允许」。
                # 所以先自动重启一次，仍然 unauthorized 才交给上层报错。
                if not restarted:
                    log("设备未授权，重启 adb 服务后重试一次")
                    adb("kill-server")
                    nap(2.0)
                    restarted = True
                    continue
                return last  # 重启过还是不行，需要人工点允许
            left = deadline - time.time()
            if left <= 0:
                return last
            # 剩余时间过半仍没设备，重启一次 adb 服务（USB 端口挂起时常见）
            if not restarted and left < timeout * 0.5:
                log("设备未就绪，重启 adb 服务后继续等待...")
                try:
                    adb("kill-server")
                    nap(2)
                    adb("start-server")
                except Exception as exc:
                    log("重启 adb 服务失败: {}".format(exc))
                restarted = True
            log("等待设备上线，剩余 {:.0f}s（当前: {}）".format(
                left, last or "无设备"))
            nap(5)

    def preflight(self) -> str:
        """检查 adb 与设备状态，返回可用序列号"""
        if not setup_adb():
            raise EnvError("找不到 adb：请确认 tools/platform-tools/adb.exe 存在，"
                             "或把 adb 加入 PATH")
        cfg_serial = (self.cfg.get("serial") or "").strip()
        if cfg_serial:
            # 无线 adb 掉线后自动拉回
            ensure_online(cfg_serial)
        devs = self.wait_device(float(self.cfg.get("device_wait_seconds", 120)))
        if not devs:
            raise EnvError(
                "等待 {} 秒后仍未检测到设备。请检查：\n"
                "  1) 换一根能传数据的 USB 线\n"
                "  2) 手机已开启「USB 调试」\n"
                "  3) 手机弹窗里点过「允许」\n"
                "然后运行 .venv\\Scripts\\python.exe doctor.py 复查".format(
                    self.cfg.get("device_wait_seconds", 120)))

        states = dict(devs)
        log("  设备状态: {}".format(", ".join("{}={}".format(k, v) for k, v in states.items())))
        if "unauthorized" in states.values():
            raise EnvError("设备未授权：解锁手机，在「允许 USB 调试」弹窗里点允许")
        if "offline" in states.values():
            raise EnvError("设备 offline：执行 tools\\platform-tools\\adb.exe kill-server "
                             "后重新插拔数据线")

        serials = [s for s, st in devs if st == "device"]
        if not serials:
            raise EnvError("设备状态异常: {}".format(states))

        cfg_serial = (self.cfg.get("serial") or "").strip()
        if cfg_serial:
            if cfg_serial not in serials:
                raise EnvError("config.json 指定的 serial {} 不在线，当前在线: {}".format(
                    cfg_serial, serials))
            return cfg_serial
        if len(serials) > 1:
            raise EnvError("检测到多台设备 {}，请在 config.json 的 serial 里指定一台".format(serials))
        return serials[0]

    def connect(self) -> None:
        try:
            import uiautomator2 as u2
        except ImportError:
            raise EnvError("缺少依赖，请执行: .venv\\Scripts\\python.exe -m pip install "
                             "-r requirements.txt")

        serial = self.preflight()
        set_serial(serial)
        self.serial = serial
        try:
            self.d = u2.connect(serial)
        except Exception as exc:
            raise EnvError("连接设备失败: {}\n首次连接需要打开「USB 安装」以部署 atx-agent".format(exc))

        self.has_root = has_root()
        self.inject = "root" if self.has_root else "shell"
        log("输入注入: {}（shell 无 INJECT_EVENTS 权限时必须用 root）".format(self.inject))
        if not self.has_root:
            log("警告: 未取得 root，若 HyperOS 关闭了「USB 调试(安全设置)」则点击会被拒绝")

        try:
            self.d.settings["wait_timeout"] = 8.0
        except Exception:
            pass
        info = {}
        try:
            info = self.d.info or {}
        except Exception:
            pass
        log("已连接设备: {}x{}".format(info.get("displayWidth"), info.get("displayHeight")))
        try:
            self.width, self.height = self.d.window_size()
        except Exception:
            self.width, self.height = 1080, 2400

    # ---------- 输入注入（root 转发，绕开 INJECT_EVENTS 限制） ----------
    def _input(self, *args: str) -> None:
        cmd = "input " + " ".join(args)
        out = shell_root(cmd) if self.inject == "root" else shell(cmd)
        if "Exception" in out or "Error" in out:
            log("  注入异常: {}".format(out.strip().splitlines()[0][:160]))

    def tap(self, x: float, y: float, jitter: int | None = None) -> None:
        """点一下。

        原来的抖动是 random.randint(-3, 3)，在 1080 宽的屏上只有 ±0.28%，
        等于每次都点在同一处——真手指同一点的离散度远大于此。
        现在默认按高斯分布散开（σ ≈ 1% 短边，约 11px，最大 ±2 倍 σ），
        真人点击也是集中在中心、少数偏一点，而不是均匀分布。
        显式传 jitter（含 0）时仍按原来的均匀抖动，供配置里的精确坐标步骤使用。
        """
        if not HUMANIZE:
            self._input("tap", str(int(x)), str(int(y)))
            return
        if jitter is not None:
            dx = random.randint(-jitter, jitter)
            dy = random.randint(-jitter, jitter)
        else:
            sd = max(4.0, min(self.width, self.height) * 0.010)
            cap = sd * 2
            dx = int(max(-cap, min(cap, random.gauss(0, sd))))
            dy = int(max(-cap, min(cap, random.gauss(0, sd))))
        x = max(2, min(self.width - 2, int(x + dx)))
        y = max(2, min(self.height - 2, int(y + dy)))
        self._input("tap", str(x), str(y))

    def swipe(self, x1: float, y1: float, x2: float, y2: float, ms: int = 300) -> None:
        """滑动。时长与起止点都加随机，避免「永远 300ms 的完美直线」。"""
        if HUMANIZE:
            ms = int(ms * random.uniform(0.75, 1.45))
            x1 += random.randint(-9, 9)
            y1 += random.randint(-9, 9)
            x2 += random.randint(-9, 9)
            y2 += random.randint(-9, 9)
        self._input("swipe", str(int(x1)), str(int(y1)),
                    str(int(x2)), str(int(y2)), str(max(80, int(ms))))

    def key(self, code: str) -> None:
        self._input("keyevent", code)

    def current_focus(self) -> str:
        return shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'").strip()

    def current_package(self) -> str:
        """当前前台包名，拿不到返回空串"""
        try:
            m = re.search(r"([a-zA-Z][\w.]*)/[\w.$]+", self.current_focus())
            return m.group(1) if m else ""
        except Exception:
            return ""

    def escape_foreign(self, max_back: int = 2) -> bool:
        """广告误点会跳到应用商店/浏览器。若前台已不在目标 App，连按返回撤回。
        返回 True 表示最终回到了目标 App。"""
        guard = self.cfg.get("package", "com.mipay.wallet").rsplit(".", 1)[0]
        for i in range(max_back):
            pkg = self.current_package()
            if not pkg or pkg.startswith(guard):
                return True
            log("  前台已跳到外部页面（{}），按返回键撤回".format(pkg))
            self.key("4")
            nap(1.8)
        pkg = self.current_package()
        ok = (not pkg) or pkg.startswith(guard)
        if not ok:
            log("  未能撤回目标 App，当前前台: {}".format(pkg))
        return ok

    def keep_awake(self, on: bool = True) -> None:
        """跑任务期间别让屏幕自动熄灭（熄了 WebView 会读不到节点）"""
        try:
            shell_root("svc power stayon {}".format("true" if on else "false"))
            log("已{}屏幕常亮".format("开启" if on else "关闭"))
        except Exception as exc:
            log("设置常亮失败（不影响主流程）: {}".format(exc))

    def wake(self) -> None:
        try:
            screen_on = bool((self.d.info or {}).get("screenOn", True))
        except Exception:
            screen_on = True
        if not screen_on:
            log("屏幕熄灭，点亮")
            self.key("224")  # KEYCODE_WAKEUP
            nap(1.5)
        # 无密码设备用系统命令直接解散锁屏，比上滑稳
        shell_root("wm dismiss-keyguard")
        nap(1.0)
        if "Keyguard" in self.current_focus():
            log("仍在锁屏，尝试上滑")
            self.swipe(self.width * 0.5, self.height * 0.85,
                       self.width * 0.5, self.height * 0.35, 300)
            nap(1.2)

    # ---------- 网络 ----------
    # 手机断网时活动页只会白屏，脚本会照常点 6 分钟然后报"未检测到成功标识"，
    # 既浪费时间又产生误导性结论。所以每次开跑前先确认真能上外网。
    # 探测点用纯 IP + 域名混合：单个 DNS 可能屏蔽 ICMP（实测 114.114.114.114 就不回包），
    # 只要有一个通就算在线。
    NET_PROBES = ("223.5.5.5", "119.29.29.29", "www.baidu.com")

    def phone_online(self) -> bool:
        """手机当前是否能访问外网（ICMP 探测，任一目标回包即算在线）"""
        for host in self.NET_PROBES:
            out = shell("ping -c 1 -W 2 {}".format(host), timeout=8)
            if re.search(r"1 (?:packets )?received", out):
                return True
        return False

    def cycle_wifi(self) -> None:
        """关掉再打开 WiFi。用 root 转发，普通 shell 改不了 WiFi 状态"""
        log("  关闭 WiFi")
        shell_root("svc wifi disable")
        nap(3.0)
        log("  重新打开 WiFi")
        out = shell_root("svc wifi enable")
        if "Error" in out or "Exception" in out:
            # Android 12+ 上 svc 有时被弃用，退回 cmd wifi
            shell_root("cmd wifi set-wifi-enabled enabled")
        nap(2.0)

    def ensure_network(self, retries: int = 3, settle: float = 25.0) -> bool:
        """确认手机能上网；断网则重启 WiFi，最多重试 retries 轮

        每轮流程：检测 → 断网则关/开 WiFi → 在 settle 秒内轮询等待恢复。
        全部失败返回 False，由 run() 抛出 NetworkUnavailable 直接退出，
        不进入后面必然白跑的活动页流程。
        """
        if not self.cfg.get("network_check", True):
            log("  已关闭网络检查（config.network_check=false），跳过")
            return True
        if self.phone_online():
            log("  网络正常")
            self.net_ok = True
            return True

        for i in range(1, max(1, retries) + 1):
            log("  第 {}/{} 轮: 手机没有网络，重启 WiFi 试试".format(i, retries))
            self.shot("no_network_{}".format(i))
            try:
                self.cycle_wifi()
            except Exception as exc:
                log("  开关 WiFi 失败: {}".format(exc))

            deadline = time.time() + max(5.0, settle)
            while time.time() < deadline:
                nap(3.0)
                if self.phone_online():
                    log("  网络已恢复（第 {} 轮重试成功）".format(i))
                    self.net_ok = True
                    return True
            log("  第 {} 轮重试后仍无网络".format(i))

        self.net_ok = False
        return False

    # ---------- 元素工具 ----------
    def shot(self, tag: str) -> None:
        path = SHOT_DIR / "{}_{}.png".format(dt.datetime.now().strftime("%H%M%S"), tag)
        try:
            self.d.screenshot(str(path))
            self.shots.append(path)
            log("截图 -> {}".format(path.name))
        except Exception as exc:
            log("截图失败: {}".format(exc))

    def texts(self, timeout: float = 0.0) -> list[str]:
        """当前界面所有非空 text / content-desc"""
        out: list[str] = []
        try:
            for el in self.d.xpath('//*[@text!=""]').all():
                if el.text:
                    out.append(el.text.strip())
        except Exception:
            pass
        try:
            for el in self.d.xpath('//*[@content-desc!=""]').all():
                desc = el.attrib.get("content-desc")
                if desc:
                    out.append(desc.strip())
        except Exception:
            pass
        return [t for t in out if t]

    def find_countdown(self) -> str | None:
        """只在短文本里认倒计时，避免把「10月12日看10秒广告得365天」这种文案误判"""
        for t in self.texts():
            t = t.strip()
            if not t or len(t) > 12:
                continue
            if any(ch in t for ch in ("月", "日", "得", "看", "用户")):
                continue
            if COUNTDOWN_RE.search(t):
                return t
        return None

    # ---------- 任务面板 ----------
    def _wake_nodes(self) -> bool:
        """把折叠掉的无障碍节点叫醒

        这个活动页是 WebView 套 H5。稳定下来之后 WebView 会把虚拟无障碍节点
        折叠掉——dump 出来的树从 90KB 骤降到 25KB，页面上一个字都读不到，
        但截图完全是正常的。一次大幅滑动再滑回原位能强制它重建节点，
        净位移为 0，不会改变页面位置。
        """
        if time.time() - self._last_wake < 6.0:
            return False
        try:
            n = len(self.d.xpath('//*[@text!=""]').all())
        except Exception:
            return False
        if n >= 6:
            return False
        self._last_wake = time.time()
        log("  页面节点被折叠（只剩 {} 个），滑动唤醒".format(n))
        for _ in range(2):
            self.swipe(self.width * 0.5, self.height * 0.75,
                       self.width * 0.5, self.height * 0.42, 150)
            nap(0.3)
            self.swipe(self.width * 0.5, self.height * 0.42,
                       self.width * 0.5, self.height * 0.75, 150)
            nap(0.6)
            try:
                if len(self.d.xpath('//*[@text!=""]').all()) >= 6:
                    return True
            except Exception:
                pass
        return True

    def nodes(self) -> list[tuple[str, int, int, int, int]]:
        """当前界面所有可见节点：(文字, x1, y1, x2, y2)"""
        out = self._collect_nodes()
        if len(out) < 6 and self._wake_nodes():
            out = self._collect_nodes()
        return out

    def _collect_nodes(self) -> list[tuple[str, int, int, int, int]]:
        out = []
        for expr in ('//*[@text!=""]', '//*[@content-desc!=""]'):
            try:
                elements = self.d.xpath(expr).all()
            except Exception:
                continue
            for el in elements:
                attrs = el.attrib
                label = (attrs.get("text") or attrs.get("content-desc") or "").strip()
                m = BOUNDS_RE.search(attrs.get("bounds", ""))
                if not label or not m:
                    continue
                x1, y1, x2, y2 = (int(v) for v in m.groups())
                if x2 - x1 < 8 or y2 - y1 < 8:
                    continue
                out.append((label, x1, y1, x2, y2))
        return out

    def panel_open(self) -> bool:
        """做任务面板是否已展开（用邀请好友那条做锚点）"""
        return self.find_target("邀请新朋友来领会员") is not None

    def close_task_panel(self) -> bool:
        """关掉展开的任务面板

        开完宝箱后面板会自己弹出来盖住页面，不关掉就找不到「再看10秒」大按钮，
        以前就是这样误判「今天没有下一条广告」。
        """
        if not self.panel_open():
            return True
        for x, y in ((self.width * 0.907, self.height * 0.439),
                     (self.width * 0.907, self.height * 0.400)):
            self.tap(x, y)
            nap(1.2)
            if not self.panel_open():
                log("  已关闭任务面板")
                return True
        self.key("4")
        nap(1.2)
        return not self.panel_open()

    def scroll_to_top(self, times: int = 3) -> None:
        for _ in range(max(1, times)):
            self.swipe(self.width * 0.5, self.height * 0.32,
                       self.width * 0.5, self.height * 0.88, 300)
            nap(0.9)

    def open_task_panel(self) -> bool:
        if self.panel_open():
            return True
        for attempt in range(2):
            for kw in ("做任务 领时长", "做任务 领会员", "去领取"):
                hit = self.find_target(kw)
                if not hit:
                    continue
                log("  点「{}」打开任务面板".format(kw))
                self.tap(hit[1], hit[2])
                nap(1.8)
                self.dismiss_hint()
                if self.panel_open():
                    return True
            if attempt == 0:
                log("  没找到入口，先回到页面顶部再试")
                self.scroll_to_top(2)
        return self.panel_open()

    def dismiss_hint(self) -> None:
        """关掉「任务说明」这类提示弹窗"""
        for kw in ("知道了", "我知道了", "确定"):
            if self.click_any([kw], timeout=1.5):
                log("  关闭提示弹窗: {}".format(kw))
                nap(1.0)
                return

    def panel_top(self) -> int:
        """任务面板顶边：用于把「面板里的按钮」和「面板背后页面的按钮」分开"""
        top = None
        for label, x1, y1, x2, y2 in self.nodes():
            if "做任务" in label and "领会员" in label:
                top = y1 if top is None else min(top, y1)
        if top is None:
            hit = self.find_target("邀请新朋友来领会员")
            if hit:
                top = max(0, hit[2] - 200)
        return top if top is not None else self.height // 2

    def panel_buttons(self) -> list[tuple[str, int, int]]:
        """面板区域内右侧的所有短按钮"""
        # 面板顶边往下留 40px 余量：活动页背后那层「￥0.01 + 30天时长」卡片
        # 的 bounds 会压到面板顶边上方一点，不排除就会被当成本面板的按钮
        top = self.panel_top() + 40
        noise = ("时长", "视频封面", "封面", "/", "（", "）", "￥")
        out = []
        for label, x1, y1, x2, y2 in self.nodes():
            # 面板里每行左侧是标题文字，右侧才是操作按钮
            text = label.split("\n")[-1].strip()
            if not text or len(text) > 8:
                continue
            if y1 < top or y2 > self.height + 2:
                continue
            if x1 < self.width * 0.5:
                continue
            if any(n in text for n in noise) or text.isdigit():
                continue
            # 页面背后那层的「去领取」会叠到面板行上，以前误点它弹过邀请分享页
            if text == "去领取":
                continue
            out.append((text, (x1 + x2) // 2, (y1 + y2) // 2))
        return out

    def find_ad_task(self, watch_kws: list[str], skip_kws: set[str]):
        """在面板里找广告任务的按钮，返回 (文字, x, y)"""
        candidates = self.panel_buttons()
        if self.cfg.get("panel_debug", True):
            log("  面板按钮: {}".format([c[0] for c in candidates]))
        for text, x, y in candidates:
            if text in skip_kws:
                continue
            if any(kw in text for kw in watch_kws):
                return text, x, y
        return None

    # ---------- 活动页主入口广告（不在任务面板里） ----------
    # 实测：活动页中部那个「再看10秒 继续领时长」是独立入口，点了直接
    # 跳到广告落地 App（快手/应用商店…），钱包客户端按「跳转时间」计时，
    # 广告播没播完、点没点协议都无所谓，切回来就判定完成并弹宝箱。
    def on_activity_page(self) -> bool:
        for kw in ("看10秒", "点击领会员", "做任务 领时长", "做任务 加速领", "兑换会员",
                   "邀请新朋友来领会员"):
            if self.find_target(kw):
                return True
        return False

    def find_ad_entry(self) -> tuple[str, int, int] | None:
        """找活动页主入口的看广告大按钮

        文案会变：「再看10秒 继续领时长」/「看10秒 领会员」/「看10秒 得时长」，
        唯一稳定的特征是——它是横跨大半个屏宽的一整条大按钮。
        所以按「含秒字 + 宽度超过屏宽 60%」来找，取最靠上的那个。
        不写死「10秒」：新账号/新活动可能变成「看15秒」「看30秒」。
        """
        best = None
        for label, x1, y1, x2, y2 in self.nodes():
            if "秒" not in label:
                continue
            if "试用" in label:  # 面板里的「试用30s」不是广告入口
                continue
            if x2 - x1 < self.width * 0.6:
                continue
            cy = (y1 + y2) // 2
            if best is None or cy < best[2]:
                best = (label, (x1 + x2) // 2, cy)
        return best

    def on_guide_page(self) -> bool:
        """是否停在站内「活动指南」页

        今天的入口点下去没有跳第三方 App，而是打开钱包内的引导页，
        上面写着「浏览本引导说明页 10秒，可获得 1~6小时小额会员时长」。
        这种也要等够时间再返回，返回后才弹宝箱。
        """
        for kw in ("活动指南", "浏览本引导说明页", "将有机会获得更多会员时长"):
            if self.find_target(kw):
                return True
        return False

    def ensure_activity_page(self, timeout: float = 20.0) -> bool:
        """保证当前停在「玩家视界」活动页"""
        if self.on_activity_page():
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            # 还在首页：点「领视频会员」进去。不要用泛化的「视频会员」，
            # 它会命中状态栏通知和活动页里的其它文案，点下去反而跑飞
            for kw in ("领视频会员", "会员中心"):
                hit = self.find_target(kw)
                if hit:
                    log("  从首页进活动页: 「{}」".format(kw))
                    self.tap(hit[1], hit[2])
                    nap(3.0)
                    break
            if self.on_activity_page():
                return True
            nap(1.5)
        return self.on_activity_page()

    def close_reward_popup(self) -> bool:
        """关掉开箱后的「恭喜获得 X 小时」奖励展示"""
        for _ in range(3):
            if not self.find_target("恭喜获得"):
                return True
            for fx, fy in ((0.500, 0.692), (0.500, 0.778),
                           (0.500, 0.798), (0.500, 0.860)):
                self.tap(self.width * fx, self.height * fy)
                nap(1.4)
                if not self.find_target("恭喜获得"):
                    log("  已关闭奖励弹窗")
                    return True
        return not self.find_target("恭喜获得")

    def open_treasure_box(self) -> bool:
        """开「领取奖励」宝箱

        宝箱是 Canvas 画的，uiautomator 里一个文字节点都没有，只能按坐标点；
        点开后才会出现「恭喜获得」文字，所以用文字做成功判据。
        开箱按钮在屏高 44% 左右（实测 1047~1070 像素，按钮本身直径约 230px）。
        """
        if self.find_target("恭喜获得"):  # 上一次没关干净
            self.close_reward_popup()
            return False
        # 宝箱没有文字节点，只能按坐标点。位置随活动页布局浮动
        # （实测 0.440 / 0.446 / 0.464 都出现过），换账号后布局还会整体偏移，
        # 所以给一组候选逐个试，点偏了也只是点在遮罩空白处，无副作用。
        for fy in (0.440, 0.464, 0.420, 0.485, 0.505):
            self.tap(self.width * 0.5, self.height * fy)
            nap(2.0)
            if self.find_target("恭喜获得"):
                log("  宝箱已打开")
                nap(1.2)
                self.close_reward_popup()
                return True
        return False

    def claim_pending_box(self) -> bool:
        """主按钮变成「点击领会员」= 有奖励没领，点它才会弹宝箱

        实测（2026-11-07）：站内「活动指南」页停留够 10 秒后返回，
        页面不直接弹宝箱，而是把大按钮改成「点击领会员」，
        再点一下才出「领取奖励」。如果流程直接结束，这份时长就丢了。
        """
        hit = self.find_target("点击领会员")
        if not hit:
            return False
        log("  主按钮是「点击领会员」，有奖励待领，点开")
        self.tap(hit[1], hit[2])
        nap(2.5)
        if self.open_treasure_box():
            log("  已补领待领取奖励")
            return True
        return False

    def dismiss_guide(self) -> None:
        """关掉「从桌面入口访问」这类涂层引导（右下角 X）"""
        for kw in ("从桌面入口访问", "桌面入口访问", "添加入口", "添加桌面", "添加小组件"):
            if self.find_target(kw):
                log("  关闭桌面引导图层")
                self.tap(self.width * 0.639, self.height * 0.784)
                nap(1.2)
                return

    def enter_from_app(self) -> bool:
        """没有桌面小组件时的回退路径：直接开 App，从首页「领视频会员」进活动页

        换新账号、重置桌面、换设备后小组件大概率不存在。旧代码找不到组件就直接
        return False，后面所有步骤都在桌面上空跑，整轮等于白做。
        """
        shell("monkey -p {} -c android.intent.category.LAUNCHER 1".format(
            self.cfg["package"]))
        for _ in range(12):
            nap(1.0)
            if self.current_package().startswith(self.cfg["package"]):
                break
        nap(3.5)
        self.dismiss_hint()
        if self.ensure_activity_page(timeout=12):
            log("  已从 App 内入口进入活动页")
            self.scroll_to_top(4)
            return True
        log("  App 内入口也没能进到活动页")
        return False

    def launch_from_widget(self, cold: bool = False) -> bool:
        """从桌面「钱包玩家视界」小组件进入活动页

        实测（2026-11-05）：只有小组件入口能把活动页正常拉起来；
        而且杀掉进程再进，往往会刷新出一条新的「看10秒」广告任务
        （同一天连着做了 7 小时 + 12 小时两条），所以 cold=True 时先杀进程。
        """
        if cold:
            shell("am force-stop {}".format(self.cfg["package"]))
            nap(2.5)
        self.key("3")
        nap(2.0)
        hit = self.find_target("钱包玩家视界")
        if not hit:
            # 组件在第二屏，往左翻页找
            for _ in range(3):
                self.swipe(self.width * 0.9, self.height * 0.55,
                           self.width * 0.1, self.height * 0.55, 300)
                nap(1.8)
                hit = self.find_target("钱包玩家视界")
                if hit:
                    break
        if not hit:
            log("  桌面上找不到「钱包玩家视界」小组件，改走 App 内首页入口")
            return self.enter_from_app()
        tx, ty = self.widget_tap_point(hit[1], hit[2])
        log("  点桌面小组件 @({},{})".format(tx, ty))
        self.tap(tx, ty)
        for _ in range(12):
            nap(1.0)
            if self.current_package().startswith(self.cfg["package"]):
                break
        pkg = self.current_package()
        if not pkg.startswith(self.cfg["package"]):
            # 小组件节点覆盖整个卡片，中心点右侧是推广图（优酷等），
            # 点偏了会直接跳到应用商店的下载页，整轮跑死在商店里。
            log("  点小组件后跳到了 {}，退回桌面重试一次".format(pkg))
            self.key("3")
            nap(2.0)
            tx, ty = self.widget_tap_point(hit[1], hit[2], force_safe=True)
            log("  重试点小组件 @({},{})".format(tx, ty))
            self.tap(tx, ty)
            for _ in range(12):
                nap(1.0)
                if self.current_package().startswith(self.cfg["package"]):
                    break
        nap(3.5)
        self.scroll_to_top(4)
        return self.on_activity_page()

    def widget_tap_point(self, cx: int, cy: int, force_safe: bool = False) -> tuple[int, int]:
        """算小组件的安全点击点

        「钱包玩家视界」小组件是一个整块节点，bounds 覆盖左边信息区和右边的
        推广图。直接点节点中心（=卡片正中）有一半机会点在推广图上，
        实测会跳到应用商店的「优酷视频」下载页，之后找不到活动页入口、
        任务面板也打不开，整轮直接失败。所以优先点左边的「去领会员」按钮，
        找不到按钮节点就退到左下角，彻底避开右半边。
        """
        if not force_safe:
            for kw in ("去领会员", "领取会员", "去会员中心", "查看会员"):
                b = self._find_bounds(kw)
                if b:
                    return (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
        b = self._find_bounds("钱包玩家视界")
        if b:
            w, h = b[2] - b[0], b[3] - b[1]
            return int(b[0] + w * 0.26), int(b[1] + h * 0.78)
        return cx, cy

    def wait_ad_entry(self, tries: int = 4, delay: float = 2.0):
        """等首页大按钮重新出现

        正常账号是「看完一条，回来立刻能点下一条」，不需要重启；
        但宝箱/奖励弹窗刚关掉时页面还没重绘，或者页面被推下去了会读不到。
        所以先回顶 + 多次重试，实在没有才考虑冷启动。
        """
        hit = self.find_ad_entry()
        for i in range(tries):
            if hit:
                return hit
            self.wake()
            if i == 0:
                self.scroll_to_top(times=2)
            nap(delay)
            hit = self.find_ad_entry()
        return hit

    def action_page_ads(self, step: dict) -> int:
        """循环做活动页主入口的「看10秒」，返回完成次数"""
        rounds = int(step.get("page_rounds", 3))
        wait_s = float(step.get("page_watch_seconds", 14))
        reenter_total = int(step.get("page_reenter", 2))
        reenter_left = reenter_total
        # 每次冷启动重进前要等多久（秒）。领完一条后存在冷却期：
        # 2026-11-10 实测 08:58 领完 → 08:53/09:03 两次冷启动都无入口 →
        # 09:22 冷启动后 0.4 秒就出现「再看10秒」。只重进一次就判定"今天做完了"
        # 会漏掉后面所有广告，所以必须按递增间隔多试几次。
        waits = step.get("page_reenter_waits") or [90, 300, 600]
        if getattr(self, "sweep", False):
            # 补检模式每 2 小时就再来一轮，没必要在单轮里干等十几分钟：
            # 入口消失就先收工，下一轮补检自然会接住新下发的任务。
            reenter_total = int(step.get("page_sweep_reenter", 1))
            reenter_left = reenter_total
            waits = step.get("page_sweep_waits") or [90]
        done = 0
        while done < rounds:
            hit = self.wait_ad_entry()
            if not hit:
                if reenter_left <= 0:
                    log("  没有「看10秒」入口了，今天广告任务做完（共 {} 条）".format(done))
                    break
                got = False
                while reenter_left > 0:
                    idx = reenter_total - reenter_left
                    wait = waits[min(idx, len(waits) - 1)]
                    reenter_left -= 1
                    log("  入口消失（领完后有冷却期），等 {}s 后第 {}/{} 次冷启动重进".format(
                        int(wait), idx + 1, reenter_total))
                    nap(wait, 1.0, 1.0)
                    if not self.launch_from_widget(cold=True):
                        continue
                    # 重进后 H5 要重新加载才会出现按钮，实测约 14 秒，
                    # 只查一次就放弃会漏掉后面的广告
                    hit = self.wait_ad_entry(tries=8, delay=3.0)
                    if hit:
                        got = True
                        break
                if not got:
                    log("  冷启动重进 {} 次仍无入口，判定今天广告已做完（共 {} 条）".format(
                        reenter_total, done))
                    break
            label, x, y = hit
            log("  第 {} 条: 点「{}」@({},{})".format(done + 1, label[:14], x, y))
            self.tap(x, y)
            nap(3.0)

            pkg = self.current_package()
            if pkg.startswith(self.cfg["package"]):
                # 没跳出去：可能是「活动指南」这种站内引导页（浏览10秒就算完成），
                # 也可能只是慢半拍，先给它一次机会
                nap(2.5)
                pkg = self.current_package()
                if pkg.startswith(self.cfg["package"]):
                    # 站内「活动指南」页刚跳过来时无障碍树常常还是空的，
                    # 单靠文字会漏判，所以再加一条：大按钮不见了 = 已经离开活动页
                    entry_left = self.find_ad_entry() is None
                    if self.on_guide_page() or entry_left:
                        dwell = ad_dwell(wait_s)
                        log("  进入站内「活动指南」页，停留 {}s 后返回".format(int(dwell)))
                        nap(dwell, 1.0, 1.0)
                        self.key("4")
                        nap(2.5)
                    else:
                        log("  点击后没有跳转，本轮放弃")
                        self.shot("page_ad_no_jump")
                        break
                else:
                    dwell = ad_dwell(wait_s)
                    log("  已跳转广告页: {}，等 {}s".format(pkg, int(dwell)))
                    nap(dwell, 1.0, 1.0)
                    self.bring_wallet_front()
                    nap(2.5)
            else:
                dwell = ad_dwell(wait_s)
                log("  已跳转广告页: {}，等 {}s".format(pkg, int(dwell)))
                nap(dwell, 1.0, 1.0)
                self.bring_wallet_front()
                nap(2.5)
            self.ensure_activity_page()
            self.dismiss_guide()
            if self.open_treasure_box():
                log("  第 {} 条广告已领取".format(done + 1))
            elif self.claim_pending_box():
                log("  第 {} 条广告已领取（按钮变「点击领会员」后补领）".format(done + 1))
            else:
                log("  第 {} 条广告没看到宝箱（可能已自动入账）".format(done + 1))
            # 开完箱常跟着弹「桌面入口访问 惊喜福利」涂层，或者任务面板自己弹出来，
            # 两者都会挡住下面的大按钮，不关掉就会误判「没有下一条广告」
            self.close_task_panel()
            self.dismiss_guide()
            nap(1.0)
            done += 1
        # 收尾：有可能最后一条的奖励还挂在「点击领会员」上
        self.dismiss_guide()
        if self.claim_pending_box():
            done = max(done, 1)
        log("  活动页广告完成 {} 次".format(done))
        return done

    def action_ad_tasks(self, step: dict) -> bool:
        """循环完成面板里的看广告任务，直到没有可看的为止"""
        rounds = int(step.get("rounds", 4))
        watch_kws = step.get("watch_keywords") or [
            "去观看", "看广告", "立即观看", "观看视频", "观看广告", "去看", "去浏览", "立即领取", "领取"]
        skip_kws = set(step.get("skip_keywords") or ["去下载", "去完成", "换一个", "去邀请"])
        close_kws = step.get("close_keywords") or ["关闭", "跳过", "X", "×", "领取奖励"]

        # 先吃掉活动页主入口的「再看10秒」——它不在任务面板里，
        # 打开面板反而会把它挡住，所以必须放在最前面做。
        self.ensure_activity_page()
        page_done = self.action_page_ads(step)
        self.ensure_activity_page()

        if not self.open_task_panel():
            log("  任务面板打不开")
            self.shot("panel_missing")
            self.ads_done = page_done
            if page_done:
                log("  面板打不开，但活动页广告已完成 {} 次".format(page_done))
                self.ad_tasks_ok = True
                return True
            self.ad_tasks_ok = False
            return False

        watched = 0
        for i in range(1, rounds + 1):
            hit = self.find_ad_task(watch_kws, skip_kws)
            if not hit:
                log("  第 {} 轮: 面板里已没有广告任务".format(i))
                break
            label, x, y = hit
            log("  第 {} 轮: 点「{}」@({},{})".format(i, label, x, y))
            self.tap(x, y)
            nap(3.0)
            self.dismiss_hint()

            played = self.action_ad({"max_seconds": step.get("max_seconds", 100),
                                     "close_keywords": close_kws})
            log("  广告播放结束: {}".format(played))
            watched += 1

            # 广告可能把页面带到应用商店或落地页，先撤回钱包再找面板
            self.escape_foreign()

            # 回到面板继续下一轮
            nap(1.0)
            if not self.panel_open():
                self.key("4")
                nap(1.5)
                if not self.open_task_panel():
                    log("  看完广告后回不到任务面板")
                    self.shot("panel_back_fail")
                    self.ads_done = page_done + watched
                    self.ad_tasks_ok = False
                    return False

        # 判定：面板里还留着试用/邀请任务，且没有广告任务 → 当天广告任务已完成
        remaining = self.find_ad_task(watch_kws, skip_kws)
        if remaining:
            log("  仍存在广告任务，可能受限或文案不符: {}".format(remaining[0]))
            self.shot("ad_task_left")
            self.ads_done = page_done + watched
            self.ad_tasks_ok = False
            return False
        if watched == 0 and page_done == 0:
            # 一条都没做，必须分清「今天本来就没什么可做」和「全失败了」。
            # 以前只要面板里有个「去完成」就算成功，会把做失败报成成功（假成功）。
            # 真正的判据是活动页主按钮变成「做任务 …」= 今日广告额度已经用完。
            finished = (self.find_target("做任务 领时长")
                        or self.find_target("做任务 领会员")
                        or self.find_target("做任务 得时长"))
            self.ads_done = page_done
            if finished:
                log("  主按钮已是「做任务 …」，判定今日广告任务已全部完成")
                self.ad_tasks_ok = True
                return True
            log("  一条广告都没做成，且主按钮不是已完成的状态")
            self.shot("ad_none_not_finished")
            self.ad_tasks_ok = False
            return False

        self.ads_done = page_done + watched
        log("  本轮共处理 {} 个面板广告任务、{} 个页面广告（本次合计 {} 条）".format(
            watched, page_done, self.ads_done))
        self.ad_tasks_ok = True
        return True

    # ---------- 试用任务 ----------
    # 面板背后那一层页面里的「去领取」「兑换会员」坐标会和面板行重叠，
    # 所以只认这个白名单里的按钮文案，其余一律忽略。
    TRIAL_BTN_HINTS = ("领取奖励", "去下载", "下载中", "安装中", "去试用", "试用",
                       "打开", "已领奖", "已完成", "已参与", "进行中",
                       "明天再来", "去完成", "待领取",
                       "体验", "去玩", "去听", "去阅读")

    def panel_rows(self, hints: tuple[str, ...] | None = None) -> list[dict]:
        """把任务面板解析成行：[{btn, title, x, y}]"""
        hints = hints or self.TRIAL_BTN_HINTS
        nodes = self.nodes()
        top = self.panel_top()
        btns: list[tuple[str, int, int, int, int]] = []
        titles: list[tuple[str, int, int]] = []
        # 页面层的大按钮（「做任务 领时长」「再看10秒」）会和面板行抢标题，
        # 它的 bounds 很高、中心正好落在第一行按钮上，必须先排除
        page_big = ("做任务", "再看", "兑换会员", "换肤", "提醒")
        for label, x1, y1, x2, y2 in nodes:
            text = label.split("\n")[-1].strip()
            if not text or y1 < top:
                continue
            if x1 < self.width * 0.55:
                if len(text) >= 6 and not text.startswith(page_big):
                    titles.append((text, y1, y2))
                continue
            if len(text) > 10:  # 右侧按钮都是短文案
                continue
            if any(h in text for h in hints):
                btns.append((text, x1, y1, x2, y2))
        rows: list[dict] = []
        for text, x1, y1, x2, y2 in btns:
            cy = (y1 + y2) // 2
            best, best_d = "", 10 ** 6
            for t, ty1, ty2 in titles:
                d = abs(cy - (ty1 + ty2) // 2)
                if d < best_d:
                    best, best_d = t, d
            rows.append({"btn": text, "title": best,
                         "x": (x1 + x2) // 2, "y": cy})
        return rows

    def find_row(self, rows: list[dict], title: str) -> dict | None:
        """按标题前缀找回同一行（面板重排/刷新后按钮文案会变）"""
        key = (title or "")[:6]
        if not key:
            return None
        for r in rows:
            if (r.get("title") or "")[:6] == key:
                return r
        return None

    def bring_wallet_front(self) -> None:
        """从试用 App 切回钱包。

        必须用 LAUNCHER 启动而不是 app_start(force_stop=True)：
        钱包的 H5 活动页状态保存在进程里，进程被清掉就得重新走一遍入口。
        """
        shell("monkey -p {} -c android.intent.category.LAUNCHER 1".format(
            self.cfg["package"]))
        for _ in range(10):
            nap(0.8)
            if self.current_package().startswith(
                    self.cfg["package"].rsplit(".", 1)[0]):
                nap(2.0)
                return
        log("  切回钱包超时")

    def handle_app_dialogs(self, keywords: list[str], seconds: float = 25.0,
                           max_taps: int = 6) -> int:
        """处理试用 App 的一次性弹窗（隐私协议、通知权限等）

        只看前台不在钱包时生效；点到关键词就继续，点不到就退出，不硬循环。
        """
        guard = self.cfg["package"].rsplit(".", 1)[0]
        deadline = time.time() + seconds
        taps = 0
        while time.time() < deadline and taps < max_taps:
            if self.current_package().startswith(guard):
                return taps
            hit = None
            for kw in keywords:
                hit = self.find_target(kw)
                if hit:
                    break
            if not hit:
                nap(1.2)
                continue
            kw, x, y = hit
            log("  试用 App 弹窗: 点「{}」@({},{})".format(kw, x, y))
            self.tap(x, y)
            taps += 1
            nap(2.0)
        return taps

    def trial_download(self, title: str, step: dict) -> bool:
        """点「去下载」，等后台下载+静默安装完成（按钮变成「去试用」）"""
        timeout = float(step.get("install_timeout", 420))
        rows = self.panel_rows()
        row = self.find_row(rows, title)
        if not row:
            log("  找不到试用行: {}".format(title))
            return False
        log("  「{}」点「{}」@({},{})".format(title, row["btn"], row["x"], row["y"]))
        self.tap(row["x"], row["y"])

        # 第一次点「去下载」会弹一个「任务说明」浮层，真正的下载按钮在里面，
        # 只点第一层什么都不会发生（这是最容易卡死的地方）
        for _ in range(3):
            nap(2.0)
            hit = self.find_target("立即下载")
            if not hit:
                break
            log("  任务说明弹窗: 点「{}」@({},{})".format(*hit))
            self.tap(hit[1], hit[2])
            nap(1.5)

        start = time.time()
        last = ""
        while time.time() - start < timeout:
            nap(4.0)
            rows = self.panel_rows()
            row = self.find_row(rows, title)
            if not row:
                continue  # 安装过程中面板会短暂重绘/缺行，属正常
            btn = row["btn"]
            if btn != last:
                log("  下载进度: {}".format(btn))
                last = btn
            if any(k in btn for k in ("试用", "打开", "领取奖励")):
                log("  下载安装完成: {}".format(btn))
                return True
            # 安装确认 / 权限这类系统弹窗（部分 App 会弹）
            pkg = self.current_package()
            if pkg and "packageinstaller" in pkg:
                for kw in ("继续安装", "安装", "允许", "确定"):
                    hit = self.find_target(kw)
                    if hit:
                        log("  安装确认弹窗: 点「{}」".format(kw))
                        self.tap(hit[1], hit[2])
                        nap(2.0)
                        break
        log("  等待下载安装超时({}s)，最后状态: {}".format(int(timeout), last))
        self.shot("trial_download_timeout")
        return False

    def close_trial_hint(self) -> None:
        """关掉「今日试用已完成 / 明日再来」倒计时框"""
        if self.click_any(["关闭提醒我也能来", "关闭提醒", "暂不提醒"], timeout=2.0):
            nap(1.5)
            return
        self.tap(self.width * 0.5, self.height * 0.798)
        nap(1.5)

    def trial_launch_and_browse(self, title: str, step: dict) -> bool:
        """点「去试用」→ 处理弹窗 → 浏览 → 回钱包"""
        dia_kws = step.get("dialog_keywords") or [
            "同意", "始终允许", "仅在使用中允许", "允许", "我知道了", "跳过", "以后再说"]
        browse = float(step.get("browse_seconds", 35))

        rows = self.panel_rows()
        row = self.find_row(rows, title)
        if not row:
            return False
        log("  「{}」点「{}」@({},{})".format(title, row["btn"], row["x"], row["y"]))
        self.tap(row["x"], row["y"])

        # 等试用 App 起来
        guard = self.cfg["package"].rsplit(".", 1)[0]
        got = False
        for _ in range(15):
            nap(1.0)
            # 「今日试用已完成」：今天的次数用完，弹倒计时框，不会再跳 App
            if self.find_target("今日试用已完成") or self.find_target("明日再来可领"):
                log("  今天这一步试用已经做过了，关掉提示")
                self.close_trial_hint()
                return True
            pkg = self.current_package()
            if pkg and not pkg.startswith(guard):
                got = True
                break
        if not got:
            log("  没跳到试用 App，放弃这一行")
            self.shot("trial_no_jump")
            return False
        nap(3.0)
        log("  已进入试用 App: {}".format(self.current_package()))
        self.handle_app_dialogs(dia_kws, seconds=float(step.get("dialog_timeout", 25)))

        # 浏览：分几次滑动，凑够时长
        end = time.time() + browse
        n = 0
        while time.time() < end:
            self.swipe(self.width * 0.5, self.height * 0.75,
                       self.width * 0.5, self.height * 0.30, 500)
            n += 1
            nap(max(1.0, min(8.0, (end - time.time()) / 3)))
        log("  浏览 {}s（滑动 {} 次）".format(int(browse), n))

        self.key("3")  # 回桌面
        nap(2.0)
        self.bring_wallet_front()
        nap(3.0)
        return True

    def action_trial_tasks(self, step: dict) -> bool:
        """逐个处理任务面板里的试用任务

        面板每天不一样：连续2天试用是固定的，试用30秒是偶尔出现。
        所以按行扫描，能点的就做，做完再扫一遍，直到没有可动的行为止。
        """
        rounds = int(step.get("rounds", 4))
        if not self.open_task_panel():
            log("  任务面板打不开")
            self.shot("panel_missing_trial")
            return False

        def phase_of(btn: str) -> str:
            """按钮所属阶段：同一行会依次经过 下载 -> 试用 -> 领取"""
            if "领取" in btn and "去领取" not in btn:
                return "claim"
            if any(k in btn for k in ("试用", "打开", "体验", "去玩", "去听", "去阅读")):
                return "launch"
            if "下载" in btn or "安装" in btn:
                return "download"
            return "other"

        handled = 0
        # 按「标题+阶段」去重：同一行的不同阶段必须都能走到，
        # 只按标题去重会把「下载完变试用30s」这一步直接挡掉（今天踩到）
        touched: set[tuple[str, str]] = set()

        def fresh(r: dict, phase: str) -> bool:
            return (r["title"][:6], phase) not in touched

        for i in range(1, rounds + 1):
            rows = self.panel_rows()
            if not rows and (self.find_target("今日试用已完成")
                             or self.find_target("明日再来可领")):
                log("  弹「今日试用已完成」倒计时框，关掉后重读面板")
                self.close_trial_hint()
                nap(1.2)
                self.open_task_panel()
                rows = self.panel_rows()
            log("  第 {} 轮面板: {}".format(
                i, ["{}|{}".format(r["title"][:8], r["btn"]) for r in rows]))
            claim = next((r for r in rows if phase_of(r["btn"]) == "claim"
                          and fresh(r, "claim")), None)
            if claim:
                log("  领奖: 「{}」".format(claim["title"][:10]))
                self.tap(claim["x"], claim["y"])
                touched.add((claim["title"][:6], "claim"))
                nap(3.0)
                handled += 1
                continue
            launch = next((r for r in rows if phase_of(r["btn"]) == "launch"
                           and fresh(r, "launch")), None)
            if launch:
                touched.add((launch["title"][:6], "launch"))
                if self.trial_launch_and_browse(launch["title"], step):
                    handled += 1
                    self.open_task_panel()
                else:
                    break
                continue
            down = next((r for r in rows if phase_of(r["btn"]) == "download"
                         and fresh(r, "download")), None)
            if down:
                touched.add((down["title"][:6], "download"))
                if self.trial_download(down["title"], step):
                    handled += 1
                    self.open_task_panel()
                else:
                    break
                continue
            log("  面板里没有可执行的试用任务")
            break
        else:
            # 循环跑满还没 break，说明还有活没干完（通常是"下载完等着去试用"）
            log("  ⚠ 达到轮次上限 {}，可能还有未完成的试用任务（见下次截图）".format(rounds))
            self.shot("trial_rounds_exhausted")

        # 收尾：把"刚下载完停在去试用"和"刚试完停在领取奖励"的行都走完。
        # 原来这里只领「领取」，于是 11-11 那条下载完的「试用阿里巴巴30秒」
        # 就一直停在「去试用」，2 天奖励当天白丢。
        for _ in range(3):
            rows = self.panel_rows()
            if not rows and (self.find_target("今日试用已完成")
                             or self.find_target("明日再来可领")):
                self.close_trial_hint()
                nap(1.2)
                self.open_task_panel()
                rows = self.panel_rows()
            claim = next((r for r in rows if phase_of(r["btn"]) == "claim"
                          and fresh(r, "claim")), None)
            if claim:
                log("  收尾领奖: 「{}」".format(claim["title"][:10]))
                self.tap(claim["x"], claim["y"])
                touched.add((claim["title"][:6], "claim"))
                nap(3.0)
                handled += 1
                continue
            launch = next((r for r in rows if phase_of(r["btn"]) == "launch"
                           and fresh(r, "launch")), None)
            if launch:
                log("  收尾试用: 「{}」".format(launch["title"][:10]))
                touched.add((launch["title"][:6], "launch"))
                if self.trial_launch_and_browse(launch["title"], step):
                    handled += 1
                    self.open_task_panel()
                    continue
            break

        log("  试用任务共处理 {} 步".format(handled))
        self.shot("trial_done")
        return handled > 0

    def find_target(self, keyword: str) -> tuple[str, int, int] | None:
        """按 text / content-desc 模糊匹配，返回 (关键字, 中心x, 中心y)

        自己算坐标 + root 注入，而不是用 uiautomator2 的 el.click()：
        本机 shell 没有 INJECT_EVENTS，且钱包大量控件只有 content-desc。
        """
        hit = self._find_target_once(keyword)
        if hit:
            return hit
        # 读不到时多半是 WebView 把节点折叠了，唤醒后再试一次
        if self._wake_nodes():
            return self._find_target_once(keyword)
        return None

    def _find_bounds(self, keyword: str):
        """按 text / content-desc 找节点，返回 (x1, y1, x2, y2)；找不到返回 None"""
        safe = keyword.replace('"', "").replace("'", "")
        if not safe:
            return None
        for attr in ("text", "content-desc"):
            expr = '//*[contains(@{}, "{}")]'.format(attr, safe)
            try:
                elements = self.d.xpath(expr).all()
            except Exception:
                continue
            for el in elements:
                m = BOUNDS_RE.search(el.attrib.get("bounds", ""))
                if not m:
                    continue
                x1, y1, x2, y2 = (int(v) for v in m.groups())
                if x2 - x1 < 8 or y2 - y1 < 8:
                    continue
                if y2 <= 110 or y1 >= self.height - 40:
                    continue
                return x1, y1, x2, y2
        return None

    def _find_target_once(self, keyword: str) -> tuple[str, int, int] | None:
        safe = keyword.replace('"', "").replace("'", "")
        if not safe:
            return None
        for attr in ("text", "content-desc"):
            expr = '//*[contains(@{}, "{}")]'.format(attr, safe)
            try:
                elements = self.d.xpath(expr).all()
            except Exception:
                continue
            for el in elements:
                m = BOUNDS_RE.search(el.attrib.get("bounds", ""))
                if not m:
                    continue
                x1, y1, x2, y2 = (int(v) for v in m.groups())
                if x2 - x1 < 8 or y2 - y1 < 8:
                    continue
                # 避开状态栏（含系统通知，例如「日历通知：…点击领取视频会员」，
                # 之前误点过它）和底部导航栏
                if y2 <= 110 or y1 >= self.height - 40:
                    continue
                return keyword, (x1 + x2) // 2, (y1 + y2) // 2
        return None

    def click_any(self, keywords: list[str], timeout: float = 4.0,
                  desc: str = "") -> str | None:
        """在 timeout 内依次尝试命中并点击，返回命中的关键字"""
        if not keywords:
            return None
        deadline = time.time() + max(timeout, 0.1)
        while True:
            for kw in keywords:
                hit = self.find_target(kw)
                if hit:
                    _, x, y = hit
                    log("  命中「{}」@({},{})".format(kw, x, y))
                    self.tap(x, y)
                    return kw
            if time.time() >= deadline:
                return None
            nap(0.6)

    # ---------- 动作 ----------
    def action_ad(self, step: dict) -> bool:
        """等待激励视频播放结束，然后点掉关闭/跳过"""
        max_s = float(step.get("max_seconds", 100))
        close_kws = step.get("close_keywords") or ["关闭", "跳过", "×"]
        # 开头这段时间不点关闭：广告页刚加载时点「关闭」很容易点到广告本身
        grace = float(step.get("grace_seconds", 8))
        start = time.time()
        saw_countdown = False
        last_cd = None
        while time.time() - start < max_s:
            cd = self.find_countdown()
            if cd:
                saw_countdown = True
                if cd != last_cd:
                    log("  广告倒计时: {}".format(cd))
                    last_cd = cd
                nap(1.1)
                continue
            if time.time() - start >= grace and self.click_any(close_kws, timeout=1.2):
                log("  已关闭广告页")
                nap(1.5)
                return True
            nap(1.0)
        log("  广告等待超时（{}s，见过倒计时={}）".format(int(max_s), saw_countdown))
        return saw_countdown

    def run_step(self, index: int, step: dict) -> None:
        act = step.get("do", "click")
        desc = step.get("desc") or act
        optional = bool(step.get("optional", True))
        log("步骤 {}: {}".format(index, desc))
        ok = True

        if act == "click":
            texts = step.get("texts", [])
            if any(k in t for t in texts for k in ("领视频会员", "会员中心")) \
                    and self.on_activity_page():
                # 已经在活动页了，再去点首页入口会误点到活动页里的大按钮（跑飞过）
                log("  已在活动页，跳过首页入口点击")
                hit = None
                ok = True
            else:
                hit = self.click_any(texts, step.get("timeout", 6), desc)
                ok = hit is not None
                log("  命中: {}".format(hit) if hit else "  未找到目标元素")
        elif act == "widget":
            ok = self.launch_from_widget(cold=bool(step.get("cold", False)))
        elif act == "ad":
            ok = self.action_ad(step)
        elif act == "ad_tasks":
            ok = self.action_ad_tasks(step)
        elif act == "trial_tasks":
            ok = self.action_trial_tasks(step)
        elif act == "sleep":
            nap(float(step.get("seconds", 1)))
        elif act == "back":
            self.key("4")
            nap(float(step.get("seconds", 1.2)))
        elif act == "home":
            self.key("3")
        elif act == "swipe":
            w, h = self.width, self.height
            direction = step.get("direction", "up")
            if direction == "up":
                self.swipe(w * 0.5, h * 0.8, w * 0.5, h * 0.35, 300)
            elif direction == "down":
                self.swipe(w * 0.5, h * 0.35, w * 0.5, h * 0.8, 300)
            nap(float(step.get("seconds", 1.0)))
        elif act == "tap":
            # 坐标兜底：纯图标按钮没有文字节点时用，坐标是物理像素
            point = step.get("point") or [0, 0]
            log("  点击坐标 ({}, {})".format(point[0], point[1]))
            self.tap(point[0], point[1], jitter=int(step.get("jitter", 0)))
            nap(float(step.get("seconds", 1.2)))
        elif act == "shot":
            self.shot(desc)
        elif act == "launch":
            self.d.app_start(self.cfg["package"], wait=True)
        else:
            log("  未知动作类型: {}".format(act))

        if not ok and not optional:
            self.shot("fail_step{}".format(index))
            raise RuntimeError("关键步骤失败: {}".format(desc))
        nap(float(step.get("post_sleep", 0.8)))

    # ---------- 主流程 ----------
    def run(self) -> tuple[bool, str]:
        cfg = self.cfg
        self.connect()
        self.wake()

        # 没网的话后面全是白跑：活动页白屏、宝箱点不出来、最后报"未检测到成功标识"
        retries = int(cfg.get("network_retries", 3))
        settle = float(cfg.get("network_settle", 25))
        if not self.ensure_network(retries=retries, settle=settle):
            self.shot("no_network")
            raise NetworkUnavailable(
                "手机无网络，重启 WiFi {} 次仍未恢复，放弃本轮".format(retries))

        try:
            self.d.app_start(cfg["package"], wait=True)
            log("已启动 {}".format(cfg["package"]))
        except Exception:
            # 退回 adb 方式启动，避免 atx-agent 掉线时整轮失败
            shell("monkey -p {} -c android.intent.category.LAUNCHER 1".format(cfg["package"]))
            log("已通过 adb monkey 启动 {}".format(cfg["package"]))
        nap(2.0)

        self.keep_awake(True)
        try:
            for i, step in enumerate(cfg["steps"], 1):
                self.run_step(i, step)
        finally:
            self.keep_awake(False)

        # 校验结果：广告任务流程自己会给出结论
        if self.ad_tasks_ok is True:
            return True, "广告任务已处理完（面板已无广告任务）"
        screen = self.texts()
        joined = " | ".join(screen)
        for kw in cfg.get("success_keywords", []):
            if any(kw in t for t in screen):
                return True, "命中成功标识: {}".format(kw)
        if not screen:
            # 界面读不到文字时，用截图兜底人工确认
            self.shot("verify_empty")
        return False, "未检测到成功标识，界面文本: {}".format(joined[:300])

    def close(self) -> None:
        try:
            if self.d:
                self.key("3")
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="忽略当天已完成的标记")
    parser.add_argument("--sweep", action="store_true",
                        help="补检模式：今天广告还没做满 ad_target 条就再跑一轮，满了直接跳过")
    parser.add_argument("--dry", action="store_true", help="只连接并打印界面文本")
    args = parser.parse_args()

    global HUMANIZE

    init_dirs()
    cfg = load_config()
    HUMANIZE = bool(cfg["humanize"])
    log("=" * 60)
    log("开始执行，模式: {}，拟人化: {}".format(
        "sweep" if args.sweep else ("dry" if args.dry else "run"),
        "开" if HUMANIZE else "关"))

    state = read_state()
    today = dt.date.today().isoformat()
    # 广告条数按天累计：广告是分批下发的，单轮做不完，
    # 需要多轮补检才能凑满目标条数（见 ad_target）
    target = int(cfg.get("ad_target", 3))
    if state.get("ads_date") != today:
        state["ads_date"] = today
        state["ads_done"] = 0
    ads_done = int(state.get("ads_done") or 0)

    if args.sweep:
        if ads_done >= target:
            log("补检: 今日广告已做满 {}/{}，今天不再检查".format(ads_done, target))
            return 0
        log("补检: 今日广告 {}/{}，继续找剩下的 {} 条".format(
            ads_done, target, target - ads_done))
    elif cfg["skip_if_done"] and not args.force and state.get("last_success") == today:
        log("今天已成功领取过（{}），跳过。用 --force / --sweep 强制重跑。".format(today))
        return 0

    bot = WalletBot(cfg)
    bot.sweep = bool(args.sweep)
    if args.dry:
        try:
            bot.connect()
            bot.wake()
            log("当前页面: {}".format(bot.d.app_current()))
            for t in bot.texts():
                log("  text: {}".format(t))
            bot.shot("dry")
        finally:
            bot.close()
        return 0

    success = False
    reason = ""
    attempts = max(1, int(cfg["max_retry"]))
    for attempt in range(1, attempts + 1):
        log("第 {}/{} 次尝试".format(attempt, attempts))
        try:
            success, reason = bot.run()
            if success:
                break
            log("结果不理想: {}".format(reason))
        except EnvError as exc:
            # 环境问题重试无用（没设备/未授权/offline 都要人去处理），
            # 立刻写清原因并退出，不要再静默穿透。
            reason = str(exc)
            log("环境问题: {}".format(reason))
            shot_list = "\n".join(str(p) for p in bot.shots[-2:])
            notify(cfg, "小米钱包每日领取失败（环境问题）",
                   "{}\n截图: {}\n处理完可直接手动跑一次，或等明天计划任务。".format(
                       reason, shot_list or "无"))
            return 4
        except NetworkUnavailable as exc:
            # 网络重试已经做过（不止一次重启 WiFi），再重跑整轮只是重复消耗时间，
            # 直接退出并推送告警，等人处理。
            reason = str(exc)
            log("网络不可用: {}".format(reason))
            log("已重试 {} 次重启 WiFi，退出本次执行".format(cfg.get("network_retries", 3)))
            shot_list = "\n".join(str(p) for p in bot.shots[-2:])
            notify(cfg, "小米钱包每日领取失败（手机无网络）",
                   "{}\n截图: {}\n请检查手机 WiFi 或路由器，恢复后手动跑一次。".format(
                       reason, shot_list or "无"))
            return 3
        except Exception as exc:
            reason = str(exc)
            log("异常: {}".format(reason))
            log(traceback.format_exc())
        finally:
            bot.close()
        if attempt < attempts:
            log("30 秒后重试")
            nap(30)

    if success:
        got = int(getattr(bot, "ads_done", 0) or 0)
        state["ads_date"] = today
        state["ads_done"] = ads_done + got
        state["last_success"] = today
        state["last_run"] = dt.datetime.now().isoformat(timespec="seconds")
        write_state(state)
        log("执行成功: {}".format(reason))
        log("今日广告进度: {}/{}（本次 {} 条）".format(
            state["ads_done"], target, got))
        if args.sweep and state["ads_done"] < target:
            log("还没做满 {} 条，下一轮补检会继续找".format(target))
        notify(cfg, "小米钱包每日领取成功", "{}\n时间: {}\n今日广告: {}/{}".format(
            reason, dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            state["ads_done"], target))
        return 0

    log("执行失败: {}".format(reason))
    shot_list = "\n".join(str(p) for p in bot.shots[-3:])
    notify(cfg, "小米钱包每日领取失败", "{}\n截图:{}\n请检查 config.json 里的界面关键字。".format(
        reason, shot_list or "无"))
    return 2


if __name__ == "__main__":
    sys.exit(main())
