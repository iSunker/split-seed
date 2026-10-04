# -*- coding: utf-8 -*-
"""`movie.can_publish`（可发种状态枚举）+ `reseed-state mark` 子命令的回归。

## 为什么这个文件必须存在

用户指令（2026-10-02，本轮才落地）：
  > 给库里 UNMATCHED 的种子加一个字段「可发种」（未来自动发种用）。

它的**全部风险**在于「**和 `stage` 混在一起**」：只要有人图省事把它塞进
`ALL_STAGES`，那么 `todo()` / `sync` / 出勤率分母会**全错**，而**症状完全看不出来**
（计数看着都正常）。所以本文件的重心是**正交性**：

  ① `can_publish` 是**独立枚举**，**不在** `ALL_STAGES`（else 分母错）；
  ② `set_can_publish` **只动 can_publish**，不碰 stage/attempts/next_retry_at；
  ③ `register_dirs`（重扫）**不重置** can_publish（else 每次 sync 把它抹回 NO）；
  ④ 老库迁移后旧行 = `NO`、迁移**幂等**（跑两次不炸）；
  ⑤ 非法取值**拒绝**（白名单）；
  ⑥ CLI `mark` 的三种用法（`--dir` / `--all-unmatched` / `--dry-run`）与
     「只动 UNMATCHED」这条**判据**。

## 为什么 CLI 那几段用**子进程**跑（而不是 import）
`reseed-state.py` 是**带连字符**的脚本名，import 不了；且它 `from orchestrator import …`
依赖 `data/` 在 `PYTHONPATH` 上（容器里就是这么跑的）。子进程是最贴近真实的形状。
★ 夹具路径全用 `pathlib`，**不把 Windows 路径塞进 shell**（Git-Bash 的 `/tmp`
  与 Python 的 `/tmp` 解析不同 —— 实测因此假红过一次）。
"""
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
DATA = REPO / "data"
SCRIPT = DATA / "drive-loop" / "scripts" / "reseed-state.py"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(DATA))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from orchestrator import state as S  # noqa: E402

fails: list[str] = []


def ck(label, got, want):
    ok = got == want
    print(("  ok  " if ok else " FAIL ") + f"{label}: {got!r}  (want {want!r})")
    if not ok:
        fails.append(f"{label}: got {got!r} want {want!r}")


def ck_true(label, cond, detail=""):
    print(("  ok  " if cond else " FAIL ") + f"{label}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        fails.append(f"{label} [{detail}]")


TMP = pathlib.Path(tempfile.mkdtemp(prefix="canpub-"))
DB = TMP / "state.db"


def fresh_db():
    if DB.exists():
        DB.unlink()


def mkstore():
    return S.StateStore(str(DB), create=True)


# --------------------------------------------------------------------------- #
print("== ① 常量是**独立枚举**，绝不能进 ALL_STAGES ==")
ck("CAN_PUBLISH_VALUES 是 4 个", len(S.CAN_PUBLISH_VALUES), 4)
ck("含 NO", S.CAN_PUBLISH_NO in S.CAN_PUBLISH_VALUES, True)
ck("含 CANDIDATE", S.CAN_PUBLISH_CANDIDATE in S.CAN_PUBLISH_VALUES, True)
ck("含 PUBLISHED", S.CAN_PUBLISH_PUBLISHED in S.CAN_PUBLISH_VALUES, True)
ck("含 EXCLUDED", S.CAN_PUBLISH_EXCLUDED in S.CAN_PUBLISH_VALUES, True)
# ★★ 正交性核心：can_publish 的任一取值都**不在** ALL_STAGES（否则 todo/sync 分母错）
_overlap = set(S.CAN_PUBLISH_VALUES) & set(S.ALL_STAGES)
ck("★★ 与 ALL_STAGES **零重叠**（正交性；重叠 ⇒ 分母全错）", _overlap, set())
# 前提：ALL_STAGES 确实非空（否则上面那条恒真）
ck_true("前提：ALL_STAGES 非空", len(S.ALL_STAGES) > 3, str(S.ALL_STAGES))

print("== ② 新库有列，默认 NO ==")
fresh_db()
with mkstore() as st:
    cols = {r["name"] for r in st.con.execute("PRAGMA table_info(movie)")}
    ck_true("movie 表含 can_publish 列", "can_publish" in cols, sorted(cols))
    st.upsert_pack("P", "/x")
    st.register_dirs("P", [("A", "/x/A"), ("B", "/y/B")])
    rows = {r["dir_name"]: r["can_publish"] for r in st.movies("P")}
    ck("新登记行默认 NO", rows, {"A": "NO", "B": "NO"})

print("== ③ set_can_publish：合法改、非法拒、返回 rowcount ==")
with mkstore() as st:
    ck("合法设置返回 1", st.set_can_publish("P", "A", S.CAN_PUBLISH_CANDIDATE), 1)
    ck("不存在的 dir 返回 0", st.set_can_publish("P", "ZZ", S.CAN_PUBLISH_NO), 0)
    _e = None
    try:
        st.set_can_publish("P", "A", "BOGUS")
    except ValueError as ex:
        _e = str(ex)
    ck_true("非法取值抛 ValueError", _e is not None, repr(_e))
    ck("  且非法值**没有写进去**（A 仍是 CANDIDATE）",
       st.movie_by_dir("P", "A")["can_publish"], S.CAN_PUBLISH_CANDIDATE)

print("== ④ ★★★ 正交：set_can_publish **只动 can_publish** ==")
with mkstore() as st:
    before = dict(st.movie_by_dir("P", "B"))
    st.set_can_publish("P", "B", S.CAN_PUBLISH_PUBLISHED)
    after = dict(st.movie_by_dir("P", "B"))
    for k in ("stage", "attempts", "last_attempt_at", "next_retry_at",
              "seeding_count", "path"):
        ck(f"  {k} 未被改动", after[k], before[k])
    ck("  can_publish 确实变了", after["can_publish"], S.CAN_PUBLISH_PUBLISHED)

print("== ⑤ ★★ register_dirs（重扫）**不重置** can_publish ==")
with mkstore() as st:
    # A 现在是 CANDIDATE（③ 里设过）。重扫同一批（含改名后的同一 dir）。
    st.register_dirs("P", [("A", "/x/A-RESCAN"), ("C", "/z/C")])
    ck("重扫后 A 仍是 CANDIDATE（没被抹回 NO）",
       st.movie_by_dir("P", "A")["can_publish"], S.CAN_PUBLISH_CANDIDATE)
    ck("  但 path 被更新了（重扫该做的它做了）",
       st.movie_by_dir("P", "A")["path"], "/x/A-RESCAN")
    ck("  新登记的 C 是 NO", st.movie_by_dir("P", "C")["can_publish"], "NO")

print("== ⑥ can_publish_counts 分档 ==")
with mkstore() as st:
    cnt = st.can_publish_counts("P")
    # A=CANDIDATE, B=PUBLISHED, C=NO
    ck("  NO 档", cnt[S.CAN_PUBLISH_NO], 1)
    ck("  CANDIDATE 档", cnt[S.CAN_PUBLISH_CANDIDATE], 1)
    ck("  PUBLISHED 档", cnt[S.CAN_PUBLISH_PUBLISHED], 1)
    ck("  EXCLUDED 档（无人）", cnt[S.CAN_PUBLISH_EXCLUDED], 0)

print("== ⑦ 老库迁移：手工造**无 can_publish 列**的老 movie 表，旧行应变 NO；且幂等 ==")
fresh_db()
_con = sqlite3.connect(str(DB))
_con.executescript("""
CREATE TABLE pack (name TEXT PRIMARY KEY, root TEXT NOT NULL);
CREATE TABLE movie (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pack TEXT NOT NULL REFERENCES pack(name) ON DELETE CASCADE,
  dir_name TEXT NOT NULL, path TEXT NOT NULL,
  stage TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
  last_attempt_at TEXT, next_retry_at TEXT,
  searched_indexers TEXT NOT NULL DEFAULT '[]', indexer_seen TEXT NOT NULL DEFAULT '{}',
  matched_indexers TEXT NOT NULL DEFAULT '[]', matched_hashes TEXT NOT NULL DEFAULT '[]',
  seeding_count INTEGER NOT NULL DEFAULT 0, last_error TEXT, note TEXT, updated_at TEXT,
  UNIQUE(pack, dir_name));
INSERT INTO pack VALUES ('OLD','/o');
INSERT INTO movie(pack,dir_name,path,stage) VALUES ('OLD','OldMovie','/o/OldMovie','UNMATCHED');
""")
_con.commit()
_con.close()
with mkstore() as st:      # 打开即触发 _migrate
    ck("迁移后旧行 can_publish = NO",
       st.movie_by_dir("OLD", "OldMovie")["can_publish"], "NO")
    ck("  旧行的 stage 没被迁移碰坏",
       st.movie_by_dir("OLD", "OldMovie")["stage"], "UNMATCHED")
with mkstore() as st:      # 再开一次（幂等，不炸）
    ck("二次打开仍 OK（迁移幂等）",
       st.movie_by_dir("OLD", "OldMovie")["can_publish"], "NO")


# --------------------------------------------------------------------------- #
# CLI（子进程）
# --------------------------------------------------------------------------- #
def run_cli(*args):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(DATA) + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("PATH", "/usr/bin:/bin")
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(DB), *args],
        cwd=str(REPO), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120, env=env,
    )


def seed_cli_db():
    fresh_db()
    with mkstore() as st:
        st.upsert_pack("P", "/x")
        st.register_dirs("P", [("A", "/x/A"), ("B", "/y/B"), ("C", "/x/C")])
        st.con.execute("UPDATE movie SET stage='UNMATCHED' WHERE dir_name IN ('A','B')")
        st.con.execute("UPDATE movie SET stage='SEEDING' WHERE dir_name='C'")
        st.con.commit()


print("== ⑧ CLI：`mark --all-unmatched` 只动 UNMATCHED ==")
seed_cli_db()
r = run_cli("mark", "--pack", "P", "--all-unmatched", "--set", "candidate")
ck("退出码 0", r.returncode, 0)
with mkstore() as st:
    ck("A（UNMATCHED）→ CANDIDATE", st.movie_by_dir("P", "A")["can_publish"], "CANDIDATE")
    ck("B（UNMATCHED）→ CANDIDATE", st.movie_by_dir("P", "B")["can_publish"], "CANDIDATE")
    ck("C（SEEDING）**没被动**", st.movie_by_dir("P", "C")["can_publish"], "NO")
    ck("  C 的 stage 也没变", st.movie_by_dir("P", "C")["stage"], "SEEDING")

print("== ⑨ CLI：`--dry-run` 不写库 ==")
seed_cli_db()
r = run_cli("mark", "--pack", "P", "--all-unmatched", "--set", "published", "--dry-run")
ck("退出码 0", r.returncode, 0)
ck_true("dry-run 打了 [dry-run]", "[dry-run]" in r.stdout, r.stdout[-200:])
with mkstore() as st:
    ck("dry-run 后 A 仍是 NO", st.movie_by_dir("P", "A")["can_publish"], "NO")

print("== ⑩ CLI：`--dir` 单件；非法取值 rc=2；互斥参数 rc=2 ==")
seed_cli_db()
r = run_cli("mark", "--pack", "P", "--dir", "A", "--set", "excluded")
ck("单件设置退出码 0", r.returncode, 0)
with mkstore() as st:
    ck("A → EXCLUDED", st.movie_by_dir("P", "A")["can_publish"], "EXCLUDED")
    ck("B 未动（仍 NO）", st.movie_by_dir("P", "B")["can_publish"], "NO")

r = run_cli("mark", "--pack", "P", "--dir", "A", "--set", "bogus")
ck("非法取值 rc=2", r.returncode, 2)
r = run_cli("mark", "--pack", "P", "--dir", "A", "--all-unmatched", "--set", "NO")
ck("--dir 与 --all-unmatched 互斥 rc=2", r.returncode, 2)
r = run_cli("mark", "--pack", "P", "--set", "NO")
ck("既无 --dir 又无 --all-unmatched ⇒ rc=2", r.returncode, 2)

print("== ⑪ CLI：`show` 显示 can_publish（只读）==")
r = run_cli("show", "--pack", "P", "--movie", "A")
ck("show 退出码 0", r.returncode, 0)
ck_true("show 输出里有 can_publish 行", "can_publish" in r.stdout, r.stdout[-200:])
ck_true("  且值是 EXCLUDED（⑩ 设的）", "EXCLUDED" in r.stdout, r.stdout[-200:])

print()
if fails:
    print(f"!!! {len(fails)} 个失败")
    for f in fails:
        print("   " + f)
    sys.exit(1)
print("全部通过")
