# -*- coding: utf-8 -*-
"""观测对账的三条判据（a−b / b−c 两口径 / 全场无人认领）—— 离线合成，不碰农场。

为什么要把这三条钉住
====================
它们全是"**报个数出来**"的判据，而"报 0"在这类判据里是最危险的输出：
既可能是"没事"，也可能是"判据根本没走到"。历史上这个项目在这上面栽过三次
（§16.6.5 的字面量太松、§18.17 的静默通道、NanyangPT 的名字盖了两个模型）。
所以本文件**不测"数为 0"**，只测"换个输入，数跟着变、且变的方向可预期"。

★ 三条各自的对账基准（都取自判据之外的**真实记录**，不是照着实现抄的）：
  ① `count_found_lines`：照 `scripts/diag/audit-found-lines.py` 手工跑出来的口径 ——
     L1 那一个字面量会把 `Found 0 torrents for {` 与 `Found N torrent file(s)
     to inject` 一起吃进来（历史实测 2200 vs 靶心 1011），所以**必须**是合取。
  ② `resolve_found_lines`：三包**共用一个 farm_root**（`fix-statedb-farm-root.py`：
     「有 3 行 farm_root 指向旧路径」）→ 生产口径下别的包的 searchee 必然落到
     other_pack。2026-09-12 实测 865 / 146 / 1011，**不是 0**。
  ③ `unclaimed_searchees`：2026-09-12 实测 1888 条里**恰好 1 条**无人认领
     （`0观影清单chrlee整理`，农场里只含一个 xlsx）。合成用例复刻这个形状。

素材全部合成：SQLite 在 `tempfile.mkdtemp()` 里现造，没有网络、没有 NAS、没有真库。
"""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from orchestrator.state import (            # noqa: E402
    CrossSeedSnapshot, L1_LABEL, StateStore, count_found_lines,
    resolve_found_lines, unclaimed_searchees,
)

# ★ 输出强制 UTF-8：GBK 控制台下会在中途炸掉，
#   而**已经过的断点看着全是 ok** —— 极易误判成代码坏了。
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


# --------------------------------------------------------------------------- #
# ① a − b
# --------------------------------------------------------------------------- #
# 四行里三行是靶心形状，一行是**被 L1 吃到但不是靶心**的其它消息。
# 这正是那个 2200 的坑：只看 L1 会数出 4，合取才数得出 3。
#
# ★ 行的形状**照生产日志抄**（2026-09-12 从 info.current.log 取的原样）：
#     [webhook] Found <searchee名> [hash...] on <站名> by <判定> from dataDir (<路径>) - <尾巴>
#   组3 = 站名、组4 = **判定**（MATCH / MATCH_PARTIAL / MATCH_SIZE_ONLY），
#   组5 = searchee 路径。★ 第一版合成时把组4 写成了 `webhook`/`inject` ——
#   那是**合成的形状**，生产里从不出现；拿它当判据就是"合成数据自说自话"。
LOG4 = "\n".join([
    '2026-09-12 01:00:00.000 info: [webhook] Found 0 torrents for {"a":1}',
    '2026-09-12 01:00:01.000 info: [webhook] Found The.Movie.2020 [abcd1234...]'
    ' on HDFans by MATCH from dataDir (/v/farm/The.Movie.2020) - MATCH',
    '2026-09-12 01:00:02.000 info: [inject] Found 南洋寻子 [deadbeef...]'
    ' on NanyangPT (南洋) by MATCH_PARTIAL from dataDir (/v/farm/Nanyang) - MATCH_PARTIAL',
    '2026-09-12 01:00:03.000 info: [webhook] Found Other.2019 [0badf00d...]'
    ' on HDFans by MATCH_SIZE_ONLY from dataDir (/v/farm/Other.2019) - MATCH_SIZE_ONLY',
])

print("== ① a − b：合取才是靶心，L1 不是 ==")
r = count_found_lines(LOG4)
ck("总行数", r.total_lines, 4)
ck("L1 单字面量（那个 2200 的坑）", r.lit_counts.get(L1_LABEL), 4)
ck("a  合取形状", r.a, 3)
ck("b  _RE_FOUND", r.b, 3)
ck("a − b", r.delta, 0)
ck("其它消息被分出去（不进 a）", sum(r.other_found.values()), 1)
ck("控制通过", r.controls_ok, True)
ck("组3 带括号的站名（09-12 修掉的那类）", r.group3_odd, 1)
ck("组4 = 判定，三种都认得",
   sorted(r.group4), ["MATCH", "MATCH_PARTIAL", "MATCH_SIZE_ONLY"])
ck("没有漏行", r.unparsed, [])

print("== ① 反向：靶心形状但 _RE_FOUND 吃不下 → a − b = 1 ==")
# ★ 这一格同时**量出了基线的松紧**：六个字面量**没有**钉住 `[webhook]|[inject]`
#   那个标签，而 `_RE_FOUND` 钉了。所以基线是正则的**超集**，恰好松这一个词。
#   今天生产上 a == b（1011），说明没有别的标签的 Found 行 —— 但这是**实测**，
#   不是**结构保证**。有了这一格，那个"恰好"就有人看着了。
LOG_MISS = LOG4 + "\n" + (
    '2026-09-12 01:00:04.000 verbose: [search] Found Miss.2021 [cafebabe...]'
    ' on HDFans by MATCH from dataDir (/v/farm/Miss.2021) - MATCH')
rm = count_found_lines(LOG_MISS)
ck("a 吃到 4 条", rm.a, 4)
ck("b 只吃下 3 条", rm.b, 3)
ck("a − b 非零", rm.delta, 1)
ck("漏的那条被留证", len(rm.unparsed), 1)

print("== ① 空转 vs 判据坏掉：两个都要能分辨 ==")
re_ = count_found_lines("")
ck("读 0 行 → 控制不过", re_.controls_ok, False)
ck("读 0 行 → a、b 都是 0", (re_.a, re_.b), (0, 0))
# 只有别的消息、一条靶心都没有：控制**通过**（读到行、L1 数到了），b = 0。
# ★ 这就是"空转"：判据走到了，只是真没有 Found 行。它该报的是告警，
#   不是"控制没过" —— 混在一起就分不清「判据坏了」和「真的没发生」。
rz = count_found_lines('2026-09-12 01:00:00.000 info: [webhook] Found 0 torrents for {"a":1}')
ck("只有别的消息 → 控制通过", rz.controls_ok, True)
ck("只有别的消息 → b = 0（空转，不是控制失败）", rz.b, 0)

# --------------------------------------------------------------------------- #
# 合成库：两个包**共用一个农场根**（生产就是这个形状）
# --------------------------------------------------------------------------- #
FARM = "/vol1/farm"
A_ROOT = "/vol1/tv"
B_ROOT = "/vol1/movies"

db = os.path.join(tempfile.mkdtemp(), "t.db")
st = StateStore(db, create=True)   # 夹具建库（生产默认不建，见 StateStore.__init__）
st.upsert_pack("alpha", A_ROOT, roots=[A_ROOT], farm_root=FARM)
st.register_dirs("alpha", [("Show.S01", A_ROOT + "/Show.S01")])
st.upsert_pack("beta", B_ROOT, roots=[B_ROOT], farm_root=FARM)
st.register_dirs("beta", [("Movie.A", B_ROOT + "/Movie.A")])

# 三条 searchee：两条归 A/B（**农场路径**，v3 之后 cross-seed 报的就是这个），
# 一条在农场里但哪个包都不认 —— 复刻真实的那个 xlsx。
SNAP = CrossSeedSnapshot(searchee_paths={
    "Show.S01":          FARM + "/Show.S01",
    "Movie.A":           FARM + "/Movie.A",
    "0观影清单chrlee整理": FARM + "/0观影清单chrlee整理",
})

# --------------------------------------------------------------------------- #
# ② b − c：**两种口径的数不一样，而且各自都对**
# --------------------------------------------------------------------------- #
print("== ② 两口径：同一批行，by 谁试，other_pack 完全不是一个数 ==")
LOG2 = "\n".join([
    '2026-09-12 01:00:00.000 info: [webhook] Found Show.S01 [abcd1234...]'
    ' on HDFans by MATCH from dataDir (%s/Show.S01) - MATCH' % FARM,
])
rv = resolve_found_lines(LOG2, st)
ck("b", rv.b, 1)
ck("c 全量口径归到单片", rv.c, 1)
ck("全量口径 b − c", rv.delta, 0)
# ★ 全量口径：所有包一起试 → 这一行归给 alpha，other_pack = 0。
ck("〔全量口径〕归到 alpha", rv.belong_all, {"in_pack": 1})
# ★★ 生产口径：sync_pack 一次只给一个包 —— 对 beta 来说这条**必然**是 other_pack。
#    实测在生产上就是 865 / 146 / 1011，不是 0。
ck("〔生产口径〕alpha 看它是 in_pack", rv.belong_prod["alpha"], {"in_pack": 1})
ck("〔生产口径〕beta  看它是 other_pack", rv.belong_prod["beta"], {"other_pack": 1})
ck("农场行计数（这一行确实在农场根下）", (rv.farm_lines, rv.orig_lines), (1, 0))
ck("组5 路径留证（给无人认领复用）", rv.paths, [FARM + "/Show.S01"])

# --------------------------------------------------------------------------- #
# ③ 全场无人认领
# --------------------------------------------------------------------------- #
print("== ③ 无人认领：三包合起来都认不出的那些 ==")
un = unclaimed_searchees(st, SNAP)
ck("恰好 1 条", len(un), 1)
ck("就是那条（★ 与实测同名同形状）", un[0][1], FARM + "/0观影清单chrlee整理")
# ★ 正向控制：A/B 两条**不能**出现在结果里 —— 否则"认得出"这件事没被证明。
ck("alpha 那条没被误报", any("Show.S01" == n for n, _ in un), False)
ck("beta  那条没被误报", any("Movie.A" == n for n, _ in un), False)

print("== ③ 反向：补上一个包认领它，数就掉到 0 ==")
st.register_dirs("alpha", [("观影清单", FARM + "/0观影清单chrlee整理")])
ck("补登记后归零", unclaimed_searchees(st, SNAP), [])
# 再撤掉，确认这个 0 是"改对了"而不是"判据死了"
st.con.execute("DELETE FROM movie WHERE dir_name='观影清单'")
st.con.commit()
ck("撤掉后又回到 1", len(unclaimed_searchees(st, SNAP)), 1)

# --------------------------------------------------------------------------- #
# ④ 接线层：`drive-loop.reconcile_watch()` 绝不抛，且**真的报警**
# --------------------------------------------------------------------------- #
# ★ 这一节钉的不是"数算得对"（上面已经钉了），而是三件更容易静默坏掉的事：
#   ① 它挂在每天一次的日报里，日报挂在**每 15 分钟一批**的生产循环里 ——
#      它抛一次，整份日报就没了；
#   ② 读不到时 metrics 必须给 `n/a`，不能给 0 —— 否则 TSV 里
#      "这次读失败了"和"那天根本没跑"长得一模一样；
#   ③ 差不为 0 时**必须真的发 alert** —— 只写进正文的告警等于没发。
import argparse                      # noqa: E402
import contextlib                    # noqa: E402
import importlib.util                # noqa: E402
import io                            # noqa: E402
import re                            # noqa: E402
import sqlite3                       # noqa: E402

DRIVE_LOOP = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "drive-loop.py"


def _load(path, name):
    """按 tests/README.md 的规矩：importlib 载**真的那份**，且先登记 sys.modules。"""
    if str(path.parent.parent) not in sys.path:
        sys.path.insert(0, str(path.parent.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod          # ★ 不登记的话 dataclass 会炸
    with contextlib.redirect_stdout(io.StringIO()), \
         contextlib.redirect_stderr(io.StringIO()):
        try:
            spec.loader.exec_module(mod)
        except SystemExit:
            pass
    return mod


print("== ④ reconcile_watch：绝不抛，读不到给 n/a 而不是 0 ==")
D = _load(DRIVE_LOOP, "drive_loop_reconcile")
events: list = []
D.emit = lambda kind, title, body="", *, key=None, metrics=None: (
    events.append((kind, title, body, key, metrics)) or True)
# ★ 「生产会写文件」的路径，测试里必须**改向到临时目录** ——
#   不改的话跑一次测试就往 `scripts/` 里留一个 `.reconcile.state`
#   （tests/README 的坑那一节记着同类事故：`.farm-check.state`）。
#   ★ 用模块级常量（`RECONCILE_FILE`）而不是函数里的局部路径，就是为了让这一行成立。
D.RECONCILE_FILE = pathlib.Path(tempfile.mkdtemp()) / ".reconcile.state"

LOG_A = os.path.join(tempfile.mkdtemp(), "info.log")
pathlib.Path(LOG_A).write_text(LOG4, encoding="utf-8")
LOG_B = os.path.join(tempfile.mkdtemp(), "info2.log")
pathlib.Path(LOG_B).write_text(LOG_MISS, encoding="utf-8")

note, m = D.reconcile_watch(argparse.Namespace(log=None, db=None, db_path=None))
ck("没有 --log：不抛", isinstance(note, str), True)
ck("没有 --log：fa 是 n/a（★ 不是 0）", m["fa"], "n/a")
ck("没有 --log：正文点名「跳过」", "跳过" in note, True)

note, m = D.reconcile_watch(argparse.Namespace(log=[LOG_A], db=None, db_path=None))
ck("有日志：fa / fb 是真数", (m["fa"], m["fb"]), (3, 3))
ck("有日志：a − b", m["fd"], 0)
# ★ 口径名必须出现在正文里：`info.current.log` **按天轮转**，所以这几个数天然是
#   「从今天 00:00 到现在」。不写出来的话，"昨天 1011、今天日报 400" 会被念成判据坏了。
ck("正文带口径名（当日日志）", "〔当日日志〕" in note, True)
ck("没库：b−c 给 n/a", m["fb_c_all"], "n/a")
ck("没库：无人认领给 n/a（不是 0）", m["unclaimed"], "n/a")

print("== ④ 状态库**不在** → 必须先问 is_file，绝不能让它把空库建出来 ==")
# ★★ 这一格钉的是**唯一一个「沉默会变成数字」的入口**。
#    `StateStore.__init__` 第一件事就是 mkdir(parents=True) + connect +
#    `executescript(SCHEMA)` —— 库不存在时**就地建一个空的**。于是：
#        "库不在" → 不抛异常 → pack/movie 两张表全空 → pack_contexts() 返回 {}
#                 → 库里**每一条** searchee 都算「无人认领」→ 报出一个**巨大的假数**，
#                   外加一条醒目告警，还在错位置写下了一个库文件。
#    ★ 这正是 ③ 自己立的规矩（「读不到给 n/a 不给 0」）在**第三个输入**上被破的
#      那一格 —— 而且是往**假阳性**那头破。对照 `read_crossseed_db()`：它自己
#      `raise FileNotFoundError`，所以 `--db-path` 那一路天然是安全的。
events.clear()
# ★ 故意造一个**父目录也不存在**的路径：StateStore 连父目录都会替你建出来，
#   所以"父目录没被建"是比"文件没被建"更强的证据 —— 证明我们压根没把它交出去。
_missing = os.path.join(tempfile.mkdtemp(), "sub", "not-there.db")
note, m = D.reconcile_watch(
    argparse.Namespace(log=[LOG_A], db=_missing, db_path=None))
ck("库不在 → 无人认领给 n/a（★ 不是一个大数）", m["unclaimed"], "n/a")
ck("库不在 → b−c 也给 n/a", m["fb_c_all"], "n/a")
ck("★ 那个文件没有被建出来", os.path.exists(_missing), False)
ck("★ 连父目录都没被建（证明没交给 StateStore）",
   os.path.isdir(os.path.dirname(_missing)), False)
ck("正文点名「状态库不在」", "状态库不在" in note, True)
ck("库不在时**不发**那两条告警（没跑到，不是有信号）",
   [e[3] for e in events if e[0] == "alert" and e[3] in
    ("unclaimed-searchee", "b-c")], [])
# ★ LOG_A 里有一条 `Found 0 torrents for {`（三行式）⇒ **会**发 reconcile-other-found。
#   这正是 2026-10-02 的处置：别的 Found 形状不再静默。见下面 ⑤ 段。
ck("★ 但『别的 Found 形状』照发（LOG_A 里有三行式）",
   [e[3] for e in events if e[0] == "alert"], ["reconcile-other-found"])

# ★★ 下面两格钉的是**另外两个出口** —— 它们此前**一条断言都没有**：
#    三个出口里只有 `log-parse-miss` 被钉过，另两条**整段删掉测试也不会红**。
#    而本系统的全部价值就在「判据的出口会响」：一个不会响的出口，
#    在无人值守下和"根本没有这个出口"等价。这也正是 §18.18 那个形状 ——
#    **判据被验过了，判据的出口没被验。**
print("== ④ 出口 2：控制没过（判据压根没走到）—— 又是一条，key 也不一样 ==")
LOG_C = os.path.join(tempfile.mkdtemp(), "info3.log")
# 日志**读得进来**，但一整行 `] Found ` 都没有 → 基线一个字面量都数不到。
# ★ 注意这与"空文件"不同：空文件是 total_lines = 0，这里是"读了、但没有靶心形状"。
pathlib.Path(LOG_C).write_text(
    '2026-09-12 01:00:00.000 info: [webhook] Received search request',
    encoding="utf-8")
events.clear()
note, m = D.reconcile_watch(argparse.Namespace(log=[LOG_C], db=None, db_path=None))
ck("控制没过：a / b 都是 0", (m["fa"], m["fb"]), (0, 0))
ck("控制没过：发了一条 alert", [e[0] for e in events], ["alert"])
ck("key 是 reconcile-controls", events[0][3], "reconcile-controls")
# ★★ 2026-09-14 改。原来这里钉的是「正文必须说『匹配逻辑坏了』」——
#   **那个断言把一次误判钉成了契约。** `controls_ok` 只有一条实质条件：
#   `lit_counts[L1] > 0`，也就是**当日日志里出现过一次 `] Found `**。
#   而日报是在**当天第一批（00:0x–01:3x）**采样的，于是任何安静的夜它都会失败 —
#   那时正文说「匹配逻辑坏了」就是**指错方向**（改正则 vs 去查为什么没搜）。
#   实测（2026-09-14）：当日日志 40 行 / L1 命中 0 / a = b = 0，报了「匹配逻辑坏了」；
#   而**同一个函数、同一张字面量表**跑前三个完整日：
#     09-11 a=399 b=399 a−b=0 ｜ 09-12 a=1011 b=1011 a−b=0 ｜ 09-13 a=179 b=179 a−b=0
#   ⇒ 判据是好的，0 只是「当日口径」的 0。所以现在钉的是**并列两种可能**，
#     并钉死「不许再下那个结论」。
ck("正文并列两种可能（①没搜出去 / ②匹配逻辑坏了）",
   ("①" in events[0][2] and "匹配逻辑" in events[0][2]), True)
ck("★ 不再断言「匹配逻辑坏了」", "说明**匹配逻辑坏了**" in events[0][2], False)
ck("同目录没有别的已轮转日志 → 明说判不了（不猜）",
   "判不了" in events[0][2], True)

print("== ④ 出口 2b：有完整日对照时，必须给**证据**而不是猜（09-14 就是这一格）==")
# ★ 复刻 2026-09-14：当日日志刚轮转、一条 Found 都没有，而**昨天那个完整日**
#   数得到。判据没坏，坏的只是「当日口径」这个窗口 —— 所以正文必须把对照摆出来，
#   让读告警的人不必自己去猜该往哪儿查。
DIR_2B = tempfile.mkdtemp()
LOG_CUR = os.path.join(DIR_2B, "info.current.log")
LOG_PREV = os.path.join(DIR_2B, "info.2026-09-13.log")
pathlib.Path(LOG_CUR).write_text(
    '2026-09-14 00:00:01.000 info: [webhook] Received search request',
    encoding="utf-8")
pathlib.Path(LOG_PREV).write_text(LOG2, encoding="utf-8")   # 完整日：有靶心形状
events.clear()
note, m = D.reconcile_watch(argparse.Namespace(log=[LOG_CUR], db=None, db_path=None))
ck("当日 0 → 仍然发 alert", [e[0] for e in events], ["alert"])
ck("正文点名那个完整日", "info.2026-09-13.log" in events[0][2], True)
ck("★ 给出「数得到 ⇒ 判据没坏」", "判据没坏" in events[0][2], True)
ck("★ 并说清本日的 0 是当日口径的 0", "当日口径" in events[0][2], True)

print("== ④ 出口 2c：完整日**也**数不到 → 这才该怀疑判据 ==")
DIR_2C = tempfile.mkdtemp()
LOG_CUR2 = os.path.join(DIR_2C, "info.current.log")
LOG_PREV2 = os.path.join(DIR_2C, "info.2026-09-13.log")
pathlib.Path(LOG_CUR2).write_text(
    '2026-09-14 00:00:01.000 info: [webhook] Received search request',
    encoding="utf-8")
pathlib.Path(LOG_PREV2).write_text(
    '2026-09-13 01:00:00.000 info: [webhook] Received search request',
    encoding="utf-8")
events.clear()
note, m = D.reconcile_watch(argparse.Namespace(log=[LOG_CUR2], db=None, db_path=None))
ck("★ 两天都数不到 → 才指向正则", "匹配逻辑真的坏了" in events[0][2], True)

print("== ④ 出口 3：空转（b == 0 但**控制通过**）—— 必须与上一格分得开 ==")
# ★ 两格的 metrics 都是 fb = 0，**但含义相反**：一个是"判据坏了"，
#   一个是"判据好的、只是今天真没有 Found 行"。混成一条就没法从告警
#   判断该去查哪儿（§18.17.3 要的正是把这两件事分开）。
#   这一行的形状照生产日志抄：L1 那个字面量吃得到，六字面量合取吃不到。
LOG_D = os.path.join(tempfile.mkdtemp(), "info4.log")
pathlib.Path(LOG_D).write_text(
    '2026-09-12 01:00:00.000 info: [webhook] Found 0 torrents for {"a":1}',
    encoding="utf-8")
events.clear()
note, m = D.reconcile_watch(argparse.Namespace(log=[LOG_D], db=None, db_path=None))
ck("控制**通过**（L1 数到了）", m["fa"], 0)
ck("b == 0（空转本身）", m["fb"], 0)
ck("空转发的是 alert", [e[3] for e in events if e[0] == "alert" and e[3] == "reconcile-empty"],
   ["reconcile-empty"])
ck("key 是 reconcile-empty（★ 不是 reconcile-controls）",
   events[0][3], "reconcile-empty")
ck("正文点明「不是「干净」」", "干净" in events[0][2], True)

print("== ④ 出口 4：a > 0 且 b == 0 —— 最严重的那一种，不许被「空转」吞掉 ==")
# ★★ 这一格钉的是**判断顺序**：旧顺序是 ①controls → ②b==0 → ③delta，
#   于是 `a > 0 且 b == 0`（形状数到了六字面量、正则**一条都没吃下**，
#   于是 delta = a > 0）被 ② 吞了 —— 正文说「要么真没搜出去过」，
#   而 metrics 里 `fa` 明明白白是 1，**两个字段互相打脸**。
#   更要命的是两者指向**完全不同的排查动作**：一个去查"为什么没搜"，
#   一个去查正则。看告警的人只读正文，就会被指错方向。
# ★ 合成形状照生产抄：标签不是 `webhook`/`inject`（`_RE_FOUND` 钉了这两个），
#   所以六个字面量全中、生产正则一条也不吃。
LOG_E = os.path.join(tempfile.mkdtemp(), "info5.log")
pathlib.Path(LOG_E).write_text(
    '2026-09-12 01:00:04.000 verbose: [search] Found Miss.2021 [cafebabe...]'
    ' on HDFans by MATCH from dataDir (/v/farm/Miss.2021) - MATCH', encoding="utf-8")
events.clear()
note, m = D.reconcile_watch(argparse.Namespace(log=[LOG_E], db=None, db_path=None))
ck("形状 1 行 / 正则 0 行（★ 这就是那个极端形）", (m["fa"], m["fb"]), (1, 0))
ck("delta = 1（不是 0 —— 所以它**不是**空转）", m["fd"], 1)
ck("★ key 是 log-parse-miss（不是 reconcile-empty）",
   [e[3] for e in events if e[0] == "alert"], ["log-parse-miss"])
ck("★ 正文点名「一条都没吃下」", "一条都没吃下" in events[0][2], True)
# ★ 反向控制：两个出口**用同一个数（fb == 0）报出来**，只有这句话能把它们分开 ——
#   所以"空转那句不许出现"必须单独钉一条，否则顺序改回去这格照样绿。
ck("★ 正文**不**出现空转那句「真没搜出去过」", "真没搜出去过" in events[0][2], False)

print("== ④ 差不为 0 → 必须真的发 alert，且 key 固定 ==")
events.clear()
note, m = D.reconcile_watch(argparse.Namespace(log=[LOG_B], db=None, db_path=None))
ck("差值 1 被报出来", m["fd"], 1)
ck("发了一条 log-parse-miss alert",
   [e[3] for e in events if e[0] == "alert" and e[3] == "log-parse-miss"], ["log-parse-miss"])
ck("key 是 log-parse-miss（冷却靠它去重）", events[0][3], "log-parse-miss")
# ★ 留证行必须在**告警正文**里（只在日报正文里不算 —— 告警和日报是两条路）
ck("告警正文带留证行", "Miss.2021" in events[0][2], True)

print("== ④ 库可达 → b−c 与无人认领都是真数 ==")
events.clear()


def _fake_read(_p, *a, **k):
    return SNAP


D.S.read_crossseed_db = _fake_read        # 真库里那 1888 行的形状，用 SNAP 复刻
db2 = os.path.join(tempfile.mkdtemp(), "t2.db")
st2 = StateStore(db2, create=True)
# ★ 两个包都要登记 —— 只登记 alpha 的话，beta 那条会被正确地报成"无人认领"，
#   于是本节测的就不是接线而是夹具（第一版就是这么错的，数报了 2）。
st2.upsert_pack("alpha", A_ROOT, roots=[A_ROOT], farm_root=FARM)
st2.register_dirs("alpha", [("Show.S01", A_ROOT + "/Show.S01")])
st2.upsert_pack("beta", B_ROOT, roots=[B_ROOT], farm_root=FARM)
st2.register_dirs("beta", [("Movie.A", B_ROOT + "/Movie.A")])
pathlib.Path(LOG_A).write_text(
    '2026-09-12 01:00:00.000 info: [webhook] Found Show.S01 [abcd1234...]'
    ' on HDFans by MATCH from dataDir (%s/Show.S01) - MATCH' % FARM, encoding="utf-8")
note, m = D.reconcile_watch(argparse.Namespace(log=[LOG_A], db=db2, db_path="<stub>"))
ck("b − c〔全量口径〕", m["fb_c_all"], 0)
ck("农场行计数", m["fb_c_farm"], 1)
ck("无人认领 = 1（SNAP 里那条）", m["unclaimed"], 1)
# ★★ 无人认领改成「只报变化」（2026-09-13）：
#   那 1 条是**已接受**的现状，配上 alert 的 12 小时冷却就是"每天响两次、永远"
#   —— 现状不是新闻（同 `--packs` 那条）。所以**首读只记基线、不响**。
ck("★ 首读记基线、**不发** alert",
   [e[3] for e in events if e[0] == "alert"], [])
ck("正文说明这是首读", "首次读数" in note, True)
ck("正文点名那条路径（只报数 = 换个地方藏）",
   FARM + "/0观影清单chrlee整理" in note, True)
ck("基线已落盘",
   D._reconcile_read().get(D.UNCLAIMED_BASELINE_KEY),
   [FARM + "/0观影清单chrlee整理"])

# ★★ 本节的核心那格：**同一个集合连读两次，第二次必须静默。**
#   少了这一格，「首读不响」可以被实现成「永远不响」而照样全绿。
events.clear()
note, m = D.reconcile_watch(argparse.Namespace(log=[LOG_A], db=db2, db_path="<stub>"))
ck("★ 同一批连读两次：第二次**静默**", [e[0] for e in events], [])
ck("第二次仍是 1", m["unclaimed"], 1)
ck("正文说明与基线一致", "与基线一致" in note, True)

# 出现**新的**一条 → 这才该响，且要点名新那条（只报总数 = 换个地方藏）
SNAP2 = CrossSeedSnapshot(searchee_paths=dict(
    SNAP.searchee_paths, **{"新混进来的杂物": FARM + "/新混进来的杂物"}))
D.S.read_crossseed_db = lambda _p, *a, **k: SNAP2
events.clear()
note, m = D.reconcile_watch(argparse.Namespace(log=[LOG_A], db=db2, db_path="<stub>"))
ck("新增 1 条 → unclaimed = 2", m["unclaimed"], 2)
ck("★ 新增才发 alert",
   [e[3] for e in events if e[0] == "alert"], ["unclaimed-searchee"])
ck("★ 告警正文点的是**新增**那条", "新混进来的杂物" in events[0][2], True)
ck("★ metrics 把「总数」和「新增数」分开",
   (events[0][4].get("unclaimed"), events[0][4].get("unclaimed_new")), (2, 1))

# 缩回（新的那条没了）→ 静默采纳新基线，**但正文要写出来** ——
# 否则「那个目录被清掉了（预期）」和「判据今天没读到（故障）」在日报里长得一样。
D.S.read_crossseed_db = _fake_read
events.clear()
note, m = D.reconcile_watch(argparse.Namespace(log=[LOG_A], db=db2, db_path="<stub>"))
ck("缩回：不告警", [e[0] for e in events], [])
ck("缩回后 = 1", m["unclaimed"], 1)
ck("★ 缩回也在正文里写出来（不是静默无痕）", "比基线**少**" in note, True)

# ★ 反向控制：缩回采纳了新基线 ⇒ 它**再长回来必须被抓住**。
#   少了这一格，「缩回静默采纳」可以被实现成「顺手把基线也删了」而照样绿。
D.S.read_crossseed_db = lambda _p, *a, **k: SNAP2
events.clear()
note, m = D.reconcile_watch(argparse.Namespace(log=[LOG_A], db=db2, db_path="<stub>"))
ck("★ 缩回后再长回来 → **必须报**",
   [e[3] for e in events if e[0] == "alert"], ["unclaimed-searchee"])
D.S.read_crossseed_db = _fake_read

print("== ④ 声明点〔--packs〕：只报**变化**，不报现状（2026-09-12 深夜改） ==")
# ★★ 这一格钉的形状照 2026-09-12 实测（NAS 只读）：`pack` 表 **3 行**
#    （dc-collection / frds-top250-2024 / **mbf**）、`--packs` 默认只驱动**前两个**
#    → 差集 = {`mbf`}。而 `mbf` 在 unclaimed / report / trend 上**全绿**：
#    pack 表有行、movie 表有 4 行、farm_root 也有。抓不到它**不是判据算错了**，
#    是**没有一条判据的输入源包含 `--packs` 的实际值**（§18.18 那个形状）。
#    合成库里用 alpha/beta 复刻：登记 2 个、只驱动 1 个。
#
# ★★ 为什么改成「只报变化」：`mbf` 是**已接受的现状**，非空就发 alert 等于
#    每天喊一次 —— 而噪音的代价是**真的出问题时没人看**。
#    判据本身（差集算得对）没变，变的只是**出口的触发条件**。
print("   ① 首次读数：记基线、**不告警**（现状不是新闻）")
events.clear()
note, m = D.reconcile_watch(
    argparse.Namespace(log=[LOG_A], db=db2, db_path=None, packs="alpha,beta"))
ck("首次读数：差集是 0", (m["packs_undriven"], m["packs_unreg"]), (0, 0))
ck("★ 首次读数**不发** alert", [e for e in events if e[0] == "alert"], [])
ck("正文说明白了这是首次", "首次读数" in note, True)

print("   ② 差集**变大** → 告警（这才是新问题）")
events.clear()
note, m = D.reconcile_watch(
    argparse.Namespace(log=[LOG_A], db=db2, db_path=None, packs="alpha"))
ck("登记了没被驱动：计数", m["packs_undriven"], 1)
ck("在名单里但没登记：0", m["packs_unreg"], 0)
ck("正文点名那个包（只报数 = 换个地方藏）", "beta" in note, True)
ck("★ 相对基线**有新增** → 发 alert",
   [e[3] for e in events if e[0] == "alert"], ["packs-mismatch"])
ck("告警标题说的是「变了」不是「对不上」", "变了" in events[0][1], True)
ck("告警正文点名新冒出来的那个", "新增" in events[0][2] and "beta" in events[0][2], True)

print("   ③ ★★ 同一个差集**再读一次** → 不许再喊（这就是「只报变化」）")
# ★★ 这一格是整个改动的**核心断言**：没有它，「改了但没生效」和「改了且生效」
#    长得一模一样。它同时也是 `mbf` 那个日常噪音的**回归钉子**。
events.clear()
note, m = D.reconcile_watch(
    argparse.Namespace(log=[LOG_A], db=db2, db_path=None, packs="alpha"))
ck("差集仍然是 1（判据照跑，没被跳过）", m["packs_undriven"], 1)
ck("★★ 与基线一致 → **不发** alert", [e for e in events if e[0] == "alert"], [])
ck("正文仍打印差集（不告警 ≠ 看不见）", "beta" in note, True)
ck("正文说明了为什么不喊", "与基线一致" in note, True)

print("   ④ 另一个方向：名单里有、库里没有 —— 比上一格更糟")
# ★ 方向不同、后果不同：`pack` 表 − 名单 = 白登记（片子永远搜不到）；
#   名单 − `pack` 表 = 状态机看不见它的片子，整包会被判成 other_pack 而静默降级。
events.clear()
note, m = D.reconcile_watch(
    argparse.Namespace(log=[LOG_A], db=db2, db_path=None, packs="alpha,gamma"))
ck("名单里的 gamma 库里没有", m["packs_unreg"], 1)
ck("同时 beta 仍没被驱动", m["packs_undriven"], 1)
ck("两个方向都打印了", ("gamma" in note and "beta" in note), True)
ck("alert 仍是同一条 key",
   [e[3] for e in events if e[0] == "alert"], ["packs-mismatch"])
ck("★ 变化那一段点名的是**新增的** gamma",
   "新增" in events[0][2] and "gamma" in events[0][2], True)

print("   ⑤ 差集缩回 0 → 静默采纳（修好了不必喊）")
events.clear()
note, m = D.reconcile_watch(
    argparse.Namespace(log=[LOG_A], db=db2, db_path=None, packs="alpha,beta"))
ck("两个方向都是 0", (m["packs_undriven"], m["packs_unreg"]), (0, 0))
ck("缩回到 0 → 不发 alert", [e for e in events if e[0] == "alert"], [])
# ★★ 缩回也**必须写进正文**（2026-09-13 补）—— 上一版这里只会打一句「与基线一致」，
#    而它明明是**少了** `beta`：**文案断言了一件没发生的事**。
#    为什么非要写出来：不写的话，「那条差集被修掉了」和「判据今天没读到」在日报里
#    长得一模一样 —— 而这两件事的处置完全相反（一个是收工，一个是去查故障）。
#    ★ 触发点就是 #40（把 `mbf` 排进 `--packs` 正是一次缩回），所以这一格是它的回归钉子。
ck("★★ 缩回要写出来：正文说「比基线少」", "比基线**少**" in note, True)
ck("★★ 并**点名**少了谁（beta）", "少" in note and "beta" in note, True)
ck("★★ 缩回时**不许**说「与基线一致」—— 那是假话", "与基线一致" in note, False)

print("   ⑥ ★★ 再长回来 → 必须报（这才是真该抓的：回归）")
# ★ 这一格钉的是「**缩也要采纳新基线**」那条决定：如果缩的时候不更新基线，
#   基线里还留着 beta/gamma，那它俩再冒出来时就会被判成"老样子"而**没人报**。
events.clear()
note, m = D.reconcile_watch(
    argparse.Namespace(log=[LOG_A], db=db2, db_path=None, packs="alpha"))
ck("又变成 1", m["packs_undriven"], 1)
ck("★★ 回归被抓住 → 又发 alert",
   [e[3] for e in events if e[0] == "alert"], ["packs-mismatch"])

print("== ④ 没给 packs / 库读不到 → 必须 n/a，**且不许把基线抹掉** ==")
# ★ 与 `fa` / `unclaimed` 同一条规矩：「调用方没传」和「名单对得上」在 TSV 里
#   长得一模一样，事后就分不开。而且**两种都不许发 alert**（没跑到 ≠ 有信号）。
events.clear()
note, m = D.reconcile_watch(
    argparse.Namespace(log=[LOG_A], db=db2, db_path=None))
ck("库在但没给 packs：两个方向都 n/a",
   (m["packs_undriven"], m["packs_unreg"]), ("n/a", "n/a"))
ck("没给 packs：不发 alert", [e for e in events if e[0] == "alert"], [])

note, m = D.reconcile_watch(
    argparse.Namespace(log=[LOG_A], db=None, db_path=None, packs="alpha"))
ck("库读不到：两个方向都 n/a（不是 0）",
   (m["packs_undriven"], m["packs_unreg"]), ("n/a", "n/a"))
ck("库读不到：正文说清「没跑成」", "没跑成" in note, True)

print("== ④ ★ 上面两轮 n/a 之后，基线**没被抹掉** ==")
# ★ 这一格是 `packs_baseline is not None` 那行守卫的钉子：没有它，n/a 的两轮
#   会把基线写成空，于是下一轮 `mbf` 这种老问题会被当成「新变化」再喊一遍 ——
#   正是这次要消掉的那种噪音。
events.clear()
note, m = D.reconcile_watch(
    argparse.Namespace(log=[LOG_A], db=db2, db_path=None, packs="alpha"))
ck("差集仍是 1", m["packs_undriven"], 1)
ck("★★ n/a 之后仍是「与基线一致」→ **不发** alert",
   [e for e in events if e[0] == "alert"], [])

print("== ④ #34：n/a 要配「上次成功读数时刻」—— 长期 n/a 与偶发 n/a 分开 ==")
# ★ 为什么非要这一节：`n/a` **只说明这一次**。TSV 里一个连续三天读不到的格子
#   和一个昨天还好、今天抖了一下的格子**长得一模一样**，于是"判据长期失效"
#   和"偶发抖动"事后完全分不开 —— 判据要能指回时间，才谈得上指回真实记录。
#
# ① 换一个**全新的**台账文件 → 谁都没成功过
D.RECONCILE_FILE = pathlib.Path(tempfile.mkdtemp()) / ".reconcile.state"
events.clear()
note, m = D.reconcile_watch(argparse.Namespace(log=None, db=None, db_path=None))
ck("全新台账：8 格全是 n/a", sorted(k for k, v in m.items() if v == "n/a"), sorted(m))
ck("★ 正文点名「从未成功读到过」", "从未成功读到过" in note, True)

# ② 真读到数 → 给**每一格**盖上时间戳（packs 也带上，否则那两格没被覆盖）
events.clear()
note, m2 = D.reconcile_watch(
    argparse.Namespace(log=[LOG_A], db=db2, db_path="<stub>", packs="alpha,beta"))
ck("真读到数：8 格全不是 n/a", [k for k, v in m2.items() if v == "n/a"], [])

# ③ 再读不到 → 必须说得出来「上次是什么时候」
note, m3 = D.reconcile_watch(argparse.Namespace(log=None, db=None, db_path=None))
ck("这次又是 n/a", m3["fa"], "n/a")
ck("★ 正文带上「上次成功」的时刻", "上次成功" in note, True)
ck("★ 且**不是**「从未成功」—— 那两句话指向完全不同的处置",
   "从未成功读到过" in note, False)
print("   ↑ ③ 的正文：", note.splitlines()[-2].strip()[:100])

# --------------------------------------------------------------------------- #
# ⑤ ★★★ 其它 Found 形状必须**报警**，且 a 的口径**不含**三行式（2026-10-02 修）
# --------------------------------------------------------------------------- #
# 现场（§32.16 第 7 个真 bug）：cross-seed 有**两种** `] Found ` 行 ——
#   ① 单行式 `Found <片名> [hash] on <站> by <判定> from dataDir (…) - …`（含站名）；
#   ② 三行式 `[webhook] Found N torrents for {`（本行 + path: + }，**无站名**）。
# 判据（`FOUND_LITS` 六字面量 / `_RE_FOUND`）只描述① ⇒ ②掉进 `other_found`
# **且一声不响**（09-16~09-21 每天几百条，10-01 还有 136 条）⇒ a 长期漏计。
# ★ 本次口径拍板：**a = 「单行式（可回灌）已识别条数」，三行式刻意不进 a**
#   （并进 a 会让 a−b 常年为正、判据恒红）。处置是**把 other_found 报出来**。
print("== ⑤ 三行式：不进 a、但必须发 alert（不再静默） ==")
# 一天里既有单行式（靶心）又有三行式（别的形状）。
LOG_3LINE = "\n".join([
    # 三行式的三行（本行 + path + }）—— 照生产抄
    '2026-10-01 01:00:00.000 info: [webhook] Found 1 torrents for {',
    "2026-10-01 01:00:00.001 info:   path: '/v/farm/The.Movie.2020',",
    "2026-10-01 01:00:00.002 info: }",
    # 一条单行式（靶心）
    '2026-10-01 01:00:01.000 info: [webhook] Found The.Movie.2020 [abcd1234...]'
    ' on HDFans by MATCH from dataDir (/v/farm/The.Movie.2020) - MATCH',
])
r5 = count_found_lines(LOG_3LINE)
ck("★ 三行式**不进** a（a 只数单行式）", r5.a, 1)
ck("★ b（正则）也只吃单行式", r5.b, 1)
ck("★ a − b 仍为 0（三行式不参与，判据不恒红）", r5.delta, 0)
ck("★ 三行式的『Found N torrents for {』那一行进了 other_found",
   sum(r5.other_found.values()), 1)  # 只有含 `] Found ` 的那一行（path / } 两行不含）
ck("其它形状里有『torrents for』那一类", any("torrents for" in k for k in r5.other_found), True)

# ★ 端到端：把它喂给 reconcile_watch，必须**发一条 key=reconcile-other-found** 的告警。
D.RECONCILE_FILE = pathlib.Path(tempfile.mkdtemp()) / ".reconcile.state"
LOG_3 = os.path.join(tempfile.mkdtemp(), "info3line.log")
pathlib.Path(LOG_3).write_text(LOG_3LINE, encoding="utf-8")
events.clear()
note5, m5 = D.reconcile_watch(argparse.Namespace(log=[LOG_3], db=None, db_path=None))
_keys5 = [e[3] for e in events if e[0] == "alert"]
ck("★★ 其它 Found 形状 ⇒ 真的发了 alert（不再静默）", _keys5, ["reconcile-other-found"])
ck("★ metrics 带 f_other（别的形状总数）", events[0][4].get("f_other"), 1)
ck("★ metrics 带 f_3line（三行式那一类单独点名）", events[0][4].get("f_3line"), 1)
ck("★ 正文写明 a 的口径（不是「搜出去总量」）",
   "搜出去总量" in events[0][2], True)
ck("★ 正文点名三行式已知、且刻意不进 a",
   "三行式" in events[0][2] and "没有站名" in events[0][2], True)
ck("★ a 仍是 1（没被三行式污染）—— 正文读数与 metrics 一致",
   m5.get("fa"), 1)

# 阴性对照：**只有**单行式的日志 —— 一条别的形状都没有 ⇒ **不发**那条告警。
LOG_ONLY1 = ('2026-10-01 01:00:01.000 info: [webhook] Found M [abcd1234...]'
             ' on HDFans by MATCH from dataDir (/v/farm/M) - MATCH')
LOG_1 = os.path.join(tempfile.mkdtemp(), "info1only.log")
pathlib.Path(LOG_1).write_text(LOG_ONLY1, encoding="utf-8")
events.clear()
note6, m6 = D.reconcile_watch(argparse.Namespace(log=[LOG_1], db=None, db_path=None))
ck("★★ 对照：只有单行式 ⇒ **不发**其它形状的告警（判据非恒真）",
   [e[3] for e in events if e[0] == "alert"], [])

print()
if fails:
    print(f"★ {len(fails)} 条失败：")
    for f in fails:
        print("   " + f)
    sys.exit(1)
print("✓ 全过")
