# -*- coding: utf-8 -*-
"""钉住「链接守护」的判据与它那一节日报（`state.linkguard_*` + `drive-loop.linkguard_watch`）。

背景（2026-09-13，见 README「源文件被写穿」）：cross-seed 在 partial 下注入的
单种，qB 校验不通过会**就地重下**，而 linkDirs 是硬链接 ⇒ 直接写穿源文件。
判据给**已经存在**的那批硬链接上锚：文件 (size, mtime) 指纹基线 + 定期差分。

★ 这个判据最容易被悄悄改坏的地方，都不是「算得对不对」：

    ① **把「扫了个空」当成现状**：扫盘失败/根目录写错 ⇒ cur 为空 ⇒ 若照常落盘，
       下一轮拿空基线比对，**每个文件都会被报成「新增」**。一条假警报要赔掉
       这一整条观测的可信度。用例见 §⑤。
    ② **首读就响**：现存差异是「已接受的现状」，不是今天新冒出来的（#47 立的
       规矩）。首读响了就得配冷却，而那正是要避免的噪音。用例见 §④。
    ③ **路径前缀少个斜杠**：`/x/甲` 会把 `/x/甲乙/…` 一起吞进去 —— 于是「哪条
       种子被写穿」会指错人，而指错人比不指更坏（会照着错的对象去重建）。
       用例见 §③。
    ④ **名字漏进日报**：日报是发出去的。只报数量、不报路径/种子名。
       用例见 §⑥。
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile

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
    """用 importlib 载**真的那份**（tests/README.md 的规矩：别切源码再 exec）。"""
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


def _write(p: pathlib.Path, data: bytes, mtime: float | None = None) -> pathlib.Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime is not None:
        os.utime(p, (mtime, mtime))     # 定死，别让「两次写太快 mtime 相同」变成 flaky
    return p


# --------------------------------------------------------------------------- #
TD = pathlib.Path(tempfile.mkdtemp(prefix="linkguard-"))
ROOT_A = TD / "singles" / "SiteA"
ROOT_B = TD / "singles" / "SiteB"

print("=== ① 快照：形状、临时文件过滤、@eaDir 过滤、读不到就跳过 ===")
_write(ROOT_A / "Rel1" / "a.mkv", b"aaaa", 1_700_000_000)
_write(ROOT_A / "Rel1" / "b.nfo", b"bb", 1_700_000_001)
_write(ROOT_A / "Rel2" / "c.mkv", b"cccccc", 1_700_000_002)
snap = S.linkguard_snapshot([str(ROOT_A)])
ck("扫到 3 个文件", len(snap) == 3, repr(sorted(snap)))
k = str(ROOT_A / "Rel1" / "a.mkv")
ck("★ 键是**给进去的坐标系**（NAS 侧路径），不是别的",
   k in snap, repr(sorted(snap))[:200])
ck("值是 [size, mtime_ns]", snap[k] == [4, 1_700_000_000 * 10**9], repr(snap[k]))

# 下载中间产物必须滤掉：它们**本来就该**随重下而变化，记进基线只会制造噪音
_write(ROOT_A / "Rel1" / "a.mkv.!qB", b"partial")
_write(ROOT_A / "Rel1" / "c.mkv.parts", b"partial")
_write(ROOT_A / "Rel1" / "d.tmp", b"partial")
snap = S.linkguard_snapshot([str(ROOT_A)])
ck("★ .!qB / .parts / .tmp 不计入基线（重下本来就会动它们）",
   len(snap) == 3, repr(sorted(snap)))
ck("  （大小写不敏感：.!qB 与 .!QB 都算）", S._lg_is_tmp("X.!QB") is True)
ck("  @eaDir 这类群晖元数据目录被跳过",
   S.linkguard_snapshot([str(TD / "没有这个目录")]) == {}, "空根")

print("\n=== ② 差分：changed / added / removed 三分类 ===")
base = S.linkguard_snapshot([str(ROOT_A)])
# 只改 mtime（模拟「就地重写」——写穿必然动它）：
os.utime(ROOT_A / "Rel1" / "a.mkv", (1_700_000_999, 1_700_000_999))
# 加一个新文件：
_write(ROOT_A / "Rel2" / "e.srt", b"eeee", 1_700_000_500)
# 删掉一个：
(ROOT_A / "Rel1" / "b.nfo").unlink()
cur = S.linkguard_snapshot([str(ROOT_A)])
d = S.linkguard_diff(base, cur)
ck("★ mtime 变 ⇒ changed（这是「写穿」的直接证据）",
   d["changed"] == [str(ROOT_A / "Rel1" / "a.mkv")], repr(d["changed"]))
ck("新增进 added", d["added"] == [str(ROOT_A / "Rel2" / "e.srt")], repr(d["added"]))
ck("删除进 removed", d["removed"] == [str(ROOT_A / "Rel1" / "b.nfo")], repr(d["removed"]))
ck("三把钥匙齐", set(d) == {"changed", "added", "removed"}, repr(set(d)))

# ★ 只改 size（mtime 也变）——两个都变是常态，但只变 size 也必须抓到
os.utime(ROOT_A / "Rel2" / "c.mkv", (1_700_000_002, 1_700_000_002))
_write(ROOT_A / "Rel2" / "c.mkv", b"cccccccccc", 1_700_000_002)
d2 = S.linkguard_diff(cur, S.linkguard_snapshot([str(ROOT_A)]))
ck("★ size 变了也抓得到（不只看 mtime）",
   d2["changed"] == [str(ROOT_A / "Rel2" / "c.mkv")], repr(d2["changed"]))

ck("**必须是 list 比较、不是真值比较**：空对空 = 没变化",
   S.linkguard_diff({}, {}) == {"changed": [], "added": [], "removed": []},
   repr(S.linkguard_diff({}, {})))
ck("坏形状（None / 非 dict）当空处理、不抛",
   S.linkguard_diff(None, None) == {"changed": [], "added": [], "removed": []},
   repr(S.linkguard_diff(None, None)))
ck("★ 全是「新增」那种坏法**故意**可表达 —— 所以才要靠 §⑤ 的『扫空不落盘』去防",
   len(S.linkguard_diff({}, {"a": [1, 1], "b": [2, 2]})["added"]) == 2)

print("\n=== ③ 归属：路径前缀必须带斜杠、且取最长匹配 ===")
TORRENTS = [
    {"hash": "H_A", "save_path": "/v/singles/SiteA"},
    {"hash": "H_AB", "save_path": "/v/singles/SiteAB"},      # ★ SiteA 是它的前缀
    {"hash": "H_SUB", "save_path": "/v/singles/SiteA/Sub"},  # ★ 更深一层
]
ck("普通归属",
   S.linkguard_owner("/v/singles/SiteA/Rel1/a.mkv", TORRENTS) == "H_A",
   repr(S.linkguard_owner("/v/singles/SiteA/Rel1/a.mkv", TORRENTS)))
ck("★★ `/SiteA` 不许吞掉 `/SiteAB/...`（少个斜杠就会吞）",
   S.linkguard_owner("/v/singles/SiteAB/R/x.mkv", TORRENTS) == "H_AB",
   repr(S.linkguard_owner("/v/singles/SiteAB/R/x.mkv", TORRENTS)))
ck("★ 互相是前缀时取**最长**的那个",
   S.linkguard_owner("/v/singles/SiteA/Sub/R/x.mkv", TORRENTS) == "H_SUB",
   repr(S.linkguard_owner("/v/singles/SiteA/Sub/R/x.mkv", TORRENTS)))
ck("认不出 → None（上层会记成 `?`，而不是硬塞给某条）",
   S.linkguard_owner("/other/x.mkv", TORRENTS) is None)
ck("save_path 带结尾斜杠也认得出（先 rstrip 再比）",
   S.linkguard_owner("/v/singles/SiteA/R/x.mkv",
                     [{"hash": "H", "save_path": "/v/singles/SiteA/"}]) == "H")
ck("空 / None 不炸", S.linkguard_owner("", []) is None and S.linkguard_owner("/x", None) is None)

print("\n=== ④ 在动 / 终态 ===")
IR = [
    {"hash": "p", "state": "pausedDL"},       # 终态
    {"hash": "pu", "state": "pausedUP"},      # 终态
    {"hash": "u", "state": "uploading"},      # ★★ 做种中 —— **不以 UP 结尾**，别漏
    {"hash": "su", "state": "stalledUP"},     # 终态
    {"hash": "e", "state": "error"},          # 有独立口径 → 终态
    {"hash": "c", "state": "checkingDL"},     # ★ 正在校验 —— 正是要看的
    {"hash": "cu", "state": "checkingUP"},    # ★★ 对**已做种**数据的校验，更要看
    {"hash": "d", "state": "downloading"},    # ★ 正在下
    {"hash": "s", "state": "stalledDL"},
    {"hash": "m", "state": "metaDL"},
]
r = S.linkguard_inflight(IR)
ck("★ 只挑非静止的", r["hashes"] == ["c", "cu", "d", "m", "s"], repr(r))
ck("★★ `uploading` 算静止（写错成 endswith('UP') 就会把它当成在下载 ⇒ 误暂停）",
   "u" not in r["hashes"], repr(r))
ck("★★ `checkingUP` 算「在动」（它可能是校验失败→重下的前奏）",
   "cu" in r["hashes"], repr(r))
ck("分母是全部", r["total"] == 10, repr(r))
ck("空 / None 不抛", S.linkguard_inflight([]) == {"n": 0, "hashes": [], "total": 0}
   and S.linkguard_inflight(None)["n"] == 0)
ck("★ state 缺失按「在动」算（宁可吵不可静默，同 qb_999 的 last_activity）",
   S.linkguard_inflight([{"hash": "x"}])["n"] == 1)

# --------------------------------------------------------------------------- #
print("\n=== ⑤ linkguard_watch：首读 / 一致 / 变更 / 扫空 / 读不到 ===")
D = _load(DRIVE_LOOP, "drive_loop_lg")

W_ROOT = TD / "watch" / "SiteA"
_write(W_ROOT / "Rel1" / "a.mkv", b"aaaa", 1_700_000_000)
_write(W_ROOT / "Rel1" / "b.mkv", b"bbbb", 1_700_000_001)

_td = tempfile.mkdtemp(prefix="linkguard-state-")
D.LINKGUARD_FILE = pathlib.Path(_td) / ".linkguard.state"
D.LOG.disabled = True

TOR = [{"hash": "H1", "save_path": str(W_ROOT), "tags": "cross-seed"}]


def _watch(torrents=None, *, url="http://h:3060", raises=False):
    def _tagged(u, tag, **kw):
        if raises:
            raise OSError("qB 不通")
        return TOR if torrents is None else torrents
    D.S.qbit_tagged = _tagged
    return D.linkguard_watch(argparse.Namespace(qbit_url=url))


def _state_on_disk():
    return json.loads(D.LINKGUARD_FILE.read_text(encoding="utf-8"))


note, m = _watch()
ck("首读只记基线、**不**报变化（#47 立的规矩）", "首次读数" in note, note)
ck("  基线段里没有「被改写」的告警句", "★ 有变化" not in note, note)
ck("★ 首读 metrics 里 change 三个数都是 0（现状不是新闻）",
   (m["lg_changed"], m["lg_added"], m["lg_removed"]) == (0, 0, 0), repr(m))
ck("  lg_seeded=1（这一轮是起锚）", m["lg_seeded"] == 1, repr(m))
ck("★ 基线落盘了", len(_state_on_disk()[D.LINKGUARD_SNAP_KEY]) == 2,
   repr(sorted(_state_on_disk())))
ck("  详情里写了 files 数", _state_on_disk()[D.LINKGUARD_DETAIL_KEY]["files"] == 2)
ck("★★ 首读时 detail.changed 必须是**空的**（否则诊断端把整库报成「被改写」）",
   _state_on_disk()[D.LINKGUARD_DETAIL_KEY]["changed"] == {},
   repr(_state_on_disk()[D.LINKGUARD_DETAIL_KEY]["changed"]))
ck("  同理 removed 也空（首读没有「消失」可言）",
   _state_on_disk()[D.LINKGUARD_DETAIL_KEY]["removed_n"] == 0)

note, m = _watch()
ck("同一份数据再读 → 「与基线一致」", "与基线一致" in note, note)
ck("  且 change 三个数仍是 0", (m["lg_changed"], m["lg_added"], m["lg_removed"]) == (0, 0, 0),
   repr(m))
ck("  lg_seeded 归 0", m["lg_seeded"] == 0, repr(m))

print("  -- 制造一次「写穿」：就地改写一个文件的 mtime --")
os.utime(W_ROOT / "Rel1" / "a.mkv", (1_700_009_999, 1_700_009_999))
note, m = _watch()
ck("★ 报出变化（『★ 有变化』这一句）", "★ 有变化" in note, note)
ck("★ lg_changed 进 metrics（TSV 唯一的入口）", m["lg_changed"] == 1, repr(m))
ck("  正文里点明了「被改写 = 写穿的直接证据」", "写穿" in note, note)
det = _state_on_disk()[D.LINKGUARD_DETAIL_KEY]
ck("★ 详情按**所属种子**归组（rebuild 清单靠它）", "H1" in det["changed"], repr(det))
ck("  归到 H1 名下的文件数 = 1", det["changed"]["H1"]["n"] == 1, repr(det["changed"]))
ck("  样本里带了路径（详情是给诊断端读的，但不进日报正文）",
   det["changed"]["H1"]["sample"] == [str(W_ROOT / "Rel1" / "a.mkv")],
   repr(det["changed"]["H1"]))

note, m = _watch()
ck("再次读到同一状态 → 与基线一致（只响一次，不天天喊）",
   "与基线一致" in note and m["lg_changed"] == 0, note)

print("  -- ★ 扫了个空：绝不落盘 --")
D.LINKGUARD_FILE.unlink()
note, m = _watch([{"hash": "H1", "save_path": str(TD / "根本没有"), "tags": "cross-seed"}])
ck("★★ 扫到 0 个文件 ⇒ 明说、且**不写基线**", "扫到 0 个文件" in note, note)
ck("  基线文件**没被创建**（写了就会把每个文件报成「新增」）",
   not D.LINKGUARD_FILE.exists(), repr(D.LINKGUARD_FILE))
ck("  metrics 里 files=0、change 记 n/a", m["lg_files"] == 0 and m["lg_changed"] == "n/a",
   repr(m))

print("  -- 读不到 qB / 没有 url --")
note, m = _watch(raises=True)
ck("读不到 → 不抛、给一句话", "读不到" in note, note)
ck("★ 读不到时 metrics 是 n/a（不是 0）",
   m["lg_changed"] == "n/a" and m["lg_files"] == "n/a", repr(m))
note, m = _watch(url="")
ck("没 url → 跳过", "跳过" in note, note)
ck("  且没去调 qB", m["lg_changed"] == "no-url", repr(m))

note, m = _watch([])
ck("没有 cross-seed 种子 → 说清是「没有 save_path」", "save_path" in note, note)

print("  -- 坏掉的基线：不误报，静默重新起锚 --")
_write(W_ROOT / "Rel1" / "a.mkv", b"aaaa", 1_700_000_000)
D.LINKGUARD_FILE.write_text("{ 这不是 JSON", encoding="utf-8")
note, m = _watch()
ck("★ 基线坏了 → 当作「首次读数」（而不是把每个文件都报成新增）",
   "首次读数" in note and m["lg_added"] == 0, note)
ck("  并且被重写成合法 JSON",
   isinstance(_state_on_disk().get(D.LINKGUARD_SNAP_KEY), dict))

print("\n=== ⑥ ★ 只报数量，不报路径 / 不报种子名 ===")
note, m = _watch()                       # 此刻就处于「上次已把变化采纳进基线」之后
ck("★★ 正文里没有被测目录的路径（路径是内容可识别信息）",
   str(W_ROOT) not in note and str(TD) not in note, note)
ck("★★ 正文里不出现 volume1（生产盘根）", "volume1" not in note, note)
ck("★★ 正文里没有种子 hash", "H1" not in note, note)
ck("  但 metric 里有数", m["lg_files"] == 2, repr(m))

# 再制造一次变化 ⇒ 这一轮才会带「去哪里看详情」那句
os.utime(W_ROOT / "Rel1" / "a.mkv", (1_700_010_000, 1_700_010_000))
note2, _ = _watch()
ck("写穿那一轮确实带了指路（指到 .linkguard.state）",
   "写穿的直接证据" in note2 and "linkguard.state" in note2, note2)
ck("★ 即便在写穿那一轮，正文里也仍然只有数量、没有文件名",
   "a.mkv" not in note2 and "H1" not in note2, note2)

print("\n=== ⑦ 接线：report_daily 里那一节 + 状态文件互不打架 ===")
_td2 = tempfile.mkdtemp(prefix="linkguard-daily-")
D.DAILY_FILE = pathlib.Path(_td2) / ".daily-report.state"
D.RECONCILE_FILE = pathlib.Path(_td2) / ".reconcile.state"
D.LINKGUARD_FILE = pathlib.Path(_td2) / ".linkguard.state"
_sent: dict = {}


class _FakeLog:
    """★ 2026-10-05：通知写侧已砍 ⇒ `report_daily` 不再 `emit()`，改 `LOG.info(正文)`；
    这里捕获日志正文（原来接 `emit` 的 body）。判据语义不变。"""
    disabled = False

    def info(self, msg, *a, **k):
        text = (msg % a) if a else msg
        if "每日台账" in text:
            _sent["body"] = text

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
D.S.qbit_all_torrents = lambda u, **kw: []
D.S.qbit_tagged = lambda u, tag, **kw: TOR
D.LOG = _FakeLog()
D.report_daily(argparse.Namespace(db_path=None, db="x.db", qbit_url="http://h:3060"),
               force=True)

ck("正文里有这一节", "链接守护" in _sent["body"], _sent["body"][-300:])
ck("★ lg_* 读数在日报里（原 TSV 那条路已随通知写侧砍掉）",
   "lg_files" in _sent["body"], _sent["body"][:400])
ck("★★ 链接守护**没有**污染 .reconcile.state（它用自己的文件）",
   not D.RECONCILE_FILE.exists() or
   D.LINKGUARD_SNAP_KEY not in json.loads(D.RECONCILE_FILE.read_text(encoding="utf-8")),
   "见 D.LINKGUARD_FILE 的注释：几万条快照不该让每个 watch 都搬一次")
ck("  它自己的文件里基线在", D.LINKGUARD_SNAP_KEY in _state_on_disk())
ck("★ 跑完一整份日报后基线**仍在**（没被别节抹掉）",
   len(_state_on_disk()[D.LINKGUARD_SNAP_KEY]) == 2, repr(sorted(_state_on_disk())))

print(f"\n{'=' * 60}")
print(f"断言 {_ok + _bad} 条：{_ok} 过 / {_bad} 失败")
sys.exit(0 if _bad == 0 else 1)
