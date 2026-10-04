# -*- coding: utf-8 -*-
"""**欠账池 vs 常态池** —— 两种"待搜"必须分开报（`e404b1ca#6` ②）。

为什么要钉这个（用户 2026-09-20 拍：「上次没中的往池 A 最后面排」）
================================================================
这两类"待搜"的**性质完全不同**，混在一个数里会**同时误导两头**：

  · **欠账池** = `SKIPPED` / `ERROR` / `PENDING` —— 「**还没搜过**」的欠账。
    `SKIPPED` = 上次被站点退避**秒跳**（请求根本没发出去，所以 `attempts` **不涨**）、
    `PENDING` = 从没搜过。实测 frds **232 部 `SKIPPED` 全部 `attempts=0`**。
    ⇒ 它们是**有限**的：**清一部少一部**，必须在有限轮内清零。
  · **常态池** = `UNMATCHED` —— 「**搜过、没命中**」。这是**稳态**：
    只要片子还在、站上还没出那个单种，它就会一直在 ⇒ **永远清不完**，
    而**清不完不是故障**。它的正常节奏是"按周期重搜"，不是"赶紧消灭"。

★ 混着报的两种误读（各一条）：
  ① 只报合计（实测 565 部）⇒ 读成"欠了 565 部的债"（其实其中 272 部本来就一直在）
  ② 只报欠账 ⇒ 读成"没事了"，而常态池一直在按周期消耗额度

★ 本文件钉四个分界：

  ① ★★ **分档正确**：`SKIPPED`/`ERROR`/`PENDING` → `debt`；`UNMATCHED` → `steady`；
     `SEEDING`/`MATCHED` → `done`（**不进任何池**）。
  ② ★★ **`DONE_STAGES` 不进池** —— 把"正在做种的"算成"待搜"正是 `§26.33`
     那个 30 倍偏差的形状，**必须**有反向断言钉住。
  ③ ★★ **欠账池优先、常态池补位**（用户拍）—— 取件顺序**不变**
     （仍是 `TODO_PRIORITY` 那条全局排序），分池只让它**可读**。
     ⇒ 判据：欠账没取完时，批里**不该出现**常态；欠账取完了，常态才补上。
  ④ ★★ **`ERROR` 算欠账**（不是常态）—— 它是"上次异常、该尽快补"，
     与 `UNMATCHED` 的"搜过没中"性质相反。★ 这条最容易漏。

★★ 变异验证（**实测过，数字是当场跑出来的**）：
  · 把 `DEBT_STAGES` 里的 `STAGE_ERROR` 删掉（= 把异常当常态）
    ⇒ 红 **6** 条（①④ 各一条 + ⑤⑥ 的常量自洽/相加各一条 —— 那几条正是
      "漏一个阶段 = 一个永远数不到的待搜"的守卫）；
  · 把 `pool_of` 的 `done` 分支改成返回 `"steady"`（= `DONE_STAGES` 也算待搜）
    ⇒ 红 **5** 条（★ 含②那三条 —— 这正是 §26.33 那个 30 倍偏差的形状）；
  · 把两池合一（`UNMATCHED` 塞进 `DEBT_STAGES`、`STEADY_STAGES` 清空）
    ⇒ 红 **7** 条（①③④⑥ 各一条，两池分不开就全乱）。

★ 注：`pack_stage_census` 的计数**复用 `pool_of`**（不是自己再写一遍
  `DEBT_STAGES`/`STEADY_STAGES`）—— 若各写一份，改一处忘一处就是
  「计数与分档打架，而没有任何东西会报出来」。变异②之所以能红 5 条，就是靠这个。

素材全部合成：SQLite 在 `tempfile.mkdtemp()` 里现造，没有网络、没有 NAS、没有真库。
"""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
# ★ 2026-10-05（B2）：容器代码收进 data/ ⇒ orchestrator / drive-loop 源码
#   都在 data/ 下。把 data/ 也加进 import 搜索路径（本文件上一行插的是仓库根）。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "data"))

from orchestrator.state import (            # noqa: E402
    ALL_STAGES, DEBT_STAGES, DONE_STAGES, STAGE_ERROR, STAGE_MATCHED,
    STAGE_PENDING, STAGE_SEEDING, STAGE_SKIPPED, STAGE_UNMATCHED,
    STEADY_STAGES, StateStore, pack_stage_census, pool_of,
)

# ★ 输出强制 UTF-8：GBK 控制台下会中途炸掉，而**已过的断言看着全是 ok** —— 极易误判。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = "/vol1/movies"

fails = []


def ck(label, got, want):
    ok = got == want
    print(("  ok  " if ok else " FAIL ") + f"{label}: {got!r}")
    if not ok:
        fails.append(f"{label}: got {got!r} want {want!r}")


def mkstore(spec):
    """`spec`: {包名: [(目录名, stage), …]} —— 直接建库并写死 stage。"""
    st = StateStore(os.path.join(tempfile.mkdtemp(), "t.db"), create=True)
    for pack, entries in spec.items():
        st.upsert_pack(pack, ROOT, max_depth=2)
        st.register_dirs(pack, [(d, f"{ROOT}/{d}") for d, _ in entries])
        for d, stage in entries:
            st.con.execute("UPDATE movie SET stage=? WHERE pack=? AND dir_name=?",
                           (stage, pack, d))
    st.con.commit()
    return st


print("== ① ★★ 分档正确：欠账 / 常态 / 不进池 三档 ==")

ck("SKIPPED → debt", pool_of(STAGE_SKIPPED), "debt")
ck("PENDING → debt", pool_of(STAGE_PENDING), "debt")
ck("ERROR → debt（★ 最容易漏：它是'该尽快补'，不是'搜过没中'）",
   pool_of(STAGE_ERROR), "debt")
ck("UNMATCHED → steady", pool_of(STAGE_UNMATCHED), "steady")
ck("★ SEEDING → done（**不进池**）", pool_of(STAGE_SEEDING), "done")
ck("★ MATCHED → done（**不进池**）", pool_of(STAGE_MATCHED), "done")

print()
print("== ② ★★ `DONE_STAGES` 不进池 —— 这是 §26.33 那个 30 倍偏差的形状 ==")

st = mkstore({"p": [
    ("d1", STAGE_SEEDING),     # 在做种
    ("d2", STAGE_MATCHED),     # 匹配到了
    ("a1", STAGE_SKIPPED),
    ("b1", STAGE_UNMATCHED),
]})
c = pack_stage_census(st, "p")
ck("欠账只数 SKIPPED（1）—— 做种/匹配的**一个都不算**", c["pool_debt"], 1)
ck("常态只数 UNMATCHED（1）—— 做种/匹配的**一个都不算**", c["pool_steady"], 1)
ck("合计 = 2（**不是** 4）", c["pool_total"], 2)
# ★ 反面对照：若把 DONE_STAGES 也算进来，合计会是 4 —— 逐字钉住"不是 4"
ck("★ 反面对照：合计**不是** 4（做种的不算待搜）", c["pool_total"] != 4, True)

print()
print("== ③ ★★ 欠账优先、常态补位（用户拍：不限常态保底）==")

# 欠账 3 部、常态 3 部，limit=2 ⇒ 两批都该是欠账，常态一部都不该出现
st = mkstore({"p": [
    ("a_skip1", STAGE_SKIPPED), ("a_skip2", STAGE_SKIPPED), ("a_pend", STAGE_PENDING),
    ("z_unm1", STAGE_UNMATCHED), ("z_unm2", STAGE_UNMATCHED), ("z_unm3", STAGE_UNMATCHED),
]})
got = st.todo_pooled(["p"], limit=2)
ck("limit=2 ⇒ 全是欠账（常态一部都不出现）",
   [r["stage"] for _, r, _ in got], [STAGE_SKIPPED, STAGE_SKIPPED])
got4 = st.todo_pooled(["p"], limit=4)
ck("limit=4 ⇒ 3 部欠账 + **补 1 部**常态（欠账不够时常态补位）",
   [r["stage"] for _, r, _ in got4],
   [STAGE_SKIPPED, STAGE_SKIPPED, STAGE_PENDING, STAGE_UNMATCHED])
ck("★ 常态**没有保底名额**（limit=2 时它拿到 0，而不是 20%）",
   sum(1 for _, r, _ in got if r["stage"] == STAGE_UNMATCHED), 0)

print()
print("== ④ ★★ `ERROR` 归欠账，不归常态 ==")

st = mkstore({"p": [
    ("e1", STAGE_ERROR),
    ("u1", STAGE_UNMATCHED),
]})
c = pack_stage_census(st, "p")
ck("ERROR 计入欠账", c["pool_debt"], 1)
ck("UNMATCHED 计入常态（**不是**欠账）", c["pool_steady"], 1)
# ★ 取件顺序：ERROR（优先级 1）在 UNMATCHED（优先级 3）之前
got = st.todo_pooled(["p"])
ck("★ 取件时 ERROR 在 UNMATCHED 之前",
   [r["stage"] for _, r, _ in got], [STAGE_ERROR, STAGE_UNMATCHED])

print()
print("== ⑤ 常量自洽：两池 = 除 `DONE_STAGES` 外**全部**阶段 ==")

ck("DEBT ∪ STEADY == ALL_STAGES − DONE",
   set(DEBT_STAGES) | set(STEADY_STAGES),
   set(ALL_STAGES) - set(DONE_STAGES))
ck("DEBT 与 STEADY **不相交**", sorted(DEBT_STAGES & STEADY_STAGES), [])
ck("★ 没有阶段被漏掉（漏一个 = 一个永远数不到的待搜）",
   sorted(DEBT_STAGES | STEADY_STAGES | DONE_STAGES), sorted(ALL_STAGES))

print()
print("== ⑥ 跨包合计：分池计数**能直接相加** ==")

st = mkstore({
    "p1": [("s1", STAGE_SKIPPED), ("u1", STAGE_UNMATCHED)],
    "p2": [("e1", STAGE_ERROR), ("u2", STAGE_UNMATCHED), ("u3", STAGE_UNMATCHED)],
})
d1 = pack_stage_census(st, "p1")
d2 = pack_stage_census(st, "p2")
ck("两包欠账相加 == 各自之和", d1["pool_debt"] + d2["pool_debt"], 2)
ck("两包常态相加 == 各自之和", d1["pool_steady"] + d2["pool_steady"], 3)
ck("合计相加 == 待搜总数（5）", d1["pool_total"] + d2["pool_total"], 5)

print()
if fails:
    print("FAILED %d:" % len(fails))
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("全过")
