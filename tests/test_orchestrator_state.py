# -*- coding: utf-8 -*-
"""编排器 `state` 子命令的自测（§11.9 那条待办）。

重点不是"能打印"，而是两件**容易做错**的事：
  ① ★ 库不存在时**绝不许**把文件建出来（`StateStore.__init__` 会凭空建库）
  ② `state` 必须在**读配置之前**分派 —— 配置坏了的时候它还得能用

不碰生产：全部在一个临时库里现造。
"""
import os
import sys
import tempfile
from pathlib import Path

REPO = str(Path(__file__).resolve().parent.parent)  # 仓库根
sys.path.insert(0, REPO)
# ★ 2026-10-05（B2）：容器代码收进 data/ ⇒ orchestrator / drive-loop 源码
#   都在 data/ 下。把 data/ 也加进 import 搜索路径（本文件上一行插的是仓库根）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))

from orchestrator import state as S   # noqa: E402
from orchestrator import main as M    # noqa: E402

# ★ 输出强制 UTF-8：GBK 控制台下 ⑪(U+246A) 编不出去，会在中途炸掉。
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


class Capture:
    """把 stdout 收起来，好断言打印内容。"""

    def __init__(self):
        self.buf = ""

    def __enter__(self):
        import io
        self._old = sys.stdout
        sys.stdout = io.StringIO()
        return self

    def __exit__(self, *a):
        self.buf = sys.stdout.getvalue()
        sys.stdout = self._old
        return False


def run(argv):
    """跑一次 main()，返回 (退出码, 输出)。

    ★ 要接住 SystemExit：argparse 参数错时会直接 `sys.exit(2)`，
      不接的话整个测试脚本会**在断言之前就死掉**（第一次跑就是这样）。
    """
    with Capture() as c:
        try:
            rc = M.main(argv)
        except SystemExit as e:                  # argparse 的参数错
            rc = e.code if isinstance(e.code, int) else 2
    return rc, c.buf


def make_db(path: Path, packs):
    """packs: [(name, root, [(dir_name, stage)])]

    ★ `create=True`：夹具的职责就是建库。生产默认 `create=False` —— 见
      `StateStore.__init__`（读一个不存在的路径会伪装成「读到空库」）。
    """
    with S.StateStore(path, create=True) as st:
        for name, root, movies in packs:
            st.con.execute("INSERT INTO pack(name,root,created_at) VALUES(?,?,?)",
                           (name, root, "t"))
            for dn, stage in movies:
                # ★ id 是 INTEGER PRIMARY KEY，别拿字符串凑（会报 datatype mismatch）
                st.con.execute(
                    "INSERT INTO movie(id,pack,dir_name,path,stage,matched_indexers)"
                    " VALUES((SELECT COALESCE(MAX(id),0)+1 FROM movie),?,?,?,?,?)",
                    (name, dn, f"{root}/{dn}", stage, '["HDFans"]'))
        st.con.commit()


DB = TMP / "state.db"
make_db(DB, [
    ("bbb-pack", "/r/bbb", [("片1", S.STAGE_SEEDING), ("片2", S.STAGE_PENDING)]),
    ("aaa-pack", "/r/aaa", [("片A", S.STAGE_SEEDING), ("片B", S.STAGE_UNMATCHED),
                            ("片C", S.STAGE_SKIPPED)]),
])

# --------------------------------------------------------------------------- #
print("== ① 不带 --pack：列出全部包，按名字排序 ==")
rc, out = run(["state", "--db", str(DB)])
ck("  退出码 0", rc, 0)
ck("  两个包都在", ("aaa-pack" in out and "bbb-pack" in out), True)
ck("  ★ aaa 在 bbb 之前（SQL 里排的序）", out.index("aaa-pack") < out.index("bbb-pack"), True)
ck("  登记数打出来了", "登记 3 部" in out, True)
ck("  各阶段的中文含义在", "★被退避跳过" in out, True)

print("\n== ② 带 --pack：只出那一个 ==")
rc, out = run(["state", "--db", str(DB), "--pack", "aaa-pack"])
ck("  退出码 0", rc, 0)
ck("  有 aaa", "aaa-pack" in out, True)
ck("  ★ 没有 bbb", "bbb-pack" in out, False)

print("\n== ③ 未登记的包 → 退出码 2，不是 0 ==")
rc, out = run(["state", "--db", str(DB), "--pack", "查无此包"])
ck("  退出码 2", rc, 2)
ck("  说清楚是未登记", "未登记" in out, True)

print("\n== ④ ★★ 库不存在：报错、退出码 2、且**绝不许**把文件建出来 ==")
# 背景：StateStore.__init__ 会 mkdir -p 再 sqlite3.connect()，
# 而 sqlite 连不存在的路径**不报错、直接建一个 0 字节的库**。
# 实测踩过 —— 打错一层路径就在媒体目录里留下一个空 state.db。
missing = TMP / "sub" / "nope.db"
rc, out = run(["state", "--db", str(missing)])
ck("  退出码 2", rc, 2)
ck("  明说不会替你建库", "不会替你建一个空库" in out, True)
ck("  ★★ 文件**没有**被建出来", missing.exists(), False)
ck("  ★ 连父目录也没被建出来", missing.parent.exists(), False)
ck("  给了真实位置（drive-loop/hlink）", "hlink/state.db" in out, True)

print("\n== ④b ★★ 根因闸：StateStore **自己**不建库（不只是 cmd_state 那一层）==")
# ★ 为什么单钉这一格：上面 ④ 证的是 `cmd_state` **这个调用点**上有一道闸。
#   而 2026-09-12 那次漏掉的，**正是"闸所在的调用点之外的地方"** ——
#   `reconcile_watch` 一个人加了 `is_file()`，另外 13 处没加，
#   其中 `run_round` 在热路径上：路径打错 → 建空库 → 整包算 PENDING
#   → **全量重搜，额度静默烧掉一轮**。
#   闸搬到 `__init__` 之后，判据也必须跟着搬 —— 否则它只是换了个位置继续没被验。
gate = TMP / "gate" / "nope.db"
try:
    S.StateStore(gate)
    raised = None
except FileNotFoundError as e:
    raised = e
ck("  抛 FileNotFoundError", isinstance(raised, FileNotFoundError), True)
ck("  提示里点名真实位置", "hlink/state.db" in str(raised), True)
ck("  ★★ 文件**没有**被建出来", gate.exists(), False)
ck("  ★ 连父目录也没被建出来", gate.parent.exists(), False)
# ★ 反向控制：不验这一条的话，一道"永远抛"的闸也能让上面四格全绿 ——
#   而那道闸会把 init（全仓唯一该建库的地方）一起挡掉。
gate2 = TMP / "gate" / "ok.db"
with S.StateStore(gate2, create=True) as st2:
    st2.upsert_pack("p", "/r")
ck("  反向：create=True 就建得出来（别把 init 一起挡了）", gate2.is_file(), True)

print("\n== ⑤ --detail：逐部列出待搜 ==")
rc, out = run(["state", "--db", str(DB), "--pack", "aaa-pack", "--detail"])
ck("  退出码 0", rc, 0)
ck("  列出了片名（PENDING/SKIPPED 都算待搜）", "片B" in out or "片C" in out, True)
ck("  没加 --detail 时不列片名", "片B" in run(
    ["state", "--db", str(DB), "--pack", "aaa-pack"])[1], False)

print("\n== ⑥ --trend：附上趋势（§16.3） ==")
rc, out = run(["state", "--db", str(DB), "--trend"])
ck("  退出码 0", rc, 0)
ck("  出现了趋势那一段", "新增做种趋势" in out, True)

print("\n== ⑦ ★ state 必须在读 config.yml **之前**分派 ==")
# 只读一个本地库，没理由因为"配置文件不在"就跑不了 ——
# 出故障的时候恰恰最需要它还能用。
# ★ 注意 --config 是**顶层**参数，必须写在子命令**之前**：
#   `main --config X state` ✅   /   `main state --config X` ❌（argparse 不认）
rc, out = run(["--config", str(TMP / "根本没有这个文件.yml"),
               "state", "--db", str(DB), "--pack", "aaa-pack"])
ck("  显式指一个不存在的 --config 也照样跑通", rc, 0)
ck("  没有报配置错误", "配置错误" in out, False)

# ★ 默认配置路径 /config/config.yml 在 Windows 上**本来就不存在**，
#   所以上面那轮其实已经证明了"没读到配置也能跑"。
#   记一条真事实：RESEED_CONFIG 是在**导入时**读的（DEFAULT_CONFIG 是模块级常量），
#   导入之后再设环境变量**不起作用** —— 别写出依赖它的测试。
ck("  DEFAULT_CONFIG 是模块级常量（导入后改环境变量无效）",
   M.DEFAULT_CONFIG, os.environ.get("RESEED_CONFIG", "/config/config.yml"))

print("\n== ⑧ packs() 辅助方法 ==")
with S.StateStore(DB, create=True) as st:
    names = [r["name"] for r in st.packs()]
ck("  按名字升序", names, ["aaa-pack", "bbb-pack"])

print("\n== ⑨ ★★ `--qbit-url` 指向 cross-seed（:2468）必须出声（#66）==")
#   现场：2026-09-14 11:01:02 / 11:01:31，cross-seed 的 error 日志各一条
#     `[server] Unknown endpoint: /api/v2/app/version`
#   ⇒ 那两天里**只有那两条**，其余每天 0 条 ⇒ 一次抄错的端口。
#   ★ 为什么这值得一条断言：`orchestrator/qbit_client.py:83` 请求的正是
#     `/api/v2/app/version`（**qB 的**路径）⇒ 把 cross-seed 的地址给它，
#     报错会出现在 **cross-seed 的日志里**，**看起来像 cross-seed 出问题**，
#     而真正错的是调用方。这才是要挡的那一格。
#   ★ 判据：两个方向都要钉 —— 该出声的出声、**不该出声的绝不出声**
#     （尤其 `:12468` 不能被 `2468` 子串误伤）。
import importlib.util as _ilu
import argparse as _ap
_rs_spec = _ilu.spec_from_file_location(
    "reseed_state", os.path.join(REPO, "data", "drive-loop", "scripts", "reseed-state.py"))
_rs = _ilu.module_from_spec(_rs_spec)
sys.modules["reseed_state"] = _rs
_rs_spec.loader.exec_module(_rs)


def _guard_says(url):
    """跑 _guard_qbit_url，返回它有没有出声。"""
    import io as _io
    buf, old = _io.StringIO(), _rs._say
    _rs._say = lambda s: buf.write(str(s) + "\n")
    try:
        _rs._guard_qbit_url(_ap.Namespace(qbit_url=url))
    finally:
        _rs._say = old
    return bool(buf.getvalue().strip())


for _u, _want in (("http://192.168.0.7:2468", True),
                  ("http://cross-seed:2468", True),
                  ("http://192.168.0.7:2468/", True)):
    ck(f"  指向 cross-seed 的 {_u} → 出声", _guard_says(_u), _want)
for _u in ("http://192.168.0.7:3060", "http://qbittorrent-reseed:3060", None):
    ck(f"  正确的 {_u} → **不出声**", _guard_says(_u), False)
ck("  ★ 阴性对照：`:12468` 不被 `2468` 子串误伤", _guard_says("http://192.168.0.7:12468"), False)

print("\n== ⑤ ★★★ 「待搜数」必须写明口径（`ERR-SVC-19` / 表 3 #5 —— 2026-09-24 `§26.65`）==")
#   背景（真实发生过）：`state` 报「待搜 3 部」，而下一批**实际发了 40 条** webhook。
#   ★ 两个数**都对** —— 量的不是同一件事：
#     · 带 `--indexers` = **发信口径**（下一批发几条）
#     · 不带（库视角）  = 「这包还有几部没走完流程」
#   ⇒ 只印一个数、不说口径 ⇒ 人会拿它去解释"批批 40 条"，
#     并推出「**在重复搜**」这个**错结论**（2026-09-19）。
#   ★ 本段钉两件事：① 带 `--indexers` 时**点明是发信口径**；
#                    ② 不带时**必须给警告**（库视角会漏掉"从没搜过的站"那一整格）。

rc, out = run(["state", "--db", str(DB), "--pack", "aaa-pack"])
ck("  ⑤a 不带 `--indexers` → 明说这是**库视角**", "库视角" in out, True)
ck("  ⑤b ★ 且**必须警告**「不能用来判下一批发几条」",
   "不能" in out and ("发几条" in out or "发信" in out), True)
ck("  ⑤c ★ 且给出**两条**正确读法（传 --indexers / 读日志）",
   "--indexers" in out and "drive-loop.log" in out, True)
ck("  ⑤d ★ 点明「两个都对、别推『在重复搜』」（那句错结论的预防）",
   "重复搜" in out, True)

rc, out = run(["state", "--db", str(DB), "--pack", "aaa-pack",
               "--indexers", "HDFans,HDtime"])
ck("  ⑤e 带 `--indexers` → 明说这是**发信口径**", "发信口径" in out, True)
ck("  ⑤f ★ 且把那几个站名列出来（读数要能对到输入上）",
   "HDFans" in out and "HDtime" in out, True)
ck("  ⑤g ★ 带口径时**不再**出现『库视角』那句警告（别自相矛盾）",
   "库视角" not in out, True)

print()
if fails:
    print(f"!!! {len(fails)} 个失败")
    for f in fails:
        print("   " + f)
    sys.exit(1)
print("全部通过")
