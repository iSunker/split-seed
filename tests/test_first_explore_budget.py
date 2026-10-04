# -*- coding: utf-8 -*-
"""**首次探索预算** —— 「从没搜过的站」必须限流，否则它吃光 `--limit`（`e404b1ca#6` ③）。

为什么要钉这个（实测，2026-09-20）
==================================
`due_indexers()` 原先只有一条规则：**「从没在这个站搜过 → 无条件该搜」**。
于是**每一批**都把这批 `UNMATCHED` 判成"到期"。`state.db` 实测分布：

    缺 ['BTSCHOOL','HDtime','NanyangPT']            n=234
    缺 ['BTSCHOOL','HDFans','HDtime','NanyangPT']   n=38

⇒ 272 部**每批都占满 `--limit 50` 的名额**。用户看到的
「limit 50 就一直把上次没匹配进的加到这次」的**机制真身就是它** ——
不是"优先级排错"，是**这几个站对它们永远判该搜**。

★ 本文件钉三个**看着像放宽、其实是限流**的分界：

  ① ★★ **每轮最多放 `first_explore_budget` 个"没搜过的站"** —— 这是这次修的全部内容。
     ★ 反向断言也要有：**够周期的站不受预算限制**（预算防的是无限欠账，
       不是压下正常轮换；混在一起会让"早就该重搜"的站被挤掉）。
  ② ★★ **收敛性**：预算只**推迟**、不**放弃** —— 同一个站下一轮仍然会被放出来。
     判据是「同一输入、只改预算 ⇒ 能选出不同的站，且合起来覆盖全集」。
  ③ ★★ **确定性**：同一输入两次调用给**同一个答案**（否则"这批搜了谁"复现不出来）。
  ④ ★★ **`None` = 不限**（老调用点语义），**不是 0** —— 0 会让没搜过的站**永远排不上**。

★★ 变异验证（**实测过，数字是当场跑出来的**）：
  · 把 `picked_never = sorted(never)[:int(first_explore_budget)]` 改回 `sorted(never)`
    （= 旧行为）⇒ 红 **8** 条（①~⑥ 各组几乎全红）；
  · 把"够周期的"也塞进预算里（`(fresh + sorted(never))[:budget]`）
    ⇒ 红 **6** 条（★ 含①那条反向断言：够周期的站被挤掉）；
  · 把 `None` 当成 0
    ⇒ 红 **2** 条（④ 那两条）。

素材全部合成：没有 SQLite、没有网络、没有 NAS —— 这个函数是**纯函数**。
"""
import pathlib
import sys
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
# ★ 2026-10-05（B2）：容器代码收进 data/ ⇒ orchestrator / drive-loop 源码
#   都在 data/ 下。把 data/ 也加进 import 搜索路径（本文件上一行插的是仓库根）。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "data"))

from orchestrator.state import (              # noqa: E402
    FIRST_EXPLORE_BUDGET, cadence_for, due_indexers,
)

# ★ 输出强制 UTF-8：GBK 控制台下会中途炸掉，而**已过的断言看着全是 ok** —— 极易误判。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

T_NOW = datetime(2026, 9, 20, 12, 0, 0)


def ts(days_ago: float) -> str:
    return (T_NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


#: 演示用的站池 —— 与实测那三个缺口同名（★ 顺序照抄 `--indexers` 的形态）
POOL = ["HDtime", "HDFans", "NanyangPT", "BTSCHOOL"]

fails = []


def ck(label, got, want):
    ok = got == want
    print(("  ok  " if ok else " FAIL ") + f"{label}: {got!r}")
    if not ok:
        fails.append(f"{label}: got {got!r} want {want!r}")


print("== ① 每轮最多放 N 个「没搜过的站」（默认 %d）==" % FIRST_EXPLORE_BUDGET)

# 实测形状：缺 3 个站（234 部是这个形状）
seen_a = {"HDtime": ts(100)}                  # 只有 HDtime 搜过（且早就够周期了）
got = due_indexers(seen_a, POOL, now=T_NOW)
ck("缺 3 个站 ⇒ 只放 1 个没搜过的（+ 够周期的 HDtime）", got, ["HDtime", "BTSCHOOL"])

# ★ 反向断言：**够周期的站不受预算限制**。四个站全够周期 ⇒ 四个都该搜，一个都不许被压。
#   ★ 期望按 **POOL 的顺序**（够周期的站保持池顺序），而不是字母序 ——
#     `fresh` 是"照池子顺序挑出来的"，排序会把它变成另一种语义。
all_fresh = {ix: ts(100) for ix in POOL}
got_all = due_indexers(all_fresh, POOL, now=T_NOW)
ck("四个站全够周期 ⇒ 四个全放（预算不压正常轮换）", got_all, list(POOL))

# ★ 混合：3 个够周期 + 1 个没搜过 ⇒ 4 个全放（没搜过的那个也在预算内）
mixed = {"HDtime": ts(100), "HDFans": ts(100), "NanyangPT": ts(100)}
got_mixed = due_indexers(mixed, POOL, now=T_NOW)
ck("3 个够周期 + 1 个没搜过 ⇒ 4 个全放", got_mixed, ["HDtime", "HDFans", "NanyangPT", "BTSCHOOL"])

print()
print("== ② ★★ 收敛性：预算只**推迟**、不**放弃** ==")

# 全是"没搜过"⇒ 一轮只放 1 个。
seen_none: dict = {}
r1 = due_indexers(seen_none, POOL, now=T_NOW)
ck("一轮只放 1 个", len(r1), 1)
# 模拟"那一轮搜过了" ⇒ 下一轮放的是**下一个**没搜过的站。
seen_after_r1 = dict(seen_none)
for ix in r1:
    seen_after_r1[ix] = ts(0)                 # 刚搜过
r2 = due_indexers(seen_after_r1, POOL, now=T_NOW)
ck("下一轮放的是另一个没搜过的站（不是同一个）", len(r2), 1)
ck("且确实换了站", r2 != r1, True)
ck("两轮合起来是 2 个**不同**的站", len(set(r1 + r2)), 2)

# 走完全程：4 轮正好把 4 个站覆盖完（一个都不会被丢下）
seen = dict(seen_none)
covered = []
for _ in range(len(POOL)):
    due = due_indexers(seen, POOL, now=T_NOW)
    covered += due
    for ix in due:
        seen[ix] = ts(0)
ck("4 轮之后 4 个站**全都搜过**（没有站被永久丢掉）", sorted(covered), sorted(POOL))

print()
print("== ③ ★★ 确定性：同一输入两次调用给同一个答案 ==")

r_a = due_indexers(seen_none, POOL, now=T_NOW)
r_b = due_indexers(seen_none, POOL, now=T_NOW)
ck("两次调用结果相同", r_a, r_b)
# ★ 换一个**传入顺序**也必须给同一答案：池的顺序是 `--indexers` 给的，不该影响选择
r_c = due_indexers(seen_none, list(reversed(POOL)), now=T_NOW)
ck("池顺序反过来 ⇒ 仍然是同一批（选择与传入顺序无关）", r_c, r_a)

print()
print("== ④ ★★ `None` = 不限（老调用点语义），**不是 0** ==")

r_none = due_indexers(seen_a, POOL, now=T_NOW, first_explore_budget=None)
ck("budget=None ⇒ 没搜过的站**全放**（= 旧行为）", r_none, ["HDtime"] + sorted(set(POOL) - {"HDtime"}))

r_zero = due_indexers(seen_a, POOL, now=T_NOW, first_explore_budget=0)
ck("budget=0 ⇒ 一个没搜过的站都不放（★ 这才叫 0，与 None 不同）", r_zero, ["HDtime"])
ck("★ None 与 0 **必须不同**（混同会让老调用点静默丢事）", r_none != r_zero, True)

print()
print("== ⑤ 默认值来自常量（别在调用处硬编码 1）==")

ck("默认预算 == FIRST_EXPLORE_BUDGET", FIRST_EXPLORE_BUDGET, 1)
ck("不传参 == 传 FIRST_EXPLORE_BUDGET",
   due_indexers(seen_a, POOL, now=T_NOW),
   due_indexers(seen_a, POOL, now=T_NOW, first_explore_budget=FIRST_EXPLORE_BUDGET))

print()
print("== ⑥ 与周期规则的**交互**：够周期的站优先，且不被预算挤掉 ==")

# ★ 实测形状：HDtime 够周期了、另外三个没搜过 ⇒ 应该是 HDtime + 1 个没搜过的
seen_hd = {"HDtime": ts(cadence_for("HDtime") + 1)}
got_hd = due_indexers(seen_hd, POOL, now=T_NOW)
ck("够周期的 HDtime **在**结果里（没被预算挤掉）", "HDtime" in got_hd, True)
ck("且只多带 1 个没搜过的", len(got_hd), 2)

# ★ 没够周期：HDtime 刚搜过 ⇒ 它不该出现，只放 1 个没搜过的
seen_fresh_hd = {"HDtime": ts(1)}
got_fresh = due_indexers(seen_fresh_hd, POOL, now=T_NOW)
ck("HDtime 刚搜过 ⇒ 不在结果里", "HDtime" in got_fresh, False)
ck("但仍放 1 个没搜过的", len(got_fresh), 1)

print()
if fails:
    print("FAILED %d:" % len(fails))
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("全过")
