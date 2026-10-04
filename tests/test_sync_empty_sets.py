# -*- coding: utf-8 -*-
"""`sync_movie` 收到**四个空集**之后会变成什么 —— 离线合成，不碰农场。

为什么要钉这个：`sync_movie` 是**整行覆盖**写（stage / searched / indexers /
matched / seeding_count 全部重写，只有 `indexer_seen` 与 `attempts` 是合并的）。
只要有一批同步**没能把某部片子的 searchee 认出来**，它这一次拿到的就是四个空集，
于是它会被原样写回成 PENDING —— 看起来像"从没搜过"，**不是**报错、**不是**删行。

而 `indexer_seen` 是合并保留的，所以 `next_retry_at` 会算到**未来**：
一部 SEEDING 的片子塌成 PENDING 之后，要等一整个周期才会被重搜，
期间它既不在做种、也不在重试 —— 这正是"额度被吃掉"的形状。

素材全部合成：SQLite 在 `tempfile.mkdtemp()` 里现造，没有网络、没有 NAS、没有真库。
"""
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
# ★ 2026-10-05（B2）：容器代码收进 data/ ⇒ orchestrator / drive-loop 源码
#   都在 data/ 下。把 data/ 也加进 import 搜索路径（本文件上一行插的是仓库根）。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "data"))

from orchestrator.state import (            # noqa: E402
    ALL_STAGES, DEFAULT_CADENCE_DAYS, STAGE_MATCHED, STAGE_PENDING,
    STAGE_SEEDING, STAGE_SKIPPED, STAGE_UNMATCHED, StateStore, compute_stage,
    sync_pack,
)

# ★ 输出强制 UTF-8：GBK 控制台下 ⑪(U+246A) 编不出去，会在中途炸掉，
#   而**已经过的断言看着全是 ok** —— 极易误判成代码坏了。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = "/vol1/movies"

# ★★ 这一节**必须把时钟冻住**，不能靠「写死一个 T0」。
#
#   `next_due_at`（orchestrator/state.py）的收尾是：
#       return soonest if soonest > now else None     # 「已经该搜的返回 None」
#   所以写死的 T0 一旦让 `T0 + 周期` 落进过去，期望值就从字符串**变成 None** ——
#   整节无端变红。2026-09-15 起就是这样，红了整整两天没人发现（一个常红的测试
#   会把下一次真失败淹掉，正是本仓反复记档的那件事）。
#
#   ★ 把 now 钉死之后，这一节的语义（"软锁还剩一天"）与真实日期**彻底脱钩**，
#     不会烂。换日期只是把引信拉长，不是修。
import orchestrator.state as _S             # noqa: E402


class _FrozenNow(datetime):
    """`datetime.now()` 永远返回 2026-09-10 12:00（其余行为不变）。"""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 10, 12, 0, 0)


_S.datetime = _FrozenNow

T_NOW = "2026-09-10 12:00:00"               # 冻住的"现在"
T0 = "2026-09-01 00:00:00"                  # 上次搜索时间（T_NOW 之前 9 天）
T_NEXT = "2026-09-15 00:00:00"              # T0 + DEFAULT_CADENCE_DAYS（仍 > T_NOW）

fails = []


def ck(label, got, want):
    ok = got == want
    print(("  ok  " if ok else " FAIL ") + f"{label}: {got!r}")
    if not ok:
        fails.append(f"{label}: got {got!r} want {want!r}")


def mkstore(pack="p", dirs=("Movie.A.2024", "Movie.B.2019", "Movie.C.2021")):
    # create=True：夹具建库（生产默认不建，见 StateStore.__init__）
    st = StateStore(os.path.join(tempfile.mkdtemp(), "t.db"), create=True)
    st.upsert_pack(pack, ROOT, max_depth=2)
    st.register_dirs(pack, [(d, f"{ROOT}/{d}") for d in dirs])
    return st


def mid_of(st, pack, dir_name):
    for r in st.movies(pack):
        if r["dir_name"] == dir_name:
            return r["id"]
    raise KeyError(dir_name)


def row_of(st, mid):
    return st.con.execute("SELECT * FROM movie WHERE id=?", (mid,)).fetchone()


def mk_csdb(path, entries, indexers=(("HDtime", "http://prowlarr:9696/1/api", 1),)):
    """合成一个 cross-seed.db（只建 `read_crossseed_db` 真正会读的那 5 张表）。

    `entries`: [(searchee 名, data.path 或 None, info_hash 或 None)]
      * path=None → 相当于 `data` 表里查不到这条（老库 / 名字对不上）
      * hash=None → `decision` 表里没有匹配结论
    """
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE indexer(id INTEGER PRIMARY KEY, name TEXT, url TEXT,
                             active INTEGER, status TEXT, retry_after INTEGER);
        CREATE TABLE searchee(id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE data(path TEXT, title TEXT);
        CREATE TABLE timestamp(searchee_id INTEGER, indexer_id INTEGER, last_searched INTEGER);
        CREATE TABLE decision(searchee_id INTEGER, decision TEXT, info_hash TEXT);
    """)
    for iid, (nm, url, act) in enumerate(indexers, start=1):
        con.execute("INSERT INTO indexer VALUES(?,?,?,?,'OK',NULL)", (iid, nm, url, act))
    ms = int(datetime.strptime(T0, "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
    for sid, (name, dpath, ihash) in enumerate(entries, start=1):
        con.execute("INSERT INTO searchee VALUES(?,?)", (sid, name))
        if dpath:
            con.execute("INSERT INTO data VALUES(?,?)", (dpath, name))
        con.execute("INSERT INTO timestamp VALUES(?,?,?)", (sid, 1, ms))
        if ihash:
            con.execute("INSERT INTO decision VALUES(?,?,?)", (sid, "MATCH", ihash))
    con.commit()
    con.close()
    return path


print(f"== ① compute_stage：四个空集是**唯一**落到 PENDING 的输入（纯函数）==")
ck("四空集 → PENDING",
   compute_stage(seeding_count=0, matched_count=0,
                 searched_indexers=set(), skipped_indexers=set()), STAGE_PENDING)
ck("seeding 非空 → SEEDING",
   compute_stage(seeding_count=1, matched_count=0,
                 searched_indexers=set(), skipped_indexers=set()), STAGE_SEEDING)
ck("matched 非空 → MATCHED",
   compute_stage(seeding_count=0, matched_count=1,
                 searched_indexers=set(), skipped_indexers=set()), STAGE_MATCHED)
ck("searched 非空 → UNMATCHED",
   compute_stage(seeding_count=0, matched_count=0,
                 searched_indexers={"HDtime"}, skipped_indexers=set()), STAGE_UNMATCHED)
ck("只有 skipped 非空 → SKIPPED",
   compute_stage(seeding_count=0, matched_count=0,
                 searched_indexers=set(), skipped_indexers={"HDtime"}), STAGE_SKIPPED)

print()
print("== ② sync_movie：SEEDING 收到四个空集 → PENDING（是降级，不是删行）==")
st = mkstore()
a = mid_of(st, "p", "Movie.A.2024")
ck("先真的进 SEEDING",
   st.sync_movie(a, searched_indexers={"HDtime"}, skipped_indexers=set(),
                 matched=[("h1", "HDtime")], seeding_hashes={"h1"},
                 indexers_now=["HDtime"], searched_at={"HDtime": T0}), STAGE_SEEDING)
ck("再喂四个空集 → PENDING",
   st.sync_movie(a, searched_indexers=set(), skipped_indexers=set(),
                 matched=[], seeding_hashes=set(),
                 indexers_now=["HDtime"]), STAGE_PENDING)

row = row_of(st, a)
ck("行还在（降级不是删行）", row is not None, True)
ck("seeding_count 归零", row["seeding_count"], 0)
ck("matched_hashes 被覆盖成空", json.loads(row["matched_hashes"]), [])
ck("★ searched_indexers 也被清空 —— 连「搜过」这件事都没了",
   json.loads(row["searched_indexers"]), [])
ck("attempts 没有虚增（空集不算一次尝试）", row["attempts"], 1)
ck("流水把两次变档都记下了（PENDING→SEEDING 那次也是变档）",
   [r["note"] for r in st.con.execute(
       "SELECT note FROM attempt WHERE movie_id=? AND kind='sync' ORDER BY id", (a,))],
   ["PENDING → SEEDING", "SEEDING → PENDING"])

print()
print("== ③ 被保留下来的 indexer_seen，把 next_retry_at 推到**未来** ==")
ck("indexer_seen 仍是合并保留的", json.loads(row["indexer_seen"]), {"HDtime": T0})
ck(f"next_retry_at = T0 + {DEFAULT_CADENCE_DAYS} 天", row["next_retry_at"], T_NEXT)
ck("★ 它现在既不 SEEDING 也不重搜 —— 要等到那个时刻才轮到它",
   row["next_retry_at"] is not None and row["next_retry_at"] > T_NOW,
   True)

print()
print("== ④ 对照：同样是 PENDING，但**没有**历史 → 立刻可搜 ==")
st2 = mkstore(dirs=("Movie.D.2024",))
d = mid_of(st2, "p", "Movie.D.2024")
ck("冷启动四空集 → PENDING",
   st2.sync_movie(d, searched_indexers=set(), skipped_indexers=set(),
                  matched=[], seeding_hashes=set(), indexers_now=["HDtime"]),
   STAGE_PENDING)
ck("没有 indexer_seen → next_retry_at 为空（现在就该搜）",
   row_of(st2, d)["next_retry_at"], None)
ck("★ 两个 PENDING 的差别只来自 indexer_seen：一个有软锁、一个没有",
   (row_of(st, a)["stage"] == row_of(st2, d)["stage"] == STAGE_PENDING)
   and row_of(st, a)["next_retry_at"] != row_of(st2, d)["next_retry_at"], True)

print()
print("== ⑤ 端到端：searchee 落到**别的包** → SEEDING 塌成 PENDING，而报告**不响** ==")
st3 = mkstore(dirs=("Movie.A.2024",))
tmp = tempfile.mkdtemp()
good = mk_csdb(os.path.join(tmp, "good.db"),
               [("Movie.A.2024", f"{ROOT}/Movie.A.2024", "h1")])
rep0 = sync_pack(st3, "p", crossseed_db=good, qbit_torrents=[{"hash": "h1"}])
ck("先真的进 SEEDING（后面所有断言的前提）",
   row_of(st3, mid_of(st3, "p", "Movie.A.2024"))["stage"], STAGE_SEEDING)
ck("报告也认了做种", rep0.seeding, 1)

drift = mk_csdb(os.path.join(tmp, "drift.db"),
                [("Movie.A.2024", "/vol1/OTHER/Movie.A.2024", "h1")])   # ← 路径漂到别的包
rep1 = sync_pack(st3, "p", crossseed_db=drift, qbit_torrents=[{"hash": "h1"}])
ck("★ 塌成 PENDING", row_of(st3, mid_of(st3, "p", "Movie.A.2024"))["stage"], STAGE_PENDING)
ck("★ 报告里没有任何一格说「有一部片子被丢了」：unresolved", rep1.unresolved, 0)
ck("   from_db 也归零（那条 searchee 根本没进统计）", rep1.from_db, 0)
ck("   seeding 从 1 变 0，而报告只说「当前 0」，没说「刚刚丢了 1」", rep1.seeding, 0)

print()
print("== ⑥ 对照：同一批里，**认不出的名字**会被计数器抓出来 ==")
ghost = mk_csdb(os.path.join(tmp, "ghost.db"),
                [("Ghost.Movie.2001", None, "h2")])     # ← 名字对不上任何目录
rep2 = sync_pack(st3, "p", crossseed_db=ghost, qbit_torrents=[{"hash": "h1"}])
ck("★ unresolved 抓到了（=1）", rep2.unresolved, 1)
ck("  而这一批里 Movie.A.2024 同样塌成 PENDING",
   row_of(st3, mid_of(st3, "p", "Movie.A.2024"))["stage"], STAGE_PENDING)
ck("★★ ⑤ 和 ⑥ 后果一样，只有 ⑥ 会喊 —— 这就是「一条计数器 / 两条静默通道」",
   (rep1.unresolved, rep2.unresolved), (0, 1))

print()
print("== ⑦ 收口：塌下去之后，它要等满一个周期才轮得到 ==")
r3 = row_of(st3, mid_of(st3, "p", "Movie.A.2024"))
ck("indexer_seen 跨两轮 sync 都没丢", json.loads(r3["indexer_seen"]), {"HDtime": T0})
ck("next_retry_at 仍是未来那一刻", r3["next_retry_at"], T_NEXT)
ck("stage 是 PENDING（看着像「还没搜过」），实际上额度已经烧掉一轮",
   r3["stage"], STAGE_PENDING)

st.close(); st2.close(); st3.close()
print()
if fails:
    print(f"!!! {len(fails)} 个失败")
    for f in fails:
        print("   " + f)
    sys.exit(1)
print(f"全部通过（阶段全集: {' '.join(ALL_STAGES)}）")
