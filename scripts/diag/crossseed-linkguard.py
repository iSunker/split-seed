# -*- coding: utf-8 -*-
"""链接守护 · 诊断端（在 Windows 上跑，只读）。

背景（2026-09-13，见 README「源文件被写穿」）：
  cross-seed 在 matchMode=partial 下把「名称+大小匹配、但 piece 不一致」的单种
  注入 qB；qB 校验不通过**不会拒绝**，而是**就地重下**那几个 piece —— 而
  linkDirs 是硬链接（与源同一个 inode），这一写**直接落到源文件上**，无声改写
  库里的母本，Farm 侧却仍显示 100%。

修法两步：
  ① 关上闸门 —— `matchMode` ⇄ `linkType` 互锁（cross-seed/config.js）：
     只有 reflink(COW) 才允许 partial ⇒ **将来新建**的链接写不穿。
     这一步已经在做（本仓库 config.js 里那段 resolveMatchMode）。
  ② 给**已经存在**的那 628 条硬链接上锚 —— 它们是硬链接，且**故意不重建**
     （重摆几十 TB 链接的风险大于收益），但要能发现「它正在被写穿」。

本脚本是 ② 的**诊断端**：判据与基线由 NAS 上的 drive-loop 每天算一次
（`linkguard_watch`，判据本体在 `orchestrator/state.py`），落在
`<compose>/drive-loop/scripts/.linkguard.state`。这里只**读**它 + 问一次 qB，
把「具体是哪几条」列出来。

★ 只读：不写任何文件、不动 qB、不碰 NAS 上的任何东西（经 SMB 读一个 JSON）。
★ 默认**只出 hash**（12 位）—— 名字里带发布名与站点，别让它随随便便进日志。
  要名字加 `--show-names`（只取 save_path 的最后一段，不打完整路径）。
★ 修复本身**没有自动化**：写穿已经发生 ⇒ 源文件已被改写，跨站做种的那份数据
  和源**同时**是坏的。这里的输出是「哪几条要重新下」，重下得回站点去拿 ——
  这正是 #74「无备份可恢复」那条记录的意思，别指望本脚本能还原。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8")

#: NAS 上的状态文件（drive-loop 写，本脚本读）。UNC 用**正斜杠** ——
#: 反斜杠在这个 Git Bash → Python 的组合里会被当转义符吃掉（踩过）。
DEFAULT_STATE = ("//iSunker-DS423/docker_ssd/reseed"
                 "/drive-loop/scripts/.linkguard.state")
REPO = pathlib.Path(__file__).resolve().parent.parent

SNAP_KEY = "_linkguard_snapshot"
DETAIL_KEY = "_linkguard_detail"


def load_env() -> dict:
    """读本地 .env（只取 qB 的地址与鉴权；**值不回显**）。"""
    env = {}
    p = REPO / ".env"
    if not p.is_file():
        return env
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def load_state(path: str) -> dict:
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"✗ 找不到状态文件：{path}")
        print("  ⇒ drive-loop 还没跑过一次日报（基线是那时候起的锚）。")
        print("     当场起锚：在 NAS 上跑一次")
        print("       sh /volume2/docker_ssd/reseed/"
              "drive-loop/run.sh  （或 drive-loop.py --daily-now）")
        raise SystemExit(2)
    except Exception as e:                              # noqa: BLE001
        print(f"✗ 状态文件读不出/不是 JSON：{type(e).__name__}: {e}")
        raise SystemExit(2)


def qb_alive_by_hash(env: dict) -> dict:
    """{hash: torrent}（只读；失败返回空 dict 并说明）。"""
    import urllib.parse
    import urllib.request

    base = "http://%s:3060" % env.get("NAS_IP", "192.168.0.7")
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())

    def call(p, d=None):
        b = urllib.parse.urlencode(d).encode() if d else None
        r = urllib.request.Request(base + p, data=b)
        if b:
            r.add_header("Referer", base)
            r.add_header("Content-Type", "application/x-www-form-urlencoded")
        return op.open(r, timeout=60).read()

    try:
        call("/api/v2/auth/login", {"username": env.get("QBIT_USER", "admin"),
                                    "password": env.get("QBIT_PASSWORD", "")})
        info = json.loads(call("/api/v2/torrents/info"))
    except Exception as e:                              # noqa: BLE001
        print(f"（读不到 qB：{type(e).__name__}: {e} —— 只出状态文件里的信息）")
        return {}
    return {t.get("hash"): t for t in info or []}


def release_of(t: dict) -> str:
    """save_path 的最后一段（= 站点目录名），**不是**完整 content_path。"""
    sp = (t.get("save_path") or "").rstrip("/")
    return sp.rsplit("/", 1)[-1] if sp else "?"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="链接守护诊断：那 628 条既有硬链接里，哪些被改写过",
    )
    ap.add_argument("--state", default=DEFAULT_STATE, help="状态文件路径")
    ap.add_argument("--show-names", dest="show_names", action="store_true",
                    help="显示发布名（默认只出 hash 12 位）")
    ap.add_argument("--all-changed", action="store_true",
                    help="列出全部被改写文件（默认只出每条的样本 5 个）")
    args = ap.parse_args()

    st = load_state(args.state)
    snap = st.get(SNAP_KEY)
    detail = st.get(DETAIL_KEY) or {}
    if snap is None:
        print("✗ 状态文件里没有基线（`_linkguard_snapshot`）—— 起锚没成功。")
        return 2

    env = load_env()
    known = qb_alive_by_hash(env)

    print("链接守护 · 诊断")
    print(f"  状态文件：{args.state}")
    print(f"  最近一次比对：{detail.get('at') or '(未知)'}")
    print(f"  基线覆盖：{len(snap)} 个文件")
    print()

    changed = detail.get("changed") or {}
    inflight = detail.get("inflight") or []
    removed_n = detail.get("removed_n", 0)

    if not changed and not removed_n:
        print("✓ 与基线一致：没有任何文件被改写或消失。")
    else:
        print(f"★ 被改写 / 新增（{len(changed)} 条种子受影响，"
              f"共 {sum(v.get('n', 0) for v in changed.values())} 个文件）")

    if changed:
        print()
        for h, v in sorted(changed.items(), key=lambda kv: -kv[1].get("n", 0)):
            t = known.get(h)
            label = h[:12] if h != "?" else "?（认不出属于哪条种子）"
            if t is None and known:
                label += "  ← qB 里已不存在"
            elif t is not None and args.show_names:
                label += f"  [{release_of(t)}]"
            if t is not None:
                label += (f"  state={t.get('state')}"
                          f" prog={(t.get('progress') or 0):.4f}")
            print(f"  {label}")
            print(f"      文件数 {v.get('n', 0)}")
            sample = v.get("sample") or []
            if not args.all_changed:
                sample = sample[:5]
            for p in sample:
                print(f"        {p}")
            if not args.all_changed and v.get("n", 0) > len(sample):
                print(f"        …（其余 {v['n'] - len(sample)} 个略；--all-changed 看全）")

    if removed_n:
        print(f"\n消失的文件：{removed_n} 个（重建链接/人工删除都会出现）")
        for p in (detail.get("removed_sample") or []):
            print(f"        {p}")

    if inflight:
        print(f"\n此刻正在动（非终态，可能在重下）：{len(inflight)} 条")
        for h in inflight[:20]:
            t = known.get(h)
            extra = "" if t is None else f"  state={t.get('state')}"
            print(f"        {h[:12]}{extra}")

    print()
    if changed:
        print("★ 被改写 = 内容被就地覆写 = 写穿的直接证据。")
        print("  这些跨站做种的数据**和源**同时是坏的（同一个 inode），没有自动还原：")
        print("  要恢复得回站点重新下这几条（见任务 #74「无备份可恢复」）。")
        print("  在此之前**别去动 qB 里对应的种子** —— 删掉只会少一份证据。")
    else:
        print("  没有写穿迹象。闸门（matchMode ⇄ linkType 互锁）已经在管新建的链接。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
