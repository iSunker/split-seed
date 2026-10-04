#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 drive-loop 状态库里 `pack.farm_root` 的旧农场路径改成新的（一次性迁移）。

背景
----
2026-09-12 把 `reseed_farm` 收进 `reseed/` 父目录（SUMMARY §18）。`pack` 表里
有 **3 行** `farm_root` 指向旧路径，不改的话农场自检会指着一个不存在的目录。

★ `movie.path` 存的是**源大包**路径（605 行），**不受本次搬迁影响，不用动**。

★ 为什么写成脚本、而不是一条 `python3 -c`：这条命令在多行粘贴时会被终端折行
  并补上缩进，Python 直接 `IndentationError`。落成文件就绕开了引号与折行两重坑，
  NAS 上只要敲一行调用。

用法（**在 NAS 上跑**，需要写权限）
----
    sudo python3 fix-statedb-farm-root.py            # 只读预检（默认）
    sudo python3 fix-statedb-farm-root.py --apply    # 真改

退出码：0 = 符合预期；1 = 有意外（待改行数不是 3、或改完后仍有残留），此时别慌，
        把输出贴出来 —— 脚本**没有**做任何不可逆操作，只是 UPDATE 了 3 行的字符串。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 —— 老 python 没有 reconfigure，乱码也比崩了强
    pass

DEFAULT_DB = "/volume2/docker_ssd/reseed/drive-loop/hlink/state.db"
OLD = "/volume1/video/download/reseed_farm"
NEW = "/volume1/video/download/reseed/reseed_farm"

#: ★ 预期待改行数。2026-09-12 实测 pack 表共 3 行（三个包）。
#: 钉死它是有意的：数字对不上说明状态库不是我们以为的那个（或被别的迁移动过），
#: 那时候**应该停下来看一眼**，而不是"有几行改几行"。
EXPECTED = 3


def main() -> int:
    ap = argparse.ArgumentParser(description="改 pack.farm_root 的旧农场路径")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"默认 {DEFAULT_DB}")
    ap.add_argument("--apply", action="store_true", help="真改（不加就是只读预检）")
    ap.add_argument("--expect", type=int, default=EXPECTED,
                    help=f"预期待改行数（默认 {EXPECTED}）；对不上就退出 1")
    args = ap.parse_args()

    print(f"库   : {args.db}")
    print(f"旧   : {OLD}")
    print(f"新   : {NEW}")
    print(f"模式 : {'★ APPLY（会改）' if args.apply else '预检（只读）'}\n")

    c = sqlite3.connect(args.db)

    # --- 1) 现状 ----------------------------------------------------------
    packs = c.execute("SELECT name, farm_root FROM pack ORDER BY name").fetchall()
    print(f"== pack 表共 {len(packs)} 行 ==")
    for name, fr in packs:
        mark = "  ← 待改" if fr == OLD else ("  ✓ 已是新值" if fr == NEW else "  ? 第三种值")
        print(f"  {name:24s} {fr}{mark}")

    todo = [n for n, fr in packs if fr == OLD]
    other = [(n, fr) for n, fr in packs if fr not in (OLD, NEW)]
    print(f"\n待改 {len(todo)} 行（预期 {args.expect}）")

    if other:
        print(f"\n[!!] 有 {len(other)} 行的 farm_root 既不是旧值也不是新值，"
              f"本脚本不知道怎么处理：")
        for n, fr in other:
            print(f"     {n}  →  {fr}")
        print("     → 先搞清楚它们是什么再决定。**未做任何修改。**")
        return 1

    # ★ 顺序有意：先判"没事可做"，再判"数目对不上"。
    #   反过来的话，**成功跑完再跑一次**会报「待改 0 ≠ 预期 3」并退出 1 ——
    #   把一次正常的幂等复跑变成一声假警报。（2026-09-12 本地副本上实测踩到。）
    if not todo:
        print("\n没有要改的 —— 已经是新值了（幂等，重复跑安全）。")
        return 0

    if len(todo) != args.expect:
        print(f"\n[!!] 待改行数 {len(todo)} ≠ 预期 {args.expect}。")
        print("     → 钉死预期值是为了拦住'状态库不是我们以为的那个'这种情况。")
        print("       确认无误后可以： --expect {n}".format(n=len(todo)))
        print("       **未做任何修改。**")
        return 1

    if not args.apply:
        print("\n（预检结束，什么都没改。确认无误后加 --apply）")
        return 0

    # --- 2) 落地 ----------------------------------------------------------
    n = c.execute("UPDATE pack SET farm_root=? WHERE farm_root=?", (NEW, OLD)).rowcount
    c.commit()
    print(f"\n✓ 已 UPDATE {n} 行")

    # --- 3) 回读 ----------------------------------------------------------
    left = c.execute("SELECT COUNT(*) FROM pack WHERE farm_root=?", (OLD,)).fetchone()[0]
    now = c.execute("SELECT COUNT(*) FROM pack WHERE farm_root=?", (NEW,)).fetchone()[0]
    print(f"回读：旧值还剩 {left} 行（应为 0）；新值 {now} 行（应为 {len(packs)}）")

    if left:
        print("\n[!!] 还有残留旧值 —— 别继续，把输出贴出来。")
        return 1

    print("\n下一步：`sh build-farm.sh --verify` 退出码应为 0。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
