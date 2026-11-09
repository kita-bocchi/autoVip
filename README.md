# 小米钱包「看广告领视频会员」每日自动执行器

电脑通过 USB/adb 驱动一台闲置小米手机，每天自动完成小米钱包「钱包玩家视界」活动里的
**看广告领会员时长 + 试用 App 领会员**，全程留日志和截图。

> **当前状态**：已在真机上全自动跑通，看广告与试用任务可无人值守完成，单轮约 6 分钟。

一句话原理：**脚本不在手机上跑，手机只是执行终端**。电脑负责调度、判断、重试、取证。

```
07:00 + 0~60min 随机  计划任务（Windows）
         │  wallet_daily.py
         ├─ adb 连手机 → 点亮 → 解锁 → 屏幕常亮
         ├─ 回桌面点「钱包玩家视界」小组件（找不到就回退 App 内入口）
         ├─ 阶段一：活动页大按钮「看10秒」→ 跳广告 → 停留 16~26s（随机）→ 切回 → 开宝箱
         ├─ 阶段二：打开任务面板，做完剩余看广告任务
         ├─ 阶段三：试用任务（下载 → 安装 → 试用 30s → 领奖）
         └─ 校验 → 写 state.json → 截图 + 可选推送
```

---

## 目录

- [适合谁用 / 不适合谁用](#适合谁用--不适合谁用)
- [环境要求](#环境要求)
- [部署五步](#部署五步)
- [一次运行到底做了什么](#一次运行到底做了什么)
- [日常命令](#日常命令)
- [config.json 参考](#configjson-参考)
- [目录结构](#目录结构)
- [排错对照表](#排错对照表)
- [已知限制与风险](#已知限制与风险)
- [给接手的人](#给接手的人)

---

## 适合谁用 / 不适合谁用

**适合**：有一台常年开机的 Windows 电脑 + 一台闲置的小米/红米手机（HyperOS，最好已 root），
愿意一次配置、之后每天只看日志。

**不适合**：想「装了就能跑」的人。这个项目的本质是**跟着 App 改版持续维护**——
按钮文案每隔几天就可能变一种，代码里已经兼容了 4 种，第 5 种出现时仍需人工加关键词。
如果你不能接受每隔一段时间调一次，请直接用「半自动」方案：脚本只负责打开 App 并定位到活动页，
最后 30 秒人工点一下，到账率 100%，维护成本接近 0。

---

## 环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| 电脑 | Windows 10/11，常开 | 睡眠会错过定时任务，建议接电源 |
| 手机 | 小米/红米，HyperOS | 需能打开 USB 调试；有 root 可免去「USB 调试(安全设置)」 |
| 连接 | **能传数据**的 USB 线 | 纯充电线只能充电，adb 看不到设备 |
| Python | 3.10+ | 3.13 已验证 |
| adb | 37.0.1 | 建议放 `tools/platform-tools/`（脚本会优先用它，不改系统 PATH） |
| root | 建议有 | 没有 root 时，`input tap` 需要打开「USB 调试(安全设置)」才不被拒 |

依赖只有 `uiautomator2`（见 `requirements.txt`），adb 由 `adb_env.py` 自动定位。

---

## 部署五步

### 第 1 步：手机设置（唯一必须手动做的一步）

**1.1 打开开发者选项**
设置 → 我的设备 → 全部参数与信息 → 连点「OS 版本」7 次。

> 如果点了没反应：多半是「开发者选项」的 Activity 被精简工具 `pm disable` 掉了。
> root 下执行 `pm enable 'com.android.settings/.Settings$DevelopmentSettingsDashboardActivity'`，
> 重启即可恢复。**之后不要再用「隐藏设置项 / 去广告」这类功能**，否则会被再次禁用。

**1.2 打开三个开关**（设置 → 更多设置 → 开发者选项）

- USB 调试
- USB 安装 ← 首次部署 atx-agent 必需
- USB 调试（安全设置）← 需要登录小米账号；有 root 时可不依赖它

**1.3 去掉锁屏密码**
设置 → 锁屏 → 改为「无密码/滑动解锁」。脚本只能滑动解锁，输不了密码。

**1.4 保活小米钱包**
设置 → 应用设置 → 应用管理 → 小米钱包 → 省电策略「无限制」，并打开「自启动」「后台弹出界面」。

**1.5 对小米钱包隐藏 root**
Magisk → 设置 → 开启 Zygisk → DenyList 勾选 `com.mipay.wallet`（或用 Shamiko 黑名单模式）。
小米钱包是金融 App，检测到 root 会降风控，表现为「点得对但奖励不到账」。

**1.6 插线**
手机弹「允许 USB 调试吗」→ 勾「一律允许」→ 确定。

### 第 2 步：电脑侧装环境

```bat
cd /d <项目目录>
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> 坑：如果系统里同时有多个 Python，**别直接敲 `pip install`**，会装到别的解释器上。
> 一定要用 `.venv\Scripts\python.exe -m pip`。

adb 二选一：
- 把 platform-tools 解压到 `tools\platform-tools\`（推荐，脚本优先用它，不污染系统 PATH）
- 或保证系统 PATH 里已有可用的 `adb`

### 第 3 步：自检

```bat
doctor.bat
```

期望 7 项全绿：

```
[1/7] Python 环境      [OK] Python 3.13.x / uiautomator2 3.x
[2/7] adb              [OK] Android Debug Bridge version 1.0.41
[3/7] 设备连接          [OK] 设备在线: <序列号>
[4/7] 设备信息          [OK] <厂商> / <机型> / Android <版本>
[5/7] 小米钱包          [OK] 已安装: com.mipay.wallet
[6/7] uiautomator2     [OK] 已连接 <宽>x<高>
[7/7] 唤醒与截图        [OK] 截图成功: shots/doctor.png
结论: 全部通过
```

常见卡点：
- `unauthorized` → 解锁手机看授权弹窗，或开发者选项里「撤销 USB 调试授权」后重插
- `offline` → `tools\platform-tools\adb.exe kill-server` 后重插
- 第 6 项失败 → 「USB 安装」没开，atx-agent 装不上
- 第 7 项仍停在锁屏 → 锁屏密码没去干净

### 第 4 步：试跑

```bat
:: 只看现状，不动手（连设备 + 打印界面文字 + 截图）
run.bat --dry

:: 真跑一次（忽略"今天已完成"标记）
run.bat --force
```

跑完看两处：
- `logs\daily_YYYY-MM-DD.log` 的步骤流水
- `shots\` 里带时间戳的截图（每个关键节点都会截图）

### 第 5 步：上计划任务

```bat
setup_task.bat
```

注册任务 `XiaomiWalletDaily`，每天 07:00 之后随机延迟最多 60 分钟启动，用 `pythonw.exe` 静默执行。
脚本本身不睡觉，延迟由任务计划程序的 `RandomDelay` 完成——固定的 07:00:00 准点触发是最明显的时间指纹。

**注意：`schtasks` 注册出来是「就绪」可直接跑；如果任务被停用过，需要显式启用：**

```powershell
Enable-ScheduledTask -TaskName "XiaomiWalletDaily"
Start-ScheduledTask  -TaskName "XiaomiWalletDaily"   # 立刻跑一次验证
```

想改执行时间（例如改成 06:30）：

```powershell
$t = Get-ScheduledTask -TaskName "XiaomiWalletDaily"
$t.Triggers[0].StartBoundary = "2026-01-01T06:30:00"
$t.Triggers[0].RandomDelay   = "PT60M"      # 随机延迟 0~60 分钟，可去掉
Set-ScheduledTask -TaskName "XiaomiWalletDaily" -Trigger $t.Triggers[0]
```

改完用 `Get-ScheduledTaskInfo` 确认 `NextRunTime`（带随机延迟时它会落在整点之后的随机分钟）。

建议在「任务计划程序」里再改两处：勾「不管用户是否登录都要运行」、勾「使用最高权限运行」。
（`schtasks.exe` 在本机曾被安全软件拦截，改用 PowerShell 的 `Set-ScheduledTask` 更稳。）

查看 / 删除：

```powershell
Get-ScheduledTask   -TaskName "XiaomiWalletDaily"
Unregister-ScheduledTask -TaskName "XiaomiWalletDaily" -Confirm:$false
```

---

## 一次运行到底做了什么

入口顺序是 **页面广告 → 面板广告 → 试用**，顺序不能换（面板一展开就会挡住页面大按钮）。

### 阶段 0：网络检查（不通过就直接退出）

活动页是 H5，手机没网时只会白屏。脚本如果硬着头皮往下跑，会老老实实点 6 分钟，
最后报「未检测到成功标识」——既浪费时间，也把「断网」误诊成「界面改版」。

所以每次开跑前先探测外网：

```
ping 223.5.5.5 / 119.29.29.29 / www.baidu.com（任一回包即算在线）
  ├─ 通 → 继续
  └─ 不通 → 关 WiFi → 等 3s → 开 WiFi → 25s 内轮询等恢复
              ├─ 恢复 → 继续
              └─ 未恢复 → 重复，最多 network_retries 轮（默认 3）
                            └─ 全部失败 → 截图 + 推送告警 → 退出码 3，不进入后续流程
```

探测点用「纯 IP + 域名」混合，因为单个 DNS 可能屏蔽 ICMP（实测 `114.114.114.114` 就不回包），
选一个不通就判离线会误杀。

一轮完整的网络重试约 35 秒；3 轮全失败约 100 秒后退出，比空跑 6 分钟划算得多。

### 阶段 1：进活动页

| 步骤 | 动作 | 失败时 |
|---|---|---|
| 冷启动 | `am force-stop com.mipay.wallet`（`cold: true`） | — |
| 回桌面 | `keyevent 3`，在桌面找「钱包玩家视界」小组件，找不到就左滑翻页（最多 3 次） | 走 App 内兜底 |
| 兜底 | monkey 启动钱包 → 点首页「领视频会员」→ 等 H5 加载 | 整轮失败 |
| 回顶 | 活动页会保留上次滚动位置，向上滑 4 次回到顶部 | — |

### 阶段 2：活动页「看10秒」循环

```
找大按钮（含"秒" + 宽度 > 60% 屏宽）
  → 点它
  → 判断落点：第三方 App / 站内「活动指南」页 / 应用商店
  → 停留 `page_watch_seconds` × 1.15~1.90 秒（默认基准 14s，实际 16~26s 随机；
     客户端从跳转瞬间开始计时，不用等广告播完）
  → 切回钱包 → 关掉「桌面入口访问」涂层
  → 点宝箱（Canvas 绘制，无文字节点，按坐标点）→ 「恭喜获得」出现即成功
  → 关奖励弹窗 → 关任务面板 → 找下一条
入口消失 → 冷启动重进 → 等约 24 秒再找 → 仍没有才判定今天没广告了
```

当前配置：最多 4 条（`page_rounds`），最多冷启动 3 次（`page_reenter`）。

### 阶段 3：任务面板里的剩余广告

面板里既有看广告任务，也有试用/邀请任务。按 `watch_keywords` 找按钮、按 `skip_keywords` 跳过，
逐个点完，每轮重新读面板。

### 阶段 4：试用任务

按行扫描，按钮分四个阶段，**同一行必须允许走完所有阶段**：

| 按钮文案 | 阶段 | 动作 |
|---|---|---|
| 去下载 / 立即下载 | download | 点下载 → 等安装（最多 420s）→ 直到按钮变「试用30s」 |
| 试用30s / 去试用 / 打开 / 体验 | launch | 跳目标 App → 处理权限弹窗 → 刷 35 秒 → 回钱包 |
| 领取奖励 | claim | 点领取 |
| 已领奖 / 明天再来 / 已完成 | done | 跳过 |

试用进度是「连续 2 天」型：第 1 天做完变 `1/2 明天再来`，第 2 天做完领 3 天。

### 成功判定

按顺序判断，**不只看有没有点到按钮**：

1. `ad_tasks_ok == True` → 成功
2. 界面命中 `success_keywords`（已领取 / 明日再来 / 明天再来）→ 成功
3. 否则失败，退出码 2，并推送最后 3 张截图

一条广告都没做时不会直接报成功，必须看到主按钮变成「做任务 领时长 / 领会员 / 得时长」
（= 今日额度已用完）才判成功——这是为了避免把「一条没做成」报成成功。

### 退出码

| 码 | 含义 | 怎么处理 |
|---|---|---|
| 0 | 成功，或当天已完成跳过 | 不用管 |
| 2 | 流程跑完但未判定成功（界面改动 / 没领到） | 看 `shots/` 最后几张，对照排错表 |
| 3 | 手机没网络，重启 WiFi 多次仍失败 | 检查手机 WiFi / 路由器，恢复后 `run.bat --force` |

计划任务里「上次运行结果」显示的就是这个码。

---

## 日常命令

| 命令 | 作用 |
|---|---|
| `run.bat` | 正常执行；当天成功过会跳过（幂等） |
| `run.bat --force` | 忽略幂等标记，强制再跑 |
| `run.bat --dry` | 只连接、打印界面文字、截图，不点击 |
| `doctor.bat` | 7 项环境自检 |
| `probe.bat` | 界面探测器：手动操作手机，实时刷出每页的文字与坐标 |
| `act.py open\|dump\|tap 文字\|ad\|shot\|key\|focus` | 单步操作器，复用生产代码路径 |
| `trial_test.py` | 只跑试用任务那一段 |
| `stab.py -n 90 -i 2` | USB/adb 链路压测（换线后验证） |

看结果：

```
logs\daily_<日期>.log          # 当天流水
shots\<时分秒>_<标签>.png      # 每个关键节点都有截图
state.json                    # {"last_success": "<日期>", ...}
```

---

## config.json 参考

### 顶层字段

| 字段 | 默认 | 说明 |
|---|---|---|
| `package` | `com.mipay.wallet` | 小米钱包包名 |
| `serial` | `""` | 只连一台就留空；多台时填 `adb devices` 里的序列号 |
| `device_wait_seconds` | 120 | 等设备上线；超时中途会 `kill-server` 重试一次 |
| `network_check` | true | 开跑前检查手机网络，断了自动重启 WiFi |
| `network_retries` | 3 | 网络重试轮数，全部失败则退出（退出码 3） |
| `network_settle` | 25 | 每轮重启 WiFi 后等待恢复的秒数 |
| `success_keywords` | 已领取/明日再来/明天再来 | 界面命中即判成功 |
| `max_retry` | 2 | 整轮重试次数，**别调大**（风控特征） |
| `skip_if_done` | true | 当天成功过就跳过 |
| `humanize` | true | 等待随机化 + 点击高斯抖动。设为 false 则所有等待和坐标恢复固定值，只用于调试复现 |
| `notify_webhook` | `""` | 企业微信机器人 / Server酱 / pushplus，留空不推送 |
| `panel_debug` | true | 日志里打印面板按钮列表，排错时很有用 |

### step 支持的动作

| `do` | 关键参数 | 说明 |
|---|---|---|
| `widget` | `cold` | 从桌面小组件进活动页；`cold: true` 先杀进程 |
| `ad_tasks` | `rounds` `page_rounds` `page_reenter` `page_watch_seconds` `watch_keywords` `skip_keywords` `close_keywords` `grace_seconds` | 页面广告 + 面板广告全套 |
| `trial_tasks` | `rounds` `install_timeout` `browse_seconds` `dialog_keywords` | 试用任务全套 |
| `click` | `texts` `timeout` | 按文字找元素并点击 |
| `ad` | `max_seconds` `close_keywords` | 等单条广告并关闭 |
| `tap` | `point` `jitter` | 纯坐标点击（纯图标按钮兜底） |
| `sleep` | `seconds` | 等待 |
| `back` / `home` | — | 返回键 / Home 键 |
| `swipe` | `direction` | 上/下滑 |
| `shot` | `desc` | 截图 |
| `launch` | — | 启动目标 App |

`optional` **默认 true（可跳过）**，只有显式写 `"optional": false` 的步骤失败才会终止整轮。
当前只有 `ad_tasks` 是 `false`。

---

## 行为拟人化（`humanize`）

自动化行为里有几类特征是可以被统计出来的，本项目默认把它们打散：

| 特征 | 原实现 | 现在 |
|---|---|---|
| **时间点** | 每天 07:00:00 准点启动 | 任务计划程序 `RandomDelay=PT60M`，07:00~08:00 之间任意时刻 |
| **操作间隔** | `time.sleep(固定值)`，整轮耗时精确到秒 | `nap()`：基准 × 0.90~1.50 随机，整轮时长变成浮动的 4~8 分钟 |
| **观看时长** | 每条广告恰好停 14.0 秒 | `ad_dwell()`：14s × 1.15~1.90，实际 16~26 秒 |
| **点击位置** | `±3px` 均匀抖动（1080 屏上 0.28%） | 高斯分布，σ≈10px，最大 ±21px；真人点击也是聚在中心、偶尔偏一点 |
| **滑动手势** | 永远 300ms 的匀速直线 | 时长 × 0.75~1.45，起止点 ±9px |

调试时把 `humanize` 设为 `false`，等待与坐标全部恢复固定值，方便复现问题。

**改不掉的部分要清楚**：手机上常驻的 `atx-agent`、root 权限、以及 `input tap` 注入事件本身
（`deviceId=-1`、压力恒为 1.0）。任何做了基础检测的 App 都能识别"这台设备上有自动化框架"。
`humanize` 打散的是**行为特征**，不是**环境特征**。所以：别用主账号跑。

---

## 目录结构

| 路径 | 作用 |
|---|---|
| `wallet_daily.py` | 主程序，1418 行，config 驱动的步骤引擎 + 面板/宝箱/试用逻辑 |
| `adb_env.py` | adb 路径自举 + root 转发（`su -c`）+ 设备列表 |
| `config.json` | 全部可调参数与流程 |
| `doctor.py` / `.bat` | 环境自检 |
| `ui_probe.py` / `probe.bat` | 界面探测器，取真实文案与坐标 |
| `act.py` | 单步操作器 |
| `trial_test.py` | 单测试用流程 |
| `stab.py` | USB/adb 压测 |
| `wifi_adb.py` / `.bat` | 无线 adb 切换（本机环境不通，见交接文档） |
| `run.bat` / `setup_task.bat` | 手动执行 / 注册计划任务 |
| `tools/platform-tools/` | 内置 adb 37.0.1（不入库） |
| `.venv/` | 项目虚拟环境（不入库） |
| `logs/` `shots/` `probe/` | 日志、截图、界面快照（不入库） |

---

## 排错对照表

| 现象 | 最可能的原因 | 处理 |
|---|---|---|
| 日志「第 N/3 轮: 手机没有网络」 | 手机 WiFi 断了 / 路由器没网 | 正常会自动关开 WiFi；3 轮后仍失败即退出码 3，`shots/no_network_*.png` 是当时画面 |
| 有网但一直判离线 | 网络屏蔽了 ICMP | 换探测点：改 `NET_PROBES`，或临时 `network_check: false` 跳过 |
| 断网但没触发重启 WiFi | `svc wifi` 被系统禁用 | 日志会有「开关 WiFi 失败」，改用 `cmd wifi set-wifi-enabled enabled` |
| 日志里「桌面上找不到小组件」但流程继续 | 组件不在桌面 / 换设备 | 正常，会自动走 App 内入口；确认下一行是「已从 App 内入口进入活动页」 |
| 只领到 1~2 条广告就结束 | 开完宝箱后面板/涂层挡住大按钮 | 检查 `close_task_panel()` / `dismiss_guide()` 是否被跳过；看 `shots` 里当时截图 |
| 日志「点击后没有跳转，本轮放弃」 | 站内「活动指南」页无障碍树为空 | 已用「大按钮消失」做双判据，若仍复现，看截图确认文案 |
| 一条没做成但退出码 0 | 假成功 | 核对会员时长是否增长；查 `ad_none_not_finished` 截图是否存在 |
| 「没看到宝箱（可能已自动入账）」 | 坐标点偏 / 弹窗高度变了 | 用 `shots` 里截图量新坐标，加到 `open_treasure_box()` 候选里 |
| 试用只下载不试用 | 阶段去重粒度错 | 确认 `touched` 按 `(标题, 阶段)`；见交接文档「坑 5」 |
| 点击无反应 / `SecurityException` | shell 无 INJECT_EVENTS | 开「USB 调试(安全设置)」或用 root（脚本会自动 `su -c`） |
| 读不到任何文字（截图正常） | WebView 无障碍节点折叠 | 代码有 `_wake_nodes()` 自动唤醒；无效时手动大幅滑动再滑回 |
| 手机重启后失效 | USB 授权丢失 | 重插授权；不要用 `adb tcpip` 无线调试 |
| 计划任务没跑 | 任务是 Disabled / 未登录 | `Enable-ScheduledTask`，并勾「不管用户是否登录都运行」 |

---

## 已知限制与风险

**技术限制**

- 活动页文案已知 4 种变体，出现第 5 种时 `on_activity_page()` / `find_ad_entry()` 可能判不出。
- 宝箱是 Canvas 绘制，没有文字节点，只能按坐标点；布局大改时 5 个候选可能全落空
  （不会崩，日志会记「没看到宝箱」）。
- 每天广告条数与奖励不固定（1~3 条不等），脚本只能"尽力做完"。
- 网络检查只判断"能不能访问公网"，识别不了需要网页登录的认证热点（能 ping 通但打不开活动页）。
- 试用任务依赖目标 App 的启动流程（首次启动常有多层协议/权限弹窗），App 大改版会卡住。

**风险提醒（请认真读）**

- 自动化点击违反活动规则，查实可取消资格、清空时长。账号与小米支付、NFC 门禁绑定，
  封号代价远大于省下的会员费。
- 广告不计费会引发广告主投诉，活动方会持续加校验（互动题、随机停留），维护成本只升不降。
- 保持 `skip_if_done: true`、`max_retry: 2`，不要写死循环重试——那是风控最容易抓的特征。
- 折中方案始终存在：只自动打开 + 定位活动页，最后 30 秒人工点，到账率 100%。

**使用本项目的风险由使用者自行承担。**

---

## 给接手的人

**先读 `HANDOFF.md`**。里面写了：已解决的 14 个问题及其根因、还剩哪些坑、
绝对不能踩的 9 条、换新账号时的完整调试步骤。

快速上手路径：

1. `HANDOFF.md` 的「五分钟必知事实」—— 记住 5 个坐标和 4 种文案
2. `HANDOFF.md` 的「代码地图」—— 知道改哪个函数
3. 出问题时先看 `logs/` 最后 30 行，再对照「坑」清单
