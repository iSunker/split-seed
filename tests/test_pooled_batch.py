# -*- coding: utf-8 -*-
"""**跨包合池** —— 一批横跨多包，`--limit` 是**全局**的，但账**仍按包分段**（`e404b1ca#6` ①）。

为什么要钉这个（用户 2026-09-20 拍：方案 a）
==========================================
原话：「**一批多少种子不能按包定，三包合起来取 N 个**」。

原先批次来源是**按包轮换**（`once_round` 的 `(last_pack_idx+1) % len(packs)`），
每次只跑一个包，于是：

    · `--limit 50` 其实是**每包** 50，三包名义上一轮吃 150
    · 一个小包（`mbf` 只有 4 部、且实测 `Found 0 torrents`）**也占掉一整个批次**
    · 而欠账全在 frds（232 部 SKIPPED）里 ⇒ **轮到 dc / mbf 的那些批纯属浪费额度**

★ 真实读数（`state.db`，2026-09-20）：三包待搜 **565 部**，其中
  `frds` 473（**232 SKIPPED 全部 `attempts=0`** = 从没搜过的欠账）
  `dc` 88、`mbf` 4。⇒ 合池之后**前 50 名精确落在 frds 的 SKIPPED 上**。

★ 本文件钉四个分界：

  ① ★★ **池序 = `TODO_PRIORITY`**（`SKIPPED` → `ERROR` → `PENDING` → `UNMATCHED`）
     —— 欠账**永远**优先，**不管它属于哪个包**。这是合池的全部目的。
  ② ★★ **`limit` 是全局的**：`limit=7` 就是 7 部（哪怕它们来自 2 个包），
     **不是"每包 7 部"**。
  ③ ★★ **按包分段记账**：返回值带 `pack` ⇒ 调用方能按真包名各发一条 batch 事件
     ⇒ 日报「按包聚合」口径**一字不改**。
  ④ ★★ **平手键不带包名**：同级按 `dir_name` 排（全局唯一）。若带上包名，
     就退化成"按包名排序" —— 那正是要摆脱的东西。

★★ 变异验证（**实测过，数字是当场跑出来的**）：
  · 把排序键改成 `(优先级, pack, dir_name)`（= 按包名当平手键）
    ⇒ 红 **2** 条（⑤那两条：同级不再交错）；
  · 把 `pooled[:limit]` 改成"每包各自 `[:limit]`"
    ⇒ 红 **2** 条（③那两条：`limit=4` 拿到 6 部）；
  · 让返回值丢掉 `pack`（改成 `(row, due)` 二元组）
    ⇒ **当场异常中止**（`ValueError: not enough values to unpack`）——
      形状变了，所有断言连跑都跑不到 ⇒ 这比"红几条"**更强**，不改。★ 如实记：它是崩，不是红。

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
    STAGE_ERROR, STAGE_PENDING, STAGE_SKIPPED, STAGE_UNMATCHED,
    TODO_PRIORITY, StateStore,
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
    """`spec`: {包名: [(目录名, stage), …]} —— 直接建库并写死 stage。

    ★ 用 `create=True`（夹具建库；生产默认不建，见 `StateStore.__init__`）。
    ★ stage 直接写 `movie` 表：本文件钉的是**取件顺序**，不是 stage 怎么算出来的
      （那是 `test_seeding_by_name` / `test_sync_empty_sets` 的事）。
    """
    st = StateStore(os.path.join(tempfile.mkdtemp(), "t.db"), create=True)
    for pack, entries in spec.items():
        st.upsert_pack(pack, ROOT, max_depth=2)
        st.register_dirs(pack, [(d, f"{ROOT}/{d}") for d, _ in entries])
        for d, stage in entries:
            st.con.execute("UPDATE movie SET stage=? WHERE pack=? AND dir_name=?",
                           (stage, pack, d))
    st.con.commit()
    return st


print("== ① ★★ 池序 = TODO_PRIORITY：欠账永远优先，**不管它属于哪个包** ==")

# 三个包，故意让**字母序靠后**的包持有欠账 ⇒ 若按包名排，欠账会被压到后面。
st = mkstore({
    "aaa-pack": [("A.2020", STAGE_UNMATCHED), ("B.2020", STAGE_UNMATCHED)],
    "zzz-pack": [("Z.2020", STAGE_SKIPPED), ("Y.2020", STAGE_SKIPPED)],
})
got = st.todo_pooled(["aaa-pack", "zzz-pack"])
ck("前两条是 zzz-pack 的 SKIPPED（欠账压过包名字母序）",
   [(p, r["dir_name"]) for p, r, _ in got[:2]],
   [("zzz-pack", "Y.2020"), ("zzz-pack", "Z.2020")])
ck("★ 池序 == TODO_PRIORITY 的次序",
   [r["stage"] for _, r, _ in got],
   [STAGE_SKIPPED, STAGE_SKIPPED, STAGE_UNMATCHED, STAGE_UNMATCHED])

print()
print("== ② 完整优先级：SKIPPED → ERROR → PENDING → UNMATCHED（跨包混排）==")

st = mkstore({
    "p1": [("u1", STAGE_UNMATCHED)],
    "p2": [("p1row", STAGE_PENDING)],
    "p3": [("s1", STAGE_SKIPPED)],
    "p4": [("e1", STAGE_ERROR)],
})
got = st.todo_pooled(["p1", "p2", "p3", "p4"])
ck("次序 == SKIPPED, ERROR, PENDING, UNMATCHED",
   [r["stage"] for _, r, _ in got],
   [STAGE_SKIPPED, STAGE_ERROR, STAGE_PENDING, STAGE_UNMATCHED])
ck("★ 且与传入的包顺序无关（p1 先传也不影响）",
   [r["stage"] for _, r, _ in st.todo_pooled(["p4", "p3", "p2", "p1"])],
   [STAGE_SKIPPED, STAGE_ERROR, STAGE_PENDING, STAGE_UNMATCHED])
ck("★ TODO_PRIORITY 的定义确实是这个次序",
   [k for k, _ in sorted(TODO_PRIORITY.items(), key=lambda kv: kv[1])],
   [STAGE_SKIPPED, STAGE_ERROR, STAGE_PENDING, STAGE_UNMATCHED])

print()
print("== ③ ★★ `limit` 是**全局**的，不是每包一份 ==")

st = mkstore({
    "p1": [("a1", STAGE_SKIPPED), ("a2", STAGE_SKIPPED), ("a3", STAGE_SKIPPED)],
    "p2": [("b1", STAGE_SKIPPED), ("b2", STAGE_SKIPPED), ("b3", STAGE_SKIPPED)],
})
got = st.todo_pooled(["p1", "p2"], limit=4)
ck("limit=4 ⇒ 共 4 部（**不是** 每包 4 部 = 8 部）", len(got), 4)
ck("★ 且确实横跨了两个包（合池的意义所在）",
   sorted({p for p, _, _ in got}), ["p1", "p2"])
# 对照：若错写成"每包各自截断"，会拿到 6 部（每包 3 部，因为只有 3 部）
ck("★ 反面对照：limit=4 给的不是 6 部", len(got) != 6, True)

got_all = st.todo_pooled(["p1", "p2"])
ck("不给 limit ⇒ 全给（6 部）", len(got_all), 6)

print()
print("== ④ ★★ 按包分段记账：返回值**必须**带包名 ==")

st = mkstore({
    "packA": [("x1", STAGE_SKIPPED)],
    "packB": [("x2", STAGE_SKIPPED)],
})
got = st.todo_pooled(["packA", "packB"])
ck("每项是**三元组** (pack, row, due)", len(got[0]), 3)
ck("包名是**真包名**（不是 'pool' 之类）",
   sorted(p for p, _, _ in got), ["packA", "packB"])
# ★ 按包分组要能直接喂给"每包发一条 batch 事件"那套
by_pack: dict = {}
for p, r, _ in got:
    by_pack.setdefault(p, []).append(r["dir_name"])
ck("★ 能按包分组（= 分段记账的前提）", by_pack, {"packA": ["x1"], "packB": ["x2"]})

print()
print("== ⑤ ★★ 平手键不带包名：同级按 dir_name（全局唯一）==")

# 两个包各有同优先级的行；名字故意交错，若按包名当平手键会分组、按 dir_name 会交错。
st = mkstore({
    "p1": [("m_2", STAGE_SKIPPED), ("m_4", STAGE_SKIPPED)],
    "p2": [("m_1", STAGE_SKIPPED), ("m_3", STAGE_SKIPPED)],
})
got = st.todo_pooled(["p1", "p2"])
ck("同级**按 dir_name 交错**（不是按包聚在一起）",
   [r["dir_name"] for _, r, _ in got], ["m_1", "m_2", "m_3", "m_4"])
ck("★ 反面对照：不是 p1 的四条在前",
   [r["dir_name"] for _, r, _ in got] != ["m_2", "m_4", "m_1", "m_3"], True)

print()
print("== ⑥ 确定性：同一输入两次调用给同一个答案 ==")

ck("两次调用结果相同",
   [(p, r["dir_name"]) for p, r, _ in st.todo_pooled(["p1", "p2"])],
   [(p, r["dir_name"]) for p, r, _ in st.todo_pooled(["p1", "p2"])])

print()
print("== ⑦ 空池 / 单包退化 ==")

st_empty = mkstore({"p1": []})
ck("一个包都没有 ⇒ 空表（不报错）", st_empty.todo_pooled([]), [])
ck("包里没行 ⇒ 空表", st_empty.todo_pooled(["p1"]), [])

st1 = mkstore({"only": [("z1", STAGE_SKIPPED), ("z2", STAGE_PENDING)]})
ck("单包 ⇒ 等价于该包自己的 todo（顺序一致）",
   [r["dir_name"] for _, r, _ in st1.todo_pooled(["only"])],
   [r["dir_name"] for r in st1.todo("only")])

print()
if fails:
    print("FAILED %d:" % len(fails))
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("全过")
