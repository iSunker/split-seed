#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
反馈驱动循环 —— 自动续跑 cross-seed 搜索，无需人盯。

原理
----
每次跑一批 `drive --limit N --apply`，跑完读 `DriveStats` 的三个信号：
  * `still_skipped > 0` 或 `backoff_hits > 0`  → 站点在退避/限流 → 下次间隔拉长
    ★ 但退避**按实际等待时长分级**（见 next_sleep）：站点打 1 分钟喷嚏不该罚 2 小时
  * `newly_seeding` 稳定产出、无退避           → 站点健康 → 下次间隔收紧
  * `aborted`（> --max-wait 提前中止）         → 休整 + 间隔拉满
    ★ 但它有**两种性质完全不同**的来源，看 `aborted_kind`（见 DriveStats）：
      站点退避超时是**良性**（剩下的条目下轮重排），webhook 鉴权/路径才是真故障。
      两者分开计数、分开报警 —— 见 update_abort_streak（#45）。
然后在 DC / FRDS / (MBF) 三个包之间轮流推进。
一批做完重跑同一条命令即可推进 —— 本脚本把「重跑」自动化了。

用法
----
  python scripts/drive-loop.py --once              # 只跑一轮（当前包下一批）
  python scripts/drive-loop.py                      # 循环跑（默认）
  python scripts/drive-loop.py --max-rounds 3       # 最多 3 轮后退出
  python scripts/drive-loop.py --pack dc-collection # 只跑指定包

参数（除包与循环控制外，其余透传给 drive）
  --url / --api-key / --db-path 必需（与 drive 一致；--api-key 可省略→读 .env）
  --indexers HDFans,NanyangPT   参与搜索的站（逗号分隔）
  --limit 50                    每批条数
  --interval / --check-every / --max-wait / --settle 透传

调度（2026-09-11 起跑在 NAS 上）
--------------------------------------------------------
NAS 侧用 DSM 任务计划，每 15 分钟唤醒一次：
  控制面板 → 任务计划 → 用户定义的脚本，用户选 root，频率「每 15 分钟」
  脚本 = sh <compose>/drive-loop/run.sh   （<compose> = NAS compose 目录，见下方 CROSSSEED_DIRS 段）

`--once` 模式下每次唤醒只跑一批。批间隔 = `max(--min-sleep, 上一批 next_sleep 的结论)`，
由 `.drive-loop.state` 的 `last_sleep_sec` 跨进程传递 —— 于是「站点在退避就缓一缓」
真的会生效，而不只是往日志里写一行字（2026-09-12 之前就是那样，见 once_round）。

★★ 电脑端（Windows）已于 2026-09-12 退役，配置写操作现走 SSH 直推（旧 deploy.sh 已删；
   见 README「电脑端已不参与」）。
   下面这两条是**当时**为什么要搬走的记录 —— 不是待办，是历史，别再照着它去配 Windows 任务。

★ 为什么当初要搬到 NAS（原本是 Windows 计划任务）—— 两个独立的坑，见 SUMMARY §14：
  ① Windows 任务计划的 <StopOnIdleEnd>true（默认配置，不是代码问题）：
     你一动鼠标/键盘就**直接 TerminateProcess 整个任务实例**，
     表现是批次「凭空消失」：没有 traceback、没有 finally 收尾、状态文件里
     running_pid 永远挂着，之后每轮都空转。
  ② 包装器 drive-loop-once.cmd 曾是 LF 行尾：cmd.exe 按字节块读批处理文件，
     LF-only 会让它从某行中间开始执行（REM 注释的单词被当命令跑），
     set 的变量全丢 → 留下 exit=9009 且**有 exit= 没有 start=**。已修成 CRLF。
     （该 .cmd 已于 2026-09-12 删除 —— 退役的不是"修好它"，是"不再需要它"。
       所以别看到"已修成 CRLF"就以为那个文件还在。）
  根因与完整证据见 README「把调度挂到 NAS 上」+ SUMMARY §14。
"""
# ★ 必须放在所有其它 import 之前 —— 这行是 Python 3.10 以下能跑起来的前提。
#   本文件通篇用 `str | None` / `list[str]` 这种写法，它们在 <3.10 上会在
#   **函数定义时**当场求值并抛 TypeError（不是等到调用）。PEP 563 让所有注解
#   退化成字符串、不求值，于是 3.8 也能正常导入。
#   ★ NAS 上系统 python3 就是 3.8.15（DSM 自带，没有更新的）——
#     所以这不是「顺手加的好习惯」，是运行前提：去掉它 drive-loop 在 NAS 上
#     会在 import 阶段就崩，且**连一行日志都写不出来**（崩在 logging 配置之前）。
#   本项目的 state.py / reseed-state.py 早就带了这行，这里补齐。
from __future__ import annotations

import argparse
import json
import logging
import os
import re
# [电脑端已退役 2026-09-12] subprocess 原先只给 Windows 的 tasklist 分支用，
# 那支已注释掉（见 pid_alive）。
# ★ 2026-09-12 恢复启用：现在由**农场巡检** `check_farm()` 用（起 build-farm.sh --verify）。
import subprocess
import sys
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
# ★★ 2026-10-05（B2，data/ 收拢）：`ROOT` 不再简单等于 `HERE.parent`。
#   背景：`orchestrator` 包在**本机仓库**里是 `data/orchestrator/`，而本文件在
#   `data/drive-loop/scripts/` ⇒ 二者**不再是兄弟**（`HERE.parent` 会指到
#   `data/drive-loop/`，那里没有 orchestrator）。而在**镜像**里两者是 `/app/orchestrator/`
#   与 `/app/scripts/` —— 又是兄弟。
#   ⇒ 判据改成「**向上找到含 `orchestrator/` 的那一层**」：本机与镜像都对，
#     且以后无论把代码放多深都不必再改这一行。
def _find_root() -> Path:
    for cand in (HERE.parent, HERE.parent.parent, HERE.parent.parent.parent):
        if (cand / "orchestrator" / "__init__.py").is_file():
            return cand
    return HERE.parent          # 兜底：找不到就退回原行为（导入会自己报错）
ROOT = _find_root()
sys.path.insert(0, str(ROOT))

# --------------------------------------------------------------------------- #
# ★★ 2026-10-04：运行时目录「**可注入**」（为 `data/` 收拢 + 代码入镜像）
# --------------------------------------------------------------------------- #
# 背景：本次重构把 drive-loop 的**代码搬进镜像**（`/app/…`），只把**运行时状态**
#   与**配置**留在挂载卷里。于是 `__file__` 所在目录（`HERE`）**不再是**运行时数据目录
#   —— 它变成只读的镜像路径。若继续用它写 `.state`/日志，容器一重建就全丢，
#   跨进程互斥闸门（`.drive-loop.state` 的 pid/心跳）也会失明。
#
# ⇒ 拆出一个**可注入**的状态目录（有默认回退 ⇒ 本机/旧部署行为**逐字不变**）：
#     · `STATE_DIR` —— 6 个 `.state` 文件 + `drive-loop.log` 的落点。
#       植入方式：环境变量 `DRIVE_STATE_DIR`（compose 里挂到 `/state`）。
#
# ★ 为什么**必须**给回退值：本机（Windows）跑测试、或旧镜像没设环境变量时，
#   行为要与改动前**逐字相同**（`tests/test_drive_loop_service.py` 等钉着这些路径）。
STATE_DIR = Path(os.environ.get("DRIVE_STATE_DIR") or HERE).resolve()


from orchestrator import state as S  # noqa: E402

# 通知（**已砍掉** 2026-10-04，随后 2026-10-05 连"挂掉开关"的残留也清干净）。
# 原先这里 `import notify`，把纯文本事件写进 NAS 本机的 spool，再由 NAS 侧
# `notify-spool.sh` 读 DSM 自己的 SMTP 发信。**该模块整体迁去独立项目**
# （`D:\Projects\nas里pt相关程序邮件通知告警项目\reseed-toolkit`）。
# ⇒ 本文件**不再 import notify**，也不再有任何 spool 写侧：`emit()` /
#   `init_notifier()` / `reset_notify_count()` / `--notify*` 参数**全部删除**。
# ★ 残留的"告警语义"仍在日志里（`LOG.warning/error`）—— 那部分**不删**：
#   它们是跑批自己的诊断输出，与"发不发信"无关。
# ★ 想恢复邮件：去独立项目接 `emit`，不要在本仓复活 spool 写侧。

LOG = logging.getLogger("drive-loop")

# sidecar 状态库默认位置（与 reseed-state.py 的 DEFAULT_DB 保持一致）。
# ★★ 2026-10-05（B2，代码入镜像）：与 `STATE_DIR` 同一套约定 ——
#   ① RESEED_STATE_DB（显式全路径）最优先；② 否则 DRIVE_STATE_DIR/hlink/state.db；
#   ③ 都没有才回退 ROOT/hlink/（本机开发/旧行为）。ROOT 在镜像里是 /app ⇒ 反推必错。
DEFAULT_DB = os.environ.get("RESEED_STATE_DB") or (
    str(STATE_DIR / "hlink" / "state.db") if os.environ.get("DRIVE_STATE_DIR")
    else str(ROOT / "hlink" / "state.db")
)

#: ★★★ **2026-10-05「去掉包的闸门」（用户拍板）**：这里原本是一个模块常量
#:   `PACKS_DEFAULT`（`--packs` 的默认名单，一度是 `frds-top250-2024`），
#:   外加 compose 的 `DRIVE_PACKS` 覆盖口 —— **已整体退休**。
#:
#:   为什么要退休（用户原话）：「包只是个框架范围，把里面的全部单片硬链接到文件夹
#:   里面，每一片做个登记就好了…每一片搜索过几次，记录下来，每个站搜索到一定数量
#:   还匹配不上就移动到 14 天搜一次的慢队列去」。
#:   **核查结论：这套流水线系统里已经全都有了** ——
#:     · 包 = 范围声明（`pack` 表）　· 单片进农场 + 每片一行（`movie` 表）
#:     · 匹配 → `SEEDING`/`MATCHED` → `DONE_STAGES` → 永久出待搜（`todo()`）
#:     · 每片搜索次数、每片每站最后搜索时间（`movie.attempts` / `indexer_seen`）
#:     · **14 天慢档 = `DEFAULT_CADENCE_DAYS`（`state.py:116`）**：搜过没命中 →
#:       `UNMATCHED` → 按**片×站**记时间，够 14 天（或新增站点）才重新入队。
#:   **唯一的问题**是驱动循环按"名单"逐个包轮换：**不在名单里的包 `todo(pack)`
#:   根本不被调用** ⇒ 里面的片"连够没够 14 天都不会被问一次" ⇒ `dc-collection`
#:   /`mbf` 被**冻住**（不是变慢，是停电）。2026-09-21 把它们移出名单读到的现象，
#:   正是"包不驱动 = 永久不搜"。⇒ 去掉闸门：池子扫 **`pack` 表全部登记的包**。
#:
#:   ★ 旧注释里那段"名单顺序 = 偷偷改下一批跑谁"（`last_pack_idx` 下标坑）
#:     连同那段 2026-09-13「为什么把 mbf 排进来」的实验理由，**一并删除** ——
#:     它们描述的是一个**已经不存在的机制**（`b.10`：理由也有保质期）。
#:   ★ 那两段里仍然**成立**的一条教训，已移到 `pool_packs()` 的 docstring：
#:     "原始串 vs 包名列表"那个字形坑（2026-09-22 事故）—— 去掉名单后这个区分
#:     连同它的入口**一起没了**。
#:   ★ `--pool` 那个开关也删了（用户拍板）：池路径**就是唯一路径**，
#:     `run.sh --once` 与常驻**共用同一套取池逻辑**，差别只在 `--once`/`--limit`。
#:   ★ **注意**：池子不再是"每次从头取前 N、三包合起来"这种"合池"叙述 ——
#:     现在没有"不合池"的对照物了，它就是**池**。`--limit` 是**全局**的。
def pool_packs(st: "S.StateStore") -> list[str]:
    """池子的**包源** = `pack` 表里**全部已登记的包**（`2026-10-05` 去掉「包的闸门」）。

    ★★★ 为什么不再有 `--packs` / `PACKS_DEFAULT` / `DRIVE_PACKS`
    ------------------------------------------------------------
    可选的"包名单"（谁被驱动）**本身就是那道闸门**：名单外的包，其片子的 `stage`/
    `indexer_seen` **一次都不会被问**（`todo(pack)` 压根没被调用）⇒ 不是"变慢"，
    是**停电**。用户 2026-09-21 把 `dc-collection`/`mbf` 移出名单，读到的现象正是
    "包不驱动 = 永久不搜"。去掉闸门后，取件语义变成「每片自己的 `stage` +
    `indexer_seen` 说了算」—— 登记（`pack` 表有行）即被驱动，无需第二处名单。

    ⇒ 于是 `PACKS_DEFAULT`（默认值会漂）、`packs_from_arg`（解析会漂，见它原 docstring
      里那段 2026-09-22 事故）、`DRIVE_PACKS`（compose 覆盖口）**整体退休**：
      少了"原始串 vs 包名列表"这个区分，那个字形坑连同它的入口**一起没了**。

    ★ 枚举口**只有一处**：`StateStore.packs()`（`SELECT * FROM pack ORDER BY name`）。
      ★ 空表 ⇒ 池恒空 ⇒ 会**假报**「🎉 全干完」（同 2026-09-22「静默算空」形状）
        ⇒ 调用方须先过 `require_packs()` 守卫，别把这句当"没东西可搜"咽下去。
    """
    return [r["name"] for r in st.packs()]


class NoPacksRegistered(RuntimeError):
    """`pack` 表为空 —— **不是**"全都搜完了"，是"登记缺失/库读错"。

    ★★ 为什么必须**显式区分**这两个：`alert_if_all_done` 判的是"池子空"，
      而空池有**两种成因**，后果完全相反：
        · 真有欠账但都到期了、都搜完了 → 🎉 里程碑，该发日报
        · `pack` 表**一行都没有**（读错库 / 库被清 / 路径退化）→ **故障**，
          该立刻报错，绝不是"全干完"
      两者的读数**都是 0**，合成一个数就是又一个「`n/a` = 0 = 没事」（`ERR-AI-03`）
      —— 2026-09-22 那次"池子恒 0、连空 14 轮"正是这个形状（当时成因是名单被拆成
      单字符，现在是表空）：**静默**、日志看着正常。⇒ 表空就直接抛，别咽下去。
    """


def require_packs(st: "S.StateStore") -> list[str]:
    """取池源，**并守卫"表非空"**；空表 ⇒ 抛 `NoPacksRegistered`（不静默算空）。"""
    names = pool_packs(st)
    if not names:
        raise NoPacksRegistered(
            "`pack` 表里没有任何已登记的包 —— 池子恒空，这不是「全干完」。"
            " 排查：① `--db` 是否指对库（state.db）；② 是否漏跑 `reseed-state.py init`。")
    return names


# --------------------------------------------------------------------------- #
# cross-seed 的 compose 目录
# --------------------------------------------------------------------------- #
# 在 NAS 本机（DSM 计划任务跑）： <compose> —— 现为 /volume2/docker_ssd/reseed（见下方常量）
#
# ★★ 2026-09-12：**电脑端已退役**，UNC 兜底已注释掉（保留可查，别删注释）。
#   留着的理由不是"以后可能还用" —— 是这段注释本身记着一个坑：2026-09-11 之前，
#   同一份代码要在 Windows 和 NAS 两边都能跑，所以这里得有"NAS 原生优先、
#   Windows 落 UNC"的顺序。现在只剩 NAS 一个执行环境，多一条候选路径
#   就多一个「哪天 NAS 路径写错，它会**悄悄**绕回 SMB 连自己」的静默降级点 ——
#   那种情况不会报错，只是变慢，属于最难查的一类。
#   要回退：取消下面那行的注释即可（代码本身没错，是场景没了）。
# in-place: cross-seed 目录改为可注入（见上方 STATE_DIR 段）。默认回退到**新根**。
# ★★ 2026-10-05（B6 实测补）：`COMPOSE_DIR` 是**仓库根**（含 `.env` / `build-farm.sh`），
#   但 2026-10-05 的结构重构把 **cross-seed 运行时数据收进了 `data/`**
#   （`cross-seed/cross-seed.db`、`cross-seed/logs/info.current.log`、
#   `hlink/` 清单）。⇒ 只搜 `COMPOSE_DIR` 一层会**全部落空**，正是
#   `first_existing()` 的静默退化形状（日报 `packpct=0`，§26.15）。
#   ⇒ 依次搜 **根** 与 **根/data** 两层（`first_existing()` 取第一个存在的）。
_COMPOSE_ROOT = os.environ.get("COMPOSE_DIR") or "/volume2/docker_ssd/reseed"
CROSSSEED_DIRS = (
    _COMPOSE_ROOT,
    str(Path(_COMPOSE_ROOT) / "data"),
    # [电脑端已退役 2026-09-12] 原本是 Windows 经 SMB 跑时的兜底路径：
    # "//iSunker-DS423/docker_ssd/reseed",
)


def first_existing(rel: str) -> str | None:
    """在 CROSSSEED_DIRS 里找第一个存在的 `rel`，返回完整路径；都没有返回 None。

    ★ 找不到时**必须让调用方出声**（见 main() 里两处 LOG.warning）：
      这两个文件缺失会让「回灌」静默退化 —— cross-seed.db 没了就推不出
      MATCH/SEEDING，info 日志没了就推不出「是否在做种」，结果是 SEEDING
      被误降级成 MATCHED。数算错但**不报错**，正是无人值守最怕的那种坏法。
    """
    for d in CROSSSEED_DIRS:
        p = Path(d) / rel
        if p.is_file():
            return str(p)
    return None


# --------------------------------------------------------------------------- #
# 通知出口（**已整体砍掉** 2026-10-05）
# --------------------------------------------------------------------------- #
# 原先这一段的 `emit` / `reset_notify_count` / `notify_delivered` /
# `describe_notifier` / `init_notifier`（外加模块级 `_NOTIFIER` 单例）**全部删除**。
# 通知模块（写侧 spool）已整体迁去独立项目
# `D:\Projects\nas里pt相关程序邮件通知告警项目\reseed-toolkit`，本仓不留任何写侧。
# ★ 与之相配：所有 `emit("...")` 调用点改为**纯日志**（`LOG.info/warning/error`），
#   语义不变、只是不再往 spool 投事件。想发信 ⇒ 去独立项目接通道。


# --------------------------------------------------------------------------- #
# 反馈 → 下次间隔
# --------------------------------------------------------------------------- #
BASE_SLEEP = 60 * 45        # 站点健康时，批间最小间隔 45 分钟
BACKOFF_SLEEP = 60 * 120     # 站点在压我们 → 拉长到 2 小时
SNOOZE_SLEEP = 60 * 90       # 中等退避 → 放缓到 1.5 小时
ABORT_SLEEP = 60 * 180       # 提前中止（退避等太久），休整 3 小时

MIN_SLEEP = 60 * 30          # 硬下限：任何情况批间隔不少于 30 分钟
MAX_SLEEP = 60 * 240         # 硬上限：4 小时

# ---- 退避分级的阈值（2026-09-12）------------------------------------------- #
# `backoff_hits > 0` 只说明「见到过限流」，**不说明被耽误了多久**：实测的 429
# 只 snooze 30~60 秒，站点转头就恢复，跟「站点持续压着我们」是两回事。
# 一律罚 2 小时，等于把打喷嚏和重感冒当同一种病。所以按这批**实际等掉的时间**
# （`waited_sec`）分档 —— 没真耽误我们的，就别惩罚。
#
#   waited < BACKOFF_SHORT_SEC          → 喷嚏，按基准 45 分钟
#   BACKOFF_SHORT_SEC ≤ waited < BACKOFF_LONG_SEC → 放缓到 1.5 小时
#   waited ≥ BACKOFF_LONG_SEC           → 站点确实在压，拉满 2 小时
#
# ★ 想回到「命中就 2 小时」的老行为：把两个阈值都设成 0 即可。
# ★ 「等了 0 秒却记了一笔」是**故意**的：那一笔来自「窗口落在两次检查之间、
#   我们压根没等」的限流 —— 它确实发生过，记进 backoff_hits 供观测，但既然
#   没挡住我们，就不该改间隔。这正是本次分级的核心目的。
BACKOFF_SHORT_SEC = 180.0    # 3 分钟
BACKOFF_LONG_SEC = 600.0     # 10 分钟


def clamp(s: float) -> float:
    return max(MIN_SLEEP, min(MAX_SLEEP, s))


def next_sleep(stats: S.DriveStats) -> tuple[float, str]:
    """根据上一批的结果决定下次批间隔。返回 (秒, 原因)。"""
    if stats.aborted:
        return ABORT_SLEEP, f"提前中止：{stats.aborted}"
    # ★ still_skipped 与 backoff_hits 性质不同，不参与分级：
    #   前者是「真有片子没推进」（结果问题），后者只是「站点限过流」（过程噪声）。
    if stats.still_skipped > 0:
        return BACKOFF_SLEEP, f"仍有 {stats.still_skipped} 部被退避跳过"
    if stats.backoff_hits > 0:
        w = stats.waited_sec
        if w >= BACKOFF_LONG_SEC:
            return BACKOFF_SLEEP, (f"本轮 {stats.backoff_hits} 次退避、等了 "
                                   f"{w / 60:.1f} 分钟，站点在限流")
        if w >= BACKOFF_SHORT_SEC:
            return SNOOZE_SLEEP, (f"本轮 {stats.backoff_hits} 次退避、等了 "
                                  f"{w / 60:.1f} 分钟，放缓一档")
        return BASE_SLEEP, (f"短暂退避 {stats.backoff_hits} 次（{w:.0f} 秒），"
                            "站点已恢复，按基准")
    if stats.newly_seeding > 0:
        return BASE_SLEEP, f"新增做种 {stats.newly_seeding} 部，站点健康"
    # 一批发出去、既没退避也没新增 —— 站点正常但命中一般，按基准即可
    return BASE_SLEEP, "无退避、无新增（正常）"


# --------------------------------------------------------------------------- #
# 「出声」机制 —— 无人值守最怕的是**无声停摆**
# --------------------------------------------------------------------------- #
# 站点持续 502 时 next_sleep 只会给它 3 小时休息，然后接着试、接着失败。
# 跑一晚上没人知道。所以连续失败要跨过阈值就大声喊；全部干完也要喊一声。
ABORT_ALERT_AFTER = 3

#: `S.DriveStats.aborted_kind` 里属于**良性**的那一种（见那边的说明）。
#: 良性 = 本批条目**没有失败**，只是站点还在退避、等到超过 `--max-wait` 就先收工，
#: 剩下的条目**下轮会重新排上**。它和「批失败」是两件事，不能共用一个计数。
BACKOFF_ABORT_KIND = "indexer-backoff"


def _abort_kind(stats: S.DriveStats | None, failed: bool) -> str | None:
    """归一化出「这批为什么没跑完」。返回 `None` = 正常跑完。

    ★ 兜底方向是**宁可吵**：`aborted` 非空却认不出种类时按真失败算 ——
      把真故障误判成良性会**静默**，把良性误判成故障只是多喊一声。
    """
    if failed:
        return "batch-exception"
    if stats is None or not stats.aborted:
        return None
    return getattr(stats, "aborted_kind", None) or "unknown"


def update_abort_streak(prev: tuple[int, int], stats: S.DriveStats | None, *,
                        failed: bool = False) -> tuple[int, int]:
    """维护两个**分开的**连续计数，返回 `(真失败批数, 良性收工批数)`。

    正常跑完（含"本包无待搜"）把**两个都清零**。

    ★ 为什么必须拆成两个（#45）：原先只有一个计数、只看 `stats.aborted` 的真假，
      于是**站点退避超时**被念成「批失败」。实测 2026-09-13 的 TSV 里**同一批**
      既写 `ok=22 failed=0`、又写「连续 3 批失败（索引器 HDtime 要等到 …）」
      —— 一个名字盖了两种模型，和 `NanyangPT` vs `NanyangPT (南洋)` 同一个形状。
    ★ 两个计数**互不干扰**，谁都不清零对方 —— 这一条是**被测试逼出来的**：
      起初写成"互相清零"，于是「鉴权炸了、但中间夹了一批站点退避」时，
      真失败的计数会被反复冲回 0，**永远到不了 3** ⇒ 那个告警存在的意义
      （无人值守下抓住持续故障）正好被抹掉。所以：
      本次是哪一种，就只涨哪一种；另一种**原样留着**；只有**正常跑完**才两个都清零。
      于是 `hard + backoff` = 「距离上一次正常跑完过了几批」，谁也别想被掩盖。
    """
    kind = _abort_kind(stats, failed)
    if kind is None:
        return 0, 0                     # 一次正常跑完 → 两个都清零

    hard, backoff = prev
    reason = (stats.aborted if (stats is not None and stats.aborted)
              else "整批异常")

    if kind == BACKOFF_ABORT_KIND:
        n = backoff + 1
        total = hard + n                # 距上次正常跑完过了几批
        # ★ 只在两种混着出现时才补这句 —— 常见情况下 n == total，补了是噪音。
        mixed = f"（共 {total} 批没跑成）" if total != n else ""
        if n < ABORT_ALERT_AFTER:
            LOG.warning("连续第 %d 批提前收工（%s）", n, reason)
            return hard, n
        LOG.warning("!" * 62)
        LOG.warning("⚠ 已连续 %d 批提前收工%s（最近：%s）", n, mixed, reason)
        LOG.warning("  这**不是**「批失败」—— 本批条目没失败，只是站点还在退避。")
        LOG.warning("  解禁后自己会恢复；长期如此就考虑把这个站从 --indexers 摘掉。")
        LOG.warning("!" * 62)
        # ★ 通知写侧已砍掉（2026-10-05）：这里原先 `emit("alert", …, key="consec-backoff")`。
        #   语义保留在**上面的日志**里；要不要发信由独立通知项目决定。
        return hard, n

    n = hard + 1
    total = n + backoff
    mixed = f"（共 {total} 批没跑成）" if total != n else ""
    if n < ABORT_ALERT_AFTER:
        LOG.warning("连续第 %d 批失败（%s）", n, reason)
        return n, backoff
    LOG.warning("!" * 62)
    LOG.warning("⚠ 已连续 %d 批失败%s（最近：%s）", n, mixed, reason)
    LOG.warning("  无人值守下这通常意味着两种可能：")
    LOG.warning("   ① 鉴权 / 路径问题（webhook 返回 400/401/403 时最可能）—— 看 key 与 URL")
    LOG.warning("   ② .env 改了但没 force-recreate（容器里还是旧配置，见 SUMMARY §13.6）")
    LOG.warning("  请人工看一眼 cross-seed 日志与 Prowlarr。")
    LOG.warning("!" * 62)
    # ★ 通知写侧已砍掉（2026-10-05）：这里原先 `emit("alert", …, key="consec-abort")`。
    #   语义保留在**上面的日志**里（连续失败批数、可能原因清单）。
    return n, backoff


def alert_if_all_done(pack_count: int, db: str, packs: list[str]) -> bool:
    """所有登记的包都没有待搜项 → 大声报告"干完了"（否则循环静默退出，没人知道）。

    ★★ 2026-10-05（去掉闸门后改）：判据从"按**名单**数每包"改成"按 **`pack` 表全部
       登记的包**数"。`pack_count` = `pack` 表行数（= 池源包数）；`packs` 仅用于
       日志/事件里的名字列举。
    ★ 空池（`pack_count == 0`）**不到这里**：那是"表空"故障，已由 `require_packs()`
      在**取池之前**拦下并抛 `NoPacksRegistered` —— 本函数只处理"有表、但都没待搜"。
    """
    remaining = 0
    st = S.StateStore(db)
    try:
        for p in packs:
            remaining += sum(1 for r in st.movies(p) if r["stage"] not in S.DONE_STAGES)
    finally:
        st.con.close()
    if remaining:
        return False
    LOG.warning("=" * 62)
    LOG.warning("🎉 所有登记的包均无待搜项 —— 自动循环已无事可做。")
    LOG.warning("   计划任务可保留（新片/新站接入后会自动变回待搜），")
    LOG.warning("   或手动停掉：schtasks /Delete /TN \"reseed-drive-loop\" /F")
    LOG.warning("=" * 62)
    # ★ 通知写侧已砍掉（2026-10-05）：这里原先 `emit("batch", …, key="all-done")`。
    #   里程碑语义保留在**上面的日志**里（🎉 全部干完）。
    return True


# --------------------------------------------------------------------------- #
# 索引器自检 —— cross-seed 实际会搜的站 vs --indexers
# --------------------------------------------------------------------------- #
def _norm_indexer_name(n: str) -> str:
    """取站名主干：'NanyangPT (南洋)' → 'nanyangpt'。

    ★ 实现**只有一份**，在 `orchestrator/state.py`（2026-09-16 挪过去）：本站的
      「`--indexers` 与库里站名对账」和 `DriveSession` 的「是不是所有站都在退避」
      必须用同一把尺，各写一份迟早会分叉。这里只留一个转发，调用点不动。
    """
    return S.norm_indexer_name(n)


def check_indexers(args) -> None:
    """★ 这两份信息是**分开维护**的，对不上会**静默错记**。

    cross-seed 按它自己的 `TORZNAB_URLS` **全站搜索**；
    `--indexers` 只决定状态机把"搜过"**记到哪个站名下**（`indexer_seen` / 重搜周期）。
    加了站却忘了改 `--indexers` → 新站搜到的结果被记成"没搜过那个站"
    → 要么反复重搜（浪费额度），要么永远不重搜。而且**不报错**，只是数算错。
    """
    if not args.db_path:
        return
    try:
        snap = S.read_crossseed_db(args.db_path)
    except Exception as e:  # noqa: BLE001 —— 自检失败不该挡住正事
        LOG.debug("索引器自检跳过（读不到 cross-seed.db）: %s", e)
        return

    live = list(snap.indexers)
    mine = {_norm_indexer_name(x) for x in (args.indexers or "").split(",") if x.strip()}

    # cross-seed 侧用 "prowlarr#<N>" 表示"active 但拉不到名字"的索引器（见 read_crossseed_db）
    # ★ N 是 **URL 里的 N**（Prowlarr 的索引器号，与 TORZNAB_URLS 的写法一致），
    #   不是 cross-seed 数据库的行号 —— 别拿它去对 indexer 表的 id。
    unnamed = [lbl for lbl in live if lbl.startswith("prowlarr#")]
    named = [lbl for lbl in live if not lbl.startswith("prowlarr#")]
    missing = [lbl for lbl in named if _norm_indexer_name(lbl) not in mine]
    extra = sorted(mine - {_norm_indexer_name(lbl) for lbl in named})

    if unnamed:
        LOG.warning("⚠ cross-seed 有 %d 个 active 索引器**拉不到名字**（%s）——"
                    " 多半是该站返回错误（410/403/CF），caps 取不回来。",
                    len(unnamed), ", ".join(unnamed))
        LOG.warning("  编号是 `TORZNAB_URLS` 里的那个（`#N` = `/N/api`），不是 Prowlarr 界面序号。")
        LOG.warning("  常见原因：`.env` 里删了站但容器没重建，或该站被 Prowlarr 禁用。")
        for lbl in unnamed:
            # ★ 通知写侧已砍掉（2026-10-05）：原先对每个拉不到名字的站 `emit("alert")`。
            LOG.warning("    · 拉不到名字的索引器：%s（编号即 TORZNAB_URLS 里的 #N=/N/api）",
                        lbl)
    if missing:
        LOG.warning("⚠ cross-seed 实际会搜 %s，但 --indexers 没列 ——"
                    " 这些站的搜索结果会被状态机**漏记**（→ 重复搜 / 永不重搜）。",
                    ", ".join(missing))
        LOG.warning("  建议把 --indexers 改成：--indexers %s", ",".join(named))
        # ★ 通知写侧已砍掉（2026-10-05）：原先 `emit("alert", …, key="indexer-missing:…")`。
        #   修法提示保留在**上面的日志**里。
    if extra:
        LOG.warning("⚠ --indexers 列了 %s，但 cross-seed 根本不会搜 ——"
                    " 状态机会把它们记成'搜过'，实际是假的（→ 永远不会去搜）。",
                    ", ".join(extra))
        # ★ 通知写侧已砍掉（2026-10-05）：原先 `emit("alert", …, key="indexer-extra:…")`。
        #   这里把"怎么分清是哪种多了"的处置**原样搬进日志**（原本在邮件正文里）。
        LOG.warning("  ⚠ 但删之前先分清是哪种「多了」—— 删错了会把新站**永远**排除：")
        LOG.warning("    ① 只在 --indexers 里写过、.env 里没有它 → 删掉它。")
        LOG.warning("    ② .env 的 TORZNAB_URLS 里**有**它 → 那是容器没重建：")
        LOG.warning("       sudo docker compose up -d --force-recreate cross-seed")
        LOG.warning("       （**不是**删 --indexers）。")
    if not (unnamed or missing or extra):
        LOG.info("索引器自检通过：cross-seed 实际会搜 %s，与 --indexers 一致。",
                 ", ".join(named))


# --------------------------------------------------------------------------- #
# .env 生效自检 —— cross-seed 日志里**最近**的 410/401/403
# --------------------------------------------------------------------------- #
#: 只看最近这么久内的日志。
#: ★ 必须卡时间窗：容器重建成功后，历史 410 还在日志里躺着，不卡窗口就会
#:   **永远报警**（狼来了），反而把真问题淹掉。
STALE_ENV_WINDOW_SEC = 2 * 3600
#: 只读日志**尾部**这么多字节（info.current.log 会长到几百 MB，全读不可接受）
STALE_ENV_TAIL_BYTES = 512 * 1024

#: cross-seed 够不着索引器的两种固定句式（v6.13.7 实测）：
#:   ① 搜索中途：warn: [webhook] Failed to reach <url>: request failed with code 410, snoozing until …
#:   ② 启动取 caps：error: <url> returned 401 Unauthorized when fetching caps, check your apikey
_RE_UNREACHABLE = (
    re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d+\s+warn:\s+.*?"
        r"Failed to reach (?P<url>\S+?):\s+request failed with code (?P<code>\d+)"
    ),
    re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d+\s+error:\s+"
        r"(?P<url>\S+?) returned (?P<code>\d+) \w+ when fetching caps"
    ),
)

#: **只有这几种码**算「配置没生效」。
#:   410 = 索引器在 Prowlarr 里已经没了；401 = apikey 对不上；403 = 该站被禁用。
#:   三者都是「改了 .env 但容器没重建」的表现 —— 正是本自检要抓的东西。
#:
#: ★ 429 **必须排除**。它是限流，实测长这样（注意是**站名**不是 URL，还带原因说明）：
#:
#:     warn: [webhook] Failed to reach HDFans: request failed with code 429
#:           due to rate limiting, snoozing until 2026-09-11 21:00:53
#:
#:   限流是**周期性的正常现象**，退避逻辑自己会处理，跟 .env 毫无关系。
#:   上面那个宽泛的正则会把 429 一起捞进来（`\S+?` 连站名也匹配），
#:   于是推出一封「.env 未生效：HDFans 返回 429」——**把人骗去白重建一次容器**。
#:   日志里它只是条误导性警告，一旦接上邮件就成了真骚扰。
STALE_ENV_CODES = frozenset({"410", "401", "403"})


def _tail_lines(path: Path, nbytes: int) -> list[str]:
    """读文件**尾部** nbytes 字节（SMB 上是范围读，不会把 2.6 MB 全拉过来）。"""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - nbytes))
        data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > nbytes and lines:
        lines = lines[1:]      # 第一行多半被切断在半路，丢掉
    return lines


def check_env_applied(args) -> None:
    """★ 本项目的**头号复发坑**：改了 `.env`，但容器没重建。

    `docker compose restart` **不会**重新注入环境变量，必须
    `up -d --no-deps --force-recreate cross-seed`（见 SUMMARY §13.3 / §13.6）。

    它的症状是 cross-seed 日志里不停出现：

        warn: [webhook] Failed to reach http://prowlarr:9696/3/api:
              request failed with code 410, snoozing until …

    410 = 这个索引器在 Prowlarr 里**已经没了**，但容器的 `TORZNAB_URLS` 还留着它
    （删站忘了重建）；401 = apikey 对不上；403 = 该站被禁用。三者多半是同一件事。

    ★ 429 **不算** —— 那是限流，正常现象，退避逻辑自己会处理（见 `STALE_ENV_CODES`）。

    这里**只报警、不自动修** —— 重建容器必须由人执行（会打断正在跑的批次）。
    """
    if not args.log:
        return
    cutoff = time.time() - STALE_ENV_WINDOW_SEC
    hits: dict[tuple[str, str], int] = {}
    newest = 0.0
    for p in args.log:
        try:
            lines = _tail_lines(Path(p), STALE_ENV_TAIL_BYTES)
        except Exception as e:  # noqa: BLE001 —— 自检失败不该挡住正事
            LOG.debug("读不到 cross-seed 日志 %s: %s", p, e)
            continue
        for ln in lines:
            m = None
            for rx in _RE_UNREACHABLE:
                m = rx.match(ln)
                if m:
                    break
            if not m:
                continue
            if m["code"] not in STALE_ENV_CODES:
                # 429 之类的限流 —— 退避逻辑自己会处理，不是配置问题（见 STALE_ENV_CODES）
                LOG.debug("忽略非配置类错误：%s → HTTP %s", m["url"], m["code"])
                continue
            try:
                ts = datetime.strptime(m["ts"], "%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                continue
            if ts < cutoff:
                continue
            key = (m["url"], m["code"])
            hits[key] = hits.get(key, 0) + 1
            newest = max(newest, ts)

    if not hits:
        LOG.debug("`.env` 生效自检通过：最近 %.0f 小时内没有够不着索引器的记录。",
                  STALE_ENV_WINDOW_SEC / 3600)
        return

    LOG.warning("!" * 62)
    LOG.warning("⚠ cross-seed 最近 %.0f 小时内**一直够不着**这些索引器：",
                STALE_ENV_WINDOW_SEC / 3600)
    for (url, code), n in sorted(hits.items(), key=lambda kv: -kv[1]):
        LOG.warning("    %-46s ← HTTP %s × %d", url, code, n)
    LOG.warning("  最近一次：%s（%.0f 分钟前）",
                time.strftime("%H:%M:%S", time.localtime(newest)),
                (time.time() - newest) / 60)
    LOG.warning("")
    LOG.warning("  ★ 本项目的头号复发坑：**改了 .env，但容器没重建**。")
    LOG.warning("    410 = 该索引器在 Prowlarr 里已经没了，容器里的 TORZNAB_URLS 还留着它；")
    LOG.warning("    401 = apikey 对不上；403 = 该站被禁用。三者多半是同一件事。")
    LOG.warning("    ⚠ `docker compose restart` **不重新注入环境变量**，必须：")
    LOG.warning("      cd %s \\", CROSSSEED_DIRS[0])
    LOG.warning("        && sudo docker compose up -d --no-deps --force-recreate cross-seed")
    LOG.warning("    验证：本条警告消失（上面的「索引器自检」也会跟着变绿）。")
    LOG.warning("!" * 62)

    # 逐 (url, code) 报一行 —— 每个都是独立可修的问题。
    # ★ 通知写侧已砍掉（2026-10-05）：原先这里对每个 (url, code) `emit("alert")`。
    for (url, code), n in sorted(hits.items(), key=lambda kv: -kv[1]):
        LOG.warning("  · .env 未生效：%s 返回 %s ×%d（最近 %s）",
                    url, code, n,
                    time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(newest)))


# --------------------------------------------------------------------------- #
# 读 .env 里的 CROSSSEED_API_KEY（避免 key 出现在命令行/聊天）
# --------------------------------------------------------------------------- #
def read_key_from_env(env_path: Path) -> str | None:
    for ln in env_path.read_text(encoding="utf-8").splitlines():
        if ln.startswith("CROSSSEED_API_KEY="):
            return ln.split("=", 1)[1].strip()
    return None


def env_value(env_path: Path, key: str) -> str:
    """从 .env 取一个值；取不到（文件不在 / 没这个键）返回 ""。

    ★ 用 `splitlines()` 而不是 `read_text().split("\\n")`：前者会把 CRLF 的
      `\\r` 一并剥掉。`.env` 是 CRLF，而 shell 那边的 `sed` **不会**剥
      —— §16.2.1.2 那个假漂移就是这么来的。Python 这边天然没这个坑，
      但**前提是用对方法**：拿 `split("\\n")` 就会把 `\\r` 留在值里。
    ★ 一律吞掉 OSError：这类"配置缺一项"不该让调用方炸。
    """
    try:
        for ln in env_path.read_text(encoding="utf-8").splitlines():
            if ln.startswith(key + "="):
                return ln.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def prowlarr_cfg(args) -> tuple[str, str]:
    """从 --env 指的 .env 里取 (PROWLARR_URL, PROWLARR_API_KEY)。

    ★ 凭据**只从文件读**，不提供命令行开关 —— 同 `read_key_from_env` 的理由：
      避免 key 落进 shell 历史 / 进程表 / 聊天记录。
    """
    p = Path(getattr(args, "env", "") or "")
    if not p.is_file():
        return "", ""
    return env_value(p, "PROWLARR_URL"), env_value(p, "PROWLARR_API_KEY")


# --------------------------------------------------------------------------- #
# 跑一轮（一个包的一批）
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 收尾通知：每日台账（额度 §16.1 + 趋势 §16.3）+ 正在退避的即时告警
#
# ★ 为什么额度进**日报**而不是告警：额度是**慢性**问题（超了不会当场炸）。
#   即时通道必须留给**急性**故障 —— 否则就是 README 警告过的
#   「每 15 分钟一封骚扰 → 你去建过滤规则 → 连真告警一起过滤掉」（§16.1.3）。
#   真正急的那条（站点**此刻**正在退避）单独走 alert，见下。
#
# ★ 来源 B（问 Prowlarr /api/v1/indexerstats）**2026-09-12 起接在这里了**。
#   原先不接的理由是"字段名没拿真实响应核对过，不把没验证过的东西塞进每轮都跑的
#   无人值守路径"。当天用真响应核对过，结构是：
#       {"indexers":[{"indexerName":..., "numberOfQueries":..., ...}],
#        "userAgents":[...], "hosts":[...]}
#   —— 与 `S.prowlarr_indexer_stats` 的解析完全一致，理由不再成立。
#   而且它现在多了一个**更重要的用途**：把「Prowlarr 里有、我们没在用」的站也摆出来，
#   那是"有别的工具在用同一个 Prowlarr"的唯一信号（§16.6）。
#   ★ 拿不到时**只写一句原因**，绝不编数（见 `attach_prowlarr_quota` 的保证）。
# --------------------------------------------------------------------------- #
DAILY_FILE = STATE_DIR / ".daily-report.state"


def _daily_last() -> str:
    try:
        return json.loads(DAILY_FILE.read_text(encoding="utf-8")).get("last_day", "")
    except Exception:                       # 文件不存在 / 坏了 —— 都当"还没发过"
        return ""


def _daily_set(day: str) -> None:
    try:
        tmp = DAILY_FILE.with_suffix(".state.tmp")
        tmp.write_text(json.dumps({"last_day": day}), encoding="utf-8")
        os.replace(tmp, DAILY_FILE)         # 原子替换，别让下一轮读到半个文件
    except OSError:
        LOG.debug("每日台账标记写不进去（不影响跑批）", exc_info=True)


def alert_blocked_indexers(args, *, now: datetime | None = None) -> int:
    """有站点**此刻正在**退避 → alert（即时）。返回发了几条。

    ★ `now` 只为**可测**而存在（生产调用一律省略 → 用真实时钟）。
      起因是 2026-09-12：自测里 fixture 的解禁时刻是写死的 `NOW + 1 小时`，
      而这个函数内部读的是真实时钟 —— 于是自测**上午还过、11:00 一过必挂**，
      变成一颗按钟点引爆的定时炸弹。把「此刻」变成入参，测试才能钉死时间。

    ★ 判据是 `blocking_backoffs()`（状态非 OK **且解禁时间还没到**），
      不是 `snoozed_indexers()`（只要见过限流）。差别很要命：cross-seed 的
      `status` 列**不会自动清**回 OK —— 实测 HDFans 的 `retry_after` 早就过期了、
      状态还挂着 `RATE_LIMITED`。只看"见过限流"的话，每隔一个冷却期就会为
      **同一件早就过去的事**再喊一次，喊到人不再看它 —— 那正好毁掉告警通道。
    """
    if not getattr(args, "db_path", None):
        return 0
    try:
        blocked = S.blocking_backoffs(S.read_indexer_backoff(args.db_path), now)
    except Exception:                       # 读不到就当没这回事；通知是附属功能
        LOG.debug("读退避状态失败，跳过站点告警", exc_info=True)
        return 0
    n = 0
    for b in blocked:
        until = b.until.strftime("%m-%d %H:%M") if b.until else "未知"
        LOG.warning("  ⏸ 站点退避中：%s（%s）解禁 %s —— 不用手动处理，"
                    "连续几天都在退避就把 --cadence-days 调大或考虑换站。",
                    b.name, b.status, until)
        n += 1
    return n


# --------------------------------------------------------------------------- #
# IYUU 辅种条数 —— §18.8 留下的**唯一生产级证伪点**
# --------------------------------------------------------------------------- #
# 搬迁（reseed_farm → reseed/）时推断：IYUU 不存目标目录、每次从 qB 现读，所以
# 搬迁对它**透明**。**唯一能证伪这个推断的观测**，就是看它次日 01:45（它的
# cron 是 `45 1 * * *`）跑完之后，`:3060` 上 `IYUU自动辅种` 的条数还在不在涨。
#
# ★ 只记账、**不告警**：这是个慢性观测（要几天才有结论），而"急性故障才喊"是
#   这条通道的根本纪律 —— 骚扰多了人就会建过滤规则，然后连真告警一起过滤掉。
#   它冻住了也不该当场喊。（"alert 通道"原指邮件；写侧已砍掉，纪律不变。）
# ★ 数字必须同时进 `metrics`：日报渲染时**只读 metrics**，不读正文。
#   （原先是 notify 的 TSV 流水只记 ts/kind/title/**metrics**；通知写侧已砍掉，
#    但"数字要进 metrics 才留得下来"这条规则不变。）只写在正文里，回头看是拿不到的。
IYUU_TAG = "IYUU自动辅种"
IYUU_BASELINE = 100     # 2026-09-12 搬迁实测（SUMMARY §18.8 的构成表）


def iyuu_verdict(n: int, baseline: int = IYUU_BASELINE) -> str:
    """条数 → 一句话结论。**纯函数**，好钉测试。"""
    if n > baseline:
        return f"仍在增长（基线 {baseline}）"
    if n == baseline:
        return f"与基线持平（{baseline}）"
    return f"⚠ **低于**基线 {baseline}"


def iyuu_watch(args) -> tuple[str, dict]:
    """问 :3060 要 `IYUU自动辅种` 的条数 → (给正文的一行, 给 metrics 的字典)。

    ★ **绝不抛**：它挂在每天一次的日报里，而日报挂在**每 15 分钟一批**的生产
      循环里。一次 qB 抖动不该让整份日报消失（同本函数上面各节的规矩）。
    ★ 读不到时 metrics 给 `n/a` —— **必须给**，否则 TSV 里"这次读失败了"和
      "那天根本没跑"长得一模一样，事后分不开。
    """
    url = getattr(args, "qbit_url", None)
    if not url:
        return "IYUU 辅种条数：跳过（没有 --qbit-url）", {"iyuu": "no-url"}
    try:
        n = len(S.qbit_tagged(url, IYUU_TAG))
    except Exception as e:              # noqa: BLE001 —— 附属观测，绝不拖垮日报
        LOG.debug("读 IYUU 辅种条数失败", exc_info=True)
        return f"IYUU 辅种条数：读不到（{type(e).__name__}: {e}）", {"iyuu": "n/a"}
    return f"IYUU 辅种条数：**{n}** —— {iyuu_verdict(n)}", {"iyuu": n}


# --------------------------------------------------------------------------- #
# qB「卡 999」—— 停滞的未完成种子
# --------------------------------------------------------------------------- #
# 判据本体在 `orchestrator/state.py` 的 `qb_999_band`，**不在这个文件里** ——
# 理由与对账那几条一样：本进程跑在 NAS 宿主机上，判据只有一份才谈得上两侧一致。
#
# 立这一节的动机（2026-09-13）：这个 qB 只挂拆大包的种子（**全员硬链接**），
# 本不该下载任何东西。而 99.x% 是其中唯一「**既不报错也不完成**」的静默档 ——
# 它同时躲过 error 计数和 seeding 计数，从所有现有观测里一起漏出去。
# 硬链接一旦被重新下载就断了（qB 写新数据 = 新 inode = 与源文件脱钩），
# 所以「卡住」与「在重下」这两类都值得记账。
#
# ★ 只记账、**不告警**（同 `iyuu_watch`）：慢性观测，即时 alert 通道留给急性故障。
#
# ★ 基线：不配就是「**每天发同样一封**」—— 那正是 #59 邮件风暴教训的**反面**
#   （那次是同一封重发 6 次，这次是会天天重发）。现状不是新闻，只报**新增**。
#   形状照抄 #47「无人认领」：首读只记基线不响、与基线一致则静默、
#   缩回静默采纳但**在正文里写出来**（否则「清掉了」和「判据没读到」分不开）。
QB_999_BASELINE_KEY = "_qb_999_baseline"


def _qb_999_norm(d) -> list:
    """把基线归一成**排序后的 hash 列表**（老版本 / 坏形状一律当空）。

    ★ 归一在**读**这一侧做（同 `_unclaimed_norm`）：老版本写下的文件可能没有
      `hashes` 键，读的时候不补就会 KeyError。
    """
    if isinstance(d, dict):
        d = d.get("hashes") or []
    return sorted(x for x in (d if isinstance(d, list) else []) if x)


def qb_999_watch(args) -> tuple[str, dict]:
    """问 :3060 要「卡 999」的条数 → (给日报正文的一段, 给 metrics 的字典)。

    ★ **绝不抛**：它挂在每天一次的日报里，而日报挂在每 15 分钟一批的生产循环里。
      一次 qB 抖动不该让整份日报消失（同 `iyuu_watch`）。
    ★ 读不到时 metrics 给 `n/a` —— **必须给**，否则 TSV 里「这次读失败了」和
      「那天根本没跑」长得一模一样，事后分不开（同 `iyuu_watch`）。
    ★ `qb_total`（分母）**必须跟着条数一起进 metrics**：单说「2 条」没有量纲，
      判不了是 2 / 932 还是 2 / 5。
    """
    url = getattr(args, "qbit_url", None)
    if not url:
        return ("qB 卡 999：跳过（没有 --qbit-url）",
                {"qb_999": "no-url", "qb_total": "no-url"})
    try:
        r = S.qb_999_band(S.qbit_all_torrents(url), time.time())
    except Exception as e:              # noqa: BLE001 —— 附属观测，绝不拖垮日报
        LOG.debug("读 qB 卡 999 失败", exc_info=True)
        return (f"qB 卡 999：读不到（{type(e).__name__}: {e}）",
                {"qb_999": "n/a", "qb_total": "n/a"})

    # ★★ 读 → 改 → 写必须**紧挨着**，中间不许夹别的写入者。
    #    `_reconcile_write` 是**整文件覆盖**，而 `reconcile_watch` 会把自己那份
    #    快照（`prev_ok`，见 :1071）**攥着走完整个函数体**、到末尾才写回。
    #    所以真正的约束**不是「谁先谁后」**（两个函数是顺序执行的，后跑的那个
    #    读到的就是先跑那个写下的，先后都安全），而是：**本函数绝不能卡在
    #    `reconcile_watch` 的「读」与「写」之间被调用** —— 那会被它连同快照
    #    一起覆盖掉。今天 `report_daily` 里是并列调用，安全；将来若有人把它
    #    挪进 `reconcile_watch` 内部、或起个线程去调，这个键会被**静默抹掉**：
    #    抹的那一刻不报错，要到次日发现基线「自己没了」才看得见。
    live = _reconcile_read()
    raw = live.get(QB_999_BASELINE_KEY)
    first = raw is None                 # ★ 与「基线记过一个空集」是两回事
    base = set(_qb_999_norm(raw))
    cur = set(r["hashes"])
    new = sorted(cur - base)
    live[QB_999_BASELINE_KEY] = sorted(cur)      # ★ 写 == cur，**不是 base ∪ cur**
    _reconcile_write(live)

    if first:
        # ★ 首读**不响**（#47 立的规矩）：现存的那几条是「已接受的现状」，
        #   不是今天新冒出来的。响一次就得配冷却，而那正是要避免的噪音。
        verdict = f"首次读数，记基线（{r['n']} 条）"
    elif new:
        verdict = f"新增 {len(new)} 条"
    elif cur == base:
        verdict = "与基线一致"
    else:
        # 缩回（含清空）：静默采纳，但**必须在正文里写出来** —— 否则「清掉了」
        # 和「判据没读到」从外面看一模一样（#47 的教训，别省这一句）。
        # ★ 这里**不许**打印「与基线一致」：基线刚刚被改写成 cur，那句是假话。
        gone = len(base) - len(cur)
        verdict = f"比基线少 {gone} 条（已采纳）" + ("—— 已清空" if not cur else "")

    note = (f"qB 卡 999（分母 {r['total']}）\n"
            f"  卡 999（停滞 > 24h，progress ≥ 0.99）： {r['n']} 条 —— {verdict}")
    # ★ 只报数量，**不报种子名**（用户 2026-09-13 钉的口径）。
    #   `hashes` 只参与基线的集合运算，不进正文、不进 metrics。
    return note, {"qb_999": r["n"], "qb_999_new": 0 if first else len(new),
                  "qb_total": r["total"]}


# --------------------------------------------------------------------------- #
# qB「卡种」—— 匹配到了却下不来（`e404b1ca#7`）
# --------------------------------------------------------------------------- #
# 判据本体在 `orchestrator/state.py`（`qb_stalled_band`）—— 同上面几条：
# **本进程跑在 NAS 宿主机上**，判据只有一份才谈得上两侧一致。
#
# ★★ 为什么要有它（用户 2026-09-20 的场景：「涌入上百个种子做不下」）：
#   `MATCHED` 已在 `DONE_STAGES`（**不重搜是对的**，代码早就这样），
#   `qb_999_band` 也早有了。**缺的是"看得见"** —— 这批片子
#     · 不进 `UNMATCHED` 的分池计数（它们不是"没搜到"）
#     · 不算 `SEEDING`（进度里既不加分子也不加分母）
#     · 不卡 999（那条要求 `progress ≥ 0.99`，而"做不下"的大多没到）
#   ⇒ **从所有现有观测里同时漏出去**。这正是 §26.33 那个形状：
#     观测坏了，下游全是假红/假绿。
#
# ★ 基线：同 `qb_999_watch` 的形状（首读只记基线不响、一致则静默、
#   缩回静默采纳但正文写出来）。★ 用**同一个** `.reconcile.state` 文件、
#   **另一个**键 —— 那条"读→改→写必须紧挨着"的约束（见 `qb_999_watch` 里那段）
#   对 `_reconcile_write` 成立，本函数照同样的规矩紧挨着读写，所以安全。
STALLED_BASELINE_KEY = "_qb_stalled_baseline"


def qb_stalled_watch(args) -> tuple[str, dict]:
    """问 :3060 要「卡种」的条数 → (给日报正文的一段, 给 metrics 的字典)。

    ★ **绝不抛**（同 `qb_999_watch`）：它挂在每天一次的日报里，而日报挂在
      每 15 分钟一批的生产循环里。一次 qB 抖动不该让整份日报消失。
    ★ 读不到时 metrics 给 `n/a` —— **必须给**，否则 TSV 里「这次读失败了」和
      「那天根本没跑」长得一模一样，事后分不开。
    ★ `total`（分母）跟着走：单说「3 条」判不了是 3/932 还是 3/5。
    ★★ **只识别 + 通知，不碰 qB**（不 pause / 不删 / 不改 tag）——
      同 `reseed_freeze` 那条**否决记录**：用户要的是先"看见"。
    """
    url = getattr(args, "qbit_url", None)
    if not url:
        return ("qB 卡种：跳过（没有 --qbit-url）",
                {"qb_stalled": "no-url", "qb_stalled_total": "no-url"})
    try:
        r = S.qb_stalled_band(S.qbit_all_torrents(url))
    except Exception as e:              # noqa: BLE001 —— 附属观测，绝不拖垮日报
        LOG.debug("读 qB 卡种失败", exc_info=True)
        return (f"qB 卡种：读不到（{type(e).__name__}: {e}）",
                {"qb_stalled": "n/a", "qb_stalled_total": "n/a"})

    # ★★ 读 → 改 → 写**紧挨着**（约束同 `qb_999_watch` 里那段注释）。
    live = _reconcile_read()
    raw = live.get(STALLED_BASELINE_KEY)
    first = raw is None                 # ★ 「从没读过」与「读到过一个空集」是两回事
    base = set(_qb_999_norm(raw))
    cur = set(r["hashes"])
    new = sorted(cur - base)
    live[STALLED_BASELINE_KEY] = sorted(cur)     # ★ 写 == cur，**不是 base ∪ cur**
    _reconcile_write(live)

    if r["n"] == 0 and first and not cur:
        # ★ 从来没卡过、现在也没有 ⇒ **一句话就够**，不配基线（给"没事"配基线没意义）。
        return (f"qB 卡种（分母 {r['total']}）：0 条",
                {"qb_stalled": 0, "qb_stalled_new": 0, "qb_stalled_total": r["total"]})
    if first:
        # ★ 首读**不响**（#47 立的规矩）：现存的那几条是"已接受的现状"，
        #   不是今天新冒出来的。响一次就得配冷却，而那正是要避免的噪音。
        verdict = f"首次读数，记基线（{r['n']} 条）"
    elif new:
        verdict = f"新增 {len(new)} 条"
    elif cur == base:
        verdict = "与基线一致"
    else:
        # 缩回（含清空）：静默采纳，但**必须在正文里写出来** —— 否则「清掉了」
        # 和「判据没读到」从外面看一模一样（#47 的教训，别省这一句）。
        gone = len(base) - len(cur)
        verdict = f"比基线少 {gone} 条（已采纳）" + ("—— 已清空" if not cur else "")

    # ★ 按档列（**只列命中过的档**）：四档的处置方向不同，合并成一个数就再也分不开。
    detail = ""
    if r["by_state"]:
        parts = "，".join(f"{k} {v}" for k, v in sorted(r["by_state"].items()))
        detail = f"  分档：{parts}\n"
    note = (f"qB 卡种（分母 {r['total']}）\n"
            f"  卡住的（stalledDL/metaDL/checkingDL/error）： {r['n']} 条 —— {verdict}\n"
            f"{detail}")
    # ★ 只报数量与档名，**不报种子名 / hash**（同 `qb_999_watch` 的口径：
    #   日报是要发出去的）。`hashes` 只参与基线运算，不进正文、不进 metrics。
    m: dict = {"qb_stalled": r["n"], "qb_stalled_total": r["total"],
               "qb_stalled_new": 0 if first else len(new)}
    for k, v in r["by_state"].items():
        m[f"qb_stalled_{k}"] = v
    return note, m


# --------------------------------------------------------------------------- #
# qB「装不出来」—— 缺的那一点点永远补不上的单种（**只识别 + 通知，不碰 qB**）
# --------------------------------------------------------------------------- #
# 判据本体在 `orchestrator/state.py`（`reseed_unbuildable_band` / `reseed_no_peer_band`），
# 理由同上面几条：**本进程跑在 NAS 宿主机上**，判据只有一份才谈得上两侧一致。
# 现场与因果链写在 `orchestrator/state.py` 那两段的注释里（真因**不是**「校验不通过」，
# 而是「发布组把 `.nfo`/`.jpg` 附件也写进了种子，而农场里没有这些附件」）。
#
# ★★ 用户 2026-09-18 拍板：**只做「识别 + 通知」，一点不碰 qB**
#    （不 pause / 不打 tag / 不删）。他目前靠**手动限速**把这些种子临时隔离，
#    要的是先「冻结」在观测里，处置办法后面再定。
#    ⇒ 本函数**只读** qB（`qbit_all_torrents` 一个 GET），写只写自己的状态文件。
#    ★ 将来若有人想在这里补一句 `pause`：先回去读上面那行 —— 那是**否决过**的方案，
#      不是漏掉的 TODO。
#
# ★ 分两格报，不并成一个数：两格的**处置方向相反**
#    （「装不出来」= 存量、已定型；「没 peer」= 会变，可能自己活过来）。
#    并起来就再也分不开了（同 `qb_999_band` 不并进主账的理由）。
#
# ★ IYUU 来源的**只记数、不处理，但必须出现在邮件里**（用户原话：「有的是 iyuu
#   推过来的种子，这先不管，但也要添加到邮件通知里」）⇒ 正文里**显式**分层列出，
#   绝不因为它"不是我们造成的"就从通知里静默滤掉。
#
# ★ 基线**单独一个文件**（`.reseed-freeze.state`），与 `qb_999_watch` 走
#   `.reconcile.state` 不同：那边那条「读→改→写必须紧挨着、别夹在
#   `reconcile_watch` 的读与写之间」的约束是对 `.reconcile.state` 成立的，
#   这里用自己的文件就**没有**那个约束（照 `linkguard_watch` 的做法）。
#   同理，本函数**不需要**被摆在 `report_daily` 的某个特定位置。
#
# ★ 只报**数量与来源**，绝不报种子名 / tracker / content_path（同 `qb_999_watch`
#   的口径：日报是要发出去的）。要知道具体是哪几条，跑**只读**的
#   `scripts/diag/reseed-freeze-report.py`（人主动跑，且也要显式 `--show-hashes`）。
RESEED_FREEZE_FILE = STATE_DIR / ".reseed-freeze.state"
RESEED_FREEZE_KEY = "_reseed_freeze"
RESEED_FREEZE_SRC_OURS = "cross-seed"
RESEED_FREEZE_SRC_IYUU = IYUU_TAG
#: 两个分组的**规范组名**（顺序也是正文里的顺序）。★ 单独抽出来是因为它被三处
#: 引用（`_reseed_freeze_norm` / `_reseed_freeze_flat` / `reseed_freeze_watch`）——
#: 把字面量抄三遍，改一处漏两处是迟早的事，而那类错**不报错**、只是某一格悄悄空掉。
RESEED_FREEZE_GROUPS = ("unbuildable", "no_peer")
#: 告警冷却的 key。★ 必须是**固定字面量** —— 绝不把条数写进去（那会每天生成一个
#: 新桶、等于没有冷却，同 `alert_blocked_indexers` 的 `indexer-blocked:{name}`）。
RESEED_FREEZE_ALERT_KEY = "reseed-freeze"


def _reseed_freeze_read() -> dict:
    try:
        d = json.loads(RESEED_FREEZE_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:                  # noqa: BLE001
        # ★ 坏掉的基线**不能当空**处理 —— 那会把现存每一条都算成「新增」，
        #   当晚就发一封假告警。异常路径与 `qb_999_watch` 的 `raw is None`
        #   殊途同归：返回 {} ⇒ 下面走 `first=True` ⇒ **静默重新起锚**
        #   （代价是这一轮失去检测能力，可接受；假警报比漏报更消耗信任）。
        LOG.warning("装不出来基线读不出（本轮按「无基线」重新起锚）: %s", e)
        return {}


def _reseed_freeze_write(d: dict) -> None:
    try:
        RESEED_FREEZE_FILE.write_text(json.dumps(d, ensure_ascii=False),
                                     encoding="utf-8")
    except OSError as e:
        LOG.warning("装不出来基线写不进去: %s", e)


def _reseed_freeze_norm(d) -> dict:
    """把基线归一成 `{组: {来源: [hash...]}}`（老版本 / 坏形状一律当空）。

    ★ 归一在**读**这一侧做（同 `_qb_999_norm` / `_unclaimed_norm`）：老版本写下的
      文件可能没有某个键，读的时候不补就会 KeyError。
    ★ 形状是**两层字典**而不是平铺的 hash 集合 —— 因为正文要按「组 × 来源」分层，
      而这个分层的依据（tag）**只有 qB 那侧才有**，基线里不带上就永远补不回来。
    """
    out: dict[str, dict[str, list]] = {}
    d = d if isinstance(d, dict) else {}
    for grp in RESEED_FREEZE_GROUPS:
        g = d.get(grp)
        g = g if isinstance(g, dict) else {}
        out[grp] = {}
        for src in (RESEED_FREEZE_SRC_OURS, RESEED_FREEZE_SRC_IYUU):
            v = g.get(src)
            out[grp][src] = sorted(x for x in (v if isinstance(v, list) else []) if x)
    return out


def _reseed_freeze_flat(groups: dict) -> set:
    """把两层形状拍平成「组:hash」的集合 —— 基线的**比较**按这个做。

    ★ 为什么带上组名前缀：同一条种子若从「没 peer」变成「装不出来」（或反过来），
      那是**真的变了**，该报一次。拍平时丢掉组名就看不见这种迁移。

    ★★ 只遍历 `RESEED_FREEZE_GROUPS` 这两个**组名**，绝不 `for k, v in groups.items()`
      —— 本函数的入参**有两个来源**：`_reseed_freeze_norm()`（只含两组）和
      `_reseed_freeze_split()`（**多一个标量 `total`**）。2026-09-18 实测踩到：
      用 `.items()` 会把 `total=1999` 那个整数当成分组字典去 `.items()` ⇒
      `AttributeError: 'int' object has no attribute 'items'`。
      ⇒ 形状的**边界由这里钉死**，不依赖调用方传得干净。
    """
    s = set()
    for grp in RESEED_FREEZE_GROUPS:
        by_src = (groups or {}).get(grp)
        if not isinstance(by_src, dict):
            continue
        for src, hashes in by_src.items():
            for h in hashes or []:
                s.add(f"{grp}:{h}")
    return s


def _reseed_freeze_split(torrents: list, url: str) -> dict:
    """把两条判据的结果各自**按来源 tag 分桶**。

    来源只看 tag（`cross-seed` / `IYUU自动辅种`）—— ★ 绝不看 `tracker` /
    `content_path`（README 安全约定：绝不打印它们；这里连"读进来分桶"都不做）。
    两个 tag 都没挂的（理论上不该有）落进 `others`：只计数、**不进告警正文**
    （不知道是谁的不该冒充"我们搜来的"）。
    """
    by_hash = {t.get("hash"): t for t in torrents or [] if t.get("hash")}

    def bucket(band: dict) -> dict:
        out = {RESEED_FREEZE_SRC_OURS: [], RESEED_FREEZE_SRC_IYUU: [], "others": []}
        for h in band["hashes"]:
            tags = [x.strip() for x in ((by_hash.get(h) or {}).get("tags") or "").split(",")]
            if RESEED_FREEZE_SRC_OURS in tags:
                out[RESEED_FREEZE_SRC_OURS].append(h)
            elif RESEED_FREEZE_SRC_IYUU in tags:
                out[RESEED_FREEZE_SRC_IYUU].append(h)
            else:
                out["others"].append(h)
        return out

    return {"unbuildable": bucket(S.reseed_unbuildable_band(torrents)),
            "no_peer": bucket(S.reseed_no_peer_band(torrents)),
            "total": len(torrents or [])}


def reseed_freeze_watch(args) -> tuple[str, dict]:
    """认「装不出来」与「没 peer」两类停滞单种 → (给日报正文的一段, 给 metrics 的字典)。

    ★ **绝不抛**：挂在每天一次的日报里，而日报挂在每 15 分钟一批的生产循环里
      （同 `iyuu_watch` / `qb_999_watch`）。
    ★ 读不到时 metrics 给 `n/a` —— **必须给**，否则 TSV 里「这次读失败了」和
      「那天根本没跑」长得一模一样，事后分不开（同 `iyuu_watch`）。
    ★ 首次读数**只记基线、不告警**；与基线一致**不告警**；缩回**静默采纳但要印在
      正文里**（否则「清掉了」与「判据没读到」从外面看一模一样）；只有**真新增**
      才 `emit("alert")`。五条理由逐条同 `qb_999_watch`，不再重复。
    """
    url = getattr(args, "qbit_url", None)
    na = {"fz": "n/a", "fz_new": "n/a", "fz_ours": "n/a", "fz_iyuu": "n/a",
          "fz_np": "n/a", "fz_np_iyuu": "n/a", "fz_total": "n/a"}
    if not url:
        return "装不出来的单种：跳过（没有 --qbit-url）", dict(na, fz="no-url")
    try:
        torrents = S.qbit_all_torrents(url)
        cur_by = _reseed_freeze_split(torrents, url)
    except Exception as e:              # noqa: BLE001 —— 附属观测，绝不拖垮日报
        LOG.debug("读装不出来的单种失败", exc_info=True)
        return (f"装不出来的单种：读不到（{type(e).__name__}: {e}）", dict(na))

    live = _reseed_freeze_read()
    raw = live.get(RESEED_FREEZE_KEY)
    first = raw is None                 # ★ 与「基线记过一个空集」是两回事
    base_by = _reseed_freeze_norm(raw)
    cur = _reseed_freeze_flat(cur_by)
    base = _reseed_freeze_flat(base_by)
    new = sorted(cur - base)
    # ★ 写 == cur，**不是** base ∪ cur（缩回要被采纳，否则会永远挂着旧条目）
    live[RESEED_FREEZE_KEY] = {
        "unbuildable": {k: sorted(v) for k, v in cur_by["unbuildable"].items()},
        "no_peer": {k: sorted(v) for k, v in cur_by["no_peer"].items()},
    }
    _reseed_freeze_write(live)

    ub, np_ = cur_by["unbuildable"], cur_by["no_peer"]
    n_ub = sum(len(v) for v in ub.values())
    n_np = sum(len(v) for v in np_.values())
    n_our = len(ub.get(RESEED_FREEZE_SRC_OURS) or []) + len(np_.get(RESEED_FREEZE_SRC_OURS) or [])
    n_iyuu = len(ub.get(RESEED_FREEZE_SRC_IYUU) or []) + len(np_.get(RESEED_FREEZE_SRC_IYUU) or [])
    n_oth = (len(ub.get("others") or []) + len(np_.get("others") or []))

    if first:
        verdict = f"首次读数，记基线（{len(cur)} 条）—— 不告警"
    elif new:
        verdict = f"★ 新增 {len(new)} 条（基线 {len(base)} 条）"
    elif cur == base:
        verdict = "与基线一致 —— 不告警"
    else:
        # 缩回（含清空）：静默采纳，但**必须在正文里写出来** —— 否则「清掉了」
        # 和「判据没读到」从外面看一模一样（#47 的教训，别省这一句）。
        # ★ 这里**不许**打印「与基线一致」：基线刚刚被改写成 cur，那句是假话。
        gone = len(base) - len(cur)
        verdict = (f"比基线少 {gone} 条（已采纳）"
                   + ("—— 已清空" if not cur else ""))

    note = (f"未完成且停滞的单种（分母 {cur_by['total']}）\n"
            f"  ① 装不出来（缺口 ≤ 0.1%，永远补不上）： {n_ub} 条\n"
            f"     来源：本项目 {len(ub.get(RESEED_FREEZE_SRC_OURS) or [])}"
            f" / IYUU {len(ub.get(RESEED_FREEZE_SRC_IYUU) or [])}（IYUU 只记数、不处理）\n"
            f"  ② 没 peer（一个字节都没下到、源也看不见）： {n_np} 条\n"
            f"     来源：本项目 {len(np_.get(RESEED_FREEZE_SRC_OURS) or [])}"
            f" / IYUU {len(np_.get(RESEED_FREEZE_SRC_IYUU) or [])}（IYUU 只记数、不处理）\n"
            f"  共 {len(cur)} 条 —— {verdict}")
    if n_oth:
        # ★ 挂了别的 tag 的（理论上不该有）：**只报数量**，不塞进上面两行里去
        #   冒充「本项目」或「IYUU」。要让来源永远可追溯。
        note += f"\n  （另有 {n_oth} 条挂了别的 tag，未计入上面任一行）"
    if not first and new:
        # ★ 只在**真新增**时喊一声（原先 `emit("alert", …, key=RESEED_FREEZE_ALERT_KEY)`）。
        #   正文里**绝不**带 hash / 种子名 / tracker / content_path。
        # ★ 通知写侧已砍掉（2026-10-05）：改为纯日志（下面这句）。

        LOG.warning("  ⚠ 装不出来的单种新增 %d 条（分母 %d）：装不出来 %d / 没 peer %d —— "
                    "本工具不做任何处置，定位用 "
                    "`scripts/diag/reseed-freeze-report.py --show-hashes`。",
                    len(new), cur_by["total"], n_ub, n_np)

    # ★ 键名一律 `fz_` 前缀，且**绝不**恰好叫 `pack`、**绝不**含空格 ——
    #   （原先这条是给 `notify.py:_render` 的 key 规范化用的；通知写侧已砍掉，
    #    但 metrics 键名仍是**对外的稳定口径**，保持不动。）
    return note, {"fz": len(cur), "fz_new": 0 if first else len(new),
                  "fz_ours": n_our, "fz_iyuu": n_iyuu,
                  "fz_np": n_np, "fz_np_iyuu": len(np_.get(RESEED_FREEZE_SRC_IYUU) or []),
                  "fz_total": cur_by["total"]}


# --------------------------------------------------------------------------- #
# 链接守护：既有 628 条硬链接有没有被**写穿**
# --------------------------------------------------------------------------- #
# 判据本体在 `orchestrator/state.py`（`linkguard_snapshot` / `linkguard_diff` /
# `linkguard_owner` / `linkguard_inflight`），理由同上面几条：**本进程跑在 NAS
# 宿主机上**，判据只有一份才谈得上两侧一致。
#
# 立这一节的现场（2026-09-13，见 README「源文件被写穿」）：cross-seed 在
# `matchMode=partial` 下把「名称+大小匹配、但 piece 不一致」的单种也注入 qB；
# qB 校验不通过**不报错**，而是**就地重下**那几个 piece —— 而 linkDirs 是硬
# 链接（与源同一个 inode），这一写**直接落到源文件上**，Farm 侧还显示 100%。
#
# 修法分两步，**这一节只负责第二步**：
#   ① 关上闸门：`matchMode` ⇄ `linkType` 互锁（cross-seed/config.js），只有
#      reflink(COW) 才允许 partial ⇒ **将来新建**的链接写不穿。
#   ② 给**已经存在**的那 628 条上锚：它们是硬链接，且**故意不重建**（重建要
#      重摆几十 TB 的链接，风险大于收益）—— 但得能发现「它正在被写穿」。
#      判据就是这一节：文件 (size, mtime) 指纹基线 + 定期差分。
#
# ★ 基线**单独一个文件**（`.linkguard.state`），不塞进 `.reconcile.state`：
#   它是几千~几万条 (路径 → [size, mtime])，而 `.reconcile.state` 被好几个
#   watch **整文件读改写** —— 塞进去等于让每一个 watch 都搬一次这个体积。
#   本文件**只有本函数读写**，所以不需要 `qb_999_watch` 里那套
#   「读→改→写必须紧挨着、别夹在别人中间」的约束。
#
# ★ 只报**数量**，不报路径 / 不报种子名（同 `qb_999_watch` 的口径）：
#   日报是要发出去的。要知道具体是哪些，跑 `scripts/diag/crossseed-linkguard.py`
#   —— 那是人主动跑，且默认也只出 hash。
LINKGUARD_FILE = STATE_DIR / ".linkguard.state"
LINKGUARD_TAG = "cross-seed"
LINKGUARD_SNAP_KEY = "_linkguard_snapshot"
LINKGUARD_DETAIL_KEY = "_linkguard_detail"


def _linkguard_read() -> dict:
    try:
        d = json.loads(LINKGUARD_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:                  # noqa: BLE001
        # ★ 坏掉的基线**不能当空**处理 —— 那会把每个文件都算成「新增」，然后
        #   当成一次事件报出去。假警报比漏报更消耗信任。返回 {} 会让下面走
        #   `first=True` 分支：静默重新起锚（代价是这一轮失去检测能力，可接受）。
        LOG.warning("链接守护基线读不出（本轮按「无基线」重新起锚）: %s", e)
        return {}


def _linkguard_write(d: dict) -> None:
    # ★ 直接写、**不做「写临时文件再改名」**：临时名（`.linkguard.state.tmp`）
    #   不匹配 check-deploy-drift 的 `^drive-loop/scripts/\.[^/]+\.state$`，
    #   扫到就会被报成「未知文件」。而写坏的后果已经被 `_linkguard_read` 兜住
    #   （解析失败 ⇒ 静默起锚，不误报），所以原子性的收益不值这个噪音。
    LINKGUARD_FILE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def linkguard_watch(args) -> tuple[str, dict]:
    """看那 628 条既有硬链接有没有被就地改写 → (给日报正文的一段, 给 metrics 的字典)。

    ★ **绝不抛**：挂在每天一次的日报里，而日报挂在每 15 分钟一批的生产循环里
      （同 `iyuu_watch` / `qb_999_watch`）。
    ★ 读不到时 metrics 给 `n/a` —— **必须给**，否则 TSV 里「这次没读到」和
      「那天根本没跑」长得一模一样（同 `iyuu_watch`）。
    """
    na = {"lg_changed": "n/a", "lg_added": "n/a", "lg_removed": "n/a",
          "lg_files": "n/a", "lg_inflight": "n/a", "lg_seeded": "n/a"}
    url = getattr(args, "qbit_url", None)
    if not url:
        return "链接守护：跳过（没有 --qbit-url）", dict(na, lg_changed="no-url")
    try:
        # 服务端按 tag 过滤，再在本地用「子串」复核一次 —— 判据与当初那把
        # 看门狗（watchdog-xp.py）保持一致，别在这里引入第二套口径。
        torrents = [t for t in S.qbit_tagged(url, LINKGUARD_TAG)
                    if LINKGUARD_TAG in [x.strip() for x in (t.get("tags") or "").split(",")]]
    except Exception as e:                  # noqa: BLE001 —— 附属观测，绝不拖垮日报
        LOG.debug("链接守护：取 qB 失败", exc_info=True)
        return (f"链接守护：读不到 qB（{type(e).__name__}: {e}）", dict(na))

    roots = sorted({(t.get("save_path") or "").rstrip("/") for t in torrents} - {""})
    if not roots:
        return ("链接守护：这些种子都没有 save_path，跳过", dict(na, lg_changed="no-path"))
    try:
        cur = S.linkguard_snapshot(roots)
    except Exception as e:                  # noqa: BLE001
        LOG.debug("链接守护：扫盘失败", exc_info=True)
        return (f"链接守护：扫盘失败（{type(e).__name__}: {e}）", dict(na))

    # ★ 一个文件都没扫到 ⇒ **绝不写基线**。写了就等于把「扫了个空」记成现状，
    #   下一轮拿它比对会把所有文件报成「新增」（整条链路最容易踩的假警报）。
    #   宁可这一轮 metric 记 n/a、基线保持不动。
    if not cur:
        return (f"链接守护：扫到 0 个文件（save_path 根 {len(roots)} 个）—— 基线保持不变",
                {"lg_changed": "n/a", "lg_added": "n/a", "lg_removed": "n/a",
                 "lg_files": 0, "lg_inflight": "n/a", "lg_seeded": "n/a"})

    state = _linkguard_read()
    first = LINKGUARD_SNAP_KEY not in state     # ★ 与「基线记过一个空集」是两回事
    base = state.get(LINKGUARD_SNAP_KEY) or {}
    d = S.linkguard_diff(base, cur)
    inflight = S.linkguard_inflight(torrents)

    # 受影响的文件按「属于哪条种子」归组（给 --rebuild-plan 用；不进正文）
    # ★★ 首读时**这份清单必须留空**。那一轮 base 是空的，`linkguard_diff` 会把
    #   **整库**算进 `added`，而这里把 `changed + added` 合并写进同一个键 ⇒
    #   `detail.changed` 里躺着 2816 个文件，于是谁去跑 `crossseed-linkguard.py`
    #   都会看到「★ 被改写 / 新增（共 2816 个文件）」，**而那天什么写穿都没发生**。
    #   2026-09-13 当场实测（18:00 那次起锚就是这个输出），记在这里当证据。
    #   正文与 metrics 本来就是对的（`first` ⇒ 三个数全 0），错只错在这份**给诊断端
    #   读的**清单上 —— 与「扫了个空绝不落盘」是同一族假警报：**逐字看着像真的**。
    detail: dict[str, dict] = {}
    if not first:
        for p in d["changed"] + d["added"]:
            h = S.linkguard_owner(p, torrents) or "?"
            e = detail.setdefault(h, {"n": 0, "sample": []})
            e["n"] += 1
            if len(e["sample"]) < 5:
                e["sample"].append(p)

    state[LINKGUARD_SNAP_KEY] = cur
    state[LINKGUARD_DETAIL_KEY] = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "changed": detail,
        "removed_n": len(d["removed"]),
        "removed_sample": d["removed"][:5],
        "inflight": inflight["hashes"],
        "files": len(cur),
    }
    try:
        _linkguard_write(state)
    except OSError as e:
        LOG.warning("链接守护基线写不进去: %s", e)

    n_ch, n_ad, n_rm = len(d["changed"]), len(d["added"]), len(d["removed"])
    if first:
        # ★ 首读**不响**（#47 立的规矩）：现存的差异是「已接受的现状」，不是今天
        #   新冒出来的。响一次就得配冷却，而那正是要避免的噪音。
        verdict = f"首次读数，记基线（{len(cur)} 个文件）"
    elif n_ch or n_ad or n_rm:
        verdict = "★ 有变化 —— 见下面一行"
    elif inflight["n"]:
        verdict = "与基线一致（但有种子正在动）"
    else:
        verdict = "与基线一致"

    note = (f"链接守护（既有硬链接的锚；{len(torrents)} 条种子 / {len(cur)} 个文件）\n"
            f"  被改写 {n_ch} · 新增 {n_ad} · 消失 {n_rm} · 正在动 {inflight['n']}"
            f" —— {verdict}")
    if not first and n_ch:
        # 写穿是**必须有人动手**的那一类（要按需重建），所以单独点一句，
        # 但仍然只说数量与去哪里看 —— 名字不进日报。
        note += ("\n  ⚠ 被改写 = 内容被就地覆写（写穿的直接证据）。"
                 "定位与重建清单：看 //iSunker-DS423/.../drive-loop/scripts/.linkguard.state 的"
                 " _linkguard_detail，或跑 scripts/diag/crossseed-linkguard.py --rebuild-plan")
    return note, {"lg_changed": 0 if first else n_ch,
                  "lg_added": 0 if first else n_ad,
                  "lg_removed": 0 if first else n_rm,
                  "lg_files": len(cur), "lg_inflight": inflight["n"],
                  "lg_seeded": 1 if first else 0}


# --------------------------------------------------------------------------- #
# 观测对账：a − b / b − c / 全场无人认领
# --------------------------------------------------------------------------- #
# ★ 判据本体**不在这个文件里**，在 `orchestrator/state.py`
#   （`count_found_lines` / `resolve_found_lines` / `unclaimed_searchees`）。
#   理由只有一条、但是硬的：**本进程跑在 NAS 宿主机上**
#   （drive-loop-nas.sh: /usr/bin/python3），而 `scripts/audit-found-*.py` 是
#   `check-deploy-drift.py` 里的 LOCAL_ONLY（"在 Windows 上跑"）——
#   那两个文件**根本不在 NAS 上**，就算拷上来，它们写死的 `//iSunker-DS423/...`
#   在 NAS 宿主机上也不存在。`orchestrator/state.py` 被部署两次
#   （构建上下文 + drive-loop/orchestrator/），是两侧**唯一都能到达**的地方。
#   两个脚本现在是薄壳，核的是同一份实现 —— 判据只有一份。
#
# ★ 为什么挂在**日报**里而不是每批：日报是这套系统里唯一"每天恰好一次"的
#   观测出口 —— 放进 `report_daily` 的 metrics，这些数才真的**留得下来**。
#   （原话还有半句"notify 只记 metrics 不记正文"；通知写侧已砍掉，理由不变。）
_REDACT_SUB = (
    (re.compile(r"(apikey=)[^,&\s)\]]+", re.I), r"\1<redacted>"),
    (re.compile(r"(passkey=)[^,&\s)\]]+", re.I), r"\1<redacted>"),
)


def _redact(s: str) -> str:
    for rx, rep in _REDACT_SUB:
        s = rx.sub(rep, s)
    return s


#: 对账读数台账：记下**每一格最后一次真的读到数**是什么时候。
#: ★ 为什么不并进 `.daily-report.state`：那个文件是 `_daily_set()` **整文件覆盖写**
#:   （`tmp.write_text(json.dumps({"last_day": day}))`）—— 并进去会被静默抹掉，
#:   而且是那种"这次抹了、下次才发现"的抹法。宁可多一个文件。
RECONCILE_FILE = STATE_DIR / ".reconcile.state"

#: `.reconcile.state` 里存「声明点基线」的保留键。
#: ★ 它**不是一格读数** —— 读数是「本次读到了什么」，基线是「**现状**（已接受的差集）」。
#:   前缀 `_` 是给未来的自己看的：`n/a` 记账那一圈只遍历 `metrics` 里的键，
#:   碰不到它，所以它会被原样带过去，不会被误当成"某个格子从没读到过"。
PACKS_BASELINE_KEY = "_packs_baseline"

#: `.reconcile.state` 里存「无人认领基线」的保留键（**已接受**的那些路径的集合）。
#: ★ 和 `_packs_baseline` 同一个理由、同一个形状：`unclaimed == 1`
#:   （`0观影清单chrlee整理`，只含一个 `.xlsx`）是**已接受的现状**，
#:   而告警有 12 小时冷却 ⇒ 只要那个目录还在，就**每天响 1~2 次、永远**。
#:   现状不是新闻 —— 只报**新增**：出现基线里没有的路径才响，缩回（含清空）静默采纳。
UNCLAIMED_BASELINE_KEY = "_unclaimed_baseline"


def _unclaimed_norm(d) -> list:
    """把基线归一成**排序后的路径列表**（老版本/坏形状一律当空）。"""
    if isinstance(d, dict):
        d = d.get("paths") or []
    return sorted(x for x in (d if isinstance(d, list) else []) if x)


def _pack_never_searched(store, pack: str) -> bool:
    """这个包**从没被搜过一次**吗？（`2026-10-05` 声明点对账的新判据）

    「被搜过」的证据（`movie` 表逐行看，任一成立即可）：
      · `attempts > 0`        —— 搜索次数记过（哪怕全 FAILED/SKIPPED）
      · `seeding_count > 0`   —— 正在做种（搜到并挂上了）
      · `stage ∈ DONE_STAGES` —— SEEDING / MATCHED
    三者皆无 ⇒ 从没被搜过（"白登记"）。

    ★ 只读、绝不抛（对账挂在每天一次的日报里，见 `reconcile_watch` 的规矩），
      但**不吞异常**：读库出错由调用方的 try/except 统一记 `算不出` —— 这里若
      自己 try 成"没被搜过"，就会把"读失败"误报成"白登记"（`ERR-AI-03` 那个形状）。
    ★ 包在 `movie` 表里**一行都没有**时，`movies()` 返回空 ⇒ 循环不执行 ⇒ 返回
      `True`（从没被搜过）—— 这正是要报的：登记了却没有 searchee 行。
    """
    for r in store.movies(pack):
        if (r["attempts"] or 0) > 0:
            return False
        if (r["seeding_count"] or 0) > 0:
            return False
        if r["stage"] in S.DONE_STAGES:
            return False
    return True


def _packs_norm(d) -> dict:
    """把基线读数归一成可比较的形状（缺字段补空、排序）。

    ★ 归一在**读**这一侧做，而不是在**写**那侧 —— 老版本写下的文件可能是
      `{"undriven": ["mbf"]}` 而没有别的键，读的时候不补就会 KeyError。
    ★★ 2026-10-05（去掉闸门）：判据改成「从没被搜过的包」单维 ⇒ 只剩 `undriven`。
      旧基线里可能还带一个 `unreg` 键（旧判据的"在名单里但库里没登记"那一头）——
      这里**读进来源忽略**它（`unreg` 这个概念随名单一起退休了）。
    """
    d = d if isinstance(d, dict) else {}
    return {"undriven": sorted(d.get("undriven") or [])}


def _reconcile_read() -> dict:
    try:
        d = json.loads(RECONCILE_FILE.read_text(encoding="utf-8"))
    except Exception:                       # 文件不存在 / 坏了 —— 都当"从没读到过"
        return {}
    return d if isinstance(d, dict) else {}


def _reconcile_write(ok: dict) -> None:
    try:
        tmp = RECONCILE_FILE.with_suffix(".state.tmp")
        tmp.write_text(json.dumps(ok), encoding="utf-8")
        os.replace(tmp, RECONCILE_FILE)     # 原子替换，别让下一轮读到半个文件
    except OSError:
        LOG.debug("对账读数标记写不进去（不影响跑批）", exc_info=True)


def _age_h(sec: float) -> str:
    """把秒数说成人话。**只是给人看的**，不参与任何判据。"""
    if sec < 90:
        return f"{sec:.0f} 秒前"
    if sec < 5400:
        return f"{sec / 60:.0f} 分钟前"
    if sec < 36 * 3600:
        return f"{sec / 3600:.1f} 小时前"
    return f"{sec / 86400:.1f} 天前"


def _prev_day_found_count(log_path: str) -> tuple[str | None, "S.FoundLineCount | None"]:
    """回退读**最近一个已轮转的完整日**日志 —— 只给「控制没过」那条当**证据**用。

    为什么这么找：cross-seed 的 info 日志**按天轮转**，轮转出来的名字是
    `info.<YYYY-MM-DD>.log`（口径见 `reconcile_watch` 那段）。文件名带 ISO 日期
    ⇒ **字典序即时间序**，取最后一个就是最近的那个完整日；`info.current.log`
    是当日的那个，排除掉。

    ★ 它**不参与判据** —— 判据永远还是 `S.count_found_lines` 那一个。
      这里只是把「昨天数得到吗」这个**便宜且可验证**的对照摆到告警正文里，
      好让读告警的人不必自己去猜该往哪儿查。
    ★ 读不动就回 None。这条告警**绝不抛**（它挂在每天一次的日报里，
      而日报挂在每 15 分钟一批的生产循环里）。
    ★ 不用 `Path.resolve()`：这条路径在 NAS 上是 SMB/网络路径，
      resolve 会真的去问文件系统，慢且可能失败；比名字就够了。
    """
    try:
        d = Path(log_path).parent
        cur = Path(log_path).name
        cands = sorted(p for p in d.glob("info.*.log")
                       if p.name != "info.current.log" and p.name != cur)
    except OSError:
        return None, None
    if not cands:
        return None, None
    p = cands[-1]
    try:
        return str(p), S.count_found_lines(p.read_text(encoding="utf-8", errors="replace"))
    except (OSError, UnicodeError):
        LOG.debug("回退读完整日日志失败：%s", p, exc_info=True)
        return str(p), None


def reconcile_watch(args) -> tuple[str, dict]:
    """四类对账的读数 → (给日报正文的一段, 给 metrics 的字典)。

    四条各自的对账基准（都指回判据之外的**真实记录**）：
      · a − b：`] Found ` 那个字面量会吃到别的消息（实测 2231 vs 靶心 1011），
        所以判据是**六个字面量的合取**；a == b == 1011。
      · b − c：三包**共用一个 farm_root** → **生产口径**下 other_pack 恒非零
        （实测 865 / 146 / 1011）。所以这里报的是**全量口径**的 b − c，
        并且 metrics 名带 `all` —— 不许念成"生产里没有静默丢行"。
      · 无人认领：实测 1888 条里恰好 1 条（`0观影清单chrlee整理`，只含一个 xlsx）。
        这个数才恒为 0，且非零时**每条都指得出名字**。
        ★ **只报新增**（同 `--packs` 那条）：那 1 条是**已接受的现状**，
          配上 alert 的 12 小时冷却就是"每天响两次、永远" —— 现状不是新闻。
          基线（已接受的那几条路径）存 `.reconcile.state` 的 `_unclaimed_baseline`；
          出现基线里没有的路径才响，缩回（含清空到 0）静默采纳但**在正文里写出来**。
      · 声明点〔`pack` 表〕：★★ 2026-10-05（去掉「包的闸门」）**改了判据**。
        旧判据 = `pack` 表 − `--packs` 名单的差集，问「**登记了却没被驱动**的包」——
        去掉名单后名单这个概念没了，那个差集**恒为空**（§18.18「恒绿摆设」形状），
        于是判据必须换一个**仍然会红**的问题：
        **新判据 = 「登记的包里，从没被搜过一次的」**。
        证据（只读 `movie` 表，逐包判）：一个包被搜过 ⟺ 它有任一条件成立 ——
          `attempts > 0`（搜索次数记过）∨ `coalesce(seeding_count,0) > 0`（做着种）
          ∨ `stage ∈ DONE_STAGES`（SEEDING/MATCHED）。
        ⇒ 三者皆无 = **白登记**：状态机有它、农场有它、池子也会点它，但它**一次
          都没被搜到过**（成因可能是 DB 里没 searchee、农场没硬链接、路径全错）。
        ★ 与旧判据**同一个病灶**（"认得出来、就是不产读数"），只是**成因从"不在
          名单"换成了"登记时没落实"** —— 去掉闸门后前者已不可能，后者仍会。
        ★ 基线 0；涨到非 0 就是又落下一个这样的包。缩回基线之内**静默采纳**
          （修好了不该喊），但正文里写出来。
        ★ 只报**变化**：`_packs_baseline` 现在是 `{"undriven": [<包名>]}` 单维。
          旧的 `{"undriven":…, "unreg":…}` 基线读进来时靠 `_packs_norm` 兼容
          （`unreg` 维被忽略，不再参与判据）。

    ★ **口径的第三个维度：这次的读是「当日日志」，不是"全量"。**
      `--log` 指向的 `info.current.log` 由 cross-seed **按天轮转**
      （实测 09-12 当天 6.2 MB，旁边躺着 `info.2026-09-11.log` 3.5 MB），
      所以这三个数天然是「**从今天 00:00 到现在**」，会随一天推进而涨。
      ★ 这不是缺陷，但必须先说清楚 —— 否则"昨天 1011、今天日报说 400"
      会被当成判据坏了。手工核总数用 `scripts/diag/audit-found-lines.py`
      （它读的就是同一个当日文件，两者应当对得上）。
      ★ 顺带：正因为按天轮转，这里**可以**整读全文 —— 不会涨到几百 MB。
      （对比 `STALE_ENV_TAIL_BYTES` 那边只读尾部：那是**每批都跑**的热路径。）

    ★ **绝不抛**：它挂在每天一次的日报里，而日报挂在**每 15 分钟一批**的生产
      循环里。一次 SMB/库抖动不该让整份日报消失（同 `iyuu_watch` 的规矩）。
    ★ 读不到时 metrics 给 `n/a` —— **必须给**，否则 TSV 里"这次读失败了"和
      "那天根本没跑"长得一模一样，事后分不开。
    ★★ `n/a` 还差一半：它**只说明这一次**。所以本函数末尾会把"每一格最后一次
      真的读到数"的时刻记进 `.reconcile.state`，并在有 `n/a` 时写进正文 ——
      刚抖一下 vs 连续三天读不到，从此分得开（#34）。
    ★★ 但"读不到"有个**反向的**坑：`StateStore(path)` 会**把不存在的库建出来**，
      于是"库不在"会伪装成"库是空的"→ 报出一个巨大的 `unclaimed`。这里问一句
      `is_file()`（见下面的注释）；**根因**已经在 `StateStore.__init__(create=False)`
      堵上 —— 这里这一问现在是第二道，不是唯一那道。
    ★★ **三个出口的判断顺序就是判据本身**，别随手调换：
        ① `not controls_ok`   —— 判据压根没走到（连基线字面量都没数到）
        ② `delta`             —— 形状数到了、正则没吃下（**含 b == 0 的极端形**）
        ③ `b == 0`            —— 走到这 ⇒ `a == 0`，是"真没有 Found 行"，不是"正则坏了"
      旧顺序是 ①③②，于是 `a > 0 且 b == 0`（delta = a > 0，最严重的那种）
      被 ③ 吞了，正文和 metrics 互相打脸。**错的不是数，是它指向的排查动作。**
      四条分支在 `tests/test_reconcile.py` §④ 里**逐条钉了 key**。
    """
    m: dict = {"fa": "n/a", "fb": "n/a", "fd": "n/a",
               "fb_c_all": "n/a", "fb_c_farm": "n/a", "unclaimed": "n/a",
               "packs_undriven": "n/a"}
    lines: list[str] = []
    prev_ok = _reconcile_read()             # #34：每一格上次真的读到数是什么时候
    packs_baseline = None                   # 本次算出的新基线（没算就保持 None，别抹掉旧的）
    unclaimed_baseline = None               # 同上（无人认领的已接受路径集合）

    log_path = (getattr(args, "log", None) or [None])[0]
    text = None
    if log_path:
        try:
            text = Path(log_path).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            LOG.debug("读 cross-seed 日志失败", exc_info=True)
            lines.append(f"观测对账：读不到日志（{type(e).__name__}）—— "
                         f"a−b **没跑成**，别念成 0")
    else:
        lines.append("观测对账：跳过（没有 --log）")

    if text is not None:
        c = S.count_found_lines(text)
        m.update(fa=c.a, fb=c.b, fd=c.delta)
        lines.append(
            f"观测对账 a−b〔当日日志〕：形状 {c.a} / 正则 {c.b} / 差 {c.delta}"
            f"（期望差 0；合取比 L1 单字面量收窄了 "
            f"{c.lit_counts.get(S.L1_LABEL, 0) - c.a} 行）")
        if not c.controls_ok:
            # ★★★ 2026-09-14 改。原话是「说明**匹配逻辑坏了**」——
            #   **那是下结论，而且下反了。**
            #   `controls_ok` 的定义只有一条实质条件：`lit_counts[L1] > 0`，
            #   也就是**当日日志里至少出现一次 `] Found `**。于是它必然在
            #   「本日还没搜出去」时失败 —— 而那**不是**判据坏了。实测就是它：
            #       当日日志 40 行 / L1 命中 0 / a = b = 0 → 报警「匹配逻辑坏了」
            #     而用**同一个函数、同一张字面量表**跑前三个完整日：
            #       09-11 a=399 b=399 a−b=0 ｜ 09-12 a=1011 b=1011 a−b=0
            #       09-13 a=179 b=179 a−b=0  （三个完整日**全是 0 差**）
            #   09-14 之所以是 0，只因**那天一条 Found 行都没产生**（HDtime 退避）。
            #   ★ 再叠上「日报在当天第一批（00:0x–01:3x）采样」这个已知口径
            #     （见本函数头部那段）—— 任何安静的夜都必然踩空 =
            #     **每天一封假告警**。而本文件 754-758 那段自己写着：
            #     假告警会把人喊到不再看它，那正好毁掉告警通道。
            #   ⇒ 所以这里**不下结论**，改成给**可验证的对照**：
            #     回退读最近一个**完整日**。昨天数得到、今天数不到 ⇒ 是口径；
            #     两天都数不到 ⇒ 这才该往 `_RE_FOUND` 和那六个字面量上想。
            #   ★ metrics 仍报真数（0）—— 它确实是「从 00:00 到现在」的计数；
            #     要修的是**它被念成了什么**，不是这个数（§18.17.3 那条：错的
            #     不是数，是它指向的排查动作）。
            prev_name, prev = _prev_day_found_count(log_path)
            if prev is None:
                ev = ("对照：同一目录下没有别的已轮转日志 —— **这次判不了**"
                      "是不是判据坏了，别急着改正则。\n")
            else:
                ev = (f"对照〔最近一个完整日〕{Path(prev_name).name}："
                      f"总行 {prev.total_lines} / 形状 {prev.a} / 正则 {prev.b}"
                      f" / L1 命中 {prev.lit_counts.get(S.L1_LABEL, 0)}\n")
                if prev.controls_ok:
                    ev += ("   ⇒ 那个**数得到** ⇒ 判据没坏。本日的 0 只是"
                           "「当日口径」的 0（今天还没搜出去）。\n"
                           "     手工复核用 scripts/diag/audit-found-lines.py"
                           "（读的是同一个函数）。\n")
                else:
                    ev += ("   ★ 那个**也数不到** ⇒ 这才可能是**匹配逻辑真的坏了**"
                           "—— 去核 `_RE_FOUND` 与那六个字面量。\n")
            LOG.warning("观测对账：本日尚无 Found 行（判据不可判）—— 当日日志里一条 "
                        "`] Found ` 都没有，下面的 0 什么都不说明。%s", ev.strip())
        elif c.delta:
            # ★★★ 顺序很关键：这一支必须排在 `b == 0` **前面**。
            #   旧顺序是 `controls_ok → b == 0 → delta`，于是 `a > 0 且 b == 0`
            #   （delta = a > 0，**最严重**的那种「形状对、正则一条都没吃下」）
            #   被前面的 `b == 0` 吞了：正文说「要么真没搜出去过，要么日志抓错了」，
            #   而 metrics 里 `fa` 明明白白 > 0 —— **两个字段互相打脸**。
            #   看告警的人只读正文，于是会去查「为什么没搜」，而真问题是正则整条失效。
            #   ★ 错的不是数，是它**指向的排查动作**（同 §18.17.3 那条）。
            head = "\n".join("   " + _redact(s)[:190] for s in c.unparsed) or "   （没留样本）"
            extra = ""
            if c.b == 0:
                extra = (f"   ★★ b == 0：**一条都没吃下** —— 形状明明数到了 {c.a} 行，"
                         "这不是「今天没搜出去」（那要 a == 0），是正则**整条失效**。\n")
            LOG.warning("观测对账：%d 行「形状对、正则没吃下」\n%s   ★ 这些行在台账里"
                        "等于没发生过（抓不到=不存在）\n%s",
                        c.delta, extra, head)
        elif c.b == 0:
            # ★ 走到这里 ⇒ `delta == 0` 且 `a == 0`（上面那支已把 delta > 0 接走）。
            #   即**形状一条都没数到** —— 不是「正则没吃下」（那样 delta 会 > 0），
            #   是**根本没有 Found 行**。§18.17.3：零命中既可能是没事，也可能是没跑。
            LOG.warning("观测对账：一条 Found 都没抓到（a=%d b=%d）—— **这不是干净**："
                        "要么真没搜出去过，要么日志抓错了文件。日志 %s 总行 %d。",
                        c.a, c.b, log_path, c.total_lines)

        # ★★★ 2026-10-02 加（§32.16 第 7 个真 bug 的处置）：`other_found` 非空 ⇒ **投告警**。
        #   为什么：`other_found` 是个**兜底格** —— 任何**没预料到的** `] Found ` 形状都掉
        #   进来，且历史上**一声不响**。这正是它为什么会藏住 bug：cross-seed 有**两种**
        #   Found 行，判据（`FOUND_LITS` 六字面量）只描述**单行式**
        #   `Found <片名> [hash] on <站> by <判定> from dataDir (…) - …`；
        #   另一种**三行式** `[webhook] Found N torrents for {` 长期掉进这一格
        #   （09-16~09-21 每天几百条、10-01 还有 136 条），**没人知道**。
        #   ⇒ 判据如实把它报出来：**"我看见了一种我不认识的东西"**。
        #   ★ 这一条**独立于上面 a/b 三条告警**（不放进 elif 链）—— 因为
        #     "有没识别的形状"与"a/b 是否健康"是**两件事**：a==b 可能很干净
        #     （三行式根本不参与 a/b），而其它形状照旧在漏。
        #   ★★ 口径（本次拍板）：**a 的口径是「单行式（可回灌）已识别条数」，不是
        #     「搜出去总量」**。三行式**不进 a** —— 它没有站名（只有 `path:`），
        #     拿不到「哪个站搜到了」这一维，状态机回灌用不上它。
        #     所以这里**不**把三行式并入 a（那会让 a−b 常年为正、判据恒红），
        #     而是**显式报一条**「有 N 行别的 Found 形状（含三行式 M 行）」。
        #   ★ 冷却：用独立 key，与 a/b 三条互不遮蔽。基线不设 —— 每一条告警都是
        #     "又出现没识别的形状"，值得人看一眼；真有稳定存在的新形状，应当
        #     去改 `FOUND_LITS` / `_RE_FOUND`，而不是把它养成常态。
        if c.other_found:
            n_other = sum(c.other_found.values())
            # 三行式单独点名：它是历史上最大的一块（每天几百条），且**已知** ——
            # 不是为了"修"，是为了让读数**一眼知道是它**，不必再去猜。
            n_3line = next((v for k, v in c.other_found.items()
                            if "torrents for" in k), 0)
            detail = "\n".join(f"   {v:>5}  {k}" for k, v in
                               sorted(c.other_found.items(), key=lambda kv: -kv[1])[:6])
            LOG.warning("观测对账：%d 行「别的 Found 形状」（判据没识别，含三行式约 %d 行）\n"
                        "   ★ a 的口径是「单行式（可回灌）已识别条数」，不是「搜出去总量」。\n"
                        "---- 别的 Found 形状（形状→行数）----\n%s", n_other, n_3line, detail)

    # b − c 与无人认领都要真库；库不可达时**分开报**，别让一条坏了带走另一条。
    store = snap = None
    try:
        db_p = getattr(args, "db", None)
        if db_p:
            # ★★ 必须先问「文件在不在」，不能直接把它交给 StateStore ——
            #    `StateStore.__init__` 的第一件事就是 mkdir + connect +
            #    `executescript(SCHEMA)`，**库不存在时会就地建一个空的**。
            #    那样「读不到」就变成了「读到了一个空库」：pack/movie 两张表全空
            #    → `pack_contexts()` 返回 `{}` → 库里**每一条** searchee 都算
            #    「无人认领」→ 报出一个巨大的假数 + 一条醒目告警，顺带在错位置
            #    写下一个空库。**沉默被念成了数字**，正是这一格要防的方向
            #    （而且它和「零命中既可能是没事也可能是没跑」是同一个形状）。
            #    ★ 对照 `read_crossseed_db()`：它自己 `raise FileNotFoundError`，
            #      所以 `--db-path` 那一路天然走 `n/a`。这里补的正是缺掉的那一问。
            if not Path(db_p).is_file():
                lines.append(f"观测对账：状态库不在（{db_p}）—— "
                             f"b−c 与无人认领**没跑成**，别念成 0")
                LOG.debug("状态库不存在，跳过 b−c / 无人认领：%s", db_p)
                db_p = None
            else:
                store = S.StateStore(db_p)
        if getattr(args, "db_path", None):
            snap = S.read_crossseed_db(args.db_path)
    except Exception as e:                  # noqa: BLE001
        LOG.debug("开库失败", exc_info=True)
        lines.append(f"观测对账：库读不到（{type(e).__name__}）—— b−c 与无人认领没跑成")

    try:
        if text is not None and store is not None:
            rv = S.resolve_found_lines(text, store)
            m.update(fb_c_all=rv.delta, fb_c_farm=rv.farm_lines)
            prod = "  ".join(
                f"{pk}: other_pack={d.get('other_pack', 0)}"
                for pk, d in sorted(rv.belong_prod.items()))
            lines.append(
                f"观测对账 b−c〔全量口径〕：{rv.b} − {rv.c} = {rv.delta}"
                f"（农场行 {rv.farm_lines}）\n"
                f"   ★ 同一批行的〔生产口径〕是另一个数：{prod or '(无包)'}\n"
                f"     —— 三包共用一个农场根，对任一包来说别的包的 searchee 天然就是"
                f" other_pack。\n"
                f"     这个数恒非零、大体恒定，**没有信息量**；要看真丢了什么，看下一行。")
    except Exception as e:                  # noqa: BLE001
        LOG.debug("b−c 对账失败", exc_info=True)
        lines.append(f"观测对账 b−c：算不出（{type(e).__name__}: {e}）")

    try:
        if store is not None and snap is not None:
            un = S.unclaimed_searchees(store, snap)
            m.update(unclaimed=len(un))
            cur_paths = sorted(p for _, p in un)
            unclaimed_baseline = cur_paths      # 无论告不告警都采纳
            prev_u = _unclaimed_norm(prev_ok.get(UNCLAIMED_BASELINE_KEY))

            listing = "\n".join(f"   {p}" for p in cur_paths[:10])
            more = f"\n   …还有 {len(cur_paths) - 10} 条" if len(cur_paths) > 10 else ""
            if not cur_paths:
                head = ("观测对账〔全场无人认领〕：**0** 条"
                        "（三包合起来认得出库里每一条 searchee）")
            else:
                head = (f"观测对账〔全场无人认领〕：**{len(cur_paths)}** 条\n"
                        f"{listing}{more}\n"
                        f"   ★ 三包合起来都不认它 —— 别的包的 searchee 会落进"
                        f"〔生产口径〕的 other_pack，\n"
                        f"     只有这个数才指得出**谁都不归**的那些。")

            if UNCLAIMED_BASELINE_KEY not in prev_ok:
                # 第一次读数：现状不是新闻 → 记基线，**不告警**。
                # ★ 这一步正是「那条 `.xlsx` 目录不再每天喊」的开关。
                lines.append(head + "\n   ★ 首次读数 → 已记为基线，**不告警**"
                                    "（现状不是新闻；往后只报变化）。")
            else:
                new = [p for p in cur_paths if p not in prev_u]
                if new:
                    new_list = "\n".join(f"   ★ {p}" for p in new[:10])
                    new_more = (f"\n   …还有 {len(new) - 10} 条"
                                if len(new) > 10 else "")
                    lines.append(head + "\n   ★ 与基线相比**有新增** —— 已告警。")
                    # ★ 通知写侧已砍掉（2026-10-05）：原先这里 `emit("alert", …,
                    #   key="unclaimed-searchee")`。读数仍进日报（lines/metrics），
                    #   只是不再投 spool 事件；下面补一行日志保证无人值守也能看见。
                    LOG.warning("观测对账〔无人认领〕：新增 %d 条 searchee 谁都不归"
                                "（现存 %d）：\n%s",
                                len(new), len(cur_paths), new_list)
                else:
                    gone = [p for p in prev_u if p not in cur_paths]
                    if gone:
                        # 缩了也是好消息 —— 但**要写出来**：否则"baseline 少了"
                        # 在日报里和"什么都没发生"长得一样，就分不清
                        # 「那个目录被清掉了（预期）」和「判据今天没读到（故障）」。
                        note = (f"   ★ 比基线**少** {len(gone)} 条 —— 不告警"
                                f"（清掉了不必喊；新基线已采纳，**再长回来会报**）：\n"
                                + "\n".join(f"   − {p}" for p in gone[:10]))
                    else:
                        note = "   ★ 与基线一致 —— 不告警（只报变化，不再每天喊）。"
                    lines.append(head + "\n" + note)
    except Exception as e:                  # noqa: BLE001
        LOG.debug("无人认领对账失败", exc_info=True)
        lines.append(f"观测对账〔无人认领〕：算不出（{type(e).__name__}: {e}）")

    # ---- 声明点〔`pack` 表〕：**登记的包里，从没被搜过一次的** ----
    # ★★ 2026-10-05（去掉「包的闸门」）**改了判据**。旧判据问的是「`pack` 表 −
    #    `--packs` 名单 = 登记了却没被驱动的包」—— 而名单（`--packs`/`PACKS_DEFAULT`）
    #    已整体退休 ⇒ 那个差集**恒为空**（§18.18「恒绿摆设」形状：规则根本不参与）。
    #    ⇒ 必须换一个**仍然会红**的问题，否则这条判据就成了摆设。
    #
    # 新判据 = 「登记的包里，**从没被搜过一次**的」。
    #   一个包"被搜过"的证据（只读 `movie` 表，逐包判，任一成立即可）：
    #     · `attempts > 0`（搜索次数记过） ∨ `coalesce(seeding_count,0) > 0`（做着种）
    #       ∨ `stage ∈ DONE_STAGES`（SEEDING/MATCHED）
    #   三者皆无 = **白登记**：状态机有它（`pack` 表有行）、农场大概也有它，
    #   但它**一次都没被搜到过**。成因与旧的"不在名单"不同（那个已不可能），
    #   可能是：登记时没建 searchee、农场没硬链接、路径全错。
    #   ★ 与旧判据**同一个病灶**（"认得出来、就是不产读数"），只是换了成因 ——
    #     而它在 unclaimed / report / trend 上**仍然全绿**，所以还得单独报。
    # ★ 这条**只报变化，不报现状**（2026-09-12 改）：现状（例如某个包登记时就没
    #    落实）是**已接受**的，每天喊一次只会把告警喊成噪音 —— 而噪音的代价是
    #    **真的出问题时没人看**。
    #      · 差集里出现了**基线里没有**的 → 发 alert（有新问题）
    #      · 与基线一致、或**缩回**基线之内 → **不发**（修好了不必喊）
    #    无论发不发，都把它采纳为新基线 —— 所以同一个差集只喊一次。
    #    ★ 缩也采纳：不然「删掉打的那一行」之后基线还是旧的，将来它再冒出来时
    #      「又冒出来了」这件事就没人报（那才是真要抓的）。
    # ★ 基线**不进代码、进状态文件**（`.reconcile.state` 的 `_packs_baseline`）：
    #    写进代码就分不清「回到基线」和「判据死了」（§18.19 的教训）。
    # ★ 但**日报正文照旧每天打印**完整差集 —— 不告警 ≠ 看不见。
    # ★ 库读不到必须给 `n/a` 而不是 0：TSV 里两者长得一样就没法事后分开
    #   （同上面 `fa`/`unclaimed` 的规矩）。
    try:
        if store is None:
            lines.append("声明点对账〔pack 表〕：状态库读不到 —— "
                         "「从没被搜过」**没跑成**，别念成 0")
        else:
            reg = [r["name"] for r in store.packs()]
            undriven = [name for name in reg if _pack_never_searched(store, name)]
            m.update(packs_undriven=len(undriven))
            cur = {"undriven": sorted(undriven)}
            packs_baseline = cur          # 无论告不告警都采纳
            prev_b = _packs_norm(prev_ok.get(PACKS_BASELINE_KEY))

            if PACKS_BASELINE_KEY not in prev_ok:
                # 第一次读数：现状不是新闻 → 记基线，**不告警**。
                note = ("   ★ 首次读数 → 已记为基线，**不告警**"
                        "（现状不是新闻；往后只报变化）。")
            else:
                new = [x for x in cur["undriven"] if x not in prev_b["undriven"]]
                # ★ 缩了也是好消息 —— 但**要写出来**（与「无人认领」那条对齐）：
                #   否则「基线少了」在日报里和「什么都没发生」长得一样，就分不清
                #   「某包被搜过了（预期）」和「判据今天没读到（故障）」。
                lost = [x for x in prev_b["undriven"] if x not in cur["undriven"]]
                if new:
                    note = "   ★ 与基线相比**有变化** —— 已告警。"
                    # ★ 通知写侧已砍掉（2026-10-05）：原先这里 `emit("alert", …,
                    #   key="packs-mismatch")`。读数仍进日报（lines/metrics）。
                    LOG.warning("声明点对账：**从没被搜过**的包变多了（+%d）：新增 %s；"
                                "全部从没被搜过的包：%s",
                                len(new), "、".join(new),
                                "、".join(undriven) or "(无)")
                elif lost:
                    note = ("   ★ 比基线**少** —— 不告警（修好了不必喊；"
                            "新基线已采纳，**再长回来会报**）：\n"
                            + "\n".join("     − 从没被搜过：" + x for x in lost))
                else:
                    note = "   ★ 与基线一致 —— 不告警（只报变化，不再每天喊）。"
            lines.append(
                f"声明点对账〔pack 表〕：登记 {len(reg)} 个 / "
                f"其中从没被搜过 {len(undriven)} 个\n"
                f"   从没被搜过（{len(undriven)}）："
                f"{'、'.join(undriven) or '(无)'}\n"
                f"   ★ 判据 = 该包**一条 `attempts>0` / `seeding_count>0` / "
                f"`stage∈DONE` 都没有**；\n"
                f"     它在 unclaimed / report / trend 上全是绿的，所以单列一条。\n"
                f"{note}")
    except Exception as e:                  # noqa: BLE001
        LOG.debug("pack 表对账失败", exc_info=True)
        lines.append(f"声明点对账〔pack 表〕：算不出（{type(e).__name__}: {e}）")

    try:
        if store is not None:
            store.con.close()
    except Exception:                       # noqa: BLE001
        pass

    # ---- #34：`n/a` 要配「上次成功读数时刻」 ----
    # ★ 为什么非要有这一行：`n/a` 本身**只说明这一次**。TSV 里一个已经连续
    #   三天读不到的格子，和一个昨天还好、今天抖了一下的格子，长得**一模一样** ——
    #   于是"判据长期失效"和"偶发抖动"在事后完全分不开。
    #   判据要能指回时间，才谈得上"指回判据之外的真实记录"。
    # ★ 只记**真的读到数**的那些格：`n/a` 不覆盖旧时间戳（否则一次失败就把
    #   "上次成功"抹成"从没成功过"，反而丢信息）。
    now_ts = time.time()
    live = dict(prev_ok)
    for k, v in m.items():
        if v != "n/a":
            live[k] = now_ts
    na = sorted(k for k, v in m.items() if v == "n/a")
    if na:
        marks = []
        for k in na:
            t = prev_ok.get(k)
            # ★ 「上次成功 X 前」和「从未成功读到过」是**两种处置**：
            #   前者去查这一次为什么抖（SMB/库占用），后者去查这个格子
            #   从装上那天起是不是根本没接上（输入源配错了）。所以两句都要写全。
            marks.append(f"{k}（上次成功 {_age_h(now_ts - t)}）" if t
                         else f"{k}（★ 从未成功读到过）")
        lines.append("观测对账〔本轮 n/a 的格子〕：" + "、".join(marks)
                     + "\n   ★ 括号里是**这个格子最后一次真的读到数**是什么时候 ——"
                       "刚抖一下 vs 长期读不到，差别全在这里。")
    if packs_baseline is not None:
        # ★ 只在**本轮真的算了差集**时才写。`--packs` 没给、或库读不到的那几轮，
        #   必须保留旧基线 —— 否则一次抖动就把「已接受的现状」抹成空，
        #   下一轮会把上一轮那批差集（当时的 `mbf`）当成「新变化」再喊一遍
        #   （正是要避免的噪音）。
        live[PACKS_BASELINE_KEY] = packs_baseline
    if unclaimed_baseline is not None:
        # 同上：库读不到的那几轮保留旧基线，别把已接受的那几条当成新增再喊。
        live[UNCLAIMED_BASELINE_KEY] = unclaimed_baseline
    _reconcile_write(live)
    return "\n".join(lines), m


def pack_progress_watch(st) -> tuple[str, dict]:
    """各包「做种 / 总数 + **② 口径完成度**」→ (给日报正文的一段, 给 metrics 的字典)。

    ★★ **必须在这个 `with S.StateStore(args.db) as st:` 里被调用**（`#96`）——
      本函数**不自己开库**。理由不是风格：`report_daily` 里读库那段是
      `try/with`（差的时候只往正文追一句「算不出」），而 `after_batch_reports`
      的**外层** `except` 是 `LOG.debug(...)` ⇒ 一旦抛到那一层，
      **整份日报连正文都没了**。所以新节必须落在**同一个** `try` 里。

    ★ 读不到 ⇒ `n/a`，**不许给 0**（`n/a ≠ 0 ≠ 没事`，`ERR-AI-03`）。

    ★ 每包带它**自己**的 `scanned_at`（`pack.scan_finished_at`）—— 日报挂在
      「当天第一批」上，`stage` 却是**逐包**更新的，所以当天还没跑过的包，
      那两数停在上次 sync。一个全局「截至 HH:MM」说不清这件事，
      每包自己的时刻才说得清（`#94`）。

    ★★ **② 口径的定义不在这里** —— 真源在 `orchestrator/state.py` 的
      `CENSUS_DENOM_STAGES` 上方（`A.11`：同一事实只在一处）。本函数只负责
      **措辞**。两件必须照抄的东西：`denom == 0` 是 **`n/a` 不是 `0%`**；
      百分比**非单调**（会因加站而回落），所以正文里有那句预先堵漏。

    ★★ **总计 = 各包分子之和 ÷ 各包分母之和**，**不是**各包百分比的平均。
      实测这两者能差 **32 个百分点**（`mbf` 的 4 行会与 `frds` 的 486 行同权）。
      行尾那句「各包之和，非平均值」就是防这个读法，**别删**。

    ★★ **绝不许写**「下降 / 退化 / 变差 / 做种率」这类**替读者下的结论**
      （`B.10` 第 14 条第三个方向）：日报是**发出去的**，读者没有上下文去判断
      这句话有没有根据。正文只许说**读数撑得起的话**。
    """
    rows = S.pack_progress(st)
    if not rows:
        # 空 ⇒ 真的一个包都没登记。**这与「读不到」不同**，所以不是 n/a。
        return "各包进度：（库里一个包都没登记）", {}

    lines = ["各包进度（做种 / 总数，② 口径完成度 = 做种 ÷（做种+待搜+跳过+错误+已匹配），"
             "截至该包上次扫描）："]
    m: dict = {}
    # 总计用**分子之和 / 分母之和**（见 docstring 里那 32 个百分点的实测）。
    sum_num = 0
    sum_den = 0
    for r in rows:
        name = r["name"]
        fin = r["scanned_at"] or "从未"
        cen = r["census"]
        sum_num += cen["numerator"]
        sum_den += cen["denom"]
        pct = r["pct"]
        if pct == "n/a":
            # ★ 分母为 0 ⇒ 只写 n/a，**不写 %** —— 写成 "n/a%" 是把两件事缝在一起。
            lines.append(f"  {name}：{r['seeding']} / {r['total']}（n/a）  {fin}")
        else:
            # 既印百分比、又印**未约简的分数**：百分比四舍五入过，
            # 而下面那句「可能回落」要靠读者看得见两个原数才判得了。
            lines.append(f"  {name}：{r['seeding']} / {r['total']}"
                         f"（{pct}%，{cen['numerator']}/{cen['denom']}）  {fin}")
        # ★ 3N 个 kv，进 **metrics 这一个字段**（不是 TSV 的列，见 `#93`）。
        #   ★ key 里的空格由渲染侧规范化（原 `#95` 是 notify 的规矩；写侧已砍掉，
        #     这里仍不预加工 —— 一处负责）。
        #   ★★ 前缀选 `packpct:` 而**不是** `pct:`，是为了让任何键都不可能
        #      拼出 `pack=`（摘要数批次靠它**整键相等**）。★ `packnum:` /
        #      `packden:` 字面含子串 `pack` 但**安全** —— 只因那条 awk 按 `=`
        #      切开后比**整段**。这条是**险过**，测试里专门钉了它。
        m[f"packpct:{name}"] = pct
        m[f"packnum:{name}"] = cen["numerator"]
        m[f"packden:{name}"] = cen["denom"]
        # ★★★ `packwhy:` —— **分母为 0 时说明"为什么"**（2026-09-24 `§26.67`）。
        #   ★ 起因：`包 mbf  n/a (n/a)  做种 0 / 总 4` 这一行**被用户报上来三次**
        #     （2026-09-21 改渲染、`§26.29` 记注释、09-24 又在信里问"怎么是这样"）。
        #     ★ 前两次都在治**渲染**，而**读数旁边始终没有解释** ⇒
        #       并排的 `n/a (n/a)` + `总 4` **仍然第一眼像矛盾**（看不出"还没搜过"）。
        #   ⇒ 真因是**设计在正常工作**（分母刻意排除 `UNMATCHED`：
        #     完成度量的是「**已搜过、还没做成种**」那个集合，
        #     没搜过的片子进了分母会得出"0% 完成"，而事实是"**还没开始**"）。
        #   ★ 所以这里把**原因**也发射出去，让摘要能印在那一行**旁边** ——
        #     一句话，但**省掉第三次同样的问**。
        #   ★ 只在 `n/a` 时发射（正常包不写这个键 ⇒ 不占屏、不误导）。
        #   ★ 值**不许含空格** —— 原先这条约束是给 `notify-spool.sh` 的（它按空格
        #     切成 `键=值`）。通知写侧已砍掉（2026-10-05），但**保留这条约束**：
        #     metrics 是对外稳定口径，值里塞空格在任何渲染侧都是坑。
        #     所以这里发**下划线连写的标记**，由渲染侧翻成人话。
        if pct == "n/a":
            m[f"packwhy:{name}"] = "den0_no_searched_yet"
        # ★ 原有的两个键**保留**：日报渲染侧会把整段 metrics 原样打进批次明细
        #   ⇒ 摘要里并排出现 seeding/total/packpct/packnum/packden，读者能**自己验算**。
        #   没有任何程序解析它们（已 grep 证实）。
        m[f"seeding:{name}"] = r["seeding"]
        m[f"total:{name}"] = r["total"]

    if sum_den > 0:
        lines.append(f"总计：{sum_num} / {sum_den}"
                     f"（{round(100 * sum_num / sum_den)}%，{sum_num}/{sum_den}）"
                     f"  ← 各包之和，非平均值")
    else:
        lines.append("总计：n/a  ← 各包分母皆为 0")

    # ★★ 两个池（`e404b1ca#6` ②，用户 2026-09-20 拍）：**欠账 vs 常态**分开报。
    #   ★ 为什么要分开：13 行上方那个「待搜」是个**合计数**，两种性质混在一起 ——
    #     欠账（`SKIPPED`/`ERROR`/`PENDING`）是**有限**的、清一部少一部；
    #     常态（`UNMATCHED`）是**稳态**、只要片子在就会一直在。
    #     混着报会**同时误导两头**：以为欠了 565 部的债 / 以为清完就没事了。
    #   ★ 口径：只数 `DEBT_STAGES` ∪ `STEADY_STAGES`（`DONE_STAGES` 不进任何池）。
    #   ★ `n/a` 与 `0` 分开：库里一个包都没登记时**不写 0**（`ERR-AI-03`）。
    pool_debt = sum(r["census"].get("pool_debt", 0) for r in rows)
    pool_steady = sum(r["census"].get("pool_steady", 0) for r in rows)
    lines.append(
        f"待搜分池：欠账 {pool_debt}（SKIPPED/ERROR/PENDING，**有限**、要清零）"
        f" / 常态 {pool_steady}（UNMATCHED，**稳态**、按周期走）  ← 各包之和")
    m["pool_debt"] = pool_debt
    m["pool_steady"] = pool_steady

    # ★ 那句「可能回落」：说的是**机理**不是判定（它不说"这没事"，它说**什么在动**），
    #   只预先堵**一个**误读，且是括号里的一句 —— 不会被读成一条状态行。
    lines.append("（② 口径分母 = 做种+待搜+跳过+错误+已匹配，**不含未匹配** ⇒ 新站接入或\n"
                 "  片子转入未匹配时，分母会缩、百分比**可能回落**，回落不代表做种丢了。）")

    m["pct"] = round(100 * sum_num / sum_den) if sum_den > 0 else "n/a"
    m["pct_num"] = sum_num
    m["pct_den"] = sum_den
    return "\n".join(lines), m


def report_daily(args, *, force: bool = False, farm_note: str = "") -> bool:
    """每天最多投一次的台账：额度（来源 A+C）+ 新增做种趋势 + IYUU 辅种条数
    + qB 卡 999（停滞的未完成种子）+ **装不出来 / 没 peer**（未完成且停滞的单种，
    只识别 + 通知、不碰 qB）+ 观测对账（a−b / b−c〔全量口径〕/
    全场无人认领 / 声明点〔--packs〕）。

    ★ 为什么必须自己记「今天发过没有」（**通知写侧已砍掉，此段留作历史**）：
      原先 notify 的**冷却只对 alert 生效**（`batch`/`info` 走 `_cooled` 不查），
      也就是同一个 batch 事件**每一批都会写一个新文件进 spool** —— 45 分钟一批
      就是一天 30 多条。日报不能靠 notify 去重，得自己记。现在没有 spool 了，
      但"每天恰好一次"这个**幂等约束**仍旧由 `_daily_set` 守着。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    if not force and _daily_last() == today:
        return False

    parts: list[str] = []
    if getattr(args, "db_path", None):
        try:
            lines = S.quota_snapshot(args.db_path)
            pl_url, pl_key = prowlarr_cfg(args)
            if pl_url and pl_key:
                S.attach_prowlarr_quota(lines, pl_url, pl_key, timeout=8)
                parts.append(S.render_quota(lines, limit=8))
            else:
                parts.append(S.render_quota(lines, limit=8))
                miss = "PROWLARR_URL" if not pl_url else "PROWLARR_API_KEY"
                parts.append(f"（来源 B 互校：没配 {miss}，跳过 —— 见 SUMMARY §16.1.4）")
        except Exception as e:              # noqa: BLE001 —— 一节坏了不该拖垮整份日报
            parts.append(f"站点额度台账：读不到（{type(e).__name__}: {e}）")
    else:
        parts.append("站点额度台账：跳过（没有 --db-path / 找不到 cross-seed.db）")

    try:
        with S.StateStore(args.db) as st:
            parts.append(st.trend(weeks=4).render())
            # ★★ 各包进度**必须在这个 try/with 里**（`#96`）：放外面会抛到
            #    `after_batch_reports` 的外层 `except: LOG.debug` ⇒ 整份日报消失。
            pp_note, pp_metrics = pack_progress_watch(st)
            parts.append(pp_note)
    except Exception as e:                  # noqa: BLE001
        parts.append(f"新增做种趋势：算不出（{type(e).__name__}: {e}）")
        parts.append(f"各包进度：算不出（{type(e).__name__}: {e}）")
        # ★ 读不到 ⇒ `n/a`，**不许**给 0（`ERR-AI-03`：`n/a` / 0 / 没事 是三件事）
        pp_metrics = {"pp": "n/a"}

    # 农场巡检那一行摘要（§16.2.2 任务 6）。★ 它**不是**可有可无的装饰：
    #   巡检每天才跑一次，而 `batch` 级通知是**每批都真的投一条**（见本函数开头的说明），
    #   所以「巡检还活着」这个心跳只能由日报带出来 —— 否则巡检哪天静默不跑了，
    #   从外面看与「一直没漂移」完全一样。这正是 §16.2 要防的那种坏法。
    if farm_note:
        parts.append(farm_note)

    iyuu_note, iyuu_metrics = iyuu_watch(args)
    parts.append(iyuu_note)

    # 观测对账（a−b / b−c〔全量口径〕/ 全场无人认领 / 声明点〔pack 表〕）。
    # ★ 放在**日报里**而不是每批：日报是这套系统里唯一"每天恰好一次"的观测出口 ——
    #   进日报的 metrics，这些数才真的留得下来。（"notify 只记 metrics"那半句已随
    #   通知写侧砍掉而作废，结论不变。）同 iyuu_watch。
    rec_note, rec_metrics = reconcile_watch(args)
    if rec_note:
        parts.append(rec_note)

    # qB「卡 999」（停滞的未完成种子；见本节函数上方的说明）。
    # ★ 与 `reconcile_watch` **并列**调用，**不能挪进它内部**：那个函数对
    #   `.reconcile.state` 是「开头读一份快照、末尾整文件覆盖写回」，夹在它的
    #   读与写之间的写入会被连同快照一起抹掉（详见 `qb_999_watch` 里的说明）。
    qb_note, qb_metrics = qb_999_watch(args)
    parts.append(qb_note)

    # qB「卡种」：匹配到了却下不来（`stalledDL`/`metaDL`/`checkingDL`/`error`）。
    # ★ 与上面那条**并列**、同样受 `reconcile_watch` 那条「读→改→写必须紧挨着」
    #   的约束（它也用 `.reconcile.state`，只是**另一个键**）—— 详见
    #   `qb_999_watch` 里的说明。**别挪进 `reconcile_watch` 内部**。
    # ★★ 它是 `qb_999_watch` 的**互补**观测，不是重复：那条只看得见
    #   `progress ≥ 0.99` 且停滞 >24h 的；用户场景里"做不下"的大多**没到 99%**
    #   ⇒ 那些**只有这条**看得见（`e404b1ca#7`）。
    st_note, st_metrics = qb_stalled_watch(args)
    parts.append(st_note)

    # 「装不出来」/「没 peer」（见本节函数上方的说明；★ 只识别 + 通知，**不碰 qB**）。
    # ★ 用自己的状态文件 `.reseed-freeze.state`，所以**不**受 `reconcile_watch`
    #   那条「读→改→写」的约束 —— 但**仍然摆在它外面**：缘由与 `qb_999_watch`
    #   一样，两条 watch 的顺序与位置都别去动（动了只有到次日才发现，见那里的说明）。
    fz_note, fz_metrics = reseed_freeze_watch(args)
    parts.append(fz_note)

    # 链接守护：既有那 628 条硬链接有没有被就地改写（=写穿）。
    # ★ 用**自己的状态文件** `.linkguard.state`，与 `reconcile_watch` 那条
    #   「读→改→写」的约束无关（那个约束只对 `.reconcile.state` 成立）。
    lg_note, lg_metrics = linkguard_watch(args)
    parts.append(lg_note)

    body = "\n\n".join(parts)
    # ★ 通知写侧已砍掉（2026-10-05）：原先这里 `emit("batch", "每日台账", …,
    #   key="daily")` 投进 spool 走每日摘要邮件。现在只有：
    #   ① 日志（下面）；② `_daily_set(today)` 记"今天已报过"（与通知无关，是幂等开关）。
    LOG.info("每日台账（额度 + 趋势，metrics=%s）：\n%s",
             {"day": today, **iyuu_metrics, **rec_metrics, **qb_metrics,
              **st_metrics, **fz_metrics, **lg_metrics, **pp_metrics}, body)
    _daily_set(today)
    return True


# --------------------------------------------------------------------------- #
# 农场巡检：把 `build-farm.sh --verify` 挂上来（SUMMARY §16.2.2 —— 任务 6）
# --------------------------------------------------------------------------- #
# 要解决的问题：农场（v3 硬链接）与它的那 49 条源之间会**慢慢漂移** ——
# 源里新增了片子、或者片子在源里被删/改名。漂移了不报错，只会安静地变坏：
#   源有农场无 → cross-seed 看不见这部，白等一轮；
#   农场有源无 → 农场那条还在，cross-seed **照样拿去搜 = 白烧站点额度**。
# 原先靠人工偶尔跑一次 `--verify`，所以漂移能存在很久没人知道。
#
# 四条约束（照 §16.2.2 抄；有改动的地方在注释里说明）：
#   1. `--verify` 有漂移 → **退出码 1**。这是 2026-09-12 09:52 才补上的：
#      在那之前它只打印计数、恒返回 0 —— 挂上去也**永远不会报警**，
#      那才是真正的前置条件（不是"没时间挂"）。
#   2. 有漂移 → `alert`（**即时**）。这是要人去处理的事，不能埋进日报里。
#   3. 零漂移 → **不出告警**，只留一行摘要由每日台账带出去，兼作"巡检还活着"的心跳。
#   4. ★★ **绝不自动 `--prune`**：prune 判「源没了」的依据正是那份期望集，
#      拿它自动删 = 把一次误判放大成**不可逆**的数据丢失。**只报告，删不删由人定。**
#      —— 这条有测试盯着（断言 argv 里永远不出现 --prune）。
#
# ★ 一处比 §16.2.2 更严的地方：**脚本本身跑不起来也算故障**，照样 alert。
#   退出码既不是 0（无漂移）也不是 1（有漂移），说明"根本没查成"。不这么判的话，
#   "巡检静默地没在跑"和"真的没漂移"从外面看一模一样 —— 那正是 §16.2 开头说的
#   「分不清'真的没漂移'和'还是没在检查'」，比不做校验更坏，因为它给的是**虚假的安心**。
#   ★ 2026-10-06 细化：其中 **退出码 2 单独点名 = 参数解析错**（`build-farm.sh` L123
#     `未知参数 … exit 2`）—— 它的排查动作与"农场/脚本坏了"**不同**（查命令，不是查农场）。
FARM_CHECK_FILE = STATE_DIR / ".farm-check.state"
FARM_CHECK_MIN_GAP_SEC = 20 * 3600      # 约一天一次（用时间戳差值，绕开"跨天/时区"边界）
# 超时按**实测**给余量，不是拍脑袋：2026-09-12 在 NAS 上量到 `--verify` **4 秒**
# （475 条，本地 stat + 列 49 个目录；全是本机 I/O，没有网络往返）。
# 120 秒 = 30 倍余量 —— 日常远够，真卡死时又能及时收手去发告警。
# ★ 别为了"保险"把它调得很大：这个值同时也是**判据** —— 超时会被当成
#   「巡检跑不起来」而发 alert。调太大，等于把一次真故障从告警变成静默等待。
FARM_CHECK_TIMEOUT_SEC = 120


def _farm_check_read() -> dict:
    try:
        d = json.loads(FARM_CHECK_FILE.read_text(encoding="utf-8"))
    except Exception:                       # 文件不存在 / 坏了 —— 都当"还没查过"
        return {}
    return d if isinstance(d, dict) else {}


def _farm_check_write(note: str, *, now: float) -> None:
    try:
        tmp = FARM_CHECK_FILE.with_suffix(".state.tmp")
        tmp.write_text(json.dumps({"ts": now, "note": note}), encoding="utf-8")
        os.replace(tmp, FARM_CHECK_FILE)    # 原子替换，别让下一轮读到半个文件
    except OSError:
        LOG.debug("农场巡检标记写不进去（不影响跑批）", exc_info=True)


def _run_verify(argv: list[str]) -> tuple[int, str]:
    """真跑一次 `build-farm.sh --verify`。返回 (退出码, 输出)。

    ★ 超时兜底是**必须**的：这一步挂在批次收尾里，而收尾后面还有个 finally
      要落状态（跑了哪个包、下批间隔）。子进程真卡住时，宁可报一条"超时"，
      也不能让状态写不回去 —— 写不回去会让下一轮的闸门算错。
    ★ 用 PIPE 收 stdout+stderr：漂移明细只出现在 stdout，而 die() 走 stderr，
      两条都得看得到（合并成一条流，顺序也保住了）。
    """
    try:
        p = subprocess.run(
            argv, cwd=str(Path(argv[1]).parent),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=FARM_CHECK_TIMEOUT_SEC,
            # ★ 显式传 COMPOSE_DIR：脚本里的默认值是写死的 NAS 绝对路径，
            #   这里用脚本自己所在的目录覆盖掉它 —— 以后装到别处也不用改脚本。
            # ★★ 2026-10-05（B6 实测修正）：再传 **FARM_MANIFEST_DIR**。
            #   缘由：drive-loop 容器把 compose 根挂成 **`:ro`** ⇒ build-farm.sh 往
            #   `data/hlink/` 写清单会 `Read-only file system` ⇒ 农场巡检退 2。
            #   容器里**唯一可写**的是 `/state`（= STATE_DIR）⇒ 清单落到 `$STATE_DIR/hlink`。
            #   ★ 本机开发机跑时 STATE_DIR = HERE（可写），行为不变。
            env={**os.environ,
                 "COMPOSE_DIR": str(Path(argv[1]).parent),
                 "FARM_MANIFEST_DIR": str(STATE_DIR / "hlink")},
        )
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, f"build-farm.sh --verify 超时（> {FARM_CHECK_TIMEOUT_SEC} 秒）"
    except OSError as e:                    # sh 不在 / 脚本没执行权限 / 路径坏了
        return 127, f"起不了 build-farm.sh：{type(e).__name__}: {e}"


def check_farm(*, force: bool = False, now: float | None = None,
               runner=None) -> str:
    """跑一次农场巡检，返回一行摘要（由每日台账引用）。返回 "" = 库里没记录。

    间隔没到就直接返回**缓存的那一行**（默认 20 小时一次）—— 这样日报里
    永远有一句农场的话，而巡检本身不用每批都跑。

    ★ `now` / `runner` 只为**可测**而存在（生产调用一律省略）。
      —— 与 `alert_blocked_indexers(now=...)` 同一个理由：测试要能钉死时间、
      要能打桩子进程。否则这段代码只能靠"真跑一次 NAS"来验证。
    """
    now = time.time() if now is None else now
    cached = _farm_check_read()
    if not force and cached.get("ts") and \
            now - float(cached["ts"]) < FARM_CHECK_MIN_GAP_SEC:
        return str(cached.get("note") or "")

    script = first_existing("build-farm.sh")
    if not script:
        note = "农场巡检：跳过（compose 目录里找不到 build-farm.sh）"
        LOG.warning("%s", note)
        _farm_check_write(note, now=now)
        return note

    argv = ["sh", script, "--verify"]       # ★ 只有 --verify；永远不加 --prune
    rc, out = (runner or _run_verify)(argv)
    out = (out or "").strip()

    if rc == 0:
        note = "农场巡检：无漂移"
        LOG.info("%s", note)
    else:
        tail = "\n".join(out.splitlines()[-40:]) if out else "(无输出)"
        if rc == 1:
            note = "农场巡检：**有漂移**（处理与否由你定）"
            head = ("农场（硬链接）与它的源对不上了。**只报告、不动手** —— "
                    "脚本从不自动删，`--prune` 跑不跑由你定。\n"
                    "· 补新建项：`sh build-farm.sh --apply`\n"
                    "· 删孤儿项：**确认源真的没了**再 `sh build-farm.sh --apply --prune`\n"
                    "★ 判「源没了」的依据就是同一份期望集，误判一次即不可逆，"
                    "所以这一步**故意**留给人。\n")
        else:
            note = f"农场巡检：**跑不起来**（退出码 {rc}）"
            # ★ 2026-10-06：退出码语义按 `build-farm.sh` **实测**（L44/L123）——
            #   0=无漂移、1=有漂移、**2=参数解析错**（`未知参数 … exit 2`）。
            #   早先把「2」笼统写成"根本没查成"是**指向错的排查动作**：
            #   码 2 的真问题**几乎总是命令传错**（argv 写错），不是农场/脚本坏了。
            if rc == 2:
                head = ("这**不是**「没有漂移」，也不是「农场坏了」 —— 退出码 2 = "
                        "**参数解析错**（`build-farm.sh` 收到不认识的参数就 `exit 2`）。"
                        "多半是**调用它的命令写错了**（argv），先核命令，别去动农场。\n")
            else:
                head = ("这**不是**「没有漂移」，而是「根本没查成」 —— 退出码既不是 "
                        "0（无漂移）也不是 1（有漂移）也不是 2（参数错）。修好之前，"
                        "农场有没有漂移**无人知道**，别把它当成「一切正常」。\n")
        # ★ 通知写侧已砍掉（2026-10-05）：原先这里 `emit("alert", title, …, key=…)`。
        #   漂移/跑不起来的结论保留在**上面的日志**（LOG.warning）与日报 note 里。
        LOG.warning("%s\n---- 输出末尾 ----\n%s", head.strip(), tail)

    _farm_check_write(note, now=now)
    return note


def after_batch_reports(args) -> None:
    """跑批收尾的通知 + 农场巡检。

    ★ 外面再包一层 try —— 这些全是**附属功能**，任何一件出事都不该让整批算失败。
      （emit 自己已经吞异常了，但 `report_daily` 里还有读库/算趋势这些代码，
      `check_farm` 还要起子进程。）
    ★ 顺序：巡检**在日报之前**跑，好把它那一行摘要塞进本次日报里。
    """
    try:
        alert_blocked_indexers(args)
        farm_note = check_farm()
        report_daily(args, farm_note=farm_note)
    except Exception:                       # noqa: BLE001
        LOG.debug("收尾通知失败（不影响跑批）", exc_info=True)


def run_round(pack: str, args, api_key: str) -> S.DriveStats | None:
    """跑一批 drive + 回灌（池路径：跨**全部登记包**取全局前 N）。返回 DriveStats；
    无待搜 / 出错返回 None。

    ★ 回灌必须带 qB（qbit_torrents）—— 否则 stage 推导拿不到"是否在做种"，
      原本 SEEDING 的片子会被误降级成 MATCHED（2026-09-11 踩过）。

    ★★ 2026-10-05（去掉「包的闸门」）：**只剩池路径**。`pack` 不再"轮到哪个包"，
      而只当**批次标签**用（可传 `"pool"` 或某包名做日志前缀）；实际取件是
      `st.todo_pooled(require_packs(st), …)` —— 扫 **`pack` 表全部登记的包**、
      `--limit` 是**全局**的。原先那条"一个包一批"（`todo_detail(pack)`）与非池
      `apply_batch` 分支已删。

    ★★ 记账仍**按包分段**（用户 2026-09-20 拍）：每个出现在本批里的包各发一条
      `batch` 事件、各带**自己的** `pack=` 与读数 ⇒ 日报「按包聚合」口径不变。
    """
    st = S.StateStore(args.db)
    # ★★ 本批的**时间窗起点**（2026-09-23）：给 `first_seeding_between()` 用。
    #   为什么取在这里：在这一刻之后写的 `attempt` 行都算"本批干的活"。
    #   ★ 格式必须是 19 字符 ISO（`%Y-%m-%d %H:%M:%S`）—— 与 `attempt.at` 同形，
    #     定长 ⇒ 字典序即时间序（见 `state.py::first_seeding_between` 的注释）。
    #   ★ 这里**自己生成**、不去 import `state._now`：那个私有名是 state 模块
    #     的内部约定，跨模块引用会把它变成事实上的公开 API。
    batch_t0 = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        idx = [i.strip() for i in (args.indexers or "").split(",") if i.strip()] or None
        # ★ 池源 = `pack` 表全部登记的包；空表 ⇒ 抛 NoPacksRegistered（不静默算空）。
        pool = require_packs(st)
        pooled = st.todo_pooled(
            pool, indexers_now=idx,
            include_cooldown=args.include_cooldown,
            cadence_days=args.cadence_days,
            cadence_by_indexer=S.parse_cadence(args.cadence),
            limit=args.limit)
        pairs = [(r, due) for _, r, due in pooled]
        batch_packs = sorted({pk for pk, _, _ in pooled})
        seeding_before = {pk: sum(1 for r in st.movies(pk)
                                  if r["stage"] == S.STAGE_SEEDING)
                          for pk in batch_packs}
        plan = (f"池：{len(pool)} 个包共 {len(pairs)} 部待搜，"
                f"本批取全局前 {len(pairs)}（--limit {args.limit}）"
                f"；本批涉及包：{', '.join(batch_packs) or '（无）'}")
    finally:
        st.con.close()

    if pairs is None:
        LOG.warning("[%s] %s", pack, plan)
        return None
    paths = [r["path"] for r, _ in pairs]
    if not paths:
        LOG.info("[%s] 没有待搜索项。", pack)
        return None
    LOG.info("[pool] %s", plan)

    # ★ 必须把 on_event 接到 LOG 上，否则整批**全程零输出**（2026-09-12 凌晨踩过，
    #   当时对着空日志怀疑批次被杀了，白查一轮）。
    #   DriveSession 自己**不写任何日志**，进度全靠这个回调；不传时它默认是
    #   `lambda *a, **k: None`（见 orchestrator/state.py:1517）。于是最长的那一段
    #   （50 部 × --interval 30s ≈ 25 分钟起，撞上站点退避还可能再等 max_wait）
    #   在 drive-loop.log 里一个字都没有 —— 从日志上**无法区分**
    #   「在正常推进」和「卡死在第 3 部」。无人值守的系统在最长的阶段没有可观测性，
    #   这是实打实的缺陷，不是"日志打得少"而已。
    #   对照：reseed-state.py 的 drive 子命令一直有接（reseed-state.py:452），
    #   所以手工跑的时候看得到进度 —— 差别只在于这里漏传了参数。
    last = [0.0]

    def on_event(kind, *rest):
        now = time.time()
        if kind == "sent":
            pth, code, i, total = rest
            tag = "OK" if code in (200, 202, 204) else "!!"
            gap = f" (+{now - last[0]:.0f}s)" if last[0] else ""
            last[0] = now
            LOG.info("  [%d/%d] %s %s%s  %s", i, total, tag, code, gap,
                     Path(pth).name[:56])
        elif kind == "wait":
            LOG.info("  ⏸  %s", rest[0])
        elif kind == "warn":
            LOG.warning("  !  %s", rest[0])
        elif kind == "abort":
            LOG.error("  ✗  %s", rest[0])
        elif kind == "drained":
            LOG.info("  ·  %s", rest[0])

    sess = S.DriveSession(
        url=args.url,
        api_key=api_key,
        crossseed_db=args.db_path,
        interval=args.interval,
        check_every=args.check_every,
        check_secs=args.backoff_check_secs,
        max_wait=args.max_wait,
        timeout=args.timeout,
        pause_on_backoff=not args.no_pause_on_backoff,
        # ★ 把 `--indexers` 交给会话：它靠这份名单区分「**所有**站都在退避」（该等/该中止）
        #   与「只禁了一部分、还有健康站」（不等、照发）。不传 = 退化回旧行为。
        indexers=idx,
        on_event=on_event,
    )
    stats = sess.run(paths)
    LOG.info("[%s] 发送完毕：成功 %s / 失败 %s，退避等待 %.1f 分钟（%s 次）",
             pack, stats.ok, stats.failed, stats.waited_sec / 60, stats.backoff_hits)

    # ---- 回灌（带 qB，否则 SEEDING 会被误降级）----
    if args.log:
        S.wait_for_log_quiet(args.log[0], quiet_sec=args.settle,
                             max_wait=args.drain_max_wait)
    # qB 挂了不该让整批死 —— 少一个数据源而已（代价：推不出 SEEDING，会被降级）
    qb: list[dict] = []
    if args.qbit_url:
        try:
            qb = S.qbit_torrents(args.qbit_url, args.category)
        except Exception as e:  # noqa: BLE001
            LOG.warning("取 qB 列表失败（忽略）: %s", e)
    st = S.StateStore(args.db)
    # ★★ 记账口径：**按包分段**（用户 2026-09-20 拍；2026-10-05 后是唯一口径）。
    #   ★ `sync_pack` **按包调**（它本来就是"重算这个包的行"），所以"分段"不是
    #     额外发明 —— 是把原本那一次调用变成**每个涉及的包各一次**。
    #     日报的「按包聚合」正是靠 `batch` 事件的 `pack=` 键，于是口径**一字不改**。
    seg_packs = batch_packs
    segs: list[dict] = []
    try:
        for pk in seg_packs:
            rep = S.sync_pack(
                st, pk,
                crossseed_db=args.db_path,
                log_paths=args.log or [],
                qbit_torrents=qb,
                indexers_override=[i.strip() for i in (args.indexers or "").split(",")
                                   if i.strip()] or None,
                indexer_alias=S.parse_alias(args.indexer_alias),
                cadence_days=args.cadence_days,
                cadence_by_indexer=S.parse_cadence(args.cadence),
            )
            before = (seeding_before.get(pk, 0) if isinstance(seeding_before, dict)
                      else seeding_before)
            segs.append({
                "pack": pk,
                "rep": rep,
                "still_skipped": sum(1 for r in st.movies(pk)
                                     if r["stage"] == S.STAGE_SKIPPED),
                "newly_seeding": (sum(1 for r in st.movies(pk)
                                      if r["stage"] == S.STAGE_SEEDING) - before),
                # ★★ `first_seeding` = 本批**首次**转 SEEDING 的片数（**真增量**）。
                #   ★ 与 `newly_seeding` 的区别（别把它们当同一个数）：
                #     `newly_seeding` 是**净额**（当前 SEEDING − 批次前 SEEDING），
                #       它答不了「今天干了活没有」—— 实测 `443` 连着两天一模一样
                #       （§26.50 三 那对 `-443/+443`），而同一封信里 `发出请求`
                #       从 162 掉到 37 ⇒ 两个读数**互相矛盾**，读者合理质疑数据有错。
                #     `first_seeding` 是**增量**：`attempt` 表里本窗内 MIN(at) 落在
                #       本批的片数（口径与 `state.py::trend()` 逐字相同 ⇒ 同一片
                #       反复 sync **不会**重复计数）。
                #   ★ `newly_seeding` **原样保留** —— `next_sleep()`（本文件 :339）
                #     与 `pilot_verdict` 都按**净额**语义读它，改它会波及自适应间隔
                #     判定与"该不该留这个包"的判据。所以这里只**新增**，不改旧的。
                #   ★ `until` 传 `None`（由 state 侧取此刻）：本批的**上界**就是
                #     "回灌写完之后"，不必在这里再取一次时钟。
                "first_seeding": st.first_seeding_between(pk, batch_t0),
            })
    finally:
        st.con.close()
    # ★ 整批的汇总（老字段保持**老语义**：单包时 == 那一个包；合池时 == 各段之和）。
    #   ★ `stats.resync` 只在单包时给"那一个" —— 合池时给**最后一段**会是错的读数，
    #     所以合池下显式留 None，改由各段自己报（`LOG.info` 逐段打印）。
    if len(segs) == 1:
        stats.resync = segs[0]["rep"]
    stats.still_skipped = sum(s["still_skipped"] for s in segs)
    stats.newly_seeding = sum(s["newly_seeding"] for s in segs)
    # ★ 真增量（件数，不是净额）—— 合池时是各段之和（各包各算各的首次转正）。
    stats.first_seeding = sum(s["first_seeding"] for s in segs)
    for s in segs:
        rep = s["rep"]
        LOG.info("[%s] 回灌：搜过 %s / 匹配 %s / 新增做种 %d / 仍 SKIPPED %d",
                 s["pack"], rep.from_db + rep.from_log, rep.matched,
                 s["newly_seeding"], s["still_skipped"])
    # 批次汇总 → 只进日志。好消息不该半夜吵醒人，也不该只躺在几万行日志里 ——
    # 现在（通知写侧已砍掉）它就在日志里，靠轮转保留。
    # ★★ `--pool` 下**每个包打一条**（`pack` = 真包名 ⇒ 日志按包可读）。
    #   ★ `ok`/`failed`/退避次数是**整批**的读数、**无法按包拆**（一轮 `DriveSession`
    #     是混着发的）⇒ 只在**第一段**记它们，其余段记 0 并在正文里写明。
    #     这比"按片数摊派"诚实 —— 摊派出来的数是编的，而 `A.12.1` 不许编读数。
    # 批次汇总 —— 只进日志（原先 `emit("batch")` 走每日摘要邮件；通知写侧已砍掉
    # 2026-10-05）。
    # ★★ `--pool` 下**每个包打一条**（`pack` = 真包名 ⇒ 日志按包可读）。
    #   ★ `ok`/`failed`/退避次数是**整批**的读数、**无法按包拆**（一轮 `DriveSession`
    #     是混着发的）⇒ 只在**第一段**记它们，其余段记 0 并在正文里写明。
    #     这比"按片数摊派"诚实 —— 摊派出来的数是编的，而 `A.12.1` 不许编读数。
    for i, s in enumerate(segs):
        pk, rep = s["pack"], s["rep"]
        first = (i == 0)
        LOG.info("[%s] 本批完成：%s新增做种 %d 部 / 做种净额 %d 部 / 仍 SKIPPED %d / "
                 "回灌 搜过 %d 匹配 %d%s",
                 pk,
                 "" if len(segs) == 1 else
                 f"（合池批 {len(segs)} 包：{', '.join(x['pack'] for x in segs)}）",
                 s["first_seeding"], s["newly_seeding"], s["still_skipped"],
                 rep.from_db + rep.from_log, rep.matched,
                 f" / 发送 成功 {stats.ok} 失败 {stats.failed}"
                 f" / 退避 {stats.backoff_hits} 次" if first else "")

    # 每天一次的台账（额度 + 趋势）+「有站点此刻在退避」的即时告警。
    # ★ 放在批次汇总之**后** —— 台账要反映刚回灌完的最新状态。
    after_batch_reports(args)

    return stats


# --------------------------------------------------------------------------- #
# 主循环
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# --once 的跨进程节流 + 包轮换（计划任务每 15 分钟唤醒时靠这个防重叠）
# --------------------------------------------------------------------------- #
STATE_FILE = STATE_DIR / ".drive-loop.state"


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 没有 / 损坏都当空状态
        return {}


#: 原子写用的临时后缀。★★ 这个字符串**不是随便选的**，见 `write_state` 的注释。
STATE_TMP_SUFFIX = ".tmp"


def write_state(d: dict) -> None:
    """整体覆盖 `.drive-loop.state`（**原子**：先写同目录临时文件，再 `os.replace`）。

    ★★ 为什么要原子（2026-09-18 加；原先是裸 `write_text`）：
      读者是 **容器外** 的 `compose.yaml` healthcheck —— `json.load(open(...))`。
      裸写在 truncate 与写完之间若被读走，读到的是**半截 JSON** ⇒ 解析抛异常
      ⇒ 判 **unhealthy** ⇒ autoheal 杀容器 ⇒ 又是一封「意外停止」邮件。
      ★ 这个窗口**本来就在**，但 `#27` 把心跳改成「睡眠期也刷」之后，
      写频率从「跑批期」扩到「**全时**」⇒ 暴露面变大，所以现在补。
    ★ 同形先例有三处：`.daily-report` / `.reconcile` / `.farm-check`
      （都 `tmp.write_text` → `os.replace`），以及（历史）`notify.py` 的 spool。

    ★★ 临时名**只能**是 `<原名>.tmp`，**不能**是 `.drive-loop.tmp.state` 之类。
      两个守卫对这个名字的判据**方向相反**（都实测跑过）：
        · 哨兵 `check-deploy-drift` 的 `KNOWN_NAS` 有一条
          「`drive-loop/scripts/` 下以 `.state` 结尾的隐藏文件」。
          临时名以 `.tmp` 结尾 ⇒ **不匹配** ⇒ 窗口内被扫到会报「未知文件」
          ⇒ 所以哨兵里**显式加了一条 `.tmp` 豁免**（不是靠通配糊过去）。
        · `chk58.sh` `[3]` 的反向那条要求「`.state` 结尾」——
          临时名不满足 ⇒ **自动逃过**，不必改。
      ⇒ 两个方向**只有这一个名字能同时满足**，别改。
      ★ 临时文件只在 `os.replace` 之前存在**微秒级**，且只在同一个目录里。
    """
    tmp = STATE_FILE.with_name(STATE_FILE.name + STATE_TMP_SUFFIX)
    try:
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, STATE_FILE)     # 同目录 rename ⇒ 原子；读者永远看到完整的一版
    except OSError as e:
        LOG.warning("写状态文件失败（忽略）: %s", e)
        # ★ 失败时清掉临时文件 —— 否则它会**留在目录里**，被哨兵/chk58 当成新文件。
        #   清理失败本身也吞掉：这个函数的契约是「写状态失败绝不拖垮批次」。
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# 心跳多久没刷新就认为上一批已经死了。批次里每 60s 刷一次，10 分钟足够宽裕
# （等于容忍 10 次丢拍），又能让被 kill 的残留批次在 10 分钟内被识别、不再挡住后续唤醒。
HEARTBEAT_STALE_SEC = 600

# ★★ `phase`：这个常驻进程**此刻在干什么**（`PHASE_IDLE` / `PHASE_RUNNING`）。
#
#   为什么需要它 —— `heartbeat_ts` **一职两用，而两个消费者对它的要求正好相反**：
#     · `compose.yaml` 的 healthcheck 问「**容器死了没有**」
#       ⇒ 睡眠期**也必须刷**，否则健康的容器被判 unhealthy、被 autoheal 杀掉。
#       （2026-09-18 实测：睡眠 45 分钟 ≫ 阈值 900s ⇒ **每 41 分钟一封「意外停止」邮件**。）
#     · `batch_alive()` 问「**上一批还在跑吗**」
#       ⇒ 睡眠期**绝不能刷**，否则永远判「在跑」⇒ 每轮跳过 ⇒ **静默永久停工**。
#   ⇒ 同一个字段不可能同时满足两边。**加上 `phase` 把两职拆开**：
#     healthcheck 继续看心跳（现在全时刷），`batch_alive()` 改看 `phase`。
#
#   ★ `phase` **不是 `mode` 的同义词**（见 `_resident_state` 的注释「不新造同义词」）：
#     `mode`   = **上次是哪条路**（身份，跨轮不变，只有常驻分支写）
#     `phase`  = **现在在干什么**（状态，每轮翻转，每次心跳都写）
#     两个维度正交，所以是两个字段而不是一个。
#
#   ★ 缺失时**必须**走原逻辑：升级窗口里旧版本写的状态文件没有这个键，
#     那时「只信心跳」是对的（与 `batch_alive()` 里那个「旧版格式」分支同理）。
PHASE_IDLE = "idle"        # 在睡觉 / 等下一批 —— 让位，别的容器可以接管
PHASE_RUNNING = "running"  # 正在跑批 —— 别抢


def write_heartbeat(phase: str | None = None) -> None:
    """刷新心跳（合并进现有状态，不动 running_pid）。

    `phase` 为 None 时**不动**已有的 `phase` 键（保持向后兼容：
    老的调用点一个参数都不传，行为与加这个参数之前**逐字相同**）。
    """
    st = read_state()
    st["heartbeat_ts"] = time.time()
    if phase is not None:
        st["phase"] = phase
    write_state(st)


class Heartbeat:
    """后台心跳线程：批次运行期间每 `period` 秒刷一次状态文件。

    ★ 为什么不能只靠 PID 判断"上一批还在不在"
      **PID 会被复用**（Windows 上尤其容易撞到，Linux 内核的 pid_max 也会绕回）。
      批次被强杀后状态文件里留着 running_pid，
      若那个号恰好被别的进程占用，pid_alive() 会永远返回 True
      —— 于是每次唤醒都判"上一批还在跑"，**静默永久停工**。
      这正是无人值守最怕的失败模式：不报错，只是不动。

      加了心跳就变成「PID 活着 **且** 心跳新鲜」才算在跑：进程真死了心跳必停，
      PID 被复用也救不回来。

    ★ 为什么用独立线程，而不是"每发一条 webhook 就刷一次"
      批次里有长时间不发请求的阶段 —— 等 cross-seed 日志静默（最多
      --drain-max-wait，默认 1 小时）和回灌。那些阶段没有可挂钩的事件，
      心跳会假死、被误判成残留。独立线程与批次同生共死，最省心。

    用法：`with Heartbeat(): stats = run_round(...)`
    ★★ 2026-09-18：新增 `phase` —— **跑批时用默认 `PHASE_RUNNING`；
      睡眠期另开一个 `Heartbeat(phase=PHASE_IDLE)` 把 `time.sleep()` 包起来。**
      这样心跳**全时新鲜**（healthcheck 不再误杀），而 `batch_alive()` 靠 `phase`
      仍能分辨「在跑」与「在睡」—— 详见 `PHASE_IDLE` 上方那段注释。
    """

    def __init__(self, period: float = 60.0, phase: str = PHASE_RUNNING):
        self.period = period
        self._phase = phase
        self._stop = threading.Event()
        self._th: threading.Thread | None = None

    def __enter__(self) -> "Heartbeat":
        # ★ 进 `with` 立刻刷一拍 —— 不刷的话第一拍要等 `period`（默认 60s），
        #   而「刚进睡眠」到「第一拍心跳」之间正好是 healthcheck 最该看到新鲜心跳的窗口。
        try:
            write_heartbeat(self._phase)
        except Exception:  # noqa: BLE001 —— 心跳失败绝不能拖垮批次
            pass
        self._th = threading.Thread(target=self._loop, daemon=True, name="drive-loop-hb")
        self._th.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(self.period):
            try:
                write_heartbeat(self._phase)
            except Exception:  # noqa: BLE001 —— 心跳失败绝不能拖垮批次
                pass

    def __exit__(self, *exc) -> bool:
        self._stop.set()
        if self._th:
            self._th.join(timeout=5)   # 确保不再与 finally 里写状态竞争
        return False


def pid_alive(pid) -> bool:
    """判断 pid 是否还在跑。

    ★ 原本这里为 Windows 分了一支（`os.kill(pid, 0)` 在 Windows 上**会真的
      把进程杀掉**，只能改用 `tasklist`）。电脑端退役后那支用不上了，
      注释保留在下面 —— 它记的是一个**反直觉的坑**：POSIX 上"发 0 号信号
      探测存活"是标准做法，搬到 Windows 上就变成"探测即击杀"。
    """
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    # [电脑端已退役 2026-09-12] Windows 专用的 tasklist 分支，现只剩 NAS(Linux) 一个环境：
    # if os.name == "nt":
    #     try:
    #         out = subprocess.run(
    #             ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
    #             capture_output=True, text=True, errors="replace", timeout=15,
    #         ).stdout
    #         return str(pid) in (out or "")
    #     except Exception:  # noqa: BLE001
    #         return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # ★★ 2026-09-17 补（`#58` 写测试时撞出来的）：**Windows 上少了这一支**。
        #   实测：Windows 的 `os.kill(不存在的 pid, 0)` 抛的是
        #   **`OSError`（winerror 87）**，而**不是** POSIX 的 `ProcessLookupError`
        #   （连 pid=1 那种"存在但无权限"也是 87）⇒ 原来会**把 OSError 抛出去**。
        #   ⇒ 生产只在 NAS(Linux) 跑，所以**生产没受影响**；但**测试在 Windows 跑**
        #     ⇒ 直接崩在 `pid_alive(999999)` 上。
        #   ★ 取值：`False`（判不出存活 ⇒ 当作**没在跑**）。
        #     理由：对两个平台都安全 —— 最坏也只是"多跑一批"，而反过来（猜 True）
        #     会变成"永远判在跑 ⇒ 静默停工"（本函数 docstring 说的正是这个坏法）。
        #   ★ 注意：这**不是**在鼓励在 Windows 上用它 —— Windows 上"发 0 号信号
        #     探测存活"是**探测即击杀**（见上面的注释），所以那边本来就不该调它。
        #     这一支只是让"意外调到"时**失败得安全**，而不是抛出去打断整条闸门。
        return False


def batch_alive(st: dict) -> bool:
    """上一批是否**真的**还在跑：PID 存活 **且** 心跳新鲜。

    只看 PID 会被 PID 复用骗到（详见 Heartbeat 的说明）——
    那会导致"永远判在跑 → 静默停工"。

    ★★ 2026-09-17（`#58` D1）：**容器里 PID 这一半必须停用**。
      实测（本机真容器里 import 本函数跑）：
        · 容器内 `os.getpid()` **恒为 1**（容器里第一个进程就是 PID 1）；
        · `pid_alive(1)` → **True**（那个 1 是**容器自己**）；
        · `batch_alive({"running_pid": 1, "heartbeat_ts": now})` → **True**
          ⇒ 下一个容器看到上一容器留下的 `1`，会判「上一批还在跑」——
          **而它判的那个 pid 就是它自己**。
      ⇒ 容器版写下的 `running_pid` **语义与宿主版不同**（宿主是真 pid，容器恒为 1），
        拿宿主那套 `pid_alive` 去读它 = **恒真的假信号**。
      ⇒ 所以容器版**显式标注** `running_pid_pidns="container"`，本函数据此**跳过 pid 那一半**，
        只信心跳。★ 宿主版不受影响（不写那个键 ⇒ 走原逻辑）。
      ★ 为什么不干脆删 pid 那一半：**宿主版还靠它**（pid 复用 + 心跳过期那道警告，
        真环境里是有用的）。两边的语义不同 ⇒ 分开判，而不是一起删。
    """
    pid = st.get("running_pid")

    # ★★ 容器写的 pid 不可比 —— 只信心跳（见 docstring）。
    if st.get("running_pid_pidns") == "container":
        # ★★ 2026-09-18：容器分支**先看 `phase`**，心跳降级为「卡死兜底」。
        #   起因：healthcheck 要心跳「睡觉也刷」，而这里要「睡觉别刷」——
        #   一职两用必然有一边被牺牲。加了 `phase` 之后两边各取所需。
        #   ★ 顺序很重要：`phase` 是**主判据**，心跳只用来确认
        #     「说在跑的那位**真的**在刷」——否则「卡死」（进程活着、不干活）
        #     就会变回检测不到，而卡死**正是当初加 autoheal 的理由**。
        phase = st.get("phase")
        if phase == PHASE_IDLE:
            return False            # 明确空闲 ⇒ 上一批早跑完了，放心接管
        if phase == PHASE_RUNNING:
            hb = st.get("heartbeat_ts")
            if hb is None:
                LOG.warning("phase=running 但缺 heartbeat_ts（文件被截断？）—— 保守跳过本轮。")
                return True
            stale = time.time() - float(hb or 0)
            if stale <= HEARTBEAT_STALE_SEC:
                return True
            LOG.warning("phase=running 但心跳已停 %.0f 分钟 —— 判定卡死，接管本轮。",
                        stale / 60)
            return False
        # ★ 旧格式（没有 `phase`）⇒ **原逻辑逐字保留**。
        #   升级窗口里状态文件还是旧版本写的，那时「只信心跳」是对的；
        #   下一批用新代码写状态后就带上 `phase` 了，此分支自然不再走到。
        hb = st.get("heartbeat_ts")
        if hb is None:
            # 容器版**一定**会带心跳（同一个 write_state 写进去的）⇒ 这里只可能是
            # 文件被截断/手改。保守当"在跑"（与下面旧格式分支同理：宁少跑一轮）。
            LOG.warning("容器版状态缺 heartbeat_ts（文件被截断？）—— 保守跳过本轮。")
            return True
        stale = time.time() - float(hb or 0)
        if stale <= HEARTBEAT_STALE_SEC:
            return True
        LOG.warning("容器版状态的心跳已停 %.0f 分钟 —— 判定为残留，接管本轮。",
                    stale / 60)
        return False

    if not pid_alive(pid):
        return False

    # ★ 区分「字段不存在」和「字段过期」—— 这两者处理方式**相反**。
    hb = st.get("heartbeat_ts")
    if hb is None:
        # 旧版（≤ 4f53425）写的状态文件没有心跳字段，**只在升级窗口出现一次**。
        # 那时 pid 确实还活着、批次真的在跑。
        # ★ 这里必须**保守当"在跑"**：抢先接管会让两批并发发 webhook，
        #   一次 429 能废掉几百条（README 坑 4）。少跑一轮的代价小得多。
        #   下一批用新代码写状态后就带上心跳了，此分支自然消失。
        LOG.warning("状态文件是旧版格式（无 heartbeat_ts），但 pid %s 仍在 ——"
                    " 保守当作「上一批还在跑」，本轮跳过。下批起恢复正常。", pid)
        return True

    stale = time.time() - float(hb or 0)
    if stale <= HEARTBEAT_STALE_SEC:
        return True
    LOG.warning("状态文件里的 pid %s 仍存在，但心跳已停 %.0f 分钟"
                "（PID 复用？进程卡死？）—— 判定为残留，接管本轮",
                pid, stale / 60)
    return False


def log_result(pack: str, stats: S.DriveStats | None) -> float | None:
    """把本批结果写进日志，并**返回算出的下批间隔**（交给 once_round 落盘）。

    ★ 返回值这一环是 2026-09-12 补的：在那之前 `next_sleep()` 的结论只进日志、
      不落任何地方，于是 `--once` 模式下真正的闸门是写死的 `min_sleep`，
      「站点在限流就缓一缓」这条策略**压根没接线**。现在把它交出去。
      返回 None = 本批没动作（没待搜 / 计划为空），调用方按「无退避证据」处理。
    """
    if stats is None:
        LOG.info("[%s] 本批无动作（没待搜或计划为空）", pack)
        return None
    sleep_sec, reason = next_sleep(stats)
    LOG.info("[%s] %s | 原因：%s | 下次间隔 %.0f 分钟", pack,
             stats.render().splitlines()[0], reason, sleep_sec / 60)
    if stats.newly_seeding:
        LOG.info("  → 本轮新增做种 %d 部 🎉", stats.newly_seeding)
    if stats.still_skipped:
        LOG.warning("  → 仍有 %d 部被退避（站点侧 502/限流？见 SUMMARY §6.5）",
                    stats.still_skipped)
    return sleep_sec


def once_round(args, api_key: str, min_sleep: float) -> int:
    """`--once` 单批模式：跨进程节流（防计划任务唤醒重叠）+ 跑**一池**。

    - 上一批还在跑（pid 活着）→ 直接退出
    - 距上次批次结束不足 **闸门** 秒 → 直接退出（防猛打）
    - 否则跑**一池**（扫 `pack` 表全部登记包取全局前 N），并把 {结束时间, 下批间隔}
      写回状态文件

    ★★ 2026-10-05（去掉「包的闸门」）：**没有"轮到的那个包"了** —— 每批都取全局
      池前 N。原先的 `last_pack_idx` 取模轮换**已删**（那是"名单"机制的产物）。
      ⇒ `--once` 与常驻**共用同一套取池逻辑**（都在 `run_round` 里），差别只在
         `--once` 每进程一批、常驻循环多批。

    ★ 闸门 = max(min_sleep, 上一批算出的间隔)。上一批撞了退避 → `next_sleep()`
      给 1.5~2 小时，这里就真的会等那么久；站点健康 → 45 分钟，但不短于
      `min_sleep`（默认 30 分钟）。2026-09-12 之前这里**只有** min_sleep 一项，
      `next_sleep()` 的结论不落盘、也就没人用 —— 退避分级形同虚设。
    """
    st = read_state()
    if batch_alive(st):
        LOG.info("上一批（pid %s）仍在运行，跳过本轮", st.get("running_pid"))
        return 0
    last_end = float(st.get("last_end_ts") or 0)
    last_sleep = float(st.get("last_sleep_sec") or 0)
    wait = clamp(max(min_sleep, last_sleep))
    gap = time.time() - last_end
    if last_end and gap < wait:
        # ★ 2026-09-12 改判据。原先这里是 `if wait > min_sleep:` → 一律打印
        #   「上一批报告站点在退避」——**在完全正常的健康档也成立**，因为健康档
        #   BASE_SLEEP(45 分钟) 本就大于 min_sleep(30 分钟)。实测 NAS 上 09-12
        #   10:00/10:15/10:30/11:15 四次跳过全是 45 分钟健康档，却全报「在退避」，
        #   而**同一批**上面那行刚说过「无退避、无新增（正常）」—— 一个批次的两行
        #   日志自相矛盾。这不是行为错（闸门确实按 45 分钟挡住了），是文案在
        #   **断言一个没有依据的原因**；下次真出限流时，这行会把人往错的方向带。
        #   真退避档只可能是 SNOOZE(90)/BACKOFF(120)/ABORT(180)，都**严格大于**
        #   BASE_SLEEP，所以判据要用「last_sleep 超过了健康档」而不是「超过了硬下限」。
        if last_sleep > BASE_SLEEP:
            why = "上一批报告站点在退避"
        elif min_sleep > last_sleep:
            # last_sleep 还没记录（0）或被 --min-sleep 抬到了更大 —— 此时是下限在管
            why = "按 --min-sleep 下限"
        else:
            why = "按上一批算出的批间隔"
        LOG.info("距上次批次结束仅 %.1f 分钟（< %.0f 分钟：%s），跳过本轮",
                 gap / 60, wait / 60, why)
        return 0

    # ★★ 2026-10-05（去掉闸门）：**没有"轮到哪个包"** —— 每批都取全局池前 N。
    #   ★ `last_pack_idx` 的取模轮换**已删**（那是"名单"机制的产物；见 `once_round`
    #     docstring）。这里只给批次一个**日志标签**，取件完全由 `run_round` 里的
    #     `todo_pooled(require_packs(st), …)` 决定。
    pack = "pool"
    # ★★ `#58` D1：容器里 `running_pid` **语义不同**（恒为 1，见 batch_alive 的说明）。
    #   这里自动判"我是不是在容器里"，容器版就多写一个标记键，让 batch_alive 跳过 pid。
    #   ★ 判据用**文件系统**（`/.dockerenv`）而不是环境变量：环境变量可能是人传进来的、
    #     也可能是 compose 特意设的，而 `/.dockerenv` 是**运行时自己长出来的**。
    _in_container = os.path.exists("/.dockerenv")
    # ★★ 2026-09-18：显式写 `phase`。这里原本是 `{**st, ...}`（合并）⇒ 若 `st` 里
    #   恰好有上一轮残留的 `phase`，就会被**继承**下来 —— 而「继承一个不属于本进程的
    #   阶段」正是 `phase` 最危险的坏法（残留 `running` ⇒ 下一个容器永远判「在跑」）。
    #   ⇒ 一律**显式声明**，不靠合并。
    write_state({**st, "running_pid": os.getpid(),
                 "running_pid_pidns": "container" if _in_container else "host",
                 "phase": PHASE_RUNNING,
                 "heartbeat_ts": time.time()})
    if _in_container:
        LOG.info("[容器] running_pid=%d（容器内 PID，**不参与存活判据**；只信心跳）",
                 os.getpid())
    LOG.info("[--once] 跑池（全局前 --limit %s）", args.limit)

    stats = None
    sleep_sec = None      # 本批算出的下批间隔；没跑成 / 无动作 → None（按无退避证据）
    rc = 0
    failed = False
    try:
        # 心跳线程只包住跑批阶段：跑完就停，免得和下面 finally 写状态打架
        # ★★ 2026-09-18：显式 `PHASE_RUNNING`。`once_round` 写的是**宿主**语义
        #   （`running_pid_pidns="host"`），所以 `batch_alive()` 的容器分支**读不到**
        #   它；但 `write_heartbeat` 是**合并写**——万一那份状态被**容器**读到
        #   （例如宿主任务与常驻容器交接的窗口），写一个准确的 `running` 是对的。
        #   ★ 也**不能**在这里写 `idle`：这一批**确实在跑**。
        with Heartbeat(phase=PHASE_RUNNING):
            stats = run_round(pack, args, api_key)
        sleep_sec = log_result(pack, stats)
        if stats is None:
            # 这个池没待搜 —— 看看是不是所有登记的包都干完了（无人值守时必须出声）
            # ★ 池源 = `pack` 表全部登记的包。空表本应由 run_round 里的 `require_packs`
            #   抛出 `NoPacksRegistered` 走下面 except —— 但那条路在**本分支**之前，
            #   这里 run_round 已返回 None（可能是"池真空"而不是"表空"）。
            #   ★ 读库这一步**自己兜住异常**：`alert_if_all_done` 是收尾通知，
            #     读不到库时**不该**让整批判成异常（同 reconcile_watch 的规矩）。
            try:
                _cp = S.StateStore(args.db)
                try:
                    _pool = pool_packs(_cp)
                finally:
                    _cp.con.close()
                alert_if_all_done(len(_pool), args.db, _pool)
            except Exception:            # noqa: BLE001 —— 收尾通知失败不影响收尾
                LOG.debug("全完成判定跳过（读库失败）", exc_info=True)
            # ★ 农场巡检**不能只挂在「批次跑完了」那条路上**：它在
            #   after_batch_reports 里，而 run_round 没待搜时会**提前 return None**，
            #   根本走不到那里。于是"所有包都搜完了"的那几天，巡检会**跟着一起停**
            #   —— 而那恰恰是最该跑的时候：没在搜索不等于农场没漂移，
            #   只等于**没人看了**。等哪天真要搜了，漂移已经攒了几天。
            #   它自己有 20 小时间隔兜着，所以这里多调一次几乎总是空转（只读一个状态文件）。
            check_farm()
    except Exception:  # noqa: BLE001 —— 单批异常也要正确收尾状态
        # ★ 通知写侧已砍掉（2026-10-05）：原先这里 `emit("alert", …, key="batch-exception:…")`。
        LOG.exception("[%s] 本批异常（常见：cross-seed 没起来 / API 连不上 / "
                      "state.db 被占用 / NAS 掉线）；连续 3 批会升级为「连续批失败」",
                      pack)
        failed, rc = True, 1
    finally:
        # 连续失败计数跨进程持久化（无人值守下没人盯着，只能靠日志喊）
        # ★ 两个计数（真失败 / 良性提前收工）各存各的 —— 见 update_abort_streak
        prev_streak = (int(st.get("consec_abort") or 0),
                       int(st.get("consec_backoff") or 0))
        streak, backoff_streak = update_abort_streak(prev_streak, stats,
                                                     failed=failed)
        # ★ last_sleep_sec 必须落盘 —— 下一轮的闸门读它（见 once_round 开头）。
        #   sleep_sec 为 None（没跑成 / 本包无待搜）时写 0，即「没有退避证据」，
        #   闸门退回 min_sleep 下限。
        #   注意这个 write_state 是**整体覆盖**、不合并 st（这是刻意的：顺便把
        #   heartbeat_ts 清掉，否则残留的心跳会让下一轮误判「上一批还在跑」）。
        # ★★ 2026-09-18：这里同样**显式写 `phase`**。这是整体覆盖（不是 `{**st}`），
        #   所以不写就等于**删掉** `phase` ⇒ 下一个读者走「旧格式」分支（只信心跳）
        #   ⇒ 「说在跑的那位其实早收工了」检测不到。写 `idle` = 「我不在了」。
        write_state({"running_pid": None, "last_end_ts": time.time(),
                     "consec_abort": streak,
                     "consec_backoff": backoff_streak,
                     "phase": PHASE_IDLE,
                     "last_sleep_sec": clamp(sleep_sec) if sleep_sec else 0.0})
    LOG.info("--once 完成。")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="反馈驱动循环：自动续跑 cross-seed 搜索")
    # ★★ 2026-10-05（去掉「包的闸门」）：`--packs` / `--pool` / `--batch` 三个参数
    #   **已删**。取件永远扫 `pack` 表全部登记的包（见 `pool_packs` / `run_round`），
    #   `--limit` 是**全局**的。删 `--batch` 的理由：它只在**非池**的"切片"语义下
    #   有意义，池是"每次从头取前 N"，`--batch` 本就无效（原先只是**告警后忽略**，
    #   现在连同它的非池分支一起退休）。
    ap.add_argument("--once", action="store_true", help="只跑一轮（配合计划任务）")
    # ★ 为什么需要它：有几个观测（链接守护、对账基线、卡 999）**只挂在日报里**
    #   —— 它们要有个「锚」才能谈"变化"，而自然日报一天只投一次。想**当场起锚**
    #   （比如刚刚改了 cross-seed 的匹配策略，要立刻开始盯）就得能手动催一次。
    #   语义是 `report_daily(force=True)`：**绕开「今天已投过」这道闸**，所以
    #   真的会再发一封日报邮件 —— 是故意的，人主动跑的命令，不该静默什么都不做。
    ap.add_argument("--daily-now", dest="daily_now", action="store_true",
                    help="立刻投一次日报并退出（给「挂在日报里」的观测当场起锚）")
    ap.add_argument("--max-rounds", type=int, default=0, help="最多跑几轮（0=不限）")
    ap.add_argument("--min-sleep", type=float, default=0, help="批间最小等待秒数（覆盖默认）")
    # --- 透传 drive 参数 ---
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--db-path", dest="db_path")
    ap.add_argument("--url", default="http://192.168.0.7:2468")
    ap.add_argument("--qbit-url", dest="qbit_url", default="http://192.168.0.7:3060",
                    help="qB :3060 地址（回灌必需，否则 SEEDING 会被误降级）")
    ap.add_argument("--category", default="reseed-singles")
    ap.add_argument("--log", action="append", default=None,
                    help="cross-seed 日志路径（回灌用，可多次；默认用 NAS 上的 info.current.log）")
    ap.add_argument("--indexer-alias", action="append", default=None)
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--env", default=str(ROOT / ".env"))
    ap.add_argument("--indexers", default="HDFans,NanyangPT")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--check-every", type=int, default=10)
    ap.add_argument("--backoff-check-secs", type=float, default=60.0,
                    help="★除了每 --check-every 条，再按秒数检查索引器退避，默认 60。"
                         "实测 429 只 snooze 55 秒，而 10 条×30s=300s 才看一眼 —— "
                         "窗口整段落在两次检查之间，退避就等于没发生（backoff_hits 恒 0）")
    ap.add_argument("--max-wait", type=float, default=1800.0)
    ap.add_argument("--no-pause-on-backoff", action="store_true")
    ap.add_argument("--settle", type=float, default=90.0)
    ap.add_argument("--drain-max-wait", type=float, default=3600.0)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--include-cooldown", action="store_true")
    ap.add_argument("--cadence-days", type=int, default=S.DEFAULT_CADENCE_DAYS)
    ap.add_argument("--cadence", default=None)
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不发请求")
    # ★ 通知参数（--notify-spool / --no-notify / --notify-cooldown-hours）已**整体删除**
    #   （2026-10-05）：通知写侧已砍掉，这些参数没有任何调用方 —— 留着只会让
    #   `--help` 和文档里出现"以为能发信"的误导。要发信 ⇒ 去独立通知项目。
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            # ★必须轮转：无人值守会跑几个月，FileHandler 只追加会涨到几百 MB
            RotatingFileHandler(
                str(STATE_DIR / "drive-loop.log"),
                maxBytes=5_000_000, backupCount=5, encoding="utf-8",
            ),
        ],
    )

    # 默认日志路径（cross-seed 的 info 日志），回灌要用
    if not args.log:
        p = first_existing("cross-seed/logs/info.current.log")
        if p:
            args.log = [p]
            LOG.debug("info 日志：%s", p)
        else:
            LOG.warning("⚠ 找不到 cross-seed 的 info 日志（试过 %s）——"
                        " 回灌将**推不出「是否在做种」**，原本 SEEDING 的片子会被"
                        " 误降级成 MATCHED。", " 和 ".join(CROSSSEED_DIRS))

    # 默认 cross-seed.db 路径
    if not args.db_path:
        p = first_existing("cross-seed/cross-seed.db")
        if p:
            args.db_path = p
            LOG.debug("cross-seed.db：%s", p)
        else:
            LOG.warning("⚠ 找不到 cross-seed.db（试过 %s）——"
                        " 回灌与索引器自检都会跳过，状态机会停止更新。",
                        " 和 ".join(CROSSSEED_DIRS))

    # 默认 .env（取 CROSSSEED_API_KEY）。★ 默认值 ROOT/.env 在 Windows 上对，
    # 但搬到 NAS 后脚本住在 <compose>/drive-loop/，.env 在**上一级**。
    # 所以这里补一个回退：ROOT/.env 不在就去 compose 目录找。这样即使 run.sh
    # 忘了传 --env，也不会退化成"找不到 key"直接退出码 2。
    if not Path(args.env).is_file():
        for d in CROSSSEED_DIRS:
            cand = Path(d) / ".env"
            if cand.is_file():
                LOG.debug("--env 默认值 %s 不存在，改用 %s", args.env, cand)
                args.env = str(cand)
                break

    LOG.info("=== drive-loop 启动：池=pack 表全部登记包（无名单） indexers=%s limit=%d ===",
             args.indexers, args.limit)

    # ★ 通知写侧已整体砍掉（2026-10-05）：原先这里 `init_notifier(args)` +
    #   `describe_notifier()`，以及随后那段「通知目录不存在」的兜底自检。
    #   现在没有通知器，也没有可写的 spool —— 要发信去独立通知项目。

    # --daily-now：把每日台账**就地打印一次**到日志（原先走邮件投递）。
    if getattr(args, "daily_now", False):
        sent = report_daily(args, force=True)
        LOG.info("--daily-now：日报%s", "已打印" if sent else "未打印")
        return 0 if sent else 4

    # 自检：cross-seed 真实搜索范围 vs --indexers（不一致会静默错记，务必先喊出来）
    check_indexers(args)

    # 自检：cross-seed 是否正够不着某些索引器（头号复发坑 = .env 没生效到容器）
    check_env_applied(args)

    # key：--api-key 显式给优先，否则从 --env 读
    api_key = args.api_key
    if not api_key:
        env_path = Path(args.env)
        if env_path.is_file():
            api_key = read_key_from_env(env_path)
        if not api_key:
            LOG.error("找不到 CROSSSEED_API_KEY（--api-key 或 --env 的 .env）")
            return 2

    # 批次之间的最小间隔（常驻模式用它 sleep；--once 模式用它拦掉过密的唤醒）
    min_sleep = args.min_sleep or MIN_SLEEP

    # --once：单批模式（配合 DSM 计划任务）。跨进程节流见 once_round()。
    if args.once and not args.dry_run:
        return once_round(args, api_key, min_sleep)

    round_no = 0
    consec = (0, 0)      # (连续真失败批数, 连续良性收工批数)，各自跨阈值报警

    # ------------------------------------------------------------------ #
    # ★★ `#58` 15.a 修复（2026-09-17）：**常驻分支也要落盘状态与心跳**。
    #
    # 修之前：`write_state()` 全文件只有三处调用，**全在 `once_round()` 里**
    #   ⇒ 常驻模式 `.drive-loop.state` 的 `heartbeat_ts` **这键根本不存在**。
    #   而 `batch_alive()` 对容器版**只信心跳**（缺心跳时**保守返回 True**）
    #   ⇒ 「容器卡死后被接管」**永远不会发生**：新容器读到一个没有心跳的状态，
    #     按保守分支判「上一批还在跑」，于是**永远不接管**。
    #   ★ 这正是 README「还没做」15.a 记的那条「不报错、只是不动」的缺陷。
    #
    # 修法：**复用**已有机制（`write_state` / `Heartbeat`），不新造第二个写法 ——
    #   与 `once_round()` 的 `:2296-2311` 段保持**同一形状**，读代码的人只需认一套。
    #
    # ★ 新增一个 `mode` 键，专为 `§26.5` 前提 ② 那个缺口：
    #   常驻与 `--once` **没有任何跨进程互斥**（一个写 `"container"`、一个写 `"host"`，
    #   `batch_alive()` 两者互不相识）。两条路同时在跑时，
    #   光看状态文件**分不出是谁写的** —— `mode` 就是让这件事**能被看出来**。
    #   ⇒ `cat .drive-loop.state` 一眼可知当前是 `resident` 还是 `once`。
    #
    # ⚠ **修完仍不能**解决「容器卡死后自动重启」：`restart: unless-stopped` 只对
    #   容器**退出**生效，**healthcheck 不健康并不触发重启**（README 15.a 末段）。
    #   接管要靠 `autoheal` 侧车或 DSM 轮询 `docker inspect` —— **本次不写**。
    #   但心跳修好之后，那条路的**判据才存在**（在此之前判据恒不成立）。
    # ------------------------------------------------------------------ #
    _in_container = os.path.exists("/.dockerenv")
    _resident_started = time.time()
    #: 常驻模式的状态快照。**写在一个地方**（而不是在每个 `continue` 前各写一遍）——
    #: 循环里有三处 `continue`、一处正常返回，抄四遍必然会漂。
    def _resident_state(round_n: int, streak, note: str,
                        phase: str = PHASE_IDLE) -> None:
        """刷新常驻状态：心跳 + round + 包下标 + 连续失败计数。

        ★ 字段名**沿用 `once_round()` 已有的**（`consec_abort` / `consec_backoff`），
          不新造同义词 —— 否则 `batch_alive()` 与文档都要认两套。
          ★★ 2026-10-05（去掉闸门）：`last_pack_idx` **已删** —— 它记的是"轮到第几个
            包"的下标，而池路径**没有"轮到谁"**（每批都取全局池前 N）。
        ★ `running_pid` 在容器里语义不同（恒为 1）⇒ 照 `once_round` 的做法带
          `running_pid_pidns` 标记，让 `batch_alive()` 跳过 pid 那一半。
        ★★ 2026-09-18：新增 `phase`，**默认 `PHASE_IDLE`** —— 这个函数在四处的调用里
          有三处（启动 / dry-run / 无动作 / 正常收工）都是「**我现在要让位**」，
          只有跑批期间该是 `running`，而跑批期间由 `Heartbeat` 负责刷。
          ⇒ 默认写 `idle` 是**保守且正确**的：万一某一处漏了传参，
            后果是「别的容器可能来接管」，**不会**是「永远判在跑 ⇒ 静默停工」。
        """
        write_state({"running_pid": os.getpid(),
                     "running_pid_pidns": "container" if _in_container else "host",
                     "heartbeat_ts": time.time(),
                     "mode": "resident",
                     "phase": phase,
                     "round": round_n,
                     "consec_abort": int(streak[0]),
                     "consec_backoff": int(streak[1]),
                     "started_ts": _resident_started,
                     "note": note})

    _resident_state(0, consec, "启动")
    LOG.info("常驻模式：已写状态（pid=%d pidns=%s；round/heartbeat 每轮刷新）",
             os.getpid(), "container" if _in_container else "host")

    while args.max_rounds == 0 or round_no < args.max_rounds:
        # ★★ 2026-10-05（去掉闸门）：**每轮都取全局池的前 N**（没有"轮到谁"了）。
        #   `pack` 只是批次**日志标签**；取件完全由 `run_round` 决定。
        pack = "pool"
        round_no += 1

        if args.dry_run:
            LOG.info("[%s] dry-run：列出待搜计划（不发请求）", pack)
            st = S.StateStore(args.db)
            idx = [i.strip() for i in (args.indexers or "").split(",") if i.strip()] or None
            # ★ 池源 = `pack` 表全部登记的包（空表抛 NoPacksRegistered，不静默算空）。
            pool = require_packs(st)
            pooled = st.todo_pooled(
                pool, indexers_now=idx,
                include_cooldown=args.include_cooldown,
                cadence_days=args.cadence_days,
                cadence_by_indexer=S.parse_cadence(args.cadence),
                limit=args.limit)
            from collections import Counter as _C
            by_pack = _C(pk for pk, _, _ in pooled)
            LOG.info("[pool] dry-run：全池 %d 部待搜，本批取全局前 %d"
                     "（--limit %d）；本批涉及包：%s",
                     sum(1 for pk in pool
                         for _ in st.todo_detail(
                             pk, indexers_now=idx,
                             include_cooldown=args.include_cooldown,
                             cadence_days=args.cadence_days,
                             cadence_by_indexer=S.parse_cadence(args.cadence))),
                     len(pooled), args.limit,
                     ", ".join(f"{k}×{v}" for k, v in sorted(by_pack.items()))
                     or "（无）")
            st.con.close()
            if args.once:
                return 0
            _resident_state(round_no, consec, "dry-run")
            with Heartbeat(phase=PHASE_IDLE):
                time.sleep(min_sleep)
            continue

        t0 = time.time()
        LOG.info("[第 %d 轮] 跑包 %s ...", round_no, pack)
        try:
            # ★ 心跳线程只包住跑批阶段（与 once_round 同形）：跑完就停，
            #   免得和循环尾部写状态打架。批次里有长时间不发请求的阶段
            #   （等日志静默最多 --drain-max-wait、回灌），所以必须**独立线程**，
            #   不能靠"每发一条 webhook 刷一次"（见 Heartbeat docstring）。
            # ★★ 2026-09-18：显式给 `phase=PHASE_RUNNING` —— 这是**唯一的**跑批态，
            #   其它三处（启动 / dry-run / 无动作 / 收工）都该是 `idle`。
            with Heartbeat(phase=PHASE_RUNNING):
                stats = run_round(pack, args, api_key)
        except Exception as e:  # noqa: BLE001 —— 循环不能因单批异常而死
            # ★ 通知写侧已砍掉（2026-10-05）：原先这里 `emit("alert", …, key="batch-exception:…")`。
            LOG.exception("[%s] 本批异常（继续循环）：%s: %s —— 常见：cross-seed 没起来 / "
                          "state.db 被占用 / NAS 掉线；连续 3 批会升级为「连续批失败」",
                          pack, type(e).__name__, e)
            consec = update_abort_streak(consec, None, failed=True)
            if args.once:
                return 1
            _resident_state(round_no, consec, "本批异常")
            with Heartbeat(phase=PHASE_IDLE):
                time.sleep(BACKOFF_SLEEP)
            continue

        if stats is None:
            LOG.info("[%s] 本批无动作（没待搜或计划为空）", pack)
            consec = update_abort_streak(consec, None)   # 正常，两个计数都清零
            # ★★ 2026-10-05（去掉闸门）：**一个批就是整个池** ⇒ "本批没待搜"
            #   就**直接**等于"全部登记的包都没待搜" ⇒ 立刻判全完成，
            #   不用再等"每个包都轮到一次空批"（老的非池逻辑已删）。
            #   ★ 空表（包数 0）**不到这里**：池源为空的故障已由 `run_round` 里的
            #     `require_packs()` 抛出、走上面的 except，**不假报"全干完"**。
            _cp = S.StateStore(args.db)
            try:
                _pool = pool_packs(_cp)
            finally:
                _cp.con.close()
            if alert_if_all_done(len(_pool), args.db, _pool):
                return 0
            if args.once:
                return 0
            _resident_state(round_no, consec, "本批无动作")
            with Heartbeat(phase=PHASE_IDLE):
                time.sleep(min_sleep)
            continue

        sleep_sec, reason = next_sleep(stats)
        consec = update_abort_streak(consec, stats)
        LOG.info("[第 %d 轮] %s | 原因：%s | 下次间隔 %.0f 分钟",
                 round_no, stats.render().splitlines()[0] if stats else "?",
                 reason, sleep_sec / 60)
        LOG.info("  → %s", reason)
        if stats.newly_seeding:
            LOG.info("  → 本轮新增做种 %d 部 🎉", stats.newly_seeding)
        if stats.still_skipped:
            LOG.warning("  → 仍有 %d 部被退避（站点侧 502/限流？见 SUMMARY §6.5）",
                        stats.still_skipped)

        if args.once:
            LOG.info("--once 模式：本轮完成，退出")
            return 0
        sleep_sec = max(sleep_sec, min_sleep)
        _resident_state(round_no, consec, "正常收工")
        LOG.info("等待 %.1f 分钟后跑下一池（全局前 --limit %s）",
                 sleep_sec / 60, args.limit)
        # ★★ 2026-09-18：**这就是那 45 分钟的睡眠** —— autoheal 误杀就发生在这里。
        #   包一层 `PHASE_IDLE` 心跳之后，心跳**全时新鲜** ⇒ healthcheck 不再判死；
        #   而 `batch_alive()` 看到 `phase=idle` ⇒ 知道该让位（而不是「永远在跑」）。
        with Heartbeat(phase=PHASE_IDLE):
            time.sleep(sleep_sec)

    # ★ 循环正常结束（`--max-rounds` 到了）—— 收尾与 `once_round` 的 finally 同形：
    #   把 running_pid 置空，免得下一次唤醒看到残留 pid。
    #   ★ 这里**同时清掉 heartbeat_ts**（整体覆盖、不合并）——理由与 `once_round`
    #     的注释写的一样：残留的心跳会让下一轮误判「上一批还在跑」。
    #   ★★ 2026-09-18：**必须同时写 `phase`**。残留 `PHASE_IDLE` 是安全的
    #     （它就说「我让位」）；但残留 `PHASE_RUNNING` 会让下一个容器
    #     一直判「在跑」——而「永远判在跑 ⇒ 静默永久停工」正是本仓最怕的形状。
    #     ⇒ 收尾**一律写 `idle`**，与上面清心跳同一个目的：把「我不在了」说清楚。
    write_state({"running_pid": None, "last_end_ts": time.time(),
                 "consec_abort": int(consec[0]),
                 "consec_backoff": int(consec[1]),
                 "mode": "resident",
                 "phase": PHASE_IDLE,
                 "round": round_no})
    LOG.info("常驻模式：已到 --max-rounds=%d，正常收尾。", args.max_rounds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
