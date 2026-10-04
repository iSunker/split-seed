#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对账 b − c —— **薄壳**：判据本体在 `orchestrator.state.resolve_found_lines()`。

b = `_RE_FOUND` 命中行数（`audit-found-lines.py` 已对平：a − b = 0）
c = 其中组5（searchee **路径**）走生产代码能归到某个单片的行数

★★ 口径 —— 本文件存在的**一半**理由（2026-09-12 加）
------------------------------------------------------
同一批 Found 行，按"几个包一起试"还是"一次一个包"去归置，`other_pack`
是**两个完全不同的数**：

  · **全量口径**（所有包一起试、命中即停）：`other_pack` 按构造接近 0。
    它回答的是「这条 searchee 是不是**某个**包的」。
  · **生产口径**（一次一个包 —— `drive-loop.py` 的 `S.sync_pack(st, pack,
    crossseed_db=<同一个库>)` 就是这么调的）：三个包**共用一个 `farm_root`**，
    所以对任一包来说，别的包的 searchee 天然就是 `other_pack`。
    实测 **dc-collection 865 / frds 146 / mbf 1011** —— 恒非零、大体恒定。

★ 出过的事：全量口径报出的 `other_pack = 0` 被念成"干净"，而生产口径下根本
  不是这个数。这跟 `NanyangPT` 与 `NanyangPT (南洋)` 是**同一个形状** ——
  一个名字盖了两种模型。所以下面每个数都**绑定自己的口径名**，
  不出现裸的 `other_pack`，也不合并成一个"总对账"。
★ 两个口径**都报**，这是本脚本的硬要求：只报一个，读数的人就无从知道
  自己看的是哪一个。

★ 判据**只有一份**：本文件原先自己重写了一遍 roots/dpaths/farm 的拼装逻辑
  （"用我的模型去核对生产模型" = 循环论证）。现在走
  `orchestrator.state.pack_contexts()` + 生产自己的 `_resolve_searchee_to_pack()`
  —— 与 `drive-loop.py` 在 NAS 上跑的是**同一个函数**。

只读：库用 `mode=ro` 拿不到（UNC 不支持 file:// authority），改用
  **裸 UNC 路径 + `PRAGMA query_only = ON`**（就是生产 `_open_csdb()` 那套）。
  已确认库旁没有 state.db-wal / -shm / -journal（journal_mode=delete，事务干净），
  所以直读不会漏任何已提交事务。

用法
----
    python scripts/audit-found-resolve.py

全程只读；不写任何本地文件（连 `state.db` 都不碰，见 `ro_store()`）。
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

import orchestrator.state as S          # noqa: E402

# ★ 输出强制 UTF-8：GBK 控制台下会在中途炸掉，
#   而**已经过的打印看着全是 ok** —— 极易误判成逻辑坏了。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

NAS = pathlib.Path(r"//iSunker-DS423/docker_ssd/reseed")
LOG = NAS / "cross-seed/logs/info.current.log"
DB = NAS / "drive-loop/hlink/state.db"

B_PREV = 1011          # 2026-09-12 实测的 b（audit-found-lines.py 报出），仅作参照
PROD_PREV = {"dc-collection": 865, "frds-top250-2024": 146, "mbf": 1011}   # 同日实测


def open_ro(p: pathlib.Path) -> sqlite3.Connection:
    """只读打开 —— 库里绝不放行任何写（query_only 是硬闸）。"""
    con = sqlite3.connect(str(p))                  # 裸路径：UNC 只有这一种开法能成
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")          # ★ 硬闸：本连接从此拒绝任何写
    if not con.execute("PRAGMA query_only").fetchone()[0]:
        raise RuntimeError("query_only 没设上 —— 拒绝继续")
    return con


def ro_store(con: sqlite3.Connection) -> "S.StateStore":
    """用**真的** StateStore 方法 + 只读连接 —— 不落任何本地文件。

    `object.__new__` 绕过 `__init__`：那条路会 mkdir + executescript + commit，
    对着 UNC 上的**生产库**跑它就等于写库。这里只借方法，不借构造。
    """
    st = object.__new__(S.StateStore)
    st.path = pathlib.Path("<ro>")
    st.con = con
    return st


def main() -> int:
    if not LOG.is_file() or not DB.is_file():
        print(f"✗ 缺文件: log={LOG.is_file()} db={DB.is_file()}", file=sys.stderr)
        return 2

    con = open_ro(DB)
    store = ro_store(con)

    # ── 正向控制①：先证明"这套解析真的能解析"，否则下面的 0 什么都不是 ──
    ok = True
    ctx = S.pack_contexts(store)
    for pk, c in sorted(ctx.items()):
        print(f"   {pk}: roots={len(c.roots)} 单片={len(c.dirs)} "
              f"dpaths={len(c.dpaths)} farm={c.farm or '(未登记)'}")
    probe = None
    for pk, c in sorted(ctx.items()):
        for mp in c.dpaths:
            probe = (pk, mp) + S._resolve_one(mp, c)
            break
        if probe:
            break
    if probe is None:
        print("✗ 控制①失败：库里一条 movie.path 都取不到 —— 下面所有 0 都不可信")
        ok = False
    elif probe[2] is None:
        print(f"✗ 控制①失败：拿库**自己**的路径 {probe[1][:90]!r} 去解析，"
              f"居然归不到单片（belong={probe[3]}）—— 解析链是坏的")
        ok = False
    else:
        print(f"✓ 控制①：库自带路径可归片  {probe[1][:60]}… → {probe[2]!r}"
              f"（belong={probe[3]}）")

    text = LOG.read_text(encoding="utf-8", errors="replace")
    r = S.resolve_found_lines(text, store)     # ★ 判据本体，与 drive-loop 同一份

    print()
    print("━" * 68)
    print("  ★ 口径：下面【全量】表 = 所有包一起试、命中即停。")
    print("     它与生产口径（sync_pack **一次一个包**）**不可比** —— 同一批行，")
    print("     两边的 other_pack 完全不是一个数。两张表都列在下面，别混着念。")
    print("━" * 68)
    print()
    print("【全量口径】所有包一起试、命中即停")
    print(f"   b  Found 行                 : {r.b}   （参照 {B_PREV}，"
          f"差 {r.b - B_PREV} —— 日志是滚动的，差不为 0 正常）")
    print(f"      组5 落在农场根下         : {r.farm_lines}"
          f"   ← ★ 这一格恒为 0 时，下面那个 b−c 覆盖不到农场防线")
    print(f"      组5 是原路径             : {r.orig_lines}")
    print(f"   c  in_pack（真归到单片）    : {r.c}")
    print(f"   ★ 全量 b − c                : {r.delta}   （在这个口径下 = other_pack + unresolved）")
    for k in ("other_pack", "unresolved", "in_pack"):
        print(f"      [全量] {k:<12} {r.belong_all.get(k, 0)}")
    for k, v in r.samples.items():
        if k != "in_pack":
            print(f"      ── [全量] {k} 样本 ──")
            for s in v:
                print(f"         {s}")

    print()
    print("【生产口径】一次一个包（= drive-loop 里 sync_pack 的调法）")
    print("   ★ 三包共用一个 farm_root，所以对任一包来说，**别的包的 searchee")
    print("     必然落到 other_pack** —— 这个数恒非零、大体恒定，本身没有信息量。")
    print("     要看「真丢了什么」，判据是 unclaimed（全场无人认领），不是这一格。")
    for pk in sorted(r.belong_prod):
        d = r.belong_prod[pk]
        prev = PROD_PREV.get(pk)
        note = f"   （同日实测 {prev}）" if prev is not None else ""
        print(f"      {pk:<24} [生产] in_pack={d.get('in_pack', 0):>4}"
              f"  other_pack={d.get('other_pack', 0):>4}"
              f"  unresolved={d.get('unresolved', 0):>4}{note}")

    if ok and r.c == 0:
        print()
        print("★ 控制②失败：c=0，但控制①证明解析链是通的 —— "
              "0 说明的是**日志里的路径一条都不是本库的路径**，不是干净")
        ok = False
    print()
    print("✓ 控制全过 —— 上面的数可信" if ok else "★ 控制没过：不要把这些数当结论")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
