# -*- coding: utf-8 -*-
"""`q.py` 的**分层版** —— 把 `todo_pooled` 的 43 在生产里变成 0 的那一层找出来。

★ 为什么还要有第二支探针（`q.py` 已经能给 43 了）
------------------------------------------------
  `q.py` 证明了「用挂载那份代码算，池子是 43」，而生产同一份代码、同一个库、
  同一组参数，日志写着「没有待搜索项」。**这两个数必须有一个是错的**，
  而`q.py` 与生产之间**已知**的差别有三处（q.py 用 `__new__` 绕过 `__init__`、
  不传 `include_cooldown`、不传 `cadence_by_indexer`）⇒ 单看总数分不清是哪一处。

  ⇒ 这一支**逐层数**，每层都对生产**逐字复刻**：
     ① `StateStore(args.db)` —— **走真 `__init__`**（`q.py` 绕过了它；
        而 `__init__` 里有 `executescript(SCHEMA)` 与 `_migrate()`，
        **它可能改数据** —— 这正是"绕过"这个简化可能丢掉的东西）
     ② `movies(pack)` 行数
     ③ `todo(pack)` 行数            ← 生产走这条
     ④ `todo_detail(pack)` 行数
     ⑤ `todo_pooled(packs)` 行数     ← 生产真调的这条
  ★ 哪一层从 >0 掉到 0，就是那一层。**不再靠猜**。

★ 与 `q.py` 同样的版本闸门（回读 md5）—— **不通过就当场炸**，绝不吐"看着像读数"的东西。
  理由见 `q.py` 顶部：上一版探针读到过 `/app` 里 Sep 18 的旧 state.py。

★ 用法（NAS，一条短命令）：
    docker exec reseed-drive-loop python /volume2/docker_ssd/reseed/q2.py
  ★ 它会**打开可写库**（因为要复刻 `__init__`）—— `__init__` 里有 `commit()`，
    但不改任何业务数据（`SCHEMA`/`_migrate` 都是幂等的建表/补列）。
    ⇒ 若你不想让它碰库：`-e Q_SKIP_INIT=1`（那就退回 `q.py` 的绕法，但**层数会失真**）。
"""
import hashlib
import os
import sys

#: ★★ 强制 UTF-8 —— **必须在任何 print 之前**。本文件里全是 ⇒ ★ ✓ ↳ 这类多字节符，
#:   而 Windows 控制台是 GBK ⇒ 打到第一个 ⇒ 就 `UnicodeEncodeError` **把探针打挂**，
#:   且**挂在哪一行取决于数据** ⇒ 最坏是"算出了 43 却没打出来"（`ERR-AI-10` 那个形状）。
#:   ★ 这一条是**本机实测**出来的：不加它，本文件在写它的机器上第 110 行就炸。
#:   生产（`run-resident.sh`）也 export 同一个变量，理由一字不差。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ORCH = ("/volume2/docker_ssd/reseed"
        "/drive-loop/orchestrator")
DB = ("/volume2/docker_ssd/reseed"
      "/drive-loop/hlink/state.db")

# ★★ 与 drive-loop.py 同一条路径规则：它 sys.path[0]=<drive-loop>，
#    然后 `from orchestrator import state`。这里把两者都放进去，保证解析结果一致。
DRIVE = ("/volume2/docker_ssd/reseed/drive-loop")
sys.path = [p for p in sys.path if p not in ("/app", "")]

#: ★★ 本机可验性：容器路径 + **本文件上溯的仓库根**。
#:   只写容器那套的后果实测过 —— 探针在**写它的机器上** `ModuleNotFoundError`，
#:   于是只能"裸奔上线"（`probe-pool.py` 顶部那条教训，我在这里又踩了一次）。
_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))        # <repo>/scripts/diag → <repo>
for _c in (ORCH, DRIVE, os.path.join(_REPO, "orchestrator"), _REPO):
    if os.path.isdir(_c) and _c not in sys.path:
        sys.path.insert(0, _c)

# ★ 库里那台用 --db；本机实测用本仓的 hlink/state.db
if os.environ.get("DB_OVERRIDE"):
    DB = os.environ["DB_OVERRIDE"]
elif not os.path.isdir(DRIVE):
    DB = os.path.join(_REPO, "hlink", "state.db")

import state as S  # noqa: E402

REAL = os.path.realpath(S.__file__)
WANT = os.path.realpath(ORCH + "/state.py")
MD5_WANT = "46d7c4626674d9933fdf94e116c854eb"

if os.environ.get("Q_SKIP_MD5") != "1":
    assert REAL == WANT, (
        "★ 读到的是别的 state.py：%s\n  期望：%s\n  ⇒ 读数不可信，先修这个。" % (REAL, WANT))
    with open(REAL, "rb") as fh:
        _got = hashlib.md5(fh.read()).hexdigest()
    if _got != MD5_WANT:
        raise SystemExit(
            "★★ state.py 的 md5 对不上 —— 读数不可信，拒绝继续。\n"
            "   实际：%s\n   期望：%s\n   路径：%s\n"
            "   ⇒ 若你确认这是**有意推上去**的新版本：更新本文件的 MD5_WANT 再跑。\n"
            "   ★ 只想先看结构、不判版本：加 -e Q_SKIP_MD5=1（★ 那就别再信结论）。"
            % (_got, MD5_WANT, REAL))

PK = os.environ.get("PK", "frds-top250-2024")
LIMIT = int(os.environ.get("LIMIT", "50"))          # ★ 默认 50：复刻**生产**，不是 q.py 的 500
IX = [s.strip() for s in os.environ.get(
    "IX", "HDtime,HDFans,NanyangPT,BTSCHOOL").split(",") if s.strip()]
CAD = S.parse_cadence(os.environ.get("CAD") or None)   # ★ 生产传的是这个（可能非空！）

print("=" * 64)
print("代码   :", REAL)
print("md5    :", MD5_WANT, "（★ 回读 md5 才是判据）")
print("库     :", DB)
print("包     :", PK)
print("额度   :", LIMIT, "（★ 复刻生产；q.py 用的是 500）")
print("站名表 :", IX)
print("cadence_by_indexer :", CAD, "← ★ 生产传的是它；q.py **没传**")
print("周期   :", S.DEFAULT_CADENCE_DAYS, "天")
print("Q_SKIP_INIT =", os.environ.get("Q_SKIP_INIT"))
print("=" * 64)

import datetime  # noqa: E402
import sqlite3  # noqa: E402

now = datetime.datetime.now()
print("now    :", now)

# ---- 打开：★ 默认走真 __init__（生产就是这么开的）-------------------------
if os.environ.get("Q_SKIP_INIT") == "1":
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=1")
    st = S.StateStore.__new__(S.StateStore)
    st.con = con
    st.path = DB
    print("★ Q_SKIP_INIT=1 ⇒ 绕过了 __init__（与 q.py 同形；层数可能与生产不同）")
else:
    st = S.StateStore(DB)          # ★ 与生产逐字相同
    st.dir_name = os.path.dirname(DB)   # 生产那边由 CLI 设；这里给个无害值
    con = st.con
    print("★ 走了真 __init__（executescript(SCHEMA) + _migrate()，幂等）")

# ★ 把生产实际会传的全部参数**集中在一处** —— 后面每一层都用同一个 kwargs
#   ⇒ 任何两层之间的差异都只能来自**函数内部**，不可能来自参数不同。
KW = dict(indexers_now=IX, include_cooldown=False, cadence_days=S.DEFAULT_CADENCE_DAYS,
          cadence_by_indexer=CAD, now=now)


def line(tag, n, extra=""):
    print("  %-34s %6s %s" % (tag, n, extra))


print("\n-- 逐层（每一层都用同一份 KW；层间差异只能来自函数内部）--")

# ① pack 表里登记了什么
packs_reg = [r["name"] for r in con.execute("SELECT name FROM pack ORDER BY name")]
print("  ① pack 表登记：", packs_reg)
print("     在本题的 packs 列表里吗：", PK in packs_reg,
      "  ← ★ 若为 False ⇒ movies() 会返回空，**池子必然为 0**")

# ② movies
try:
    m = st.movies(PK)
    line("② movies(PK)", len(m))
except Exception as e:
    print("  ② movies 抛了：%r" % (e,)); m = []

# ③ todo —— 生产 todo_pooled 走的就是它
try:
    t = st.todo(PK, **KW)
    line("③ todo(PK)", len(t), "← ★ 生产这条路上的第一层过滤")
except Exception as e:
    import traceback
    print("  ③ todo 抛了："); traceback.print_exc(); t = []

# ④ todo_detail
try:
    td = st.todo_detail(PK, **KW)
    line("④ todo_detail(PK)", len(td))
except Exception as e:
    import traceback
    print("  ④ todo_detail 抛了："); traceback.print_exc(); td = []

# ⑤ todo_pooled —— 生产真调的这条
try:
    pooled = st.todo_pooled([PK], limit=LIMIT, **KW)
    line("⑤ todo_pooled([PK])", len(pooled), "← ★★ 生产真调的就是这条")
except Exception as e:
    import traceback
    print("  ⑤ todo_pooled 抛了："); traceback.print_exc(); pooled = []

# ⑥ 同⑤但 limit=500（对照 q.py 的口径）
try:
    p500 = st.todo_pooled([PK], limit=500, **KW)
    line("⑥ todo_pooled(limit=500)", len(p500), "（q.py 的口径）")
except Exception as e:
    print("  ⑥ 抛了：%r" % (e,))

print("\n-- 若③为 0 而②不为 0：把 todo() 的分支数一遍（★ 定位到 stage）--")
if m and not t:
    from collections import Counter
    stg = Counter(r["stage"] for r in m)
    print("  movies 的 stage 分布：", dict(stg))
    print("  DONE_STAGES      =", sorted(S.DONE_STAGES),
          " ⇒ 这些不搜：", sum(v for k, v in stg.items() if k in S.DONE_STAGES))
    print("  RETRY_NOW_STAGES =", sorted(S.RETRY_NOW_STAGES),
          " ⇒ 这些**立刻**搜：", sum(v for k, v in stg.items() if k in S.RETRY_NOW_STAGES))
    print("  COOLDOWN_STAGES  =", sorted(S.COOLDOWN_STAGES),
          " ⇒ 这些**按周期**搜：", sum(v for k, v in stg.items() if k in S.COOLDOWN_STAGES))
    print("  ★ 三类加起来 = %d；不等于 movies 总数(%d) 的那些 stage 就是被静默丢掉的：%s"
          % (sum(v for k, v in stg.items()
                 if k in S.DONE_STAGES or k in S.RETRY_NOW_STAGES or k in S.COOLDOWN_STAGES),
             len(m),
             dict(Counter(r["stage"] for r in m
                          if r["stage"] not in S.DONE_STAGES
                          and r["stage"] not in S.RETRY_NOW_STAGES
                          and r["stage"] not in S.COOLDOWN_STAGES))))

print("\n-- ⑦ 对照：q.py 的算法（绕过 __init__、手工 due_indexers）--")
try:
    con2 = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    con2.row_factory = sqlite3.Row
    con2.execute("PRAGMA query_only=1")
    n = 0
    for r in con2.execute("SELECT * FROM movie WHERE pack=?", (PK,)).fetchall():
        if r["stage"] in S.DONE_STAGES:
            continue
        if S.due_indexers(S._d(r["indexer_seen"]), IX,
                          cadence_days=S.DEFAULT_CADENCE_DAYS, now=now):
            n += 1
    line("⑦ q.py 口径（手工 due_indexers）", n, "（q.py 报 43）")
    con2.close()
except Exception as e:
    print("  ⑦ 抛了：%r" % (e,))

print("\n完成。★ 判据：③/⑤ 与生产的「没有待搜索项」是否一致 —— 一致才说明复刻成功；\n"
      "        若 q2 给 43 而生产给 0，则**不是参数问题**，是**运行期数据**问题。")
