# -*- coding: utf-8 -*-
"""**卡种观测** —— 匹配到了却下不来的那些（`e404b1ca#7`）。

为什么要钉这个（用户 2026-09-20 的场景：「**涌入上百个种子做不下**」）
=================================================================
用户问「搜到了 stalledDL/checkingDL/卡999 就别再搜了」。**前半句代码本来就是
这么设计的**（`MATCHED` 已在 `DONE_STAGES` ⇒ 搜到就不重搜）。**缺的是"看得见"**：

「匹配到却下不来」这批片子**从所有现有观测里同时漏出去**：

    · 不进 `UNMATCHED`（它们不是"没搜到"）
    · 不算 `SEEDING`（进度里既不加分子也不加分母）
    · 不卡 999（那条要求 `progress ≥ 0.99`，而"做不下"的大多没到）

⇒ 这正是 `§26.33` 那个形状：**观测坏了，下游全是假红/假绿**。

★ 本文件钉四个分界：

  ① ★★ **四档全认**：`stalledDL` / `metaDL` / `checkingDL` / `error`。
     ★ `stalledDL` 是**实测现成的那 3 个**（用户场景的阳性对照）。
  ② ★★ **`by_state` 只含命中过的档** —— 不把 4 个档都填 0。
     "没提到" 与 "提到了是 0" 是两件事（`n/a ≠ 0 ≠ 没事`）。
  ③ ★★ **`n` 从 `by_state` 来，不从 `hashes` 来** —— 两者**可以不等**
     （`hash` 字段缺失时那条仍该被数到）。若用 `len(hits)`，一个缺 hash 的
     条目会让 `n` 悄悄少 1，而 `by_state` 还是对的 ⇒ **同一封信里两个数互相打脸**。
  ④ ★★ **绝不认「还在动」的档** —— `downloading` / `uploading` / `stalledUP` /
     `checkingUP` 都**不许**进（把健康的算成卡住 = 天天响假警）。

★★ 变异验证（**实测过，数字是当场跑出来的**）：
  · 把 `STALLED_STATES` 里的 `"metaDL"` 删掉
    ⇒ 红 **3** 条（①的分档断言 + ② + ⑥ 各一条：磁力链没种子那档看不见了）；
  · 把 `by_state` 改成"4 档全填 0"
    ⇒ 红 **6** 条（②那三条 + ⑤两条 + ①一条 —— "没提到"被写成了"提到了是 0"）；
  · 把 `n` 改成 `len(hits)`
    ⇒ 红 **3** 条（③那三条：★ 正是"同一封信里两个数互相打脸"那个形状）；
  · 把 `"stalledUP"` 也塞进 `STALLED_STATES`
    ⇒ 红 **2** 条（④那两条：把在做种的算成"卡住" = 天天响假警）。

素材全部合成：判据是**纯函数**，没有网络、没有 qB、没有 NAS。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
# ★ 2026-10-05（B2）：容器代码收进 data/ ⇒ orchestrator / drive-loop 源码
#   都在 data/ 下。把 data/ 也加进 import 搜索路径（本文件上一行插的是仓库根）。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "data"))

from orchestrator.state import STALLED_STATES, qb_stalled_band   # noqa: E402

# ★ 输出强制 UTF-8：GBK 控制台下会中途炸掉，而**已过的断言看着全是 ok** —— 极易误判。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

fails = []


def ck(label, got, want):
    ok = got == want
    print(("  ok  " if ok else " FAIL ") + f"{label}: {got!r}")
    if not ok:
        fails.append(f"{label}: got {got!r} want {want!r}")


def T(state, h=None):
    """造一条 qB 的 torrents/info 条目。`h=None` ⇒ **没有 hash 字段**。"""
    d = {"state": state, "progress": 0.5, "amount_left": 1000,
         "last_activity": 0, "name": "Some.Movie.2024"}
    if h is not None:
        d["hash"] = h
    return d


print("== ① ★★ 四档全认（`stalledDL` 是实测现成的那 3 个）==")

ck("STALLED_STATES 含 stalledDL", "stalledDL" in STALLED_STATES, True)
ck("STALLED_STATES 含 metaDL（磁力链没种子）", "metaDL" in STALLED_STATES, True)
ck("STALLED_STATES 含 checkingDL（校验中断）", "checkingDL" in STALLED_STATES, True)
ck("STALLED_STATES 含 error（qB 自己标了错）", "error" in STALLED_STATES, True)

# ★ 实测形状：3 个 stalledDL
ts = [T("stalledDL", "a"), T("stalledDL", "b"), T("stalledDL", "c")]
r = qb_stalled_band(ts)
ck("实测形状：3 条 stalledDL ⇒ n=3（★ 用户场景的阳性对照）", r["n"], 3)
ck("分档：stalledDL 3", r["by_state"], {"stalledDL": 3})
ck("分母跟着走（3）", r["total"], 3)

print()
print("== ② ★★ `by_state` 只含命中过的档（不把 4 档都填 0）==")

r = qb_stalled_band([T("metaDL", "m1")])
ck("只有 metaDL 命中 ⇒ by_state 只有它一格", r["by_state"], {"metaDL": 1})
ck("★ 没有 'stalledDL': 0 这种格（没提到 ≠ 提到了是 0）",
   "stalledDL" not in r["by_state"], True)
r0 = qb_stalled_band([T("uploading", "u1")])
ck("一条都不命中 ⇒ by_state 是**空字典**（不是 4 个 0）", r0["by_state"], {})
ck("且 n=0", r0["n"], 0)

print()
print("== ③ ★★ `n` 从 `by_state` 来，不从 `hashes` 来 ==")

# ★ 那条 checkingDL **没有 hash 字段** —— 它仍该被数到
ts = [T("stalledDL", "a"), T("stalledDL", "b"), T("checkingDL", None)]
r = qb_stalled_band(ts)
ck("有 hash 的 2 条 + 无 hash 的 1 条 ⇒ n=3（★ 不是 2）", r["n"], 3)
ck("by_state 之和 == n（两个数**不许打架**）", sum(r["by_state"].values()), r["n"])
ck("但 hashes 只有 2 个（缺 hash 的进不了基线）", len(r["hashes"]), 2)
# ★ 反面对照：若用 len(hits)，n 会是 2 —— 钉住"不是 2"
ck("★ 反面对照：n **不是** len(hashes)", r["n"] != len(r["hashes"]), True)

print()
print("== ④ ★★ 绝不认「还在动」的档 ==")

for healthy in ("downloading", "uploading", "stalledUP", "checkingUP",
                "forcedDL", "pausedDL", "queuedDL"):
    ck(f"{healthy} **不算**卡住",
       qb_stalled_band([T(healthy, "h")])["n"], 0)

# ★ 混合：健康的 + 卡的 ⇒ 只数卡的
mixed = [T("uploading", "u1"), T("stalledUP", "u2"), T("stalledDL", "s1")]
r = qb_stalled_band(mixed)
ck("混合列表 ⇒ 只数卡的那 1 条", r["n"], 1)
ck("分母仍是**全部** 3 条（分母不许被过滤）", r["total"], 3)

print()
print("== ⑤ 边界：空列表 / 字段缺失 / 不抛异常 ==")

ck("空列表 ⇒ n=0, total=0", qb_stalled_band([]), {"n": 0, "by_state": {},
                                                  "hashes": [], "total": 0})
ck("None ⇒ 同上（调用方可能传 None）",
   qb_stalled_band(None), {"n": 0, "by_state": {}, "hashes": [], "total": 0})
# ★ 没有 state 字段的条目 ⇒ 当"不命中"，**不许抛**
ck("没有 state 字段 ⇒ 当不命中（不抛）",
   qb_stalled_band([{"hash": "x"}])["n"], 0)
ck("state=None ⇒ 当不命中", qb_stalled_band([{"state": None, "hash": "x"}])["n"], 0)

print()
print("== ⑥ `hashes` 有序、去重语义 ==")

r = qb_stalled_band([T("stalledDL", "z"), T("error", "a"), T("metaDL", "m")])
ck("hashes **升序**（让基线与断言都稳定）", r["hashes"], ["a", "m", "z"])
# ★ 定序是**基线比对**的前提：无序的列表会让"新增/缩回"看起来天天在变
ck("同一输入两次调用给同一 hashes 顺序",
   qb_stalled_band([T("stalledDL", "z"), T("error", "a")])["hashes"],
   qb_stalled_band([T("error", "a"), T("stalledDL", "z")])["hashes"])

print()
if fails:
    print("FAILED %d:" % len(fails))
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("全过")
