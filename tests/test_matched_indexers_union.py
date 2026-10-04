# -*- coding: utf-8 -*-
"""`matched_indexers` 是**单调事实** —— 跨天滚动不许丢、多站要并集（#73）。

钉什么：

  · ★★ **日志跨天滚动后必须保留。** `facts.found` 的唯一输入是**当天**的
    `info.current.log`，而这一列每次 sync 都**重算**。老写法（`{i for _, i in
    matched if i}`）是**整行覆盖** ⇒ 日志一滚动，下一次 sync 就把整列抹成 `[]`；
    而 SEEDING 的行**不会再被搜** ⇒ **永不恢复**。
    生产见证：同一张 605 行的表，09-12 非空 **215 部** → 09-14 **0 部**。
  · **并集**，不是替换 —— 新站匹配到之后，旧站不许被挤掉。
  · 源头**不再造 `"A|B"` 合体标签**；老库里残留的合体标签要**拆开**再并
    （读侧 `_sites()` 是按 JSON 数组逐项取的，不认里面的 `|`）。
  · 对照组：`matched_hashes` / `seeding_count` / `stage` **都没被碰** ——
    单调的**只有** `matched_indexers` 这一列，而 stage 照旧会塌。

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
    STAGE_PENDING, STAGE_SEEDING, StateStore, sync_pack,
)

# ★ 输出强制 UTF-8：GBK 控制台下会在中途炸掉，而**已经过的断言看着全是 ok**。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = "/vol1/movies"
DIR = "Movie.A.2024"
H1 = "234ce088335dbfc36d484215802323b3cd7d7f97"
H2 = "83d4914b1a2c3d4e5f60718293a4b5c6d7e8f901"
T0 = "2026-09-12 03:15:05"

fails = []


def ck(label, got, want):
    ok = got == want
    print(("  ok  " if ok else " FAIL ") + f"{label}: {got!r}")
    if not ok:
        fails.append(f"{label}: got {got!r} want {want!r}")


def found_line(hash8, site, path, when=T0):
    """一行真实的 `[webhook] Found ...`（格式逐字取自生产 verbose 日志）。"""
    return (f"{when}.123 info: [webhook] Found {DIR} [{hash8}...] on {site} "
            f"by MATCH from dataDir ({path}) - injected")


#: 09-14 生产上真实存在的那一行 —— cross-seed 一整天一个匹配都没有。
#: 它就是"跨天滚动之后 `facts.found` 为空"的那一天。
LOG_NO_HIT = "2026-09-14 11:36:01.463 info: [webhook] Found 0 torrents for {\n"

IN_PATH = f"{ROOT}/{DIR}"


def mkstore():
    st = StateStore(os.path.join(tempfile.mkdtemp(), "t.db"), create=True)
    st.upsert_pack("p", ROOT, max_depth=2)
    st.register_dirs("p", [(DIR, IN_PATH)])
    return st


def mid_of(st, dir_name=DIR):
    for r in st.movies("p"):
        if r["dir_name"] == dir_name:
            return r["id"]
    raise KeyError(dir_name)


def row_of(st, mid):
    return st.con.execute("SELECT * FROM movie WHERE id=?", (mid,)).fetchone()


def idx_of(st, mid):
    return json.loads(row_of(st, mid)["matched_indexers"])


def mk_csdb(path, dpath=IN_PATH, hashes=(H1, H2)):
    """合成一个 cross-seed.db（只建 `read_crossseed_db` 真正会读的那 5 张表）。"""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE indexer(id INTEGER PRIMARY KEY, name TEXT, url TEXT,
                             active INTEGER, status TEXT, retry_after INTEGER);
        CREATE TABLE searchee(id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE data(path TEXT, title TEXT);
        CREATE TABLE timestamp(searchee_id INTEGER, indexer_id INTEGER, last_searched INTEGER);
        CREATE TABLE decision(searchee_id INTEGER, decision TEXT, info_hash TEXT);
    """)
    con.execute("INSERT INTO indexer VALUES(1,'HDtime','http://prowlarr:9696/1/api',"
                "1,'OK',NULL)")
    ms = int(datetime.strptime(T0, "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
    con.execute("INSERT INTO searchee VALUES(1,?)", (DIR,))
    if dpath:
        con.execute("INSERT INTO data VALUES(?,?)", (dpath, DIR))
    con.execute("INSERT INTO timestamp VALUES(1,1,?)", (ms,))
    for h in hashes:
        con.execute("INSERT INTO decision VALUES(1,'MATCH',?)", (h,))
    con.commit()
    con.close()
    return path


tmp = tempfile.mkdtemp()
CS_OK = mk_csdb(os.path.join(tmp, "ok.db"))            # 认得出、有 hash
QB = [{"hash": H1}, {"hash": H2}]


def sync(st, csdb=CS_OK, log=LOG_NO_HIT, qb=QB):
    return sync_pack(st, "p", crossseed_db=csdb, log_texts=[log], qbit_torrents=qb)


print("== ① 日志里有 Found 行 → 站名写进去（后面所有断言的起点）==")
st = mkstore()
a = mid_of(st)
sync(st, log=found_line("234ce088", "HDtime", IN_PATH))
ck("stage 真的进了 SEEDING", row_of(st, a)["stage"], STAGE_SEEDING)
ck("matched_indexers 记下了匹配到的站", idx_of(st, a), ["HDtime"])

print()
print("== ② ★★ 跨天滚动：下一次 sync 的日志里**没有** Found 行了 ==")
sync(st)                                    # LOG_NO_HIT —— 就是 09-14 生产上的那一行
ck("★★ 旧值**没被抹掉**（老写法这里是 []）", idx_of(st, a), ["HDtime"])
ck("matched_hashes 也还在（它走 cross-seed.db，不靠日志）",
   json.loads(row_of(st, a)["matched_hashes"]), [H1, H2])
ck("stage 仍是 SEEDING", row_of(st, a)["stage"], STAGE_SEEDING)

print()
print("== ③ 并集：又来一个新站 → 两个都在（不是替换）==")
sync(st, log=found_line("83d4914b", "BTSCHOOL", IN_PATH))
ck("★ 两个站都在", idx_of(st, a), ["BTSCHOOL", "HDtime"])
ck("★ 没有任何元素带 `|`（源头不再造合体标签）",
   [s for s in idx_of(st, a) if "|" in s], [])

print()
print("== ④ 老库残留的 `\"A|B\"` 合体标签 → 拆开再并 ==")
st2 = mkstore()
b = mid_of(st2)
st2.con.execute("UPDATE movie SET matched_indexers=? WHERE id=?",
                ('["HDFans|NanyangPT (南洋)"]', b))
st2.con.commit()
ck("先确认夹具就是那个坏形状", idx_of(st2, b), ["HDFans|NanyangPT (南洋)"])
sync(st2, log=found_line("234ce088", "HDtime", IN_PATH))
ck("★ 拆成三个**个体站名**再并（读侧 `_sites()` 才认得）",
   idx_of(st2, b), ["HDFans", "HDtime", "NanyangPT (南洋)"])

print()
print("== ⑤ 对照：per-站展开**没有**污染 hashes / seeding_count / stage ==")
ck("matched_hashes 仍是 2 个 hash（不是 2 hash × 2 站 = 4）",
   json.loads(row_of(st, a)["matched_hashes"]), [H1, H2])
ck("seeding_count = |hashes ∩ qB| = 2", row_of(st, a)["seeding_count"], 2)
ck("stage 仍是 SEEDING", row_of(st, a)["stage"], STAGE_SEEDING)

print()
print("== ⑥ 对照：searchee 落到**别的包** → stage 照旧塌，只有站点归属是单调的 ==")
drift = mk_csdb(os.path.join(tmp, "drift.db"), dpath="/vol1/OTHER/" + DIR)
sync(st, csdb=drift)
ck("stage 塌回 PENDING（本次改动没碰 stage）", row_of(st, a)["stage"], STAGE_PENDING)
ck("matched_hashes 仍被整行覆盖成空（它没改成并集）",
   json.loads(row_of(st, a)["matched_hashes"]), [])
ck("★★ 而 matched_indexers **保留** —— 单调的只有这一列",
   idx_of(st, a), ["BTSCHOOL", "HDtime"])

st.close(); st2.close()
print()
if fails:
    print(f"!!! {len(fails)} 个失败")
    for f in fails:
        print("   " + f)
    sys.exit(1)
print("全部通过")
