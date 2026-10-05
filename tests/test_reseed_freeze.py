# -*- coding: utf-8 -*-
"""钉住「装不出来 / 没 peer」的判据与它的 watcher。

全离线：纯函数 + 打桩的 qB，不联网、不碰 NAS、不碰真库。

★ 这个判据最容易被悄悄改坏的地方**不是「算得对不对」**（那太简单），
  而是几条**看起来像噪声、其实是判据本身**的分界：

    ① **阈值的位置**：`amount_left/size` 恰好 `1e-3` 记、比它大一点就不记。
       没人钉它的话，随手把 `>` 写成 `>=`、或把 1e-3 改成 1e-2，
       表现都是「测试全绿、行为不同」——而两者之间**实测是一条断层**（见
       `orchestrator/state.py` 里那张分布表），改了就正好落进空档里。
    ② **「没 peer」与「装不出来」不许混**：两条判据**正交**，且**处置方向相反**。
       把 `availability == 0` 那条并进主判据，就会让一个数同时意味着两件事。
    ③ **健康下载中不许被记**：现场挂着 6 条真在下、进度正常的种子
       （`(0.5, 1]` 那一档）。这一档**必须**留在判据外面。
    ④ **读失败 ≠ 基线是空**：前者给 `n/a`、后者给 0，TSV 里必须分得开。

★ 本文件里**明确标注**了哪几条是在钉「当前行为」而不是「期望行为」（见 §⑧）。
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


GB = 1 << 30
#: 现场那颗载荷（`.mkv`）的**真实字节数** —— 抄自 2026-09-18 实测。
#: ★ 用它当分母不是装饰：附件只有几百 KB，而载荷是 9 GB，
#:   所以「只差附件」的缺口自然落在 1e-3 以下 —— 这就是断层的成因。
MKV = 9_658_005_448


def U(h, *, left=2_097_152, size=MKV, state="stalledDL", **extra):
    """造一条「装不出来」形状的记录（= 应该被记的那一类）。"""
    d = {"hash": h, "amount_left": left, "size": size, "state": state,
         "tags": ""}
    d.update(extra)
    return d


def NP(h, *, size=40 * GB, **extra):
    """造一条「没 peer」形状的记录（一个字节都没下到）。"""
    d = {"hash": h, "amount_left": size, "size": size, "state": "stalledDL",
         "num_seeds": 0, "availability": 0.0, "tags": ""}
    d.update(extra)
    return d


print("=== ① 命中：抄自 2026-09-18 现场的三条真实读数 ===")
# 真值：state=stalledDL, amount_left=2097152, size=9658005448, seeds=0, avail=0.999
r = S.reseed_unbuildable_band([U("a", left=2097152)])
ck("★ 原样的缺口（2097152 / 9658005448）→ 命中",
   r["n"] == 1 and r["hashes"] == ["a"], repr(r))
r = S.reseed_unbuildable_band([U("b", left=2549942)])
ck("  另一条实测值（2549942）也命中", r["n"] == 1, repr(r))
r = S.reseed_unbuildable_band([U("c", left=7951007)])
ck("  第三条（7951007，约 8e-4）仍命中", r["n"] == 1, repr(r))

print("\n=== ② ★ 两条判据正交：「没 peer」的不进「装不出来」 ===")
r = S.reseed_unbuildable_band([NP("np1")])
ck("★★ 一个字节都没下到的**不**命中主判据（缺口是「全缺」不是「小」）",
   r["n"] == 0, repr(r))
r = S.reseed_no_peer_band([NP("np1")])
ck("  但它命中「没 peer」", r["n"] == 1, repr(r))
r = S.reseed_no_peer_band([U("a")])
ck("★ 反过来：「装不出来」的**不**命中「没 peer」（缺口小、且 avail 未必是 0）",
   r["n"] == 0, repr(r))

print("\n=== ③ ★ 阈值的位置（1e-3 这个数本身） ===")
r = S.reseed_unbuildable_band([U("edge", left=int(MKV * 1e-3))])
ck("缺口恰好 == 1e-3 命中（上界是含等号的）", r["n"] == 1, repr(r))
r = S.reseed_unbuildable_band([U("edge2", left=int(MKV * 1e-3) + 1)])
ck("缺口比 1e-3 大一点点 → 不命中（实测那一段是断层，越过去就是另一类）",
   r["n"] == 0, repr(r))

print("\n=== ④ ★ 健康下载中不许被记（现场 6 条） ===")
r = S.reseed_unbuildable_band([U("dl", left=int(MKV * 0.4))])
ck("★★ 真在下、缺口 40% 的**不**命中", r["n"] == 0, repr(r))
r = S.reseed_no_peer_band([U("dl", left=int(MKV * 0.4), num_seeds=5, availability=0.8)])
ck("★★ 有 peer 的**不**命中「没 peer」", r["n"] == 0, repr(r))
r = S.reseed_no_peer_band([NP("np2", num_seeds=1)])
ck("★ 只要有 1 个 peer 就不算「没起来」（它是在下，只是慢）", r["n"] == 0, repr(r))
r = S.reseed_no_peer_band([NP("np3", availability=0.999)])
ck("★ 见过源碎片（avail≠0）也不进「没 peer」—— 那是另一回事",
   r["n"] == 0, repr(r))

print("\n=== ⑤ 已完成的不算（两条判据都要） ===")
r = S.reseed_unbuildable_band([U("done", left=0)])
ck("amount_left == 0 不命中主判据", r["n"] == 0, repr(r))
r = S.reseed_no_peer_band([U("done", left=0, num_seeds=0, availability=0.0)])
ck("amount_left == 0 不命中「没 peer」", r["n"] == 0, repr(r))

print("\n=== ⑥ error / missingFiles 有独立口径，不重复记 ===")
r = S.reseed_unbuildable_band([U("err", state="error")])
ck("state == 'error' 的不进主判据", r["n"] == 0, repr(r))
r = S.reseed_unbuildable_band([U("mf", state="missingFiles")])
ck("state == 'missingFiles' 的也不进", r["n"] == 0, repr(r))

print("\n=== ⑦ 分母与形状 ===")
mixed = [U("a"), U("b", left=int(MKV * 0.4)), U("c", left=0)]
r = S.reseed_unbuildable_band(mixed)
ck("n 只数命中的", r["n"] == 1, repr(r))
ck("★ total 是**全部**种子数（3），不是命中数 —— 否则「3 条」没有量纲",
   r["total"] == 3, repr(r))
r = S.reseed_unbuildable_band([U("z"), U("a")])
ck("hashes 升序（基线与断言都要定序）", r["hashes"] == ["a", "z"], repr(r))
ck("返回的就是三把钥匙", set(r) == {"n", "hashes", "total"}, repr(set(r)))
r = S.reseed_no_peer_band([NP("z"), NP("a")])
ck("「没 peer」同形（n / hashes / total）", set(r) == {"n", "hashes", "total"}, repr(set(r)))

print("\n=== ⑧ ★ 只出 hash，不出 name ===")
r = S.reseed_unbuildable_band([U("h1", name="某部剧.S01.2160p"),
                               U("h2", name="另一部.2026")])
ck("★ 结果里不含种子名（名字不该有机会流进正文/日志）",
   "某部剧" not in repr(r) and "另一部" not in repr(r), repr(r))
ck("  但命中的 hash 是齐的", r["hashes"] == ["h1", "h2"], repr(r))

print("\n=== ⑨ 不抛：空、None、缺字段 ===")
ck("空列表", S.reseed_unbuildable_band([]) == {"n": 0, "hashes": [], "total": 0})
ck("None（qB 回 null 时的形状）",
   S.reseed_unbuildable_band(None) == {"n": 0, "hashes": [], "total": 0})
ck("「没 peer」也一样", S.reseed_no_peer_band(None) == {"n": 0, "hashes": [], "total": 0})
r = S.reseed_unbuildable_band([{"hash": "bare"}])
ck("只有 hash 的记录不炸也不命中", r == {"n": 0, "hashes": [], "total": 1}, repr(r))
r = S.reseed_unbuildable_band([{"hash": "n", "amount_left": None, "size": None,
                                "state": None}])
ck("字段全是 None 也不炸", r == {"n": 0, "hashes": [], "total": 1}, repr(r))
# ★ 文档化行为：size 缺失/为 0 ⇒ **不命中**（不是当 0 算）。
#   理由：算不出比值就**不声称** —— 这里的误报会把一条正常大种子说成坏了。
r = S.reseed_unbuildable_band([{"hash": "nosize", "amount_left": 4096}])
ck("★ size 缺失 → 不命中（算不出比值就不声称，宁可漏不可误报）",
   r["n"] == 0, repr(r))
r = S.reseed_unbuildable_band([{"hash": "zsize", "amount_left": 4096, "size": 0}])
ck("★ size == 0 → 同样不命中（防 ZeroDivisionError）", r["n"] == 0, repr(r))
r = S.reseed_no_peer_band([{"hash": "zsize2", "amount_left": 4096, "size": 0}])
ck("  「没 peer」也走同一条兜底", r["n"] == 0, repr(r))
r = S.reseed_unbuildable_band([{"amount_left": 4096, "size": MKV}])
ck("没有 hash 的不进 hashes（否则基线里会出现 None）",
   r == {"n": 0, "hashes": [], "total": 1}, repr(r))

print("\n=== ⑩ 不改动输入 ===")
one = U("keep")
before = dict(one)
S.reseed_unbuildable_band([one])
S.reseed_no_peer_band([one])
ck("传进去的记录没被就地改过", one == before, repr(one))

print("\n=== ⑪ reseed_freeze_watch：首读 / 一致 / 新增 / 缩回 / 读不到 ===")
D = _load(DRIVE_LOOP, "drive_loop_freeze_mod")

# ★ 生产会写文件，测试里必须改向（tests/README.md：`test_once_gate.py` §⑤ 的教训）
_td = tempfile.mkdtemp(prefix="fz-")
D.RESEED_FREEZE_FILE = pathlib.Path(_td) / ".reseed-freeze.state"
D.RECONCILE_FILE = pathlib.Path(_td) / ".reconcile.state"
D.DAILY_FILE = pathlib.Path(_td) / ".daily-report.state"

_alerts: list = []


def _fake_emit(kind, title, body="", *, key=None, metrics=None):
    _alerts.append({"kind": kind, "title": title, "body": body,
                    "key": key, "metrics": metrics or {}})
    return True


D.emit = _fake_emit


def _watch(torrents, *, url="http://h:3060", raises=False):
    def _all(u, **kw):
        if raises:
            raise OSError("qB 不通")
        return torrents
    D.S.qbit_all_torrents = _all
    _alerts.clear()
    return D.reseed_freeze_watch(argparse.Namespace(qbit_url=url))


def _base_on_disk():
    return json.loads(D.RESEED_FREEZE_FILE.read_text(encoding="utf-8")) \
        .get(D.RESEED_FREEZE_KEY)


_OUR = "cross-seed"
_IY = "IYUU自动辅种"


def _ours(h, **kw):
    return U(h, tags=_OUR, **kw)


def _iyuu_np(h, **kw):
    return NP(h, tags=_IY, **kw)


note, m = _watch([_ours("aaa"), _ours("bbb", left=int(MKV * 0.4))])
ck("首读只记基线、**不**告警（#47 立的规矩）", "首次读数" in note, note)
ck("★ 首读不发 alert（现状不是新闻）", _alerts == [], repr(_alerts))
ck("条数 1（那条缺口 40% 的被判据挡掉）", m["fz"] == 1, repr(m))
ck("★ 分母一起进 metrics", m["fz_total"] == 2, repr(m))
ck("首读的 new 是 0", m["fz_new"] == 0, repr(m))
ck("正文里有分母", "分母 2" in note, note)
ck("★ 基线落盘了（自己的文件，不塞 .reconcile.state）",
   _base_on_disk()["unbuildable"][_OUR] == ["aaa"], repr(_base_on_disk()))

note, m = _watch([_ours("aaa")])
ck("同一集合再读 → 「与基线一致」", "与基线一致" in note, note)
ck("  且 new 仍是 0", m["fz_new"] == 0, repr(m))
ck("★ 一致时也**不**告警", _alerts == [], repr(_alerts))

note, m = _watch([_ours("aaa"), _ours("ccc")])
ck("冒出来一条 → 「新增 1 条」", "新增 1 条" in note, note)
ck("  new 计数进 metrics", m["fz_new"] == 1, repr(m))
# ★★ 2026-10-05：通知写侧已砍 ⇒ `reseed_freeze_watch` 内**不再 `emit(alert)`**，
#   改 `LOG.warning` 一条（无人值守也能看见）。下面三条断言**语义不变**
#   （"真新增时要有一条告警、key 是固定字面量、正文不含种子名/hash"），
#   取证点从 `_alerts` 换成上面的 `note`。★ 若将来恢复写侧，这几条要跟着改回。
ck("★ 真新增时**要有一条告警**（现走 LOG.warning；正文里带「新增」）",
   "新增 1 条" in note, note)
ck("★★ 告警正文里**没有**种子名 / hash（只报数不报名）",
   "aaa" not in note and "ccc" not in note, note)

note, m = _watch([_ours("aaa")])
ck("缩回 → 「比基线少 1 条」", "比基线少 1 条" in note, note)
ck("★★ 缩回时**不许**说「与基线一致」（基线刚被改写成现状，那句是假话）",
   "与基线一致" not in note, note)
ck("★ 缩回**不**告警（静默采纳）", _alerts == [], repr(_alerts))

note, m = _watch([])
ck("清空 → 必须说出来（否则和「判据没读到」分不开）", "已清空" in note, note)
ck("  条数 0", m["fz"] == 0, repr(m))

print("\n=== ⑫ ★ IYUU 只记数、也要报出来（用户原话：「也要添加到邮件通知里」） ===")
_td2 = tempfile.mkdtemp(prefix="fz-iyuu-")
D.RESEED_FREEZE_FILE = pathlib.Path(_td2) / ".reseed-freeze.state"
note, m = _watch([_ours("aaa"), _iyuu_np("iy1"), _iyuu_np("iy2")])
ck("正文里**显式**列出 IYUU 那一格（不被静默滤掉）",
   "IYUU" in note and "只记数" in note, note)
ck("★ 条数把 IYUU 的算进去（不然它就从通知里消失了）", m["fz"] == 3, repr(m))
ck("★ 两格分开报：装不出来 1 / 没 peer 2", m["fz_ours"] == 1 and m["fz_np"] == 2, repr(m))
ck("★ IYUU 那一格单独进 metrics", m["fz_iyuu"] == 2, repr(m))
ck("★ 「没 peer」里的 IYUU 也单独记", m["fz_np_iyuu"] == 2, repr(m))
ck("★★ 首次读数**不**告警（IYUU 这几条是存量，不是今天冒出来的）",
   _alerts == [], repr(_alerts))

print("\n=== ⑬ ★★ 组迁移要能被看见（同一条种子换了个组 = 真变了） ===")
# aaa 从「装不出来」变成「没 peer」—— 拍平时若不带上组名，这个变化**看不见**。
note, m = _watch([NP("aaa", tags=_OUR), _iyuu_np("iy1"), _iyuu_np("iy2")])
ck("★★ 同 hash 换组 → 算「新增」（拍平必须带组名前缀）", m["fz_new"] == 1, repr(m))

print("\n=== ⑭ 读不到 / 没 url ===")
note, m = _watch([], raises=True)
ck("读不到 → 不抛、给一句话", "读不到" in note, note)
ck("★ 读不到时 metrics 全是 n/a（不是 0）",
   m["fz"] == "n/a" and m["fz_new"] == "n/a" and m["fz_total"] == "n/a", repr(m))
ck("★ n/a 那一轮**不抹掉**已记下的基线", _base_on_disk() is not None, repr(_base_on_disk()))
ck("★ 读不到时**不**告警", _alerts == [], repr(_alerts))

note, m = _watch([], url="")
ck("没 url → 跳过", "跳过" in note, note)
ck("  且没去调 qB", m["fz"] == "no-url", repr(m))

print("\n=== ⑮ ★ 坏掉的基线 ≠ 空基线（前者静默起重锚，不假警报） ===")
D.RESEED_FREEZE_FILE.write_text("{ 这不是 JSON", encoding="utf-8")
note, m = _watch([_ours("aaa")])
ck("★★ 基线文件坏了 → 当「首次读数」处理，**不**把所有条目报成新增",
   "首次读数" in note and _alerts == [], f"{note} / {_alerts}")
ck("  且能把新的基线写回去（自愈）",
   _base_on_disk()["unbuildable"][_OUR] == ["aaa"], repr(_base_on_disk()))

print("\n=== ⑯ ★ metrics 键名卫生（写错会静默污染 notify-spool 的批次统计） ===")
_names = set(m)
ck("★★ 键名里**不许**恰好有一个叫 `pack` 的", "pack" not in _names, repr(sorted(_names)))
ck("★ 键名一律 fz_ 前缀（除 fz 本身）",
   all(k == "fz" or k.startswith("fz_") for k in _names), repr(sorted(_names)))
ck("★ 键名不含空格（notify.py:_render 会把空格换成 _，可能裂出 `pack`）",
   all(" " not in k for k in _names), repr(sorted(_names)))
ck("★ 值也不含空格（同上，值里的空格同样会被换掉）",
   all(" " not in str(v) for v in m.values()), repr(m))

print("\n=== ⑰ 接线：report_daily 里那一节 ===")
_td3 = tempfile.mkdtemp(prefix="fz-daily-")
D.RESEED_FREEZE_FILE = pathlib.Path(_td3) / ".reseed-freeze.state"
D.RECONCILE_FILE = pathlib.Path(_td3) / ".reconcile.state"
D.DAILY_FILE = pathlib.Path(_td3) / ".daily-report.state"
_sent: dict = {}


class _FakeLog:
    """★ 2026-10-05：通知写侧已砍 ⇒ `report_daily` 不再 `emit()`，改 `LOG.info(正文)`；
    这里捕获日志正文（原来接的是 `emit` 的 body）。判据语义不变。"""
    disabled = False

    def info(self, msg, *a, **k):
        _sent["body"] = (msg % a) if a else msg

    def warning(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass

    def exception(self, *a, **k):
        pass


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
D.S.qbit_all_torrents = lambda u, **kw: [_ours("ddd")]
D.S.qbit_tagged = lambda u, tag, **kw: [{"hash": "x"}] * 5
D.LOG = _FakeLog()
D.report_daily(argparse.Namespace(db_path=None, db="x.db", qbit_url="http://h:3060"),
               force=True)

ck("日报正文里有这一节", "未完成且停滞的单种" in _sent["body"], _sent["body"][-300:])
ck("★ fz 读数进了日报正文（原 TSV 那条路已随通知写侧砍掉）",
   "① 装不出来" in _sent["body"], _sent["body"][-300:])
ck("★ 分母也在（正文里带「分母 N」）",
   "分母 1" in _sent["body"], _sent["body"][-300:])
ck("★ 键名卫生也过了日报这一关（正文里没有裸 `pack=` 键）",
   " pack=" not in _sent["body"], _sent["body"][-300:])
# ★ 2026-10-05：日报不再经 `emit()` 发（写侧已砍），下面两条改为直接读**落盘的状态**
#   —— 判据语义不变（"这一轮不该告警 / 两条 watch 互不挤掉 / 基线进了自己的文件"），
#      只是取证点从 `emit` 的入参换成磁盘与正文。
ck("  日报那一轮**不该**告警（首读只记基线 ⇒ 正文里是「不告警」）",
   "不告警" in _sent["body"], _sent["body"][-300:])
ck("★★ 卡 999 那一节也没被挤掉（两条 watch 互不影响）",
   "qb_999" in _sent["body"], _sent["body"][-300:])
ck("  链接守护那一节也在", "lg_files" in _sent["body"], _sent["body"][-300:])
ck("★ 基线落进了**自己的**文件（不塞 .reconcile.state）",
   D.RESEED_FREEZE_FILE.exists()
   and json.loads(D.RESEED_FREEZE_FILE.read_text(encoding="utf-8"))
   .get(D.RESEED_FREEZE_KEY) is not None, repr(D.RESEED_FREEZE_FILE))

print(f"\n{'=' * 60}")
print(f"断言 {_ok + _bad} 条：{_ok} 过 / {_bad} 失败")
sys.exit(0 if _bad == 0 else 1)
