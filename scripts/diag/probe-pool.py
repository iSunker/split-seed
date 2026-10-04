# -*- coding: utf-8 -*-
"""容器内**只读**探针：`--pool` 报「没有待搜索项」到底是怎么算出来的。

★ 为什么要写成文件：此前给的两版都是 `docker exec sh -c '...'` 里塞 python，
  而**转义 + heredoc 缩进**先后把两版都搞坏了（`IndentationError`）。
  ⇒ 写成文件 `docker cp` 进去跑 —— 零转义、零引号嵌套。

★ 安全性（逐条，不是"应该没问题"）：
  · **只读**：`StateStore` 以 `mode=ro` 打开（见下），不写任何东西；
  · **不发请求**：不碰网络、不读 `torznab`、不读 `apikey`；
  · **不选 `url`/`apikey` 列**：只数行数；
  · 输出里**没有 URL**（`redact()` 也没必要用，因为压根不碰）。
  · 不写日志、不写状态文件。

用法（在 NAS 上）：
  docker cp scripts/diag/probe-pool.py reseed-drive-loop:/tmp/probe-pool.py
  sudo docker exec reseed-drive-loop python /tmp/probe-pool.py
  # 想换名单/额度：
  sudo docker exec -e PROBE_PACKS="frds-top250-2024" -e PROBE_LIMIT=500 \
       reseed-drive-loop python /tmp/probe-pool.py
"""
import datetime
import os
import pathlib
import sqlite3
import sys

#: ★★ 强制 UTF-8 —— **必须在任何 print 之前**。片名与校验符（✓ ↳）全是多字节，
#:   而 Windows 控制台 / 容器 locale 常是 GBK 或 POSIX(C) ⇒ 打印到一半
#:   `UnicodeEncodeError` **把探针打挂**，而**挂在哪一行取决于数据** ⇒
#:   最坏情况是"读到了要的读数却没打出来"（`ERR-AI-10` 那个形状）。
#:   ★ 入口脚本 `run-resident.sh` 也 export 了同一个变量，理由一字不差。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

DB = os.environ.get(
    "PROBE_DB",
    "/volume2/docker_ssd/reseed/drive-loop/hlink/state.db")
#: ★★ 路径候选**两套都给** —— 容器里一套、仓库里一套。
#:   只写容器那套的后果是：**这个探针在写它的机器上跑不起来** ⇒ 只能"裸奔上线"，
#:   而本仓的纪律是**先在本机验一遍再推**（我第一版正是只写了容器那套，
#:   于是在本机直接 `ModuleNotFoundError: state` —— 探针自己成了不可验的东西）。
#:   ★ 候选顺序：`PROBE_ROOT`（显式）→ 容器路径 → 本文件上溯的仓库根。
HERE = pathlib.Path(__file__).resolve().parent
_CANDS = []
if os.environ.get("PROBE_ROOT"):
    _CANDS.append(pathlib.Path(os.environ["PROBE_ROOT"]))
_CANDS.append(pathlib.Path("/volume2/docker_ssd/reseed"
                           "/drive-loop"))
#: 本文件在 <repo>/scripts/diag/ ⇒ 上两级 = 仓库根；orchestrator 也在仓库根下
_CANDS.append(HERE.parent.parent / "orchestrator")
_CANDS.append(HERE.parent.parent)                     # 仓库根（hlink/state.db 在这儿）
for _c in _CANDS:
    if _c.is_dir() and str(_c) not in sys.path:
        sys.path.insert(0, str(_c))
print("sys.path 头两项:", sys.path[:2])

import state as S  # noqa: E402

print("=" * 68)
print("now        =", datetime.datetime.now())
#: ★ 用 `now(timezone.utc)` 而不是 `utcnow()` —— 后者 deprecated，
#:   且它返回的是 **naive** datetime，**差不出时区偏移**（naive - aware 会抛）。
#:   ★ 但这里要的正是"本机本地时间 vs UTC"那个差 ⇒ 两个都取 **aware** 再相减；
#:     差值 ≠ 0 ⇒ 时区没配错（★ 这一步就是 §26.46 那次错判的**自动判据**）。
_utc = datetime.datetime.now(datetime.timezone.utc)
print("now(utc)   =", _utc)
print("★ 本地偏移 =", datetime.datetime.now().astimezone().utcoffset())
print("TZ env     =", os.environ.get("TZ"))
print("db         =", DB)
_p = pathlib.Path(DB)
print("db exists  =", _p.exists(), _p.stat().st_size if _p.exists() else "-")

PACKS = [s.strip() for s in os.environ.get(
    "PROBE_PACKS", "frds-top250-2024").split(",") if s.strip()]
LIMIT = int(os.environ.get("PROBE_LIMIT", "500"))
#: ★ 与 `run-resident.sh` 的参数行**逐字一致**（漏一个站就少一个到期源）
IX = [s.strip() for s in os.environ.get(
    "PROBE_INDEXERS", "HDtime,HDFans,NanyangPT,BTSCHOOL").split(",") if s.strip()]
print("PACKS      =", PACKS)
print("LIMIT      =", LIMIT)
print("INDEXERS   =", IX)
print("=" * 68)

# 只读打开（★ 不跑 schema 迁移：那是写操作）
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
con.execute("PRAGMA query_only=1")

print("\n-- ① pack 表里登记了什么（对照 PACKS）--")
try:
    rows = con.execute("SELECT name FROM pack ORDER BY name").fetchall()
    for r in rows:
        print("   pack:", r["name"],
              "  ★在 PACKS 里" if r["name"] in PACKS else "  （登记了但没被驱动）")
except Exception as e:  # noqa: BLE001
    print("   !! pack 表读不出来:", e)

print("\n-- ② 每个包的 movie 行数 / 按 stage 分布 --")
STAGES = {}
for pk in PACKS:
    try:
        n = con.execute("SELECT COUNT(*) c FROM movie WHERE pack=?",
                        (pk,)).fetchone()["c"]
    except Exception as e:  # noqa: BLE001
        print(f"   movie({pk}) 读不出来: {e}")
        continue
    print(f"   movie({pk:<18}) = {n}")
    try:
        for r in con.execute(
                "SELECT stage, COUNT(*) c FROM movie WHERE pack=? GROUP BY stage"
                " ORDER BY c DESC", (pk,)).fetchall():
            STAGES[r["stage"]] = STAGES.get(r["stage"], 0) + r["c"]
            print(f"        stage={r['stage']:<12} {r['c']}")
    except Exception as e:  # noqa: BLE001
        print("        stage 分组失败:", e)
print("   合计 stage:", STAGES)

print("\n-- ③ indexer_seen 里实际出现过的**站名全集**（★ 对照 --indexers）--")
try:
    seen_names = {}
    for r in con.execute(
            "SELECT indexer_seen FROM movie WHERE indexer_seen IS NOT NULL"
            " AND indexer_seen NOT IN ('', '{}')").fetchall():
        for k in S._d(r["indexer_seen"]):
            seen_names[k] = seen_names.get(k, 0) + 1
    for k, v in sorted(seen_names.items(), key=lambda t: -t[1]):
        mark = "  ✓在 --indexers 里" if k in IX else "  ★★ 不在 --indexers 里"
        print(f"   {k:<28} {v:>5} 部{mark}")
    missing = [k for k in IX if k not in seen_names]
    if missing:
        print("   ★★ --indexers 里有、但库里从没搜过的:", missing)
except Exception as e:  # noqa: BLE001
    print("   读不出来:", e)

print("\n-- ④ ★ 核心：todo_pooled 三层各数一遍（每一层都可能把数清零）--")
st = S.StateStore.__new__(S.StateStore)      # 绕过 __init__（它会跑迁移 = 写）
st.con = con
#: 目录：从 drive-loop.py 那边抄口径（不 import 它，避免 argparse 副作用）
st.dir_name = ""
st.movies = lambda pk, **kw: con.execute(
    "SELECT * FROM movie WHERE pack=?", (pk,)).fetchall()

now_dt = datetime.datetime.now()
total_pairs = 0
for pk in PACKS:
    rows = con.execute("SELECT * FROM movie WHERE pack=?", (pk,)).fetchall()
    n_done = n_todo = 0
    per_ix = {}
    n_no_ix = 0
    for r in rows:
        if r["stage"] in S.DONE_STAGES:
            n_done += 1
            continue
        due = S.due_indexers(S._d(r["indexer_seen"]), IX,
                             cadence_days=S.DEFAULT_CADENCE_DAYS,
                             now=now_dt)
        if due:
            n_todo += 1
            for ix in due:
                per_ix[ix] = per_ix.get(ix, 0) + 1
        else:
            n_no_ix += 1
    total_pairs += n_todo
    print(f"   {pk:<18} 共 {len(rows):>4}｜DONE {n_done:>4}｜**该搜 {n_todo:>4}**"
          f"｜该搜但无到期站 {n_no_ix:>4}")
    if per_ix:
        print("        ↳ 到期分布:", dict(sorted(per_ix.items(), key=lambda t: -t[1])))

print(f"   ⇒ 全池该搜合计 = {total_pairs}（`--limit {LIMIT}` 会截到 {min(total_pairs, LIMIT)}）")

try:
    pooled = st.todo_pooled(PACKS, indexers_now=IX, cadence_days=S.DEFAULT_CADENCE_DAYS,
                            limit=LIMIT, now=now_dt)
    print(f"\n   todo_pooled(...) 返回 = {len(pooled)}")
    if pooled:
        print("   前 5 条:", [(pk, r["dir_name"]) for pk, r, _ in pooled[:5]])
except Exception as e:  # noqa: BLE001
    import traceback
    print("\n   ★★ todo_pooled 抛了:")
    traceback.print_exc()

print("\n-- ⑤ 那两部『登记了却没被驱动』的包（dc/mbf）还在不在 --")
for pk in ("dc-collection", "mbf"):
    try:
        n = con.execute("SELECT COUNT(*) c FROM movie WHERE pack=?",
                        (pk,)).fetchone()["c"]
        print(f"   {pk:<18} movie 行 = {n}  ★ PACKS 里{'有' if pk in PACKS else '没有'}")
    except Exception as e:  # noqa: BLE001
        print(f"   {pk}: {e}")

con.close()
print("\n完成（全程只读、零请求）。")
