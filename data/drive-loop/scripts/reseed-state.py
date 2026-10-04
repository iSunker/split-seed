#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reseed-state —— 单片状态机 CLI（登记 / 同步 / 汇报 / 待办 / 驱动重搜）。

它**只读** cross-seed 的库与日志、以及 qB 的 API；唯一被写的是我们自己的
sidecar 状态库（默认 `hlink/state.db`，可用 --db 或 RESEED_STATE_DB 覆盖）。

重搜周期
--------
「每站按周期重搜」是默认行为（默认 14 天，见 orchestrator/state.py 的
DEFAULT_CADENCE_DAYS 注释里的取舍理由）：cross-seed 的 `timestamp` 表里记着
**每个 searchee 在每个索引器上最后一次搜索的时间**，`todo` 就按它逐站比周期。
想改：`--cadence-days 7`，或 `--cadence "SiteA=14,NanyangPT=30"` 给单个站单独设。

常用流程
--------
    # 1) 登记大包（可反复跑，不会重置已有状态）
    python scripts/reseed-state.py init --pack frds-top250-2024 \\
        --root /volume1/video/download/movies/DouBan_IMDB.TOP250.Movies.Mixed.Collection.20240501.FRDS \\
        --local-root //YOUR-NAS/video/download/movies/DouBan_IMDB.TOP250.Movies.Mixed.Collection.20240501.FRDS

    # 2) 同步：把 cross-seed.db + 日志 + qB 的事实翻译成"每部到哪一步了"
    python scripts/reseed-state.py sync --pack frds-top250-2024 \\
        --db-path  //YOUR-NAS/docker_ssd/reseed/cross-seed/cross-seed.db \\
        --log //YOUR-NAS/docker_ssd/reseed/cross-seed/logs/info.current.log \\
        --qbit-url http://NAS_IP:3060

    # 3) 看还剩多少没搜 / 每站下次什么时候能重搜
    python scripts/reseed-state.py report --pack frds-top250-2024

    # 4) 导出"该搜但没搜"的清单
    python scripts/reseed-state.py todo --pack frds-top250-2024 \\
        --indexers SiteA,SiteB --out scripts/todo-paths.txt

    # 5) 驱动重搜：控速 + 退避感知 + 打完自动回灌（默认 dry-run，加 --apply 才发）
    python scripts/reseed-state.py drive --pack frds-top250-2024 --apply \\
        --url http://NAS_IP:2468 --api-key <CROSSSEED_API_KEY> \\
        --db-path <cross-seed.db> --log <info.current.log> --qbit-url http://NAS_IP:3060

设计要点见 orchestrator/state.py 顶部的注释。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestrator import state as S  # noqa: E402

# ★★ 2026-10-05（B2，代码入镜像）：sidecar 状态库的位置**不再靠 `REPO/hlink/`** ——
#   镜像里 REPO=/app，而 `hlink/state.db` 是**运行时**产物（挂在 `/state`），
#   不在镜像层里。⇒ 与 `drive-loop.py` 的 `STATE_DIR` 同一套约定：
#     ① `RESEED_STATE_DB`（显式全路径）最优先；
#     ② 否则 `DRIVE_STATE_DIR/hlink/state.db`；
#     ③ 都没有才回退 `REPO/hlink/state.db`（本机开发/旧行为）。
#   ★ 顺序的理由：显式路径 > 环境约定的状态目录 > 反推 —— 反推在镜像里必错，
#     而错的后果是 `StateStore` 在**只读层**上 mkdir 失败或建空库（见 state.py 那段）。
_STATE_DIR = os.environ.get("DRIVE_STATE_DIR")
DEFAULT_DB = os.environ.get("RESEED_STATE_DB") or (
    str(Path(_STATE_DIR) / "hlink" / "state.db") if _STATE_DIR
    else str(REPO / "hlink" / "state.db")
)


def _say(*a) -> None:
    print(*a, flush=True)


def _note(*a) -> None:
    """走 stderr：用于分批说明等提示，避免污染 todo 的路径清单输出。"""
    print(*a, file=sys.stderr, flush=True)


def _safe_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass


def _scan_pack(nas_roots: list[str], local_roots: list[str], *,
               depth: int, pattern: str = "*",
               exclude: list[str] | None = None) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """把若干 dataDir 扫成 `[(单片名, NAS 路径)]`。

    返回 `(条目, 重名, 问题)`。**不写库**，方便 `--dry-run` 先核对。

    路径映射：`--root`（NAS 视角，webhook 要用）与 `--local-root`（能列目录的 UNC）
    **按序一一对应**；扫描在 local 侧做，结果再按根前缀替换回 NAS 视角。
    """
    problems: list[str] = []
    if not nas_roots:
        return [], [], ["至少要有一个 --root"]
    if local_roots and len(local_roots) != len(nas_roots):
        return [], [], [
            f"--root {len(nas_roots)} 个、--local-root {len(local_roots)} 个，数量不等 —— "
            f"多根包必须**按序一一对应**（第 i 个 local-root 就是第 i 个 root 的 UNC）"]

    pairs: list[tuple[str, str]] = []
    for i, nr in enumerate(nas_roots):
        lr = local_roots[i] if i < len(local_roots) else nr
        nr, lr = S._norm_path(nr), S._norm_path(lr)
        if not os.path.isdir(lr):
            problems.append(f"列不了目录: {lr}")
            continue
        pairs.append((nr, lr))
    if not pairs:
        return [], [], problems or ["没有任何一个根可以列目录"]

    entries_local, dups = S.scan_pack([lr for _, lr in pairs], max_depth=depth)

    excl = [p for p in (exclude or []) if p]
    out: list[tuple[str, str]] = []
    for name, path in entries_local:
        if name.startswith(".") or name == "@eaDir":
            continue
        if any(Path(name).match(p) for p in excl):
            continue
        if pattern not in ("*", "") and not Path(name).match(pattern):
            continue
        np = S._norm_path(path)                    # scan_pack 已规范化，这里是双保险
        nas = np                                   # 兜底：万一没配对上就用原路径
        for nr, lr in pairs:
            if np == lr or np.startswith(lr + "/"):
                nas = nr + np[len(lr):]
                break
        out.append((name, nas))
    return out, dups, problems


def _idx_list(s: str | None) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _read_env_file(path: str) -> dict[str, str]:
    """读一个 `.env` 风格的键值文件（只取 `KEY=VALUE`，忽略注释与空行）。"""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise SystemExit(f"[!!] 读不了 {path}: {e}") from e
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _guess_unc_host() -> str | None:
    """从 `scripts/.nasrc`（gitignored）里读 NAS_NAME，拼成 `//HOST`。"""
    rc = Path(__file__).resolve().parent / ".nasrc"
    if not rc.is_file():
        return None
    for raw in rc.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("NAS_NAME="):
            v = line.split("=", 1)[1].strip().strip('"').strip("'")
            return ("//" + v) if v else None
    return None


def _roots_from_env(envfile: str, matches: list[str] | None,
                    unc_host: str | None,
                    nas_prefix: str) -> tuple[list[str], list[str], list[str]]:
    """从 `.env` 里挑出属于某个包的根 —— **首选 `FARM_SOURCES`，缺席时退回 `DATA_DIRS`**。

    ★★ v3 之后这两个键的语义**换了位置**，这里必须跟着换（2026-09-12 补）：

        切换前   DATA_DIRS    = 那 49 条**源目录**  → 读它就等于读源清单，**正确**
        切换动作 = 把 DATA_DIRS 改成农场那一条
        切换后   DATA_DIRS    = `/…/reseed_farm`（cross-seed 的**输入**，不是源清单）
                 FARM_SOURCES = 那 49 条源目录（compose 只透传 DATA_DIRS，
                                cross-seed 压根看不到它）

      ★ 切换之后还从 `DATA_DIRS` 派生，挑到的是**农场那一条** —— 于是
        `--match <包关键词>` 一条都命中不了，直接报「没挑到任何根」。这是死路，
        只是**不挡路**（只有真加包时才会踩）。

    ★ 这不是一次新设计：`scripts/build-farm.sh:132-155` 早就是
      「优先 `FARM_SOURCES`，退回 `DATA_DIRS`」，连退回时的报错文案都写好了
      （`build-farm.sh:178-181`）。所以这里是**补上同一个切换里漏改的那一个工具**，
      不是另立一套规矩 —— 同理，也不该有第二种"优先谁"的说法。

    ★ 当初为什么选 `DATA_DIRS`：因为它是 cross-seed **实际会扫的清单**，从它派生就
      不会和 cross-seed 漂移。这个理由**只在切换前成立** —— 切换后 cross-seed 扫的是
      农场，而包的 `roots` 要的是**源路径**（webhook 发的就是 `movie.path`，
      见 `state.py:1133`），两者的语义在 v3 里正好对调了。

    返回 `(NAS 根, 本地根, 说明)`。本地根 = 把 `nas_prefix` 换成 `unc_host`。
    """
    env = _read_env_file(envfile)
    src_key = "FARM_SOURCES"
    entries = [p.strip() for p in env.get("FARM_SOURCES", "").split(",") if p.strip()]
    fallback = ""
    if not entries:
        src_key = "DATA_DIRS"
        entries = [p.strip() for p in env.get("DATA_DIRS", "").split(",") if p.strip()]
        fallback = ("  ★ 用的是**退回来源**：`FARM_SOURCES` 在 .env 里不存在或为空。"
                    "v3 切换后 `DATA_DIRS` = 农场那一条，若它挑不出你要的包，"
                    "说明这个 .env 该补一行 `FARM_SOURCES=`（格式同 DATA_DIRS）。")
    if not entries:
        return [], [], [f"{envfile} 里既没有 FARM_SOURCES 也没有 DATA_DIRS，或两者都是空的"]

    pats = [m for m in (matches or []) if m]
    picked = [p for p in entries if any(m in p for m in pats)] if pats else list(entries)
    if not picked:
        return [], [], [f"{src_key} 共 {len(entries)} 条，没有一条包含 {pats}"]

    notes = [f"从 {envfile} 的 {src_key}（共 {len(entries)} 条）里挑了 {len(picked)} 条"
             + (f"，匹配 {pats}" if pats else "（未给 --match，全取）")]
    if fallback:
        notes.append(fallback)

    host = (unc_host or "").rstrip("/")
    if not host:
        notes.append("[warn] 没给 --unc-host，也没能从 scripts/.nasrc 读到 NAS_NAME；"
                     "本地根将直接沿用 NAS 路径（只有本机就是 NAS 时才可行）")
    pfx = (nas_prefix or "/volume1").rstrip("/")

    nas_roots: list[str] = []
    local_roots: list[str] = []
    off_prefix = 0
    for p in picked:
        nas_roots.append(p)
        if not host:
            local_roots.append(p)
        elif p == pfx:
            local_roots.append(host + "/")
        elif p.startswith(pfx + "/"):
            local_roots.append(host + p[len(pfx):])
        else:
            local_roots.append(p)
            off_prefix += 1
    if host and off_prefix:
        notes.append(f"[warn] {off_prefix} 条不以 {pfx}/ 开头，本地根直接沿用原路径（请核对）")
    return nas_roots, local_roots, notes


def _sync_now(args, st, *, quiet: bool = False):
    """跑一次完整 sync（三来源）。drive 的回灌也用它。"""
    qb = []
    if getattr(args, "qbit_url", None):
        try:
            qb = S.qbit_torrents(args.qbit_url, args.category)
            if not quiet:
                _say(f"qB {args.qbit_url} 分类 {args.category!r}：{len(qb)} 个种")
        except Exception as e:  # noqa: BLE001
            _say(f"[!!] 取 qB 列表失败（忽略，不影响其他来源）: {e}")
    return S.sync_pack(
        st, args.pack,
        crossseed_db=getattr(args, "db_path", None),
        log_paths=getattr(args, "log", None) or [],
        qbit_torrents=qb,
        indexers_override=_idx_list(getattr(args, "indexers", None)) or None,
        indexer_alias=S.parse_alias(getattr(args, "indexer_alias", None)),
        cadence_days=args.cadence_days,
        cadence_by_indexer=S.parse_cadence(getattr(args, "cadence", None)),
        log_attempts=not getattr(args, "quiet_attempts", False),
    )


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_init(args) -> int:
    nas_roots = [r for r in (args.root or []) if r]
    local_roots = [r for r in (getattr(args, "local_root", None) or []) if r]
    notes: list[str] = []

    # ---- 根的来源：显式 --root 列表，或从 .env 派生（FARM_SOURCES → DATA_DIRS，二选一）----
    if args.roots_from_env:
        if nas_roots:
            _say("[!!] --root 与 --roots-from-env 是两种取根方式，只能选一种")
            return 2
        host = args.unc_host or _guess_unc_host()
        nas_roots, local_roots, notes = _roots_from_env(
            args.roots_from_env, args.match, host, args.nas_prefix)
        for n in notes:
            _say(f"    {n}")
        if not nas_roots:
            _say("[!!] --roots-from-env 没挑到任何根（原因见上一行）")
            return 2

    if not nas_roots:
        _say("[!!] 没有根。两种用法：")
        _say("    A) 显式列出（适合单根包）：")
        _say("       --root /volume1/video/... --local-root //YOUR-NAS/video/...")
        _say("    B) 从 .env 派生（适合多根包，推荐）：")
        _say("       --roots-from-env .env --match <路径关键词> [--unc-host //YOUR-NAS]")
        _say("    路径视角说明：--root 必须是 cross-seed 视角的 NAS 路径（webhook 要用它），")
        _say("    列目录另外用本地/UNC 路径；多根包两者**按序一一对应**。")
        return 2

    entries, dups, problems = _scan_pack(
        nas_roots, local_roots,
        depth=args.depth, pattern=args.pattern, exclude=args.exclude)

    for p in problems:
        _say(f"[!!] {p}")
    if not entries:
        _say("没有扫到任何单片，未写库。")
        return 2

    _say(f"根 {len(nas_roots)} 个，深度 maxDataDepth={args.depth}")
    for i, nr in enumerate(nas_roots):
        lr = local_roots[i] if i < len(local_roots) else nr
        _say(f"  [{i + 1}] NAS: {nr}")
        _say(f"      本地: {lr}")
    _say(f"识别到 {len(entries)} 个单片（= cross-seed 会去搜的 searchee 目录）")
    if dups:
        uniq = sorted(set(dups))
        _say(f"[warn] {len(uniq)} 个重名（两个根下同名，只保留了先出现的那个）: "
             + ", ".join(uniq[:5]) + (" ..." if len(uniq) > 5 else ""))

    if args.dry_run:
        for n, p in entries[:15]:
            _say(f"    {n}")
        if len(entries) > 15:
            _say(f"    ... 其余 {len(entries) - 15} 个")
        _say("(--dry-run，未写库)")
        return 0

    # ★★ 这是全仓**唯一**一处该建库的地方 —— 所以 `create=True` 写在这里，
    #    而不是当成 `StateStore` 的默认值。其余 7 个调用点（farm/sync/report/
    #    trend/todo/drive/show）都作用在**已登记**的包上，库不在就该报错，
    #    不是"顺手建一个空库"（空库 = 每部片都像没登记过）。
    with S.StateStore(args.db, create=True) as st:
        st.upsert_pack(
            args.pack, nas_roots[0],
            local_root=(local_roots or nas_roots)[0],
            roots=nas_roots,
            local_roots=local_roots or nas_roots,
            max_depth=args.depth,
            farm_root=getattr(args, "farm_root", None),
            link_dir=args.link_dir, category=args.category)
        added = st.register_dirs(args.pack, entries)
        total = len(st.movies(args.pack))
    _say(f"新登记 {added} 部，库内共 {total} 部（已存在的不重置 stage）")
    return 0


def cmd_farm(args) -> int:
    """给**已登记**的包补一个农场根，不动 roots、不重扫目录。

    v3 把 cross-seed 的 `DATA_DIRS` 换成了硬链接农场，包的原路径还在，
    但 cross-seed 报上来的 searchee 路径变成了农场路径 —— 不登记这个根，
    `sync` 会认不出来，把整包算成"别的包"（症状：qB 里明明在做种，
    状态机却集体降级成 PENDING/UNMATCHED）。
    """
    with S.StateStore(args.db) as st:
        p = st.pack(args.pack)
        if p is None:
            _say(f"[!!] 未登记: {args.pack}（先 init）")
            return 2
        if args.clear:
            st.con.execute("UPDATE pack SET farm_root=NULL WHERE name=?", (args.pack,))
            st.con.commit()
            _say(f"{args.pack}: 已清除农场根（回到只认原路径的老行为）")
            return 0
        if not args.farm_root:
            cur = st.farm_root(args.pack)
            _say(f"=== 包 {args.pack} ===")
            _say(f"    源目录: {p['root']}")
            _say(f"    农场根: {cur or '（未设置）'}")
            _say(f"    镜像键: {len(st.farm_dir_paths(args.pack))} 条")
            return 0
        st.upsert_pack(args.pack, p["root"], farm_root=args.farm_root)
        n = len(st.farm_dir_paths(args.pack))
        _say(f"{args.pack}: 农场根 = {args.farm_root}（可识别 {n} 条农场路径）")
        if not n:
            _say("[warn] 镜像键为 0 —— 检查农场根是否写对，或包的 roots 是否还指向旧路径")
            return 2
        return 0


def cmd_sync(args) -> int:
    with S.StateStore(args.db) as st:
        rep = _sync_now(args, st)
        _say(rep.render())
        sm = st.summary(args.pack)
        _say("  库内阶段: " + "  ".join(f"{k}={sm[k]}" for k in S.ALL_STAGES if sm[k]))
        _print_cadence(st, args)
    return 0


def _print_cadence(st, args) -> None:
    idx = _idx_list(getattr(args, "indexers", None))
    tab = st.cadence_table(args.pack, indexers_now=idx,
                           cadence_days=args.cadence_days,
                           cadence_by_indexer=S.parse_cadence(getattr(args, "cadence", None)))
    if not tab:
        return
    _say("")
    _say("  按站重搜周期：")
    for ix, d in sorted(tab.items()):
        nxt = d["next"].strftime("%m-%d %H:%M") if d["next"] else "现在"
        _say(f"    {ix:<16} 搜过 {d['searched']:>4} 部 · 到周期 {d['due']:>4} 部 · "
             f"下次最早可重搜 {nxt}")


def cmd_report(args) -> int:
    with S.StateStore(args.db) as st:
        p = st.pack(args.pack)
        if p is None:
            _say(f"[!!] 未登记: {args.pack}（先 init）")
            return 2
        sm = st.summary(args.pack)
        _say(f"=== 包 {args.pack} ===")
        _say(f"    源目录: {p['root']}")
        _say(f"    登记 {sm['TOTAL']} 部")
        _say("")
        _say("    阶段          数量   含义")
        meanings = {
            S.STAGE_SEEDING: "已在 qB 里（终点）",
            S.STAGE_MATCHED: "匹配到单种，等 qB 确认",
            S.STAGE_UNMATCHED: "真搜过，但没匹配到",
            S.STAGE_SKIPPED: "★被退避跳过 —— 必须重搜",
            S.STAGE_PENDING: "还没搜过",
            S.STAGE_ERROR: "异常",
        }
        for k in S.ALL_STAGES:
            _say(f"    {k:<13} {sm[k]:>5}   {meanings[k]}")

        idx = _idx_list(args.indexers)
        pairs = st.todo_detail(args.pack, indexers_now=idx,
                               include_cooldown=args.include_cooldown,
                               cadence_days=args.cadence_days,
                               cadence_by_indexer=S.parse_cadence(args.cadence))
        _say("")
        _say(f"    → 待搜索: {len(pairs)} 部")
        _say(f"       周期: 每站 {args.cadence_days} 天"
             + (f"（{args.cadence} 单独覆盖）" if args.cadence else ""))
        _print_cadence(st, args)

        if args.verbose:
            _say("")
            for r, due in pairs:
                _say(f"       [{r['stage']:<10}] 该搜: {','.join(due) or '-':<18} {r['dir_name']}")
        else:
            _say("")
            _say("    (加 -v 看逐部清单)")
    return 0


def cmd_trend(args) -> int:
    """新增做种趋势（§16.3）—— 按 (周, 站) 看，而不是全局一个数。

    换站决策问的是「**这个站最近还值不值得留**」，快照答不了，得看趋势。
    """
    with S.StateStore(args.db) as st:
        if args.pack and st.pack(args.pack) is None:
            _say(f"[!!] 未登记: {args.pack}（先 init）")
            return 2
        rep = st.trend(weeks=args.weeks, pack=args.pack)
    head = f"（包 {args.pack}）" if args.pack else "（全部包）"
    _say(f"=== 新增做种趋势 {head} ===")
    _say(rep.render())
    if args.verbose and rep.weeks:
        _say("")
        _say("    逐周 × 逐站：")
        for wk in rep.weeks:
            _say(f"      {wk}   合计 {rep.week_totals[wk]} 部（去重）")
            for s in rep.sites:
                n = rep.cells.get((wk, s), 0)
                if n:
                    _say(f"          {s}  {n}")
    else:
        _say("")
        _say("    (加 -v 看逐周 × 逐站明细)")
    _say("")
    _say("    ★ 「新增做种」按**每片首次**变 SEEDING 算（attempt 表按 at 聚合）——")
    _say("      同一部片不会因为反复 sync 被重复计数。")
    return 0


def cmd_quota(args) -> int:
    """站点额度台账（§16.1）—— 来源 A（自己数）+ C（保险丝），B（Prowlarr）可选。

    ★★ 它**不是站点余额表**：Prowlarr 的 Query Limit 是我们自己配的本地计数器，
    站点网页上那句"今天还剩 N 次"我们拿不到（§16.1.1 的 ③）。
    这个功能的定位是「把『每天登站看两次』降到『出异常才登站』」。
    """
    if not args.db_path:
        _say("[!!] 需要 --db-path（cross-seed.db 的路径；只读）")
        return 2
    try:
        lines = S.quota_snapshot(args.db_path, hours=args.hours)
    except FileNotFoundError as e:
        _say(f"[!!] {e}")
        return 2

    key = args.prowlarr_api_key or _read_env_key(Path(args.env), "PROWLARR_API_KEY")
    note = ""
    if args.with_prowlarr:
        if not args.prowlarr_url:
            note = "（要 --prowlarr-url 才知道问谁）"
        else:
            S.attach_prowlarr_quota(lines, args.prowlarr_url, key or "")
    elif key:
        note = "（没有 --with-prowlarr，本次不做来源 B 互校）"

    _say(S.render_quota(
        lines, title=f"=== 站点额度台账（滚动 {args.hours:g} 小时）===", note=""))
    _say("")
    _say("    ★ 口径：这是「最近窗口内搜过的**部数**」，不是发出去的请求次数")
    _say("      （timestamp 表每 (片,站) 只留最后一次，重搜不叠加）。")
    _say("    ★ 这不是站点余额 —— 站点网页上的剩余额度 Prowlarr 不知道（§16.1.1）。")
    if args.hours != 24.0:
        _say(f"    ★ 注意：这里是 {args.hours:g} 小时窗口；Prowlarr 的 limit 按 UTC 0 点"
             "重置（= 本地 08:00），比大小前先对齐窗口。")
    if note:
        _say(f"    {note}")
    return 0


def _read_env_key(env_path: Path, key: str) -> str | None:
    """从 .env 里取一个值。★ 凭据**只从文件读**，不提供命令行开关 ——
    项目的规则是 PT 站/API 凭据不进命令行（会留在 shell 历史和进程列表里）。"""
    try:
        for ln in env_path.read_text(encoding="utf-8").splitlines():
            if ln.startswith(key + "="):
                v = ln.split("=", 1)[1].strip()
                return v or None
    except OSError:
        pass
    return None


def cmd_todo(args) -> int:
    with S.StateStore(args.db) as st:
        idx = _idx_list(args.indexers)
        pairs = st.todo_detail(args.pack, indexers_now=idx,
                               include_cooldown=args.include_cooldown,
                               cadence_days=args.cadence_days,
                               cadence_by_indexer=S.parse_cadence(args.cadence))
        if args.stage:
            want = {s.strip().upper() for s in args.stage.split(",")}
            pairs = [(r, d) for r, d in pairs if r["stage"] in want]
        pairs, plan = S.apply_batch(pairs, limit=args.limit, batch=args.batch)
        if pairs is None:
            _note(f"[!!] {plan}")
            return 2
        if args.limit or args.batch or args.plan:
            _note(f"    {plan}")
        if args.plan:
            return 0
        lines = [r["path"] for r, _ in pairs]
        if args.out:
            Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
            _say(f"已写出 {len(lines)} 条 → {args.out}")
        else:
            for ln in lines:
                _say(ln)
        return 0


def cmd_drive(args) -> int:
    with S.StateStore(args.db) as st:
        idx = _idx_list(args.indexers)
        cad_by = S.parse_cadence(args.cadence)
        pairs = st.todo_detail(args.pack, indexers_now=idx,
                               include_cooldown=args.include_cooldown,
                               cadence_days=args.cadence_days,
                               cadence_by_indexer=cad_by)
        if args.stage:
            want = {s.strip().upper() for s in args.stage.split(",")}
            pairs = [(r, d) for r, d in pairs if r["stage"] in want]
        total_all = len(pairs)
        pairs, plan = S.apply_batch(pairs, limit=args.limit, batch=args.batch)
        if pairs is None:
            _say(f"[!!] {plan}")
            return 2
        paths = [r["path"] for r, _ in pairs]

        if not paths:
            _say("没有待搜索项。")
            return 0

        by_stage: dict[str, int] = {}
        for r, _ in pairs:
            by_stage[r["stage"]] = by_stage.get(r["stage"], 0) + 1
        eta = max(0, len(paths) - 1) * args.interval
        _say(plan)
        _say(f"本批 {len(paths)} 部  " + "  ".join(f"{k}={v}" for k, v in sorted(by_stage.items())))
        _say(f"节流：每条间隔 {args.interval:.0f}s → 本批约 {eta / 60:.0f} 分钟"
             + (f"；全部 {total_all} 部约 {max(0, total_all - 1) * args.interval / 60:.0f} 分钟"
                if args.limit else ""))

        if args.plan:
            _say("（--plan：只打印计划，未发任何请求）")
            return 0

        if not args.apply:
            for p in paths[:20]:
                _say("   ", p)
            if len(paths) > 20:
                _say(f"    ... 其余 {len(paths) - 20} 部")
            _say("加 --apply 才真正发 webhook。")
            return 0
        if not (args.url and args.api_key):
            _say("[!!] --apply 需要 --url 和 --api-key")
            return 2

        seeding_before = sum(1 for r in st.movies(args.pack) if r["stage"] == S.STAGE_SEEDING)

        last = [0.0]

        def on_event(kind, *rest):
            now = time.time()
            if kind == "sent":
                pth, code, i, total = rest
                tag = "OK " if code in (200, 202, 204) else "!! "
                gap = f" (+{now - last[0]:.0f}s)" if last[0] else ""
                last[0] = now
                _say(f"  [{i}/{total}] {tag}{code}{gap}  {Path(pth).name[:56]}")
            elif kind == "wait":
                _say(f"  ⏸  {rest[0]}")
            elif kind == "warn":
                _say(f"  !  {rest[0]}")
            elif kind == "abort":
                _say(f"  ✗  {rest[0]}")
            elif kind == "drained":
                _say(f"  ·  {rest[0]}")

        sess = S.DriveSession(
            url=args.url, api_key=args.api_key,
            crossseed_db=args.db_path,
            interval=args.interval,
            check_every=args.check_every,
            check_secs=args.backoff_check_secs,
            max_wait=args.max_wait,
            timeout=args.timeout,
            pause_on_backoff=not args.no_pause_on_backoff,
            # ★ 同 drive-loop：交给会话去区分「所有站都在退避」与「只禁了一部分」。
            #   手工跑没收 `--indexers` 时是空表 ⇒ 退化回旧行为（该等就等）。
            indexers=_idx_list(getattr(args, "indexers", None)),
            on_event=on_event,
        )
        stats = sess.run(paths)
        _say("")
        _say(f"发送完毕：成功 {stats.ok} / 失败 {stats.failed}"
             + (f"，因退避等待 {stats.waited_sec / 60:.1f} 分钟（{stats.backoff_hits} 次）"
                if stats.waited_sec else ""))
        if stats.aborted:
            _say(f"⚠ 提前中止：{stats.aborted}")

        if args.no_auto_sync:
            _say("(--no-auto-sync：跳过回灌。之后请自己跑一次 sync)")
            return 0 if not stats.aborted else 3
        if not args.db_path:
            _say("[!!] 回灌需要 --db-path（cross-seed.db 路径），已跳过")
            return 3

        _say("")
        _say("等 cross-seed 忙完（日志静默即视为队列空）...")
        S.wait_for_log_quiet(args.log[0] if args.log else "", quiet_sec=args.settle,
                             max_wait=args.drain_max_wait, on_event=on_event)
        _say("回灌状态...")
        rep = _sync_now(args, st, quiet=True)
        stats.resync = rep
        stats.still_skipped = sum(1 for r in st.movies(args.pack) if r["stage"] == S.STAGE_SKIPPED)
        stats.newly_seeding = (sum(1 for r in st.movies(args.pack)
                                   if r["stage"] == S.STAGE_SEEDING) - seeding_before)
        _say("")
        _say("=== 回灌结果 ===")
        _say(rep.render())
        _say(f"  本次新增做种     : {stats.newly_seeding}")
        _say(f"  仍为 SKIPPED     : {stats.still_skipped}  ← 这些是「又被退避」的")
        _say("")
        _print_cadence(st, args)
        if stats.still_skipped:
            _say("")
            _say("仍被跳过 → 多半是站点侧 502/限流（见 SUMMARY §6.5）。")
            _say("去 Prowlarr 日志看该站真实状态码，别急着调 delay。")
        return 0 if not stats.aborted else 3


def cmd_show(args) -> int:
    with S.StateStore(args.db) as st:
        r = st.movie_by_dir(args.pack, args.movie) or st.movie_by_path(args.pack, args.movie)
        if r is None:
            _say(f"[!!] 找不到: {args.movie}")
            return 2
        _say(f"{r['dir_name']}")
        for k in ("stage", "attempts", "last_attempt_at", "next_retry_at", "seeding_count"):
            _say(f"  {k:<16} {r[k]}")
        for k in ("searched_indexers", "indexer_seen", "skipped_indexers",
                  "matched_indexers", "matched_hashes"):
            _say(f"  {k:<16} {r[k]}")
        _say("  --- 流水 ---")
        for a in st.attempts(r["id"]):
            _say(f"  {a['at']}  {a['kind']:<7} {a['result']:<10} "
                 f"idx={a['indexers']} skip={a['skipped_indexers']} {a['note'] or ''}")
    return 0


# --------------------------------------------------------------------------- #
def _add_cadence_opts(p) -> None:
    p.add_argument("--cadence-days", type=int, default=S.DEFAULT_CADENCE_DAYS,
                   help=f"每个站多久重搜一次（天），默认 {S.DEFAULT_CADENCE_DAYS}")
    p.add_argument("--cadence", help="按站覆盖周期，如 'SiteA=7,SiteB=30'")


def _add_source_opts(p) -> None:
    p.add_argument("--db-path", dest="db_path", help="cross-seed.db 路径（只读）")
    p.add_argument("--log", action="append", help="cross-seed 日志路径，可多次")
    p.add_argument("--qbit-url", help="如 http://NAS_IP:3060")
    p.add_argument("--category", default="reseed-singles")
    p.add_argument("--indexers", help="当前生效的索引器名，逗号分隔（覆盖 db 推断）")
    p.add_argument("--indexer-alias", action="append",
                   help="索引器 url=名字，可多次。如 'http://prowlarr:9696/1/api=SiteB'")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reseed-state", description="单片状态机 CLI")
    p.add_argument("--db", default=DEFAULT_DB, help=f"sidecar 状态库（默认 {DEFAULT_DB}）")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("init", help="登记大包与包内子目录")
    a.add_argument("--pack", required=True)
    a.add_argument("--root", action="append",
                   help="★cross-seed 视角的路径（NAS 上），如 /volume1/video/download/movies/<PACK>。"
                        "多根包（嵌套合集）可重复传，与 --local-root 按序一一对应")
    a.add_argument("--local-root", action="append",
                   help="本机可访问的等价路径（Windows 上填 //YOUR-NAS/video/...），只用于列目录。"
                        "多根包可重复传")
    # ---- 从 .env 派生根（多根包强烈推荐，免手抄 47 条）----
    a.add_argument("--roots-from-env", metavar="ENVFILE",
                   help="从该 .env 的 FARM_SOURCES 派生根（★ 首选：v3 切换后的源清单；"
                        "缺席时退回 DATA_DIRS）。从它派生就不会与源清单漂移")
    a.add_argument("--match", action="append", metavar="KEYWORD",
                   help="配合 --roots-from-env：只取路径里含该关键词的条目（可重复，OR）")
    a.add_argument("--unc-host", metavar="//HOST",
                   help="配合 --roots-from-env：本地根的主机前缀，如 //YOUR-NAS。"
                        "不给则尝试从 scripts/.nasrc 的 NAS_NAME 推断")
    a.add_argument("--nas-prefix", default="/volume1",
                   help="配合 --roots-from-env：NAS 上的卷前缀，用于拼本地根，默认 /volume1")
    a.add_argument("--depth", type=int, default=S.DEFAULT_MAX_DATA_DEPTH,
                   help=f"枚举深度，对应 cross-seed 的 maxDataDepth，默认 {S.DEFAULT_MAX_DATA_DEPTH}。"
                        "★必须与 cross-seed 的配置一致，否则会登记出 cross-seed 根本不搜的幽灵条目")
    a.add_argument("--link-dir")
    a.add_argument("--category")
    a.add_argument("--farm-root",
                   help="v3：cross-seed 实际扫描的硬链接农场根（如 "
                        "/volume1/video/download/reseed/reseed_farm）。设了它，农场路径才能"
                        "归回本包 —— 不设的话 sync 会把整包判成“别的包”而集体降级")
    a.add_argument("--pattern", default="*")
    a.add_argument("--exclude", nargs="*", default=[])
    a.add_argument("--dry-run", action="store_true")
    a.set_defaults(func=cmd_init)

    a = sub.add_parser("farm", help="给已登记的包设置硬链接农场根（v3，不用重扫）")
    a.add_argument("--pack", required=True)
    a.add_argument("--farm-root", nargs="?", default=None,
                   help="不给就只打印当前设置")
    a.add_argument("--clear", action="store_true", help="清空农场根")
    a.set_defaults(func=cmd_farm)

    a = sub.add_parser("sync", help="从 cross-seed.db + 日志 + qB 同步状态")
    a.add_argument("--pack", required=True)
    _add_source_opts(a)
    _add_cadence_opts(a)
    a.add_argument("--quiet-attempts", action="store_true")
    a.set_defaults(func=cmd_sync)

    a = sub.add_parser("report", help="阶段汇总 + 按站重搜周期")
    a.add_argument("--pack", required=True)
    a.add_argument("--indexers")
    _add_cadence_opts(a)
    a.add_argument("--include-cooldown", action="store_true",
                   help="把还没到周期的也计入待搜（用于强制全量重扫）")
    a.add_argument("-v", "--verbose", action="store_true")
    a.set_defaults(func=cmd_report)

    a = sub.add_parser("trend", help="新增做种趋势（按周 × 按站）")
    a.add_argument("--pack", help="不填 = 全部包")
    a.add_argument("--weeks", type=int, default=4, help="回溯几周，默认 4")
    a.add_argument("-v", "--verbose", action="store_true", help="逐周 × 逐站展开")
    a.set_defaults(func=cmd_trend)

    a = sub.add_parser("quota", help="站点额度台账（滚动 24 小时；★不是站点余额）")
    a.add_argument("--db-path", help="cross-seed.db 路径（只读）")
    a.add_argument("--hours", type=float, default=24.0,
                   help="滚动窗口小时数，默认 24")
    a.add_argument("--with-prowlarr", action="store_true",
                   help="额外做来源 B 互校（问 Prowlarr；需要 PROWLARR_API_KEY）")
    a.add_argument("--prowlarr-url",
                   help="如 http://NAS_IP:9696 —— ★宿主机 IP，不是容器名")
    a.add_argument("--prowlarr-api-key",
                   help="Prowlarr 的 API key（★不是 Torznab 那个）。"
                        "更推荐写进 .env 的 PROWLARR_API_KEY，避免留在 shell 历史里")
    a.add_argument("--env", default=str(REPO / ".env"),
                   help="从哪个 .env 读 PROWLARR_API_KEY")
    a.set_defaults(func=cmd_quota)

    a = sub.add_parser("todo", help="导出待搜索清单")
    a.add_argument("--pack", required=True)
    a.add_argument("--indexers")
    a.add_argument("--stage", help="只保留这些阶段，逗号分隔")
    a.add_argument("--limit", type=int, help="每批多少条（分批用）")
    a.add_argument("--batch", type=int, default=None,
                   help="取第几批（配合 --limit）。默认第 1 批；"
                        "一批做完重跑同一条命令即可推进到下一批")
    a.add_argument("--plan", action="store_true",
                   help="只打印分批计划，不导出清单")
    a.add_argument("--out")
    _add_cadence_opts(a)
    a.add_argument("--include-cooldown", action="store_true",
                   help="把还没到周期的也带上（强制全量重扫）")
    a.set_defaults(func=cmd_todo)

    a = sub.add_parser("drive", help="按待办清单控速驱动 cross-seed 重搜，并自动回灌")
    a.add_argument("--pack", required=True)
    _add_source_opts(a)
    _add_cadence_opts(a)
    a.add_argument("--stage", help="只驱动这些阶段，逗号分隔")
    a.add_argument("--limit", type=int, help="每批多少条（分批用）")
    a.add_argument("--batch", type=int, default=None,
                   help="取第几批（配合 --limit）。默认第 1 批；"
                        "一批做完重跑同一条命令即可推进到下一批")
    a.add_argument("--plan", action="store_true",
                   help="只打印分批计划，不发请求")
    a.add_argument("--include-cooldown", action="store_true")
    a.add_argument("--url", help="cross-seed 地址，如 http://NAS_IP:2468")
    a.add_argument("--api-key")
    a.add_argument("--interval", type=float, default=30.0,
                   help="两条 webhook 之间的间隔秒数，默认 30（对齐 cross-seed 的 delay）")
    a.add_argument("--check-every", type=int, default=10,
                   help="每发多少条检查一次索引器退避，默认 10")
    a.add_argument("--backoff-check-secs", type=float, default=60.0,
                   help="★再按秒数检查一次索引器退避，默认 60。实测 429 只 snooze "
                        "55 秒，光靠 --check-every×interval 会整段错过它")
    a.add_argument("--max-wait", type=float, default=1800.0,
                   help="单次退避最多等多少秒，超过就中止，默认 1800")
    a.add_argument("--no-pause-on-backoff", action="store_true",
                   help="不理会退避，闷头发（不建议）")
    a.add_argument("--settle", type=float, default=90.0,
                   help="回灌前等日志静默多少秒，默认 90")
    a.add_argument("--drain-max-wait", type=float, default=3600.0,
                   help="等 cross-seed 忙完的上限，默认 3600")
    a.add_argument("--no-auto-sync", action="store_true", help="打完不回灌")
    a.add_argument("--timeout", type=float, default=30.0)
    a.add_argument("--apply", action="store_true", help="真的发请求（默认 dry-run）")
    a.set_defaults(func=cmd_drive)

    a = sub.add_parser("show", help="看单片的详情与流水")
    a.add_argument("--pack", required=True)
    a.add_argument("--movie", required=True, help="目录名或完整路径")
    a.set_defaults(func=cmd_show)
    return p


def _guard_qbit_url(args) -> None:
    """★ `--qbit-url` 指向 cross-seed 自己 ⇒ **当场说出来**（2026-09-17，`#66`）。

    为什么值得一条断言：这两个 URL 在 README 里**挨着写**（`:1610-1611`）：
        URL=http://192.168.0.7:2468     # cross-seed
        Q="http://192.168.0.7:3060"     # ★必须带 --qbit-url
    ⇒ 抄错一格（或把 `URL` 的值复制到 `--qbit-url`）**不会有任何提示**：
      `orchestrator/qbit_client.py:83` 会拿它去请求 `/api/v2/app/version`，
      而那个路径是 **qB 的**，cross-seed 只会回一句
      `[server] Unknown endpoint: /api/v2/app/version` ——
      **错在调用方，看起来却像 cross-seed 的问题**。
    ★ 真实现场（`#66`）：2026-09-14 11:01:02 / 11:01:31 各一条，那天只有这两条，
      其余每天 0 条 ⇒ 就是一次抄错端口。
    ★ **为什么不"容忍"**：`--qbit-url` 给 cross-seed 的地址**永远是错的用途**
      （cross-seed 不提供 qB 那种接口），没有任何场景需要它成立 ⇒ 直接挡下，
      代价为 0。
    """
    u = getattr(args, "qbit_url", None)
    if not u:
        return
    # 只比"主机:端口"里那个端口 —— scheme/host 写法在两种场合本来就不同
    # （容器内 `cross-seed:2468` vs 宿主机 `192.168.0.7:2468`），拿全串比会漏。
    import re as _re
    m = _re.search(r":(\d+)(?:/|$)", u.strip())
    if m and m.group(1) == "2468":
        _say(f"[!!] `--qbit-url {u}` 看着是 **cross-seed 自己**（端口 2468），"
             f"不是 qB。\n"
             f"     qB 在 **:3060**；拿 2468 当 qB 会让 cross-seed 的日志里出现\n"
             f"     `[server] Unknown endpoint: /api/v2/app/version`——\n"
             f"     **错在调用方，但看起来像 cross-seed 出问题**（见 #66）。\n"
             f"     本次仍继续（只是少一个数据源，见下面那行 `[!!]`）。")


def main(argv: list[str] | None = None) -> int:
    _safe_stdout()
    args = build_parser().parse_args(argv)
    _guard_qbit_url(args)
    try:
        return args.func(args)
    except FileNotFoundError as e:
        # ★ 库不在不是"异常"，是**一条要读的提示** —— 逐字打 traceback 会把它埋掉。
        #   唯一会建库的子命令是 init（它传 create=True），所以走到这里的
        #   一定是只读/改档类命令，本来就该停下。
        _say(f"[错误] {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
