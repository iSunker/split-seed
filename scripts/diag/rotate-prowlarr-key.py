#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""#109 第 2 步的工具：把 NAS `.env` 里那条 Prowlarr key 换成新的。

改**两处**（它们必须是**同一条** key，见下）：
  1) 第 N 行  `PROWLARR_API_KEY=<新>`
  2) 第 M 行  `TORZNAB_URLS=http://prowlarr:9696/1/api?apikey=<新>,…`（4 段，**同一个值**）

★ 为什么必须一起改：TORZNAB 段里的 apikey 与 PROWLARR_API_KEY 是**同一条**
  （`torznab-keycheck.py` 的「键比」那一行会核对；2026-09-12 也用指纹核过）。
  只改一处 ⇒ 另一半 401。

安全闸（逐条对应踩过的坑）：
  · **默认 dry-run**，`--apply` 才写；
  · 只改**这两行**，其余行**逐字节不动**（含那份 .env 混着的行尾与游离 \\n）；
  · 写前**先备份**（带时间戳，与原文件同目录）；
  · 原子写（先写临时文件再 `os.replace`）—— ★ 直接 `open(p,'w')` 会先截断，
    中途断掉就是一个**空的 .env**；
  · 新 key **从文件读**（`--new-key-file`），**不经过命令行**（命令行会进 shell 历史/进程表）。

用法：
    python scripts/rotate-prowlarr-key.py --env <生产.env> --new-key-file <文件>          # dry-run
    python scripts/rotate-prowlarr-key.py --env <生产.env> --new-key-file <文件> --apply
"""
from __future__ import annotations

import argparse
import io
import os
import pathlib
import re
import shutil
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

DEFAULT_ENV = "//iSunker-DS423/docker_ssd/reseed/.env"


def mask(u: str) -> str:
    """把 apikey 的值掩掉（**任何** 24 位以上字母数字）。"""
    return re.sub(r"([?&]apikey=)[A-Za-z0-9]+", r"\1<redacted>", u)


def main() -> int:
    ap = argparse.ArgumentParser(description="把 .env 里那条 Prowlarr key 换成新的")
    ap.add_argument("--env", default=DEFAULT_ENV)
    ap.add_argument("--new-key-file", required=True, help="只含新 key 一行的文件")
    ap.add_argument("--apply", action="store_true", help="真写（不加就是 dry-run）")
    args = ap.parse_args()

    env_p = pathlib.Path(args.env)
    key_p = pathlib.Path(args.new_key_file)
    if not env_p.is_file():
        print(f"[!!] 读不到 .env：{env_p}"); return 2
    if not key_p.is_file():
        print(f"[!!] 读不到新 key 文件：{key_p}"); return 2

    new = key_p.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{16,64}", new):
        # ★ 只报形状，不报值
        print(f"[!!] 新 key 形状不像 key（长度 {len(new)}，只允许字母数字 16–64 位）"); return 2

    # ★ 行分隔符处理：这份 .env 混着 \\n 与 \\r\\n（#109 卡里记过）
    raw = env_p.read_bytes()
    text = raw.decode("utf-8")
    lines = text.split("\n")

    idx_key = [i for i, l in enumerate(lines) if l.rstrip("\r").startswith("PROWLARR_API_KEY=")]
    idx_tz = [i for i, l in enumerate(lines) if l.rstrip("\r").startswith("TORZNAB_URLS=")]
    if len(idx_key) != 1:
        print(f"[!!] `PROWLARR_API_KEY=` 行有 {len(idx_key)} 条，预期恰好 1 条 —— 停下"); return 2
    if len(idx_tz) != 1:
        print(f"[!!] `TORZNAB_URLS=` 行有 {len(idx_tz)} 条，预期恰好 1 条 —— 停下"); return 2

    ik, it = idx_key[0], idx_tz[0]

    # --- ① PROWLARR_API_KEY ---
    old_key_line = lines[ik]
    tail_k = "\r" if old_key_line.endswith("\r") else ""
    lines[ik] = f"PROWLARR_API_KEY={new}{tail_k}"

    # --- ② TORZNAB_URLS 里的 4 段 apikey ---
    old_tz = lines[it]
    tail_t = "\r" if old_tz.endswith("\r") else ""
    body = old_tz.rstrip("\r")[len("TORZNAB_URLS="):]
    n_before = len(re.findall(r"([?&]apikey=)[A-Za-z0-9]+", body))
    new_body = re.sub(r"([?&]apikey=)[A-Za-z0-9]+", r"\g<1>" + new, body)
    lines[it] = "TORZNAB_URLS=" + new_body + tail_t

    print(f"文件: {env_p}")
    print(f"  ① 第 {ik+1} 行  PROWLARR_API_KEY  →  <新 key>")
    print(f"  ② 第 {it+1} 行  TORZNAB_URLS     →  替换 {n_before} 段 apikey")
    print(f"     替换后（脱敏）: {mask(lines[it].rstrip())}")
    if n_before != 4:
        print(f"  ★★ 注意：TORZNAB 段数是 {n_before}，卡里记的是 4 —— 请确认这是预期的")

    if not args.apply:
        print("\n( dry-run —— 未写任何东西 )")
        return 0

    bak = env_p.with_name(env_p.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(env_p, bak)
    tmp = env_p.with_name(env_p.name + ".tmp-rot")
    tmp.write_bytes("\n".join(lines).encode("utf-8"))
    os.replace(tmp, env_p)
    print(f"\n★ 已写。备份: {bak}")
    print("★ 下一步：第 3 步（cross-seed.db 的 apikey 列），**然后**才 --force-recreate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
