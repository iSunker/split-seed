#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「新大包发现」—— **只读**扫描 NAS，报出**可能是大包、但还没登记**的目录。

★★ 定位（2026-09-20 立，用户问「怎么自动扩展别的大包呢？」）
  它**只做三件事**：① 扫 ② 报 ③ 把该跑的命令**拟好给人看**。
  ★★ **它绝不自动改 `PACKS_DEFAULT`** —— 三条硬理由（引自 `scripts/drive-loop.py:103-132`）：

    ① **那是产品决定，不是机械判断**：原文「它**不是**一行等着被消除的代码债，
       **是一个产品决定**」。
    ② **加包会稀释其他包的轮换频率**：原文「名单 2→3 个，dc/frds 各自的
       **轮换频率从 1/2 掉到 1/3**」。
    ③ ★★ **"找得到" ≠ "搜得到"**：`mbf` 在 `unclaimed`/`report`/`trend` 上**全绿**，
       而实测 **`Found 0 torrents`**。
     ⇒ 自动加包会把「**一个不产出放量的包伪装成正常**」（`§18.18` 那个形状）。

★ **为什么判据是"直接子目录数 ≥ N"**：大包的定义就是「一个目录里装着**很多分集/分片**」。
  单片（一个目录=一部片）**不是包**。所以 `子目录数` 是最接近定义的可测信号。
  ★★ 但它**只是信号，不是结论** —— 实测（2026-09-20）7 个候选里**至少 2 个是假阳性**：
    · `The Beatles - Discography +` ⇒ 那是**音乐**（子目录是专辑，不是剧集）
    · `StarRupture-InsaneRamZes` ⇒ 那是**游戏**（含 `_crack` / `_extras`）
  ⇒ 所以本脚本**只报信号 + 让人自己判**，**不替人下"这是包"的结论**。

用法：
    python scripts/diag/find-packs.py                  # 只读扫描并报告
    python scripts/diag/find-packs.py --min 10         # 门槛子目录数（默认 5）
    python scripts/diag/find-packs.py --emit-cmds      # 额外打印"怎么加"的命令草稿

退出码：0 = 扫到了（哪怕 0 个候选）；2 = 读不到 NAS。

★ 安全（与 `qb-census-savepath.py` / `04_probes.py` 同一形状）：
  **只读**（`query_only` 开库、UNC 只列目录）、**不写任何东西**、
  **只报目录名与计数**（不打印文件内容、不碰凭据）。
"""
import argparse
import pathlib
import sqlite3
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

NAS = "//iSunker-DS423"
COMPOSE = "%s/docker_ssd/reseed" % NAS
DB_UNC = COMPOSE + "/drive-loop/hlink/state.db"

#: 大包通常住在这两处（照 `.env.example` 的 `DATA_DIRS` 形状）。
#: ★ 只扫这两个 —— **不递归全盘**：那既慢，又会把别人的目录也算进来。
SCAN_BASES = ("video/download/movies", "video/download/TV")


def registered():
    """已登记的包根（只读生产库）+ 已在 `.env` 声明的根。返回 (集合, 错误串)。"""
    roots = set()
    try:
        con = sqlite3.connect(DB_UNC.replace("\\", "/"), timeout=8)
        con.execute("PRAGMA query_only=1")           # ★ 只读，与 04_probes 同一做法
        try:
            for (r,) in con.execute("SELECT root FROM pack WHERE root IS NOT NULL"):
                roots.add(r)
        finally:
            con.close()
    except Exception as e:                            # noqa: BLE001
        return roots, "%s: %s" % (type(e).__name__, e)

    # `.env.example` 里的 DATA_DIRS / FARM_SOURCES 也算"已声明"
    for f in (".env", ".env.example"):
        p = pathlib.Path(__file__).resolve().parents[2] / f
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() not in ("DATA_DIRS", "FARM_SOURCES"):
                continue
            for item in v.split(","):
                item = item.strip()
                if item:
                    roots.add(item)
    return roots, None


def scan(min_subs):
    """扫出「直接子目录数 ≥ min_subs」的目录。★ 只列目录名，不读内容。"""
    out = []
    for base in SCAN_BASES:
        try:
            b = pathlib.Path("%s/%s" % (NAS, base))
            entries = list(b.iterdir())
        except Exception as e:                        # noqa: BLE001
            out.append(("ERR", base, "%s: %s" % (type(e).__name__, e)))
            continue
        for d in entries:
            try:
                if not d.is_dir():
                    continue
                sub = [x for x in d.iterdir() if x.is_dir()]
            except Exception:                         # noqa: BLE001
                continue
            if len(sub) >= min_subs:
                # ★ 顺带记"有没有散文件" —— 农场口径是 `子项 = 目录 + 散文件`（`summary/10`）
                files = sum(1 for x in d.iterdir() if x.is_file())
                out.append(("OK", "/volume1/%s/%s" % (base, d.name), (len(sub), files)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=int, default=5, help="直接子目录数门槛（默认 5）")
    ap.add_argument("--emit-cmds", action="store_true", help="打印「怎么加」的命令草稿")
    a = ap.parse_args()

    known, err = registered()
    if err:
        print("[!] 读不到生产库 ⇒ **判不了**「已登记 / 未登记」（别把读不到念成「没有」）：")
        print("    %s" % err)
    rows = scan(a.min)

    errs = [r for r in rows if r[0] == "ERR"]
    cand = [r for r in rows if r[0] == "OK"]
    for _, base, msg in errs:
        print("[!] 读不到 %s/%s：%s" % (NAS, base, msg))

    unknown = [r for r in cand if r[1] not in known]
    print("扫了 %d 个位置（%s），子目录数 ≥%d 的候选 **%d 个**；其中 **未登记 %d 个**："
          % (len(SCAN_BASES), " / ".join(SCAN_BASES), a.min, len(cand), len(unknown)))
    print()
    if not unknown:
        print("[ok] 没有「看起来是包、但没登记」的目录。")
    for _, path, (dirs, files) in sorted(unknown, key=lambda r: -r[2][0]):
        print("  %4d 个子目录 + %d 个散文件" % (dirs, files))
        print("        %s" % path)
    print()
    if known:
        print("对照：已登记/已声明的根 %d 个。" % len(known))

    print()
    print("★★ **本脚本只报信号、不改任何配置** —— 因为「要不要加一个包」是**产品决定**：")
    print("   ① 加包会**稀释**其他包的轮换频率（2 个 → 1/2；3 个 → 1/3）")
    print("   ② 「**注册了**」≠「**搜得到**」—— `mbf` 就是全绿但 `Found 0 torrents` 的例子")
    print("   ⇒ 上面每个候选**都要人先判**：它是不是真的「一包多片」？")
    print("     ★ 实测假阳性（2026-09-20）：`The Beatles - Discography +`（音乐）、")
    print("       `StarRupture-*`（游戏，含 `_crack`）都满足「子目录数」判据，却**不是 PT 大包**。")

    if a.emit_cmds and unknown:
        print()
        print("★ 若你要加其中某个（**先判它是真包**），步骤是：")
        for _, path, _ in sorted(unknown, key=lambda r: -r[2][0]):
            name = pathlib.PurePosixPath(path).name
            print()
            print("  # ── %s" % name[:60])
            print("  python scripts/reseed-state.py init --pack <包名> --root %s" % path)
            print("  # ★ 然后**手工**把它排进 scripts/drive-loop.py 的 PACKS_DEFAULT")
            print("  #   （唯一声明点；★ 顺序有意义：once_round 存的是**下标**，不是名字）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
