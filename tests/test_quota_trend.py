# -*- coding: utf-8 -*-
"""额度感知（§16.1）与趋势（§16.3）的自测。

不碰生产、不联网：
  * cross-seed.db 与 state.db 都在临时目录里现造
  * 来源 B 用假 URL / 假响应打桩
"""
import importlib.util
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

REPO = str(Path(__file__).resolve().parent.parent)  # 仓库根
sys.path.insert(0, REPO)
# ★ 2026-10-05（B2）：容器代码收进 data/ ⇒ orchestrator / drive-loop 源码
#   都在 data/ 下。把 data/ 也加进 import 搜索路径（本文件上一行插的是仓库根）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))

from orchestrator import state as S  # noqa: E402

# ★ 输出强制 UTF-8：GBK 控制台下 ⑪(U+246A) 编不出去，会在中途炸掉。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

TMP = Path(tempfile.mkdtemp())
fails = []


def ck(label, got, want):
    ok = got == want
    print(("  ok  " if ok else " FAIL ") + f"{label}: {got!r}")
    if not ok:
        fails.append(f"{label}: got {got!r} want {want!r}")


def make_crossseed(path: Path, *, indexers, ts_rows):
    """indexers: [(id, name, url, status, retry_after_ms, active)]
       ts_rows : [(indexer_id, last_searched_ms)]"""
    con = sqlite3.connect(str(path))
    con.executescript("""
      CREATE TABLE indexer (id INTEGER PRIMARY KEY, name TEXT, url TEXT,
                            status TEXT, retry_after INTEGER, active INTEGER);
      CREATE TABLE timestamp (searchee_id INTEGER, indexer_id INTEGER,
                              last_searched INTEGER);
    """)
    con.executemany("INSERT INTO indexer VALUES(?,?,?,?,?,?)", indexers)
    con.executemany("INSERT INTO timestamp VALUES(?,?,?)",
                    [(i, iid, ms) for i, (iid, ms) in enumerate(ts_rows)])
    con.commit()
    con.close()


def make_state(path: Path, movies, attempts):
    # create=True：夹具的职责就是建库（生产默认不建，见 StateStore.__init__）
    with S.StateStore(path, create=True) as st:
        st.con.execute("INSERT INTO pack(name,root,created_at) VALUES('P','/r','t')")
        for mid, mi, stage in movies:
            st.con.execute(
                "INSERT INTO movie(id,pack,dir_name,path,stage,matched_indexers)"
                " VALUES(?,?,?,?,?,?)", (mid, "P", f"m{mid}", f"/r/m{mid}", stage, mi))
        for aid, mid, at, res, idx in attempts:
            st.con.execute(
                "INSERT INTO attempt(id,movie_id,at,kind,result,indexers)"
                " VALUES(?,?,?,?,?,?)", (aid, mid, at, "sync", res, idx))
        st.con.commit()


NOW = datetime(2026, 9, 12, 10, 0, 0)
ms = lambda dt: int(dt.timestamp() * 1000)          # noqa: E731


# --------------------------------------------------------------------------- #
print("== ① 来源 A：滚动 24 小时窗口（含边界）+ 退避状态 ==")
cs = TMP / "cs.db"
make_crossseed(cs, indexers=[
    (1, "HDFans", "http://p/1/api", "OK", None, 1),
    (2, "NanyangPT", "http://p/2/api", "RATE_LIMITED", ms(NOW + timedelta(hours=1)), 1),
    (3, "", "http://p/9/api", "OK", None, 0),          # 没名字 + 禁用
], ts_rows=[
    (1, ms(NOW - timedelta(hours=1))),                 # 窗口内
    (1, ms(NOW - timedelta(hours=23, minutes=59))),    # 窗口内（贴边）
    (1, ms(NOW - timedelta(hours=24, minutes=1))),     # ★窗口外
    (2, ms(NOW - timedelta(hours=2))),
    (3, ms(NOW - timedelta(hours=3))),                 # 禁用但有量 → 必须留着
])
q = S.quota_snapshot(cs, now=NOW)
by = {x.indexer: x for x in q}
ck("  HDFans 24h 部数（边界外那条不算）", by["HDFans"].searches_24h, 2)
ck("  NanyangPT 24h", by["NanyangPT"].searches_24h, 1)
ck("  没名字的走 url 兜底（/9/api → prowlarr#9）", "prowlarr#9" in by, True)
ck("  0 部的不排在最前（按量降序）", [x.indexer for x in q][0], "HDFans")
ck("  NanyangPT 正在退避（until 在未来）", by["NanyangPT"].active(NOW), True)
ck("  HDFans 没在退避", by["HDFans"].active(NOW), False)

print("\n== ② ★ status 残留 RATE_LIMITED 但解禁时间已过 → 不算「正在退避」 ==")
cs2 = TMP / "cs2.db"
make_crossseed(cs2, indexers=[(1, "HDFans", "", "RATE_LIMITED", ms(NOW - timedelta(hours=5)), 1)],
               ts_rows=[(1, ms(NOW))])
q2 = S.quota_snapshot(cs2, now=NOW)[0]
ck("  snoozed（见过限流）", q2.snoozed, True)
ck("  active（此刻挡路）", q2.active(NOW), False)
ck("  渲染里是「见过限流」不是「退避中」",
   "见过限流" in S.render_quota([q2]) and "退避中" not in S.render_quota([q2]), True)

print("\n== ③ 渲染：hide_idle 只藏「禁用且零用量」 ==")
lines = S.quota_snapshot(cs, now=NOW)
out = S.render_quota(lines, title="T", note="")
ck("  禁用但有用量的 prowlarr#9 仍在表里", "prowlarr#9" in out, True)
ck("  禁用且零用量的（若存在）被藏掉", "未列出" in out, False)   # 本例三条都有量
ck("  ! 号只在不含隐藏时出现", "另有" in out, False)

print("\n== ④ 来源 B：拿不到就只说原因，绝不编数 ==")
ck("  没 key", S.prowlarr_indexer_stats("http://x", ""), ({}, "未配置 Prowlarr API key"))
st_, note = S.prowlarr_indexer_stats("http://127.0.0.1:1", "k", timeout=0.5)
ck("  连不上 → 空表 + 原因", (st_, bool(note)), ({}, True))
kk = [x for x in lines if x.indexer == "HDFans"][0]
S.attach_prowlarr_quota(lines, "http://127.0.0.1:1", "k", timeout=0.5)
ck("  拿不到时 prowlarr_queries 留 None（不是 0）", kk.prowlarr_queries, None)
ck("  并且记下了原因", bool(kk.prowlarr_note), True)
ck("  拿不到时不参与 disagree（否则天天误报）", kk.disagrees, False)

print("\n== ⑤ 来源 B 解析（打桩，不联网） ==")
S.prowlarr_indexer_stats.__globals__["urllib"] = None      # 占位，见下
_orig = S.prowlarr_indexer_stats


class _Resp:
    def __init__(self, body):
        self._b = body if isinstance(body, bytes) else body.encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch(body, raise_=None):
    import types
    def fake(req, timeout=None):
        if raise_:
            raise raise_
        return _Resp(body)
    fake_mod = types.SimpleNamespace(
        request=types.SimpleNamespace(
            Request=lambda url, headers=None: url),
        urlopen=fake)
    return fake_mod


def run_b(body, raise_=None):
    """在用假的 urllib 环境里跑一次 prowlarr_indexer_stats。"""
    import urllib.request as real
    saved = real.urlopen
    real.urlopen = (lambda r, timeout=None: (_ for _ in ()).throw(raise_)) if raise_ \
        else (lambda r, timeout=None: _Resp(body))
    try:
        return S.prowlarr_indexer_stats("http://p:9696", "k")
    finally:
        real.urlopen = saved


ck("  结构不认识 → 空表 + 说明",
   run_b('{"foo": 1}')[1], "响应里没有 indexers 数组（本函数未对真实响应验证过）")
ck("  不是 JSON → 说明",
   run_b('not json')[1], "响应不是合法 JSON（多半是被拦了或拿到的不是 API 响应）")
d, n = run_b(json.dumps({"indexers": [
    {"indexerName": "HDFans", "numberOfQueries": 300},
    {"indexerName": "NanyangPT", "numberOfQueries": 120}]}))
ck("  正常结构 → 解析出来", (d, n), ({"HDFans": 300, "NanyangPT": 120}, ""))
d2, n2 = run_b(json.dumps({"indexers": [{"weird": 1}]}))
ck("  条目认不出 → 空表（不是空字典硬凑）", (d2, bool(n2)), ({}, True))

print("\n== ⑥ A/B 互校：只认「一边有、一边 0」 ==")
a = S.QuotaLine("X", searches_24h=100, prowlarr_queries=1000)
ck("  两边都非 0（倍数差）→ 不算对不上", a.disagrees, False)
b = S.QuotaLine("Y", searches_24h=0, prowlarr_queries=50)
ck("  我们 0、Prowlarr 有 → 对不上（有别的东西在吃额度）", b.disagrees, True)
c = S.QuotaLine("Z", searches_24h=9, prowlarr_queries=0)
ck("  我们有、Prowlarr 0 → 对不上（根本没收到）", c.disagrees, True)
e = S.QuotaLine("W", searches_24h=0, prowlarr_queries=None)
ck("  B 没拿到 → 不参与互校", e.disagrees, False)

# --------------------------------------------------------------------------- #
print("\n== ⑦ 趋势：按片取首次 + (周, 站) 归属 ==")
sd = TMP / "state.db"
make_state(sd,
    movies=[(1, '["HDFans"]', "SEEDING"),
            (2, '["HDFans","NanyangPT"]', "SEEDING"),
            (3, "[]", "SEEDING"),
            (4, '["HDFans"]', "UNMATCHED")],
    attempts=[
        # 片 1：先 seeding，后被别的 sync 反复写成 seeding —— 只能计一次
        (1, 1, "2026-09-08 10:00:00", "seeding", '["HDFans"]'),
        (2, 1, "2026-09-09 10:00:00", "matched", '["HDFans"]'),
        (3, 1, "2026-09-10 10:00:00", "seeding", '["HDFans"]'),      # 又一次 seeding
        # 片 2：跨周（上周）
        (4, 2, "2026-09-01 10:00:00", "seeding", '["HDFans","NanyangPT"]'),
        # 片 3：matched_indexers 空，但 attempt 首行有站 → 用兜底
        (5, 3, "2026-09-11 10:00:00", "seeding", '["HDFans","NanyangPT"]'),
        # 片 4：从没 seeding 过 —— 不该出现
        (6, 4, "2026-09-11 10:00:00", "unmatched", '["HDFans"]'),
    ])
with S.StateStore(sd, create=True) as st:
    rep = st.trend(weeks=8, now=datetime(2026, 9, 12))
ck("  总数（片 1 只算一次）", rep.total, 3)
ck("  未归属 = 0（兜底生效）", rep.unattributed, 0)
ck("  周标签升序", rep.weeks, ["2026-W36", "2026-W37"])
ck("  W37 合计", rep.week_totals["2026-W37"], 2)
ck("  W36 合计", rep.week_totals["2026-W36"], 1)
ck("  W37 / HDFans", rep.cells[("2026-W37", "HDFans")], 2)
ck("  W37 / NanyangPT（片 2、3 各一次）", rep.cells[("2026-W37", "NanyangPT")], 1)
ck("  没 seeding 的片 4 不在表内", ("2026-W37", "HDFans") in rep.cells, True)
ck("  渲染含本周与上周对比", ("本周" in rep.render() and "上周" in rep.render()), True)
# ★ 实测第一反应是「237 + 30 ≠ 237，数对不上」—— 渲染必须自己说明白
ck("  按站之和 > 总数时写明原因", "各记一次" in rep.render(), True)
ck("  总数标了「去重」", "部（去重）" in rep.render(), True)
# 片 1、2 都在 HDFans，片 2 还多一个站 → 按站相加必然大于去重总数
ck("  确实是「和 > 总数」这一支", sum(
    n for (w, _s), n in rep.cells.items() if w == "2026-W37") > rep.week_totals["2026-W37"], True)

print("\n== ⑧ 趋势：窗口裁剪 + 全空 ==")
with S.StateStore(sd, create=True) as st:
    rep2 = st.trend(weeks=1, now=datetime(2026, 9, 12))
ck("  只留 1 周（W36 被裁掉）", rep2.weeks, ["2026-W37"])
sd2 = TMP / "empty.db"
make_state(sd2, movies=[], attempts=[])
with S.StateStore(sd2, create=True) as st:
    ck("  空库不炸、给一句话", st.trend().render().startswith("新增做种趋势：暂无数据"), True)

print("\n== ⑨ 趋势：两边都没记站 → 落「未记站点」并被点名 ==")
sd3 = TMP / "state3.db"
make_state(sd3, movies=[(1, "[]", "SEEDING")],
           attempts=[(1, 1, "2026-09-11 10:00:00", "seeding", "[]")])
with S.StateStore(sd3, create=True) as st:
    r3 = st.trend(now=datetime(2026, 9, 12))
ck("  未归属计数", r3.unattributed, 1)
ck("  落进「未记站点」栏", r3.cells.get(("2026-W37", S.UNATTRIBUTED_SITE)), 1)
ck("  渲染里有提示", "没记到站点" in r3.render(), True)

print("\n== ⑩ 趋势：`(未记录)` 占位不算一个站 ==")
sd4 = TMP / "state4.db"
make_state(sd4, movies=[(1, '["HDFans"]', "SEEDING")],
           attempts=[(1, 1, "2026-09-11 10:00:00", "seeding", '["(未记录)","HDFans"]')])
with S.StateStore(sd4, create=True) as st:
    r4 = st.trend(now=datetime(2026, 9, 12))
ck("  只用 matched_indexers，不把占位当站", r4.sites, ["HDFans"])

# --------------------------------------------------------------------------- #
print("\n== ⑪ 每日台账：一天只投一次 ==")
spec = importlib.util.spec_from_file_location(
    "dl", REPO + "/data/drive-loop/scripts/drive-loop.py")
dl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dl)
dl.DAILY_FILE = TMP / ".daily-report.state"
# ★ 同理必须改向：`report_daily` 会调 `reconcile_watch`，而后者末尾要写
#   「每一格上次成功读到数」的台账 —— 不改向就跑一次测试往 scripts/ 里留一个
#   `.reconcile.state`（实测：就是这里漏了才留的）。
dl.RECONCILE_FILE = TMP / ".reconcile.state"

# ★★ 2026-10-05：通知写侧已砍 ⇒ 日报/告警不再 `emit()`，改 `LOG.info/warning`。
#   下面用一个可切换的假 LOG：`sent`/`bodies`/`alerts` 仍是**反推**出来的旧式形状，
#   好让既有断言少改。`key` 按正文标记反推。
class _CapLog:
    def __init__(self):
        self.info_calls = []      # [(text)]
        self.warn_calls = []

    def _emit(self, bucket, msg, a):
        bucket.append((msg % a) if a else msg)

    def info(self, msg, *a, **k):
        self._emit(self.info_calls, msg, a)

    def warning(self, msg, *a, **k):
        self._emit(self.warn_calls, msg, a)

    def debug(self, *a, **k):
        pass

    def exception(self, *a, **k):
        pass

    disabled = False


_cap = _CapLog()


def _install_log():
    dl.LOG = _cap
    _cap.info_calls.clear()
    _cap.warn_calls.clear()


def _warn_key(text):
    for marker, k in (("农场巡检", "farm-drift"), ("没查成", "farm-check-broken"),
                      ("参数解析错", "farm-check-broken"),
                      ("正在退避", "indexer-blocked"), ("退避中", "indexer-blocked"),
                      ("额度", "quota"), ("趋势", "trend")):
        if marker in text:
            return k
    return ""


_install_log()
dl._daily_set("")
_install_log()
import argparse
args = argparse.Namespace(db=str(sd), db_path=str(cs), dry_run=False)
ck("  第一次 → 写了", dl.report_daily(args), True)
ck("  第二次 → 拦住（一天一次）", dl.report_daily(args), False)
ck("  第一次的那条日报写出了（LOG.info）",
   any("每日台账" in t for t in _cap.info_calls), True)
ck("  force=True 能强行再写", dl.report_daily(args, force=True), True)

print("\n== ⑫ 每日台账：一节坏了不影响另一节 ==")
dl._daily_set("")
_install_log()
bad = argparse.Namespace(db=str(TMP), db_path=str(TMP / "nope-cs.db"),
                        dry_run=False)
ck("  两份都读不到 → 照样写出来", dl.report_daily(bad, force=True), True)
blob = "\n".join(_cap.info_calls)
ck("  额度那节写明了读不到", "站点额度台账：读不到" in blob, True)
ck("  趋势那节写明了算不出", "新增做种趋势：算不出" in blob, True)
ck("  正文里有「每日台账」", "每日台账" in blob, True)
dl._daily_set("")
ck("  读不到时**不**标今天已发（下一轮会重试）", dl._daily_last(), "")

print("\n== ⑬ 正在退避才告警；状态残留不告警 ==")
_install_log()
# ★ 必须把 now=NOW 传进去：本函数的「此刻」在生产里默认真实时钟，
#   而这里的 fixture 钉在 NOW（解禁 = NOW+1h）。不传的话，真实时间一过 NOW+1h，
#   「正在退避」就自愈成「已过期」—— 2026-09-12 就是这么在 11:00 整准时挂掉的。
dl.alert_blocked_indexers(argparse.Namespace(db_path=str(cs2)), now=NOW)   # 残留 RATE_LIMITED、已过期
ck("  残留状态（解禁已过）→ 不告警", len(_cap.warn_calls), 0)
dl.alert_blocked_indexers(argparse.Namespace(db_path=str(cs)), now=NOW)    # NanyangPT 正在退避
ck("  正在退避 → 告警 1 条", len(_cap.warn_calls), 1)
ck("  告警点名那个站（不同站互不覆盖）",
   ("NanyangPT" in _cap.warn_calls[0]) if _cap.warn_calls else False, True)
ck("  没有 db_path 时不炸",
   dl.alert_blocked_indexers(argparse.Namespace(db_path=None), now=NOW), 0)
ck("  库不存在时不炸",
   dl.alert_blocked_indexers(argparse.Namespace(db_path=str(TMP/"no.db")), now=NOW), 0)

print("\n== ⑭ 农场巡检：漂移/跑不起来都告警，干净不告警，且**永不 --prune** ==")
# ★ 这一段测的是 check_farm 的**判断**，不是路径解析，所以 first_existing 打桩成
#   「找得到脚本」—— 开发机上 CROSSSEED_DIRS 指的是 NAS 路径，不打桩就永远走"跳过"分支。
FARM_STUB = TMP / "build-farm.sh"
dl.FARM_CHECK_FILE = TMP / ".farm-check.state"
try:
    dl.FARM_CHECK_FILE.unlink()
except OSError:
    pass
_real_first_existing = dl.first_existing
dl.first_existing = lambda rel: str(FARM_STUB) if rel == "build-farm.sh" else None

alerts = _cap.warn_calls          # 直接就是告警正文列表（原 emit 的 body）
argvs = []


def _alert_text():
    return "\n".join(alerts)


def _runner(argv):
    """替掉 _run_verify：记下 argv（好断言"绝不 --prune"），返回预设的码与输出。"""
    argvs.append(list(argv))
    return _runner.rc, _runner.out


T0 = NOW.timestamp()

_install_log()          # ★ 清掉上一节（⑬）留下的告警，⑭ 从空开始
_runner.rc, _runner.out = 0, "★ 结论：无漂移（退出码 0）"
_n = dl.check_farm(force=True, now=T0, runner=_runner)
ck("  无漂移 → 摘要说无漂移", "无漂移" in _n, True)
ck("  无漂移 → **不发**告警", len(alerts), 0)
ck("  传给脚本的就是 --verify", argvs[-1][2:], ["--verify"])
ck("  ★ argv 里**没有** --prune（绝不自动删）", "--prune" in argvs[-1], False)

argvs.clear()
_n2 = dl.check_farm(now=T0 + 3600, runner=_runner)
ck("  20 小时内 → 不再起子进程", len(argvs), 0)
ck("  20 小时内 → 回的是**缓存**那一行", _n2, _n)

alerts.clear()
_runner.rc, _runner.out = 1, "源有但农场没有 : 3\n★ 结论：有漂移（退出码 1）"
_n3 = dl.check_farm(force=True, now=T0, runner=_runner)
ck("  有漂移 → 告警 1 条", len(alerts), 1)
ck("  摘要标了有漂移", "有漂移" in _n3, True)
_t = _alert_text()
ck("  正文带上了脚本的输出末尾", "源有但农场没有" in _t, True)
ck("  ★ 正文写明「只报告、不动手」", "只报告" in _t, True)
ck("  ★ 正文给了 --prune 但写明删不删由人定", "--prune" in _t, True)

alerts.clear()
_runner.rc, _runner.out = 2, "未知参数: --verify"
dl.check_farm(force=True, now=T0, runner=_runner)
ck("  退出码 2（不是 0 也不是 1）→ 也告警", len(alerts), 1)
ck("  ★ 文案点明「2 = 参数解析错」（不是「没查成」/「农场坏了」）",
   "参数解析错" in _alert_text(), True)
ck("  ★ 码 2 别指错方向：正文说「先核命令，别去动农场」",
   "先核命令" in _alert_text(), True)

alerts.clear()
_runner.rc, _runner.out = 3, "未知退出码"
dl.check_farm(force=True, now=T0, runner=_runner)
ck("  其它非 0/1/2 → 也算没查成", len(alerts), 1)
ck("  ★ 文案点明「根本没查成」", "根本没查成" in _alert_text(), True)

alerts.clear()
dl.first_existing = lambda rel: None
_n5 = dl.check_farm(force=True, now=T0, runner=_runner)
ck("  找不到 build-farm.sh → 不炸", isinstance(_n5, str), True)
ck("  找不到 → 说清楚是「跳过」", "跳过" in _n5, True)
# ★ 2026-10-05：现在这条也走 `LOG.warning`（"跳过"，不是"漂移"）——判据改成
#   "报的是跳过、**不是**漂移告警"（原来 emit 只在漂移/跑不起来时才发）。
ck("  找不到 → 报的是「跳过」而非「漂移」",
   (len(alerts) == 1 and "跳过" in alerts[0] and "漂移" not in alerts[0]), True)
dl.first_existing = _real_first_existing

_install_log()
dl._daily_set("")
dl.report_daily(argparse.Namespace(db=str(sd), db_path=str(cs), dry_run=False),
                force=True, farm_note=_n3)
ck("  ★ 那一行摘要进了每日台账正文",
   any("农场巡检" in t for t in _cap.info_calls), True)
ck("  台账正文里有「每日台账」", any("每日台账" in t for t in _cap.info_calls), True)

print("\n== ⑮ ★ 来源 B：只在 Prowlarr 里、不在我们索引器里的站必须显出来 ==")
# 背景（2026-09-12 实测）：attach_prowlarr_quota 原先**只遍历来源 A 的行**去 B 里查名字，
# 于是「Prowlarr 配了、我们不用的站」一行都不出现 —— 而那恰恰是
# 「有别的工具在用同一个 Prowlarr」的**唯一信号**（真机上就是 HDtime，8 次查询）。
_real_stats = S.prowlarr_indexer_stats


def _stub_stats(mapping, note=""):
    S.prowlarr_indexer_stats = lambda url, key, **kw: (mapping, note)


lines_e = S.quota_snapshot(cs, now=NOW)
_stub_stats({"HDFans": 300, "NanyangPT": 120, "HDtime": 8, "ZeroSite": 0})
S.attach_prowlarr_quota(lines_e, "http://p", "k", timeout=1)
by_e = {q.indexer: q for q in lines_e}

ck("  已有的行被填上 B 的数", by_e["HDFans"].prowlarr_queries, 300)
ck("  ★ 只在 Prowlarr 的 HDtime 被补成一行", "HDtime" in by_e, True)
ck("  该行标了 prowlarr_only", by_e["HDtime"].prowlarr_only, True)
ck("  ★ 该行 A 侧是 0（它本来就不属于我们）", by_e["HDtime"].searches_24h, 0)
ck("  ★★ 但**不算**「对不上」（它有自己的旗标，别重复报）",
   by_e["HDtime"].disagrees, False)
ck("  ★ 0 次查询的不补（那只是没用到的配置，不是信号）", "ZeroSite" in by_e, False)

out_e = S.render_quota(lines_e)
ck("  渲染里有 HDtime", "HDtime" in out_e, True)
ck("  ★ 有专门的旗标说明「不是本项目的索引器」", "不是本项目的索引器" in out_e, True)
ck("  ★ 外来行的用量打 `-` 而不是 `0`（0 会被读成「我们搜了 0 次」）",
   "HDtime                - 部" in out_e, True)

# ★ limit 是给"我们自己的站"防噪音的，不能把这一档截掉
out_l = S.render_quota(lines_e, limit=1)
ck("  limit=1 时我们自己的站只留 1 个", "NanyangPT" in out_l, False)
ck("  ★★ 但外来行**不被 limit 截掉**", "HDtime" in out_l, True)
# 不写死数字：fixture 里 `prowlarr#9` 也有一部用量（没被 hide_idle 藏掉），
# 所以"我们自己的"是 3 个而不是 2 个 —— 断言"有交代"这件事本身即可。
ck("  被截掉的有交代", "…还有" in out_l, True)

print("\n== ⑯ ★ 「某行名字对不上」绝不能渲染成「来源 B 不可用」 ==")
# 2026-09-12 现场踩到：B 明明好得很（HDtime 的数就是从它来的），
# 却因为一个对不上的旧行印出「来源 B 互校不可用」—— 纯属误导。
lines_m = S.quota_snapshot(cs, now=NOW)
_stub_stats({"HDFans": 300, "NanyangPT": 120})      # 故意不带 prowlarr#9
S.attach_prowlarr_quota(lines_m, "http://p", "k", timeout=1)
ck("  对不上的那行标了 mismatch",
   any(q.prowlarr_mismatch for q in lines_m), True)
ck("  ★★ 渲染里**没有**「互校不可用」", "来源 B 互校不可用" in S.render_quota(lines_m), False)


def _global_fail():
    """整体拿不到（连不上）→ 每行都该记同一个原因，渲染成「互校不可用」。"""
    _stub_stats({}, "连不上 Prowlarr")
    return S.render_quota(
        S.attach_prowlarr_quota(S.quota_snapshot(cs, now=NOW), "http://p", "k", timeout=1))


ck("  整体拿不到时才说「互校不可用」", "来源 B 互校不可用" in _global_fail(), True)
ck("  并且带上了原因", "连不上 Prowlarr" in _global_fail(), True)

S.prowlarr_indexer_stats = _real_stats

print()
if fails:
    print(f"!!! {len(fails)} 个失败")
    for f in fails:
        print("   " + f)
    sys.exit(1)
print("全部通过")
