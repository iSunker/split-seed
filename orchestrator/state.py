"""单片状态机 —— sidecar SQLite + 从 cross-seed / qB **反向同步**。

为什么不直接给 cross-seed 的库加字段
====================================
1. cross-seed 的 schema 由它自己的 knex migration 管理。手工 `ALTER TABLE` 会在它
   升级/启动时被覆盖，甚至让它直接启动失败 —— 那是它拥有的一等公民表。
2. **它其实已经有我们要的那几个"字段"了**（2026-09-11 实测 v6.13.7）：

   ================================  ==================================================
   `timestamp(searchee_id, indexer_id, last_searched)`
                                     「这部片子在某个站**真的搜过**了吗」——天然的"已完成阶段"标记
   `decision(searchee_id, guid, decision, info_hash, ...)`
                                     匹配结论：MATCH / MATCH_PARTIAL / MATCH_SIZE_ONLY
   `indexer(id, url, name, active, status, retry_after)`
                                     索引器退避状态 + **解禁的绝对时间**
   `searchee(id, name, first_searched, last_searched)`
                                     v6.13 实测这两列**全 NULL**（已被 timestamp 取代）
   ================================  ==================================================

   实测数字：`searchee` 405 行，其中只有 **118** 行有 `timestamp`（= 真搜过），
   另外 **287** 行既无 `timestamp` 也无 `decision` —— 那 295 条被 429 秒跳的，
   在 cross-seed 眼里就是"从来没搜过"。而 118 里有 58 个有 match，**58/118 = 49.2%**。

3. **它唯独不记的，是"这条是被退避跳过的"。** 而这恰恰是我们最需要知道的：
   被跳过的条目不主动重试，就永远躺在那里（cross-seed 不会排队、不会重试）。

所以本模块只做两件事
====================
- **只读** cross-seed 的库 + 日志 + qB，把事实**翻译**成"每部片子现在到哪一步"；
- 自己补上 cross-seed 记不了的那一格：`SKIPPED`、重试策略、尝试流水。

`stage` **不是**手工维护的字段，而是由事实推导的纯函数（见 `compute_stage`）——
所以不存在"状态与现实不一致"的可能，也就不需要"同步一下状态"这种动作。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# --------------------------------------------------------------------------- #
# 阶段
# --------------------------------------------------------------------------- #
STAGE_PENDING = "PENDING"        # 还没搜过
STAGE_SEARCHED = "SEARCHED"      # 搜过了，但没匹配到（= UNMATCHED，保留别名便于阅读）
STAGE_UNMATCHED = "UNMATCHED"    # 同上，语义化名字
STAGE_MATCHED = "MATCHED"        # 匹配到单种并注入 qB，尚未确认做种
STAGE_SEEDING = "SEEDING"        # qB 里在跑，校验中或已做种
STAGE_SKIPPED = "SKIPPED"        # ★被索引器退避跳过 —— 必须重试，且**不算搜过**
STAGE_ERROR = "ERROR"            # 异常（日志/接口/路径）

ALL_STAGES = (
    STAGE_PENDING, STAGE_SKIPPED, STAGE_UNMATCHED,
    STAGE_MATCHED, STAGE_SEEDING, STAGE_ERROR,
)

#: 成功终点：到了这里就永不重搜
DONE_STAGES = frozenset({STAGE_SEEDING, STAGE_MATCHED})

#: 立刻可重搜（不消耗重试预算）
RETRY_NOW_STAGES = frozenset({STAGE_PENDING, STAGE_SKIPPED, STAGE_ERROR})

#: 需要等冷却期 / 等新增索引器才重搜
COOLDOWN_STAGES = frozenset({STAGE_UNMATCHED})

#: todo() 的**取件顺序**：数字越小越先搜。分批（--limit/--batch）时按这个排，
#: 保证先花在"最该搜"的片子上。
#:   SKIPPED   0 —— 上次根本没发出去（被退避秒跳），重搜不消耗任何重试预算
#:   ERROR     1 —— 上次异常，尽快补
#:   PENDING   2 —— 从没搜过
#:   UNMATCHED 3 —— 真搜过没命中，等周期/等新站
TODO_PRIORITY: dict[str, int] = {
    STAGE_SKIPPED: 0,
    STAGE_ERROR: 1,
    STAGE_PENDING: 2,
    STAGE_UNMATCHED: 3,
}

#: 未命中后，**每个站**默认多久重搜一次。
#:
#: ★ 2026-09-11 由 7 天改为 14 天（账号安全 > 命中延迟）。
#:   无人值守下这个周期决定了**长期查询量**：DC+FRDS+MBF ≈ 1000 部单片，
#:   7 天 = 每天约 150 次查询/站，一年 5 万+ 次，且是**永不停止**的机器人流量。
#:   delay=30 只解决"快不快"，解决不了"像不像人"——长期稳定的自动化抓取本身
#:   就可能触发站点风控。翻倍到 14 天，查询量直接减半，代价只是未命中的片子
#:   多等一周（反正命中的前提是"站上出现了这个单种"，那件事本来就不由我们控制）。
#:   单个站想更保守：`--cadence "NanyangPT=30"`；想更激进：`--cadence-days 7`。
DEFAULT_CADENCE_DAYS = 14
#: 兼容旧名字
DEFAULT_COOLDOWN_DAYS = DEFAULT_CADENCE_DAYS

STAGE_ORDER = list(ALL_STAGES)


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def cadence_for(indexer: str, *, cadence_days: int = DEFAULT_CADENCE_DAYS,
                cadence_by_indexer: dict[str, int] | None = None) -> int:
    """某个站的重搜周期（天）。可以给单个站单独设。"""
    if cadence_by_indexer and indexer in cadence_by_indexer:
        return int(cadence_by_indexer[indexer])
    return int(cadence_days)


#: ★★ 首次探索预算（2026-09-20，`e404b1ca#6` ③）。
#:
#: **为什么要有它**（实测，不是推演）：`due_indexers` 原先「从没在某个站搜过 → 无条件该搜」，
#: 于是**每一批**都把这 272 部 `UNMATCHED` 判成"到期"。实测分布（`state.db`，2026-09-20）：
#:
#:     缺 ['BTSCHOOL','HDtime','NanyangPT']            n=234
#:     缺 ['BTSCHOOL','HDFans','HDtime','NanyangPT']   n=38
#:
#: ⇒ 它们**每批都占满 `--limit 50` 的名额**。用户看到的「limit 50 就一直把上次没匹配进的
#:   加到这次」的机制真身就是它 —— 不是"优先级排错"，是**这几个站对它们永远判该搜**。
#:
#: ★ 规则：一部片子里"从没搜过的站"**最多算 `first_explore_budget` 个**（默认 1）。
#:   于是 272 部不会同一批全部到期，而是**一批消化一批**：每部每轮推进 1 个新站，
#:   3~4 轮扫完。★ 这是**限流**，不是**放弃** —— 没被选中的站**下一个周期仍然会到期**。
#:
#: ★ 为什么默认是 1 而不是 0：0 会让"从没搜过的站"**永远排不上**（那才是真丢事）。
#:   1 = 每部片每轮至少推进一个没搜过的站，欠账一定在收敛。
#:  ★ 为什么不是"不限"（=旧行为）：那样它就**退化回吃光名额**那个缺陷。
FIRST_EXPLORE_BUDGET = 1


def _lookup_seen(indexer_seen: dict[str, str], ix: str) -> str | None:
    """按**站名主干**在 `indexer_seen` 里找时间戳。找不到返回 `None`。

    ★★★ 为什么必须是主干、不能裸比（2026-09-22 实测的第二处缺陷）
    ------------------------------------------------------------
      `indexer_seen` 的键来自 **cross-seed 的原值**，带 caps 后缀：
          `'NanyangPT (南洋)'`
      而 `--indexers` 是**人手写的短名**：
          `'NanyangPT'`

      裸比 `indexer_seen.get(ix)` ⇒ `None` ⇒ 判「从没搜过」⇒ **永远到期**。
      实测（`state.db`）：43 部待搜里 **37 部**的到期站是那个永远匹配不上的幽灵名，
      而站名表里那个短名「库里从没出现过」—— 就是它。

    ★★ 这个缺陷的形状（比它本身更值得记）：
      同一仓里**已经有一把正确的尺** —— `norm_indexer_name()`。
      它被用在 `check_indexers()`（名单对账）与 `DriveSession`（退避判定），
      **那两处工作正常**（自检打「索引器自检通过」）⇒ 日志上一切正常，
      唯独**这里**漏用 ⇒ 数慢慢偏成「每轮都到期」，且不报错。
      ⇒ 教训：**同一概念有两把尺时，要数清有几处用**（本处与 `next_due_at` 是漏的两处）。

    ★ 为什么不直接 `indexer_seen[norm] = ...` 改写调用方的字典：
      调用方（`todo_detail` / `movies` 的行）可能共用同一个 dict，
      就地改会**污染**其它读者。这里是纯查询，不改输入。
    """
    if ix in indexer_seen:              # 快路径：名字本来就对得上
        return indexer_seen[ix]
    want = norm_indexer_name(ix)
    for k, v in indexer_seen.items():
        if norm_indexer_name(k) == want:
            return v
    return None


def due_indexers(
    indexer_seen: dict[str, str],
    indexers_now: list[str],
    *,
    cadence_days: int = DEFAULT_CADENCE_DAYS,
    cadence_by_indexer: dict[str, int] | None = None,
    now: datetime | None = None,
    first_explore_budget: int = FIRST_EXPLORE_BUDGET,
) -> list[str]:
    """**这一部片子，现在该在哪些站上重搜？**

    规则（这就是「每站按周期重搜」的落地）：
      * 上次搜到现在 ≥ 该站的周期   → 该搜（**优先，不受预算限制**）
      * 从没在这个站搜过            → 该搜，但**每轮最多算 `first_explore_budget` 个**
      * 否则                        → 等

    `indexers_now` 为空时，退化成"用它自己记录过的站"（避免没传 --indexers 就什么都不搜）。

    ★★ `first_explore_budget`（见 `FIRST_EXPLORE_BUDGET` 那段注释）：**"从没搜过"的站
    必须限流**，否则 272 部 `UNMATCHED` 每批都到期、吃光 `--limit`。
    ★ **够周期的站不受预算限制** —— 预算是用来**防无限欠账**的，不是用来压下正常轮换的；
      把两者混在一起会让"早就该重搜"的站被"没搜过的站"挤掉（那正是我们要修的毛病）。
    ★ 排序：先到期的（`last + cadence` 最早的），再没搜过的 —— 这样预算花在**最该补**的站上。
    """
    now = now or datetime.now()
    pool = list(indexers_now) or sorted(indexer_seen) or [UNKNOWN_INDEXER]
    fresh: list[str] = []          # 够周期的（不受预算限制）
    never: list[str] = []          # 从没搜过的（受预算限制）
    for ix in pool:
        # ★★★ 必须走 `_lookup_seen`（按主干），**不能**裸比 `indexer_seen.get(ix)`。
        #   裸比会让 `'NanyangPT'` 永远查不到库键 `'NanyangPT (南洋)'`
        #   ⇒ 判「从没搜过」⇒ 永远到期。见 `_lookup_seen` 的 docstring。
        last = _parse_ts(_lookup_seen(indexer_seen, ix))
        if last is None:
            never.append(ix)
            continue
        days = cadence_for(ix, cadence_days=cadence_days,
                           cadence_by_indexer=cadence_by_indexer)
        if (now - last) >= timedelta(days=days):
            fresh.append(ix)
    # ★ 站名排序保证**确定性**：同一输入两次调用必须给同一个答案（否则测试会飘、
    #   而"这一批到底搜了谁"就复现不出来）。`pool` 的顺序来自 `--indexers`，是稳定的，
    #   但这里仍显式排序，免得以后有人把 `pool` 改成 set。
    if first_explore_budget is None or first_explore_budget < 0:
        picked_never = sorted(never)
    else:
        picked_never = sorted(never)[:int(first_explore_budget)]
    return fresh + picked_never


def next_due_at(
    indexer_seen: dict[str, str],
    indexers_now: list[str],
    *,
    cadence_days: int = DEFAULT_CADENCE_DAYS,
    cadence_by_indexer: dict[str, int] | None = None,
) -> datetime | None:
    """下一次"最早会变得可搜"的时刻（用来填 next_retry_at / 汇报）。

    已经该搜的返回 None（= 现在就能搜）。
    """
    now = datetime.now()
    pool = list(indexers_now) or sorted(indexer_seen) or [UNKNOWN_INDEXER]
    times = []
    for ix in pool:
        # ★★ 与 `due_indexers` **同一把尺**（见 `_lookup_seen`）。
        #   原先这里是裸比 ⇒ 这个函数同样是"幽灵站名"受害者：
        #   `next_due_at` 会把"从没搜过"判成"现在就该搜"（返回 None）。
        last = _parse_ts(_lookup_seen(indexer_seen, ix))
        if last is None:
            return None                      # 有站从没搜过 → 现在就该搜
        days = cadence_for(ix, cadence_days=cadence_days,
                           cadence_by_indexer=cadence_by_indexer)
        times.append(last + timedelta(days=days))
    if not times:
        return None
    soonest = min(times)
    return soonest if soonest > now else None


def compute_stage(
    *,
    seeding_count: int,
    matched_count: int,
    searched_indexers: set[str],
    skipped_indexers: set[str],
) -> str:
    """**纯函数**：由事实推导阶段。优先级从高到低。

    这就是整个状态机的全部逻辑。没有"手工置位"，因此不会漂移。
    """
    if seeding_count > 0:
        return STAGE_SEEDING
    if matched_count > 0:
        return STAGE_MATCHED
    if searched_indexers:
        return STAGE_UNMATCHED
    if skipped_indexers:
        return STAGE_SKIPPED
    return STAGE_PENDING


# --------------------------------------------------------------------------- #
# 数据类
# --------------------------------------------------------------------------- #
@dataclass
class Movie:
    id: int
    pack: str
    dir_name: str
    path: str
    stage: str = STAGE_PENDING
    attempts: int = 0
    last_attempt_at: str | None = None
    next_retry_at: str | None = None
    searched_indexers: list[str] = field(default_factory=list)
    #: {索引器: 最后一次搜索时间} —— 每站独立计周期靠它
    indexer_seen: dict[str, str] = field(default_factory=dict)
    skipped_indexers: list[str] = field(default_factory=list)
    matched_indexers: list[str] = field(default_factory=list)
    matched_hashes: list[str] = field(default_factory=list)
    seeding_count: int = 0
    last_error: str | None = None
    note: str | None = None
    updated_at: str | None = None


#: 日志里没记下索引器名时用的占位符（info 级日志只有 "(filtered by ...)"，没写是谁）
UNKNOWN_INDEXER = "(未记录)"


@dataclass
class CrossSeedSnapshot:
    """从 cross-seed.db 读出来的事实（只读快照）。"""
    #: searchee.name -> {索引器标签: 最后一次搜索时间 "YYYY-MM-DD HH:MM:SS"}
    #: ★这就是「每站按周期重搜」里那个"上次搜是什么时候"
    searched: dict[str, dict[str, str]] = field(default_factory=dict)
    #: searchee.name -> [(info_hash, decision)]
    decisions: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    #: searchee.name -> 完整路径（来自 data 表，title 与 name 对齐）。
    #: ★有了路径才能按 dataDir 前缀把「当前包」和「别的包」的 searchee 分开，
    #:   否则 sync 会对跨包名字误报 unresolved（FRDS 的片子被算进 DC 的对不上数）。
    searchee_paths: dict[str, str] = field(default_factory=dict)
    #: 当前生效（active=1）的索引器名
    indexers: list[str] = field(default_factory=list)
    #: 索引器名 -> (status, retry_after 可读时间)
    indexer_status: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    #: 索引器 url -> 名字（用来把日志里的 url 归一成名字）
    alias: dict[str, str] = field(default_factory=dict)
    #: 有 timestamp 的 searchee 总数 / searchee 总数（用于汇报）
    searched_total: int = 0
    searchee_total: int = 0


# --------------------------------------------------------------------------- #
# 日志解析
# --------------------------------------------------------------------------- #
_LINE = re.compile(r"^(?P<date>\d{4}-\d\d-\d\d) (?P<time>\d\d:\d\d:\d\d)\.\d+ \w+: (?P<msg>.*)$")
_RE_SEARCH = re.compile(r"\[webhook\] \(\d+/\d+\) Searching for (\S+)")
_RE_SKIP = re.compile(
    r"\[webhook\] Skipping searching for (\S+) on temporarily disabled indexers \[(.*?)\]"
)
_RE_SKIP_INFO = re.compile(
    r"\[webhook\] \(\d+/\d+\) Skipped searching on indexers for (\S+) \(filtered by temporarily disabled indexers\)"
)
#: ★ 站名必须用 `(.+?)` 而不是 `(\S+)`：Prowlarr 里的站点显示名**可以带空格和括号**
#:   —— 本项目的 `NanyangPT (南洋)` 就是。用 `(\S+)` 时它只吃到 `NanyangPT`，
#:   紧接着的 ` by ` 对不上 ` (南洋) by `，**整行静默不匹配**。
#:   实测（2026-09-12）：09-11 的 91 条、09-12 的 278 条南洋 Found 行 100% 被丢弃，
#:   HDFans 一条不漏 —— 于是台账里 `matched_indexers` 永远只有 HDFans，
#:   看起来就像"只有红豆饭拆包成功"。后面 ` by (\w+) from dataDir \(` 是硬锚点，
#:   非贪婪的 `(.+?)` 不会越界吃到下一个 ` by `。
_RE_FOUND = re.compile(
    r"\[(?:webhook|inject)\] Found (.+?) \[([0-9a-f]{8})\.\.\.\] on (.+?) by (\w+) from dataDir \((.+?)\) - (.*)$"
)
_RE_SNOOZE = re.compile(r"\[torznab\] Snoozing indexers \[(.*?)\] with (\w+) until ([\d\-: ]+)")
_RE_RECEIVED = re.compile(r"\[webhook\] Received search request")


@dataclass
class LogFacts:
    searched: dict[str, set[str]] = field(default_factory=dict)       # dir_name -> indexers
    skipped: dict[str, set[str]] = field(default_factory=dict)        # dir_name -> indexers
    found: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # dir_name -> [(hash8, indexer)]
    snoozes: list[tuple[str, str, str]] = field(default_factory=list)      # (at, indexers, until)
    #: dir_name -> 最后一次 "Searching for" 的**日期时间**（只当 db 里查不到时兜底）
    searched_at: dict[str, str] = field(default_factory=dict)
    searches_seen: int = 0
    skips_seen: int = 0
    #: 日志里出现过的 (N/M) 进度，用于识别"被秒跳"的区间
    progress: list[tuple[str, int, int, str]] = field(default_factory=list)  # (time, n, m, kind)


# --------------------------------------------------------------------------- #
# searchee 枚举：**逐字复刻** cross-seed v6.13.7 的 src/dataFiles.ts
# --------------------------------------------------------------------------- #
# 为什么要把这段逻辑抄过来，而不是"凭理解写一个"
# ------------------------------------------------
# 2026-09-11 直接读了 cross-seed **v6.13.7** 的源码（src/dataFiles.ts，已核对
# master 与 v6.13.7 两版，这两段代码完全一致）：
#
#   findSearcheesFromAllDataDirs = dataDirs.flatMap(dd =>
#       readdir(dd).flatMap(child => findPotentialNestedRoots(child, maxDataDepth)))
#
#   findPotentialNestedRoots(root, depth):
#     if (depth <= 0 || shouldIgnore(root))  -> []
#     else if (isDir)                        -> [...递归(子项, depth-1), root]  # 自己也算
#     else /* 文件 */                        -> [root]
#
#   shouldIgnore: 目录名 ∈ IGNORED_FOLDERS_SUBSTRINGS / 文件扩展名 ∉ VIDEO_EXTENSIONS
#
# ⚠ 所以规则是**纯按深度**，**没有**"目录里含视频才算 searchee、否则被穿透"这回事。
#   （我之前那个"含视频即叶子"的模型是错的 —— 它预测 485、实际 405。）
#   正确含义：`maxDataDepth` = **从 dataDir 往下数几层**；第 1..N 层的
#   **目录和视频文件**全都是 searchee（非视频文件忽略，黑名单目录忽略且不再下钻）。
#
# 实测校验（FRDS，maxDataDepth=2）：
#   - 本函数枚举 857 条（487 目录 + 370 视频文件）
#   - `cross-seed.db` 的 `searchee` 表 405 条**全部**落在枚举结果里（只在 DB = 0）
#   - 日志里 "Searching for" 的路径只有**第 1 层（416 行）和第 2 层（100 行）**，无第 3 层
DEFAULT_MAX_DATA_DEPTH = 2

VIDEO_EXTENSIONS = frozenset({
    ".mkv", ".mp4", ".avi", ".ts", ".m4v", ".3gp", ".nsv", ".ty", ".strm", ".rm",
    ".rmvb", ".mov", ".qt", ".divx", ".xvid", ".bivx", ".pva", ".wmv", ".asf",
    ".asx", ".ogm", ".ogv", ".m2v", ".dvr-ms", ".mpg", ".mpeg", ".avc", ".vp3",
    ".svq3", ".nuv", ".viv", ".dv", ".fli", ".flv", ".wpl", ".wtv",
})

IGNORED_FOLDER_SUBSTRINGS = ("sample", "proof", "bdmv", "bdrom", "certificate",
                             "video_ts")


def should_ignore_path(name: str, is_dir: bool) -> bool:
    """对应 cross-seed 的 `shouldIgnorePathHeuristically()`。"""
    if is_dir:
        return name.lower() in IGNORED_FOLDER_SUBSTRINGS
    return os.path.splitext(name)[1].lower() not in VIDEO_EXTENSIONS


def find_nested_roots(path: str, depth: int, is_dir: bool | None = None) -> list[str]:
    """对应 cross-seed 的 `findPotentialNestedRoots()`。

    返回顺序与源码一致（**由深到浅**，末位是自己）——源码注释说是为了 memoization，
    我们保留同样的顺序，方便和源码逐行对照。
    """
    if is_dir is None:
        is_dir = os.path.isdir(path)
    if depth <= 0 or should_ignore_path(os.path.basename(path), is_dir):
        return []
    if not is_dir:
        return [path]
    try:
        with os.scandir(path) as it:
            children = [(e.name, e.is_dir()) for e in it]
    except OSError:
        return []
    out: list[str] = []
    for name, child_is_dir in children:
        out += find_nested_roots(os.path.join(path, name), depth - 1, child_is_dir)
    out.append(path)
    return out


def find_searchee_paths(data_dir: str, max_depth: int = DEFAULT_MAX_DATA_DEPTH) -> list[str]:
    """对应 `findSearcheesFromAllDataDirs()` 里**单个** dataDir 的分支。"""
    try:
        with os.scandir(data_dir) as it:
            names = [e.name for e in it]
    except OSError:
        return []
    out: list[str] = []
    for n in names:
        out += find_nested_roots(os.path.join(data_dir, n), max_depth)
    return out


def scan_pack(local_roots: list[str], *, max_depth: int = DEFAULT_MAX_DATA_DEPTH
              ) -> tuple[list[tuple[str, str]], list[str]]:
    """把若干 dataDir 扫成 `[(单片名, 路径)]`，粒度 = cross-seed 的 searchee 目录。

    只登记**目录**，外加"直接挂在 dataDir 下的视频文件"（罕见，但它自己就是一个
    searchee）。更深的视频文件不用单独登记 —— `dir_name_of()` 会把它们塌回所在目录，
    这正是我们要的粒度。

    返回 `(条目, 重名列表)`。重名意味着两个 dataDir 下有同名目录，只能保留一个，
    调用方应当把它报出来（别静默丢）。

    ⚠ 返回的路径**一律是 `/` 分隔的规范形式**（`_norm_path()` 处理过）——
    这样 Windows 上跑（`os.path.join` 会给反斜杠）和 NAS 上跑的产出完全一致，
    调用方做前缀替换时不会踩分隔符不一致的坑。
    """
    picked: dict[str, str] = {}
    dup: list[str] = []
    for root in local_roots:
        r = _norm_path(root)
        for p in find_searchee_paths(root, max_depth):
            p = _norm_path(p)
            is_dir = os.path.isdir(p)
            # 文件只收"直接挂在 dataDir 下"的那种
            if not is_dir and _norm_path(os.path.dirname(p)) != r:
                continue
            name = p.rsplit("/", 1)[-1]
            if name in picked:
                if picked[name] != p:
                    dup.append(name)
                continue
            picked[name] = p
    return sorted(picked.items()), dup


def _norm_path(p: str) -> str:
    """日志是 Linux 的 `/`，本地测试可能是 Windows 的 `\\` —— 统一成 `/` 好比较。"""
    return p.replace("\\", "/").rstrip("/")


def dir_name_of(path: str, root: str | list[str],
                dir_paths: dict[str, str] | None = None) -> str | None:
    """把日志里的绝对路径归到"包内单片名"。

    **优先级 1：`dir_paths`（已登记路径 → 单片名）的最长前缀匹配。**
    多根 / 嵌套包必需。例：DC 的
      `…/01.绿箭侠（…）/Arrow.S01-S08…/Arrow.S01.Bluray…`
    应当命中**季层** `Arrow.S01.Bluray…`，而不是塌回容器 `Arrow.S01-S08…`；
    而没有这一层时，容器那条 searchee 也会正确命中容器自己。

    **优先级 2：退回单根语义** —— 取 root 之后的第一个路径段（老行为，兼容旧调用）。

    注意：cross-seed 的 searchee 既可能是目录，也可能是目录里的 .mkv 文件，
    两者都归到同一个单片 —— 这正是我们要的粒度。
    """
    p = _norm_path(path)
    if not p:
        return None
    if dir_paths:
        cur = p
        while True:
            hit = dir_paths.get(cur)
            if hit is not None:
                return hit
            i = cur.rfind("/")
            if i <= 0:
                break
            cur = cur[:i]
    roots = [root] if isinstance(root, str) else list(root)
    for r in sorted((_norm_path(x) for x in roots if x), key=len, reverse=True):
        if p == r:
            return None
        if p.startswith(r + "/"):
            return p[len(r) + 1:].split("/")[0] or None
    return None


def _farm_mirror(path: str, roots: list[str], farm_root: str) -> str | None:
    """把"已登记的原路径"换算成它在硬链接农场里的镜像路径。

    v3 之后 cross-seed 扫的是农场（`DATA_DIRS=/…/reseed_farm`，一个扁平目录，
    每个子项是原 dataDir 直接子项的硬链接），报上来的 searchee 路径因此长这样：
        `/volume1/video/download/reseed_farm/<一级>/…`
    而 pack 的 `roots` 还停在原目录。两套路径靠这条规则对齐：
        `<某个root>/<一级>/<其余>`  →  `<farm_root>/<一级>/<其余>`
    —— 原 dataDir 本身则映射到农场根。归不到任何 root 返回 None。
    """
    if not farm_root:
        return None
    p, f = _norm_path(path), _norm_path(farm_root)
    if not p or not f:
        return None
    for r in sorted((_norm_path(x) for x in roots if x), key=len, reverse=True):
        if p == r:
            return f
        if p.startswith(r + "/"):
            return f + "/" + p[len(r) + 1:]
    return None


def parse_log(text: str, root: str | list[str],
              dir_paths: dict[str, str] | None = None) -> LogFacts:
    """解析 cross-seed 的 info/verbose 日志（两者格式一致，verbose 更全）。"""
    f = LogFacts()
    for line in text.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        msg = m.group("msg")
        t = m.group("time")

        mm = _RE_SEARCH.search(msg)
        if mm:
            d = dir_name_of(mm.group(1), root, dir_paths)
            if d:
                f.searched.setdefault(d, set())
                f.searches_seen += 1
                full = f"{m.group('date')} {t}"
                if full > f.searched_at.get(d, ""):
                    f.searched_at[d] = full
            pm = re.search(r"\((\d+)/(\d+)\)", msg)
            if pm:
                f.progress.append((t, int(pm.group(1)), int(pm.group(2)), "search"))
            continue

        mm = _RE_SKIP.search(msg) or _RE_SKIP_INFO.search(msg)
        if mm:
            d = dir_name_of(mm.group(1), root, dir_paths)
            if mm.re is _RE_SKIP:
                idx = {x.strip() for x in mm.group(2).split(",") if x.strip()}
            else:
                # info 级日志只写了 "(filtered by temporarily disabled indexers)"，
                # 没说具体是哪个站 —— 用占位符，别塞空串（会污染集合）
                idx = {UNKNOWN_INDEXER}
            if d:
                f.skipped.setdefault(d, set()).update(idx)
                f.skips_seen += 1
            pm = re.search(r"\((\d+)/(\d+)\)", msg)
            if pm:
                f.progress.append((t, int(pm.group(1)), int(pm.group(2)), "skip"))
            continue

        mm = _RE_FOUND.search(msg)
        if mm:
            d = dir_name_of(mm.group(5), root, dir_paths)
            if d:
                f.found.setdefault(d, []).append((mm.group(2), mm.group(3)))
            continue

        mm = _RE_SNOOZE.search(msg)
        if mm:
            f.snoozes.append((t, mm.group(1), mm.group(3).strip()))
    return f


# --------------------------------------------------------------------------- #
# 读 cross-seed 的库（只读）
# --------------------------------------------------------------------------- #
def _open_csdb(db_path: Path, keep_copy: str | None = None):
    """打开 cross-seed.db 的只读连接。

    先试**直接只读打开**（实测在 SMB/UNC 上只要 0.1~0.2s，且能看到 WAL 里的最新数据）；
    失败（锁冲突等）才退回到"把 .db/-wal/-shm 三件套复制到临时目录"的老办法。
    """
    try:
        con = sqlite3.connect(str(db_path), timeout=5)
        con.execute("PRAGMA query_only = 1")
        con.execute("SELECT 1 FROM indexer LIMIT 1").fetchone()
        return con, None
    except sqlite3.Error:
        pass

    tmp = Path(keep_copy) if keep_copy else Path(tempfile.mkdtemp(prefix="csdb-"))
    tmp.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(db_path) + suffix)
        if src.is_file():
            shutil.copy2(src, tmp / (db_path.name + suffix))
    con = sqlite3.connect(str(tmp / db_path.name))
    con.execute("PRAGMA query_only = 1")
    return con, tmp


def _fallback_indexer_label(row_id: int, url: str | None) -> str:
    """给「拉不到名字」的索引器起一个**不会误导**的标签（`name` 为空时用）。

    ★ `prowlarr#N` 里的 N 是 **URL 里的 N**，也就是 Prowlarr 的索引器号 ——
    与 `TORZNAB_URLS` 的写法（`/2/api` → `#2`）和 `normalize_indexer()` 一致。

    ★★ **绝不能拿 `indexer.id` 当 N**：那是 cross-seed 的**自增行号**，不是 Prowlarr 的号。
    实测生产库：

        id=2 → /2/api → HDFans          （有名字，不走这里）
        id=4 → /3/api → BTSCHOOL 已删    ← 行号 4 会被打成 `prowlarr#4`
        id=5 → /4/api → NanyangPT (南洋)  ← 而 Prowlarr 的 #4 是**它**

    于是「`prowlarr#4` 拉不到 caps」会被读成"NanyangPT 出问题了"，实际是 `/3/api`。
    `id=1` 恰好对应 `/1/api`，所以这个错一直没暴露。
    """
    if url:
        m = _INDEXER_URL_ID.search(url)
        if m:
            return f"prowlarr#{m.group(1)}"
    return f"prowlarr#row{row_id}"  # 连 URL 都没记 —— 明确标成行号，别冒充 Prowlarr 号


def read_crossseed_db(db_path: str | Path, *, keep_copy: str | None = None) -> CrossSeedSnapshot:
    """只读 cross-seed.db，取出三样事实：

      * 每个 searchee 在**每个索引器**上最后一次搜索的时间（`timestamp` 表）
        —— 这就是「每站按周期重搜」里那个"上次搜是什么时候"
      * 匹配结论（`decision` 表，带 info_hash）
      * 索引器退避状态与解禁时间（`indexer` 表）
    """
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"找不到 cross-seed.db: {db_path}")

    con, _tmp = _open_csdb(db_path, keep_copy)
    try:
        snap = CrossSeedSnapshot()

        # 索引器：active=1 视为"当前生效"
        idx_names: dict[int, str] = {}
        for iid, name, url, active, status, retry_after in con.execute(
            "SELECT id, name, url, active, status, retry_after FROM indexer"
        ):
            label = name or _fallback_indexer_label(iid, url)
            idx_names[iid] = label
            if url:
                snap.alias[url.rstrip("/")] = label
            ra = None
            if retry_after:
                try:
                    ra = datetime.fromtimestamp(retry_after / 1000).strftime("%Y-%m-%d %H:%M:%S")
                except (OverflowError, OSError, ValueError):
                    ra = None
            snap.indexer_status[label] = (status or "-", ra)
            if active:
                snap.indexers.append(label)

        snap.searchee_total = con.execute("SELECT COUNT(*) FROM searchee").fetchone()[0]

        # searchee 名 → 完整路径：data 表的 title 与 searchee.name 对齐（实测 100%）。
        # 用它按 dataDir 前缀区分「这个包 vs 别的包」的 searchee。
        for dpath, dtitle in con.execute("SELECT path, title FROM data"):
            if dtitle:
                snap.searchee_paths.setdefault(dtitle, _norm_path(dpath))

        # 已搜过：timestamp 表（按 searchee × indexer），带**毫秒级**时间戳
        for sname, iid, last_ms in con.execute(
            """SELECT s.name, t.indexer_id, t.last_searched FROM timestamp t
               JOIN searchee s ON s.id = t.searchee_id"""
        ):
            if not sname:
                continue
            label = idx_names.get(iid, f"prowlarr#row{iid}")
            iso = ""
            if last_ms:
                try:
                    iso = datetime.fromtimestamp(last_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
                except (OverflowError, OSError, ValueError):
                    iso = ""
            tgt = snap.searched.setdefault(sname, {})
            if iso and (label not in tgt or iso > tgt[label]):
                tgt[label] = iso
        snap.searched_total = con.execute(
            "SELECT COUNT(DISTINCT searchee_id) FROM timestamp"
        ).fetchone()[0]

        # 匹配结论
        for sname, decision, info_hash in con.execute(
            """SELECT s.name, d.decision, d.info_hash FROM decision d
               JOIN searchee s ON s.id = d.searchee_id"""
        ):
            if not sname:
                continue
            snap.decisions.setdefault(sname, []).append((info_hash or "", decision or ""))
        return snap
    finally:
        con.close()


@dataclass
class IndexerBackoff:
    """一个索引器当前的退避状态。"""
    name: str
    status: str            # OK / RATE_LIMITED / ...
    until: datetime | None  # 解禁时间（None = 没在退避）
    #: ★cross-seed 库里那一列的**原始值**（实测是毫秒 epoch，见 read_indexer_backoff）。
    #: 留着它是因为「解禁时间过没过」不足以判断有没有被限流过 —— 见
    #: DriveSession.wait_out_backoff() 里关于短退避的那段。
    retry_after: float | None = None
    #: ★cross-seed 侧这个索引器是否启用（`indexer.active` 列）。
    #: 不启用 = cross-seed 自己都不会搜它，它的退避状态跟我们无关。
    enabled: bool = True

    @property
    def snoozed(self) -> bool:
        """状态是不是「被限流/退避」这一类（不看解禁时间过没过）。"""
        return self.status not in ("", "-", "OK", "NONE")

    def active(self, now: datetime | None = None) -> bool:
        """此刻是不是真的挡着路。

        `now` 可注入：调用方（DriveSession）有自己的时钟，测试也用假时钟 ——
        不注入的话这里会偷偷读真实墙钟，假时钟下"退避永远不过期"，
        这段逻辑就没法离线验证（2026-09-12 加 `check_secs` 时踩过）。
        """
        if not self.enabled:          # 禁用的索引器不挡路
            return False
        if not self.snoozed:
            return False
        return self.until is None or self.until > (now or datetime.now())


def read_indexer_backoff(db_path: str | Path) -> list[IndexerBackoff]:
    """轻量读 `indexer` 表，只看退避状态（给 drive 循环用，几十毫秒）。

    `retry_after` 是**毫秒** epoch（实测 1789172234780 →
    `2026-09-12 08:17:14`，正好对上日志里那句 `snoozing until ...`），
    所以这里除以 1000。
    """
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"找不到 cross-seed.db: {db_path}")
    con, _tmp = _open_csdb(db_path)
    try:
        out = []
        for iid, name, url, status, retry_after, enabled in con.execute(
            "SELECT id, name, url, status, retry_after, active FROM indexer"
        ):
            until = None
            if retry_after:
                try:
                    until = datetime.fromtimestamp(retry_after / 1000)
                except (OverflowError, OSError, ValueError):
                    until = None
            out.append(IndexerBackoff(name or _fallback_indexer_label(iid, url),
                                      status or "-", until,
                                      retry_after=retry_after or None,
                                      enabled=bool(enabled)))
        return out
    finally:
        con.close()


def snoozed_indexers(backoffs: list[IndexerBackoff]) -> list[IndexerBackoff]:
    """处于退避状态、且**启用中**的索引器 —— 不管解禁时间过没过。

    跟 `blocking_backoffs()` 的区别：那个只回答「现在挡不挡路」，
    这个回答「最近被限流过没有」。短退避（实测 HDFans 只有 55 秒）在两次
    检查之间就过期了，只看后者会**永远看不见它**。
    """
    return [b for b in backoffs if b.enabled and b.snoozed]


def blocking_backoffs(backoffs: list[IndexerBackoff],
                      now: datetime | None = None) -> list[IndexerBackoff]:
    """当前真正挡住搜索的退避（状态非 OK 且解禁时间还没到）。"""
    return [b for b in backoffs if b.active(now)]


# --------------------------------------------------------------------------- #
# 额度感知（SUMMARY §16.1）—— 把「该看的数字」按时算出来
#
# ★ 先说清楚它**不是**什么：它**拿不到「站点账号还剩多少额度」**。
#   Prowlarr 的 Query Limit 是我们自己配的**本地计数器**（到顶就临时禁用索引器），
#   站点网页上那句「今天还剩 N 次」，Prowlarr 从来就不知道（§16.1.1 的 ③）。
#   所以本节的定位是「把『每天登站看两次』降到『出异常才登站』」，**不是取代人工**。
#   把它当余额表来做，一定做成一个自己骗自己的假指标。
#
# 三个来源各自独立，**对不上就是信号**（§16.1.2）：
#   A 我们自己数   cross-seed.db 的 `timestamp` 表（零额外请求、零新增存储）
#   B 问 Prowlarr  /api/v1/indexerstats（需 API key；★见该函数的"未验证"说明）
#   C 保险丝状态   cross-seed 的 `indexer` 表 —— 退避状态与解禁时间
#
# ★ 为什么不用"另开一张计数表"：§16.1.3 要求「计数必须落盘」（`--once` 每轮都是
#   新进程，内存计数器每轮归零）。来源 A **本身就是落盘的** —— `timestamp` 表是
#   cross-seed 的**持久**表，不是我们维护的计数器。所以那条要求**自然满足**，
#   一行新存储都不用加。这是选 A 当主来源的一个附带好处。
# --------------------------------------------------------------------------- #

@dataclass
class QuotaLine:
    """一个索引器在**滚动 24 小时**窗口里的用量与当前状态。"""
    indexer: str
    #: 来源 A：最近 `hours` 小时内有搜索记录的**部数**。★口径见 quota_snapshot。
    searches_24h: int
    last_search: datetime | None = None
    #: 来源 C：cross-seed 记的退避状态（OK / RATE_LIMITED / …）
    status: str = "-"
    until: datetime | None = None
    enabled: bool = True
    #: 来源 B：Prowlarr 自报的近 24h 查询数。None = 没配 / 没拿到（**不是 0**）
    prowlarr_queries: int | None = None
    #: 来源 B 不可用时的原因（人话），供渲染时说明"为什么这一列是空的"
    #: ★ **只表示"整体拿不到"**（没配 key / 连不上 / 结构不认识）——
    #: 那时所有行都会被写上同一个理由。**单行**名字对不上用 `prowlarr_mismatch`。
    prowlarr_note: str = ""
    #: ★ 单行级别：这个站在 Prowlarr 里**找不到同名条目**。
    #: 与 `prowlarr_note` 分开是必须的 —— 混在一起会导致
    #: "某一行名字对不上" 被渲染成 "来源 B 互校不可用"，
    #: 而 B 其实好得很（实测：HDtime 明明从 B 拿到了数，却仍印这句，纯属误导）。
    prowlarr_mismatch: bool = False
    #: ★ 这一行**只存在于 Prowlarr**（cross-seed.db 里没有它）——
    #: 见 `attach_prowlarr_quota` 的说明。它**不是**我们的索引器。
    prowlarr_only: bool = False

    @property
    def snoozed(self) -> bool:
        return self.status not in ("", "-", "OK", "NONE")

    def active(self, now: datetime | None = None) -> bool:
        if not self.enabled or not self.snoozed:
            return False
        return self.until is None or self.until > (now or datetime.now())

    @property
    def disagrees(self) -> bool:
        """A 与 B 对不上 —— 只在一方为 0 另一方非 0 时才叫"对不上"。

        ★ 不比较**大小**：两边口径不同（A 是"部数"，B 是 Prowlarr 记的"查询数"，
        一部片往往会发好几个查询），数值本来就该有倍数差。强行比大小只会天天误报。
        真正有信息量的是**"一边有、一边是 0"**：说明有东西在吃额度而我们没算进去
        （手动在 Prowlarr 界面搜、别的 *arr 也接了同一个索引器），或者反过来
        我们以为搜了、Prowlarr 根本没收到。

        ★ `prowlarr_only` 的行不走这里：它 A 恒为 0、B 非 0，机械地判就是"对不上"，
        但那句话没说清重点（重点不是"数对不上"，是"**这压根不是我们的站**"）。
        它有自己的旗标，见 `render_quota`。
        """
        if self.prowlarr_only:
            return False
        if self.prowlarr_queries is None:
            return False
        return (self.searches_24h == 0) != (self.prowlarr_queries == 0)


def quota_snapshot(crossseed_db: str | Path, *, hours: float = 24.0,
                   now: datetime | None = None) -> list[QuotaLine]:
    """各站「最近 24 小时搜了多少部」+ 现在的退避状态（来源 A + C）。

    ★ **口径是"部数"，不是"查询次数"**：`timestamp` 表是 (searchee, indexer)
    一行、只留 `last_searched` 最后一次。所以同一部片在窗口内被重搜多次**只算 1**。
    真正的"发了多少次请求"表里根本没存 —— 本函数的数**不能**直接拿去和
    Prowlarr 的 Query Limit 比大小。

    ★ **窗口是滚动的 24 小时，不是自然日**（§16.1.3）。Prowlarr 的 limit 重置在
    UTC 0 点（= 本地 08:00），自然日窗口跟它对不齐，会出现"我们显示 200、
    Prowlarr 却已经烧了保险丝"。滚动窗口没有这个错位。
    顺带：滚动窗口是**相对**的，所以这里**不需要**处理时区 ——
    `last_searched` 是 UTC 毫秒，但比较在 epoch 秒上做，两边同源。

    按 `searches_24h` 降序。
    """
    db_path = Path(crossseed_db)
    if not db_path.is_file():
        raise FileNotFoundError(f"找不到 cross-seed.db: {db_path}")
    now = now or datetime.now()
    cutoff = now.timestamp() - hours * 3600.0        # epoch 秒

    con, _tmp = _open_csdb(db_path)
    try:
        rows = list(con.execute(
            "SELECT id, name, url, status, retry_after, active FROM indexer"))
        counts: dict[int, int] = {}
        latest: dict[int, float] = {}
        for iid, last_ms in con.execute(
                "SELECT indexer_id, last_searched FROM timestamp"):
            if not last_ms:
                continue
            sec = last_ms / 1000.0
            if sec < cutoff:            # 窗口外的行直接丢，不参与计数
                continue
            counts[iid] = counts.get(iid, 0) + 1
            if sec > latest.get(iid, 0.0):
                latest[iid] = sec
    finally:
        con.close()

    def _dt(sec: float) -> datetime | None:
        try:
            return datetime.fromtimestamp(sec)
        except (OverflowError, OSError, ValueError):
            return None

    out: list[QuotaLine] = []
    known: set[int] = set()
    for iid, name, url, status, retry_after, active in rows:
        known.add(iid)
        until = _dt(retry_after / 1000.0) if retry_after else None
        out.append(QuotaLine(
            indexer=name or _fallback_indexer_label(iid, url),
            searches_24h=counts.get(iid, 0),
            last_search=_dt(latest[iid]) if iid in latest else None,
            status=status or "-",
            until=until,
            enabled=bool(active),
        ))
    # 库里还留着搜索记录、但 `indexer` 表里已经没有这个 id 了（索引器被删过）。
    # 不补上的话这些"幽灵用量"会被静默丢掉 —— 而"有东西在吃额度但看不见"
    # 正是本节最该报出来的那类信号。
    for iid in sorted(set(counts) - known):
        out.append(QuotaLine(
            indexer=_fallback_indexer_label(iid, None),
            searches_24h=counts[iid],
            last_search=_dt(latest[iid]) if iid in latest else None,
            enabled=False,
        ))
    out.sort(key=lambda q: (-q.searches_24h, q.indexer))
    return out


def prowlarr_indexer_stats(base_url: str, api_key: str, *,
                           timeout: float = 10.0) -> tuple[dict[str, int], str]:
    """问 Prowlarr 要各索引器**近 24 小时**的查询数（来源 B）。返回 `(表, 原因)`。

    ★★ **本函数尚未对真实响应验证过**（2026-09-12）。原因写在这里，别当它是既成事实：
    NAS 上那个 Prowlarr 的 `/api/v1/indexerstatus` 与 `/api/v1/indexerstats`
    都返回 **401** —— `.env` 里 `TORZNAB_URLS` 带的 apikey **不是 Prowlarr 的 API key**
    （两个索引器还各带一个不同的 16 位串），`/docs` 与 openapi.json 也都 401。
    所以**字段名是从 Prowlarr 公开文档抄的，没有拿真实响应核对过**。

    正因为没验证过，这里的策略是**宁可不报、绝不猜**：
      * 没配 key      → `({}, "未配置 Prowlarr API key")`
      * 请求失败      → `({}, "HTTP <错误>")`
      * 结构不认识    → `({}, "响应结构不认识（本函数未验证过）")`
      * 认出来了      → `({站名: 查询数}, "")`
    **它永远不会返回一个自己编的数** —— 报错了顶多是这一列空着，
    不会拿一个错的数字去和来源 A 做"互校"、再把结论引向错误的方向。

    配好 key 之后要做的第一件事：把返回值打出来跟 A 对一遍口径（见 `disagrees`）。
    `base_url` 形如 `http://192.168.0.7:9696`（**宿主机 IP**，不是容器名 ——
    drive-loop 跑在 NAS 宿主机上，`prowlarr` 这个服务名它解析不了）。
    """
    if not api_key:
        return {}, "未配置 Prowlarr API key"
    import urllib.error
    import urllib.request

    url = f"{base_url.rstrip('/')}/api/v1/indexerstats"
    req = urllib.request.Request(url, headers={"X-Api-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except Exception as e:                       # 网络/鉴权/超时 —— 一律降级，不抛
        return {}, f"请求失败：{type(e).__name__}: {e}"
    try:
        data = json.loads(raw)
    except ValueError:
        return {}, "响应不是合法 JSON（多半是被拦了或拿到的不是 API 响应）"
    if not isinstance(data, dict):
        return {}, "响应顶层不是对象"

    rows = data.get("indexers")
    if not isinstance(rows, list):
        # 认不出来就明说。★ 不要在这里"尽力而为"地翻找 —— 猜错了会变成一个
        # 看着很合理、其实无意义的数字，而它还要参与"互校"。
        return {}, "响应里没有 indexers 数组（本函数未对真实响应验证过）"
    out: dict[str, int] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        name = r.get("indexerName") or r.get("name")
        n = r.get("numberOfQueries")
        if isinstance(name, str) and name and isinstance(n, (int, float)):
            out[name] = int(n)
    if not out:
        return {}, "indexers 数组里没有能认出的条目（字段名未验证）"
    return out, ""


def _norm_site(s: str) -> str:
    """站点名归一化，用来把 Prowlarr 的名字跟 cross-seed 的 label 对上。

    Prowlarr 的 `indexerName` 与 cross-seed 的 label **未必逐字相同**
    （例如 cross-seed 记的是 `NanyangPT (南洋)`、Prowlarr 记的是 `NanyangPT`；
    或者反过来，Prowlarr 名字里带 "(API)" 之类后缀）。
    只保留字母数字、全部小写，把这类差异抹掉；**不**做模糊匹配 ——
    匹配不上就诚实地说匹配不上（见 `attach_prowlarr_quota`）。
    """
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def attach_prowlarr_quota(lines: list[QuotaLine], base_url: str, api_key: str, *,
                          timeout: float = 10.0) -> list[QuotaLine]:
    """把来源 B（Prowlarr 自报的 24h 查询数）填进台账，**原地改并返回**。

    整体拿不到（没配 key / 401 / 结构不认识）→ 每行 `prowlarr_queries` 留 `None`、
    `prowlarr_note` 记同一个原因 —— 渲染时就是"这一列空着 + 一句为什么"。
    单行名字对不上 → 只给那一行记 `"Prowlarr 里没有这个名字"`。

    ★★ 2026-09-12：**还往回加行** —— 只在 Prowlarr 里、cross-seed.db 里没有的索引器，
    补成 `prowlarr_only=True` 的行（只挑 `numberOfQueries > 0` 的）。

    为什么要加：本函数原先**只遍历来源 A 的行**去 B 里查名字，于是
    「Prowlarr 配了、我们不用的站」**一行都不会出现** —— 而它恰恰是
    **"有别的工具在用同一个 Prowlarr" 的唯一信号**。实测：Prowlarr 里有
    `HDtime`（启用，8 次查询含 4 次失败），而我们的 `TORZNAB_URLS` 只有 2 条，
    台账里**完全看不见它**。看不见的东西不能叫"没问题"，只能叫"没在看"。
    （`numberOfQueries == 0` 的不加：那只是一个没用的配置，不是信号，天天打是噪音。）

    ★ 无论哪条路径，**都不会出现"猜出来的数字"**：`prowlarr_queries` 非 None
    就意味着它确实来自 Prowlarr 的响应（见 `prowlarr_indexer_stats` 的说明）。
    """
    stats, note = prowlarr_indexer_stats(base_url, api_key, timeout=timeout)
    if note:
        for q in lines:
            q.prowlarr_note = note
        return lines
    by_norm = {_norm_site(k): v for k, v in stats.items()}
    seen = set()
    for q in lines:
        seen.add(_norm_site(q.indexer))
        n = by_norm.get(_norm_site(q.indexer))
        if n is None:
            q.prowlarr_mismatch = True
        else:
            q.prowlarr_queries = n
    for name, n in stats.items():
        if _norm_site(name) in seen or not n:
            continue
        lines.append(QuotaLine(indexer=name, searches_24h=0,
                               prowlarr_queries=n, prowlarr_only=True))
    return lines


def render_quota(lines: list[QuotaLine], *, title: str = "站点额度台账",
                 note: str = "滚动 24 小时；★是「部数」不是「查询次数」",
                 limit: int = 0, hide_idle: bool = True) -> str:
    """把台账渲染成**纯文本几行**（邮件用；§16.3.2 的"别做图/HTML"同理）。

    额度是**慢性**问题，所以这里是给日报用的，不是即时告警。

    `hide_idle`：把「**已禁用且窗口内 0 部**」的行藏掉 —— 那些是早就删掉的索引器
    （实测 `prowlarr#1` / `prowlarr#3`），留着只是每天占两行，看久了就没人看了。
    ★ 但**只藏"零用量"的**：已禁用却**有**用量的行必须留下 ——
    那正是「有东西在吃额度而我以为它已经关掉了」这类最该被看见的情况。
    """
    shown_lines = [q for q in lines
                   if not (hide_idle and not q.enabled and q.searches_24h == 0)]
    if not shown_lines:
        return f"{title}：没有可报告的索引器"
    out = [f"{title}（{note}）" if note else title]
    # ★ `prowlarr_only` 的行（不是我们的索引器，但 Prowlarr 记到有查询）
    #   **不参与 limit 截断**：limit 是给"我们自己的站"防日常噪音用的，
    #   而这一档本身罕见且重要 —— 被截掉就等于白加。
    ours = [q for q in shown_lines if not q.prowlarr_only]
    extras = [q for q in shown_lines if q.prowlarr_only]
    shown = ours[:limit] if limit else ours
    for q in shown + extras:
        flags = []
        if q.active():
            flags.append(f"⚠ 退避中{('，解禁 ' + q.until.strftime('%m-%d %H:%M')) if q.until else ''}")
        elif q.snoozed:
            flags.append(f"（见过限流：{q.status}）")
        if not q.enabled:
            flags.append("（已禁用）")
        if q.prowlarr_mismatch:
            flags.append("（Prowlarr 里没有这个名字 —— 两边命名不一致？）")
        if q.prowlarr_only:
            flags.append(f"★ 不是本项目的索引器，却被 Prowlarr 记了 "
                         f"{q.prowlarr_queries} 次查询（别的东西在用这个 Prowlarr？）")
        if q.disagrees:
            flags.append(f"‼ Prowlarr 说 {q.prowlarr_queries} 次，对不上")
        tail = ("  " + " ".join(flags)) if flags else ""
        # 外来行没有"我们的用量"这回事，打 `-` 而不是 `0`：
        # 0 会被读成"我们搜了 0 次"，而事实是"这一行根本不属于我们"。
        cnt = "   -" if q.prowlarr_only else f"{q.searches_24h:>4}"
        out.append(f"  {q.indexer:<18} {cnt} 部{tail}")
    if limit and len(ours) > limit:
        out.append(f"  …还有 {len(ours) - limit} 个索引器")
    hidden = len(lines) - len(shown_lines)
    if hidden:
        out.append(f"  （另有 {hidden} 个已禁用的索引器窗口内无用量，未列出）")
    lost = [q for q in lines if q.prowlarr_note]
    if lost:
        out.append(f"  （来源 B 互校不可用：{lost[0].prowlarr_note}）")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# 命中率趋势（SUMMARY §16.3）
# --------------------------------------------------------------------------- #

#: `movie.matched_indexers` 为空时的兜底归属名。★ **不并进任何站**，单列一栏 ——
#: 它变多说明"匹配到了但没记下来"，那本身是个 bug 信号，混进某个站就看不见了。
UNATTRIBUTED_SITE = "（未记站点）"


@dataclass
class TrendReport:
    """(周, 站) 的新增做种矩阵。由 `StateStore.trend()` 产出。"""
    weeks: list[str] = field(default_factory=list)              # 升序
    sites: list[str] = field(default_factory=list)
    cells: dict = field(default_factory=dict)                   # (week, site) -> n
    week_totals: dict = field(default_factory=dict)             # week -> n
    total: int = 0
    unattributed: int = 0

    def render(self, *, weeks_shown: int = 2) -> str:
        """渲染成**纯文本几行** —— 邮件里一屏能看完才有用（§16.3.2）。

        ⚠ 别改成图/HTML：收件端是纯文本邮件，HTML 只会变成一坨标签。
        """
        if not self.week_totals:
            return "新增做种趋势：暂无数据（还没有片走到 SEEDING）"
        cur = self.weeks[-1]
        out = [f"本周（{cur}）新增做种 {self.week_totals[cur]} 部（去重）"]
        site_sum = 0
        for s in sorted(self.sites,
                        key=lambda x: (-self.cells.get((cur, x), 0), x)):
            n = self.cells.get((cur, s), 0)
            if n:
                out.append(f"    {s}  {n}")
                site_sum += n
        # ★ 按站计数之和 > 总数是因为**一部片在两个站都做种会各记一次**。
        #   不写这句，读者会以为数对不上（这是实测撞到的第一反应）。
        if site_sum > self.week_totals[cur]:
            out.append(f"    ↑ 按站相加 {site_sum} > {self.week_totals[cur]}："
                       "同一部片在多个站做种时，每个站各记一次")
        # 上一周对比才是重点 —— 换站决策问的是"这周比上周好了没有"，
        # 光看本周一个数答不了。
        if len(self.weeks) >= 2:
            prev = self.weeks[-2]
            delta = self.week_totals[cur] - self.week_totals[prev]
            out.append(f"    上周（{prev}）{self.week_totals[prev]} 部 → "
                       f"{delta:+d}")
        if self.unattributed:
            out.append(f"    ★ {self.unattributed} 部没记到站点"
                       "（matched_indexers 为空 —— 可能是同步漏了）")
        return "\n".join(out)


# --------------------------------------------------------------------------- #
# 状态库
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS pack (
  name            TEXT PRIMARY KEY,
  root            TEXT NOT NULL,          -- ★cross-seed 视角的路径（NAS 上），webhook 要用它
  local_root      TEXT,                   -- 可选的本地/UNC 等价路径，只用于"列目录"
  roots           TEXT,                   -- JSON 数组：多根包的全部 root（root = 第一个）
  local_roots     TEXT,                   -- JSON 数组：与 roots 一一对应的可列目录路径
  max_depth       INTEGER,                -- 枚举深度，对应 cross-seed 的 maxDataDepth
  farm_root       TEXT,                   -- ★v3：cross-seed 现在扫的"硬链接农场"根
  link_dir        TEXT,
  category        TEXT,
  created_at      TEXT NOT NULL,
  scan_started_at TEXT,
  scan_finished_at TEXT,
  scan_complete   INTEGER NOT NULL DEFAULT 0,
  note            TEXT
);

CREATE TABLE IF NOT EXISTS movie (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  pack              TEXT NOT NULL REFERENCES pack(name) ON DELETE CASCADE,
  dir_name          TEXT NOT NULL,
  path              TEXT NOT NULL,
  stage             TEXT NOT NULL DEFAULT 'PENDING',
  attempts          INTEGER NOT NULL DEFAULT 0,
  last_attempt_at   TEXT,
  next_retry_at     TEXT,
  searched_indexers TEXT NOT NULL DEFAULT '[]',
  indexer_seen      TEXT NOT NULL DEFAULT '{}',
  skipped_indexers  TEXT NOT NULL DEFAULT '[]',
  matched_indexers  TEXT NOT NULL DEFAULT '[]',
  matched_hashes    TEXT NOT NULL DEFAULT '[]',
  seeding_count     INTEGER NOT NULL DEFAULT 0,
  last_error        TEXT,
  note              TEXT,
  updated_at        TEXT,
  UNIQUE(pack, dir_name)
);
CREATE INDEX IF NOT EXISTS idx_movie_stage ON movie(pack, stage);

CREATE TABLE IF NOT EXISTS attempt (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  movie_id         INTEGER NOT NULL REFERENCES movie(id) ON DELETE CASCADE,
  at               TEXT NOT NULL,
  kind             TEXT NOT NULL,          -- search | skip | inject | sync | manual
  result           TEXT NOT NULL,          -- matched | unmatched | skipped | seeding | error | note
  indexers         TEXT,
  skipped_indexers TEXT,
  note             TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempt_movie ON attempt(movie_id, at);
-- 2026-09-12：趋势（§16.3）要按 `at` 聚合。原先只有 (movie_id, at) 复合索引，
-- 按 `at` 单列查会**全表扫**。现在 1350 行无所谓，但趋势是长期功能。
CREATE INDEX IF NOT EXISTS idx_attempt_at ON attempt(at);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _j(v) -> str:
    return json.dumps(sorted(v), ensure_ascii=False)


def _dumps(d: dict) -> str:
    return json.dumps(d, ensure_ascii=False, sort_keys=True)


def _d(s: str | None) -> dict:
    """读一个 JSON 对象列（容错：老库里可能是 '[]'）。"""
    if not s:
        return {}
    try:
        v = json.loads(s)
        return dict(v) if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def _u(s: str | None) -> list[str]:
    if not s:
        return []
    try:
        v = json.loads(s)
        return list(v) if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


class StateStore:
    """我们的 sidecar 状态库。**唯一**被本模块写入的库。"""

    def __init__(self, db_path: str | Path, *, create: bool = False):
        """打开状态库。**默认不建**。

        ★★ `create=False`（默认）时，文件不在就抛 `FileNotFoundError` ——
          因为 `sqlite3.connect()` 连一个不存在的路径**不报错**，而下面的
          `executescript(SCHEMA)` 会把它**就地建成一个 0 字节的空库**。
          于是「读不到」伪装成「读到的是一个空库」：`pack` / `movie` 两张表全空 →
          读的人拿到的是一个**看起来成功**的结果，而不是一次失败。
          `cmd_state` 的注释里记着那次现场：打错一层路径，就在**媒体目录**里
          留下了一个空 `state.db`，而它看起来"命令跑成功了"。
        ★ 要建库的只有两种调用者：`reseed-state.py init`（它的职责就是建库）
          和测试夹具。它们显式传 `create=True` —— **让"会写磁盘"在调用点看得见**，
          而不是藏在一个 13 个调用点共享的默认值里。
        ★ 闸放在 `__init__` 而不是各调用点：**调用点各加一遍正是上次漏掉的那种做法**
          —— `reconcile_watch` 一个人加了 `is_file()`，另外 13 处没加，
          而那 13 处里有在热路径上的（`run_round`）：路径打错 →
          建空库 → `todo_detail` 把整包看成 PENDING → **全量重搜、额度静默烧掉一轮**。
        ★ 行为变更：热路径上的错路径，从「静默建空库 + 全量重搜」变成
          「本批异常 → `consec_abort` 涨 → 到 3 批发告警」。方向是**从安静变响**。
        """
        self.path = Path(db_path)
        if not create and not self.path.is_file():
            raise FileNotFoundError(
                f"状态库不存在: {self.path} —— 本路径**只读**，不会替你建空库"
                f"（空库会伪装成「一部都没登记」）。\n"
                f"       要建库：`reseed-state.py --db <路径> init --pack ...`；"
                f"要显式建：`StateStore(<路径>, create=True)`。\n"
                f"       库的真实位置由 drive-loop 的运行目录决定，常见的是 "
                f"`<compose>/drive-loop/hlink/state.db`。")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(self.path))
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self._migrate()
        self.con.commit()

    def _migrate(self) -> None:
        """给已存在的老库补列（本模块自己的库，schema 演进很轻量）。"""
        cols = {r["name"] for r in self.con.execute("PRAGMA table_info(pack)")}
        if "local_root" not in cols:
            self.con.execute("ALTER TABLE pack ADD COLUMN local_root TEXT")
        # 2026-09-11：支持多根 / 嵌套包（DC 的"单片"分散在 47 个标签目录里）
        for col in ("roots", "local_roots"):
            if col not in cols:
                self.con.execute(f"ALTER TABLE pack ADD COLUMN {col} TEXT")
        if "max_depth" not in cols:
            self.con.execute("ALTER TABLE pack ADD COLUMN max_depth INTEGER")
        # 2026-09-12：v3 农场切换后 cross-seed 报的是**农场路径**，
        # 而 pack 的 roots 还是原路径 —— 认不出就全判成"别的包"静默跳过，
        # SEEDING 会塌成 0。记下农场根，把两套路径对上。
        if "farm_root" not in cols:
            self.con.execute("ALTER TABLE pack ADD COLUMN farm_root TEXT")
        mcols = {r["name"] for r in self.con.execute("PRAGMA table_info(movie)")}
        if "indexer_seen" not in mcols:
            self.con.execute(
                "ALTER TABLE movie ADD COLUMN indexer_seen TEXT NOT NULL DEFAULT '{}'")
        # ★ 索引也要给**已存在的老库**补上 —— SCHEMA 里的 CREATE INDEX 只在建库时跑，
        #   老库 executescript 虽然也会执行那句 IF NOT EXISTS……但它跑在 _migrate 之前、
        #   而老库的 attempt 表早就存在，所以其实会建上。这里再兜一次是因为
        #   `attempt` 表本身是后来才加进 SCHEMA 的库可能连表结构都不同 ——
        #   重复 CREATE INDEX IF NOT EXISTS 是幂等的，代价只有一次 PRAGMA。
        self.con.execute("CREATE INDEX IF NOT EXISTS idx_attempt_at ON attempt(at)")

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- pack --------------------------------------------------------------- #
    def upsert_pack(self, name: str, root: str, *, local_root: str | None = None,
                    roots: list[str] | None = None,
                    local_roots: list[str] | None = None,
                    max_depth: int | None = None,
                    farm_root: str | None = None,
                    link_dir: str | None = None,
                    category: str | None = None, note: str | None = None) -> None:
        """登记/更新一个包。

        多根包（如 DC 的 47 个标签目录）传 `roots` / `local_roots`；
        `root` 仍必填 —— 它是 `roots[0]`，老代码和 webhook 都还在用它。

        `farm_root`（v3）：cross-seed 实际扫描的硬链接农场根。设了它之后，
        农场里的 `<farm_root>/<一级目录>/<其余>` 会被认作原包
        `<某个root>/<一级目录>/<其余>` 的同一条目（见 `_farm_mirror`）。
        """
        roots_json = json.dumps(roots, ensure_ascii=False) if roots else None
        lroots_json = json.dumps(local_roots, ensure_ascii=False) if local_roots else None
        self.con.execute(
            """INSERT INTO pack(name, root, local_root, roots, local_roots, max_depth,
                                farm_root, link_dir, category, created_at, note)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 root=excluded.root,
                 local_root=COALESCE(excluded.local_root, pack.local_root),
                 roots=COALESCE(excluded.roots, pack.roots),
                 local_roots=COALESCE(excluded.local_roots, pack.local_roots),
                 max_depth=COALESCE(excluded.max_depth, pack.max_depth),
                 farm_root=COALESCE(excluded.farm_root, pack.farm_root),
                 link_dir=COALESCE(excluded.link_dir, pack.link_dir),
                 category=COALESCE(excluded.category, pack.category),
                 note=COALESCE(excluded.note, pack.note)""",
            (name, root, local_root, roots_json, lroots_json, max_depth,
             farm_root, link_dir, category, _now(), note),
        )
        self.con.commit()

    def local_root(self, pack: str) -> str:
        """能用来列目录的那个根（优先 local_root，否则 root）。"""
        p = self.pack(pack)
        if p is None:
            raise KeyError(f"包未登记: {pack!r}")
        return p["local_root"] or p["root"]

    def roots(self, pack: str) -> list[str]:
        """包的全部 cross-seed 视角根（多根包返回全部；单根包返回 `[root]`）。"""
        p = self.pack(pack)
        if p is None:
            raise KeyError(f"包未登记: {pack!r}")
        return _u(p["roots"]) or [p["root"]]

    def local_roots(self, pack: str) -> list[str]:
        """包的全部"可列目录"根，与 `roots()` 按序对应。

        多根包必须逐根配对：`roots[i]` 与 `local_roots[i]` 指的是同一目录，
        只是视角不同（NAS 路径 vs UNC 路径）。数量对不上就退回 `local_root()`。
        """
        p = self.pack(pack)
        if p is None:
            raise KeyError(f"包未登记: {pack!r}")
        rs, lrs = _u(p["roots"]), _u(p["local_roots"])
        if lrs and len(lrs) == len(rs):
            return lrs
        return [p["local_root"] or p["root"]]

    def max_depth(self, pack: str) -> int:
        p = self.pack(pack)
        if p is None:
            raise KeyError(f"包未登记: {pack!r}")
        try:
            return int(p["max_depth"])
        except (TypeError, ValueError, IndexError):
            return DEFAULT_MAX_DATA_DEPTH

    def farm_root(self, pack: str) -> str:
        """cross-seed 实际扫描的硬链接农场根；没设就返回空串（旧库/未迁移）。"""
        p = self.pack(pack)
        if p is None:
            raise KeyError(f"包未登记: {pack!r}")
        return p["farm_root"] or ""

    def pack(self, name: str) -> sqlite3.Row | None:
        return self.con.execute("SELECT * FROM pack WHERE name=?", (name,)).fetchone()

    def packs(self) -> list[sqlite3.Row]:
        """全部已登记的包，按名字排序。

        给「不带 --pack 时列个总览」用（编排器的 `state` 子命令）。
        ★ 排序放在 SQL 里而不是 Python 里：包名里有中文和连字符，
          交给 SQLite 的默认排序至少是**稳定**的，跨调用顺序一致。
        """
        return list(self.con.execute("SELECT * FROM pack ORDER BY name"))

    def mark_scan(self, name: str, *, started: bool = False, finished: bool = False,
                  complete: bool | None = None) -> None:
        if started:
            self.con.execute("UPDATE pack SET scan_started_at=? WHERE name=?", (_now(), name))
        if finished:
            self.con.execute("UPDATE pack SET scan_finished_at=? WHERE name=?", (_now(), name))
        if complete is not None:
            self.con.execute("UPDATE pack SET scan_complete=? WHERE name=?",
                             (1 if complete else 0, name))
        self.con.commit()

    # -- movie -------------------------------------------------------------- #
    def register_dirs(self, pack: str, entries: list[tuple[str, str]]) -> int:
        """把 `[(单片名, 完整路径)]` 登记成 PENDING（已存在的不动，stage 不会被重置）。

        多根包必须传**完整路径** —— 因为两个根下可能有同名目录，而且
        `dir_name_of()` 要靠路径做最长前缀匹配。
        """
        added = 0
        for name, path in entries:
            cur = self.con.execute(
                """INSERT INTO movie(pack, dir_name, path, stage, updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(pack, dir_name) DO UPDATE SET path=excluded.path""",
                (pack, name, _norm_path(path), STAGE_PENDING, _now()),
            )
            added += cur.rowcount or 0
        self.con.commit()
        return added

    def dir_paths(self, pack: str) -> dict[str, str]:
        """`{规范化完整路径: 单片名}` —— 给 `dir_name_of()` 做最长前缀匹配用。

        用完整路径（而不是只有目录名）是为了让嵌套包的"季层"能被正确区分：
        DC 的 `…/01.绿箭侠…/Arrow.S01-S08…/Arrow.S01.Bluray…` 应当命中季层，
        而不是塌回容器 `Arrow.S01-S08…`。
        """
        return {_norm_path(r["path"]): r["dir_name"] for r in self.movies(pack)}

    def farm_dir_paths(self, pack: str) -> dict[str, str]:
        """`{农场镜像路径: 单片名}` —— 与 `dir_paths()` 合并后交给 `dir_name_of()`。

        农场路径和原路径指向同一条目，所以映射到同一个 `dir_name`；合并之后
        `dir_name_of` 的最长前缀匹配对**两套路径**都成立，它自己和
        `parse_log()` 的三个调用点都不用动。没登记农场根就返回空 dict（老行为）。
        """
        f = self.farm_root(pack)
        if not f:
            return {}
        roots = self.roots(pack)
        out: dict[str, str] = {}
        for r in self.movies(pack):
            m = _farm_mirror(r["path"], roots, f)
            if m:
                out.setdefault(m, r["dir_name"])
        return out

    def movies(self, pack: str, *, stages: list[str] | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM movie WHERE pack=?"
        args: list = [pack]
        if stages:
            sql += f" AND stage IN ({','.join('?' * len(stages))})"
            args += stages
        sql += " ORDER BY dir_name"
        return list(self.con.execute(sql, args))

    def movie_by_dir(self, pack: str, dir_name: str) -> sqlite3.Row | None:
        return self.con.execute(
            "SELECT * FROM movie WHERE pack=? AND dir_name=?", (pack, dir_name)
        ).fetchone()

    def movie_by_path(self, pack: str, path: str) -> sqlite3.Row | None:
        return self.con.execute(
            "SELECT * FROM movie WHERE pack=? AND path=?", (pack, path)
        ).fetchone()

    def attempts(self, movie_id: int) -> list[sqlite3.Row]:
        return list(self.con.execute(
            "SELECT * FROM attempt WHERE movie_id=? ORDER BY at, id", (movie_id,)
        ))

    # -- 同步（把事实写进 movie 行）---------------------------------------- #
    def sync_movie(
        self,
        movie_id: int,
        *,
        searched_indexers: set[str],
        skipped_indexers: set[str],
        matched: list[tuple[str, str]],       # [(info_hash, indexer)]
        seeding_hashes: set[str],
        indexers_now: list[str],
        searched_at: dict[str, str] | None = None,   # {索引器: 最后搜索时间}
        cadence_days: int = DEFAULT_CADENCE_DAYS,
        cadence_by_indexer: dict[str, int] | None = None,
        log_attempts: bool = True,
        seeding_names: set[str] | None = None,
    ) -> str:
        """按事实重算这一行的 stage。返回新 stage。

        `attempts` / `next_retry_at` 是**我们自己的**簿记（cross-seed 不管这个）。
        `indexer_seen` 记录"每个站最后一次搜它是什么时候"，是「每站按周期重搜」的依据。

        ★★ `seeding_names`（2026-09-20，`e404b1ca#5`）：qB 里所有种子的 `name`
        （含去扩展名的形态）。**这是"在做种"的第二条独立判据** ——
        原先只有 `seeding_hashes ∩ matched_hashes`，而后者源于 cross-seed 的
        `decision` 表 ⇒ **cross-seed 没记账 = 判成没做种**（实测低了约 30 倍，
        见 `sync_pack` 那段注释与 SUMMARY §26.33）。两条求并：任一条成立即算做种。
        `None` = 调用方没给（老调用点），退回旧行为 —— **不静默放宽**。
        """
        row = self.con.execute("SELECT * FROM movie WHERE id=?", (movie_id,)).fetchone()
        if row is None:
            raise KeyError(f"movie id={movie_id} 不存在")

        old_stage = row["stage"]
        old_searched = set(_u(row["searched_indexers"]))
        old_skipped = set(_u(row["skipped_indexers"]))
        old_hashes = set(_u(row["matched_hashes"]))
        old_seen: dict[str, str] = _d(row["indexer_seen"])

        matched_hashes = {h for h, _ in matched if h}
        # ★ `matched_indexers` 是**单调事实** —— 这部片在这个站匹配过，就是匹配过。
        #   与 `indexer_seen` **同语义**，所以同样**与旧值取并集**；不像其它列那样
        #   整行覆盖写。（两列语义相同、写法相反，是**遗漏**不是设计。）
        #
        #   ★★ 不并的后果（2026-09-14 实测 #73）：`found` 的唯一输入是**当天**的
        #   `info.current.log`（见 `sync_pack` 里那段），而这一列每次 sync 都**重算**。
        #   日志跨天一滚动，下一次 sync 就拿不到那批 Found 行 ⇒ 整列被抹成 `[]`；
        #   而 SEEDING 的行**不会再被搜** ⇒ **永不恢复**。
        #   实测见证：同一张 605 行的表，09-12 非空 **215 部** → 09-14 **0 部**
        #   （215 那个数见 SUMMARY「`on (\S+) by` → `on (.+?) by`」一节）。
        #
        #   ★ 旧值要**逐个站名拆开**再并：老库里可能躺着 `"A|B"` 这种**合体标签**
        #   （旧 `sync_pack` 用 `"|".join` 造的）。读侧（`_sites()`）是按 JSON 数组
        #   **逐项**取的，不认里面的 `|` —— 直接并会把它当成一个假站名原样留着。
        old_matched_idx = {p for s in _u(row["matched_indexers"])
                           for p in str(s).split("|") if p}
        matched_indexers = old_matched_idx | {i for _, i in matched if i}

        # ★★★ 做种判定**两条路求并**（`e404b1ca#5`，见函数 docstring）：
        #   ① hash 路：`matched_hashes ∩ seeding_hashes` —— 精确，但**依赖
        #      cross-seed 写了 decision**，没写就判成"没做种"（这就是那个 30 倍的偏差）。
        #   ② name 路：qB 里有没有一个名叫 `dir_name` 的种子。★ 它是**独立事实**
        #      （cross-seed 建种时用的 searchee 名 = 单片目录名），不经过它的记账。
        #   ⇒ ①∪② 才是"这部片现在到底有没有在做种"。
        seeding_count = len(matched_hashes & seeding_hashes)
        if seeding_names and row["dir_name"] in seeding_names:
            # ★ 用 `max(..., 1)` 而不是 `+1`：同一部片在 qB 里可能有多个条目
            #   （多个站各一份），但这里问的是"**这部片**做种了没"，
            #   而 `pack_seeding_total` 数的是**行数**（stage=='SEEDING'）。
            #   两者口径必须一致 —— 否则"做种 N 部"与"总数 M 部"会互相打脸。
            seeding_count = max(seeding_count, 1)

        stage = compute_stage(
            seeding_count=seeding_count,
            matched_count=len(matched_hashes),
            searched_indexers=searched_indexers,
            skipped_indexers=skipped_indexers,
        )

        # attempts：只在"真的去搜过"时 +1（skip 不算，因为那次请求根本没发出去）
        attempts = row["attempts"]
        last_attempt = row["last_attempt_at"]
        new_searched = searched_indexers - old_searched
        if new_searched:
            attempts += 1
            last_attempt = _now()

        # indexer_seen：合并"老的"与"这次拿到的"，同一站取更晚的时间
        seen = dict(old_seen)
        for ix, ts in (searched_at or {}).items():
            if ts and (ix not in seen or ts > seen[ix]):
                seen[ix] = ts
        # 已搜过但没拿到时间的站，补一个"未知"（保证它不会被当成"从没搜过"）
        for ix in searched_indexers:
            seen.setdefault(ix, "")

        # next_retry_at：按**每站各自的周期**算下一次最早可搜的时刻
        if stage in DONE_STAGES:
            next_retry = None
        else:
            nd = next_due_at(seen, indexers_now,
                             cadence_days=cadence_days,
                             cadence_by_indexer=cadence_by_indexer)
            next_retry = nd.strftime("%Y-%m-%d %H:%M:%S") if nd else None

        self.con.execute(
            """UPDATE movie SET
                 stage=?, attempts=?, last_attempt_at=?, next_retry_at=?,
                 searched_indexers=?, indexer_seen=?, skipped_indexers=?,
                 matched_indexers=?, matched_hashes=?, seeding_count=?, updated_at=?
               WHERE id=?""",
            (stage, attempts, last_attempt, next_retry,
             _j(searched_indexers), _dumps(seen), _j(skipped_indexers), _j(matched_indexers),
             _j(matched_hashes), seeding_count, _now(), movie_id),
        )

        # 流水：只在"有新事实"时记，避免每次 sync 都刷屏
        if log_attempts:
            new_skipped = skipped_indexers - old_skipped
            new_hashes = matched_hashes - old_hashes
            if stage != old_stage:
                self.add_attempt(movie_id, kind="sync", result=stage.lower(),
                                 indexers=searched_indexers,
                                 skipped_indexers=skipped_indexers,
                                 note=f"{old_stage} → {stage}")
            elif new_skipped:
                self.add_attempt(movie_id, kind="skip", result="skipped",
                                 skipped_indexers=new_skipped,
                                 note="被索引器退避跳过，需重试")
            elif new_hashes:
                self.add_attempt(movie_id, kind="inject", result="matched",
                                 indexers=matched_indexers,
                                 note=f"新增 {len(new_hashes)} 个单种")
        self.con.commit()
        return stage

    def add_attempt(self, movie_id: int, *, kind: str, result: str,
                    indexers=None, skipped_indexers=None, note: str | None = None) -> None:
        self.con.execute(
            """INSERT INTO attempt(movie_id, at, kind, result, indexers, skipped_indexers, note)
               VALUES(?,?,?,?,?,?,?)""",
            (movie_id, _now(), kind, result,
             _j(indexers or []), _j(skipped_indexers or []), note),
        )
        self.con.commit()

    # -- 查询：待办 ---------------------------------------------------------- #
    def todo(self, pack: str, *, indexers_now: list[str] | None = None,
             include_cooldown: bool = False,
             cadence_days: int = DEFAULT_CADENCE_DAYS,
             cadence_by_indexer: dict[str, int] | None = None,
             now: datetime | None = None) -> list[sqlite3.Row]:
        """哪些片子**还需要搜**？

        规则（这就是"已完成的阶段就不重复"的落地）：
          - SEEDING / MATCHED  → 不搜（已拿到单种）
          - PENDING            → 搜
          - SKIPPED            → **立刻**搜（上次根本没发出去）
          - ERROR              → 搜
          - UNMATCHED          → 按**每站各自的周期**判断（默认见 DEFAULT_CADENCE_DAYS）
                                  —— 只要有一个站"从没搜过"或"够周期了"就该搜
        """
        now = now or datetime.now()
        out: list[sqlite3.Row] = []
        for row in self.movies(pack):
            st = row["stage"]
            if st in DONE_STAGES:
                continue
            if st in RETRY_NOW_STAGES or include_cooldown:
                out.append(row)
                continue
            if st in COOLDOWN_STAGES:
                due = due_indexers(
                    _d(row["indexer_seen"]), indexers_now or [],
                    cadence_days=cadence_days,
                    cadence_by_indexer=cadence_by_indexer,
                    now=now,
                )
                if due:
                    out.append(row)
        # 按"最该搜"排序（SKIPPED → ERROR → PENDING → UNMATCHED），同级按目录名稳定排序。
        # 分批时靠这个顺序，保证每批都优先吃掉被退避跳过 / 从没搜过的片子。
        out.sort(key=lambda r: (TODO_PRIORITY.get(r["stage"], 9), r["dir_name"]))
        return out

    def todo_detail(self, pack: str, *, indexers_now: list[str] | None = None,
                    include_cooldown: bool = False,
                    cadence_days: int = DEFAULT_CADENCE_DAYS,
                    cadence_by_indexer: dict[str, int] | None = None,
                    now: datetime | None = None) -> list[tuple[sqlite3.Row, list[str]]]:
        """和 todo() 一样，但额外返回"这部片子该在哪些站重搜"。"""
        now = now or datetime.now()
        rows = self.todo(pack, indexers_now=indexers_now, include_cooldown=include_cooldown,
                         cadence_days=cadence_days, cadence_by_indexer=cadence_by_indexer,
                         now=now)
        out = []
        for r in rows:
            if r["stage"] in DONE_STAGES:
                continue
            due = due_indexers(_d(r["indexer_seen"]), indexers_now or [],
                               cadence_days=cadence_days,
                               cadence_by_indexer=cadence_by_indexer, now=now)
            out.append((r, due))
        return out

    def todo_pooled(self, packs: list[str], *, indexers_now: list[str] | None = None,
                    include_cooldown: bool = False,
                    cadence_days: int = DEFAULT_CADENCE_DAYS,
                    cadence_by_indexer: dict[str, int] | None = None,
                    now: datetime | None = None,
                    limit: int | None = None) -> list[tuple[str, sqlite3.Row, list[str]]]:
        """★★ **跨包合池**（`e404b1ca#6` ①，用户 2026-09-20 拍：方案 a）。

        返回 `[(pack, row, due_indexers), …]`，**已按池序排好、已按 `limit` 截断**。

        **为什么要有它**（用户原话：「一批多少种子不能按包定，三包合起来取 N 个」）——
        原先批次来源是**按包轮换**（`once_round` 的 `(last_pack_idx+1) % len(packs)`），
        每次只跑一个包，于是：
          · `--limit 50` 是**每包** 50，三包名义上一轮吃 150
          · 一个小包（`mbf` 只有 4 部、且实测 `Found 0 torrents`）**也会占掉一整个批次**
          · 而欠账全在 `frds`（232 部 SKIPPED）里 ⇒ 轮到 dc/mbf 的那些批**纯属浪费额度**

        合池之后一批**横跨多包**，`--limit` 是**全局**的 —— 额度只花在**池序最前**的片子上，
        不管它属于哪个包。

        ★★ **按包分段记账**（用户 2026-09-20 拍）：返回值**带 `pack`**，
          调用方按包分段落账，`emit("batch")` 的 `pack=` **仍然是真包名** ⇒
          日报的「按包聚合」**口径一字不改**，账仍能对到包上。

        ★ 排序用的是**与 `todo()` 同一条** `TODO_PRIORITY`（`SKIPPED` → `ERROR`
          → `PENDING` → `UNMATCHED`），同级按**目录名**。这样"欠账永远优先"这条
          在合池之后仍然成立（实测：前 50 名精确落在 `frds` 的 232 部 SKIPPED 上）。
          ★ 平手键里**不带包名** —— 带上就等于"按包名排序"，那正是我们要摆脱的东西；
            靠 `dir_name` 是全局唯一的（同一农场里单片目录不重名）来保证稳定。
        """
        now = now or datetime.now()
        pooled: list[tuple[str, sqlite3.Row, list[str]]] = []
        for pk in packs:
            for row, due in self.todo_detail(
                    pk, indexers_now=indexers_now, include_cooldown=include_cooldown,
                    cadence_days=cadence_days, cadence_by_indexer=cadence_by_indexer,
                    now=now):
                pooled.append((pk, row, due))
        pooled.sort(key=lambda t: (TODO_PRIORITY.get(t[1]["stage"], 9),
                                   t[1]["dir_name"]))
        if limit:
            return pooled[:limit]
        return pooled

    def cadence_table(self, pack: str, *, indexers_now: list[str] | None = None,
                      cadence_days: int = DEFAULT_CADENCE_DAYS,
                      cadence_by_indexer: dict[str, int] | None = None) -> dict[str, dict]:
        """按站汇总：搜过几部 / 其中几部到周期了 / 下次最早可重搜的时刻。"""
        now = datetime.now()
        pool = list(indexers_now or [])
        stat: dict[str, dict] = {}
        for row in self.movies(pack):
            seen = _d(row["indexer_seen"])
            for ix in (pool or sorted(seen)):
                d = stat.setdefault(ix, {"searched": 0, "due": 0, "next": None})
                if ix in seen and seen[ix]:
                    d["searched"] += 1
                    nd = _parse_ts(seen[ix]) + timedelta(
                        days=cadence_for(ix, cadence_days=cadence_days,
                                         cadence_by_indexer=cadence_by_indexer))
                    if nd <= now:
                        d["due"] += 1
                    elif d["next"] is None or nd < d["next"]:
                        d["next"] = nd
                else:
                    d["due"] += 1
        return stat

    def trend(self, *, weeks: int = 4, pack: str | None = None,
              now: datetime | None = None) -> "TrendReport":
        """按 **(周, 站)** 汇总「新增做种」—— 换站决策要的是这个，不是全局一个数。

        ★ 数据**早就在库里**，不用另存一份（§16.3.1）：`attempt` 表本身就是事件流水。
        但**不能直接 count 当天的行** —— `result='seeding'` 是**状态**不是**增量**：
        同一部片今天已经是 SEEDING，明天的 sync **不会再写一行**，所以"当天有多少行"
        里既有新做种的、也有别的原因写下 seeding 的。
        正确口径是**按片取首次**：`MIN(at) GROUP BY movie_id`（下面那条 SQL 就是）。

        ★ 站点归属取自 `movie.matched_indexers`（哪几个站匹配到了这部片）——
        而**不是** `attempt.indexers`：后者的 `kind='inject'` 行写的是 `[]`
        （见 `sync_movie` 里那三个 add_attempt 调用），按它归属会全部落到"未记站点"。
        一部片同时匹配到两个站，就**两边各记一次**（都真的出了力）；
        但周合计只算一部（不然总和会超过实际部数）。

        只保留 `weeks` 周以内的数据 —— 更早的对"这个站最近值不值得留"没有信息量，
        留着只会让周对比表越拖越长。
        """
        now = now or datetime.now()
        cutoff = now - timedelta(weeks=weeks)
        # ★ `first_indexers` 那个子查询：`attempt.indexers` 里记着"这一行是哪个站引起的"。
        #   为什么需要它：`matched_indexers` 的来源是**解析 cross-seed 日志**，
        #   日志没覆盖到（或走的是 DB/qB 那条路）时它是空的 —— 实测 201 部里有 40 部
        #   是这样，`matched_hashes` 有值而 `matched_indexers` 是 `[]`。
        #   那 40 部的 seeding 行 `attempt.indexers` 却是准的（如 `["HDFans","NanyangPT"]`）。
        #   两个都是**记下来的事实**，不是猜的 —— 所以按"先 matched_indexers、
        #   退到首行 indexers"的顺序取，属于补全而不是编造。
        #   两边都没有才落到 `UNATTRIBUTED_SITE`（并且会被单独标出来）。
        sql = (
            "SELECT a.movie_id, MIN(a.at) AS first_at, m.pack, m.matched_indexers, "
            "       (SELECT a2.indexers FROM attempt a2 "
            "         WHERE a2.movie_id = a.movie_id AND a2.result = ? "
            "         ORDER BY a2.at, a2.id LIMIT 1) AS first_indexers "
            "  FROM attempt a JOIN movie m ON m.id = a.movie_id "
            " WHERE a.result = ?")
        params: list = [STAGE_SEEDING.lower(), STAGE_SEEDING.lower()]
        if pack:
            sql += " AND m.pack = ?"
            params.append(pack)
        sql += " GROUP BY a.movie_id"

        def _sites(*candidates: str | None) -> list[str]:
            for raw in candidates:
                got = [s for s in _u(raw) if s and s != UNKNOWN_INDEXER]
                if got:
                    return got
            return []

        rep = TrendReport()
        for _mid, first_at, _pk, matched, first_idx in self.con.execute(sql, params):
            dt = _parse_ts(first_at)
            if dt is None or dt < cutoff:
                continue
            ic = dt.isocalendar()
            wk = f"{ic[0]}-W{ic[1]:02d}"
            sites = _sites(matched, first_idx)
            if not sites:
                rep.unattributed += 1
                sites = [UNATTRIBUTED_SITE]
            for s in sites:
                rep.cells[(wk, s)] = rep.cells.get((wk, s), 0) + 1
            rep.week_totals[wk] = rep.week_totals.get(wk, 0) + 1
            rep.total += 1

        rep.weeks = sorted(rep.week_totals)
        rep.sites = sorted({s for (_w, s) in rep.cells})
        return rep

    # -- 真增量：本批**首次**转 SEEDING 的片数 ------------------------------- #
    def first_seeding_between(self, pack: str, since: str,
                              until: str | None = None) -> int:
        """`[since, until)` 窗内**首次**变 SEEDING 的片数（按片去重）。

        ★★ **为什么需要它**（2026-09-23）：批次 metrics 里那个 `newly_seeding`
           是**净额**（`当前 SEEDING 数 - 批次前 SEEDING 数`，见 `drive-loop.py`），
           而摘要把它印成「新增做种」⇒ 读起来像「今天新加了 N 部」。
           实测：`443` 连续两天一模一样（§26.50 三 已记录 `-443/+443` 那对），
           而同一封信里 `发出请求` 从 162 掉到 37 —— 两个读数**互相矛盾**，
           于是读者合理地质疑数据有错。**净额答不了「今天干了活没有」。**

        ★ 口径**与 `trend()` 逐字相同**（`MIN(at) GROUP BY movie_id`）：
          同一部片反复 sync **不会**被重复计数 —— 这是 `trend()` 早就定下的
          正确口径（`state.py` 里那段注释：「按**每片首次**变 SEEDING 算」）。
          ★ 本方法是那个口径的**时间窗切面**，不是新发明的算法。

        ★ 为什么**不能**直接 `COUNT(*) WHERE result='seeding' AND at BETWEEN …`：
          `result='seeding'` 是**状态**不是**增量**（同 `trend()` 的注释）——
          一部早就是 SEEDING 的片，下一批 sync 时**不会**再写一行，
          但别的路径（如 inject 之后的 sync）会写 ⇒ 那样数出来的既不是
          "新转正的"也不是"今天做的"，是个混合口径。

        ★ `since`/`until` 用 **ISO 19 字符**（`%Y-%m-%d %H:%M:%S`）——
          `attempt.at` 就是这个形状（`_now()`），定长 ⇒ **字典序比较即时间序**，
          可以直接在 SQL 里比，不必回 Python 解析。
        ★ `until=None` ⇒ 取**此刻**（`_now()`）。这样调用方**不必碰** `_now`
          这个私有名 —— 它是本模块的内部约定，跨模块去 import 会把它变成
          事实上的公开 API（`scripts/drive-loop.py` 只需要传 `since`）。

        ★★ 上界**闭区间**（`<=`），不是半开的（2026-09-23 实测改成这样）：
          `attempt.at` 只有**秒**精度，而 `sync_pack` 写完 `attempt` 之后
          我们**立刻**查询 —— 两者常落在**同一秒**。
          若上界取半开（`first_at < until`），那些"就在这一秒里转正"的片
          会被**静默漏掉**：数出来偏小，而读数看起来完全正常（`0` 不是
          "没有"，是"没数到"）。★ 这正是本项目 `ERR-AI-09` 那一族——
          装置本身给出的答案不可信，而症状是"平静"。
          ⇒ 用 `<=`：窗是 `[since, until]`，两端口径一致（都含）。
          ★ 代价是多算了"恰好等于 `until` 那一秒"的非本批行 —— 但 `since`
            那一端本来就含（`>=`），且 `until` 是本批**回灌刚写完**的时刻，
            这一秒里不可能混进别的批（同包不会并行跑）。
        """
        if until is None:
            until = _now()
        row = self.con.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT a.movie_id, MIN(a.at) AS first_at"
            "    FROM attempt a JOIN movie m ON m.id = a.movie_id"
            "   WHERE a.result = ? AND m.pack = ?"
            "   GROUP BY a.movie_id"
            "  HAVING first_at >= ? AND first_at <= ?)",
            (STAGE_SEEDING.lower(), pack, since, until),
        ).fetchone()
        return int(row[0]) if row else 0

    def summary(self, pack: str) -> dict[str, int]:
        rows = self.con.execute(
            "SELECT stage, COUNT(*) c FROM movie WHERE pack=? GROUP BY stage", (pack,)
        )
        d = {s: 0 for s in ALL_STAGES}
        for r in rows:
            d[r["stage"]] = r["c"]
        d["TOTAL"] = sum(d[s] for s in ALL_STAGES)
        return d


# --------------------------------------------------------------------------- #
# searchee 名 → 包内目录名
# --------------------------------------------------------------------------- #
def resolve_dir_name(name: str, dir_names: set[str]) -> str | None:
    """把 cross-seed 的 searchee.name 归到包内子目录。

    searchee 既可能是**目录**（`2001太空漫游.…MNHD-FRDS`），
    也可能是**目录里的文件**（`…MNHD-FRDS.mkv`），还可能是不带中文前缀的
    纯文件名（`V.For.Vendetta.2005….mkv`，此时目录名是 `V字仇杀队.V.For.Vendetta…`）。
    所以按 精确 → 去扩展名 → 前缀/包含 三级回退。
    """
    if not name:
        return None
    if name in dir_names:
        return name
    stem = os.path.splitext(name)[0]
    if stem in dir_names:
        return stem
    # 文件级 searchee：找一个"包住它"或"被它包住"的目录（取最长的那个，最具体）
    cands = [d for d in dir_names
             if d.startswith(stem) or stem.startswith(d) or stem in d or d in stem]
    if not cands:
        return None
    return max(cands, key=len)


def _resolve_searchee_to_pack(
    name: str,
    snap: CrossSeedSnapshot,
    roots: list[str],
    dirs: set[str],
    dpaths: dict[str, str],
    farm_root: str = "",
) -> tuple[str | None, str]:
    """把一个 cross-seed searchee 归到**当前包**的单片名。

    返回 `(单片名, 归属)`，`归属` ∈ {"in_pack", "other_pack", "unresolved"}：
      * "in_pack"     —— 路径落在当前包某个 dataDir 下，`单片名` 为归属的单片
      * "other_pack"  —— 路径在别的包下（或查不到路径），**静默跳过、不计数**
      * "unresolved"  —— 路径在当前包内、但归不到任何具体单片（真·对不上）
    区分这两种"不是本包"的情况，是为了 sync 汇报时**不把跨包 searchee 误报成
    当前包的对不上数**（FRDS/MBF 的片子不该算进 DC 的 unresolved）。

    `dpaths` 应当已经合并了农场的镜像键（见 `StateStore.farm_dir_paths`），
    这样 v3 农场路径也能归到单片。
    """
    p = _norm_path(snap.searchee_paths.get(name, ""))
    if p:
        for r in roots:
            r = _norm_path(r)
            if p == r or p.startswith(r + "/"):
                d = dir_name_of(p, roots, dpaths)
                return (d if d else None, "in_pack" if d else "unresolved")
        # v3：cross-seed 现在扫的是硬链接农场，报的是农场路径。农场是三个包
        # 共用的，所以这里**只认 dpaths 里的镜像键**（root 传空 → 不做事后
        # 兜底的单根猜测），命中不了就是别的包的，别乱认领。
        f = _norm_path(farm_root)
        if f and (p == f or p.startswith(f + "/")):
            d = dir_name_of(p, [], dpaths)
            return (d if d else None, "in_pack" if d else "other_pack")
        return (None, "other_pack")       # 路径在别的包下 → 不属于当前包
    # 查不到路径（老库 / 罕见）：退回纯名字匹配；匹配不上算 unresolved
    d = resolve_dir_name(name, dirs)
    return (d, "in_pack" if d else "unresolved")


# --------------------------------------------------------------------------- #
# 观测对账：a − b / b − c / 全场无人认领 —— ★ **判据只有这一份**
# --------------------------------------------------------------------------- #
# 为什么这些判据住在 state.py，而**不**在 scripts/audit-found-*.py 里
# ---------------------------------------------------------------
# 生产里要跑它们的是 `drive-loop.py`，而它跑在 **NAS 宿主机**上
# （`drive-loop-nas.sh`: `PY=/usr/bin/python3`，DSM 自带 3.8.15，**不是容器**）。
# 而那两个脚本在 `check-deploy-drift.py` 的 **LOCAL_ONLY** 里（注明"在 Windows
# 上跑"），`deploy.sh` 的 FILES 里根本没有它们 —— 也就是说 **NAS 上没有这两个
# 文件**；就算手工拷上去，它们写死的 `//iSunker-DS423/...` UNC 路径在 NAS
# 宿主机上也不存在（那边是 `/volume1/...`）。
#
# 本模块被部署**两次**（`orchestrator/state.py` 进构建上下文、`drive-loop/
# orchestrator/state.py` 进运行时 —— 见 deploy.sh FILES 里两处注释），
# 是两侧**唯一都能到达**的地方。于是判据放这边，两个脚本降级成薄壳：
# 手工随时能跑，但核的是**同一份实现**。
#
# ★ "抄一份"在这里的代价不是重复劳动，是**把被测对象复制成判据**：正则改了、
#   副本照旧报绿。见 `audit-found-lines.py` 头部的教训。
#
# ★★ 口径：**两个口径的名字必须跟着数一起走**
#   同一批 Found 行，按"几个包一起试"还是"一次一个包"去归置，得到的
#   `other_pack` 是两个完全不同的数（实测 0 vs 865/146/1011 —— 三包共用
#   一个 farm_root，见下）。历史上这里出过一次事：全量口径报出的
#   `other_pack = 0` 被当成"干净"，而生产口径下根本不是这个数。
#   这跟 `NanyangPT` 与 `NanyangPT (南洋)` 是同一个形状 —— **一个名字盖了
#   两种模型**。所以下面每个数都**绑定自己的口径名**，不出现裸的 `other_pack`。
#
# ★★★ 2026-10-02 拍板：**`a` 的口径名 = 「单行式（可回灌）已识别条数」**，
#   不是「搜出去总量」。为什么必须写死这个名字：
#   cross-seed 有**两种** `] Found ` 行 ——
#     ① **单行式**（本判据的靶心）：`Found <片名> [hash] on <站> by <判定> from dataDir (…) - …`
#        ⇒ 含站名，状态机可回灌「哪个站搜到了」；
#     ② **三行式**：`[webhook] Found N torrents for {`（本行 + `path: …` + `}`）
#        ⇒ **没有站名**，回灌用不上 ⇒ **刻意不进 a**。
#   若把它并进 a，则 `a−b`（b 只吃单行式）会**常年为正**、判据**恒红** —— 那是
#   把判据搞坏，不是修好。⇒ 正确处置是**显式报一条**（`other_found` 非空即告警，
#   见 `drive-loop.reconcile_watch`），而不是把它算进 a。
#   ★ 所以：**别拿 `a` 去解释「下一批会发几条 webhook」**（§B.10 第 14 条：
#     读数一样、含义相反）。
#
# 目标消息形状，用**独立字面量**描述（无捕获组、无量词、无锚点）。
# ★ 每个字面量**单独报数** —— "是哪个词把行数砍下来的"一眼可见。
# ★ 第一版的教训：拿 `] Found ` 当基线 → 数到 2200，a − b 报了 1189 的**假差**。
#   因为那个字面量同时吃到 `[webhook] Found 0 torrents for {`（662 行）与
#   `[webhook] Found N torrent file(s) to inject`（527 行）。
#   基线**既不能带锚点**（那等于把被测正则抄一遍 = 循环论证），**也不能太松**。
#
# ★ 基线与正则会差**恰好一个词**：这六个字面量**没有**钉住 `[webhook]|[inject]`
#   那个标签，而 `_RE_FOUND` 钉了 —— 所以基线是正则的**超集**。
#   实测（2026-09-12）a == b == 1011，没有别的标签的 Found 行；但那是**实测**，
#   不是**结构保证**。`tests/test_reconcile.py` 里有一格专门钉这个差
#   （拿 `[search] Found …` 造一条），好让"恰好相等"一直有人看着。
FOUND_LITS = (
    ("L1 `] Found `",        "] Found "),
    ("L2 `[8hex...]`",       None),          # None = 用 _HEX8 数
    ("L3 `] on `",           "] on "),
    ("L4 ` by `",            " by "),
    ("L5 ` from dataDir (`", " from dataDir ("),
    ("L6 ` - `",             " - "),
)
L1_LABEL = FOUND_LITS[0][0]
_HEX8 = re.compile(r"\[[0-9a-f]{8}\.\.\.\]")
_LOG_LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+ \w+: (?P<msg>.*)$")


@dataclass
class FoundLineCount:
    """a − b：a 是**形状**（六个字面量的合取）**= 「单行式（可回灌）已识别条数」**，
    b 是**生产正则** `_RE_FOUND` 命中数。

    ★ 口径（2026-10-02 拍板）：a **不是**「搜出去总量」—— 它只数**单行式**
      （含站名、可回灌）。三行式 `Found N torrents for {`（无站名）刻意不进 a，
      由 `other_found` 单独承载并**报警**（见 `drive-loop.reconcile_watch`）。
    """
    total_lines: int = 0
    a: int = 0
    b: int = 0
    lit_counts: dict = field(default_factory=dict)
    other_found: dict = field(default_factory=dict)   # 被 L1 吃到、但不是目标形状（含三行式）
    unparsed: list = field(default_factory=list)      # 目标形状、`_RE_FOUND` 却没吃下
    group4: dict = field(default_factory=dict)
    group3_odd: int = 0

    @property
    def delta(self) -> int:
        return self.a - self.b

    @property
    def controls_ok(self) -> bool:
        """两道正向控制：读到行 + 基线数到了。

        ★ **刻意不含 `b > 0`** —— `b == 0` 是「空转」（一条 Found 都没有），
          它要报的是一条**告警**，不是"控制没过"。混在一起就分不清
          「判据坏了」和「真的没发生」（§18.17.3）。
        """
        return self.total_lines > 0 and self.lit_counts.get(L1_LABEL, 0) > 0


def count_found_lines(text: str) -> FoundLineCount:
    """日志全文 → a/b 对账。`scripts/diag/audit-found-lines.py` 核的就是这一个函数。"""
    r = FoundLineCount()
    for raw in text.splitlines():
        r.total_lines += 1
        if FOUND_LITS[0][1] not in raw:
            continue
        hit = {k: (v in raw if v else bool(_HEX8.search(raw))) for k, v in FOUND_LITS}
        for k, ok in hit.items():
            if ok:
                r.lit_counts[k] = r.lit_counts.get(k, 0) + 1
        if all(hit.values()):
            r.a += 1
            m = _LOG_LINE.match(raw)
            msg = m.group("msg") if m else raw
            mm = _RE_FOUND.search(msg)
            if mm:
                r.b += 1
                r.group4[mm.group(4)] = r.group4.get(mm.group(4), 0) + 1
                if re.search(r"[ ()(]", mm.group(3)):
                    r.group3_odd += 1
            elif len(r.unparsed) < 8:
                r.unparsed.append(raw)
        else:
            head = raw.split("] Found ", 1)[1][:60] if "] Found " in raw else raw[:60]
            k = re.sub(r"\d+", "N", head.split("{")[0].strip())[:48]
            r.other_found[k] = r.other_found.get(k, 0) + 1
    return r


@dataclass
class PackCtx:
    """一个包的归属判据三件套。★ 与 `sync_pack` 里那四行**同源** ——
    口径只在这里拼一次，生产与全量两个口径都从它派生。"""
    roots: list = field(default_factory=list)
    dirs: set = field(default_factory=set)
    dpaths: dict = field(default_factory=dict)
    farm: str = ""


def pack_contexts(store) -> dict:
    """{包名: PackCtx}，一次取齐。

    ★ 为什么不放在 `sync_pack` 里算：它是**一包一调**的，一轮要算三次、
    发三条通知。观测收尾（`after_batch_reports`）才是"一轮一次"的地方。
    """
    out: dict = {}
    for p in store.packs():
        pk = p["name"]
        dpaths = dict(store.dir_paths(pk))
        dpaths.update(store.farm_dir_paths(pk))     # v3：农场镜像键并进来
        out[pk] = PackCtx(roots=store.roots(pk),
                          dirs={r["dir_name"] for r in store.movies(pk)},
                          dpaths=dpaths, farm=store.farm_root(pk))
    return out


@dataclass
class FoundResolve:
    """b − c，**两种口径各一份**。字段名自带口径，不出现裸的 `other_pack`。"""
    b: int = 0
    c: int = 0                                  # 真归到某个单片的行数
    belong_all: dict = field(default_factory=dict)   # 全量口径（所有包一起试、命中即停）
    belong_prod: dict = field(default_factory=dict)  # 〔生产口径〕逐包：{包名: {归属: 数}}
    farm_lines: int = 0
    orig_lines: int = 0
    samples: dict = field(default_factory=dict)      # 非 in_pack 的样本（带路径）
    paths: list = field(default_factory=list)

    @property
    def delta(self) -> int:
        """全量口径的 b − c。★ 这个数**不能**解释成"生产里没有静默丢行" ——
        生产口径见 `belong_prod`（同一批行、另一个数）。"""
        return self.b - self.c


def resolve_found_lines(text: str, store) -> FoundResolve:
    """Found 行的组5（= searchee 路径）走**生产代码本身**能不能归到单片。

    ★ 组5 是**路径**、组1 是 searchee **名**（实测确认）。第一版把整条路径当
      名字传进去 → `.get()` 永远取到 "" → 静默退化成"只按名字猜"的兜底分支，
      报出来的数看着合理、全不算数。别重犯。

    ★★ 两种口径都算，这是本函数存在的理由：
      · **全量口径**（`belong_all`）：所有包一起试、命中即停。它回答的是
        「这条 searchee 是不是**某个**包的」。`other_pack` 在这个口径下
        按构造就接近 0 —— 别把它念成"干净"。
      · **生产口径**（`belong_prod`）：一次只给一个包 —— `drive-loop.py` 的
        `S.sync_pack(st, pack, crossseed_db=<同一个库>)` 就是这么调的。
        三包**共用一个 farm_root**，所以对任一包来说，别的包的 searchee
        天然就是 `other_pack`；实测 dc-collection 865 / frds 146 / mbf 1011。
        → 在这个口径下 `other_pack` 恒非零、大体恒定，**它没有信息量**。
          要看"真丢了什么"，判据是 `unclaimed_searchees()`，不是这个数。
    """
    ctx = pack_contexts(store) if hasattr(store, "packs") else {}
    r = FoundResolve()
    for pk in ctx:
        r.belong_prod[pk] = {}

    for raw in text.splitlines():
        if "] Found " not in raw:
            continue
        m = _LOG_LINE.match(raw)
        if not m:
            continue
        mm = _RE_FOUND.search(m.group("msg"))
        if not mm:
            continue
        r.b += 1
        p = _norm_path(mm.group(5))
        r.paths.append(p)
        is_farm = any(c.farm and p.startswith(_norm_path(c.farm) + "/")
                      for c in ctx.values())
        r.farm_lines += 1 if is_farm else 0
        r.orig_lines += 0 if is_farm else 1

        # ── 全量口径：所有包一起试，命中即停 ──
        hit = None
        for pk, c in ctx.items():
            got, belong = _resolve_one(p, c)
            if got:
                hit = (pk, got, belong)
                break
        key = hit[2] if hit else "other_pack"
        if hit:
            r.c += 1
        r.belong_all[key] = r.belong_all.get(key, 0) + 1
        if key != "in_pack" and len(r.samples.get(key, [])) < 3:
            r.samples.setdefault(key, []).append(p[:120])

        # ── 生产口径：一次一个包（sync_pack 的调法）──
        for pk, c in ctx.items():
            _, belong = _resolve_one(p, c)
            d = r.belong_prod[pk]
            d[belong] = d.get(belong, 0) + 1
    return r


def unclaimed_searchees(store, snap) -> list:
    """★ 全场无人认领：库里每条 searchee，**三个包合起来都认不出**的。

    返回 `[(searchee 名, 规范化路径)]`，按路径排序。

    为什么这是对的判据（而不是给 `sync_pack` 加个 `other_pack` 计数器）
    ------------------------------------------------------------------
    三包共用一个 `farm_root`，所以**生产口径下的 `other_pack` 恒非零**
    （实测 865 / 146 / 1011）—— 加出来就是一个人人会无视的常数。
    真正想知道的只有一件事：**有没有哪条 searchee，哪个包都不认它**。
    这个数才恒为 0，且非零时**每一条都指得出名字**。

    ★ 期望值可指回判据之外的真实记录：2026-09-12 实测 1888 条里**恰好 1 条**
      无人认领 —— `reseed/reseed_farm/0观影清单chrlee整理`（农场里只含一个
      `.xlsx` 清单文件、三包都不认、`searched`/`decisions` 里都没有它）。
      报不出这一条，就说明这个判据又自说自话了。

    ★ 它**不是** `other_pack` 的改名：`other_pack` 说的是"这条不归我"，
      这里是"这条不归任何人"。
    """
    ctx = pack_contexts(store)
    out: list = []
    for name, path in snap.searchee_paths.items():
        p = _norm_path(path)
        for c in ctx.values():
            got, _belong = _resolve_searchee_to_pack(
                name, _OneSnap({name: path}), c.roots, c.dirs, c.dpaths, c.farm)
            if got:
                break
        else:
            out.append((name, p or "(无路径)"))
    out.sort(key=lambda kv: kv[1])
    return out


def _basename(path: str) -> str:
    """路径 → 末段。`searchee_paths` 的**键是名**、值是路径（`snap_for()` 的坑）。"""
    return path.rstrip("/").rsplit("/", 1)[-1]


@dataclass
class _OneSnap:
    """只含一条 searchee 的快照 —— 形状与 `read_crossseed_db()` 的返回一致。

    ★ 必须是**带属性**的东西，不能是裸 dict：`_resolve_searchee_to_pack` 走的是
      `snap.searchee_paths.get(name, "")`。
    """
    searchee_paths: dict


def _resolve_one(path: str, c: PackCtx) -> tuple:
    """拿**库自己那一套** roots/dirs/dpaths/farm 去归一条路径。"""
    b = _basename(path)
    return _resolve_searchee_to_pack(b, _OneSnap({b: path}),
                                     c.roots, c.dirs, c.dpaths, c.farm)


# --------------------------------------------------------------------------- #
# 索引器标签归一
# --------------------------------------------------------------------------- #
_INDEXER_URL_ID = re.compile(r"/(\d+)/api(?:/|$)")


def normalize_indexer(token: str, alias: dict[str, str] | None = None) -> str:
    """把索引器的各种写法归一成同一个标签。

    日志里同一个站可能出现三种写法：`SiteA`（名字）、
    `http://prowlarr:9696/2/api`（URL，名字还没解析出来时）、
    以及 info 级日志里的空值。不归一的话，同一个站在集合里会变成三个不同成员，
    "已搜过这个站吗"就永远算不对。
    """
    t = (token or "").strip().rstrip("/")
    if not t:
        return UNKNOWN_INDEXER
    alias = alias or {}
    if t in alias:
        return alias[t]
    m = _INDEXER_URL_ID.search(t)
    if m:
        return f"prowlarr#{m.group(1)}"
    return t


def norm_indexer_name(n: str) -> str:
    """取站名主干：`'NanyangPT (南洋)'` → `'nanyangpt'`。

    cross-seed 里的名字来自站点 caps，常带括号后缀；`--indexers` 是人手写的短名。
    直接比集合会每次都误报，所以去掉括号后缀与大小写再比。

    ★ 实现**只有这一份**（原先在 `scripts/drive-loop.py`，2026-09-16 挪过来）：
      `check_indexers()` 的「`--indexers` 与库里站名对账」和 `DriveSession` 的
      「是不是**所有**站都在退避」必须用同一把尺，否则两处会各判各的。
    """
    # ★ maxsplit 必须写成关键字：Python 3.13 起按位置传会发 DeprecationWarning
    #   （re.split(pattern, string, maxsplit) 里 maxsplit 是 keyword-only 的语义）。
    return re.split(r"[(（]", n.strip(), maxsplit=1)[0].strip().lower()


def _norm_set(tokens, alias: dict[str, str]) -> set[str]:
    return {normalize_indexer(t, alias) for t in tokens if (t or "").strip()}


def _clean(tokens: set[str]) -> set[str]:
    """若同一件事既有具名索引器、又有 `(未记录)` 占位（info 与 verbose 各记了一行），
    丢掉占位 —— 它只是"当时还不知道是谁"。"""
    if len(tokens) > 1 and UNKNOWN_INDEXER in tokens:
        tokens = set(tokens)
        tokens.discard(UNKNOWN_INDEXER)
    return tokens


# --------------------------------------------------------------------------- #
# 把三个来源合并成一次同步
# --------------------------------------------------------------------------- #
@dataclass
class SyncReport:
    pack: str
    movies: int = 0
    from_db: int = 0          # cross-seed.db 说"搜过"的片子数
    from_log: int = 0         # 日志里出现 Searching for 的片子数
    skipped: int = 0          # ★日志里被退避跳过的片子数
    matched: int = 0
    seeding: int = 0
    indexers: list[str] = field(default_factory=list)
    stage_counts: dict[str, int] = field(default_factory=dict)
    unresolved: int = 0       # searchee 名对不上任何目录的条数

    def render(self) -> str:
        lines = [
            f"包 {self.pack}：{self.movies} 部",
            f"  cross-seed.db 认为搜过 : {self.from_db}",
            f"  日志里出现 Searching   : {self.from_log}",
            f"  日志里被退避跳过       : {self.skipped}  ← 这些是「假完成」，必须重试",
            f"  匹配到单种             : {self.matched}",
            f"  已在 qB 里             : {self.seeding}",
            f"  当前索引器             : {', '.join(self.indexers) or '(未知)'}",
        ]
        if self.unresolved:
            lines.append(f"  ⚠ 有 {self.unresolved} 个 searchee 名对不上目录（已跳过）")
        lines.append("  阶段分布: " + "  ".join(
            f"{k}={self.stage_counts.get(k, 0)}" for k in ALL_STAGES))
        return "\n".join(lines)


def sync_pack(
    store: StateStore,
    pack_name: str,
    *,
    crossseed_db: str | Path | None = None,
    log_paths: list[str | Path] | None = None,
    log_texts: list[str] | None = None,
    qbit_torrents: list[dict] | None = None,
    indexers_override: list[str] | None = None,
    indexer_alias: dict[str, str] | None = None,
    cadence_days: int = DEFAULT_CADENCE_DAYS,
    cadence_by_indexer: dict[str, int] | None = None,
    cooldown_days: int | None = None,
    log_attempts: bool = True,
) -> SyncReport:
    """把 cross-seed.db + 日志 + qB 三个来源合并进状态库。

    三者分工：
      * cross-seed.db → "搜过没有 + 什么时候搜的"（timestamp）+"匹配到什么"（decision）
      * 日志          → ★"被退避跳过"（cross-seed 不记这个）+ 是哪个索引器
      * qB            → "有没有真的在做种"（按 info_hash 与 decision 求交）
    """
    if cooldown_days is not None:            # 旧参数名，兼容
        cadence_days = cooldown_days
    p = store.pack(pack_name)
    if p is None:
        raise KeyError(f"包未登记: {pack_name!r}（先跑 init）")
    roots = store.roots(pack_name)          # 多根包会返回全部根

    dirs = {r["dir_name"] for r in store.movies(pack_name)}
    # 路径 → 单片名：给日志解析做最长前缀匹配。多根/嵌套包靠它才能把
    # "季层"路径和"容器层"路径分开（见 dir_name_of 的说明）。
    dpaths = store.dir_paths(pack_name)
    # v3：cross-seed 扫的是硬链接农场，报的是农场路径 —— 把镜像键并进来，
    # 同一个 dir_name 现在有两把钥匙（原路径 + 农场路径）。dir_name_of 与
    # parse_log 不必改动。
    farm = store.farm_root(pack_name)
    dpaths.update(store.farm_dir_paths(pack_name))
    rep = SyncReport(pack=pack_name, movies=len(dirs))

    # --- 1) cross-seed.db ------------------------------------------------- #
    db_searched: dict[str, dict[str, str]] = {}     # dir -> {索引器: 时间}
    db_matched: dict[str, set[str]] = {}
    alias: dict[str, str] = dict(indexer_alias or {})
    indexers_now: list[str] = []
    if crossseed_db:
        snap = read_crossseed_db(crossseed_db)
        alias = {**snap.alias, **alias}      # 用户显式给的优先
        indexers_now = [_norm_one(i, alias) for i in (indexers_override or snap.indexers)]
        for name, idxmap in snap.searched.items():
            d, belong = _resolve_searchee_to_pack(name, snap, roots, dirs, dpaths, farm)
            if d is None:
                if belong == "unresolved":
                    rep.unresolved += 1
                continue                      # other_pack：静默跳过，不计数
            tgt = db_searched.setdefault(d, {})
            for ix, ts in idxmap.items():
                label = normalize_indexer(ix, alias)
                if ts and (label not in tgt or ts > tgt[label]):
                    tgt[label] = ts
        for name, decs in snap.decisions.items():
            d, belong = _resolve_searchee_to_pack(name, snap, roots, dirs, dpaths, farm)
            if d is None:
                continue
            for info_hash, _decision in decs:
                if info_hash:
                    db_matched.setdefault(d, set()).add(info_hash)
    else:
        indexers_now = [_norm_one(i, alias) for i in (indexers_override or [])]

    # --- 2) 日志 ----------------------------------------------------------- #
    facts = LogFacts()
    texts = list(log_texts or [])
    for lp in (log_paths or []):
        try:
            texts.append(Path(lp).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    for t in texts:
        f = parse_log(t, roots, dpaths)
        for d, idxs in f.searched.items():
            facts.searched.setdefault(d, set()).update(idxs)
        for d, idxs in f.skipped.items():
            facts.skipped.setdefault(d, set()).update(idxs)
        for d, lst in f.found.items():
            facts.found.setdefault(d, []).extend(lst)
        for d, ts in f.searched_at.items():
            if ts > facts.searched_at.get(d, ""):
                facts.searched_at[d] = ts
        facts.snoozes.extend(f.snoozes)
        facts.searches_seen += f.searches_seen
        facts.skips_seen += f.skips_seen

    # --- 3) qB ------------------------------------------------------------- #
    #
    # ★★★ 2026-09-20（`e404b1ca#5`）：**做种判据与"匹配结果"解绑**。
    #
    #   原先只有 hash 一条路：`seeding_count = len(matched_hashes & seeding_hashes)`
    #   —— 而 `matched_hashes` 的唯一来源是 **cross-seed 的 `decision` 表**。
    #   于是「cross-seed 这次没给它写 decision」⇒ 做种被判成**没做种**。
    #
    #   实测代价（用户凭常识抓出来的，见 SUMMARY §26.33）：qB :3060 里 **2141 条
    #   做种 / 544 部唯一片**，而 `state.db` 说 `frds-top250-2024` 只做种 **13** 部
    #   —— 真实是 **422/486**。**低了约 30 倍**。后果是双向静默的：
    #     · 已在做种的片子被判 `UNMATCHED` ⇒ **永不停止重搜**（额度白烧）
    #     · `DONE_STAGES` 那条"搜到了就不重搜"的规矩形同虚设（输入是错的）
    #
    #   ★ 为什么 `name` 这条路是对的、且**不是**在放宽判据：
    #     qB 里那个种子的 `name` **就是** cross-seed 建种时用的 searchee 名，
    #     而 searchee 名与单片 `dir_name` 同名（cross-seed 枚举规则：
    #     dataDir 的直接子项 = searchee）。所以「qB 里有个名叫 <dir_name> 的种子」
    #     是**独立于 cross-seed 记账的一条事实**，不是它的派生。
    #   ★ 归属实测（2026-09-20，frds 486 部）：`name` 精确命中 **420**、
    #     `name` 去扩展名再命中 **9**（`.mkv` 那种）、两者并集 **422** ——
    #     与 qB 真值逐条吻合。`save_path` 末段命中 **0**（qB 的 save_path 是
    #     按 tracker 分的 `<linkDir>/<站名>/`，**不含单片名**）⇒ 所以不走它。
    #
    #   ★ hash 那条**保留**：`decision` 有值而且 hash 对得上时它更精确
    #     （能区分同名不同版）。两条求并，任一条成立即算"在做种"。
    seeding_hashes: set[str] = set()
    #: qB 里所有种子的 `name`（含去扩展名的形态）—— 与 `dir_name` 对齐用
    seeding_names: set[str] = set()
    for t in (qbit_torrents or []):
        h = (t.get("hash") or "").lower()
        if h:
            seeding_hashes.add(h)
        n = (t.get("name") or "").strip()
        if n:
            seeding_names.add(n)
            # ★ 同一份数据在 qB 里可能是**文件**（`X.mkv`）而不是目录（`X`）——
            #   实测 frds 有 9 部是这种形状。只剥**最后一个**扩展名，不做
            #   递归剥离（`S01.mkv.mkv` 这种不存在，而递归剥会把
            #   `Avengers.Endgame.2019.BluRay` 里的 `.2019` 当成扩展名）。
            stem = n.rsplit(".", 1)[0] if "." in n else ""
            if stem:
                seeding_names.add(stem)

    rep.indexers = indexers_now
    changed: dict[str, int] = {}

    for row in store.movies(pack_name):
        d = row["dir_name"]
        seen: dict[str, str] = dict(db_searched.get(d, {}))
        log_idx = _clean(_norm_set(facts.searched.get(d, set()), alias))
        searched = _clean(set(seen) | log_idx)
        # 日志说有搜过、但 db 里查不到时间（db 被轮换过）→ 用日志时间兜底，
        # 否则会被当成"从没搜过"而立刻重搜
        if not seen and d in facts.searched_at:
            seen = {UNKNOWN_INDEXER: facts.searched_at[d]}
            searched = _clean(searched | {UNKNOWN_INDEXER})
        skipped = _clean(_norm_set(facts.skipped.get(d, set()), alias))
        # 被跳过的那次**不算搜过**：若某索引器既没在 timestamp 里、也没真搜过，就只是 skipped
        skipped = skipped - searched
        hashes = set(db_matched.get(d, set()))
        found_idx = {i for _, i in facts.found.get(d, [])}
        # ★ 每 (hash, 站) 一个二元组 —— **不再把多个站用 `"|"` 合成一个标签**。
        #   合体标签有两处坏处：
        #     ① `matched_indexers` 里会躺着一个叫 `"A|B"` 的**假站名**，而读侧
        #        （`_sites()`）按 JSON 数组逐项取，不认里面的 `|`；
        #     ② 一旦要「与旧值取并集」（见 `sync_movie`），并集在合体元素上
        #        **根本没有定义** —— `{"A|B"} ∪ {"A"}` 该是几个？
        #   所以**在源头**就按站拆开，让下游拿到的就是集合意义上的并集。
        #   `found_idx` 为空时退回一个空标签，`sync_movie` 那侧会把它滤掉
        #   （`if i`）—— 那正是"这次没解析到 Found 行"的正常情形。
        matched = [(h, ix) for h in hashes for ix in (sorted(found_idx) or [""])]

        new_stage = store.sync_movie(
            row["id"],
            searched_indexers=searched,
            skipped_indexers=skipped,
            matched=matched,
            seeding_hashes=seeding_hashes,
            seeding_names=seeding_names,
            indexers_now=indexers_now,
            searched_at=seen,
            cadence_days=cadence_days,
            cadence_by_indexer=cadence_by_indexer,
            log_attempts=log_attempts,
        )
        changed[new_stage] = changed.get(new_stage, 0) + 1

    rep.from_db = len(db_searched)
    rep.from_log = len(facts.searched)
    rep.skipped = len([d for d in facts.skipped if d not in facts.searched and d not in db_searched])
    rep.matched = len(db_matched)
    # ★ 计数只有一份：见 pack_seeding_total（日报的 pack_progress 调的是同一个）
    rep.seeding, _ = pack_seeding_total(store, pack_name)
    rep.stage_counts = changed
    store.mark_scan(pack_name, finished=True, complete=True)
    return rep


def _norm_one(token: str, alias: dict[str, str]) -> str:
    return normalize_indexer(token, alias)


# --------------------------------------------------------------------------- #
# 各包进度（日报用）—— ★ 计数**只有一份**，见 pack_seeding_total 的说明
# --------------------------------------------------------------------------- #

def pack_seeding_total(store: StateStore, pack: str) -> tuple[int, int]:
    """`(seeding, total)` —— **唯一**那份「做种 / 总数」计数。

    ★★ 为什么抽出来：`sync_pack` 末尾已经算过同一个表达式（`rep.seeding = ...`），
       而日报的新节也要同一个数。本项目规矩是「一份判据只维护一处」——
       两处各写一遍 `stage == STAGE_SEEDING`，改了其中一处就会**静默分叉**
       （两个数都"看着对"，却对不上）。所以两处都调这里。

    ★ `total` 是 `movie` 表的**行数**，即「**声明过**的单片」累计 ——
      而 `register_dirs` 是 `ON CONFLICT(pack, dir_name) DO UPDATE`，
      **从不删行**（`state.py` 里那条 INSERT 的原话）⇒ 源目录删过、而 `init`
      没重跑时，这个数**必然大于** `reseed-state.py init --dry-run` 报的
      「识别到 N 个单片」。**这不是 bug，是定义**；要与 dry-run 比，
      前提是**先重跑一次 init**。
    """
    rows = store.movies(pack)
    return (sum(1 for r in rows if r["stage"] == STAGE_SEEDING), len(rows))


# --------------------------------------------------------------------------- #
# ② 口径「待办完成率」—— 分母的定义**只在这一处**（`A.11`）
# --------------------------------------------------------------------------- #
#: ★★ 「待办完成率」的**分母** = 除 `UNMATCHED` 之外的**全部**阶段。
#:
#:   公式：  分子 = `SEEDING`
#:           分母 = `SEEDING + PENDING + SKIPPED + ERROR + MATCHED`
#:           完成度 = round(100 × 分子 ÷ 分母)        （分母 > 0）
#:                  = `n/a`                          （分母 == 0）
#:
#:   等价写法：分母 == `summary()["TOTAL"] − summary()[UNMATCHED]`
#:   （`summary()["TOTAL"]` 是 `movie` 表的**行数**；`pack_seeding_total` 的
#:   docstring 解释了为什么这个数"只涨不缩"）。**两条式子必须恒等** ——
#:   测试里有一条断言直接把两者比一遍（④a），因为一旦有人改了 `ALL_STAGES`，
#:   这里和 `summary()` 会**各漂一半而对不上**。
#:
#:   ★★ **为什么排除 `UNMATCHED`**：它是**回收态，不是终态**。
#:   `todo()` 只排除 `DONE_STAGES`（`SEEDING`/`MATCHED`），所以 `UNMATCHED`
#:   会在**任一站点到期**（默认 14 天）或**新增索引器**时重新入队。
#:   ⇒ 这带来一个**必须告知用户的指标性质**：
#:     `TOTAL` 永不缩，但 **`TOTAL − UNMATCHED` 会缩也会涨** ⇒
#:     **本口径是非单调的**，百分比**可能回落**。
#:     最常见的触发是**加站**（一批片子转 `PENDING`/`UNMATCHED`）。
#:   ★ 正文里那句「回落不代表做种丢了」就是在堵这个误读；**别删它**。
#:
#:   ★★ **绝不能把 `STAGE_SEARCHED` 写进这个元组**：它与 `STAGE_UNMATCHED`
#:   **语义相同、值不同**（`"SEARCHED"` vs `"UNMATCHED"`）。
#:   `summary()` 的 `d` 是按 `ALL_STAGES`（含 `UNMATCHED`、**不含** `SEARCHED`）
#:   建的 ⇒ 把 `SEARCHED` 放进来会让 `d[STAGE_SEARCHED]` **永远是 0**，
#:   而 `d[STAGE_UNMATCHED]` 里的数**被漏减** ⇒ 分母偏大、百分比偏小，
#:   而且**不报任何错**。`STAGE_SEARCHED` 只该出现在 `ALL_STAGES` 之外
#:   那几处"阅读友好"的别名说明里。
#:
#:   ★ 用**显式减法**（而不是手写一份阶段列表）：将来 `ALL_STAGES` 新增阶段时，
#:   它会**自动进分母**，而不是被静默漏掉 —— 漏一个阶段 = 一个永远偏大的
#:   百分比，而那个误差**没有任何东西会报出来**。
CENSUS_DENOM_STAGES = tuple(s for s in ALL_STAGES if s != STAGE_UNMATCHED)

#: ★★ 两个池（`e404b1ca#6` ②，用户 2026-09-20 拍：**欠账优先、剩余名额给常态**）。
#:
#: **为什么分池**：这两类"待搜"的**性质不同**，混在一起读不出东西：
#:
#:   · **欠账池**（`SKIPPED` / `ERROR` / `PENDING`）—— 「**还没搜过**」的欠账。
#:     `SKIPPED` = 上次被站点退避秒跳（`attempts` **不涨**，因为请求根本没发出去）、
#:     `PENDING` = 从没搜过。★ 实测 frds 232 部 `SKIPPED` **全部 `attempts=0`**。
#:     ⇒ 它们是**纯欠账**：清一部少一部，**必须在有限轮内清零**。
#:   · **常态池**（`UNMATCHED`）—— 「**搜过、没命中**」。这是**稳态**，
#:     只要片子还在、站上还没出单种，它就会一直在 ⇒ **永远清不完**，
#:     清不完不是故障。它的正常节奏是**按周期重搜**，不是"赶紧消灭"。
#:
#: ★ 混在一个数里会**同时误导两头**：报"565 部待搜"让人以为欠了 565 部的债
#:   （其实稳态 272 部本来就会一直在）；只报欠账又会让人以为"没事了"，
#:   而稳态池其实一直在按周期消耗额度。
#:
#: ★ 取件策略（用户拍）：**欠账池优先，欠账池不足一批时才用常态池补满** ——
#:   即 `TODO_PRIORITY` 那条全局排序**本来就实现的东西**。分池**不改取件顺序**，
#:   只是把它**变得可读**（日报能分开报两个数），所以**没有第二位排序键**。
#:   ★ 为什么不给常态池保底名额（比如 20%）：那会让欠账清完从 5 批变 6 批，
#:     而欠账是**有限**的（232 部 ≈ 5 批就清零），熬过去就没了；
#:     保底则**永远**在常态池上花掉 20% 额度 —— 用**无限的**代价换**5 批**的平滑，
#:     不值。★ 这个取舍要写在这里，否则下次有人会"顺手"加个保底。
DEBT_STAGES = frozenset({STAGE_SKIPPED, STAGE_ERROR, STAGE_PENDING})
STEADY_STAGES = frozenset({STAGE_UNMATCHED})


def pool_of(stage: str) -> str:
    """某个 stage 属于哪个池 —— `"debt"` / `"steady"` / `"done"`。

    ★ `DONE_STAGES` 的片子**既不在欠账也不在常态**（它们不该被搜）⇒ 单独一档，
      **不并进任何池**：把它们算进"待搜"就是 `§26.33` 那个缺陷的形状
      （把"正在做种的"当成"待搜"）。
    """
    if stage in DEBT_STAGES:
        return "debt"
    if stage in STEADY_STAGES:
        return "steady"
    return "done"



def pack_stage_census(store: StateStore, pack: str) -> dict[str, int]:
    """每包的逐阶段普查 + ② 口径的 `numerator` / `denom`。

    ★ **复用 `store.summary(pack)`**，不另写一份 `GROUP BY` —— 那一份已经是
      本仓唯一的逐阶段普查（`idx_movie_stage` 撑着），再写一份就是
      「两个声明点迟早漂开」的典型形状。★ 开销还是**负的**：`pack_progress`
      原先每包调 `pack_seeding_total` → `store.movies(pack)`（**全行扫**），
      而这是一条**索引** `GROUP BY`。

    ★ 返回的 dict **不只有阶段键**：`summary()` 里还有一个 `"TOTAL"`，
      本函数又加 `"denom"` / `"numerator"` ⇒ 调用方别做「键都是阶段」的假设。

    ★★ `denom == 0` 有**两种**成因，处置相同：① 整包 `UNMATCHED`；
      ② 包登记过但 **0 行**。两种都 ⇒ 调用方写 `n/a`，**绝不写 `0%`** ——
      `0%` 是在断言「查了、没有在做种的」，而这两个读数**撑不起这句话**
      （`ERR-AI-03`：`n/a` / `0` / 「没事」是三件事）。
      ★ 反例最清楚：`mbf` 的 `0/4` **分母是 4** ⇒ 它的 `0%` **是合法的**，
      与「除零产生的 0%」含义**完全不同**，**别把两者合并**。
    """
    d = store.summary(pack)
    # ★★ 两个池（`e404b1ca#6` ②，见 `DEBT_STAGES` 那段注释）：`SKIPPED`/`ERROR`/`PENDING`
    #   是**欠账**（有限、要清零），`UNMATCHED` 是**常态**（无限、按周期走）。
    #   ★ 只数**不该被搜的**（`DONE_STAGES`）**不进任何池** —— 把"正在做种的"
    #     算成"待搜"正是 `§26.33` 那个 30 倍偏差的形状。
    # ★ 用 `pool_of` **分档**、而不是在这里再写一遍 `DEBT_STAGES`/`STEADY_STAGES` ——
    #   `DEBT_STAGES` 与 `STEADY_STAGES` 是**两处**可能漂开的集合，`pool_of` 是
    #   它们**唯一**的判据（`A.11`：同一事实只在一处）。这里若各写一份，改一处
    #   忘一处 ⇒ 计数与分档打架，而**没有任何东西会报出来**。
    _pool = {s: pool_of(s) for s in ALL_STAGES}
    debt = sum(d[s] for s in ALL_STAGES if _pool[s] == "debt")
    steady = sum(d[s] for s in ALL_STAGES if _pool[s] == "steady")
    return {
        **d,
        "denom": sum(d[s] for s in CENSUS_DENOM_STAGES),
        "numerator": d[STAGE_SEEDING],
        # ★ 键名带 `pool_` 前缀：`summary()` 里已有阶段名做键，直接叫 `debt`/`steady`
        #   会和将来可能的阶段名撞车，而**撞车是静默的**（`{**d, ...}` 后面覆盖前面）。
        "pool_debt": debt,
        "pool_steady": steady,
        "pool_total": debt + steady,
    }


def pack_progress(store: StateStore) -> list[dict]:
    """每个包的进度 → `[{name, seeding, total, pct, census, scanned_at}, ...]`（按包名排序）。

    ★ **返回结构化数据，不在这里渲染** —— 渲染归 `drive-loop.py`：
      现有各 watch（`iyuu_watch` / `reconcile_watch` / `qb_999_watch` /
      `linkguard_watch`）都是「(正文一段, metrics 字典)」的形状，本函数照那个形状
      只出数据，让渲染层一处负责措辞。

    ★ **空列表 = 一个包都没登记**，与「库读不到」是两件事 ——
      后者由调用方（`drive-loop.py`）在 `except` 里写成 `n/a`，
      **不许**把两者都写成 `0`（`n/a ≠ 0 ≠ 没事`，见 `ERR-AI-03`）。

    ★ `scanned_at` 取 `pack.scan_finished_at`（每包**自己**的时刻，`mark_scan`
      写入）—— 比正文里一个全局「截至 HH:MM」精确：日报挂在「当天第一批」上，
      而 `stage` 是**逐包**在各自批次里更新的，所以当天还没跑过的包，
      它的两个数停在上次 sync（见 SUMMARY §23.5 订正④）。

    ★★ `seeding` / `total` **仍然**来自 `pack_seeding_total`，
      **不许**改从 `census["numerator"]` 取 —— 今天两者相等，但
      「一个数两个来源」正是 `pack_seeding_total` 被抽出来要防的那件事；
      而且 `pack_seeding_total` 的 2 元组返回**被测试逐字钉住**（`#97`）。

    `pct` 的取值：`int` 百分比，或字符串 `"n/a"`（`denom == 0` 时）。
      ★ 它**故意不是** `float` —— 渲染层要把它和 `n/a` 分开写，
      浮点会诱导人写 `f"{pct}%"` 而把 `n/a` 印成 `n/a%`。
    """
    out: list[dict] = []
    for p in store.packs():
        seeding, total = pack_seeding_total(store, p["name"])
        census = pack_stage_census(store, p["name"])
        denom = census["denom"]
        out.append({
            "name": p["name"],
            "seeding": seeding,
            "total": total,
            # 分母为 0 ⇒ "n/a"（**不是** 0）。理由见 pack_stage_census 的说明。
            "pct": round(100 * census["numerator"] / denom) if denom > 0 else "n/a",
            "census": census,
            "scanned_at": p["scan_finished_at"],
        })
    return out


# --------------------------------------------------------------------------- #
# 驱动 cross-seed 重搜
# --------------------------------------------------------------------------- #
def post_webhook(path: str, *, url: str, api_key: str, timeout: float = 30.0) -> int:
    """打一次 cross-seed webhook，返回 HTTP 码（0 = 连不上）。

    ★ 路径必须放进 **body**（`urlencode({"path": ...})`），绝不能拼进 URL。
      见 SUMMARY §6.3：Git Bash(MINGW) 会把命令行里的 NAS 路径改写成
      `C:/Program Files/Git/volume1/...`，中文还按 GBK 重编码，cross-seed 会
      stat() 失败返回 400。用 urllib 构造 body 天然规避这个坑。
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    endpoint = url.rstrip("/") + "/api/webhook"
    body = urllib.parse.urlencode({"path": path}).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"X-Api-Key": api_key,
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001
        return 0


def drive_webhook(paths: list[str], *, url: str, api_key: str, timeout: float = 30.0,
                  on_result=None) -> tuple[int, int]:
    """对给定的路径列表逐个打 webhook（**不控速**，仅供内部/兼容使用）。

    真正给用户用的是下面的 `DriveSession` —— 它带节流、退避感知和自动回灌。
    """
    ok = err = 0
    for pth in paths:
        code = post_webhook(pth, url=url, api_key=api_key, timeout=timeout)
        if code in (200, 202, 204):
            ok += 1
        else:
            err += 1
        if on_result:
            on_result(pth, code)
    return ok, err


# --------------------------------------------------------------------------- #
# DriveSession —— 控速 + 退避感知 + 自动回灌
# --------------------------------------------------------------------------- #
@dataclass
class DriveStats:
    total: int = 0
    sent: int = 0
    ok: int = 0
    failed: int = 0
    waited_sec: float = 0.0
    backoff_hits: int = 0
    aborted: str | None = None
    #: `aborted` 的**机器可读**分类。`aborted` 是给人看的一句话，这个给判据看。
    #: ★ 为什么必须分开：`aborted` 有两种**性质完全不同**的来源 ——
    #:   `"indexer-backoff"` 站点在退避、等到超过 `--max-wait` 就先收工
    #:     （**良性**：本批条目没失败，剩下的下轮重新排上），
    #:   `"webhook-auth"` webhook 返回 400/401/403（**真故障**：鉴权/路径问题）。
    #:   下游 `update_abort_streak()` 原先只看 `aborted` 的真假，于是良性的那种
    #:   被算成「批失败」。实测 2026-09-13 的 TSV 里**同一批**既写 `ok=22 failed=0`，
    #:   又写「连续 3 批失败（索引器 HDtime 要等到 …）」—— 名字指不回真实记录。
    aborted_kind: str | None = None
    #: 打完之后再同步一次的结果
    resync: SyncReport | None = None
    #: 打完还处于 SKIPPED 的片子（= 又被退避了）
    still_skipped: int = 0
    #: ★ `newly_seeding` 是**净额**（当前 SEEDING − 批次前 SEEDING）。
    #:   它不是增量 —— 连续两天可以完全一样（实测 443/443，§26.50 三）。
    newly_seeding: int = 0
    #: ★★ `first_seeding` 是**真增量**：本批**首次**转 SEEDING 的片数。
    #:   作答「今天到底有没有新片被辅上」—— 净额答不了这个问题。
    #:   口径与 `trend()` 一致（`MIN(at) GROUP BY movie_id`，按片去重）。
    first_seeding: int = 0

    def render(self) -> str:
        lines = [
            f"计划 {self.total} 条，发出 {self.sent} 条（成功 {self.ok} / 失败 {self.failed}）",
            f"因退避等待 {self.waited_sec / 60:.1f} 分钟，命中退避 {self.backoff_hits} 次",
        ]
        if self.aborted:
            lines.append(f"⚠ 提前中止：{self.aborted}")
        if self.resync:
            lines.append("回灌结果：")
            lines.append("  " + self.resync.render().replace("\n", "\n  "))
            lines.append(f"  其中「又被退避」仍为 SKIPPED : {self.still_skipped}")
        return "\n".join(lines)


class DriveSession:
    """带节流与退避感知的 webhook 驱动器。

    为什么要控速
    ------------
    cross-seed 收到 webhook 就**立刻**开搜，多个 webhook 会变成多个并发任务；
    每个任务各自按 `delay` 发请求 —— 于是并发 N 个 webhook ≈ 把查询速率乘以 N。
    所以这里串行发送、并在两次之间 sleep `interval` 秒，让总速率可控。

    为什么要盯退避
    --------------
    §6.5 的教训：429 的根因常常是**站点侧 502**，不是我们太快。
    但不管是哪种，索引器一旦被 snooze，cross-seed 会把后续条目**直接跳过**。
    所以每次发送前先看一眼 cross-seed 的 `indexer` 表，有退避就等到解禁再继续，
    免得白发一堆注定被跳的 webhook。

    ★ 但「退避」不只有"正挡着"这一种形态（2026-09-12 修过一次，见
    `wait_out_backoff()`）：实测的 429 只 snooze 了 55 秒，比检查间隔还短，
    只查"此刻挡不挡路"就会**永远看不见它**，`backoff_hits` 恒为 0，
    `next_sleep()` 也就永远收不到"站点在限流"这个信号。所以现在：
      · 检查同时按**条数**（check_every）和**秒数**（check_secs）触发；
      · 只要 `retry_after` 变了就认定"新发生了一次限流"，哪怕窗口已经过去。
    """

    def __init__(self, *, url: str, api_key: str, crossseed_db: str | Path | None = None,
                 interval: float = 30.0, check_every: int = 10, max_wait: float = 1800.0,
                 check_secs: float = 60.0,
                 timeout: float = 30.0, pause_on_backoff: bool = True,
                 indexers: list[str] | None = None,
                 sleep=time.sleep, now=datetime.now, monotonic=time.monotonic,
                 on_event=None):
        self.url = url
        self.api_key = api_key
        self.crossseed_db = Path(crossseed_db) if crossseed_db else None
        self.interval = max(0.0, float(interval))
        self.check_every = max(1, int(check_every))
        #: ★除了「每 check_every 条」再按**时间**兜一层。只有条数触发的话，
        #: 10 条 × 30s = 300s 才看一眼，而实测的 429 只 snooze 了 55 秒 ——
        #: 整个窗口落在两次检查之间，退避就跟没发生过一样（详见 wait_out_backoff）。
        self.check_secs = max(0.0, float(check_secs))
        self.max_wait = float(max_wait)
        self.timeout = timeout
        self.pause_on_backoff = pause_on_backoff
        #: `--indexers` 归一化后的集合。**唯一的用途**是判断「是不是**所有**配置的站
        #: 都在退避」—— 只有那一种情形下，"等"和"中止"才有意义（见 wait_out_backoff）。
        #: ★ 空集 = 调用方没给 ⇒ 判据退化回旧行为（照旧等/中止），宁可吵。
        self.indexers = {norm_indexer_name(i) for i in (indexers or []) if i.strip()}
        #: 已经就"只禁了一部分站、我们照发"打过警告的组合，避免每个检查点刷屏。
        self._subset_warned: set[str] = set()
        self._sleep = sleep
        self._now = now
        self._mono = monotonic
        self._on_event = on_event or (lambda *a, **k: None)
        #: 上一次看到的「索引器 → retry_after 原值」，用来认出新发生的退避
        self._snooze_seen: dict[str, float] = {}
        self._baselined = False
        self.stats = DriveStats()

    # -- 退避 --------------------------------------------------------------- #
    def _read_backoffs(self) -> tuple[list[IndexerBackoff], list[IndexerBackoff]]:
        """一次读库，返回 `(此刻挡路的, 最近被限流过的)`。读不到就都给空表。

        两列都要：前者决定「要不要等」，后者决定「要不要记一笔、下批拉长间隔」。
        """
        if not self.crossseed_db:
            return [], []
        try:
            rows = read_indexer_backoff(self.crossseed_db)
        except (OSError, sqlite3.Error, FileNotFoundError) as e:
            self._on_event("warn", f"读索引器状态失败（忽略）: {e}")
            return [], []
        # 用会话自己的时钟判断"解禁过没过"（见 IndexerBackoff.active）
        now = self._now()
        return blocking_backoffs(rows, now), snoozed_indexers(rows)

    def backoffs(self) -> list[IndexerBackoff]:
        """此刻真正挡住搜索的索引器。"""
        return self._read_backoffs()[0]

    def wait_out_backoff(self) -> bool:
        """有索引器在退避就等到解禁。返回 False 表示等太久了，建议中止。

        ★ 2026-09-12 修：以前这里只问「**此刻**挡不挡路」（`blocking_backoffs`），
        结果**短退避根本检测不到**，`backoff_hits` 恒为 0，`next_sleep()` 也就
        永远收不到「站点在限流」这个信号。实测就是这么漏的：
            info.2026-09-12.log  `08:16:19 ... Failed to reach NanyangPT (南洋):
                                  code 429 ... snoozing until 2026-09-12 08:17:14`
        窗口只有 **55 秒**，而检查间隔是 `check_every × interval` = 10 × 30s = 300s，
        整段落在两次检查之间；下一眼看过去时 `until` 已经过去，`active=False`，
        于是既不等待、也不计数。
        现在两条腿走路：
          ① `retry_after` 跟上次看到的不一样 → **新发生的退避**，照记一笔命中，
             哪怕窗口已经过去 —— 它确实发生过，是「站点在限流」的真信号；
          ② 窗口还没过去，照旧等到解禁（受 `max_wait` 上限保护）。
        进程刚起时第一次检查只建立基线，不计数（手上没有「上一次」可比）——
        但如果那一刻退避**正在挡路**，仍然要记（它明明白白挡着我们）。
        """
        if not self.pause_on_backoff:
            return True
        baseline = not self._baselined
        self._baselined = True
        while True:
            blocking, snoozed = self._read_backoffs()
            for b in snoozed:
                key = b.retry_after if b.retry_after is not None else -1.0
                fresh = self._snooze_seen.get(b.name) != key
                # ★基线那一眼必须**只认"此刻真挡路"的**：库里常常留着一小时前
                #   那次退避的残值（status 还写着 RATE_LIMITED，retry_after 早过期），
                #   若把"没见过"当成"新发生"，每一批开头都会白记一笔 → 永远 2 小时一跑。
                if (fresh and not baseline) or (baseline and b.active(self._now())):
                    self.stats.backoff_hits += 1
                    # ★★ 时刻**必须带日期**（2026-09-16，#114）：原来这里写 `%H:%M:%S`，
                    #    于是 09-15 13:39 那行打出「解禁 01:31:11」—— **看着像当天凌晨**，
                    #    而实际是**次日** 01:31（cross-seed 侧窗口跳到"明天"了）。
                    #    同一个时间轴上 4 行之外还有 Prowlarr 侧的 `disabledTill 09-16 13:32`，
                    #    两个读数差 12 小时，**审阅时被当成"其中一个抄错了"**——
                    #    真相是**两把锁各有一个时刻**（见 ENVIRONMENT `ERR-SVC-17`）。
                    #    ⇒ 跨午夜的时刻一律带日期，与 `:1028` / `:2574` / `:2581` 统一为
                    #      `%m-%d %H:%M`；**判据**：`tests/test_backoff.py` 断言跨午夜输出里带日期。
                    #
                    # ★★★ 措辞**必须区分「还在等」与「已经过去了」**（2026-09-17，#116）：
                    #    这条路（`snoozed` 分支）的 `until` **可能是已经过去的时刻** ——
                    #    库里 `RATE_LIMITED` 是个**不会被擦掉**的陈旧标记，
                    #    `retry_after` 早过期了它还在。实测 2026-09-16 10:41:06 那两条：
                    #        `索引器 HDtime 被限流（RATE_LIMITED），解禁 10:21:19`
                    #        `索引器 NanyangPT (南洋) …，解禁 09:22:06`
                    #    两个「解禁」**都早于触发时刻** —— 与库里 `retry_after` 逐秒相同
                    #    （`cross-seed.db` 权威值：HDtime 10:21:19 / NanyangPT 09:22:06）
                    #    ⇒ **数没印错，是"解禁"这个词印错了**：它不是「将要解禁」，
                    #      而是「**上次那个窗口已经在 … 结束了**」。
                    #    ★ 代价（真实发生）：审阅时按「将要解禁」去读，就得到"时间倒流"，
                    #      于是把一条**陈旧残值**读成「今天上午又被限了一次」——
                    #      而那天根本没有新的限流事件（09-16 全天 `要等到` 计数 = 0）。
                    #    ⇒ 分两句说：窗口已过 → 说明"窗口已于 … 结束"；未过 → 才叫"解禁"。
                    #      ★ 与 `README`「读数要能自证」同族：**措辞不能把"过去"说成"将来"**。
                    _until = b.until
                    if _until and _until <= self._now():
                        self._on_event(
                            "warn",
                            f"索引器 {b.name} 曾被限流（{b.status}）"
                            f"，窗口已于 {_until:%m-%d %H:%M} 结束"
                            f"（陈旧标记，**当下不挡路**）")
                    else:
                        self._on_event("warn", f"索引器 {b.name} 被限流（{b.status}）"
                                       + (f"，解禁 {_until:%m-%d %H:%M}" if _until
                                          else "，未给解禁时间"))
                self._snooze_seen[b.name] = key
            if not blocking:
                return True
            for b in blocking:                    # 没给解禁时间的也要进基线
                self._snooze_seen.setdefault(
                    b.name, b.retry_after if b.retry_after is not None else -1.0)
            soonest = min((b.until for b in blocking if b.until), default=None)
            names = ", ".join(b.name for b in blocking)
            if soonest is None:
                # 状态是退避但没给解禁时间 —— 只能干等一小会儿再看
                self._on_event("warn", f"索引器 {names} 处于退避且无解禁时间，等 60s 再看")
                self._sleep(60)
                self.stats.waited_sec += 60
                continue
            delta = (soonest - self._now()).total_seconds()
            if delta <= 0:
                return True
            if delta > self.max_wait:
                # ★★ 判据是「**所有**配置的索引器都在退避吗」，**不是**「有没有站在退避」。
                #
                #   2026-09-16 实测（读 cross-seed 自己的 info 日志，做了阳性对照）：
                #   cross-seed 只在**过滤后一个站都不剩**时才跳过条目 ——
                #     · 09-11 那次 `Skipped searching` **296 条**（当时可用的站所剩无几）
                #     · 09-13~09-15 全程只禁 HDtime 一个（另 3 站健康）⇒ 计数是 **0**
                #   所以还有健康站时，等在 / 中止都不解决任何问题，只是白停：
                #   那 3 天 49 批里 17 批"发出 0 条"，发出率 27.7% → 3.7% → 7.1%。
                #
                #   兜底方向：**不知道配置了哪些站时一律不改行为**（照旧中止）——
                #   把真故障误判成良性会静默，反之只是多停一轮。
                #
                #   ★ 已知取舍（2026-09-16 记）：这里的"配置了哪些站"取自 `--indexers`
                #     这把**人手维护的尺**，而 `blocking` 取自 cross-seed.db。两者名字
                #     对不上时判据会偏，但两个方向都有告警兜着（见 drive-loop 的
                #     `check_indexers`：多了站报 `indexer-extra`、漏了站报 `indexer-missing`）：
                #       · 多列了站（cross-seed 根本不搜它）⇒ 误判"还有健康站" ⇒ 照发；
                #         代价是白跑一批，条目落到 SKIPPED、下一轮重搜 —— 可回收。
                #       · 漏列了站（其实健康）⇒ 误判"全在退避" ⇒ 中止，即**旧行为**。
                #     另一种写法是**完全不看 `--indexers`**：`read_indexer_backoff`
                #     返回的是整张表，`enabled` 的站减去 `blocking` 就是健康站，
                #     天生抗名字漂移。将来若名字漂移成为真问题，改走那条。
                blocked_norm = {norm_indexer_name(b.name) for b in blocking}
                if self.indexers and not self.indexers <= blocked_norm:
                    if names not in self._subset_warned:
                        self._subset_warned.add(names)
                        self._on_event(
                            "warn",
                            f"索引器 {names} 要等到 {soonest:%m-%d %H:%M}"
                            f"（超过上限 {self.max_wait / 60:.0f} 分钟），但还剩 "
                            f"{len(self.indexers - blocked_norm)} 个健康站"
                            f" —— **不等，照发**（cross-seed 会跳过被禁的站、用其余的搜）")
                    return True
                self.stats.aborted_kind = "indexer-backoff"
                self.stats.aborted = (
                    f"索引器 {names} 要等到 {soonest:%Y-%m-%d %H:%M:%S}"
                    f"（{delta / 60:.0f} 分钟 > 上限 {self.max_wait / 60:.0f} 分钟）")
                return False
            self._on_event("wait",
                           # ★ 带日期（#114 同族）：这一路是"短等"（delta 秒级），
                           #   跨午夜概率低，但**同一个函数里三种格式是不该的** ——
                           #   见 `:2589`（带完整日期）、`:2526`（本次一并改成带日期）。
                           f"索引器 {names} 退避中，等到 {soonest:%m-%d %H:%M:%S}"
                           f"（{delta:.0f}s）")
            self._sleep(min(delta + 2, self.max_wait))
            self.stats.waited_sec += min(delta + 2, self.max_wait)

    # -- 主循环 ------------------------------------------------------------- #
    def run(self, paths: list[str]) -> DriveStats:
        self.stats.total = len(paths)
        last_check = 0.0
        for i, pth in enumerate(paths, 1):
            if self.pause_on_backoff:
                now_m = self._mono()
                # 条数触发（老行为，保持）+ 时间触发（★短退避靠它才看得见）。
                # ★ check_secs=0 表示"关掉时间触发" —— 不能写成 `now-last >= 0`
                #   （那恒为真，会退化成每条都查一次库）。
                due = (i == 1
                       or (i - 1) % self.check_every == 0
                       or (self.check_secs > 0
                           and now_m - last_check >= self.check_secs))
                if due:
                    last_check = now_m
                    if not self.wait_out_backoff():
                        self._on_event("abort", self.stats.aborted or "退避等待超时")
                        break

            code = post_webhook(pth, url=self.url, api_key=self.api_key,
                                timeout=self.timeout)
            self.stats.sent += 1
            if code in (200, 202, 204):
                self.stats.ok += 1
            else:
                self.stats.failed += 1
            self._on_event("sent", pth, code, i, self.stats.total)

            if code in (400, 401, 403):
                self.stats.aborted_kind = "webhook-auth"
                self.stats.aborted = f"webhook 返回 {code}（鉴权/路径问题），已停"
                self._on_event("abort", self.stats.aborted)
                break

            if i < len(paths) and self.interval > 0:
                self._sleep(self.interval)
        return self.stats


def wait_for_log_quiet(log_path: str | Path, *, quiet_sec: float = 90.0,
                       max_wait: float = 3600.0, poll: float = 15.0,
                       sleep=time.sleep, on_event=None) -> str:
    """等 cross-seed "忙完"：**日志文件不再变大**就算它空了。

    cross-seed 没有能查队列的 HTTP 端点（v6.13 实测只有 /api/ping 和 /api/status，
    都只回 "OK"），所以用日志的静默当信号 —— 便宜、可靠，且不打扰它。

    返回 "quiet" / "timeout" / "unreadable"。
    """
    log_path = Path(log_path)
    on_event = on_event or (lambda *a, **k: None)
    if not log_path.is_file():
        return "unreadable"
    started = time.time()
    last_size = -1
    last_change = time.time()
    while True:
        try:
            size = log_path.stat().st_size
        except OSError:
            return "unreadable"
        if size != last_size:
            last_size = size
            last_change = time.time()
        elif time.time() - last_change >= quiet_sec:
            on_event("drained", f"日志已静默 {quiet_sec:.0f}s，认为 cross-seed 忙完")
            return "quiet"
        if time.time() - started >= max_wait:
            on_event("timeout", f"等了 {max_wait / 60:.0f} 分钟仍未静默，先继续")
            return "timeout"
        sleep(poll)


# --------------------------------------------------------------------------- #
# 共用小工具
# --------------------------------------------------------------------------- #
# 这几个原本在 scripts/reseed-state.py 里（私有 `_xxx`），drive-loop.py 又各复制了
# 一份公开版 —— 两份实现"改一个忘一个"就会静默算错数。集中到这里，两边都 import。
def _qbit_torrents_info(url: str, params: dict[str, str], timeout: float) -> list[dict]:
    """`GET /api/v2/torrents/info?<params>` —— `qbit_torrents` / `qbit_tagged` 共用。

    ★ 抽出来是**故意的**，理由就是上面那三行：本项目已经栽过一次"两份实现改一个
      忘一个"。与其再抄一份按 tag 取数的，不如让两条路走同一段 URL 拼装和同一段
      错误语义 —— 将来要加超时/重试/鉴权，改一处就够。
    """
    import urllib.parse
    import urllib.request

    # ★ 无参数时**不留那个光秃秃的 `?`**：`torrents/info` 全量取数（`qbit_all_torrents`）
    #   走的就是空 params，留着会拼出 `.../torrents/info?` —— 能用，但它是那种
    #   "看着像有查询、其实没有"的 URL，日志里读起来要愣一下。带参数时**一字不变**
    #   （`test_iyuu_watch.py` 钉着按分类那条的字面量）。
    q = urllib.parse.urlencode(params)
    full = url.rstrip("/") + "/api/v2/torrents/info" + (f"?{q}" if q else "")
    req = urllib.request.Request(full, headers={"Referer": url.rstrip("/")})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8")) or []


def qbit_torrents(url: str, category: str, timeout: float = 30.0) -> list[dict]:
    """取 qB 某分类的全部种子。

    **失败会抛异常** —— 由调用方决定是忽略（无人值守的循环，宁可少一个数据源
    也不要整批挂掉）还是中止（诊断脚本，想知道 qB 是不是真的不通）。
    """
    return _qbit_torrents_info(url, {"category": category}, timeout)


def qbit_tagged(url: str, tag: str, timeout: float = 30.0) -> list[dict]:
    """取 qB 上挂了某个 tag 的全部种子。**失败语义同 `qbit_torrents`。**

    ★ 为什么不复用按分类那条：**同一个 :3060 上两个系统靠 tag 区分**。
      我们的 cross-seed 打 `cross-seed`、IYUU Plus 打 `IYUU自动辅种`，
      而两者的 `save_path` 落在**同一批站点子目录**里，IYUU 那 100 条还
      **没有分类**（SUMMARY §18.8 的构成表）。按分类查会把两边混成一坨。
    """
    return _qbit_torrents_info(url, {"tag": tag}, timeout)


def qbit_all_torrents(url: str, timeout: float = 30.0) -> list[dict]:
    """取 qB 上的**全部**种子（不加 category / tag 过滤）。
    **失败语义同 `qbit_torrents`。**

    ★ 为什么必须有第三条：前两条各自带一个过滤条件，而「卡 999」那条判据要的是
      **全量** —— `total`（分母）就是全部种子数。按任一过滤取，**分母就错了**，
      而错的分母不会报错，只会让比例失真到没法判（"2 条"到底是 2/932 还是 2/5）。
    """
    return _qbit_torrents_info(url, {}, timeout)


# --------------------------------------------------------------------------- #
# qB「卡 999」—— 停滞的未完成种子
# --------------------------------------------------------------------------- #
# 立这个判据的现场（2026-09-13）：这个 qB 只挂拆大包的种子（全员硬链接），
# **本不该下载任何东西**。任何「没下完」的种子都是异常，而 99.x% 是其中唯一
# **既不报错也不完成**的静默档 —— 它不进 error 计数，也不进 seeding 计数，
# 于是从所有现有观测里同时漏出去。
#
# 判据：progress >= 0.99  且  amount_left > 0  且  (now - last_activity) > 24h
#
# ★ 为什么**故意不写上界**（不写 `progress < 1.0`）：
#   `progress` 是浮点，末尾只剩几个字节的种子会被舍入成 1.0 —— 写上界会
#   **误杀正是要抓的那一类**。而 `amount_left > 0` 已经排除了真正下完的，
#   所以上界只有风险没有收益。实测（#67）：现场 2 条 `amount_left>0 且
#   progress>=0.99` 的原始值是 0.9999955830701261 / 0.997501052321974，
#   **恰好 == 1.0 的 0 条** —— 也就是说「写上界」与「不写」今天同值，
#   差别只在将来，而将来那次差别是不写这边对的。
#
# ★ 为什么用 `last_activity` 而不是 `state == "stalledDL"`：
#   不必等 qB 自己宣布 stalled —— 实测 932 条 **0 缺失**（int，范围
#   2 天前 ~ 14 秒前）。
#
# ★ 为什么停滞闸**必须有**：现场同时挂着一条 progress=0.9975 的
#   **健康下载中**的种子（`last_activity` 是 0.0 小时前）。只按
#   `amount_left > 0` 判会把它一起记进去。这条就是
#   `tests/test_qb_999_band.py` 里那条「不命中」用例的来源。
#
# ★ 为什么要配基线（不在这里，在 drive-loop 侧）：不配就会天天发同样一封
#   —— 那正是 #59 邮件风暴教训的反面。见任务 #70。
QB_999_MIN_PROGRESS = 0.99
QB_999_STALL_SEC = 24 * 3600     # 24h 本身就是闸，**不再另配稳定性闸**（任务 #66 的答案 2）


def qb_999_band(torrents: list[dict], now: float) -> dict:
    """从 `torrents/info` 的原始列表里挑出「卡 999」的那些。

    返回 `{"n": 条数, "hashes": [升序 hash], "total": 分母}`。
      · `hashes` 给基线比对用（#70），**只出 hash 不出 name** ——
        上层只报数量，名字不该有机会流到正文或日志里。
      · `total` = 传进来的全部种子数。**分母必须跟着数一起走**：
        单说「2 条」没有量纲，判不了是「2 / 932」还是「2 / 5」。

    `now` 是**参数**、不是函数里 `time.time()` —— 否则这个函数没法测
    （时间在跑，断言写不住），而它恰恰是「阈值取 24h」唯一能被钉住的地方。

    **不抛异常**：字段缺失 / 为 None 一律当「不命中」。调用方挂在每天一次的
    日报里，一次字段变动不该让整份日报消失（同 `iyuu_watch` 的失败语义）。

    ★ `last_activity` 缺失或为 0 时**按「停了很久」算**（`or 0`）—— 这是
      **故意的**，与 `test_backoff.py` 那条「认不出种类时按真失败算
      （宁可吵不可静默）」同一条原则：判不了的时候，漏报的坏法
      （永远看不见）比误报的坏法（基线响一次就静默）重。
    """
    hits: list[str] = []
    for t in torrents or []:
        if (t.get("amount_left") or 0) <= 0:
            continue                                    # 下完了（或字段缺失）
        if t.get("state") == "error":
            continue                                    # error 有独立口径，不重复记
        if (t.get("progress") or 0.0) < QB_999_MIN_PROGRESS:
            continue                                    # 没到 99%
        if (now - (t.get("last_activity") or 0)) <= QB_999_STALL_SEC:
            continue                                    # 还在动 —— 健康下载中，不是卡住
        h = t.get("hash")
        if h:
            hits.append(h)
    hits.sort()                                         # 定序：让基线与断言都稳定
    return {"n": len(hits), "hashes": hits, "total": len(torrents or [])}


# --------------------------------------------------------------------------- #
# qB「卡种」—— 匹配到了却下不来的那些（`e404b1ca#7`）
# --------------------------------------------------------------------------- #
# 用户 2026-09-20 的场景：「**涌入上百个种子做不下**」⇒ 问「搜到了
# stalledDL/checkingDL/卡999 就别再搜了」。
#
# ★ 前半句**代码本来就是这么设计的** —— `MATCHED` 已在 `DONE_STAGES` 里
#   （搜到就不重搜），`qb_999_band` 也早就有了。**缺的是"看得见"**：
#   「匹配到却下不来」这批在日报里**一个字都不出现** —— `MATCHED` 不进
#   `UNMATCHED`（不报）、不是 `SEEDING`（进度里不算）、不卡 999（那条要
#   `progress ≥ 0.99`）⇒ **从所有现有观测里同时漏出去**（与本文件
#   `qb_999_band` 那段"99.x% 是唯一既不报错也不完成的静默档"是**同一个形状**，
#   只是坐标不同：那条卡在**进度**上，这条卡在**状态**上）。
#
# ★★ 判据：**只按 `state` 分档**，不看进度、不看时长。
#   理由：这几个状态**本身就是 qB 给的结论**，不是我们推的：
#
#     stalledDL   —— qB 判定"有 peer 但没人给我们数据"（实测现成 3 个）
#     metaDL      —— 连元数据都还没拿到（磁力链没种子）
#     checkingDL  —— 校验中断（正常几秒就过；卡住 = 出事了）
#     error       —— qB 自己标了错（tracker 拒绝 / 文件丢失…）
#
#   ★ 为什么不复用 `qb_999_band`：那条的判据是「`progress ≥ 0.99` **且**停滞 >24h」
#     —— 一条 `stalledDL` 且 `progress=0.3` 的种子**不满足它**，而它恰恰
#     是用户说的"做不下"里最典型的一条。两个判据的**输入维度不同**
#     （这条是 `state`，那条是 `progress`+`last_activity`），并起来会把
#     两个词的语义都改掉（同「不并进 qb_999」那条既有注释的道理）。
#
#   ★ 为什么不看时长：`stalledDL` **本来**就是"一段时间没进展"的结论，qB 已经
#     替我们判过；再叠一个 24h 闸只会**漏掉刚卡住的**，而"刚卡住"正是要早发现的。
#     误报的坏法（一条抖动状态进来）比漏报的坏法（涌入上百个看不见）轻得多。
#
# ★★ **只识别 + 通知，不碰 qB**（同 `reseed_freeze` 那条否决记录）：
#   不 pause / 不删 / 不改 tag。用户要的是先"看见"。
STALLED_STATES = ("stalledDL", "metaDL", "checkingDL", "error")


def qb_stalled_band(torrents: list[dict]) -> dict:
    """从 `torrents/info` 的原始列表里挑出**卡住的**那些，按 `state` 分档。

    返回 `{"n": 总数, "by_state": {state: 条数,…}, "total": 分母, "hashes": [升序]}`。

    ★ `by_state` **只含命中过的档**（不把 4 个档都填 0）—— 正文里"没提到"
      与"提到了是 0"是两件事（`A.11`：`n/a ≠ 0 ≠ 没事`）。
    ★ `total` 必须跟着走（同 `qb_999_band`）：单说"3 条"没有量纲。
    ★ **不抛异常**：字段缺失一律当"不命中"。它挂在每天一次的日报里，
      一次字段变动不该让整份日报消失（同 `iyuu_watch` / `qb_999_band`）。
    ★ `hashes` 给上层做基线用（"今天新冒出来的"）—— 正文**只报数量与档名**，
      **绝不报种子名**（同 `qb_999_watch` 的口径：日报是要发出去的）。
    """
    by_state: dict[str, int] = {}
    hits: list[str] = []
    for t in torrents or []:
        st = t.get("state")
        if st not in STALLED_STATES:
            continue
        by_state[st] = by_state.get(st, 0) + 1
        h = t.get("hash")
        if h:
            hits.append(h)
    hits.sort()                                         # 定序：让基线与断言都稳定
    # ★★ `n` 从 **`by_state`** 来（**不**从 `hashes` 来）—— 两者**可以不等**：
    #   `hash` 字段缺失时那条仍**该被数到**（它是条卡住的种子），只是进不了基线。
    #   ★ 若用 `len(hits)`，一个缺 hash 的条目会让 `n` **悄悄少 1**，
    #     而 `by_state` 还是对的 ⇒ 同一封信里两个数**互相打脸**。
    #     实测那个形状：`by_state` 4 档之和 6、而有 hash 的只有 5 条。
    return {"n": sum(by_state.values()),
            "by_state": by_state, "hashes": hits,
            "total": len(torrents or [])}


# --------------------------------------------------------------------------- #
# qB「装不出来」—— 缺的那一点点**永远补不上**的单种
# --------------------------------------------------------------------------- #
# 立这个判据的现场（2026-09-18）：用户报「校验没通过的文件开始下载了」。
# 实测三条 cross-seed 单种，`stalledDL` / `num_seeds=0` / `availability=0.999`：
#
#   · 逐文件 progress 是 `.mkv` ≈ 99.98%、而 `.jpg`/`.nfo` **恰好 0.00000%**；
#   · 那个 0% 的文件在 `reseed_singles/` 里**根本不存在**；
#   · 同一个文件在**农场**里**存在**（`cover.jpg` / `.nfo`）；
#   · 载荷（`.mkv`）两侧**大小逐字节相同**（9658005448）。
#
# ⇒ **真机制不是「校验不通过就重下」**，而是：cross-seed 按「名称+大小」匹配上了
#   发布组把附件（`.nfo`/`.jpg`）也写进种子的那一版，而农场里没有这些附件
#   ⇒ qB 去补那几个缺文件 ⇒ 没有 peer ⇒ **永远补不上**。
#
# ★★ qB 只认 piece（README「原理 C」）：**它已经有的那块数据不会重下**
#   ⇒ **源文件目前还没被写穿** —— 那是 `linkguard` 该盯的事，不是这里。
#   真危害是**永久占位 + 踩 HnR**，且**一旦那个 `.mkv` 被动过/删过就会真重下**。
#
# ★ 为什么不并进 `qb_999_band`：那条是「**停滞时长**」这个维度、且**依赖
#   `progress >= 0.99`**；本条是「**缺口的相对大小**」这个维度。两个维度正交，
#   并起来会同时改掉两边的语义与各自的测试（`tests/test_qb_999_band.py` 钉的是
#   24h 那个**阈值位置**，不是算式）。
#
# ★ 判据为什么取 `amount_left / size` 而**不是**「逐文件 progress == 0」：
#   逐文件 progress 也是**按 piece 全局算**的（qB 没有文件级校验，见「原理 C」），
#   所以「缺文件」未必显示成 0% —— 拿它当判据会漏。而 `amount_left / size`
#   是分母归一过的**相对缺口**，不依赖 piece 怎么分摊到文件上。
#
# ★★ 阈值 `1e-3` 的依据是**实测的断层**（2026-09-18，全量 1999 条）：
#       ==0（已完成）  1990
#       (0, 1e-3]         3   ← 全是上面那三条
#       (1e-3, 1e-2]      0
#       (1e-2, 0.5]       0
#       (0.5, 1]          6   ← 真在下、进度正常
#   ⇒ **`1e-3` 与 `0.5` 之间一条都没有**，取哪都行。取 `1e-3` 是取**靠缺口那一侧**：
#     将来出现「比 0.1% 稍大但仍够不着」的，会落在 `(1e-3, 0.5]` 这个
#     **今天还是空的**区间里 —— 那正是需要有人看一眼的地方，不该被静默放过。
#     ★ 这个断层的成因是「附件只有几百 KB，而载荷是 9~11 GB」⇒ 比值落在 1e-4 档。
#       换个量级（比如小载荷）断层位置会移 —— 所以**别把这个数当普适常量**，
#       它只在本项目的「大包拆包 + 附件型发布」这个形状下被实测支撑。
#
# ★★ 有一类**不命中但不是没问题**的，必须由调用方另立一格（见 drive-loop 的
#   `reseed_freeze_watch`）：四条 IYUU 种子 `amount_left/size == 1.0`（**一个字节
#   都没下到**）、`availability == 0.0`、`num_seeds == 0`，停滞 19~140 小时。
#   那是**另一种**毛病（没 peer ⇒ 没起来），**不是**本条要抓的「装不出来」。
#   ⇒ 本条**故意不兜它**（判据要能被证伪，别把两件事塞进一个数）。
RESEED_GAP_MAX_RATIO = 1e-3       # 相对缺口上界；依据是上表那条**实测断层**
RESEED_EXCLUDE_STATES = frozenset({"error", "missingFiles"})


def reseed_unbuildable_band(torrents: list[dict], now: float | None = None) -> dict:
    """从 `torrents/info` 的原始列表里挑出「装载缺口极小、却永远补不上」的那些。

    返回 `{"n": 条数, "hashes": [升序 hash], "total": 分母}` —— 与 `qb_999_band`
    **刻意同形**：`hashes` 给基线比对用，**只出 hash 不出 name**（名字不该有机会
    流进正文或日志）；`total` 是传进来的**全部**种子数（单说「3 条」没有量纲，
    判不了是 3/1999 还是 3/5）。

    判据（三条，**没有**停滞闸 —— 见下面「为什么不设」）：
      ① `amount_left > 0`            —— 已完成的不算
      ② `amount_left / size <= 1e-3` —— 缺口小到「只差发布组的附件」
      ③ `state` 不在 `error`/`missingFiles` —— 那两种有各自的口径，不重复记

    ★ `now` **收下但不用**：签名与 `qb_999_band` 对齐是为了调用方**不必分叉**
      （两个 watcher 一个循环里调）。**故意不设停滞闸**：判据 ② 本身已经足够尖
      （实测断层到 `0.5` 之间一条都没有），再加时间闸只会**推迟发现**。
      ⇒ 传 `None` 也合法；留着参数是为了将来若要加时间维度，改这里就够。

    **不抛异常**：字段缺失 / 为 None 一律当「不命中」（同 `qb_999_band` 的失败
    语义 —— 它挂在每天一次的日报里，一次字段变动不该让整份日报消失）。

    ★ `size` 缺失或 `<= 0` 时**不命中**（不是当 0 算）：算不出比值就**不声称**，
      与 `B.10`「『读不出来』不是『没问题』」同一条原则的反面应用 ——
      判不了的时候，**宁可不报**（因为这里的误报会把一条正常大种子说成坏了）。
    """
    hits: list[str] = []
    for t in torrents or []:
        left = t.get("amount_left")
        if not left or left <= 0:
            continue                                    # 下完了 / 字段缺失
        if t.get("state") in RESEED_EXCLUDE_STATES:
            continue                                    # 有独立口径，不重复记
        size = t.get("size")
        if not size or size <= 0:
            continue                                    # 算不出比值 ⇒ 不声称
        if left / size > RESEED_GAP_MAX_RATIO:
            continue                                    # 缺口太大 ⇒ 不是这一类
        h = t.get("hash")
        if h:
            hits.append(h)
    hits.sort()                                         # 定序：让基线与断言都稳定
    return {"n": len(hits), "hashes": hits, "total": len(torrents or [])}


def reseed_no_peer_band(torrents: list[dict]) -> dict:
    """挑出「**一个字节都没下到、且没有 peer**」的 —— 与上面那条**正交**。

    实测那四条 IYUU 种子：`amount_left/size == 1.0`（0%）、`availability == 0.0`、
    `num_seeds == 0`、停滞 19~140 小时。★ 它们**不是**「装不出来」（缺口不是「小」，
    而是「全缺」），是**另一种**毛病：**没 peer ⇒ 根本没跑起来**。

    ★ 为什么单独一条而不是并进上面：两者的**处置方向相反** ——
      「装不出来」是**存量、已定型**（互锁已生效，不再新增），
      而「没 peer」**会变**（出种/加 trackers 就可能活过来）。
      塞进一个数里就再也分不开了（同 `qb_999_band` 不并进主账的理由）。

    判据：`amount_left/size >= 0.999` 且 `num_seeds == 0` 且 `availability == 0`。
    ★ 三个都用 `or 0` 兜底 ⇒ 字段缺失时**按最坏情况算**（同 `qb_999_band` 对
      `last_activity` 的处理：宁可吵不可静默）。返回形状同上。
    """
    hits: list[str] = []
    for t in torrents or []:
        size = t.get("size")
        if not size or size <= 0:
            continue
        if (t.get("amount_left") or 0) / size < 0.999:
            continue
        if (t.get("num_seeds") or 0) != 0:
            continue                                    # 有 peer ⇒ 是在下，不是没起来
        if (t.get("availability") or 0.0) != 0.0:
            continue                                    # 见过源碎片 ⇒ 另算
        h = t.get("hash")
        if h:
            hits.append(h)
    hits.sort()
    return {"n": len(hits), "hashes": hits, "total": len(torrents or [])}


# --------------------------------------------------------------------------- #
# 链接守护：628 条既有硬链接的「锚」—— 文件指纹基线 + 差分
# --------------------------------------------------------------------------- #
# 立这个判据的现场（2026-09-13）：cross-seed 在 matchMode=partial 下把
# 「名称+大小匹配、但 piece 不一致」的单种注入 qB；qB 校验不通过**不报错**，
# 而是就地重下那几个 piece —— 而 linkDirs 是硬链接，这一写**直接落到源文件**
# 上（两处实证时间分秒吻合，见 README「源文件被写穿」）。
#
# 修法分两步，缺一不可：
#   ① 从此以后：matchMode 与 linkType 互锁（cross-seed/config.js），
#      只有 reflink(COW) 才允许 partial ⇒ **新**建的都是 COW 副本，写不穿。
#   ② 已经存在的那 628 条仍是硬链接，**故意不重建**（重建本身要动 60TB 的
#      链接，风险大于收益）—— 但必须能发现「它正在被写穿」。
#
# 这一节就是 ② 的判据：给那 628 条 payload 的文件建一份 (size, mtime) 指纹，
# 之后定期重算、比对。**文件被就地改写 ⇒ mtime 必变**。
#
# ★ 为什么不直接监控「是否被 recheck」：qB 不提供逐种的 recheck 计数，
#   `checkingDL` / `checkingUP` 只是**瞬时**状态，事后查不到。而且 recheck
#   本身无害 —— 有害的是它**失败后那一次就地重下**。所以判据落在**结果**
#   （文件到底变了没）上：既可观测，又正是要紧的那件事。瞬时状态另记一格
#   （见 `linkguard_inflight`），用来抓「此刻正在发生」。
#
# ★ 指纹只取 (size, mtime_ns)，**不取 inode、不取内容哈希**：
#   · 内容哈希要读 60TB，直接出局；
#   · inode 在 SMB 上不可靠（st_nlink 实测恒为 0），而本判据要能在 NAS 宿主机
#     与 Windows 两侧得到同一个答案 —— 判据只有一份才谈得上一致；
#   · size+mtime 两边都可靠，且**写穿必然改 mtime**（内核写文件必更新它）。
#     漏报的唯一情形是「改了内容又精确还原 mtime」，那不是 qB 会做的事。
LINKGUARD_TMP_SUFFIXES = (".!qb", ".parts", ".part", ".tmp")   # 下载中间产物：不计入基线


def _lg_is_tmp(name: str) -> bool:
    n = name.lower()
    return any(n.endswith(s) for s in LINKGUARD_TMP_SUFFIXES)


def linkguard_snapshot(roots: list[str]) -> dict[str, list[int]]:
    """walk 一批根目录，返回 {NAS 绝对路径: [size, mtime_ns]}。

    ★ 存**NAS 侧路径**（`/volume1/...`），不是 SMB 的 `//iSunker-DS423/...`：
      基线由 NAS 宿主机写（drive-loop），由 Windows 侧读（诊断 CLI），
      两边唯一都认的坐标系就是 NAS 内路径 —— 也是 qB API 的 `save_path`。
    ★ 读不到的条目**跳过**（不是记成 0）：记 0 会在下次比对时被当成「变了」，
      而权限问题会天天误报。跳过则只影响覆盖面，不影响正确性。
    ★ 不抛：调用方挂在每天一次的日报里（同 `qb_999_band` 的失败语义）。
    """
    snap: dict[str, list[int]] = {}
    for root in roots or []:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in ("@eaDir", "#recycle", ".recycle")]
            for fn in filenames:
                if _lg_is_tmp(fn):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                snap[p] = [st.st_size, st.st_mtime_ns]
    return snap


def linkguard_diff(base: dict, cur: dict) -> dict:
    """比对两份快照 → {"changed": [...], "added": [...], "removed": [...]}（升序）。

    · changed：两边都有，但 size 或 mtime 不同 ⇒ **被改写**（要抓的就是这个）
    · added  ：只在 cur 里 ⇒ 新文件（qB 重下常留下 `.!qB` 之类；那些已在
               snapshot 阶段按后缀滤掉，所以这里出现的基本是真新增）
    · removed：只在 base 里 ⇒ 被删（重建链接时会出现，人工操作也会）
    """
    b = base if isinstance(base, dict) else {}
    c = cur if isinstance(cur, dict) else {}
    changed, added = [], []
    for p, v in c.items():
        old = b.get(p)
        if old is None:
            added.append(p)
        elif list(old) != list(v):
            changed.append(p)
    removed = [p for p in b if p not in c]
    return {"changed": sorted(changed), "added": sorted(added),
            "removed": sorted(removed)}


def linkguard_owner(path: str, torrents: list[dict]) -> str | None:
    """这个文件属于哪条种子（按 save_path 前缀匹配）→ 返回 hash，认不出返回 None。

    ★ 前缀要带结尾斜杠再比，否则 `/x/reseed_singles/甲` 会把
      `/x/reseed_singles/甲乙/...` 也吞进去（经典的前缀 bug）。
    ★ 取**最长**匹配：分类目录可能嵌套（`<save_path>` 之间互相是前缀）。
    ★ 两边都先把 `\\` 归一成 `/`：生产跑在 NAS（posix，两边本来就是 `/`），
      但这个判据也会被 Windows 侧的工具调到 —— 归一一次，省掉一整类
      「在 NAS 上对、在 Windows 上认不出」的诡异现象。
    """
    best, best_len = None, -1
    p = path.replace("\\", "/")
    for t in torrents or []:
        sp = (t.get("save_path") or "").replace("\\", "/").rstrip("/")
        if not sp or not p.startswith(sp + "/"):
            continue
        if len(sp) > best_len:
            best, best_len = t.get("hash"), len(sp)
    return best


#: qB 里「静止」的 state —— 除这些之外都算「在动」。
#: ★ `uploading` **必须显式列出来**：它不以 `UP` 结尾，只按 `endswith("UP")` 判
#:   会被当成「正在下载」。这个坑是 2026-09-13 由 tests/test_linkguard.py §④ 抓到的
#:   —— 当时生产上跑着的那把看门狗（watchdog-xp.py）用的是同一个写错的判据，
#:   只是这 628 条恰好全是 `stalledUP`、没有一条 `uploading`，才没被误暂停。
#:   别把这条删了。
#: ★ `checkingUP` **故意不算静止**：它是对**已做种数据**的校验，很可能正是
#:   「校验不通过 → 就地重下」的前奏，而那恰恰是要盯的事。
_LG_IDLE_EXACT = frozenset({"uploading", "error", "unknown"})


def _lg_is_motion(state: str | None) -> bool:
    st = state or ""
    if st.startswith(("paused", "stopped")):
        return False                        # 终态
    if st in _LG_IDLE_EXACT:
        return False
    if st.endswith("UP") and not st.startswith("checking"):
        return False                        # stalledUP / queuedUP / forcedUP …
    return True


def linkguard_inflight(torrents: list[dict]) -> dict:
    """此刻有多少条 cross-seed 种子**不在**静止态（=可能正在校验/重下）。

    ★ 这不是写穿的证据，只是**正在发生**的信号 —— 写穿的证据是
      `linkguard_diff` 的 changed。两者一起看才完整：前者抓当下，后者抓结果。
    ★ 判据故意**按否定写**（列出静止态、其余皆算动）：漏报的坏法（正在写穿却
      看不见）比误报重，同 `qb_999_band` 里 `last_activity or 0` 那条理由。
      所以 `state` 缺失 / 为 None 一律算「在动」。
    """
    hits: list[str] = []
    for t in torrents or []:
        if not _lg_is_motion(t.get("state")):
            continue
        h = t.get("hash")
        if h:
            hits.append(h)
    hits.sort()
    return {"n": len(hits), "hashes": hits, "total": len(torrents or [])}


def parse_alias(items: list[str] | None) -> dict[str, str]:
    """`--indexer-alias 'http://prowlarr:9696/1/api=SiteB'` → {url: name}"""
    out: dict[str, str] = {}
    for it in items or []:
        if "=" in it:
            k, v = it.split("=", 1)
            out[k.strip().rstrip("/")] = v.strip()
    return out


def parse_cadence(spec: str | None) -> dict[str, int]:
    """`--cadence "SiteA=7,SiteB=30"` → {"SiteA": 7, "SiteB": 30}"""
    out: dict[str, int] = {}
    for it in (spec or "").split(","):
        it = it.strip()
        if "=" in it:
            k, v = it.split("=", 1)
            try:
                out[k.strip()] = int(float(v.strip()))
            except ValueError:
                pass
    return out


def apply_batch(pairs, *, limit, batch):
    """按 --limit / --batch 切出一批。

    返回 (这批, 说明文字)；说明为 None 表示参数有误（调用方应报错退出）。

    顺序由 StateStore.todo() 保证（SKIPPED → ERROR → PENDING → UNMATCHED），
    所以"取前 N 条"永远先吃掉最该搜的；一批做完后状态会变（被搜过的离开待办），
    下一批自然接着往下走 —— 因此 **不加 --batch 反复跑同一条命令就能逐批推进**。
    """
    total = len(pairs)
    if not limit:
        return pairs, f"共 {total} 部待搜（未分批，一次全发）"
    nbatch = max(1, -(-total // limit))          # 向上取整
    k = batch or 1
    if k < 1 or k > nbatch:
        return None, f"--batch {k} 超出范围：共 {nbatch} 批（每批 {limit}）"
    lo, hi = (k - 1) * limit, k * limit
    return pairs[lo:hi], (
        f"共 {total} 部待搜 → 每批 {limit}，共 {nbatch} 批；"
        f"本批 = 第 {k} 批（第 {lo + 1}~{min(hi, total)} 部）"
    )


# --------------------------------------------------------------------------- #
# 试点期观测（`e404b1ca#8`）
# --------------------------------------------------------------------------- #
# 用户 2026-09-20 问：「**n 轮后 0 读数，这个 n 怎么来的？**」
# ⇒ 立规矩：**说不出就别说**。`n` 不是拍的，是**从 `mbf` 反推**的：
#
#     mbf 被驱动的实测读数（`SUMMARY §20.9`）：**8 批 / `ok=32` / `Found 0 torrents`**
#
#   · 8 批 —— 它在 `PACKS_DEFAULT` 里（index 1），轮换到它 8 次
#   · ok=32 —— 8 批里共发出 32 次搜索请求（≠ 0：**请求真的发出去了**，
#     所以"0 读数"**不是**"没跑成"）
#   · `Found 0 torrents` —— 站点侧一个单种都没找到
#
# ★★ `n = 8` 于是有了出处：**"一批"是"轮换到它一次"**。取 8 是因为
#   `mbf` 恰好在第 8 批之后拿到了它那份**完整的**读数（4 个季包 × 8 个站
#   的覆盖已经不再增长）—— 换句话说 **8 是"再看下去也不会有新信息"的那个点**，
#   而不是"取个吉利数"。★ 若要改这个默认，**必须换一个同样有出处的数**，
#   并把这个出处的现场记在这里（否则下次没人说得清 n 是怎么来的）。
PILOT_ROUNDS_DEFAULT = 8

#: 试点期里"读数"的**定义**：`newly_seeding > 0`（真有片子被辅种上了）。
#: ★ 为什么**不**用 `ok`（请求成功数）：`mbf` 的 `ok=32` 而 `Found 0 torrents`
#:   —— `ok` 量的是"我们问了几次"，不是"拿到了什么"。用 `ok` 会把
#:   `mbf` 这种**问了 32 次、什么都没得到**的包判成"有产出"，正好判反。
#: ★ 为什么也不用 `matched`：`matched` 是 cross-seed 记账（§26.33 那个 30 倍
#:   偏差的源头）—— 它**漏记**时会把有产出的包判成没产出。`newly_seeding`
#:   来自 qB 的实际做种数变化，是三条里**唯一**不依赖 cross-seed 记账的。


def pilot_verdict(rounds: int, newly_seeding: int, ok: int, *,
                  min_rounds: int = PILOT_ROUNDS_DEFAULT) -> str:
    """试点期的**建议**（**只建议，绝不自动改 `PACKS_DEFAULT`**）。

    返回 `"keep"` / `"no-yield"` / `"too-early"` / `"error"`。

    ★★ 为什么**只建议**：改 `PACKS_DEFAULT` 是**产品决定**（`drive-loop.py:103-132`
      那段注释里写得很硬：「它**不是**一行等着被消除的代码债，**是一个产品决定**」）。
      理由②是**加包会稀释其他包的轮换频率**，理由③是**"找得到" ≠ "搜得到"**。
      ⇒ 这里的输出**只进日报正文**，不给任何自动路径消费。★ 谁若想让它自动改，
      先回去读那段注释 —— 那是**否决过**的，不是漏掉的 TODO。

    ★ 判据（三条，缺一不可）：
      · `too-early`：`rounds < min_rounds` ⇒ **说"还早"比说"没用"诚实** ——
        试点期的第一条纪律是**别在样本不够时下结论**。
      · `error`：`ok == 0` ⇒ **一次请求都没发出去**（站点全在退避 / 网络挂了）
        ⇒ 这是**观测失败**，不是"包没产出"。★ 与 `no-yield` 分开是**必须**的：
        把"没跑成"读成"跑了没产出"会让人**把好包误删**
        （`n/a ≠ 0 ≠ 没事`，`ERR-AI-03`）。
      · `no-yield`：`rounds ≥ min_rounds` **且** `ok > 0` **且** `newly_seeding == 0`
        ⇒ 试够了、真跑过、一个都没拿到。**这时才建议**考虑移出名单。
      · 否则 `keep`（有产出）。
    """
    if rounds < min_rounds:
        return "too-early"
    if ok <= 0:
        return "error"
    if newly_seeding <= 0:
        return "no-yield"
    return "keep"
