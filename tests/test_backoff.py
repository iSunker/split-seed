# -*- coding: utf-8 -*-
"""退避检测的离线自测 —— 复刻 2026-09-12 那次「55 秒 snooze 被整段错过」的现场。

不起网络、不碰真库：cross-seed.db 用临时库伪造，post_webhook 与时钟全部注入。
"""
import os, pathlib, sqlite3, sys, tempfile, datetime
# 仓库根 = 本文件的上上级目录（tests/ 的上一层）。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
# ★ 2026-10-05（B2）：容器代码收进 data/ ⇒ orchestrator / drive-loop 源码
#   都在 data/ 下。把 data/ 也加进 import 搜索路径（本文件上一行插的是仓库根）。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "data"))
from orchestrator import state as S

# ★ 输出强制 UTF-8：GBK 控制台下 ⑪(U+246A) 编不出去，会在中途炸掉。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

Q = S.IndexerBackoff
fails = []


def ck(label, got, want):
    ok = got == want
    print(("  ok  " if ok else " FAIL ") + f"{label}: {got!r}")
    if not ok:
        fails.append(f"{label}: got {got!r} want {want!r}")


def make_db(rows):
    """rows: [(id, name, status, retry_after_ms|None, active)]"""
    p = os.path.join(tempfile.mkdtemp(), "cross-seed.db")
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE indexer (id INTEGER PRIMARY KEY, name varchar(255), "
                "url varchar(255), status varchar(255), retry_after INTEGER, "
                "active boolean)")
    for i, n, st, ra, act in rows:
        con.execute("INSERT INTO indexer VALUES(?,?,?,?,?,?)",
                    (i, n, f"http://prowlarr:9696/{i}/api", st, ra, act))
    con.commit()
    con.close()
    return p


def ms(dt):
    return int(dt.timestamp() * 1000)


class Clock:
    """假时钟：monotonic 和 now 一起走，sleep 只是把两者推快。"""

    def __init__(self, start=None):
        self.t = (start or datetime.datetime.now()).replace(microsecond=0)
        self.mono = 0.0
        self.slept = []

    # -- 注入给 DriveSession --
    def monotonic(self):
        return self.mono

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(round(s, 1))
        self.mono += s
        self.t += datetime.timedelta(seconds=s)

    def advance(self, s):
        self.mono += s
        self.t += datetime.timedelta(seconds=s)


def run_session(db, *, n=12, check_secs=60.0, max_wait=1800.0, interval=30.0,
                on_item=None, **kw):
    """跑一轮假批次，返回 (stats, session, clock)。"""
    clock = Clock()
    calls = [0]
    orig = S.post_webhook

    def fake_post(pth, **kwargs):
        calls[0] += 1
        if on_item:
            on_item(calls[0], clock)
        # 时间只由 run() 的 interval sleep（= 注入的 clock.sleep）推进，
        # 这里**不能**再 advance 一次，否则一个条目走 60 秒，检查节奏全乱。
        return 204

    S.post_webhook = fake_post
    try:
        sess = S.DriveSession(url="http://x", api_key="k", crossseed_db=db,
                              interval=interval, check_every=10, check_secs=check_secs,
                              max_wait=max_wait, sleep=clock.sleep, now=clock.now,
                              monotonic=clock.monotonic, **kw)
        stats = sess.run([f"/p/{i}" for i in range(n)])
        return stats, sess, clock
    finally:
        S.post_webhook = orig


def set_row(db, iid, status, retry_after, active=1):
    con = sqlite3.connect(db)
    con.execute("UPDATE indexer SET status=?, retry_after=?, active=? WHERE id=?",
                (status, retry_after, active, iid))
    con.commit()
    con.close()


print("== ① 现场复刻：55 秒的短退避发生在批次中途 ==")
db = make_db([(2, "HDFans", "OK", None, 1)])


def hit_429(call_no, clock):
    if call_no == 2:                     # 第 2 条发出后，cross-seed 被 429
        set_row(db, 2, "RATE_LIMITED", ms(clock.t + datetime.timedelta(seconds=55)))


st, sess, clock = run_session(db, n=12, on_item=hit_429)
ck("检出 1 次退避（修复前恒为 0）", st.backoff_hits, 1)
ck("没误判成中止", st.aborted, None)
ck("照发完了 12 条", st.ok, 12)
print(f"       等待序列: {clock.slept}  waited={st.waited_sec}")

print("\n== ② 退避窗口在两次检查之间**已经过去** —— 也要记这一笔 ==")
db2 = make_db([(2, "HDFans", "OK", None, 1)])


def hit_429_instant(call_no, clock):
    if call_no == 2:
        set_row(db2, 2, "RATE_LIMITED", ms(clock.t + datetime.timedelta(milliseconds=1)))


st2, _, _ = run_session(db2, n=12, on_item=hit_429_instant)
ck("已经解禁也记账（new retry_after ≠ 上次）", st2.backoff_hits, 1)
ck("已解禁就不必等", st2.waited_sec, 0.0)

print("\n== ③ 库里躺着一小时前的残值 —— 批次开头**不许**白记一笔 ==")
db3 = make_db([(2, "HDFans", "RATE_LIMITED", ms(datetime.datetime.now()
                                                - datetime.timedelta(hours=1)), 1)])
st3, _, _ = run_session(db3, n=12)
ck("残值不算命中", st3.backoff_hits, 0)
ck("残值不挡路", st3.waited_sec, 0.0)

print("\n== ④ 禁用的索引器（active=0）的退避不该挡路 ==")
db4 = make_db([(1, "prowlarr#1", "RATE_LIMITED",
                ms(datetime.datetime.now() + datetime.timedelta(hours=1)), 0)])
st4, sess4, _ = run_session(db4, n=12)
ck("不挡路", st4.waited_sec, 0.0)
ck("不记账", st4.backoff_hits, 0)
ck("不中止", st4.aborted, None)

print("\n== ⑤ 真·长退避正在挡路：记住 + 等到解禁 ==")
db5 = make_db([(2, "HDFans", "RATE_LIMITED",
                ms(datetime.datetime.now() + datetime.timedelta(seconds=120)), 1)])
st5, _, c5 = run_session(db5, n=12)
ck("记 1 笔", st5.backoff_hits, 1)
# 秒以下的小数来自测试造数据时 now() 的微秒（假时钟从整秒起步），给个区间即可
ck("等到解禁（≈122s）", 119 <= st5.waited_sec <= 124, True)
ck("13 条目全发完", st5.ok, 12)

print("\n== ⑥ 挡太久（> max_wait）→ 中止 ==")
db6 = make_db([(2, "HDFans", "RATE_LIMITED",
                ms(datetime.datetime.now() + datetime.timedelta(hours=3)), 1)])
st6, _, _ = run_session(db6, n=12, max_wait=1800.0)
ck("中止", bool(st6.aborted), True)
ck("一条都没发", st6.sent, 0)
# ★★ 中止要**分类**（#45）：这一种是**良性**的 —— 站点在退避、等到超过上限就先收工，
#    剩下的条目下轮重新排上。它和「webhook 401」是两件事，下游据此分开计数。
ck("★ 分类是 indexer-backoff（**良性**，不是真故障）",
   st6.aborted_kind, "indexer-backoff")

print("\n== ⑦ check_secs 关掉（=0）时退回老行为：只按条数检查 ==")
db7 = make_db([(2, "HDFans", "OK", None, 1)])


def hit_429_7(call_no, clock):
    if call_no == 2:
        set_row(db7, 2, "RATE_LIMITED", ms(clock.t + datetime.timedelta(seconds=55)))


st7, _, _ = run_session(db7, n=6, check_secs=0.0, on_item=hit_429_7)
ck("条数没到就不看（6 条 < 10）", st7.backoff_hits, 0)
ck("也就没有等待", st7.waited_sec, 0.0)

print("\n== ⑧ ★★ 两个计数分开：站点退避超时**不是**「批失败」（#45） ==")
# ★ 这一节钉的是**2026-09-13 生产里看到的那一格**：同一批的 TSV 既写
#   `ok=22 failed=0`，又写「连续 3 批失败（索引器 HDtime 要等到 …）」——
#   一个名字盖了两种模型（同 `NanyangPT` vs `NanyangPT (南洋)` 的形状）。
#   下面每一格都对着那条真实记录。
import importlib.util as _ilu                       # noqa: E402

_spec = _ilu.spec_from_file_location(
    "drive_loop_backoff", str(pathlib.Path(__file__).resolve().parent.parent
                              / "data" / "drive-loop" / "scripts" / "drive-loop.py"))
dl = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(dl)

_ev = []
dl.emit = lambda kind, title, body="", *, key=None, metrics=None: (
    _ev.append((kind, title, body, key, metrics)) or True)


def _backoff(reason="索引器 HDtime 要等到 2026-09-13 03:00:11（169 分钟 > 上限 30 分钟）"):
    return S.DriveStats(aborted=reason, aborted_kind="indexer-backoff", ok=22)


def _auth():
    return S.DriveStats(aborted="webhook 返回 401（鉴权/路径问题），已停",
                        aborted_kind="webhook-auth")


_ev.clear()
ck("良性收工 1 批 → (真失败, 收工) = (0, 1)",
   dl.update_abort_streak((0, 0), _backoff()), (0, 1))
ck("★ 只涨「收工」，**一点失败都没涨**", dl.update_abort_streak((0, 0), _backoff())[0], 0)

n = (0, 0)
for _ in range(3):
    n = dl.update_abort_streak(n, _backoff())
ck("连 3 批良性 → 计数到 3", n, (0, 3))
ck("★ 发的 alert key 是 consec-backoff（**不是** consec-abort）",
   [e[3] for e in _ev], ["consec-backoff"])
ck("★ 标题**不**出现「批失败」", "批失败" in _ev[0][1], False)
ck("★ 正文点明「不是失败」+ 说清下轮会重排",
   ("不是" in _ev[0][2] and "重新排上" in _ev[0][2]), True)
ck("★ 正文**不**再把人指去 force-recreate（那是上一版对退避场景的误导）",
   "force-recreate" in _ev[0][2], False)

# 反向：真故障走另一条路，key 与文案都不一样
_ev.clear()
ck("真故障 → (1, 0)：良性那格空着，不动",
   dl.update_abort_streak((0, 0), _auth()), (1, 0))
_ev.clear()
n = (0, 0)
for _ in range(3):
    n = dl.update_abort_streak(n, _auth())
ck("连 3 批真故障 → (3, 0)", n, (3, 0))
ck("★ 真故障的 key 是 consec-abort", [e[3] for e in _ev], ["consec-abort"])
ck("★ 真故障才说「批失败」", "批失败" in _ev[0][1], True)

# ★★ 互不干扰：中间夹一批良性，**不许**把真失败冲回 0 ——
#    这一格是本节存在的一半理由：写成"互相清零"的话，真实故障会被
#    交替出现的退避批次反复掩盖，**永远到不了 3**，告警等于没有。
_ev.clear()
_after = dl.update_abort_streak(dl.update_abort_streak((2, 0), _backoff()), _auth())
ck("★★ 真失败 ×2 → 夹 1 批良性 → 真失败**不被冲掉**", _after, (3, 1))
ck("★★ 而且这一批就报了（3 批真失败，中间那批良性没掩盖它）",
   [e[3] for e in _ev], ["consec-abort"])
# 反向：良性也不被真失败冲掉，且**总数**把两边都算进去（谁也别想被掩盖）
_ev.clear()
_s = dl.update_abort_streak((0, 0), _auth())        # 1 批真失败
for _ in range(3):
    _s = dl.update_abort_streak(_s, _backoff())     # 再 3 批良性
ck("真失败 1 + 良性 3 → (1, 3)", _s, (1, 3))
ck("★ 良性这边到 3 就报（那批真失败没把它冲回 0）",
   [e[3] for e in _ev], ["consec-backoff"])
ck("★ 混着出现时，标题把「共 4 批没跑成」写出来（否则 3 会被念成「连续 3 批」）",
   "共 4 批没跑成" in _ev[0][1], True)
# ★ 反向控制：**整批异常**也按真失败算（它可一点都"良性"不起来）
_ev.clear()
ck("整批异常 → 真失败",
   dl.update_abort_streak((0, 0), None, failed=True), (1, 0))
# ★ 兜底方向：`aborted` 非空却认不出种类 → **按真失败算**（宁可吵，不可静默）
ck("★ 认不出种类 → 按真失败算（宁可吵不可静默）",
   dl.update_abort_streak((0, 0), S.DriveStats(aborted="说不清的原因")), (1, 0))
# ★ 正常跑完（含"本包无待搜"）→ **两个都清零**（这是唯一的清零路径）
ck("★★ 正常跑完 → 两个计数都清零",
   dl.update_abort_streak((5, 5), S.DriveStats(ok=50)), (0, 0))
ck("没跑（stats=None 且没 failed）→ 也清零",
   dl.update_abort_streak((5, 5), None), (0, 0))
# ★ 常见情况下不补那句括号（补了是噪音）—— 上一条刚确认 5+5 被清零了，
#   这里单独跑一条干净的 3 连良性，确认标题就是简单的「连续 3 批提前收工」。
_ev.clear()
n = (0, 0)
for _ in range(3):
    n = dl.update_abort_streak(n, _backoff())
ck("★ 单一成因时不啰嗦（标题里没有「共 … 批」）", "共 " in _ev[0][1], False)

print("\n== ⑨ ★ 只禁了一部分站（还有健康站）→ 超上限也**不许中止**，照发 ==")
#   复刻 2026-09-13~09-15：HDtime 被 Prowlarr 禁满 24 小时，另 3 站健康。
#   旧行为 = 一批都不发 —— 那 3 天 49 批里 17 批「发出 0 条」，
#   发出率从 94.8% 掉到 27.7% → 3.7% → 7.1%。
#   ★ 实测依据：那三天 cross-seed 自己的 info 日志里 `Skipped searching` 计数是 **0**
#     （阳性对照：09-11 那天是 296）⇒ 它只在**过滤后一个站都不剩**时才跳过条目。
db8 = make_db([(1, "HDtime", "RATE_LIMITED",
                ms(datetime.datetime.now() + datetime.timedelta(hours=24)), 1),
               (2, "HDFans", "OK", None, 1),
               (3, "NanyangPT (南洋)", "OK", None, 1),
               (4, "BTSCHOOL", "OK", None, 1)])
ev8 = []
st8, _, c8 = run_session(db8, n=12, max_wait=1800.0,
                         indexers=["HDtime", "HDFans", "NanyangPT", "BTSCHOOL"],
                         on_event=lambda kind, *rest: ev8.append((kind, rest)))
ck("不中止", st8.aborted, None)
ck("★ 12 条全发完", st8.ok, 12)
ck("★ 一分钟也没白等", st8.waited_sec, 0.0)
ck("退避仍记一笔（下批据此放缓）", st8.backoff_hits, 1)
# ★ 检查点有 12/10 + 秒数两条腿 ⇒ 会被问很多次。警告必须只出一次，
#   否则日志里每 10 条刷一行同样的话 —— 那正好是"喊到人不再看它"。
skips = [r[0] for k, r in ev8
         if k == "warn" and "不等，照发" in str(r[0])]
ck("★ 「照发」只喊一次，不逐条刷屏", len(skips), 1)

print("\n== ⑩ ★ 全部站都在退避 → 仍然中止（⑨ 不许把这一支带松）==")
db9 = make_db([(1, "HDtime", "RATE_LIMITED",
                ms(datetime.datetime.now() + datetime.timedelta(hours=24)), 1),
               (2, "HDFans", "RATE_LIMITED",
                ms(datetime.datetime.now() + datetime.timedelta(hours=24)), 1)])
st9, _, _ = run_session(db9, n=12, max_wait=1800.0, indexers=["HDtime", "HDFans"])
ck("中止", bool(st9.aborted), True)
ck("一条都没发", st9.sent, 0)
ck("分类仍是 indexer-backoff", st9.aborted_kind, "indexer-backoff")

print("\n== ⑪ 库里的名字带括号后缀（'NanyangPT (南洋)'）仍算同一站 ==")
#   ★ 这一格让**归一化本身承重**：两站**都在退避**，而 `--indexers` 里写的是短名。
#     归一化对了 → 认出 'nanyangpt' 就是名单里的那一站 → 「全在退避」→ 中止；
#     归一化错了（拿 'nanyangpt (南洋)' 去比 'nanyangpt'）→ 以为还剩 1 个健康站 → 照发。
#   ★ 两种写法的断言**正好相反**，所以这一格钉的是实现、不是在钉常量。
#     （反例：写成「另 3 站健康、只禁 HDtime 一个」的话，健康站压根不进 `blocking`，
#       归一化整个坏掉也照样通过 —— 那是假测试。）
db10 = make_db([(1, "HDtime", "RATE_LIMITED",
                 ms(datetime.datetime.now() + datetime.timedelta(hours=24)), 1),
                (2, "NanyangPT (南洋)", "RATE_LIMITED",
                 ms(datetime.datetime.now() + datetime.timedelta(hours=24)), 1)])
st10, _, _ = run_session(db10, n=12, max_wait=1800.0,
                         indexers=["HDtime", "NanyangPT"])
ck("括号后缀归一后认得出是同名站 ⇒ 判「全在退避」⇒ 中止", bool(st10.aborted), True)
ck("一条都没发", st10.sent, 0)

print("\n== ⑫ 全部退避且**没传名单** → 保守中止（兜底方向宁可吵）==")
#   同 ⑥，但显式钉住"不知道配置了哪些站时不改行为"这一条兜底。
db11 = make_db([(1, "HDtime", "RATE_LIMITED",
                 ms(datetime.datetime.now() + datetime.timedelta(hours=24)), 1)])
st11, _, _ = run_session(db11, n=12, max_wait=1800.0)
ck("说不知道就照旧中止", bool(st11.aborted), True)
ck("一条都没发", st11.sent, 0)

print("\n== ⑬ 退避时刻**必须带日期**（#114：跨午夜分不清今天/明天）==")
#   ★ 现场（2026-09-15 13:39 的 drive-loop.log）：
#     `!  索引器 HDtime 被限流（RATE_LIMITED），解禁 01:31:11`
#   看着像"当天凌晨"，实际是**次日** 01:31 —— cross-seed 侧窗口跳到了明天。
#   同一时间轴上 4 行之外还有 Prowlarr 侧的 `disabledTill 09-16 13:32`，
#   两个读数差 12 小时，**审阅时被当成"其中一个抄错了"**。
#   ⇒ 判据：那条 warn 正文里必须能看见**日期**。
#
#   ★★ 要走到那条 warn，条件很具体（第一版测试就栽在这）：
#     `:2533` 的 warn **只在 snoozed 里发** —— 即「状态是限流类 **且** 解禁时间已过」。
#     它不是 abort 那条路（`:2589` 本来就带完整日期），也不是 `:2592` 的 wait 那条。
#     ⇒ 造一个**刚刚过期**的退避（`now - 1s`，复刻实测 HDFans 55 秒那种）。
#   ★★ 而且**必须在批次中途**才触发：批前那一眼 `baseline` 为真、且
#     `b.active()` 已为假 ⇒ 不算「新发生」。所以用 on_item 在第 2 条之后写库。
db12 = make_db([(2, "HDFans", "OK", None, 1)])


def expire_now(call_no, clock):
    if call_no == 2:      # 解禁时刻 = 此刻 − 1 秒 ⇒ snoozed 且 active 为假
        set_row(db12, 2, "RATE_LIMITED", ms(clock.t - datetime.timedelta(seconds=1)))


_ev.clear()
st12, sess12, clock12 = run_session(db12, n=8, on_item=expire_now,
                                    on_event=lambda *a: _ev.append(a))
_warn = " ".join(a[1] for a in _ev if a and a[0] == "warn")
import re as _re
ck("★ 真的走到了那条 warn（不是空集上做断言 —— 空集上任何正则都'成立'）",
   bool(_re.search(r"被限流", _warn)), True)
ck("退避正文里带**日期**（MM-DD HH:MM）",
   bool(_re.search(r"\d{2}-\d{2} \d{2}:\d{2}", _warn)), True)
#   ★ 阴性对照：**旧写法**（只有 HH:MM:SS）必须判不出来 ——
#     否则上面那条判据是恒真的，等于没测。
ck("阴性对照：只有 HH:MM:SS 的旧写法**判不出来**",
   bool(_re.search(r"\d{2}-\d{2} \d{2}:\d{2}", "索引器 HDtime 被限流（RATE_LIMITED），解禁 01:31:11")), False)

print("\n== ⑭ ★★ 「已过去的残值」不许说成「解禁」（#116）==")
#   现场（2026-09-16 10:41:06，实测行）：
#     `索引器 HDtime 被限流（RATE_LIMITED），解禁 10:21:19`
#     `索引器 NanyangPT (南洋) …，解禁 09:22:06`
#   ★ 两个「解禁」**都早于**触发时刻 10:41 —— 与 `cross-seed.db` 的 `retry_after`
#     **逐秒相同**（权威值：HDtime 10:21:19 / NanyangPT 09:22:06）
#     ⇒ **数没印错，是"解禁"这个词印错了**：它不是「将要解禁」，
#        而是「上次那个窗口**已经在 … 结束了**」。
#   ★ 代价（真实发生）：按"将要解禁"读 ⇒ 得到"时间倒流" ⇒ 把一条**陈旧残值**
#     读成「今天上午又被限了一次」，而那天**根本没有**新的限流事件
#     （09-16 全天 `要等到` 计数 = 0）。
#   ★ 这一格与 ⑬ 的关系：⑬ 管**格式**（要带日期），⑭ 管**语义**（过去 ≠ 将来）。
#     两条**不重叠** —— 旧写法（带日期、但说"解禁"）在 ⑬ 是绿的，在这里必须红。
db13 = make_db([(2, "HDFans", "OK", None, 1)])


def expire_2min(call_no, clock):
    # 第 2 条之后写一个**已过去 2 分钟**的窗口 ⇒ 走 snoozed 分支
    if call_no == 2:
        set_row(db13, 2, "RATE_LIMITED", ms(clock.t - datetime.timedelta(minutes=2)))


_ev.clear()
run_session(db13, n=8, on_item=expire_2min,
            on_event=lambda *a: _ev.append(a))
_w14 = " ".join(a[1] for a in _ev if a and a[0] == "warn")
ck("★ 走到了（不是空集上断言）", bool(_re.search(r"曾被限流|被限流", _w14)), True)
ck("★★ 说的是「窗口已于 … 结束」而不是「解禁」",
   ("窗口已于" in _w14 and "结束" in _w14), True)
ck("★★ 且**不出现**「解禁」（那个词只属于**未来**的时刻）",
   "解禁" in _w14, False)
ck("★ 明说它**当下不挡路**（否则读者会以为还要等它）",
   "当下不挡路" in _w14, True)
#   ★★ 阴性对照：**光看格式判不出来** —— 旧文案（带日期 + 说"解禁"）满足 ⑬ 的判据，
#      但它是**错的**。这一条钉的就是"⑭ 必须比 ⑬ 强"。
_old_good_date = "索引器 HDtime 被限流（RATE_LIMITED），解禁 09-16 10:21:19"
ck("阴性对照：旧文案**满足 ⑬ 的日期判据**（所以 ⑬ 单独不够）",
   bool(_re.search(r"\d{2}-\d{2} \d{2}:\d{2}", _old_good_date)), True)
ck("★★ 但旧文案**通不过 ⑭**（它把过去说成了将来）",
   ("窗口已于" in _old_good_date), False)

print()
if fails:
    print(f"!!! {len(fails)} 个失败")
    for f in fails:
        print("   " + f)
    sys.exit(1)
print("全部通过")
