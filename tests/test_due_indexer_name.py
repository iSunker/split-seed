# -*- coding: utf-8 -*-
"""`due_indexers` / `next_due_at` 必须**按站名主干**查 `indexer_seen`。

为什么要钉（2026-09-22 实测，与 `--pool` 恒零**并列**的第二处）
================================================================
`indexer_seen` 的键是 **cross-seed 的原值**，带 caps 后缀：
     `'NanyangPT (南洋)'`
而 `--indexers` 是人手写的短名：
     `'NanyangPT'`

`due_indexers()` 里是**裸比**：
     `last = _parse_ts(indexer_seen.get(ix))`
⇒ `seen.get('NanyangPT')` → `None` ⇒ 判「从没搜过」⇒ **永远到期**。

★ 实测读数（`state.db`，2026-09-22，q.py）：
     库里站名全集：HDFans 483 / **NanyangPT (南洋) 472** / BTSCHOOL 288 /
                   (未记录) 240 / HDtime 207
     站名表：HDtime,HDFans,NanyangPT,BTSCHOOL
     ★★ 「站名表里有、库里从没出现过的：['NanyangPT']」← 就是它
     该搜 43 部，到期站分布 `{'BTSCHOOL': 1, 'NanyangPT': 37, 'HDtime': 5}`
     ⇒ **43 部里有 37 部的到期站是那个永远匹配不上的幽灵名**

★★ 这个 bug 的**形状**（比它本身更值得记）
----------------------------------------
  同一个仓里**已经有一把正确的尺**：`norm_indexer_name('NanyangPT (南洋)') → 'nanyangpt'`。
  它被用在**两处**：`check_indexers()` 的名单对账、`DriveSession` 的退避判定。
     ⇒ 那两处**工作正常**（自检打「索引器自检通过」，退避判定正确）
     ⇒ 于是日志上**一切正常**，唯独池子算错。
  **`due_indexers` / `next_due_at` 这两处漏用了它。**
  ⇒ 判据的形状：**同一仓里"同一个概念"有两把尺时，要数清有几处用它** ——
    漏用的那处不会报错，只会让数**慢慢偏**（这里偏成"每轮都到期"）。

钉六条
======
  ① ★★★ 括号后缀：`seen={'NanyangPT (南洋)': T}`、`--indexers=['NanyangPT']`
     ⇒ 该站**不该**被判"从没搜过"（未到周期 ⇒ 不进 `due`）。
  ② ★★ 大小写：`'nanyangpt'` vs `'NanyangPT'` 同理。
  ③ ★★ 真到期仍然到期：把时间戳推到 14 天前 ⇒ **该**进 `due`（判据不能恒假）。
  ④ ★ `next_due_at` 同一条尺（它是同一个 bug 的第二处）。
  ⑤ ★★ 阴性对照：拿**裸比**的实现喂同一组输入 ⇒ 判据说"错"。
  ⑥ ★ 端到端：把整个 `due_indexers` 的分布跑一遍，幽灵名不该贡献到期站。

★ 素材全部合成：`indexer_seen` 是手搓 dict，零网络、零 NAS、零真库，不碰 `url`/`apikey`。
"""
import pathlib
import sys
from datetime import datetime, timedelta

# ★ 强制 UTF-8 —— **必须在任何 print 之前**。本文件里全是 ⓪①★⇒⇒ 这类多字节符，
#   而 Windows 控制台是 GBK ⇒ 打到第一个就 UnicodeEncodeError **把测试打挂**，
#   且挂在取决于内容的行上（`ERR-AI-10` 形状）。
#   ★ 本 session 已在两支探针上各栽一次（q2.py、本文件），**这是第三次** ——
#     所以把它当成新文件的**默认开头**，别再等它炸。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
# ★ 2026-10-05（B2）：容器代码收进 data/ ⇒ orchestrator / drive-loop 源码
#   都在 data/ 下。把 data/ 也加进 import 搜索路径（本文件上一行插的是仓库根）。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "data"))

from orchestrator import state as S  # noqa: E402

fails = []


def ck(label, got, want):
    ok = got == want
    print(("  ok  " if ok else " FAIL ") + "%s: %r" % (label, got))
    if not ok:
        fails.append("%s: got %r want %r" % (label, got, want))


NOW = datetime(2026, 9, 22, 12, 0, 0)
FRESH = (NOW - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
OLD = (NOW - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

print("=== ⓪ 先钉那把**已存在的尺**（不然下面不知道在比什么）===")
ck("norm_indexer_name('NanyangPT (南洋)') → 'nanyangpt'",
   S.norm_indexer_name("NanyangPT (南洋)"), "nanyangpt")
ck("norm_indexer_name('NanyangPT') → 'nanyangpt'",
   S.norm_indexer_name("NanyangPT"), "nanyangpt")
ck("★ 两者主干相同 ⇒ 本来就该被当作同一个站",
   S.norm_indexer_name("NanyangPT (南洋)") == S.norm_indexer_name("NanyangPT"), True)
ck("全角括号也归一（caps 里常见）",
   S.norm_indexer_name("NanyangPT（南洋）"), "nanyangpt")

print()
print("=== ① ★★★ 括号后缀：库里带后缀、--indexers 写短名 ===")
seen = {"NanyangPT (南洋)": FRESH}
got = S.due_indexers(seen, ["NanyangPT"], now=NOW)
ck("①a 昨天刚搜过 ⇒ **不该**到期（修好后为 []）", got, [])
ck("①b ★ 阴性对照：裸比实现喂同一输入 ⇒ 判它『从没搜过』",
   (seen.get("NanyangPT") is None), True)

print()
print("=== ② ★★ 大小写 ===")
ck("②a 'nanyangpt' 与库键 'NanyangPT (南洋)' 也应视为同一站",
   S.due_indexers({"NanyangPT (南洋)": FRESH}, ["nanyangpt"], now=NOW), [])
ck("②b 反向：库键小写、--indexers 大写",
   S.due_indexers({"nanyangpt": FRESH}, ["NanyangPT"], now=NOW), [])

print()
print("=== ③ ★★ 时间戳真到期时**仍然**要到期（判据不能恒假）===")
ck("③a 30 天前搜过 ⇒ 该到期",
   S.due_indexers({"NanyangPT (南洋)": OLD}, ["NanyangPT"], now=NOW), ["NanyangPT"])
ck("③b 阳性对照：把周期调成 100 天 ⇒ 前一条又不该到期",
   S.due_indexers({"NanyangPT (南洋)": OLD}, ["NanyangPT"],
                  cadence_days=100, now=NOW), [])

print()
print("=== ④ ★ next_due_at 是同一 bug 的第二处 ===")
ck("④a 昨天搜过 ⇒ 下一次到期**不是** None（None = 现在就该搜）",
   S.next_due_at({"NanyangPT (南洋)": FRESH}, ["NanyangPT"], now=NOW) is None, False)
ck("④b 30 天前搜过 ⇒ 返回 None（现在就该搜）",
   S.next_due_at({"NanyangPT (南洋)": OLD}, ["NanyangPT"], now=NOW), None)

print()
print("=== ⑤ ★★ 阴性对照：拿裸比逻辑当判据，确认它会判错 ===")


def naked_due(seen, indexers_now, now):
    """**事故原码的逻辑**（裸比，不归一）。用来证明①的判据不是恒真。"""
    out = []
    for ix in indexers_now:
        if seen.get(ix) is None:
            out.append(ix)
    return out


ck("⑤a 裸比逻辑：带后缀的库键 ⇒ 误判『从没搜过』",
   naked_due({"NanyangPT (南洋)": FRESH}, ["NanyangPT"], NOW), ["NanyangPT"])
ck("⑤b 真实 due_indexers（修好后）：同一输入 ⇒ 不误判",
   S.due_indexers({"NanyangPT (南洋)": FRESH}, ["NanyangPT"], now=NOW), [])
ck("⑤c ★ 两者结论**必须不同** —— 这才证明判据抓的是真缺陷",
   naked_due({"NanyangPT (南洋)": FRESH}, ["NanyangPT"], NOW)
   != S.due_indexers({"NanyangPT (南洋)": FRESH}, ["NanyangPT"], now=NOW), True)

print()
print("=== ⑥ ★ 分布口径：幽灵名不该贡献到期站 ===")
# 模拟 q.py 实测的那 43 部里最典型的形状：
# 库里只有带后缀的键，--indexers 是短名，全部 1 天前搜过。
_batch = [{"NanyangPT (南洋)": FRESH, "HDtime": FRESH} for _ in range(5)]
_due_any = [S.due_indexers(s, ["HDtime", "NanyangPT"], now=NOW) for s in _batch]
ck("⑥a 5 部、两站都刚搜过 ⇒ 一个都不该到期",
   [d for d in _due_any if d], [])
ck("⑥b ★ 对照：把 'NanyangPT' 从表里去掉，结论**不变**（说明它本来就不该贡献）",
   [S.due_indexers(s, ["HDtime"], now=NOW) for s in _batch], _due_any)

print()
print("=== ⑦ ★ (未记录) 那个幽灵：不该被当成一个真站反复到期 ===")
ck("⑦a UNKNOWN_INDEXER 常量值", S.UNKNOWN_INDEXER, "(未记录)")
# 库里既有具名站、又有 (未记录) 占位时，_clean 会丢掉占位（见 state.py:2226）
ck("⑦b _clean 丢掉占位（有具名站时）",
   S._clean({"HDtime", S.UNKNOWN_INDEXER}), {"HDtime"})
ck("⑦c 只有一个占位时不丢（那时它是唯一信息）",
   S._clean({S.UNKNOWN_INDEXER}), {S.UNKNOWN_INDEXER})

print()
if fails:
    print("FAILED %d:" % len(fails))
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("全过")
