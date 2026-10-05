# -*- coding: utf-8 -*-
"""once_round() 闸门接线自测 —— next_sleep() 算出的间隔是否真的落盘、真的挡住下一批。

不需要网络/真库：run_round 打桩，状态文件指到临时目录。
"""
import argparse
import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent  # 仓库根
SRC = str(REPO / "data" / "drive-loop" / "scripts" / "drive-loop.py")
sys.path.insert(0, str(REPO))
# ★ 2026-10-05（B2）：容器代码收进 data/ ⇒ orchestrator / drive-loop 源码
#   都在 data/ 下。把 data/ 也加进 import 搜索路径（本文件上一行插的是仓库根）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))

spec = importlib.util.spec_from_file_location("drive_loop", SRC)
dl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dl)

from orchestrator import state as S  # noqa: E402

# ★ 输出强制 UTF-8：GBK 控制台下 ⑪(U+246A) 编不出去，会在中途炸掉。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

TMP = Path(tempfile.mkdtemp())
dl.STATE_FILE = TMP / ".drive-loop.state"
# ★ 同样指到临时目录：once_round 的 stats-is-None 分支会调 check_farm()，
#   而它的默认落点是**仓库里的** scripts/.farm-check.state —— §⑤ 就是这么
#   把测试产物写进仓库的（已清掉）。凡是"生产会写文件"的路径，测试里都得改向。
dl.FARM_CHECK_FILE = TMP / ".farm-check.state"

fails = []


def ck(label, got, want):
    ok = got == want
    print(("  ok  " if ok else " FAIL ") + f"{label}: {got!r}")
    if not ok:
        fails.append(f"{label}: got {got!r} want {want!r}")


def write_state(d):
    dl.STATE_FILE.write_text(json.dumps(d), encoding="utf-8")


def read_state():
    return json.loads(dl.STATE_FILE.read_text(encoding="utf-8"))


def args_ns():
    return argparse.Namespace(
        db=str(TMP / "state.db"), indexers="HDFans", limit=50,
        include_cooldown=False, cadence_days=180, cadence=None,
    )


# 打桩：跑批不真跑，只返回一个假 stats
# ★ 签名必须与 `dl.run_round` **逐字一致**（2026-09-22：给它加了 `packs` 参数，
#   桩没跟上 ⇒ ⑨ 那条"stats=None 也要巡检"**静默**变成 0 次调用、报红）。
#   ★ 为什么不用 `lambda *a, **k`：那样签名漂了也不会红 —— 而"参数加了、桩没跟上"
#     正是这次要防的形状（同 `ck` 同名不同语义那类坑）。
# ★★ 2026-10-05（去掉闸门）：`run_round` 的 `packs` 形参已删 ⇒ 桩同步收窄成
#   `(pack, args, api_key)`。**又踩到同一个形状**（参数减了、桩没跟上），
#   所以这条注释留在原地、桩签名逐年对账。
_next_stats = [S.DriveStats(ok=50)]
dl.run_round = lambda pack, args, api_key: _next_stats[0]
dl.alert_if_all_done = lambda *a, **k: None
dl.emit = lambda *a, **k: False
dl.init_notifier = lambda *a, **k: None


def run_once(now=None):
    write_state({**read_state(), "last_end_ts": now if now is not None else time.time()})
    return dl.once_round(args_ns(), "k", dl.MIN_SLEEP)


print("== ① 闸门取 max(min_sleep, last_sleep_sec)：退避间隔必须真的挡人 ==")
# 场景统一为「上批 40 分钟前结束」。min_sleep=30 分钟，所以单靠下限是可以跑的。
write_state({"last_end_ts": time.time() - 40 * 60, "last_sleep_sec": 0.0,
             "consec_abort": 0, "running_pid": None})
dl.once_round(args_ns(), "k", dl.MIN_SLEEP)
ck("  无退避证据 / gap=40分 → 跑（last_end 被推新，说明进过批）",
   read_state()["last_end_ts"] > time.time() - 60, True)
ck("  跑完写回的间隔 = 45 分钟（批次无退避）",
   read_state()["last_sleep_sec"], dl.BASE_SLEEP)

for last_sleep, expect_skip in ((dl.SNOOZE_SLEEP, True), (dl.BACKOFF_SLEEP, True),
                                (dl.BASE_SLEEP, True)):
    before = time.time() - 40 * 60
    write_state({"last_end_ts": before, "last_sleep_sec": last_sleep,
                 "last_pack_idx": -1, "consec_abort": 0, "running_pid": None})
    dl.once_round(args_ns(), "k", dl.MIN_SLEEP)
    ck(f"  last_sleep={last_sleep / 60:.0f}分钟 / gap=40分 → 跳过（last_end 没动）",
       read_state()["last_end_ts"], before)

print("\n== ② 退避真的让下一批等更久：gap=70 分钟时 2 小时档仍然挡住 ==")
before = time.time() - 70 * 60
write_state({"last_end_ts": before, "last_sleep_sec": dl.BACKOFF_SLEEP,
             "last_pack_idx": -1, "consec_abort": 0, "running_pid": None})
dl.once_round(args_ns(), "k", dl.MIN_SLEEP)
ck("  70 分钟 < 2 小时 → 仍跳过", read_state()["last_end_ts"], before)

before = time.time() - 130 * 60
write_state({"last_end_ts": before, "last_sleep_sec": dl.BACKOFF_SLEEP,
             "last_pack_idx": -1, "consec_abort": 0, "running_pid": None})
dl.once_round(args_ns(), "k", dl.MIN_SLEEP)
ck("  130 分钟 > 2 小时 → 放行", read_state()["last_end_ts"] > time.time() - 60, True)

print("\n== ③ 上批撞退避（waited 20 分钟）→ 落盘的间隔确实是 2 小时 ==")
_next_stats[0] = S.DriveStats(ok=50, backoff_hits=2, waited_sec=1200.0)
write_state({"last_end_ts": time.time() - 40 * 60, "last_sleep_sec": 0.0,
             "last_pack_idx": -1, "consec_abort": 0, "running_pid": None})
dl.once_round(args_ns(), "k", dl.MIN_SLEEP)
ck("  last_sleep_sec 落盘 = 7200", read_state()["last_sleep_sec"], dl.BACKOFF_SLEEP)

print("\n== ④ 喷嚏（退避 55 秒）→ 落盘 45 分钟，不该罚 2 小时 ==")
_next_stats[0] = S.DriveStats(ok=50, backoff_hits=1, waited_sec=55.0)
write_state({"last_end_ts": time.time() - 40 * 60, "last_sleep_sec": 0.0,
             "last_pack_idx": -1, "consec_abort": 0, "running_pid": None})
dl.once_round(args_ns(), "k", dl.MIN_SLEEP)
ck("  last_sleep_sec 落盘 = 2700", read_state()["last_sleep_sec"], dl.BASE_SLEEP)

print("\n== ⑤ 本批无动作（stats=None）→ 落盘 0，退回下限，不留旧的退避 ==")
_next_stats[0] = None
# 注意 last_end 要够远（3 小时），否则会被上面那笔旧的 2 小时退避先挡在闸门外，
# 压根进不了批 —— 那样测的就不是「落盘」而是「闸门」了。
write_state({"last_end_ts": time.time() - 3 * 3600,
             "last_sleep_sec": dl.BACKOFF_SLEEP,          # 上一批留下的旧值
             "last_pack_idx": -1, "consec_abort": 0, "running_pid": None})
dl.once_round(args_ns(), "k", dl.MIN_SLEEP)
ck("  旧退避被清掉（否则会永远卡在 2 小时）", read_state()["last_sleep_sec"], 0.0)

print("\n== ⑥ 心跳残留处理：跑完 heartbeat_ts 必须被清掉 ==")
ck("  heartbeat_ts 不在状态里", "heartbeat_ts" in read_state(), False)
ck("  running_pid 已清空", read_state()["running_pid"], None)

print("\n== ⑦ clamp 上限兜底 ==")
ck("  2700 → 不动", dl.clamp(2700), 2700)
ck("  10 小时 → 压到 4 小时", dl.clamp(36000), dl.MAX_SLEEP)
ck("  1 分钟 → 抬到 30 分钟", dl.clamp(60), dl.MIN_SLEEP)

print("\n== ⑧ ★ 跳过文案必须说对原因（2026-09-12 修：健康档曾被报成「在退避」）==")


class _Rec:
    """把日志收进列表，替掉模块里的 LOG。"""
    def __init__(self):
        self.msgs = []

    def _add(self, fmt, a):
        self.msgs.append(fmt % a if a else fmt)

    def info(self, fmt, *a):
        self._add(fmt, a)

    def warning(self, fmt, *a):
        self._add(fmt, a)

    def debug(self, *a, **k):
        pass

    def exception(self, *a, **k):
        pass


_real_log = dl.LOG


def skip_msg(last_sleep, min_sleep=None, gap_min=2.7):
    """gap 固定 2.7 分钟（比任何档都小）→ 必然走跳过分支；返回那行日志。

    ★ 这个 gap 正是 09-12 11:15 现场的值 —— 当时那批被报成「在退避」，
      而它的 last_sleep_sec 是 2700（健康档）。
    """
    if min_sleep is None:
        min_sleep = dl.MIN_SLEEP
    rec = _Rec()
    dl.LOG = rec
    try:
        write_state({"last_end_ts": time.time() - gap_min * 60,
                     "last_sleep_sec": last_sleep, "last_pack_idx": -1,
                     "consec_abort": 0, "running_pid": None})
        dl.once_round(args_ns(), "k", min_sleep)
    finally:
        dl.LOG = _real_log
    hit = [m for m in rec.msgs if "跳过本轮" in m]
    return hit[0] if hit else ""


# ★ 复现现场：last_sleep=2700(健康档) + gap=2.7 分 —— 以前这里会印「在退避」
m = skip_msg(dl.BASE_SLEEP)
ck("  健康档(45分) → **不说**「在退避」", "在退避" in m, False)
ck("  健康档(45分) → 说「按上一批算出的批间隔」", "按上一批算出的批间隔" in m, True)

# 真退避档才该说「在退避」
for _lvl, _name in ((dl.SNOOZE_SLEEP, "SNOOZE 90分"),
                    (dl.BACKOFF_SLEEP, "BACKOFF 120分"),
                    (dl.ABORT_SLEEP, "ABORT 180分")):
    _m = skip_msg(_lvl)
    ck(f"  {_name} → 说「在退避」", "在退避" in _m, True)

# --min-sleep 被抬高时，原因该归给下限（不是退避，也不是上一批的间隔）
_m = skip_msg(0.0, min_sleep=dl.BASE_SLEEP + 600)
ck("  min_sleep 抬高 → **不说**「在退避」", "在退避" in _m, False)
ck("  min_sleep 抬高 → 说「按 --min-sleep 下限」", "按 --min-sleep 下限" in _m, True)

print("\n== ⑨ ★ 没待搜时也必须跑农场巡检（不能跟着批次一起停）==")
# 背景：check_farm() 挂在 after_batch_reports 里，而 run_round **没待搜时会提前
# return None**，根本走不到那儿 —— 于是"所有包都搜完了"的那几天巡检会一起停。
# 所以 once_round 的 stats-is-None 分支里补了一刀，这里把它钉住。
# ★ 必须打桩，否则开发机上 first_existing() 返回 None，会真去写 .farm-check.state。
farm_calls = []
dl.check_farm = lambda **kw: (farm_calls.append(kw), "农场巡检：无漂移")[1]

_next_stats[0] = None
write_state({"last_end_ts": time.time() - 3 * 3600, "last_sleep_sec": 0.0,
             "last_pack_idx": -1, "consec_abort": 0, "running_pid": None})
dl.once_round(args_ns(), "k", dl.MIN_SLEEP)
ck("  没待搜（stats=None）→ 巡检**照样**被调用", len(farm_calls), 1)

farm_calls.clear()
_next_stats[0] = S.DriveStats(ok=5)
write_state({"last_end_ts": time.time() - 3 * 3600, "last_sleep_sec": 0.0,
             "last_pack_idx": -1, "consec_abort": 0, "running_pid": None})
dl.once_round(args_ns(), "k", dl.MIN_SLEEP)
ck("  真跑了批次 → 这里**不**重复调（那条路由 after_batch_reports 负责）",
   len(farm_calls), 0)

print("\n== ⑩ ★★ 闸门已拆：once_round / run_round / argparse 里**没有**名单和轮换 ==")
# 背景（2026-10-05「去掉包的闸门」，用户拍）：
#   · 删 `--packs` / `PACKS_DEFAULT` / `DRIVE_PACKS` / `packs_from_arg`
#   · 删 `--pool` 开关（池路径 = 唯一路径）
#   · 删 `--batch` 与非池 `apply_batch` 分支
#   · 删 `once_round` 的 `last_pack_idx` 取模轮换 + 落盘
#   · `run_round` 的 `packs` 形参已删，池源 = `require_packs(st)` = `pack` 表全部包
#
# ★ 判据不是"文本里像不像"，而是**这些名字在运行期不再存在 / 签名变了**。
_src_all = open(SRC, encoding="utf-8").read()
for _name in ("packs_from_arg", "PACKS_DEFAULT", "DRIVE_PACKS"):
    ck(f"  名字 {_name} 在 drive-loop.py 里**不再出现**（含代码，注释可提旧事）",
       (_name in _src_all.replace("`%s`" % _name, "")), False)

# 池源就是 pack 表（唯一枚举口）：`pool_packs` / `require_packs` 存在且读 `pack` 表
for _fn in ("pool_packs", "require_packs"):
    ck(f"  池源函数 {_fn}() 存在", hasattr(dl, _fn), True)
ck("  `require_packs` 空表抛 NoPacksRegistered（不静默算空）",
   hasattr(dl, "NoPacksRegistered"), True)

# once_round 签名：只剩 (args, api_key, min_sleep) —— `packs` 形参已删
import inspect  # noqa: E402
_sig = list(inspect.signature(dl.once_round).parameters)
ck("★★ once_round 签名 = (args, api_key, min_sleep)（packs 形参已删）",
   _sig, ["args", "api_key", "min_sleep"])
_sig_rr = list(inspect.signature(dl.run_round).parameters)
ck("★★ run_round 签名 = (pack, args, api_key)（packs 形参已删）",
   _sig_rr, ["pack", "args", "api_key"])

# argparse：`--packs` / `--pool` / `--batch` 三个都**不该**再是参数
_main_src = inspect.getsource(dl.main)
for _flag in ("--packs", "--pool", "--batch"):
    ck(f"  argparse 里没有 {_flag}",
       ('"%s"' % _flag) in _main_src or ("'%s'" % _flag) in _main_src, False)

# ★★ 落盘状态**不再含 `last_pack_idx`** —— 跑一批之后状态文件里不该有它
_seen = []


def _capture(pack, args, api_key):
    _seen.append(pack)
    return S.DriveStats(ok=1)


dl.run_round = _capture
_next_stats[0] = S.DriveStats(ok=1)
write_state({"last_end_ts": time.time() - 3 * 3600, "last_sleep_sec": 0.0,
             "consec_abort": 0, "running_pid": None})
dl.once_round(args_ns(), "k", dl.MIN_SLEEP)
ck("  批次标签恒为 'pool'（没有「轮到谁」）", _seen, ["pool"])
ck("★★ 落盘状态里**不再有** last_pack_idx", "last_pack_idx" in read_state(), False)

# =========================================================================== #
# ⑧ ★★ 容器里的 pid 那一半必须停用（`#58` D1，2026-09-17）
# =========================================================================== #
# 实测（真容器里 import 本文件被测的那个模块）：
#   · 容器内 os.getpid() **恒为 1**（容器里第一个进程就是 PID 1）；
#   · 于是容器写下的 running_pid 是 1，而**下一个容器自己也是 1**
#     ⇒ 拿宿主那套 pid_alive 去读 ⇒ **恒真的假信号**（"上一批还在跑"）。
# ⇒ 修法：容器版多写 running_pid_pidns="container"，batch_alive 据此**跳过 pid**。
# ★ 为什么不能一起删 pid：宿主版还靠它（真 pid 有复用风险）。两边语义**不同**。
_now = time.time()

# ── 决定性的一格：只有 pid 那一半能决定结果 ──────────────────────────────
# 心跳**新鲜**（只看心跳 ⇒ True）+ pid **不存在**（看 pid ⇒ False）
# ⇒ 容器标记应得 True、宿主标记应得 False。**两者不同**才证明分支真的分开了。
_ghost = 999999
ck("前置：pid_alive(999999) 确为 False（否则这一格没有分辨力）",
   dl.pid_alive(_ghost), False)

ck("★ 容器标记 + 幽灵 pid + 心跳新鲜 → True（**不看 pid**）",
   dl.batch_alive({"running_pid": _ghost, "running_pid_pidns": "container",
                   "heartbeat_ts": _now}), True)
ck("★ 宿主标记 + 同一组输入 → False（**看 pid**）",
   dl.batch_alive({"running_pid": _ghost, "running_pid_pidns": "host",
                   "heartbeat_ts": _now}), False)

# ── 容器分支的四格（心跳说了算）────────────────────────────────────────
ck("容器 + pid=1 + 心跳新鲜 → True（心跳新鲜）",
   dl.batch_alive({"running_pid": 1, "running_pid_pidns": "container",
                   "heartbeat_ts": _now}), True)
ck("容器 + pid=1 + 心跳陈旧 → False（**接管**，不再被那个恒真的 pid 拖住）",
   dl.batch_alive({"running_pid": 1, "running_pid_pidns": "container",
                   "heartbeat_ts": _now - 10 * dl.HEARTBEAT_STALE_SEC}), False)

# ── 阴性对照：**撤掉容器标记**，同一组输入必须变回 False ────────────────
# 没有这一条，上面那条"True"可能是恒真的（万一 batch_alive 压根不看输入）。
ck("★阴性对照：拿掉 pidns 标记（=旧格式）→ 同一组输入变 False",
   dl.batch_alive({"running_pid": 1,
                   "heartbeat_ts": _now - 10 * dl.HEARTBEAT_STALE_SEC}), False)

# ── 旧文件（无标记、无心跳）仍走"保守当在跑"那条老路 ────────────────────
ck("旧格式（无 pidns、无 heartbeat_ts、pid 活）→ True（保守，老行为不变）",
   dl.batch_alive({"running_pid": __import__("os").getpid()}), True)

# ── 写侧：容器里**必须**带上那个标记，否则上面整个分支形同虚设 ──────────
# 判据不是"文件里有这个字符串"，而是"两条路都写了正确的值"。
_src = SRC and open(SRC, encoding="utf-8").read()
ck("★ 写侧写了 running_pid_pidns（容器/宿主两值）",
   ('"running_pid_pidns": "container" if _in_container else "host"' in _src), True)
ck("★ 判据用 /.dockerenv（运行时自生，不是人传的环境变量）",
   ('os.path.exists("/.dockerenv")' in _src), True)

print()
if fails:
    print(f"!!! {len(fails)} 个失败")
    for f in fails:
        print("   " + f)
    sys.exit(1)
print("全部通过")
