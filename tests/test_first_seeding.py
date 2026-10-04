# -*- coding: utf-8 -*-
"""`StateStore.first_seeding_between()`（2026-09-23）—— 批次「真增量」读数。

## 为什么要有这个方法

批次 metrics 里原来只有一个 `newly_seeding`，它是**净额**：

    newly_seeding = 当前 SEEDING 数 − 批次前 SEEDING 数
    （定义在 `scripts/drive-loop.py` 的 `run_round()` 里）

摘要把它印成「**新增做种**」⇒ 读起来像「今天新加了 N 部」。实测后果：
生产 09-22 与 09-23 两封摘要的「新增做种」都是 `443`，而同一封信里
`发出请求` 从 162 掉到 37 ⇒ **两个读数互相矛盾**，读者合理地质疑数据有错。
真因是净额答不了「今天到底有没有新片转正」——`443` 是**池子当前的大小**。

`first_seeding_between()` 就是那个增量：**本窗内首次变 SEEDING 的片数**。
口径与 `StateStore.trend()` **逐字相同**（`MIN(at) GROUP BY movie_id`），
是项目早就定下的正确口径（`trend()` 的 docstring：「按**每片首次**变 SEEDING 算」）。

## 本文件钉什么（★ 每条都在防一个具体的错法）

1. **同一部片反复 sync 不许重复计数** —— 这是选 `MIN(at)` 而不是
   `COUNT(*) WHERE result='seeding'` 的**全部理由**。若哪天有人把它改成
   直接数 seeding 行，`result='seeding'` 是**状态**不是**增量**，
   这个数会随每批 sync 虚涨。
2. **时间窗两端都是闭的** —— `attempt.at` 只有**秒**精度，而 `sync_pack`
   写完 attempt 之后我们**立刻**查询，两者常落在**同一秒**。
   上界若取半开（`<`），那些"就在这一秒里转正"的片会被**静默漏掉**：
   数出来偏小，而读数看起来完全正常（`0` 不是"没有"，是"没数到"）。
3. **按包隔离** —— A 包的片不许算进 B 包。
4. **缺包 / 空库不炸**。

全部离线：临时目录现造 state.db，不碰生产、不联网、不读 `.env`。
"""
import sys
import tempfile
from pathlib import Path

REPO = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, REPO)
# ★ 2026-10-05（B2）：容器代码收进 data/ ⇒ orchestrator / drive-loop 源码
#   都在 data/ 下。把 data/ 也加进 import 搜索路径（本文件上一行插的是仓库根）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))

from orchestrator import state as S  # noqa: E402

# ★ 输出强制 UTF-8：GBK 控制台下「⇒」这类字符编不出去，会在中途炸掉
#   （tests/README.md 记过这个坑）。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

TMP = Path(tempfile.mkdtemp())
fails = []


def ck(label, got, want):
    ok = got == want
    print(("  ok  " if ok else " FAIL ") + f"{label}: {got!r}")
    if not ok:
        fails.append(f"{label}: got {got!r} want {want!r}")


def make_state(path, pack, dirs):
    """建库：一个包 + 若干片（都从 PENDING 起）。返回 {dir_name: movie_id}。"""
    st = S.StateStore(path, create=True)
    st.con.execute("INSERT INTO pack(name,root,created_at) VALUES(?,'/r','t')",
                   (pack,))
    for i, name in enumerate(dirs, start=1):
        st.con.execute(
            "INSERT INTO movie(id,pack,dir_name,path,stage,matched_indexers)"
            " VALUES(?,?,?,?,?,?)",
            (i, pack, name, f"/r/{name}", S.STAGE_PENDING, "[]"))
    st.con.commit()
    return st


T_ALL = ("2000-01-01 00:00:00", "2099-01-01 00:00:00")

# --------------------------------------------------------------------------- #
print("== ① ★★ 同一部片反复 sync 不许重复计数（选 MIN(at) 的全部理由）==")
st = make_state(TMP / "a.db", "P1", ["m1", "m2"])
try:
    st.add_attempt(1, kind="sync", result="seeding")
    ck("  首次转正 ⇒ 1", st.first_seeding_between("P1", *T_ALL), 1)
    # ★ 再来两次 —— 若实现是 `COUNT(*) WHERE result='seeding'`，这里会变 3
    st.add_attempt(1, kind="sync", result="seeding")
    st.add_attempt(1, kind="sync", result="seeding")
    ck("★★ 同一部片再 sync 两次 ⇒ 仍是 1（不是 3）",
       st.first_seeding_between("P1", *T_ALL), 1)
    st.add_attempt(2, kind="sync", result="seeding")
    ck("  第二部片转正 ⇒ 2", st.first_seeding_between("P1", *T_ALL), 2)
    ck("  第二部片也再 sync ⇒ 仍是 2",
       st.first_seeding_between("P1", *T_ALL), 2)
finally:
    st.con.close()

# --------------------------------------------------------------------------- #
print("\n== ② ★★ 时间窗两端都是闭的（同一秒转正不许被漏掉）==")
st = make_state(TMP / "b.db", "P1", ["m1"])
try:
    st.add_attempt(1, kind="sync", result="seeding")
    at = st.con.execute("SELECT at FROM attempt").fetchone()[0]
    # ★ 生产形状：until 是本批"回灌刚写完"的时刻，attempt.at 与它同秒。
    #   若上界是半开（<），下面第一条会是 0 —— 静默漏掉，读数看着还正常。
    ck("★★ until == attempt.at ⇒ 1（闭区间；半开的话这里是 0）",
       st.first_seeding_between("P1", "2000-01-01 00:00:00", at), 1)
    ck("  since == until == at ⇒ 1（下界也是闭的）",
       st.first_seeding_between("P1", at, at), 1)
    ck("  since 晚于 at ⇒ 0", st.first_seeding_between("P1", "2099-01-01 00:00:00"), 0)
    ck("  until 早于 at ⇒ 0",
       st.first_seeding_between("P1", "2000-01-01 00:00:00", "2000-01-02 00:00:00"), 0)
    # ★ 在生产里 `until=None` 就是这条路 —— 必须能数到"刚写下的"那行
    ck("★★ until=None（取此刻）也能数到刚写下的行 ⇒ 1",
       st.first_seeding_between("P1", "2000-01-01 00:00:00"), 1)
finally:
    st.con.close()

# --------------------------------------------------------------------------- #
print("\n== ③ 按包隔离 + 只认 seeding ==")
st = make_state(TMP / "c.db", "P1", ["m1"])
try:
    st.con.execute("INSERT INTO pack(name,root,created_at) VALUES('P2','/r2','t')")
    st.con.execute(
        "INSERT INTO movie(id,pack,dir_name,path,stage,matched_indexers)"
        " VALUES(9,'P2','x','/r2/x',?,?)", (S.STAGE_PENDING, "[]"))
    st.con.commit()
    st.add_attempt(1, kind="sync", result="seeding")
    ck("  P1 有 1 部", st.first_seeding_between("P1", *T_ALL), 1)
    ck("★ P2 不受影响（按包隔离）", st.first_seeding_between("P2", *T_ALL), 0)
    # 「搜过但没匹配」不是"转正" —— 这是缺陷②那条同族的边界
    st.add_attempt(1, kind="sync", result="unmatched")
    ck("★ `unmatched` 不算转正（仍是 1）", st.first_seeding_between("P1", *T_ALL), 1)
    ck("★ 别的 result（matched 等）也不算",
       st.first_seeding_between("P2", *T_ALL), 0)
finally:
    st.con.close()

# --------------------------------------------------------------------------- #
print("\n== ④ 边界：不存在的包 / 空库不炸 ==")
st = make_state(TMP / "d.db", "P1", [])
try:
    ck("  空库 ⇒ 0", st.first_seeding_between("P1", *T_ALL), 0)
    ck("  不存在的包 ⇒ 0（不抛异常）", st.first_seeding_between("nope", *T_ALL), 0)
finally:
    st.con.close()

print()
if fails:
    print(f"!!! {len(fails)} 个失败")
    for f in fails:
        print("   " + f)
    sys.exit(1)
print("全部通过")
