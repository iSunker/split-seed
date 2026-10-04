#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读检查 cross-seed 的索引器状态：谁「真的搜出去过」、谁在被限流。

为什么需要它
------------
`drive-loop-nas.sh` 里「加站的正确顺序」第 ③ 步要求：
**确认 `cross-seed.db` 的 `timestamp` 表里开始出现该站的行，之后才动 `--indexers`。**

`timestamp` 的主键是 (searchee_id, indexer_id)，而且**失败的搜索不会留下行** ——
所以「出现了该站的行」就等于「真的发出去并被应答了」，比翻日志靠谱：
日志里 `POST /api` 的成败混在一起，而这里是个纯计数。

★ 顺序颠倒的代价（本脚本存在就是为了防这个）：站还没通就把名字写进 `--indexers`
  → 状态机把片子记成「在那站搜过了」并压上 14 天冷却，实际一次都没发出去。
  而「来了新站」的触发是**一次性**的，白烧一次就得再等一个周期。

顺带答另一个反复出现的问题：**`RATE_LIMITED` 到底是不是真的在限流？**
`indexer.status` 这一列会**留下过期不擦** —— 限流窗口过去之后它仍然写着
`RATE_LIMITED`，只看这个词会误判成"站点还在拦我们"。真正决定"能不能发请求"的
是 `retry_after`（epoch 毫秒）。本脚本把它换算成本地时间并直接判「有效 / 已过期」。

用法
----
    python scripts/check-indexer-timestamps.py                     # 一次性只读报告
    python scripts/check-indexer-timestamps.py --expect HDtime,HDFans,NanyangPT,BTSCHOOL
    python scripts/check-indexer-timestamps.py --wait              # 等下一批跑完再报

`--expect` 是第 ③ 步的闸门：名单里**每个**站都得有 ≥1 行才退出 0。
`--wait` 先读 `attempts.log` 末行做基线，等到出现新的 `exit=` 再取数（默认最多等 90 分钟）；
它**只读**，不加 `--wait` 就是纯快照。

退出码：0 = 闸门通过（或没给 --expect）；1 = 有 --expect 的站还没有行；2 = --wait 超时。

★ 全程只读，且**只打计数、索引器 id 与名字** —— `indexer` 表里的 `url` / `apikey`
  两列一律不取（那里面有全站共用的密钥）。
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sqlite3
import sys
import time
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 —— 老 python 没有 reconfigure，乱码也比崩了强
    pass

#: 默认 = NAS 上的生产 compose 目录（Windows 侧走 UNC）。
DEFAULT_COMPOSE = "//iSunker-DS423/docker_ssd/reseed"
#: 默认闸门名单 = drive-loop-nas.sh 的 --indexers 打算用的那四个站。
DEFAULT_EXPECT = "HDtime,HDFans,NanyangPT,BTSCHOOL"


def open_ro(db: pathlib.Path) -> sqlite3.Connection:
    """以只读方式打开 sqlite。

    ★ 必须 `file:////<host>/<path>?mode=ro`：写成 `file://<host>/...` 会被当成
      URI 的 authority 部分，sqlite 报 `invalid uri authority`（2026-09-12 踩过）。
    ★ 也**不要** `cp` 到本地再读 —— 那会丢掉 WAL 里还没 checkpoint 的改动，
      读到的是一个偏旧的快照（诊断一律直读原库）。
    """
    uri = "file:////" + db.as_posix().lstrip("/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=20)


def read_attempts(att: pathlib.Path) -> list[str]:
    return att.read_bytes().decode("utf-8", "replace").splitlines()


def fmt_ms(ms: int | None) -> tuple[str, str]:
    """epoch 毫秒 → (本地时间, 判定)。判定关心的是「窗口过没过」，不是那个词。"""
    if not ms:
        return "—", ""
    t = datetime.fromtimestamp(ms / 1000)
    left = (t - datetime.now()).total_seconds()
    if left <= 0:
        return t.strftime("%m-%d %H:%M:%S"), "已过期（不再拦）"
    m = int(left // 60)
    return t.strftime("%m-%d %H:%M:%S"), f"★ 有效，还剩 {m // 60}h{m % 60}m"


def snapshot(db: pathlib.Path) -> tuple[dict[str, str], dict[str, int], dict[str, tuple[str, str, str]]]:
    """返回 (id→名字, id→timestamp 行数, id→(status, retry_after 本地时间, 判定))。"""
    con = open_ro(db)
    try:
        cur = con.cursor()
        names = {str(r[0]): r[1] for r in cur.execute('SELECT id, name FROM "indexer"')}
        rows = {str(r[0]): r[1]
                for r in cur.execute('SELECT indexer_id, COUNT(*) FROM "timestamp" GROUP BY indexer_id')}
        state: dict[str, tuple[str, str, str]] = {}
        for iid, st, ra in cur.execute('SELECT id, status, retry_after FROM "indexer"'):
            when, verdict = fmt_ms(ra)
            state[str(iid)] = (st, when, verdict)
    finally:
        con.close()
    return names, rows, state


def report(db: pathlib.Path) -> int:
    names, rows, state = snapshot(db)
    print("== timestamp 各索引器行数（失败不记行 → 有行 = 真的搜出去过）==")
    for iid in sorted(names, key=int):
        st, when, verdict = state.get(iid, ("?", "—", ""))
        tail = f"  retry_after={when} {verdict}" if verdict else ""
        print(f"   id={iid} {str(names[iid]):<22} {rows.get(iid, 0):>5} 行"
              f"   status={st}{tail}")
    for iid in sorted(set(rows) - set(names), key=int):
        print(f"   id={iid} {'(indexer 表里没有这个 id)':<22} {rows[iid]:>5} 行")

    print("\n== 判读 ==")
    limited = [(i, state[i]) for i in state if state[i][0] == "RATE_LIMITED"]
    if not limited:
        print("   没有被标 RATE_LIMITED 的站。")
    else:
        active = [i for i, s in limited if s[2].startswith("★")]
        print(f"   标着 RATE_LIMITED 的有 {len(limited)} 个：{', '.join(names.get(i, i) for i, _ in limited)}")
        if active:
            print(f"   ★ 其中 {len(active)} 个的 retry_after **还没到** —— 这些是真的在被拦。")
        else:
            print("   ★ 但它们的 retry_after **全部已经过期** —— 是**陈旧标记**，不是限流。")
            print("     这个字段限流窗口过去后不会被擦掉，只看 status 会误判成'站点还在拦我们'。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="只读检查 cross-seed 索引器的 timestamp 行与限流状态")
    ap.add_argument("--compose", default=DEFAULT_COMPOSE, help=f"默认 {DEFAULT_COMPOSE}")
    ap.add_argument("--expect", default="",
                    help=f"闸门名单（逗号分隔）；默认不判。常用值：{DEFAULT_EXPECT}")
    ap.add_argument("--wait", action="store_true",
                    help="先等 drive-loop 跑完下一批（基线 = attempts.log 当前末行）")
    ap.add_argument("--minutes", type=int, default=90, help="--wait 最多等多少分钟（默认 90）")
    ap.add_argument("--interval", type=int, default=120, help="--wait 轮询间隔秒（默认 120）")
    args = ap.parse_args()

    compose = pathlib.Path(args.compose)
    db = compose / "cross-seed" / "cross-seed.db"
    att = compose / "drive-loop" / "attempts.log"
    expect = [x.strip() for x in args.expect.split(",") if x.strip()]

    if not db.is_file():
        print(f"[!!] 读不到 {db}")
        return 1

    if args.wait:
        if not att.is_file():
            print(f"[!!] --wait 需要 {att}，读不到")
            return 1
        lines = read_attempts(att)
        baseline = lines[-1] if lines else ""
        print(f"基线（attempts.log 末行）: {baseline}")
        print(f"等下一批结束（最多 {args.minutes} 分钟，每 {args.interval} 秒看一次）……")
        deadline = time.time() + args.minutes * 60
        while time.time() < deadline:
            time.sleep(args.interval)
            lines = read_attempts(att)
            if not lines or lines[-1] == baseline:
                print(f"[{datetime.now():%H:%M:%S}] 还没有新行")
                continue
            fresh = lines[lines.index(baseline) + 1:] if baseline in lines else lines
            exits = [l for l in fresh if " exit=" in l]
            if exits:
                print(f"[{datetime.now():%H:%M:%S}] ★ 批次结束：{exits[-1]}\n")
                break
            print(f"[{datetime.now():%H:%M:%S}] 批次进行中：{lines[-1]}")
        else:
            print(f"\n[!!] 超时：{args.minutes} 分钟内没等到新的批次结束。")
            return 2

    print(f"\n库  : {db}")
    print(f"现在: {datetime.now():%Y-%m-%d %H:%M:%S}\n")
    report(db)

    if not expect:
        return 0

    names, rows, _ = snapshot(db)
    # ★ 名字要**宽松**匹配：`--indexers` 那份名单里的站名与 cross-seed 自己注册进
    #   `indexer.name` 的字符串并不总是同一个 —— 实测 NanyangPT 在库里叫
    #   `NanyangPT (南洋)`。严格 `==` 会把一个**已经搜了 870 行**的站报成「不在表里」，
    #   也就是把一道通了的闸门报成没通（2026-09-12 本脚本第一版就是这么错的）。
    #   放宽的方向：精确命中优先；否则去掉 ` (` 后缀再比一次，但**要求唯一命中** ——
    #   含糊不清时宁可报「不在表里」让人来看，也不要赌一个。
    def lookup(n: str) -> tuple[str | None, str]:
        if n in names.values():
            return next(i for i, v in names.items() if v == n), n
        hits = [(i, v) for i, v in names.items() if v.split(" (")[0] == n]
        if len(hits) == 1:
            return hits[0][0], hits[0][1]
        return None, n

    print(f"\n== 闸门（第 ③ 步）—— 名单：{', '.join(expect)} ==")
    missing = []
    for n in expect:
        iid, real = lookup(n)
        if iid is not None and real != n:
            print(f"   （{n} 在库里叫 `{real}`）")
        if iid is None:
            print(f"   ⬜ {n:<22} 不在 indexer 表里 —— 说明容器还没见过它（.env 加了没重建？）")
            missing.append(n)
        elif rows.get(iid, 0) > 0:
            print(f"   ✅ {n:<22} {rows[iid]} 行 —— 真的搜出去过")
        else:
            print(f"   ⬜ {n:<22} 0 行 —— 还没搜成，**别改 --indexers**")
            missing.append(n)

    if missing:
        print(f"\n★ 还差 {len(missing)} 个站没有 timestamp 行 —— 闸门未开。")
        print("  （可能是这批没轮到它；再等一批。持续没有就查 Prowlarr 的 cookie / Test。）")
        return 1
    print("\n★★ 全部有行 —— 闸门已开，可以把 drive-loop-nas.sh 的 --indexers 改成这份名单了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
