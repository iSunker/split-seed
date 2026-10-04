# -*- coding: utf-8 -*-
"""`seeding_count` 的第二条判据（qB 的 `name`）—— 做种判定与 cross-seed 记账**解绑**。

为什么要钉这个（`e404b1ca#5`，SUMMARY §26.33）
==============================================
原先 `seeding_count = len(matched_hashes & seeding_hashes)`，而 `matched_hashes`
**唯一的来源是 cross-seed 的 `decision` 表**。于是：

    cross-seed 这次没给它写 decision  ⇒  做种被判成**没做种**

用户凭常识把它抓出来了：qB :3060 里 **2141 条做种 / 544 部唯一片**，而
`state.db` 说 `frds-top250-2024` 只做种 **13** 部（真实 **422/486**）——
**低了约 30 倍**。后果是双向静默的：已在做种的片子被判 `UNMATCHED` ⇒
**永不停止重搜**（`DONE_STAGES` 那条"搜到了就不重搜"的规矩形同虚设）。

★ 本文件钉的不是"算得对"，而是三个**看着像放宽判据、其实是纠正判据**的分界：

  ① ★★ **`name` 命中必须单独就能判 SEEDING** —— 这是这次修的全部内容。
     ★ 反向断言（阴性对照）也要有：**`name` 不在集合里时，不许靠 hash 蒙混**。
  ② ★★ **`seeding_names=None`（老调用点）必须退回旧行为** —— 不许静默放宽。
     判据是"同一个输入、只差这个参数 ⇒ 结果不同"，差异只可能来自它。
  ③ ★★ **`max(..., 1)` 而不是 `+1`** —— 同一部片在 qB 里可能有多条（多个站各一份），
     但 `pack_seeding_total` 数的是**行数**。写成 `+1` 会让两者口径打架。

★★ 变异验证（**实测过，数字是当场跑出来的**）：
  · 把 `if seeding_names and row["dir_name"] in seeding_names:` 整段短路掉（`if False and ...`）
    ⇒ 红 **6** 条（①③④⑥⑧ 各组各一条，含两条端到端）；
  · 把 `max(seeding_count, 1)` 改成 `seeding_count + 1`
    ⇒ 红 **1** 条（⑤ 那条，`seeding_count` 被写成 2 而期望 1）；
  · 把 `None` 也放宽（改成 `seeding_names or [row["dir_name"]]`）
    ⇒ 红 **1** 条（③ 那条「旧行为一字不变」）。

素材全部合成：SQLite 在 `tempfile.mkdtemp()` 里现造，没有网络、没有 NAS、没有真库。
"""
import os
import pathlib
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
# ★ 2026-10-05（B2）：容器代码收进 data/ ⇒ orchestrator / drive-loop 源码
#   都在 data/ 下。把 data/ 也加进 import 搜索路径（本文件上一行插的是仓库根）。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "data"))

from orchestrator.state import (            # noqa: E402
    STAGE_MATCHED, STAGE_PENDING, STAGE_SEEDING, STAGE_UNMATCHED,
    StateStore, sync_pack,
)

# ★ 输出强制 UTF-8：GBK 控制台下会中途炸掉，而**已过的断言看着全是 ok** —— 极易误判。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = "/vol1/movies"
T0 = "2026-09-01 00:00:00"

fails = []


def ck(label, got, want):
    ok = got == want
    print(("  ok  " if ok else " FAIL ") + f"{label}: {got!r}")
    if not ok:
        fails.append(f"{label}: got {got!r} want {want!r}")


def mkstore(pack="p", dirs=("Movie.A.2024", "Movie.B.2019", "Movie.C.2021")):
    st = StateStore(os.path.join(tempfile.mkdtemp(), "t.db"), create=True)
    st.upsert_pack(pack, ROOT, max_depth=2)
    st.register_dirs(pack, [(d, f"{ROOT}/{d}") for d in dirs])
    return st


def mid_of(st, pack, dir_name):
    for r in st.movies(pack):
        if r["dir_name"] == dir_name:
            return r["id"]
    raise KeyError(dir_name)


def stage_of(st, pack, dir_name):
    for r in st.movies(pack):
        if r["dir_name"] == dir_name:
            return r["stage"]
    raise KeyError(dir_name)


def mk_csdb(path, entries, indexers=(("HDtime", "http://prowlarr:9696/1/api", 1),)):
    """合成一个 cross-seed.db（只建 `read_crossseed_db` 真正会读的那 5 张表）。

    `entries`: [(searchee 名, data.path 或 None, info_hash 或 None)]
      * hash=None → `decision` 表里**没有**匹配结论（★ 本次修的就是这一格）
    """
    import datetime as _dt
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
    ms = int(_dt.datetime.strptime(T0, "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
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


print("== ① sync_movie：qB 里有同名种子 ⇒ SEEDING（**即使 cross-seed 没写 decision**）==")
st = mkstore()
a = mid_of(st, "p", "Movie.A.2024")
# ★ 关键：matched=[]（= cross-seed 没记账、没有 hash），只有 name 这条路能救它
ck("matched 为空 + name 命中 → SEEDING",
   st.sync_movie(a, searched_indexers={"HDtime"}, skipped_indexers=set(),
                 matched=[], seeding_hashes=set(),
                 seeding_names={"Movie.A.2024"},
                 indexers_now=["HDtime"]),
   STAGE_SEEDING)

print()
print("== ② 阴性对照 ①：matched 为空 + **name 不在集合里** ⇒ 不许判 SEEDING ==")
st = mkstore()
b = mid_of(st, "p", "Movie.B.2019")
ck("matched 空 + name 不命中 → UNMATCHED（不是 SEEDING）",
   st.sync_movie(b, searched_indexers={"HDtime"}, skipped_indexers=set(),
                 matched=[], seeding_hashes=set(),
                 seeding_names={"别的片.2020"},
                 indexers_now=["HDtime"]),
   STAGE_UNMATCHED)

print()
print("== ③ `seeding_names=None`（老调用点）必须退回旧行为 —— 不许静默放宽 ==")
st = mkstore()
c = mid_of(st, "p", "Movie.C.2021")
ck("matched 空 + 不给 seeding_names → UNMATCHED（旧行为，一字不变）",
   st.sync_movie(c, searched_indexers={"HDtime"}, skipped_indexers=set(),
                 matched=[], seeding_hashes=set(),
                 seeding_names=None,
                 indexers_now=["HDtime"]),
   STAGE_UNMATCHED)
# ★ 同一部片、只差这一个参数 ⇒ 结果不同。差异只可能来自它。
st2 = mkstore()
c2 = mid_of(st2, "p", "Movie.C.2021")
ck("同一输入、只差 seeding_names ⇒ SEEDING（分辨力就在这个参数上）",
   st2.sync_movie(c2, searched_indexers={"HDtime"}, skipped_indexers=set(),
                  matched=[], seeding_hashes=set(),
                  seeding_names={"Movie.C.2021"},
                  indexers_now=["HDtime"]),
   STAGE_SEEDING)

print()
print("== ④ hash 那条路**保留**：decision 有值、hash 对得上 ⇒ 仍然 SEEDING ==")
st = mkstore()
d = mid_of(st, "p", "Movie.A.2024")
ck("matched 有 hash + seeding_hashes 命中 → SEEDING（旧路没被拆掉）",
   st.sync_movie(d, searched_indexers={"HDtime"}, skipped_indexers=set(),
                 matched=[("h1", "HDtime")], seeding_hashes={"h1"},
                 seeding_names=None,
                 indexers_now=["HDtime"]),
   STAGE_SEEDING)

print()
print("== ⑤ ★★ 计数口径：`seeding_count` 用 max(...,1)，**不是 +1** ==")
st = mkstore()
e = mid_of(st, "p", "Movie.A.2024")
st.sync_movie(e, searched_indexers={"HDtime"}, skipped_indexers=set(),
              matched=[("h1", "HDtime")], seeding_hashes={"h1"},
              seeding_names={"Movie.A.2024"},
              indexers_now=["HDtime"])
row = st.con.execute("SELECT seeding_count, stage FROM movie WHERE id=?", (e,)).fetchone()
# ★ hash 路给 1、name 路也只给 max(...,1) ⇒ 合起来仍是 1。
#   若写成 `+1` 这条会是 2 —— 而 `pack_seeding_total` 数的是**行数**，
#   两者打架时"做种 N 部"与"总数 M 部"会互相打脸。
ck("两条路都命中 ⇒ seeding_count 仍是 1（不是 2）", row["seeding_count"], 1)
ck("且 stage 是 SEEDING", row["stage"], STAGE_SEEDING)

print()
print("== ⑥ 端到端：sync_pack 从 qB 拿到同名种子 ⇒ 整包计数涨上来 ==")
st = mkstore(dirs=("Movie.A.2024", "Movie.B.2019"))
csdb = mk_csdb(os.path.join(tempfile.mkdtemp(), "cs.db"),
               [("Movie.A.2024", None, None),      # ★ 无 path、无 decision
                ("Movie.B.2019", None, None)])
# qB 里两个种子的 name 与目录名同名，但 **cross-seed 的 decision 表是空的**
qb = [{"hash": "", "name": "Movie.A.2024"},
      {"hash": "", "name": "Movie.B.2019"}]
rep = sync_pack(st, "p", crossseed_db=csdb, qbit_torrents=qb,
                indexers_override=["HDtime"])
ck("rep.seeding == 2（旧判据下会是 0）", rep.seeding, 2)
ck("A 是 SEEDING", stage_of(st, "p", "Movie.A.2024"), STAGE_SEEDING)
ck("B 是 SEEDING", stage_of(st, "p", "Movie.B.2019"), STAGE_SEEDING)

print()
print("== ⑦ 阴性对照 ②：qB 名字对不上 ⇒ 端到端也不许涨 ==")
st = mkstore(dirs=("Movie.A.2024", "Movie.B.2019"))
rep = sync_pack(st, "p", crossseed_db=csdb,
                qbit_torrents=[{"hash": "", "name": "完全不相干.2020"}],
                indexers_override=["HDtime"])
ck("rep.seeding == 0", rep.seeding, 0)
ck("A 不是 SEEDING", stage_of(st, "p", "Movie.A.2024") != STAGE_SEEDING, True)

print()
print("== ⑧ 去扩展名那条路：qB 里是**文件**（`X.mkv`）也认 ==")
st = mkstore(dirs=("Movie.A.2024",))
rep = sync_pack(st, "p", crossseed_db=csdb,
                qbit_torrents=[{"hash": "", "name": "Movie.A.2024.mkv"}],
                indexers_override=["HDtime"])
ck("`X.mkv` ⇒ 认到 `X`（实测 frds 有 9 部是这种形状）",
   rep.seeding, 1)

print()
if fails:
    print("FAILED %d:" % len(fails))
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("全过")
