# -*- coding: utf-8 -*-
"""「装不出来 / 没 peer」· 诊断端（在 Windows 上跑，**只读**）。

背景（2026-09-18）：用户报「连校验过后、一点都不相符的文件都开始直接下载了」。
实测（全量 1999 条）**真因不是「校验不通过就重下」**，而是：

  cross-seed 按「名称 + 大小」匹配上了**发布组把附件（`.nfo` / `.jpg`）也写进
  种子的那一版**，而农场（`reseed_farm/`）里没有这些附件
  ⇒ qB 去补那几个缺文件 ⇒ 没有 peer ⇒ **永远补不上**（`stalledDL`）。

  · 逐文件 progress：`.mkv` ≈ 99.98%，而 `.jpg`/`.nfo` **恰好 0.00000%**
  · 那个 0% 的文件在 `reseed_singles/` 里**根本不存在**，在农场里**存在**
  · 载荷（`.mkv`）两侧**大小逐字节相同**（9658005448）
  · `amount_left`（2097152）≠（农场 size − 物理 size）（254167）—— 说明 qB 的
    进度是**按 piece 全局推导**的，不能跨文件对账（见 README「原理 C」）

★★ **qB 只认 piece：它已经有的那块数据不会重下 ⇒ 源文件目前还没被写穿**
  （那是 `linkguard` 在盯的事，不是这里）。真危害是**永久占位 + 踩 HnR**，
  且**一旦那个 `.mkv` 被动过/删过就会真重下并写穿源**。

判据与基线由 NAS 上的 drive-loop 每天算一次（`reseed_freeze_watch`，判据本体在
`orchestrator/state.py` 的 `reseed_unbuildable_band` / `reseed_no_peer_band`），
落在 `<compose>/drive-loop/scripts/.reseed-freeze.state`。本脚本：
  · **默认**问一次 qB 现算（要的是"此刻"),并把基线文件里的**计数**摆出来对照；
  · 默认**只打聚合**（条数 × 组 × 来源）；
  · 要看具体是哪几条，**必须显式** `--show-hashes`，且只出**12 位短 hash +
    save_path 末段**（照 `crossseed-linkguard.py:103` 的 `release_of`）。

★ 只读：**不写任何文件**、不动 qB（一个 GET）、不碰 NAS 上的任何东西。
★ 无凭据：只发 `Referer` 头，不发 cookie / 账号口令（同 `wait-for-checks.py`）。
  ⇒ 走的是 qB 的「本地免鉴权」——从 NAS 宿主机或同网段跑都行。
★ 本脚本**绝不 import** `scripts/drive-loop.py`：那个模块 import 即拉进生产的
  副作用（日志落点 / 状态文件 / 全局常量）。判据本体在 `orchestrator/state.py`
  里是**纯函数**，import 它没有副作用 —— 这正是判据不写在 drive-loop 里的理由。
★ 处置**没有自动化**（用户 2026-09-18 拍板：只做「识别 + 通知」，一点不碰 qB）。
  本脚本因此**绝不提供** `--pause` / `--tag` / `--delete` 之类的开关，
  也不生成"待处理清单"给别的工具去照着做。将来要处置，先回 SUMMARY §27 读原因。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover - 老 python / 非 tty
    pass

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))            # 好 import orchestrator.state

#: 与项目其它脚本一致：默认就是 NAS 的局域网地址（从 NAS 宿主机跑和从 Windows
#: 跑走的是同一条路）。见 `wait-for-checks.py` 里同一段说明。
DEFAULT_URL = "http://192.168.0.7:3060"

#: NAS 上 drive-loop 写的那份基线（本脚本**只读**，仅用来对照计数）。
#: ★ UNC 用**正斜杠** —— 反斜杠在这个 Git Bash → Python 的组合里会被当转义符吃掉。
DEFAULT_STATE = ("//iSunker-DS423/docker_ssd/reseed"
                 "/drive-loop/scripts/.reseed-freeze.state")

STATE_KEY = "_reseed_freeze"
GROUPS = (("unbuildable", "装不出来"), ("no_peer", "没 peer"))
SRC_OURS = "cross-seed"
SRC_IYUU = "IYUU自动辅种"

#: 组 → 缺口的**形状**（只是给人看的一句话，不参与判据）。
SHAPE_HINT = {
    "unbuildable": "缺口 ≤ 0.1%（只差发布组的附件），没有 peer ⇒ 永远补不上",
    "no_peer":     "一个字节都没下到、`availability == 0`（没见过源碎片）⇒ 根本没起来",
}


def fetch_info(url: str) -> list:
    """取一次 /torrents/info。★ 只发 Referer 头，绝不带凭据。"""
    req = urllib.request.Request(url + "/api/v2/torrents/info",
                                 headers={"Referer": url})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def buckets(torrents: list) -> dict:
    """按判据分组 → 再按来源 tag 分桶（与 drive-loop 的 `_reseed_freeze_split` 同形）。

    ★ 判据**直接复用生产那份**（`orchestrator/state.py`）—— 这里绝不重写一遍：
      两套口径一旦漂移，诊断端就会开始骗人，而它存在的意义正是"当生产说 X 时，
      到底是哪几条"。这正是 `check-indexer-timestamps.py` 那条教训的正面应用。
    """
    from orchestrator import state as S

    by_hash = {t.get("hash"): t for t in torrents if t.get("hash")}

    def split(band: dict) -> dict:
        out = {SRC_OURS: [], SRC_IYUU: [], "others": []}
        for h in band["hashes"]:
            tags = [x.strip() for x in ((by_hash.get(h) or {}).get("tags") or "").split(",")]
            if SRC_OURS in tags:
                out[SRC_OURS].append(h)
            elif SRC_IYUU in tags:
                out[SRC_IYUU].append(h)
            else:
                out["others"].append(h)
        return out

    return {"unbuildable": split(S.reseed_unbuildable_band(torrents)),
            "no_peer": split(S.reseed_no_peer_band(torrents)),
            "total": len(torrents),
            "by_hash": by_hash}


def load_state_counts(path: str) -> dict | None:
    """读 NAS 上的基线，**只取计数**（不打印、不返回 hash 明细）。

    读不到就返回 None —— 这不是错误：基线要 drive-loop 跑过一次日报才起锚
    （见 `crossseed-linkguard.py` 里同样一句说明）。
    """
    try:
        d = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as e:                              # noqa: BLE001
        print(f"（基线文件读不出：{type(e).__name__}: {e} —— 跳过对照）")
        return None
    if not isinstance(d, dict):
        return None
    d = d.get(STATE_KEY)
    if not isinstance(d, dict):
        return None
    out = {}
    for grp, _ in GROUPS:
        g = d.get(grp) or {}
        out[grp] = sum(len(g.get(s) or []) for s in (SRC_OURS, SRC_IYUU, "others"))
    return out


def release_of(t: dict) -> str:
    """save_path 的最后一段（= 站点目录名），**不是**完整 content_path。

    ★ 这是本脚本**唯一**会打印的"可识别内容"，且只在显式 `--show-hashes` 时。
      `tracker` 与 `content_path` **一律不打**（README 安全约定）。
    """
    sp = (t.get("save_path") or "").rstrip("/")
    return sp.rsplit("/", 1)[-1] if sp else "?"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="「装不出来 / 没 peer」停滞单种诊断（只读；默认只打聚合）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--url", default=DEFAULT_URL,
                    help=f"qB 地址（默认 {DEFAULT_URL}）")
    ap.add_argument("--state", default=DEFAULT_STATE,
                    help="NAS 上 drive-loop 写的基线（只读；仅对照计数）")
    ap.add_argument("--show-hashes", dest="show_hashes", action="store_true",
                    help="列出具体条目（12 位短 hash + 站点目录名；默认只打聚合）")
    args = ap.parse_args()

    try:
        torrents = fetch_info(args.url)
    except Exception as e:                              # noqa: BLE001
        print(f"✗ 读不到 qB（{args.url}）：{type(e).__name__}: {e}")
        return 2

    b = buckets(torrents)
    base = load_state_counts(args.state)

    print("「装不出来 / 没 peer」· 诊断（只读）")
    print(f"  qB：{args.url}    分母：{b['total']} 条")
    if base is None:
        print(f"  基线：{args.state}")
        print("        （还没起锚 —— 要 drive-loop 跑过一次日报才会写）")
    else:
        print(f"  基线（NAS 上那份，上次日报算的）："
              f"装不出来 {base['unbuildable']} · 没 peer {base['no_peer']}")
    print()

    total = 0
    for grp, label in GROUPS:
        by_src = b[grp]
        n = sum(len(v) for v in by_src.values())
        total += n
        print(f"{label}：{n} 条")
        print(f"  {SHAPE_HINT[grp]}")
        print(f"  来源：本项目 {len(by_src[SRC_OURS])} · "
              f"IYUU {len(by_src[SRC_IYUU])}（只记数、不处理）"
              + (f" · 别的 tag {len(by_src['others'])}" if by_src["others"] else ""))
        if args.show_hashes and n:
            for h in by_src[SRC_OURS] + by_src[SRC_IYUU] + by_src["others"]:
                t = b["by_hash"].get(h) or {}
                print(f"    {h[:12]}  [{release_of(t)}]"
                      f"  state={t.get('state')}"
                      f"  left={t.get('amount_left')}"
                      f"  prog={(t.get('progress') or 0):.5f}"
                      f"  seeds={t.get('num_seeds')}"
                      f"  avail={t.get('availability')}")
        print()

    print(f"合计 {total} 条（占分母 {b['total']} 的 "
          f"{(100.0 * total / b['total']):.2f}%）" if b["total"] else f"合计 {total} 条")
    print()
    if not args.show_hashes and total:
        print("要看具体是哪几条：加 `--show-hashes`（只出 12 位 hash + 站点目录名）")
        print()
    print("★ 本节**只是观测**：drive-loop 不做任何处置（不暂停、不打标签、不删）。")
    print("  IYUU 来源的那几条也**只记数**，但会出现在每日台账（含邮件）里 ——")
    print("  「是不是我们搜来的」不影响「该不该让你知道」。")
    print("★ 真危害是永久占位 + 踩 HnR。qB 只重下缺的 piece，所以**源文件目前")
    print("  还没被写穿**（那是 linkguard 在盯的事）。⇒ 别去动那几个种子的数据文件：")
    print("  动了才会真重下并写穿源。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
