# -*- coding: utf-8 -*-
"""钉住「qB 卡 999」的判据（`state.qb_999_band`）。

全离线：纯函数，不联网、不碰 NAS、不碰真库。

★ 这个判据最容易被悄悄改坏的地方**不是「算得对不对」**（那太简单），
  而是两个**看起来像噪声、其实是判据本身**的分界：

    ① **健康下载中被记进去**：现场就挂着一条 `progress=0.9975` 的
       真·下载中的种子。没有停滞闸，它会被天天记一笔 —— 而「重新下载」
       是用户要治的东西，「正常下载中」不是。这条用例的真值直接抄自
       #67 的实测（`last_activity` 是 0.0 小时前）。
    ② **阈值的位置**：同一颗种子，`last_activity` 在 23.3h 前时不记、
       在 25h 前时记。差的那 1.7 小时就是「24h」这个约定本身 ——
       没人钉它的话，随手把 `>` 写成 `>=`、或把小时数改成 12，
       表现都是「测试全绿、行为不同」。
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
# ★ 2026-10-05（B2）：容器代码收进 data/ ⇒ orchestrator / drive-loop 源码
#   都在 data/ 下。把 data/ 也加进 import 搜索路径（本文件上一行插的是仓库根）。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "data"))

try:
    sys.stdout.reconfigure(encoding="utf-8")   # GBK 控制台会炸（tests/README.md）
except Exception:
    pass

from orchestrator import state as S   # noqa: E402

DRIVE_LOOP = REPO / "data" / "drive-loop" / "scripts" / "drive-loop.py"


def _load(path, name):
    """用 importlib 载**真的那份**（tests/README.md 的规矩：别切源码再 exec）。

    ★ `sys.modules[name] = mod` 必须在 `exec_module` **之前** —— 否则被测文件里
      只要有 `@dataclass` 就炸，且报的是一个看着毫不相干的 AttributeError。
    """
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    with contextlib.redirect_stdout(io.StringIO()), \
         contextlib.redirect_stderr(io.StringIO()):
        try:
            spec.loader.exec_module(mod)
        except SystemExit:
            pass
    return mod

_ok = 0
_bad = 0


def ck(name: str, cond: bool, extra: str = "") -> None:
    global _ok, _bad
    if cond:
        _ok += 1
        print(f"  ok   {name}")
    else:
        _bad += 1
        print(f"  FAIL {name}{('  —— ' + extra) if extra else ''}")


NOW = 1_800_000_000                      # 定死，不让时间在跑
H = 3600


def T(h, progress=0.999, amount_left=4096, last_activity=NOW - 25 * H,
      state="stalledDL", **extra):
    """造一条 qB torrents/info 形状的记录。默认值 = 一条「应该被记」的。"""
    d = {"hash": h, "progress": progress, "amount_left": amount_left,
         "last_activity": last_activity, "state": state}
    d.update(extra)
    return d


print("=== ① 命中 / 不命中：现场那两条的真实数值 ===")
# 真值抄自 #67：这一条卡住了（23.3h）——
r = S.qb_999_band([T("stuck", progress=0.9999955830701261, amount_left=23755,
                     last_activity=NOW - int(23.3 * H))], NOW)
ck("★ 原样 23.3h 的**不**命中（阈值就在它上面一点，这是 24h 这个约定的位置）",
   r["n"] == 0, repr(r))
r = S.qb_999_band([T("stuck", progress=0.9999955830701261, amount_left=23755,
                     last_activity=NOW - 25 * H)], NOW)
ck("★ 同一颗，拨到 25h 前 → 命中", r["n"] == 1 and r["hashes"] == ["stuck"], repr(r))

# 真值抄自 #67：这一条是**健康下载中**的（0.0h）——
r = S.qb_999_band([T("healthy", progress=0.997501052321974, amount_left=250527744,
                     last_activity=NOW, state="downloading")], NOW)
ck("★★ 健康下载中的**不**命中（这条判据存在的理由就是它）", r["n"] == 0, repr(r))

r = S.qb_999_band([T("low", progress=0.985, last_activity=NOW - 48 * H)], NOW)
ck("没到 99% 的不命中", r["n"] == 0, repr(r))

r = S.qb_999_band([T("done", progress=1.0, amount_left=0,
                     last_activity=NOW - 48 * H)], NOW)
ck("下完了的（amount_left == 0）不命中", r["n"] == 0, repr(r))

print("\n=== ② 两条边界线 ===")
r = S.qb_999_band([T("edge-lo", progress=0.99, last_activity=NOW - 48 * H)], NOW)
ck("progress == 0.99 命中（下界是含等号的）", r["n"] == 1, repr(r))
r = S.qb_999_band([T("edge-lo2", progress=0.98999, last_activity=NOW - 48 * H)], NOW)
ck("progress == 0.98999 不命中", r["n"] == 0, repr(r))
r = S.qb_999_band([T("edge-t", last_activity=NOW - 24 * H)], NOW)
ck("停滞恰好 == 24h 整**不**命中（`>` 是严格的）", r["n"] == 0, repr(r))
r = S.qb_999_band([T("edge-t2", last_activity=NOW - 24 * H - 1)], NOW)
ck("停滞 24h 零 1 秒 → 命中", r["n"] == 1, repr(r))

print("\n=== ③ ★ 上界**故意**没写上（#67 的结论钉在这里） ===")
r = S.qb_999_band([T("upper", progress=1.0, amount_left=23755,
                     last_activity=NOW - 48 * H)], NOW)
ck("★★ progress 恰好 == 1.0 但 amount_left>0 且停滞 → **命中**", r["n"] == 1, repr(r))
ck("   （所以判据里不能出现 `progress < 1.0` —— 浮点舍入会误杀它）",
   "QB_999_MIN_PROGRESS" in dir(S) and not hasattr(S, "QB_999_MAX_PROGRESS"),
   repr([k for k in dir(S) if k.startswith("QB_999")]))

print("\n=== ④ error 不重复记（它有独立口径） ===")
r = S.qb_999_band([T("err", progress=0.9999, amount_left=23755,
                     last_activity=NOW - 48 * H, state="error")], NOW)
ck("state == 'error' 的不进本档", r["n"] == 0, repr(r))

print("\n=== ⑤ 分母与形状 ===")
mixed = [T("a"), T("b", progress=0.5), T("c", progress=0.999, amount_left=0)]
r = S.qb_999_band(mixed, NOW)
ck("n 只数命中的", r["n"] == 1, repr(r))
ck("★ total 是**全部**种子数（3），不是命中数 —— 否则「2 条」没有量纲",
   r["total"] == 3, repr(r))
r = S.qb_999_band([T("z"), T("a")], NOW)
ck("hashes 升序（基线与断言都要定序）", r["hashes"] == ["a", "z"], repr(r))
ck("返回的就是三把钥匙", set(r) == {"n", "hashes", "total"}, repr(set(r)))

print("\n=== ⑥ ★ 只出 hash，不出 name ===")
r = S.qb_999_band([T("h1", name="某部剧.S01.2160p"), T("h2", name="另一部.2026")], NOW)
ck("★ 结果里不含种子名（名字不该有机会流进正文/日志）",
   "某部剧" not in repr(r) and "另一部" not in repr(r), repr(r))
ck("  但命中的 hash 是齐的", r["hashes"] == ["h1", "h2"], repr(r))

print("\n=== ⑦ 不抛：空、None、缺字段 ===")
ck("空列表", S.qb_999_band([], NOW) == {"n": 0, "hashes": [], "total": 0})
ck("None（qB 回 null 时的形状）", S.qb_999_band(None, NOW) == {"n": 0, "hashes": [], "total": 0})
r = S.qb_999_band([{"hash": "bare"}], NOW)
ck("只有 hash 的记录不炸也不命中", r == {"n": 0, "hashes": [], "total": 1}, repr(r))
r = S.qb_999_band([{"hash": "n", "progress": None, "amount_left": None,
                    "last_activity": None, "state": None}], NOW)
ck("字段全是 None 也不炸", r == {"n": 0, "hashes": [], "total": 1}, repr(r))
r = S.qb_999_band([{"progress": 0.999, "amount_left": 4096, "last_activity": NOW - 48 * H}], NOW)
ck("没有 hash 的不进 hashes（否则基线里会出现 None）", r == {"n": 0, "hashes": [], "total": 1}, repr(r))

# ★ 文档化行为：last_activity 缺失 ⇒ 按「停了很久」算（宁可吵不可静默）
r = S.qb_999_band([{"hash": "no-la", "progress": 0.999, "amount_left": 4096}], NOW)
ck("★ last_activity 缺失 → 按停滞算、命中（宁可吵不可静默，同 test_backoff）",
   r["n"] == 1, repr(r))

print("\n=== ⑧ 不改动输入 ===")
one = T("keep")
before = dict(one)
S.qb_999_band([one], NOW)
ck("传进去的记录没被就地改过", one == before, repr(one))

print("\n=== ⑨ qb_999_watch：首读 / 一致 / 新增 / 缩回 / 读不到 ===")
D = _load(DRIVE_LOOP, "drive_loop_mod")
_NOW = time.time()

# ★ 生产会写文件，测试里必须改向（tests/README.md：`test_once_gate.py` §⑤ 的教训）
_td = tempfile.mkdtemp(prefix="qb999-")
D.DAILY_FILE = pathlib.Path(_td) / ".daily-report.state"
D.RECONCILE_FILE = pathlib.Path(_td) / ".reconcile.state"


def _q(h, stalled_h=25.0):
    """一条 qB 记录。默认 = 「应该被记」的；`stalled_h=0` 就是那条健康的。"""
    return {"hash": h, "progress": 0.999, "amount_left": 4096,
            "last_activity": _NOW - stalled_h * 3600, "state": "stalledDL"}


def _watch(torrents, *, url="http://h:3060", raises=False):
    def _all(u, **kw):
        if raises:
            raise OSError("qB 不通")
        return torrents
    D.S.qbit_all_torrents = _all
    return D.qb_999_watch(argparse.Namespace(qbit_url=url))


def _base_on_disk():
    return json.loads(D.RECONCILE_FILE.read_text(encoding="utf-8")).get(D.QB_999_BASELINE_KEY)


note, m = _watch([_q("aaa"), _q("bbb", stalled_h=0.0)])
ck("首读只记基线、**不**点名新增（#47 立的规矩）", "首次读数" in note and "新增" not in note, note)
ck("条数 1（那条健康下载中的被停滞闸挡掉）", m["qb_999"] == 1, repr(m))
ck("★ 分母一起进 metrics", m["qb_total"] == 2, repr(m))
ck("首读的 new 是 0（现状不是新闻）", m["qb_999_new"] == 0, repr(m))
ck("正文里有分母", "分母 2" in note, note)
ck("★ 基线落盘了", _base_on_disk() == ["aaa"], repr(_base_on_disk()))

note, m = _watch([_q("aaa")])
ck("同一集合再读 → 「与基线一致」", "与基线一致" in note, note)
ck("  且 new 仍是 0", m["qb_999_new"] == 0, repr(m))

note, m = _watch([_q("aaa"), _q("ccc")])
ck("冒出来一条 → 「新增 1 条」", "新增 1 条" in note, note)
ck("  new 计数进 metrics", m["qb_999_new"] == 1, repr(m))
ck("★ 正文里**没有**种子名（只报数不报名）", "aaa" not in note and "ccc" not in note, note)

note, m = _watch([_q("aaa")])
ck("缩回 → 「比基线少 1 条」", "比基线少 1 条" in note, note)
ck("★★ 缩回时**不许**说「与基线一致」（基线刚被改写成现状，那句是假话）",
   "与基线一致" not in note, note)

note, m = _watch([])
ck("清空 → 必须说出来（否则和「判据没读到」分不开）", "已清空" in note, note)
ck("  条数 0", m["qb_999"] == 0, repr(m))

note, m = _watch([], raises=True)
ck("读不到 → 不抛、给一句话", "读不到" in note, note)
ck("★ 读不到时 metrics 是 n/a（不是 0）", m == {"qb_999": "n/a", "qb_total": "n/a"}, repr(m))
ck("★ n/a 那一轮**不抹掉**已记下的基线", _base_on_disk() == [], repr(_base_on_disk()))

note, m = _watch([], url="")
ck("没 url → 跳过", "跳过" in note, note)
ck("  且没去调 qB", m == {"qb_999": "no-url", "qb_total": "no-url"}, repr(m))

print("\n=== ⑩ 接线：report_daily 里那一节，与对账同写一个文件不打架 ===")
_td2 = tempfile.mkdtemp(prefix="qb999-daily-")
D.DAILY_FILE = pathlib.Path(_td2) / ".daily-report.state"
D.RECONCILE_FILE = pathlib.Path(_td2) / ".reconcile.state"
_sent: dict = {}


def _fake_emit(kind, title, body="", *, key=None, metrics=None):
    _sent.update(kind=kind, title=title, body=body, metrics=metrics or {})
    return True


class _Trend:
    def render(self):
        return "（趋势：桩）"


class _Store:
    def __init__(self, path):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def trend(self, weeks=4):
        return _Trend()


D.S.StateStore = _Store
D.S.qbit_all_torrents = lambda u, **kw: [_q("ddd")]
D.S.qbit_tagged = lambda u, tag, **kw: [{"hash": "x"}] * 5
D.emit = _fake_emit
D.LOG.disabled = True
D.report_daily(argparse.Namespace(db_path=None, db="x.db", qbit_url="http://h:3060"),
               force=True)

ck("正文里有这一节", "qB 卡 999" in _sent["body"], _sent["body"][-300:])
ck("★ qb_999 进了 metrics（TSV 唯一的入口）", _sent["metrics"].get("qb_999") == 1,
   repr(_sent["metrics"]))
ck("★ 分母也在", _sent["metrics"].get("qb_total") == 1, repr(_sent["metrics"]))
ck("iyuu 那一节没被挤掉", _sent["metrics"].get("iyuu") == 5, repr(_sent["metrics"]))
ck("kind 仍是 batch（只记账、不告警）", _sent["kind"] == "batch", _sent["kind"])

_disk = json.loads(D.RECONCILE_FILE.read_text(encoding="utf-8"))
ck("★★ 跑完一整份日报后，卡 999 的基线**仍在** .reconcile.state 里",
   D.QB_999_BASELINE_KEY in _disk, repr(sorted(_disk)))
ck("   （= `qb_999_watch` 没被夹进 `reconcile_watch` 的读→写之间）",
   _disk.get(D.QB_999_BASELINE_KEY) == ["ddd"], repr(_disk.get(D.QB_999_BASELINE_KEY)))

print(f"\n{'=' * 60}")
print(f"断言 {_ok + _bad} 条：{_ok} 过 / {_bad} 失败")
sys.exit(0 if _bad == 0 else 1)
