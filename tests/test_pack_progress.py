# -*- coding: utf-8 -*-
"""各包进度（`pack_progress`）—— 日报新节 + `#95` 的空格键加固。

钉的是**三件互相独立的事**，每一件都对应 `#90–#97` 里的一条订正：

  ① `#95` **包名含空格会让 metrics 字段裂开，且碎片能伪造出 `pack=` 键。**
     这是本文件里**最重**的一条 —— 摘要数批次靠的是 `pack=` **整键相等**
     （`notify-spool.sh` 里那条 awk，`test_notify_digest.py` 专门钉过
     「`packs=` 不是 `pack=`」）。所以：

         seeding:my pack=1 ok=50
                    ↑ 按空格切开 ⇒ "seeding:my"（没有 `=`，键消失）
                                   + "pack=1"（键恰好 = `pack`！）

     ⇒ 一个叫 `my pack` 的包会让该行被**误计为一批**。
     ★ 修法在**写入端**（`notify.py` 的键也做 `.replace(' ', '_')`），
       不动已部署的 `notify-spool.sh`。

  ② `#96` **新节必须落在 `report_daily` 那个 `try/with S.StateStore` 里。**
     读不到时给 `n/a`，**不许给 0** —— `n/a` / `0` / 「没事」是三件事
     （`ERR-AI-03`）。这里用 stub 掉 `StateStore` 的办法直接验。

  ③ `#97` **计数只有一份。** `sync_pack` 与 `pack_progress` 必须走**同一个**
     表达式；两处各写一遍就会静默分叉（两个数"看着都对"却对不上）。

★ 行形用既有的 `  ok  ` / ` FAIL `（不造第四种，`tests/README.md:63-67`）；
  节编号不越过 `⑩`（`⑪` 在 GBK 控制台下编不出去，会把脚本从中间打断）。
★ 全部离线：SQLite 都在 `tempfile` 里现造，不碰 NAS、不碰生产库、不发信。
"""
import importlib.util
import io
import contextlib
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
# ★ 2026-10-05（B2）：容器代码收进 data/ ⇒ orchestrator / drive-loop 源码
#   都在 data/ 下。把 data/ 也加进 import 搜索路径（本文件上一行插的是仓库根）。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "data"))

from orchestrator.state import (            # noqa: E402
    ALL_STAGES, CENSUS_DENOM_STAGES, STAGE_SEARCHED, STAGE_UNMATCHED,
    STAGE_SEEDING, STAGE_PENDING, StateStore, pack_progress, pack_seeding_total,
    pack_stage_census,
)

# ★ 输出强制 UTF-8：GBK 控制台下会在中途炸掉，而**已经过的断点看着全是 ok**
#   —— 极易误判成代码坏了（`ERR-OS-04` / `ERR-ENC-01`）。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

fails: list[str] = []


def ck(label, got, want):
    ok = got == want
    print(("  ok  " if ok else " FAIL ") + f"{label}: {got!r}")
    if not ok:
        fails.append(f"{label}: got {got!r} want {want!r}")


def ck_true(label, cond, extra=""):
    print(("  ok  " if cond else " FAIL ") + label + (f"  —— {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(f"{label}{('  —— ' + extra) if extra else ''}")


def _load(path, name):
    """按 `tests/README.md` 的规矩：importlib 载**真的那份**，且**先**登记 `sys.modules`。

    ★ 不登记的话，被测文件里的 `@dataclass` 会炸一个看着毫不相干的
      `AttributeError: 'NoneType' object has no attribute '__dict__'`（`ERR-PY-03`）。
    """
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod                 # ★ 必须在 exec_module 之前
    with contextlib.redirect_stdout(io.StringIO()), \
         contextlib.redirect_stderr(io.StringIO()):
        try:
            spec.loader.exec_module(mod)
        except SystemExit:
            pass
    return mod


# --------------------------------------------------------------------------- #
# ① notify.py：空格键规范化 —— 判据**成对**（既要断言不再伪造，也要断言旧写法会伪造）
# --------------------------------------------------------------------------- #

print("== ① notify：键里的空格要被规范化，且**不许**再伪造出 `pack=` 键 ==")
# ★★ 2026-10-04：`notify.py` 已**整体剥离**到独立项目（见 `重构方案.md` §11）
#   ⇒ 仓库里可能不再有 `scripts/notify.py`。本段**降级而非删除**：
#     · 若文件在（本地还没清 / NAS 上仍留一份）⇒ 照旧跑**全部**断言；
#     · 若文件不在 ⇒ 打印一条 SKIP，**但仍跑 ①d 那段阴性对照**（它是纯字符串复刻、
#       不依赖 notify.py）—— 否则「键规范化」这条判据会**静默消失**，
#       而它钉的正是「含空格键会伪造出 `pack=`、污染下游批次计数」这个真缺陷。
NOTIFY = REPO / "scripts" / "notify.py"
if NOTIFY.is_file():
    NT = _load(NOTIFY, "notify_pack_progress")
    _HAVE_NOTIFY = True
else:
    NT = None
    _HAVE_NOTIFY = False
    print("  SKIP  ① notify.py 缺失（通知模块已剥离）——跳过依赖它的断言，"
          "保留纯字符串的阴性对照")

if _HAVE_NOTIFY:
    _n = NT.Notifier.__new__(NT.Notifier)
    _n.host = "lab"

    def _metrics_line(pairs: dict) -> str:
        ev = NT.Event(kind="batch", title="t", metrics=pairs)
        out = _n._render(ev, 0.0)
        return [ln for ln in out.splitlines() if ln.startswith("metrics:")][0]

    def _keys_of(line: str) -> list:
        """复刻 NAS 侧 awk 的切法：按**单个空格**切开，取第一个 `=` 之前的整串。"""
        body = line.split("metrics: ", 1)[1]
        out = []
        for tok in body.split(" "):
            p = tok.find("=")
            if p > 0:
                out.append(tok[:p])
        return out

    # ①a 含空格的键：规范化后键**原样连在一起**（不再裂开）
    line = _metrics_line({"seeding:my pack": 1, "ok": 50})
    ck("含空格键 → 键被规范化", "seeding:my_pack=1" in line, True)
    ck("含空格键 → 切出的键列表", _keys_of(line), ["seeding:my_pack", "ok"])

    # ①b ★★ 关键断言：**不许**出现键恰好等于 `pack` 的碎片
    ck_true("★ 不再伪造出 `pack=` 键（否则该行会被误计为一批）",
            "pack" not in _keys_of(line),
            f"切出的键={_keys_of(line)!r}")

    # ①c 值里的空格照旧被规范化（原有行为，不许被本次改动破坏）
    line_v = _metrics_line({"seeding:x": "frds top"})
    ck("值含空格 → 值的空格仍换成下划线", "seeding:x=frds_top" in line_v, True)

    # ①d ★ 阴性对照：这条判据**必须能红** —— 直接复刻**旧写法**（键不换空格）
    _old = " ".join(f"{NT._clean_line(str(k))}={NT._clean_line(str(v)).replace(' ', '_')}"
                    for k, v in {"seeding:my pack": 1, "ok": 50}.items())
    _old_line = f"metrics: {_old}"
    ck_true("★ 阴性对照：旧写法（键不换空格）**确实**会伪造出 `pack` 键",
            "pack" in _keys_of(_old_line),
            f"旧写法切出={_keys_of(_old_line)!r} —— 若这里不红，说明上面那条断言恒真")
else:
    # ★ notify.py 不在时，也要有一条**不依赖它**的阴性对照撑着这段的判据 ——
    #   用**纯字符串**复刻「旧写法按键里的空格切开 ⇒ 会裂出 `pack`」这个事实。
    def _keys_of_raw(line: str) -> list:
        body = line.split("metrics: ", 1)[1]
        out = []
        for tok in body.split(" "):
            p = tok.find("=")
            if p > 0:
                out.append(tok[:p])
        return out

    _old_line = "metrics: seeding:my pack=1 ok=50"   # 键 `seeding:my pack` 未规范化
    ck_true("★ 阴性对照（不依赖 notify.py）：未规范化的键确实会裂出 `pack` 键",
            "pack" in _keys_of_raw(_old_line),
            f"切出={_keys_of_raw(_old_line)!r} —— 这正是剥离后仍需守住的缺陷")

# --------------------------------------------------------------------------- #
# ② pack_progress：结构化输出 + 空集 ≠ 读不到
# --------------------------------------------------------------------------- #

print()
print("== ② pack_progress：每包 (做种/总数/时刻)，空集 ≠ 读不到 ==")

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="packprog-lab-"))
db = _tmp / "state.db"

print("  -- 夹具建库（`create=True` —— 生产默认不建，见 StateStore.__init__ / `ERR-SQL-04`）--")
with StateStore(db, create=True) as st:
    st.upsert_pack("alpha", "/volume1/video/a", max_depth=2)
    st.register_dirs("alpha", [("m1", "/volume1/video/a/m1"),
                              ("m2", "/volume1/video/a/m2"),
                              ("m3", "/volume1/video/a/m3")])
    # m1 设为 SEEDING、m2 设 MATCHED（不该计入 seeding）、m3 留 PENDING
    st.con.execute("UPDATE movie SET stage=? WHERE pack=? AND dir_name=?", (STAGE_SEEDING, "alpha", "m1"))
    st.con.execute("UPDATE movie SET stage='MATCHED' WHERE pack=? AND dir_name=?", ("alpha", "m2"))
    st.con.commit()
# ④ 每包**自己**的时刻（`mark_scan` 是生产里唯一写它的地方）
with StateStore(db) as st:
    st.mark_scan("alpha", finished=True)

with StateStore(db, create=True) as st:
    rows = pack_progress(st)
    ck("包数", len(rows), 1)
    ck("包名", rows[0]["name"], "alpha")
    ck("做种数（只有 SEEDING 计入）", rows[0]["seeding"], 1)
    ck("总数（= 声明过的单片数）", rows[0]["total"], 3)
    # ④ 每包带**自己**的时刻，不是全局「截至」—— 断言"有值"即可（`mark_scan` 写的是 now）
    ck_true("每包自己的 scan_finished_at 有值（不是 None、不是全局截至）",
            bool(rows[0]["scanned_at"]), f"got {rows[0]['scanned_at']!r}")

# ②b `total` 的定义：多于 init --dry-run 是**定义**，不是 bug
with StateStore(db) as st:
    st.register_dirs("alpha", [("gone-now", "/volume1/video/a/gone-now")])
with StateStore(db) as st:
    rows2 = pack_progress(st)
    ck("★ 重跑 init 前 total 会大于 dry-run 的数（register_dirs 从不删行）",
       rows2[0]["total"], 4)

# ②c ★★ 空集 ≠ 读不到：一个包都没有时返回**空列表**，不是 None、不是抛
empty = _tmp / "empty.db"
with StateStore(empty, create=True) as st:
    ck("库在、但一个包都没登记 → 空列表", pack_progress(st), [])

# --------------------------------------------------------------------------- #
# ③ 计数只有一份（#97）：pack_seeding_total 与 sync_pack 同源
# --------------------------------------------------------------------------- #

print()
print("== ③ 计数只有一份（#97）==")

_STATE_SRC = (REPO / "data" / "orchestrator" / "state.py").read_text(encoding="utf-8")
ck_true("★ sync_pack 不再自己写计数（改调共用函数）",
        "rep.seeding, _ = pack_seeding_total(store, pack_name)" in _STATE_SRC,
        "没找到那一行 —— 计数可能又被复制成两份了")
_NEEDLE = 'r["stage"] == STAGE_SEEDING'
ck("★ `stage == STAGE_SEEDING` 表达式只剩一处（在 pack_seeding_total 里）",
   _STATE_SRC.count(_NEEDLE), 1)

with StateStore(db) as st:
    a = pack_seeding_total(st, "alpha")
    rows = pack_progress(st)
    b = (rows[0]["seeding"], rows[0]["total"])
    ck("★ 两条路给出同一个数", a, b)

# --------------------------------------------------------------------------- #
# ④ ② 口径「待办完成率」：分子/分母/百分比 + **阴性对照**
# --------------------------------------------------------------------------- #

print()
print("== ④ ② 口径待办完成率（分母不含 UNMATCHED）==")

_READ_KEYS = {"name", "seeding", "total", "pct", "census", "scanned_at"}

# 夹具 `beta`：**六个目录、六个不同阶段** —— 目的是让分子(1)与分母(5)
# 与 total(6) **三个数互不相等**，这样一条算错的实现没法靠巧合过掉。
#   ★ 为什么不能只放两三个阶段：若分子==分母或分母==total，
#     「把 UNMATCHED 算进分母」和「按 total 算」这两条错法**都会给出正确答案**。
_beta = _tmp / "beta.db"
with StateStore(_beta, create=True) as st:
    st.upsert_pack("beta", "/volume1/video/b", max_depth=2)
    st.register_dirs("beta", [(f"b{i}", f"/volume1/video/b/b{i}") for i in range(1, 7)])
    for _d, _s in (("b1", STAGE_SEEDING), ("b2", "MATCHED"), ("b3", STAGE_PENDING),
                   ("b4", "SKIPPED"), ("b5", "ERROR"), ("b6", "UNMATCHED")):
        st.con.execute("UPDATE movie SET stage=? WHERE pack=? AND dir_name=?",
                       (_s, "beta", _d))
    st.con.commit()
# 全 UNMATCHED 的包（分母必然为 0）
_allun = _tmp / "allun.db"
with StateStore(_allun, create=True) as st:
    st.upsert_pack("allun", "/volume1/video/u", max_depth=2)
    st.register_dirs("allun", [("u1", "/volume1/video/u/u1")])
    st.con.execute("UPDATE movie SET stage='UNMATCHED' WHERE pack=?", ("allun",))
    st.con.commit()
# 登记过、但 **0 行** 的包（分母也是 0，成因不同、处置相同）
_zero = _tmp / "zero.db"
with StateStore(_zero, create=True) as st:
    st.upsert_pack("zero", "/volume1/video/z", max_depth=2)

# ---- ④a 算术 + 两种定义的等价形 ----
with StateStore(_beta) as st:
    cen = pack_stage_census(st, "beta")
    row = pack_progress(st)[0]
ck("总行数 TOTAL", cen["TOTAL"], 6)
ck("分子 = SEEDING", cen["numerator"], 1)
ck("分母 = 除 UNMATCHED 外全部", cen["denom"], 5)
ck("百分比", row["pct"], 20)
ck("★ 两种定义恒等：TOTAL − UNMATCHED == denom", cen["TOTAL"] - cen["UNMATCHED"], cen["denom"])
ck("★ seeding/total 仍来自 pack_seeding_total（不受 census 影响）",
   (row["seeding"], row["total"]), (1, 6))
ck_true("★ census 被带出来了（渲染层要用来印原始分数）",
        row["census"] is not None and row["census"]["denom"] == 5, f"got {row['census']!r}")
ck("★ 返回的键集合稳定（渲染层只许取这些）", set(row), _READ_KEYS)

# ★ 特异性：把 b6 从 UNMATCHED 改成 PENDING ⇒ 它**进**分母
with StateStore(_beta) as st:
    st.con.execute("UPDATE movie SET stage=? WHERE pack=? AND dir_name=?",
                   (STAGE_PENDING, "beta", "b6"))
    st.con.commit()
with StateStore(_beta) as st:
    cen2 = pack_stage_census(st, "beta")
ck("★ 把那条 UNMATCHED 改成 PENDING ⇒ 分母 5→6（排除的是 UNMATCHED，不是「还没做种的」）",
   cen2["denom"], 6)
with StateStore(_beta) as st:                    # 还原，后面的组还要用
    st.con.execute("UPDATE movie SET stage='UNMATCHED' WHERE pack=? AND dir_name=?",
                   ("beta", "b6"))
    st.con.commit()

# ---- ④b SEARCHED 别名 + 无重复 ----
ck("★ `STAGE_SEARCHED` **不在**分母元组里（语义同 UNMATCHED、值不同）",
   STAGE_SEARCHED in CENSUS_DENOM_STAGES, False)
ck("UNMATCHED 不在分母元组里", STAGE_UNMATCHED in CENSUS_DENOM_STAGES, False)
ck("分母元组无重复（按身份比较，不按值）", len(dict.fromkeys(CENSUS_DENOM_STAGES)),
   len(CENSUS_DENOM_STAGES))
ck("分母元组的元素集 == ALL_STAGES 去掉 UNMATCHED",
   set(CENSUS_DENOM_STAGES), set(ALL_STAGES) - {STAGE_UNMATCHED})
with StateStore(_beta) as st:
    cen3 = pack_stage_census(st, "beta")
ck("★ 逐阶段相加 == denom（普查与分母同源）",
   sum(cen3[s] for s in CENSUS_DENOM_STAGES), cen3["denom"])

# ---- ④c 分母为 0 的两种成因 ⇒ 都是 "n/a"，**不是** 0 ----
with StateStore(_allun) as st:
    r_un = pack_progress(st)[0]
ck("★ 整包 UNMATCHED ⇒ denom", r_un["census"]["denom"], 0)
ck('★ 整包 UNMATCHED ⇒ pct == "n/a"（不是 0）', r_un["pct"], "n/a")
ck_true("★ `n/a` 既不是 int 0、也不是字符串 \"0%\"",
        r_un["pct"] != 0 and r_un["pct"] != "0%", f"got {r_un['pct']!r}")
with StateStore(_zero) as st:
    r_z = pack_progress(st)[0]
ck("★ 包登记过但 0 行 ⇒ 同样的 n/a（成因不同、处置相同）", r_z["pct"], "n/a")
ck("★ 0 行的包 total 也是 0（这是「空」，不是「算不出」）", r_z["total"], 0)
# ★★ 与上面成对的那一半：**真的 0/N** ⇒ pct 就是 0，且是 int
with StateStore(_beta) as st:
    st.con.execute("UPDATE movie SET stage='MATCHED' WHERE pack=? AND dir_name=?",
                   ("beta", "b1"))
    st.con.commit()
with StateStore(_beta) as st:
    r_true0 = pack_progress(st)[0]
ck('★★ 真的 `0/N`（分母 > 0）⇒ pct == 0，**是合法读数**，与 n/a 是两个不同输出',
   r_true0["pct"], 0)
ck_true("★ 那个 0 是 int，不是字符串", isinstance(r_true0["pct"], int), f"got {type(r_true0['pct'])}")
with StateStore(_beta) as st:                    # 还原
    st.con.execute("UPDATE movie SET stage=? WHERE pack=? AND dir_name=?",
                   (STAGE_SEEDING, "beta", "b1"))
    st.con.commit()

# ---- ④d ★★ 阴性对照：①口径 / 算进 UNMATCHED / 除零给 0 —— 三条错法各给不同答案 ----
#   ★ 这是**本节最重要的一条**：没有它，一个把分母写成 `total` 的错实现
#     能过掉上面**所有**断言（因为 1/6 与 1/5 只在某些夹具上才不同）。
_真 = 20                                                    # 100 × 1/5
_按total = round(100 * 1 / 6)                               # ① 口径 = 17
ck("★ 阴性对照：① 口径（100×1/total）确实**不同**于 ② 口径",
   _按total != _真, True)
ck("  （① 口径的值，写下来免得下次重算）", _按total, 17)
ck("★ 阴性对照：把 UNMATCHED **算进**分母也得到 17 ⇒ 与 20 不同",
   round(100 * 1 / 6), 17)
ck_true("★ 阴性对照：除零给 0 与真值 \"n/a\" **不同** ⇒ ④c 不是恒真",
        "n/a" != 0, True)

# ---- ④e metrics 键：#95 合规 + 绝不拼出 `pack=` ----
# 走**真** notify.py 的渲染（与 ① 段同一套切法），验证端到端而不是猜键名。
# ★★ 2026-10-05：整段**依赖 notify.py**（`_metrics_line`/`_keys_of` 都在上面
#   `if _HAVE_NOTIFY:` 里定义）。通知模块剥离后 notify.py 不在仓库里 ⇒ 这俩名字
#   未定义；若不守卫，本文件会在**真跑**时 NameError 崩溃（而不是"跳过"）。
#   ⇒ 与 ① 段同一道 `_HAVE_NOTIFY` 闸。
if _HAVE_NOTIFY:
    _wm = {"packpct:my pack": 64, "packnum:my pack": 72, "packden:my pack": 115,
           "seeding:my pack": 74, "total:my pack": 115, "ok": 50}
    _line = _metrics_line(_wm)
    _keys = _keys_of(_line)
    ck("★ 新键里的空格被规范化（写入端一处负责）", "packpct:my_pack=64" in _line, True)
    ck_true("★★ **不许**出现键恰好等于 `pack` 的碎片（否则该行被误计为一批）",
            "pack" not in _keys, f"切出的键={_keys!r}")
    ck("★ 被消费的另三个键也不在（ok 在，另两个不在）",
       [k for k in _keys if k in ("ok", "newly_seeding", "backoff_hits")], ["ok"])
    ck_true("★ `packnum:`/`packden:` **字面含 `pack` 子串**，靠整键比较才安全",
            any("pack" in k for k in _keys) and "pack" not in _keys, f"切出的键={_keys!r}")

# ---- ④f 渲染层：② 口径那份正文 + 总计行 ----
print()
print()
print("== ④f 渲染（pack_progress_watch）==")
# ★★ 接法：**不动真 `S.pack_progress`**，只在渲染前把它换成喂数据的桩。
#   （第一版我伪造了一个 `StateStore`，而渲染层会一路走到 `store.movies()` ⇒
#    `AttributeError` —— 桩要打在**渲染层唯一那个入口**上，不是它下面。）
DL = _load(REPO / "data" / "drive-loop" / "scripts" / "drive-loop.py", "drive_loop_pack_progress")
_REAL_PP = DL.S.pack_progress

# 两个包、分母**不等** ⇒ 「总计取和」与「总计取平均」给出**不同**的数：
#   big 1/4（25%）+ small 1/2（50%） ⇒ 和 = 2/6 = 33%，平均 = 37.5% ⇒ 38%
_two = [
    {"name": "big", "seeding": 1, "total": 4, "pct": 25,
     "census": {"denom": 4, "numerator": 1, "TOTAL": 4, "UNMATCHED": 0},
     "scanned_at": "2026-09-18 01:00:00"},
    {"name": "small", "seeding": 1, "total": 2, "pct": 50,
     "census": {"denom": 2, "numerator": 1, "TOTAL": 2, "UNMATCHED": 0},
     "scanned_at": "2026-09-18 00:30:00"},
]


def _render(rows):
    """把 `S.pack_progress` 换成桩，跑一次渲染，**无论成败都还原**。
    ★ 必须还原：这个桩是**模块级**赋值，漏还原会让后面的组拿到假数据。"""
    DL.S.pack_progress = lambda _st: rows
    try:
        return DL.pack_progress_watch(object())
    finally:
        pass


_note, _mets = _render(_two)
ck_true("每包行同时含「做种 / 总数」与百分比：`1 / 2` + `50%` 在",
        "1 / 2" in _note and "50%" in _note, f"正文={_note!r}")
ck_true("同上，第二个包：`1 / 4` + `25%` 在",
        "1 / 4" in _note and "25%" in _note, f"正文={_note!r}")
ck_true("每包行还印了**未约简的分数**（百分比四舍五入过，回落那句要靠它判）",
        "1/4" in _note and "1/2" in _note, f"正文={_note!r}")
ck_true("每包带**它自己**的时刻（#94 回归）",
        "2026-09-18 01:00:00" in _note and "2026-09-18 00:30:00" in _note,
        f"正文={_note!r}")
ck_true("有 `总计` 行", "总计" in _note, f"正文={_note!r}")
# ★★★ 本节第二条最重要的断言：总计必须是**和**，不是平均
# ★ 为什么上面那个夹具（1/4 + 1/2）**故意**选得让平均落在 `.5` 上：
#   2026-09-18 拿生产库冷副本冒烟时实测，真库的「平均」写法算出来是
#   `(95+100)/2 = 97.5` ⇒ `round` ⇒ **98**，而正确的「和」写法是 99。
#   ⇒ 两个错/对答案**都**是两位数、都"看起来合理"；平均那一支还正好压在
#   银行家舍入的中点。这条断言要挡的**就是**这种"静默偏 1"的坏法 ——
#   所以夹具必须也踩在中点上（`37.5`），否则它只证明了"两个数不同"，
#   证明不了"不同会被 `round` 吃掉"。详见计划 Part F.2。
ck_true("★★ 总计用**和**（2/6 ⇒ 33%），不是各包百分比的平均（37.5% ⇒ 38%）",
        "2 / 6" in _note and "33%" in _note and "38%" not in _note, f"正文={_note!r}")
ck_true("★ 行尾写明「各包之和，非平均值」", "非平均" in _note, f"正文={_note!r}")
ck_true("★ 含「回落」那句（② 非单调性的预先堵漏）", "回落" in _note, f"正文={_note!r}")
ck_true("★★ 且**不含**替读者下的结论（下降 / 退化 / 变差 / 做种率）",
        not any(w in _note for w in ("下降", "退化", "变差", "做种率")), f"正文={_note!r}")
ck_true("★ metrics 里有事件级 pct / pct_num / pct_den",
        {"pct", "pct_num", "pct_den"} <= set(_mets), f"keys={sorted(_mets)}")
ck("★ 事件级总计分子（和）", _mets["pct_num"], 2)
ck("★ 事件级总计分母（和）", _mets["pct_den"], 6)
ck("★ 事件级百分比", _mets["pct"], 33)
ck_true("★ 每包三个新键都在（字面含 pack 但不等于 pack）",
        {"packpct:big", "packnum:big", "packden:big",
         "packpct:small", "packnum:small", "packden:small"} <= set(_mets),
        f"keys={sorted(_mets)}")
ck_true("★ 原有的 seeding:/total: 键**保留**（摘要里能自己验算）",
        {"seeding:big", "total:big", "seeding:small", "total:small"} <= set(_mets),
        f"keys={sorted(_mets)}")
ck_true("★ 渲染层的 metrics 里**没有**裸 `pack` 键",
        "pack" not in _mets, f"keys={sorted(_mets)}")

# 分母为 0（整包 UNMATCHED）⇒ 正文写 n/a，且那一行**不出现 %**
_zero_rows = [{"name": "allun", "seeding": 0, "total": 1, "pct": "n/a",
               "census": {"denom": 0, "numerator": 0, "TOTAL": 1, "UNMATCHED": 1},
               "scanned_at": "2026-09-18 02:00:00"}]
_noteN, _metsN = _render(_zero_rows)
ck_true("★ 分母为 0 的包在正文里写 n/a", "n/a" in _noteN, f"正文={_noteN!r}")
ck_true("★★ 且那一行**不出现 `%`**（别把 n/a 印成 n/a%）", "%" not in _noteN,
        f"正文={_noteN!r}")
ck("★ 分母为 0 时事件级 pct 也是 n/a（不是 0）", _metsN["pct"], "n/a")
ck("★ 分母为 0 时事件级 pct_den 是 0（先把分母说清）", _metsN["pct_den"], 0)
ck_true("★ 分母为 0 时不该出现 `0%`（那是另一句话）", "0%" not in _noteN, f"正文={_noteN!r}")

# 空集：一个包都没登记 ⇒ 与「库读不到」不是同一句话
_noteE, _metsE = _render([])
ck_true("★ 空集说的话与「算不出」不同（两种 n/a 别统一）",
        "一个包都没登记" in _noteE and "算不出" not in _noteE, f"正文={_noteE!r}")
ck("★ 空集的 metrics 为空", _metsE, {})
DL.S.pack_progress = _REAL_PP          # ★ 还原，别让桩漏到后面的组

# ---- ④g #96 回归：渲染层炸了不许带走整份日报 ----
print()
print("== ④g #96 回归：新节失败的口径 ==")
_src = (REPO / "data" / "drive-loop" / "scripts" / "drive-loop.py").read_text(encoding="utf-8")
_rd = _src[_src.find("def report_daily"):]
_rd = _rd[:_rd.find(chr(10) + "def ")]
ck_true("★ `pack_progress_watch(st)` 在 report_daily 的 try 里被调用",
        "pack_progress_watch(st)" in _rd, "没找到调用点 —— 新节可能被放到了 try 之外")
ck_true("★ 那一节的失败出口写的是「各包进度：算不出」",
        "各包进度：算不出" in _rd, "读库失败时正文没有那句话")
ck_true("★ 读不到时给 `n/a` 而不是 0（`ERR-AI-03`）",
        'pp_metrics = {"pp": "n/a"}' in _rd, "没找到 n/a 那条兜底")

# ---- ④h ⑥ 那条已知弱点的**正向**补充：渲染层只许取它有的键 ----
#   （计划 C.1.1：这条链原先没有任何断言钉住这件事）
_pk = [k for k in ("name", "seeding", "total", "pct", "census", "scanned_at")
       if k not in _two[0]]
ck("★ 夹具的两个 dict 键齐（渲染层不会撞 KeyError）", _pk, [])

# --------------------------------------------------------------------------- #
print()
if fails:
    print(f"★ {len(fails)} 条失败：")
    for f in fails:
        print("   " + f)
    sys.exit(1)
print("✓ 全过")
