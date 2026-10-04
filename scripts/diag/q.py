# -*- coding: utf-8 -*-
"""用**挂载那份** state.py 算 frds 的待搜池。只读。

★ 用法（在 NAS 上，一条短命令）：
    docker exec reseed-drive-loop python /volume2/docker_ssd/reseed/q.py

★ 可变的部分走环境变量，**不用改文件**：
    -e PK=dc-collection       换包
    -e LIMIT=50               换额度
    -e IX=HDtime,BTSCHOOL     换站名表

★★ 为什么第一段是 assert 而不是 try：
  上一版探针读到了 /app 里 **Sep 18 的旧 state.py**（镜像 COPY 的），
  于是把旧代码算出的数当成了生产读数报出去 —— 全部作废。
  ⇒ 这一版**在读库之前**先钉死"我读的是哪份代码"，
    不对就当场炸，绝不吐出"看着像读数"的东西。
  ★ 本仓的纪律：判据不是"它说读到了"，而是**回读 md5**（见 drive-loop.Dockerfile）。
    挂载那份的 md5 应为 **46d7c462**（2026-09-22 实测）。
"""
import hashlib
import os
import sys

ORCH = ("/volume2/docker_ssd/reseed"
        "/drive-loop/orchestrator")
DB = ("/volume2/docker_ssd/reseed"
      "/drive-loop/hlink/state.db")

# ★★ 把 /app 与 '' 从 sys.path 剔掉 —— 否则 import state 会解析到镜像里那份。
sys.path = [p for p in sys.path if p not in ("/app", "")]
sys.path.insert(0, ORCH)

import state as S  # noqa: E402

REAL = os.path.realpath(S.__file__)
WANT = os.path.realpath(ORCH + "/state.py")

#: ★★★ 期望的 md5。**这是真正的判据** —— 见下面为什么路径相等不够。
#:   ★★ 它会**随每次部署变** ⇒ 变了**先确认那是你有意推的版本**，再改这里。
#:     2026-09-22 实测这条闸门的**实用价值**：事故当天它连着拦下两次对话 ——
#:       46d7c462  事故前的生产版
#:       61c79ac6  **本次修复后**的生产版（_lookup_seen 那次，2026-09-22 03:57）
#:     ★ 被拦是**正确**行为，不是故障：它准确地告诉你"生产代码变了，
#:       你上次的读数不该再当读数用"。⇒ 别把 MD5_WANT 改成"自动取当前值"，
#:       那会把这个判据**变成恒真**（`ERR-AI-09` 的正对照要求）。
#:   ★ 改这里的**前置条件**：`grep -c _lookup_seen <那份 state.py>` 应为 6
#:     （即它确实是含本次修复的版本），而不是"随便一个数"。
MD5_WANT = "61c79ac6d3b60e47ddfe8249ee213f27"

if os.environ.get("Q_SKIP_MD5") != "1":
    assert REAL == WANT, (
        "★ 读到的是别的 state.py：%s\n  期望：%s\n  ⇒ 读数不可信，先修这个。" % (REAL, WANT))
    with open(REAL, "rb") as fh:
        _got = hashlib.md5(fh.read()).hexdigest()
    if _got != MD5_WANT:
        # ★★★ 为什么不能只比**路径**：我本机做过阴性对照 —— 造一份"路径写对、
        #   内容却是假的" state.py，路径断言**照样放行**，程序继续跑到别处才炸。
        #   ⇒ 路径相等**证不了**"这是我以为的那个文件"（同 `ERR-AI-09`：
        #     防假绿的装置本身可能是假绿的）。⇒ 必须回读**内容**的 md5。
        raise SystemExit(
            "★★ state.py 的 md5 对不上 —— 读数不可信，拒绝继续。\n"
            "   实际：%s\n   期望：%s\n"
            "   路径：%s\n"
            "   ⇒ 若你确认这是**有意推上去**的新版本：更新本文件的 MD5_WANT 再跑；\n"
            "     否则说明挂载/镜像里的那份不是我以为的那版（同 §26.5 那次事故）。\n"
            "   ★ 只想先看结构、不判版本：加 -e Q_SKIP_MD5=1（★ 那就别再信结论）。"
            % (_got, MD5_WANT, REAL))


PK = os.environ.get("PK", "frds-top250-2024")
LIMIT = int(os.environ.get("LIMIT", "500"))
IX = [s.strip() for s in os.environ.get(
    "IX", "HDtime,HDFans,NanyangPT,BTSCHOOL").split(",") if s.strip()]

print("=" * 60)
print("代码   :", REAL)
print("md5    :", MD5_WANT, "（★ 判据是这一行 —— 回读 md5，不是'它说读到了'）")
if os.environ.get("Q_SKIP_MD5") == "1":
    print("★★ Q_SKIP_MD5=1 ⇒ **跳过了版本判据**：下面的数只能当结构参考，别当结论。")
print("包     :", PK)
print("额度   :", LIMIT)
print("站名表 :", IX)
print("周期   :", S.DEFAULT_CADENCE_DAYS, "天 / 探索预算",
      getattr(S, "FIRST_EXPLORE_BUDGET", "?"))
print("=" * 60)

import datetime  # noqa: E402
import sqlite3  # noqa: E402
from collections import Counter  # noqa: E402

con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
con.row_factory = sqlite3.Row
con.execute("PRAGMA query_only=1")

# ---- ① stage 分布 ----------------------------------------------------------
rows = con.execute("SELECT * FROM movie WHERE pack=?", (PK,)).fetchall()
print("\n① %s：movie 行数 = %d" % (PK, len(rows)))
stg = Counter(r["stage"] for r in rows)
for k, v in sorted(stg.items(), key=lambda t: -t[1]):
    mark = "  ← DONE_FINAL（不占额度）" if k in S.DONE_STAGES else ""
    print("     stage=%-12s %5d%s" % (k, v, mark))
print("     DONE_STAGES =", sorted(S.DONE_STAGES))

# ---- ② 站名全集（★ 与代码版本无关，读的是库里的原值）----------------------
print("\n② indexer_seen 里出现过的站名（★ 库里的原值）")
seen = Counter()
for r in rows:
    for k in S._d(r["indexer_seen"]):
        seen[k] += 1
for k, v in seen.most_common():
    print("     %-30s %5d 部%s" % (k, v,
          "  ✓在站名表里" if k in IX else "  ★★ 不在站名表里"))
miss = [k for k in IX if k not in seen]
if miss:
    print("     ★★ 站名表里有、库里从没出现过的：", miss)

# ---- ③ due_indexers 逐部算（★ 池子就是这一层）-----------------------------
print("\n③ 逐部算 due_indexers")
now = datetime.datetime.now()
todo = nodue = done = 0
per_ix = Counter()
per_stage = Counter()
for r in rows:
    if r["stage"] in S.DONE_STAGES:
        done += 1
        continue
    due = S.due_indexers(S._d(r["indexer_seen"]), IX,
                         cadence_days=S.DEFAULT_CADENCE_DAYS, now=now)
    if due:
        todo += 1
        per_stage[r["stage"]] += 1
        for i in due:
            per_ix[i] += 1
    else:
        nodue += 1
print("     共 %d ｜ DONE %d ｜ ★该搜 %d ｜ 该搜但无到期站 %d"
      % (len(rows), done, todo, nodue))
print("     该搜的 stage 分布：", dict(per_stage))
print("     到期站分布：      ", dict(per_ix))
print("     ⇒ --limit %d 下本批取 %d" % (LIMIT, min(todo, LIMIT)))
if todo > LIMIT:
    print("     ★★ tail 饿死：%d 部永远取不到" % (todo - LIMIT))
else:
    print("     ✓ 池子一轮能清空")

# ---- ④ 真调 todo_pooled（★ 这才是进程实际走的那条路）----------------------
print("\n④ todo_pooled(...) —— 与 run_round 同参数")
try:
    st = S.StateStore.__new__(S.StateStore)   # 绕过 __init__（它跑 schema 迁移 = 写）
    st.con = con
    pooled = st.todo_pooled([PK], indexers_now=IX,
                            cadence_days=S.DEFAULT_CADENCE_DAYS, limit=LIMIT)
    print("     返回 =", len(pooled))
    for pk, row, due in pooled[:10]:
        print("       %-12s %-42s due=%s"
              % (row["stage"], row["dir_name"][:42], due))
except Exception:
    import traceback
    print("     ★★ 抛了：")
    traceback.print_exc()

con.close()
print("\n完成（只读：mode=ro + query_only；零网络请求）。")
