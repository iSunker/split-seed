# -*- coding: utf-8 -*-
"""**池子扫 `pack` 表**（去掉「包的闸门」）—— `2026-10-05` 用户拍板。

为什么要钉这个（用户原话）
==========================
「包只是个框架范围，把里面的全部单片硬链接到文件夹里面，每一片做个登记就好了。
哪些片匹配上了，标记掉，移出待搜。每一片搜索过几次，记录下来。每个站搜索到一定
数量还匹配不上就移动到 14 天搜一次的慢队列去。」

核查结论：这套流水线**早就全有了** —— 缺的只是**驱动循环按"名单"逐个包轮换**
（`--packs` / `PACKS_DEFAULT` / `DRIVE_PACKS`），**不在名单里的包 `todo(pack)`
压根不被调用** ⇒ 里面的片"连够没够 14 天都不会被问一次" ⇒ `dc-collection` /
`mbf` 被**冻住**（不是变慢，是**停电**）。

修法：池子的**包源**改成「`pack` 表里**全部已登记的包**」
（`drive-loop.py::pool_packs()` ⇒ `StateStore.packs()`）。机制一字未改，
只是"扫哪些包"从"名单"变成"表"。

★★ 本文件钉四个分界（都是"悄悄退化回原样"的失效模式）：

  ① ★★ **不在名单里的包，其到期片也进池** —— 这就是"去掉闸门"本身。
     老机制下 `todo_pooled(["frds-top250-2024"])` 扫不到 `mbf`/`dc` 的任何片。
  ② ★★ **`DONE_STAGES`（SEEDING/MATCHED）的片不进池** —— "匹配上的移出待搜"。
     去掉闸门**不是**"把一切都塞进池"，完成度仍由每片自己的 `stage` 说了算。
  ③ ★★ **`UNMATCHED` 按 14 天慢档**：够 14 天（或该站从没搜过）才重入池，
     没够就不重入。这是用户说的"搜不到就移到 14 天搜一次的慢队列"，**它一直在**。
  ④ ★★ **空 `pack` 表 ⇒ 抛 `NoPacksRegistered`，不静默算空** —— 空池有两种成因
     （真搜完 vs 库读错/表被清），读数都是 0、后果相反；合成一个数就是
     「`n/a` = 0 = 没事」（`ERR-AI-03`）⇒ 表空必须**显式报错**，不能假报「🎉 全干完」。

★ 数法/行形：本文件只打印 `  ok  ` / ` FAIL `（`tests/README.md` 的形状 A）。
★ 素材全部合成：SQLite 在 `tempfile.mkdtemp()` 里现造，没有网络、没有 NAS、没有真库。
"""
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
# ★ 2026-10-05（B2）：容器代码收进 data/ ⇒ orchestrator / drive-loop 源码都在 data/ 下。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "data"))

from orchestrator.state import (            # noqa: E402
    DEFAULT_CADENCE_DAYS,
    DONE_STAGES,
    STAGE_MATCHED,
    STAGE_PENDING,
    STAGE_SEEDING,
    STAGE_SKIPPED,
    STAGE_UNMATCHED,
    StateStore,
)

# ★ 输出强制 UTF-8：GBK 控制台下会中途炸掉，而**已过的断言看着全是 ok** —— 极易误判。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ★★ `pool_packs` / `require_packs` / `NoPacksRegistered` 都在 drive-loop.py 里。
#    直接 import 那个模块会拉起一堆 side-effect（日志/路径探测）——
#    本文件只**离线**钉"池源来自 pack 表"这件事，所以用**源码级**确认 + 复刻同一行：
#      drive-loop.py::pool_packs 的实现就是 `[r["name"] for r in st.packs()]`。
DL_PY = pathlib.Path(__file__).resolve().parent.parent / "data" / "drive-loop" \
    / "scripts" / "drive-loop.py"


def _dl_pool_packs_src() -> str:
    """从 drive-loop.py 源码里抠出 `pool_packs` 的函数体（不做 import，避免 side-effect）。"""
    import re
    src = DL_PY.read_text(encoding="utf-8")
    m = re.search(r"^def pool_packs\(.*?\n(?:.*\n)*?(?=^def |^class )",
                  src, re.M)
    return m.group(0) if m else ""


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
    ★ stage 直接写 `movie` 表：本文件钉的是**池源与取件资格**，
      不是 stage 怎么算出来的（那是 `test_seeding_by_name` / `test_sync_empty_sets` 的事）。
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


def set_seen(st, pack, dir_name, seen: dict):
    """把某片的 `indexer_seen` 写死（`{站名: ISO 时刻}`）—— 用来造"够没够 14 天"。"""
    import json
    st.con.execute("UPDATE movie SET indexer_seen=? WHERE pack=? AND dir_name=?",
                   (json.dumps(seen, ensure_ascii=False), pack, dir_name))
    st.con.commit()


def pool_of(st):
    """复刻 `drive-loop.py::pool_packs`：池源 = **全部登记的包**（不传名单）。

    ★ 用同一行 `[r["name"] for r in st.packs()]` —— 与生产同源。
      本文件**不 import drive-loop.py**（side-effect 多），只做**源码级**确认
      （见 ⓪），行为则在 state 层复刻。
    """
    return [r["name"] for r in st.packs()]


print("== ⓪ ★★ 前提：`pool_packs()` 的实现就是「扫 pack 表」，且名单机制已不在 ==")

_src = _dl_pool_packs_src()
ck("前提：drive-loop.py 里有 pool_packs 函数", bool(_src), True)
ck("★★ pool_packs 扫的是 `st.packs()`（= pack 表全表）",
   'st.packs()' in _src, True)
ck("★★ pool_packs 取的是 `r[\"name\"]`（包名列表）",
   'r["name"]' in _src, True)
# ★ 反向：名单机制的入口必须**不在**（去掉闸门 = 这三样一起退休）。
#   ★★ 注意判据形状：`PACKS_DEFAULT` / `packs_from_arg` / `DRIVE_PACKS`
#      **允许**出现在"它们已退休"的说明注释里（本仓风格：退休要留痕）——
#      所以查的是"**有没有定义**"（赋值 / `def`），不是"字面上还提没提"。
#      （踩过：先写成 `sym not in _full`，把注释里的字面也判红 —— 假红。）
_full = DL_PY.read_text(encoding="utf-8")
import re as _re_top  # noqa: E402
#   ★ 只认**代码行**（行首非 `#`）里的定义，注释行不算。
_code_lines = [l for l in _full.splitlines() if not l.lstrip().startswith("#")]
_code = "\n".join(_code_lines)
ck("★ 名单入口 `PACKS_DEFAULT` 已从 drive-loop.py 消失（代码里不再定义）",
   not _re_top.search(r"^\s*PACKS_DEFAULT\s*=", _code, _re_top.M), True)
ck("★ 名单入口 `packs_from_arg` 已从 drive-loop.py 消失（代码里不再定义）",
   not _re_top.search(r"^\s*def\s+packs_from_arg\b", _code, _re_top.M)
   and not _re_top.search(r"packs_from_arg\s*\(", _code), True)
# ★ DRIVE_PACKS 只允许出现在**说明它退休了**的注释里 —— 不许有任何赋值。
ck("★ DRIVE_PACKS 没有赋值（只许出现在退休说明的注释里）",
   not _re_top.search(r"^\s*DRIVE_PACKS\s*=", _code, _re_top.M), True)
# ★ `--packs` / `--pool` 不再被 argparse 定义。
ck("★ argparse 不再定义 --packs", 'add_argument("--packs"' not in _code
   and "add_argument('--packs'" not in _code, True)
ck("★ argparse 不再定义 --pool", 'add_argument("--pool"' not in _code
   and "add_argument('--pool'" not in _code, True)

print()
print("== ① ★★ 不在「名单」里的包，其到期片也进池（= 去掉闸门本身）==")

# 模拟"老名单"只驱动 frds；但池源现在是**全表** ⇒ mbf / dc 的片**也**进池。
st = mkstore({
    "frds-top250-2024": [("F.2020", STAGE_SKIPPED)],
    "mbf":              [("M.2021", STAGE_PENDING)],
    "dc-collection":    [("D.2022", STAGE_PENDING)],
})
_pool = pool_of(st)
ck("★★ 池源 = pack 表**全部**登记的包（不是只 frds）",
   sorted(_pool), ["dc-collection", "frds-top250-2024", "mbf"])

got = st.todo_pooled(_pool)
_gets = sorted((p, r["dir_name"]) for p, r, _ in got)
ck("★★ mbf / dc 的片**也在池里**（老名单下它们查不到）",
   _gets, [("dc-collection", "D.2022"), ("frds-top250-2024", "F.2020"), ("mbf", "M.2021")])

# ★ 反面对照：拿**只有 frds**的名单去取，mbf/dc 的片**取不到** ⇒ 证明"闸门"真实存在过。
_old = sorted(r["dir_name"] for _, r, _ in st.todo_pooled(["frds-top250-2024"]))
ck("★ 反面对照：旧「名单」取法下 mbf/dc 的片缺席（这正是要修的停电）",
   _old, ["F.2020"])

print()
print("== ② ★★ `DONE_STAGES`（SEEDING/MATCHED）不进池 —— 匹配上的移出待搜 ==")

ck("前提：DONE_STAGES 恰好是 {SEEDING, MATCHED}",
   set(DONE_STAGES), {STAGE_SEEDING, STAGE_MATCHED})

st = mkstore({
    "pk": [("done_matched", STAGE_MATCHED),
           ("done_seeding", STAGE_SEEDING),
           ("todo_pending", STAGE_PENDING)],
})
got = st.todo_pooled(pool_of(st))
_gets = sorted(r["dir_name"] for _, r, _ in got)
ck("★★ MATCHED / SEEDING 都**不在**池里，只有 PENDING 在",
   _gets, ["todo_pending"])
# ★ 反向：别把"去掉闸门"理解成"全都塞进池"。
ck("★ 反面对照：池里**没有** done_matched / done_seeding",
   ("done_matched" in _gets) or ("done_seeding" in _gets), False)

print()
print("== ③ ★★ `UNMATCHED` 的 14 天慢档：够周期 / 从没搜过才重入池 ==")

now = datetime(2026, 10, 5, 12, 0, 0)
ix = ["MTeam"]

# (a) 从没搜过 ⇒ 该搜（进池）
# (b) 上次搜过 20 天前（> 14）⇒ 够周期 ⇒ 进池
# (c) 上次搜过 3 天前（< 14）⇒ 未够 ⇒ **不**进池
# ★ 时间戳格式必须是 `"%Y-%m-%d %H:%M:%S"`（不是 ISO 的 `T` 分隔）——
#   `_parse_ts()` 只认空格分隔那种；写成 `T` 会让它判"从没搜过"（踩过）。
def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


_before = _fmt(now - timedelta(days=20))
_recent = _fmt(now - timedelta(days=3))

st = mkstore({
    "pk": [("never_seen", STAGE_UNMATCHED),
           ("due_20d", STAGE_UNMATCHED),
           ("recent_3d", STAGE_UNMATCHED)],
})
set_seen(st, "pk", "due_20d", {ix[0]: _before})
set_seen(st, "pk", "recent_3d", {ix[0]: _recent})

got = st.todo_pooled(pool_of(st), indexers_now=ix, now=now)
_gets = sorted(r["dir_name"] for _, r, _ in got)
ck("★★ 从没搜过 / 够 14 天的 UNMATCHED **进池**；3 天前的**不进**",
   _gets, ["due_20d", "never_seen"])

ck("★ 默认慢档就是 14 天（`DEFAULT_CADENCE_DAYS`）", DEFAULT_CADENCE_DAYS, 14)

# ★ 边界：恰好 14 天前 ⇒ `(now-last) >= 14d` 成立 ⇒ 进池（与 due_indexers 的 `>=` 一致）。
_edge = _fmt(now - timedelta(days=14))
st2 = mkstore({"pk": [("edge_14d", STAGE_UNMATCHED)]})
set_seen(st2, "pk", "edge_14d", {ix[0]: _edge})
ck("★ 边界：恰好 14 天前的 UNMATCHED 进池（判据是 >=）",
   [r["dir_name"] for _, r, _ in st2.todo_pooled(pool_of(st2), indexers_now=ix, now=now)],
   ["edge_14d"])

print()
print("== ④ ★★ 空 `pack` 表 ⇒ `NoPacksRegistered`，不静默算空 ==")

# 直接从 drive-loop.py 源码确认"空表守卫"存在（本文件不 import 它）。
ck("★★ 有 `NoPacksRegistered` 异常类（空表 ≠ 全干完）",
   "class NoPacksRegistered" in _full, True)
ck("★★ 有 `require_packs()` 守卫：空表就抛，不返回空列表",
   "def require_packs" in _full and "raise NoPacksRegistered" in _full, True)
# ★ 判据的"另一侧"：`require_packs` 在空表时**必须抛**，不能 `return []`
#   —— 否则调用方 `alert_if_all_done` 会把空池读成"🎉 全干完"（ERR-AI-03）。
_req = ""
import re as _re  # noqa: E402
_m = _re.search(r"^def require_packs\(.*?\n(?:.*\n)*?(?=^def |^class )", _full, _re.M)
_req = _m.group(0) if _m else ""
ck("★★ require_packs 里对空表**抛异常**（不是返回 []）",
   "raise NoPacksRegistered" in _req and "return []" not in _req, True)

# 行为侧：空库 ⇒ `st.packs()` 为空 ⇒ 池源为空列表（复制 require_packs 的输入）。
st_empty = StateStore(os.path.join(tempfile.mkdtemp(), "t.db"), create=True)
ck("空 pack 表 ⇒ 池源是**空列表**（这正是 require_packs 要拦的输入）",
   pool_of(st_empty), [])
# ★ 对照：有表时非空 ⇒ 证明上一条不是恒真。
st_ne = mkstore({"pk": [("x", STAGE_PENDING)]})
ck("★ 对照：有登记时池源非空", pool_of(st_ne), ["pk"])

print()
print("== ⑤ ★★ 池源确定性：同一库两次调用同序（包名排序来自 SQL）==")

st = mkstore({
    "zeta":  [("z1", STAGE_PENDING)],
    "alpha": [("a1", STAGE_PENDING)],
    "mid":   [("m1", STAGE_PENDING)],
})
ck("★ `packs()` 按名字排序（跨调用稳定，不靠 Python 端排序）",
   pool_of(st), ["alpha", "mid", "zeta"])
ck("★ 两次调用结果一致",
   pool_of(st), pool_of(st))

print()
if fails:
    print("FAILED %d:" % len(fails))
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("全过")
