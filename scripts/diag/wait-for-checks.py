#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""等 qB 的校验队列排空 —— **只读**轮询，把「还在校验的那条」等过去再动手。

为什么需要它
------------
`migrate-reseed-dirs.py` 有一道**默认闸**：`checkingDL / checkingUP / moving /
allocating` 的种子一律**不碰**（见 SUMMARY §18.9.2 —— 对校验中的种子调
`setLocation`，会先改 `save_path` 再搬数据，中间态里校验按新路径跑 → 必然
`missingFiles`，而且把已经跑了一半的校验整个作废）。

于是搬迁的**收尾**变成「等那几条校完 → 复跑一次迁移脚本」，中间这段只能是
**定时看**。本脚本就是那个"看"：反复快照，直到队列空了才退出。

★ 退出条件只认 `checkingDL`，**不等 `checkingUP`**（2026-09-12 实测修正）
--------------------------------------------------------------------------
原先写成「checkingDL 和 checkingUP 都归零」，那是**错的**：qB 的校验是**单线程**
的，`checkingUP` 那批排在我们真正关心的 `checkingDL` **后面**，实测 116 条
停在 progress≈0 一动不动，按 ~4.5 分钟/条算是**9 小时**的量级；而我们要等的
只有 11 条（≈45 分钟）。等的目标搞错了，就会白等一晚上。

★ 这跟「期望值不能来自被校验对象本身」是同一族教训（§16.2.1）：
**先想清楚"等到什么才算完"，再写循环条件。**

退出码
------
    0  `checkingDL` 归零 —— 可以复跑 `migrate-reseed-dirs.py --all-tags --apply` 了
    2  超时（`--rounds` 用完了，队列里还有）—— 加大 `--rounds` 再等，或看看到底卡在哪条
    3  qB 一次都没连上（地址/端口/网络问题；中途偶发的连不上**不算**，见下面重试）

用法
----
    python scripts/wait-for-checks.py                      # 默认等 90 分钟（45 轮 × 120s）
    python scripts/wait-for-checks.py --rounds 8 --gap 60  # 短一点，调试用
    QBIT_URL=http://192.168.0.7:3320 python scripts/wait-for-checks.py

本脚本**只读**：只调 `/api/v2/torrents/info`，不写任何东西。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover - 老 python / 非 tty
    pass

#: 与项目其它脚本一致：默认就是 NAS 的局域网地址（从 NAS 宿主机跑和从 Windows
#: 跑走的是同一条路），不需要改。
#: ★ 2026-10-06 起保种实例改名/多实例：旧 qbittorrent-reseed:3060 →
#:   qbit-reseed1:3320（宿主端口实测见 `docker ps`；3060 已无任何容器发布）。
DEFAULT_URL = os.environ.get("QBIT_URL", "http://192.168.0.7:3320")

#: 搬迁期间的「旧根」—— 用它把「还没搬走的」单独数出来给人看。
#: 这是**观测量**，不是本脚本的判断依据（判断只看 checkingDL）。
DEFAULT_OLD = "/volume1/video/download/reseed_singles"
DEFAULT_NEW = "/volume1/video/download/reseed/reseed_singles"

#: 我们关心的在途状态。与 migrate-reseed-dirs.py 的 IN_FLIGHT_STATES 对齐 ——
#: 那边是"不许碰"的集合，这里是"要等它停下来"的集合，同一批状态。
IN_FLIGHT_STATES = ("checkingDL", "checkingUP", "moving", "allocating")


def snapshot(url: str):
    """取一次 /torrents/info。★ 不发 Referer 之外的任何头，绝不带凭据。"""
    req = urllib.request.Request(url + "/api/v2/torrents/info", headers={"Referer": url})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="轮询 qB 直到校验队列排空（只读）。退出码见文件头。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--url", default=DEFAULT_URL, help=f"qB 地址（默认 {DEFAULT_URL}，也可用 QBIT_URL）")
    ap.add_argument("--old", default=DEFAULT_OLD, help="旧根路径，仅用于打印「仍在旧根=N」")
    ap.add_argument("--new", default=DEFAULT_NEW, help="新根路径，用于把新根下的排除掉")
    ap.add_argument("--rounds", type=int, default=45, help="最多看几轮（默认 45）")
    ap.add_argument("--gap", type=int, default=120, help="每轮间隔秒（默认 120）")
    args = ap.parse_args()

    ok_ever = False
    for i in range(args.rounds):
        try:
            ts = snapshot(args.url)
        except (urllib.error.URLError, OSError, ValueError) as e:
            # ★ 网络抖一下不该让整个等待作废 —— 90 分钟的轮询里遇到一次超时很正常。
            #   但**一次都没连上**要区分开（那就是配置问题了），所以记 ok_ever。
            print(f"[{time.strftime('%H:%M:%S')}] 连不上 qB（{type(e).__name__}: {e}）—— 这轮跳过", flush=True)
            if i < args.rounds - 1:
                time.sleep(args.gap)
            continue
        ok_ever = True

        c = Counter(t["state"] for t in ts)
        still_old = [t for t in ts
                     if t["save_path"].startswith(args.old)
                     and not t["save_path"].startswith(args.new)]
        in_flight = sum(c.get(s, 0) for s in IN_FLIGHT_STATES)

        # ★ 打印的全是**计数**：不打印种子名、`tracker`、`content_path`
        #   （`tracker` 带明文 announce passkey，见项目安全约定）。
        print(f"[{time.strftime('%H:%M:%S')}] 总数={len(ts)}  "
              f"stalledUP={c.get('stalledUP', 0)}  pausedUP={c.get('pausedUP', 0)}  "
              f"checkingUP={c.get('checkingUP', 0)}  checkingDL={c.get('checkingDL', 0)}  "
              f"|| 在途={in_flight}  仍在旧根={len(still_old)}", flush=True)

        if c.get("checkingDL", 0) == 0:
            print("★ checkingDL 已归零 —— 可以复跑迁移脚本把剩下那几条带走了：", flush=True)
            print("    python scripts/migrate-reseed-dirs.py --all-tags --apply", flush=True)
            print("  （脚本默认会跳过 checking*/moving，所以就算还有 checkingUP 也安全；", flush=True)
            print("    但「旧根空了」要等它们也停下才成立，见 SUMMARY §18.9.3）", flush=True)
            return 0

        if i < args.rounds - 1:
            time.sleep(args.gap)

    if not ok_ever:
        print(f"[!!] {args.rounds} 轮一次都没连上 {args.url} —— 先查地址/端口/网络。", flush=True)
        return 3
    print("超时：checkingDL 还没归零，没等到。加大 --rounds 再来。", flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
