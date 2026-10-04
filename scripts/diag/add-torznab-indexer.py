#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""往生产 `.env` 的 `TORZNAB_URLS` 里补一个 Prowlarr 索引器条目。

为什么不能用 gen-nas-env-update.py
---------------------------------
`scripts/gen-nas-env-update.py` 生成的脚本**只带 `DATA_DIRS` 和 `LINK_DIR` 两个键**，
其余行是从 NAS 现存的 `.env` **原样复制**过去的 —— 所以它**结构上就改不了
`TORZNAB_URLS`**，那个脚本里还有一道"除这两个键外逐字节不变"的安全闸，正是拦这个的。

而本地 `reseed-toolkit/.env` 是**脱敏模板**：apikey 是 16 位占位符、还缺
`PROWLARR_API_KEY` / `PROWLARR_URL` 两个键（2026-09-12 实测）。拿它去覆盖生产
会把真密钥冲掉 → cross-seed 带假密钥重启、全线 401。所以这个键**只能在生产
`.env` 上就地改**，本脚本就是干这个的。

★ 密钥不经过人眼：新条目的 apikey 是**从同一个文件里已有条目的值抄过来的**
  （Prowlarr 的 apikey 是**全站共用**的，只按索引器 id 分路径 —— 2026-09-12 用
  指纹核对过：`PROWLARR_API_KEY` 与 `TORZNAB_URLS` 里的值同一个）。所以既不用
  手敲，也不会被打印。打印出来的一律是 `apikey=<redacted>`。

用法
----
    python scripts/add-torznab-indexer.py                 # 预检（默认，什么都不改）
    python scripts/add-torznab-indexer.py --apply         # 真改（自动备份）
    python scripts/add-torznab-indexer.py --id 1 --apply  # 指定索引器（可重复/逗号分隔）

    # —— 下站（换站替换时用）——
    python scripts/add-torznab-indexer.py --remove 3            # 预检：把索引器 3 摘掉
    python scripts/add-torznab-indexer.py --remove 3 --apply    # 真摘

★ 「下站」不是「上站」的反向操作，顺序别搞反（见 drive-loop-nas.sh 里「加站的正确顺序」）：
    上站：① Prowlarr 启用 + Test ② .env 加 `/N/api` ③ 确认 timestamp 出现该站行 ④ 才改 --indexers
    下站：① **先**把 --indexers 里的那个站去掉并 deploy（状态机不再排它的期）
          ② 再从 .env 摘掉 `/N/api` ③ 重建容器
    理由：`.env` 先摘、容器先重建的话，状态机那边还以为这个站在，
    于是「每部片都还没在新站搜过」的判定会一直成立 —— 白白重排、白烧额度。
    反过来先摘 --indexers，最坏只是容器多挂一个用不到的索引器（不发查询）。

    另：**摘掉最后一个条目会被拒绝**（`TORZNAB_URLS=` 空 = cross-seed 一个站都不搜，
    那是个"看起来正常"的静默故障）。真要有意清空，加 --allow-empty。

幂等：已经在了（或已经不在）就不动，退出码 0。改完**必须重建容器**才生效：
    cd <compose 目录> && sudo docker compose up -d --force-recreate cross-seed

退出码：0 = 符合预期（含"本来就有、不用改"）；1 = 有意外（见输出，未做修改）。
"""

from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import sys
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 —— 老 python 没有 reconfigure，乱码也比崩了强
    pass

#: 默认目标 = NAS 上的生产 `.env`。从 NAS 本机跑就换成本地路径那个写法。
DEFAULT_ENV = "//iSunker-DS423/docker_ssd/reseed/.env"

#: 默认要确保存在的索引器。1 = HDtime（2026-09-12 换了 cookie 后加回来）。
DEFAULT_IDS = "1"

URL_RE = re.compile(r"^(?P<base>https?://[^/]+)/(?P<id>\d+)/api(?P<query>\?.*)?$")
KEY_RE = re.compile(r"apikey=([^,&]+)")


def redact(u: str) -> str:
    return KEY_RE.sub("apikey=<redacted>", u)


def split_urls(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def readback_urls(p: pathlib.Path) -> list[str]:
    """回读 TORZNAB_URLS 的值。

    ★ 必须 read_bytes 再 decode，**不能用 read_text**：read_text 默认做换行翻译，
      文件里的 `\\r\\n` 会被吃成 `\\n`，于是按 eol 切分一行都切不出来
      （2026-09-12 实测把回读崩成 StopIteration —— 而且是在**写完之后**崩的，
       最坏的时刻）。同一个 CR 坑在 build-farm.sh 里踩过一次，这是第二次。
    """
    text = p.read_bytes().decode("utf-8")
    line = next(l for l in text.split("\n")
                if l.rstrip("\r").startswith("TORZNAB_URLS="))
    return split_urls(line.rstrip("\r")[len("TORZNAB_URLS="):])


def commit(p: pathlib.Path, parts: list[str], i: int, value: str) -> pathlib.Path:
    """备份 → 写临时文件 → 原子替换。返回备份路径。

    ★ 不能直接 open(path,'w') —— 那会先截断再写，中途断掉就是一个**空的 .env**
      （cross-seed 起来后全线没有密钥）。写临时文件再 os.replace 是原子的。
    """
    bak = p.with_name(p.name + ".bak." + datetime.now().strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(p, bak)
    print(f"\n[1/2] 备份完成 → {bak.name}")
    parts[i] = value
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes("\n".join(parts).encode("utf-8"))
    tmp.replace(p)
    print(f"[2/2] 已写入（回滚: cp -p {bak.name} {p.name}）")
    return bak


def say_next_steps() -> None:
    print("\n下一步（必须做，否则不生效）—— 在 NAS 上：")
    print("  cd /volume2/docker_ssd/reseed && "
          "sudo docker compose up -d --force-recreate cross-seed")
    print("  ★ 同时确认 drive-loop 的 `--indexers` 里有（或已去掉）这个站，否则会触发"
          "「索引器自检」告警（那是**设计如此**，不是故障）。")


def main() -> int:
    ap = argparse.ArgumentParser(description="往生产 .env 的 TORZNAB_URLS 补索引器条目")
    ap.add_argument("--env", default=DEFAULT_ENV, help=f"默认 {DEFAULT_ENV}")
    ap.add_argument("--id", default=DEFAULT_IDS,
                    help=f"要确保存在的索引器 id，逗号分隔（默认 {DEFAULT_IDS}）")
    ap.add_argument("--apply", action="store_true", help="真改（不加就是只读预检）")
    ap.add_argument("--remove", default="",
                    help="要**摘掉**的索引器 id，逗号分隔（与 --id 互斥）")
    ap.add_argument("--allow-empty", action="store_true",
                    help="--remove 允许摘到一条不剩（默认拒绝，见文件头）")
    args = ap.parse_args()

    if args.remove and args.id != DEFAULT_IDS:
        print("[!!] --remove 与 --id 不能同时用（一次只做一件事）。")
        return 1
    remove = [x.strip() for x in args.remove.split(",") if x.strip()]
    want = [x.strip() for x in args.id.split(",") if x.strip()]

    p = pathlib.Path(args.env)
    print(f"目标 : {p}")
    if remove:
        print(f"要摘掉   : 索引器 {', '.join(remove)}\n")
    else:
        print(f"要确保存在: 索引器 {', '.join(want)}\n")
    if not p.is_file():
        print(f"[!!] 读不到 {p}")
        return 1

    # ★ 二进制读 + 按 `\n` 切，**绝不按 `\r\n` 切**。
    #   `.env` 的行分隔符是 `\n`，`\r` 只是某些行末尾多带的一个字符 ——
    #   这份生产 .env 正**混着**：41 行带尾 CR、2 行不带（2026-09-12 实测）。
    #   ★ 按 `\r\n` 切再按 `\r\n` 接，会**把目标行里多出来的 `\n` 一起吃掉**：
    #     实测 TORZNAB_URLS 那行结尾原本是 `\n\r\n`（多一个游离 LF），被静默
    #     规范化成 `\r\n`。功能上无害甚至更干净，但"写回保真"这个承诺就不成立了
    #     —— 一个装着真密钥的文件，不该有任何未经说明的字节改动。
    #   → 正确做法：切 `\n`，给每行记住它自己有没有尾 `\r`，原样拼回去。
    #   （build-farm.sh 里那段 `tr -d '\r'` 的血泪注释是同一个 CR 坑的另一面：
    #     按逗号拆值时最后一条会带上 CR，症状是"只有最后一条路径不存在"。）
    raw = p.read_bytes()
    text = raw.decode("utf-8")
    n_crlf = raw.count(b"\r\n")
    n_lf = raw.count(b"\n") - n_crlf
    print(f"行尾 : {n_crlf} 行带尾 CR、{n_lf} 行不带（原样保留）")

    parts = text.split("\n")
    idx = [k for k, l in enumerate(parts) if l.rstrip("\r").startswith("TORZNAB_URLS=")]
    if len(idx) != 1:
        print(f"[!!] `TORZNAB_URLS=` 行有 {len(idx)} 条，预期恰好 1 条 —— 停下来看一眼。")
        return 1
    i = idx[0]
    tail = "\r" if parts[i].endswith("\r") else ""
    urls = split_urls(parts[i].rstrip("\r")[len("TORZNAB_URLS="):])

    # ---- 1) 现状 ---------------------------------------------------------
    have: dict[str, str] = {}
    bad: list[str] = []
    for u in urls:
        m = URL_RE.match(u)
        if not m:
            bad.append(u)
            continue
        have[m.group("id")] = u
    print(f"\n== 现有 {len(urls)} 条 ==")
    for u in urls:
        print(f"   {redact(u)}")
    if bad:
        print(f"\n[!!] 有 {len(bad)} 条不符合 `<base>/<id>/api[?query]` 的形状，"
              f"本脚本不知道怎么处理：")
        for u in bad:
            print(f"     {redact(u)}")
        print("     **未做任何修改。**")
        return 1

    # ---- 1b) 下站模式：摘条目 ---------------------------------------------
    # ★ 下站**不是**上站的反向操作，别先摘 .env —— 顺序见文件头「下站」那段：
    #   先摘 drive-loop 的 --indexers，再摘这里，最后重建容器。
    if remove:
        present = [i_ for i_ in remove if i_ in have]
        if not present:
            print(f"\n没有要摘的 —— {', '.join(remove)} 本来就不在"
                  f"（幂等，重复跑安全）。")
            return 0
        kept = [u for u in urls if URL_RE.match(u).group("id") not in set(present)]
        # ★ 安全闸：摘到一条不剩 = cross-seed 一个站都不搜。
        #   它**不报错**，只表现成"这批没有匹配"—— 同 §16.2.1 / §18.9.1 那一族：
        #   **失败的形态是"看起来正常"**。宁可停下让人看一眼。
        if not kept and not args.allow_empty:
            print(f"\n[!!] 摘掉 {', '.join(present)} 之后**一条不剩**了 —— 拒绝执行。")
            print("     `TORZNAB_URLS=` 为空 = cross-seed 一个站都不搜，"
                  "而它只会静默地什么都搜不到。")
            print("     确实要清空，加 --allow-empty。")
            return 1
        print(f"\n== 将要摘掉 {len(present)} 条 ==")
        for i_ in present:
            print(f"   {redact(have[i_])}")
        print(f"\n   摘后剩 {len(kept)} 条：")
        for u in kept:
            print(f"   {redact(u)}")

        if not args.apply:
            print("\n（预检结束，什么都没改。确认无误后加 --apply）")
            return 0

        commit(p, parts, i, "TORZNAB_URLS=" + ",".join(kept) + tail)

        back = readback_urls(p)
        got = {URL_RE.match(u).group("id") for u in back if URL_RE.match(u)}
        print(f"\n回读：共 {len(back)} 条，索引器 id = {', '.join(sorted(got, key=int))}")
        left = [i_ for i_ in remove if i_ in got]
        if left:
            print(f"[!!] 还有没摘掉的: {', '.join(left)}")
            return 1
        say_next_steps()
        return 0

    # ---- 2) 密钥从哪来：同文件里已有条目的值（绝不打印）--------------------
    keys = {m.group(1) for u in urls if (m := KEY_RE.search(u))}
    if len(keys) != 1:
        print(f"\n[!!] 现有条目的 apikey 有 {len(keys)} 种不同取值，预期恰好 1 种"
              f"（Prowlarr 的 key 是全站共用的；0 种 = 没有可抄的）。"
              f"**未做任何修改。**")
        return 1
    base = URL_RE.match(urls[0]).group("base")

    todo = [i_ for i_ in want if i_ not in have]
    if not todo:
        print(f"\n没有要加的 —— {', '.join(want)} 都已经在 TORZNAB_URLS 里了"
              f"（幂等，重复跑安全）。")
        return 0

    key = keys.pop()
    new_entries = [f"{base}/{i_}/api?apikey={key}" for i_ in todo]

    print(f"\n== 将要追加 {len(todo)} 条 ==")
    for u in new_entries:
        print(f"   {redact(u)}")
    print(f"\n   （apikey 从现有条目原样抄来，与 PROWLARR_API_KEY 同一个值；"
          f"base={base}）")

    if not args.apply:
        print("\n（预检结束，什么都没改。确认无误后加 --apply）")
        return 0

    # ---- 3) 落地：备份 + 临时文件 + 原子替换 -------------------------------
    commit(p, parts, i, "TORZNAB_URLS=" + ",".join(urls + new_entries) + tail)

    # ---- 4) 回读 ---------------------------------------------------------
    back = readback_urls(p)
    got = {URL_RE.match(u).group("id") for u in back if URL_RE.match(u)}
    print(f"\n回读：共 {len(back)} 条，索引器 id = {', '.join(sorted(got, key=int))}")
    missing = [i_ for i_ in want if i_ not in got]
    if missing:
        print(f"[!!] 还有没加上的: {', '.join(missing)}")
        return 1

    say_next_steps()
    return 0


if __name__ == "__main__":
    sys.exit(main())
