# -*- coding: utf-8 -*-
"""next_sleep() 退避分级自测 —— 站点打喷嚏 vs 站点真在压，间隔不该一样。

不需要网络/真库：直接造 DriveStats 对象喂给 next_sleep()。

★★ 2026-09-16：**档次本身要"钉住谁负责"**，否则档位的存在会被误读成判据。
   `next_sleep` 只管**跑多久再来**，它**不判断**哪个站被限流了、还剩几个健康的
   —— 那是 `drive-loop.py` 的 `wait_out_backoff` 干的（见 `test_backoff.py` ⑨/⑩）。
   这一条分工曾经没有被任何断言钉住，于是"三档"很容易被当成
   "退避真的被处理了"的证据：**它其实只是节奏，不是结论。**
"""
import importlib.util
import pathlib
import sys

# ★ 输出强制 UTF-8：GBK 控制台下 ⑪(U+246A) 编不出去，会在中途炸掉。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

REPO = pathlib.Path(__file__).resolve().parent.parent  # 仓库根
SRC = str(REPO / "data" / "drive-loop" / "scripts" / "drive-loop.py")

spec = importlib.util.spec_from_file_location("drive_loop", SRC)
dl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dl)

S = sys.modules["orchestrator.state"] if "orchestrator.state" in sys.modules else None
# ★ 2026-10-05（B2）：容器代码收进 data/ ⇒ orchestrator / drive-loop 源码都在 data/ 下。
#   先无条件把 data/ 加进搜索路径（下面那个 `if` 只是"还没导入就再补仓库根"）。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "data"))
if S is None:
    sys.path.insert(0, str(REPO))
    from orchestrator import state as S

fails = []


def ck(label, got, want):
    ok = got == want
    print(("  ok  " if ok else " FAIL ") + f"{label}: {got!r}")
    if not ok:
        fails.append(f"{label}: got {got!r} want {want!r}")


def stats(**kw):
    return S.DriveStats(**kw)


MIN = 60
print("== ① 喷嚏：限过一次，但我们只等了 <3 分钟 → 不惩罚，按基准 45 分钟 ==")
for w in (0.0, 55.0, 179.0):
    sec, why = dl.next_sleep(stats(backoff_hits=1, waited_sec=w, ok=50))
    ck(f"  等了 {w:.0f}s → 45 分钟", sec, dl.BASE_SLEEP)
print(f"        理由样例：{why}")

print("\n== ② 中档：等了 3~10 分钟 → 放缓到 1.5 小时 ==")
for w in (180.0, 420.0, 599.0):
    sec, why = dl.next_sleep(stats(backoff_hits=2, waited_sec=w, ok=50))
    ck(f"  等了 {w:.0f}s → 90 分钟", sec, dl.SNOOZE_SLEEP)

print("\n== ③ 重感冒：等了 ≥10 分钟 → 拉满 2 小时 ==")
for w in (600.0, 1200.0, 1800.0):
    sec, why = dl.next_sleep(stats(backoff_hits=3, waited_sec=w, ok=50))
    ck(f"  等了 {w:.0f}s → 120 分钟", sec, dl.BACKOFF_SLEEP)
print(f"        理由样例：{why}")

print("\n== ④ 分级只作用于 backoff_hits —— still_skipped 一律 2 小时 ==")
sec, why = dl.next_sleep(stats(still_skipped=7, backoff_hits=1, waited_sec=5.0))
ck("  有片子没推进 → 120 分钟", sec, dl.BACKOFF_SLEEP)
ck("  理由说的是 still_skipped", "SKIPPED" in why or "跳过" in why, True)

print("\n== ⑤ 老分支没被碰坏 ==")
ck("  aborted → 3 小时",
   dl.next_sleep(stats(aborted="索引器 X 要等到明天"))[0], dl.ABORT_SLEEP)
ck("  健康 + 有新增 → 45 分钟",
   dl.next_sleep(stats(newly_seeding=12))[0], dl.BASE_SLEEP)
ck("  无退避无新增 → 45 分钟", dl.next_sleep(stats(ok=50))[0], dl.BASE_SLEEP)

print("\n== ⑥ 「等了 0 秒却记了一笔」必须落在基准档（本次分级的核心目的）==")
sec, why = dl.next_sleep(stats(backoff_hits=4, waited_sec=0.0))
ck("  4 次命中但一秒没等 → 45 分钟", sec, dl.BASE_SLEEP)

print("\n== ⑦ 三档顺序单调（等得越久，间隔越长）==")
seq = [dl.next_sleep(stats(backoff_hits=1, waited_sec=w))[0]
       for w in (0.0, 200.0, 900.0)]
ck("  45 ≤ 90 ≤ 120", seq, sorted(seq))
ck("  且不全相等", len(set(seq)) > 1, True)

print("\n== ⑧ ★★ 分工钉死：next_sleep() 只看统计量，**不认识站点** ==")
#   ★ 为什么单列一节：上面 ①~⑦ 全都在喂 `backoff_hits`/`waited_sec` 这两个**数**，
#     于是很容易读成「退避这件事被处理了」。事实上：
#       · 判断「谁在退避 / 还有几个健康站 / 要不要不等照发」→ `wait_out_backoff`
#       · 判断「下一批隔多久再来」                              → `next_sleep`（这里）
#     两条腿分开，**少一条就静默**：#115 那次事故正是「站点被限 24 小时、
#     而 next_sleep 只是把间隔拉到 2 小时」—— 节奏对了，**条目一条没发**。
#   ⇒ 判据：同样「等了 700 秒」，**命中几次不影响档位**（档位只看等掉的时间）。
_a, _ = dl.next_sleep(stats(backoff_hits=1, waited_sec=700.0))
_b, _ = dl.next_sleep(stats(backoff_hits=9, waited_sec=700.0))
ck("  命中 1 次 vs 9 次：只按等掉的时间分档（次数不影响档位）", _a, _b)
ck("  且档位就是「等 >=10 分钟」那一档", _a, dl.BACKOFF_SLEEP)
ck("  ★ 它绝不返回 None（None 是「本批没动作」，不是「站点不行」）",
   _a is None, False)
#   ★ 反向：`aborted` 一进就吃满 3 小时、**与退避档位无关** ——
#     即「超过上限先收工」这个**决定不在这里**做，这里只是接住它的结果。
ck("  只给 aborted（不带任何退避统计）→ 仍 3 小时",
   dl.next_sleep(stats(aborted="索引器 X 要等到明天"))[0], dl.ABORT_SLEEP)

print()
if fails:
    print(f"!!! {len(fails)} 个失败")
    for f in fails:
        print("   " + f)
    sys.exit(1)
print("全部通过")
