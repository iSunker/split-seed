# -*- coding: utf-8 -*-
"""钉住 IYUU 辅种条数这条观测（SUMMARY §18.8 的唯一生产级证伪点）。

全离线：网络请求打桩，`state.py` 的真实现被 importlib 载进来，不碰 NAS、不碰真库。

★ 这个功能的价值全在"**它会一直跑下去、没人盯着**"——所以测试要钉的不是
  "数字算得对"（那太简单），而是三件更容易静默坏掉的事：

    ① **重构没改行为**：`qbit_torrents` 在**每 15 分钟的热路径**上（回灌）。
       加 `qbit_tagged` 时把它俩的取数抽成了公共函数 —— 这条就是那次重构的钉子。
    ② **`iyuu_watch` 绝不抛**：它挂在每天一次的日报里，日报挂在每 15 分钟一批的
       循环里。一次 qB 抖动不该让整份日报消失。
    ③ **数字真的进了 `metrics`**：notify 的 TSV 流水**只记 ts/kind/title/metrics，
       不记正文**。数字只写在正文里的话，回头分析时是拿不到的 —— 而这条
       "看着发了、其实没留痕"的坏法，肉眼完全看不出来。
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
import urllib.parse

sys.stdout.reconfigure(encoding="utf-8")        # 见 tests/README.md：GBK 控制台会炸

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
DRIVE_LOOP = ROOT / "data" / "drive-loop" / "scripts" / "drive-loop.py"

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


# --------------------------------------------------------------------------- #
# 打桩：把 urlopen 换成假的，并记下它被要求去取哪个 URL
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_captured: dict = {}


def _stub_urlopen(payload):
    def _f(req, timeout=None):
        _captured["url"] = req.full_url
        _captured["timeout"] = timeout
        return _Resp(payload)
    return _f


def _load(path: pathlib.Path, name: str):
    """按 tests/README.md 的规矩：用 importlib 载**真的那份**，别切源码。

    ★ **必须先登记进 `sys.modules`**，否则被测文件里只要有 `@dataclass` 就炸：
      `dataclasses` 要 `sys.modules.get(cls.__module__).__dict__` 去找注解的
      命名空间，而 `spec_from_file_location` **不会**替你登记 —— 于是报一个
      看着毫不相干的 `AttributeError: 'NoneType' object has no attribute '__dict__'`。
      （`drive-loop.py` 没有 dataclass，所以它先跑通了，一度让人以为只有
      state.py 有问题。**同一个 `_load` 对两个文件行为不同，原因却不在它们身上。**）
    """
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod                    # ★ 见上
    with contextlib.redirect_stdout(io.StringIO()), \
         contextlib.redirect_stderr(io.StringIO()):
        try:
            spec.loader.exec_module(mod)
        except SystemExit:
            pass
    return mod


print("=== ① `iyuu_verdict` 三档（纯函数） ===")
D = _load(DRIVE_LOOP, "drive_loop_mod")
ck("36 > 基线 100 -> 判「仍在增长」", "仍在增长" in D.iyuu_verdict(100 + 2))
ck("100 == 基线 -> 判「持平」", "持平" in D.iyuu_verdict(100))
ck("99 < 基线 -> 判「低于」且带 ⚠", "低于" in D.iyuu_verdict(99) and "⚠" in D.iyuu_verdict(99))
ck("基线可覆写（不是写死 100）", "7" in D.iyuu_verdict(8, baseline=7))
ck("基线常量就是 §18.8 实测的那个 100", D.IYUU_BASELINE == 100, f"实际 {D.IYUU_BASELINE}")
ck("tag 字面量没被打错", D.IYUU_TAG == "IYUU自动辅种", repr(D.IYUU_TAG))

print("\n=== ② ③ 取数：URL 拼装（含那个中文 tag） ===")
S = _load(ROOT / "data" / "orchestrator" / "state.py", "state_mod")
import urllib.request as _ur

_real = _ur.urlopen

# ② 按 tag 取 —— tag 是中文，必须被百分号编码（裸中文会让 Request 抛 UnicodeEncodeError）
_ur.urlopen = _stub_urlopen([{"hash": "a"}, {"hash": "b"}])
try:
    got = S.qbit_tagged("http://h:3060/", "IYUU自动辅种")
finally:
    _ur.urlopen = _real
ck("按 tag 取回 2 条", len(got) == 2, repr(got))
ck("URL 路径正确", _captured["url"].startswith("http://h:3060/api/v2/torrents/info?"),
   _captured["url"])
ck("★ 整个 URL 是纯 ASCII（中文被编码了，没留裸字节）", _captured["url"].isascii(),
   _captured["url"])
_q = urllib.parse.parse_qs(urllib.parse.urlparse(_captured["url"]).query)
ck("解码回来等于原 tag（没编错也没截断）", _q.get("tag") == ["IYUU自动辅种"], repr(_q))
ck("末端的 / 被规范化掉了（没有双斜杠）", "//api" not in _captured["url"])
ck("超时按调用方给的传下去", _captured["timeout"] == 30.0, repr(_captured["timeout"]))

# ③ ★ 重构的钉子：按分类取出来的 URL 必须与重构前**一个字不差**
_ur.urlopen = _stub_urlopen([])
try:
    empty = S.qbit_torrents("http://h:3060", "reseed-singles")
finally:
    _ur.urlopen = _real
ck("★ qbit_torrents 的 URL 仍是重构前那个字面量",
   _captured["url"] == "http://h:3060/api/v2/torrents/info?category=reseed-singles",
   _captured["url"])
ck("qB 回空数组 -> []（不是 None；调用方靠 len() 数数）", empty == [], repr(empty))

print("\n=== ④ 失败会抛（这条**故意**要抛，由调用方决定怎么办） ===")
def _boom(*a, **k):
    raise OSError("qB 不通")


_ur.urlopen = _boom
try:
    raised = None
    try:
        S.qbit_tagged("http://h:3060", "IYUU自动辅种")
    except Exception as e:                  # noqa: BLE001
        raised = e
finally:
    _ur.urlopen = _real
ck("qbit_tagged 把异常原样抛出（不吞）", isinstance(raised, OSError), repr(raised))

print("\n=== ⑤ `iyuu_watch`：三种情形都必须不抛 ===")
# 正常
D.S.qbit_tagged = lambda url, tag, **kw: [{"hash": "x"}] * 102
_note, _m = D.iyuu_watch(argparse.Namespace(qbit_url="http://h:3060"))
ck("条数写进正文", "**102**" in _note, _note)
ck("结论跟着条目走", "仍在增长" in _note, _note)
ck("★ metrics 里是**整数** 102（好让 TSV 能直接拿来算）", _m == {"iyuu": 102}, repr(_m))

# 读不到
def _boom_tagged(url, tag, **kw):
    raise OSError("qB 不通")


D.S.qbit_tagged = _boom_tagged
_note, _m = D.iyuu_watch(argparse.Namespace(qbit_url="http://h:3060"))
ck("★ 读不到也**不抛**（返回一句话）", "读不到" in _note, _note)
ck("★ 但 metrics 仍要留痕：n/a（≠ 那天没跑）", _m == {"iyuu": "n/a"}, repr(_m))

# 没给 url
D.S.qbit_tagged = lambda url, tag, **kw: (_ for _ in ()).throw(AssertionError("不该被调用"))
_note, _m = D.iyuu_watch(argparse.Namespace(qbit_url=""))
ck("没 url 时不去调 qB", "跳过" in _note, _note)
ck("没 url 时 metrics 是 no-url", _m == {"iyuu": "no-url"}, repr(_m))

print("\n=== ⑥ ★ 接线：report_daily 把 iyuu 塞进了 metrics，且坏掉也要发得出去 ===")
class _Trend:
    def render(self) -> str:
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


def _run_daily(stub_tagged):
    """跑一遍 report_daily，返回它写进日志的正文（原先是交给 emit 的 title/metrics/body）。

    ★★ 2026-10-05：通知写侧已砍 ⇒ `report_daily` 不再 `emit()`，改 `LOG.info(正文)`。
    判据语义不变（"iyuu 读数真的进了日报"），取证点从 `emit` 的入参换成日志正文。
    """
    sent: dict = {}

    class _FakeLog:
        disabled = False

        def info(self, msg, *a, **k):
            text = (msg % a) if a else msg
            # 日报那条是唯一带「每日台账」的；其余 info（对账等）不覆盖它
            if "每日台账" in text:
                sent["body"] = text
                sent["title"] = "每日台账"

        def warning(self, *a, **k):
            pass

        def debug(self, *a, **k):
            pass

        def exception(self, *a, **k):
            pass

    D.S.StateStore = _Store
    D.S.qbit_tagged = stub_tagged
    D.LOG = _FakeLog()
    args = argparse.Namespace(db_path=None, db="x.db", qbit_url="http://h:3060")
    with tempfile.TemporaryDirectory(prefix="iyuu-probe-") as td:
        # ★ 必须改向 —— 否则 `_daily_set` 会往**仓库**里写 .daily-report.state
        #   （tests/README.md 记过这个坑：test_once_gate.py §⑤ 栽的就是它）。
        D.DAILY_FILE = pathlib.Path(td) / ".daily-report.state"
        # ★ 同一类：`report_daily` 里还挂着 `reconcile_watch`，它末尾要写
        #   「每一格上次成功读到数」的台账 —— 也是往仓库里写，也必须改向。
        D.RECONCILE_FILE = pathlib.Path(td) / ".reconcile.state"
        D.report_daily(args, force=True)
        ck("★ 日报告诉别人它写过了（否则明天会被 _daily_last 挡住）",
           D._daily_last() != "")
    return sent


sent = _run_daily(lambda url, tag, **kw: [{"hash": "x"}] * 137)
ck("★ iyuu 读数进了 metrics（日报日志那条 `metrics=%s` 里）",
   "iyuu=137" not in sent["body"] and "'iyuu': 137" in sent["body"],
   sent["body"][:400])
ck("day 没被挤掉", "'day':" in sent["body"], sent["body"][:400])
ck("正文里也有（给人看）", "IYUU 辅种条数：**137**" in sent["body"], sent["body"][-300:])

sent2 = _run_daily(_boom_tagged)
ck("★ qB 挂了，日报照样写得出来", "每日台账" in sent2["body"], sent2["body"][:200])
ck("★ 挂掉时读数也留痕（n/a），不是缺字段", "'iyuu': 'n/a'" in sent2["body"],
   sent2["body"][:400])

print(f"\n{'=' * 60}")
print(f"断言 {_ok + _bad} 条：{_ok} 过 / {_bad} 失败")
sys.exit(0 if _bad == 0 else 1)
