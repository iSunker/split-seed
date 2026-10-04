#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对账 a − b —— **薄壳**：判据本体在 `orchestrator.state.count_found_lines()`。

a = 用**六个独立字面量的合取**描述的「目标消息形状」命中行数
b = 生产代码 `orchestrator.state._RE_FOUND` 命中行数
期望 a − b = 0：形状判据和正则判据说的是同一件事。

★ 为什么这里是薄壳（2026-09-12 改）
-----------------------------------
判据本体原先写在本文件里 —— 而 `drive-loop.py`（跑在 **NAS 宿主机**上）也要
跑同一条判据，本文件却**不在 NAS 上**（`check-deploy-drift.py` 的 LOCAL_ONLY：
"在 Windows 上跑"；`DEPLOY_MANIFEST` 里没有它），而且写死的
`//iSunker-DS423/...` 在 NAS 上不存在。

于是判据搬进 `orchestrator/state.py` —— 它被部署**两次**（构建上下文 +
`drive-loop/orchestrator/`），是两侧**唯一都能到达**的地方。
★ 判据**只有一份**：抄一份不是重复劳动，是**把被测对象复制成判据**
  ——正则改了、副本照旧报绿。本文件现在核的就是生产在核的那一个函数。

★ 第一版的教训（`count_found_lines` 的注释里也写着）：
  拿 `] Found ` 当基线 → 数到 2200，a−b 报了 1189 的**假差**，
  因为那个字面量同时吃到 `Found 0 torrents for {`（662 行）与
  `Found N torrent file(s) to inject`（527 行）。
  基线**既不能带锚点**（那等于把被测正则抄一遍，属于循环论证），
  **也不能太松**（把别的消息吃进来）。

用法
----
    python scripts/audit-found-lines.py

全程只读（只 open 日志），不碰库、不碰 NAS 的写路径。
日志是**滚动**的，两次跑的窗口不同 → 绝对数会变，看的是 a − b 这个差。
"""
from __future__ import annotations

import pathlib
import re
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

LOG = pathlib.Path(
    r"//iSunker-DS423/docker_ssd/reseed"
    r"/cross-seed/logs/info.current.log")


def redact(s: str) -> str:
    s = re.sub(r"(apikey=)[^,&\s)\]]+", r"\1<redacted>", s, flags=re.I)
    s = re.sub(r"(passkey=)[^,&\s)\]]+", r"\1<redacted>", s, flags=re.I)
    return s


def main() -> int:
    if not LOG.is_file():
        print(f"✗ 日志不可达: {LOG}", file=sys.stderr)
        return 2

    text = LOG.read_text(encoding="utf-8", errors="replace")
    r = S.count_found_lines(text)          # ★ 生产那一份判据，不另抄

    print(f"日志 : {LOG.name}   总行数 {r.total_lines}")
    print()
    print("── 基线的每个字面量各自命中多少行（看是哪个词在收窄）──")
    for label, _ in S.FOUND_LITS:
        print(f"   {label:<24} {r.lit_counts.get(label, 0):>6}")
    print(f"   ★ 合取（目标形状）      {r.a:>6}")
    print()
    print(f"   b  _RE_FOUND 命中        {r.b:>6}")
    print(f"   ★ a − b                  {r.delta:>6}   （期望 0）")
    print()
    print(f"   组 3（站名）带空格/括号  {r.group3_odd:>6}  ← 09-12 修掉的那类，"
          f">0 说明修法在生产里真的被走到")
    print(f"   组 4（判定）的值         "
          f"{sorted(r.group4.items(), key=lambda kv: -kv[1])}")
    print(f"     ★ 组 1 = searchee 名、组 3 = 站名、组 4 = 判定（MATCH/_PARTIAL/"
          f"_SIZE_ONLY）、组 5 = searchee **路径**")
    print()
    print("── 被 L1 吃到、但不是目标形状的**其它消息**（应当在这里，不该算进 a）──")
    for k, v in sorted(r.other_found.items(), key=lambda kv: -kv[1])[:6]:
        print(f"   {v:>6}  {k}")
    if r.unparsed:
        print()
        print("── ★ 目标形状但 _RE_FOUND 没吃下的行 ──")
        for s in r.unparsed:
            print(f"   {redact(s)[:190]}")

    # ── 正向控制：没有它们，上面的 0 既可能是"没事"也可能是"没跑" ──
    print()
    ok = True
    if r.total_lines == 0:
        print("✗ 控制①失败：读了 0 行 —— 日志路径/编码不对，下面所有数都不可信")
        ok = False
    if r.lit_counts.get(S.L1_LABEL, 0) == 0:
        print("✗ 控制②失败：基线 L1 一行都没数到 —— 但历史版本数到过 2200，"
              "说明匹配逻辑坏了，0 是假的")
        ok = False
    if r.a == 0:
        print("✗ 控制③失败：目标形状 0 行 —— 日志里连一条 Found 都没有的话，"
              "a−b=0 是**空转**，不是干净")
        ok = False
    print("✓ 三道正向控制都过（读到了行、基线数到了、目标形状非空）—— 上面的数可信"
          if ok else "★ 控制没过：**不要**把上面的 0 当成干净")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
