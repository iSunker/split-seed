# -*- coding: utf-8 -*-
"""v3 农场路径归包的离线自测（纯合成数据，不碰真库）。"""
import pathlib, sys, tempfile, os
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from orchestrator import state as S
from orchestrator.state import StateStore, CrossSeedSnapshot, _resolve_searchee_to_pack, _farm_mirror

# ★ 输出强制 UTF-8：GBK 控制台下 ⑪(U+246A) 编不出去，会在中途炸掉。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

FARM = "/volume1/video/download/reseed/reseed_farm"

db = os.path.join(tempfile.mkdtemp(), "t.db")
st = StateStore(db, create=True)   # 夹具就是要建库（生产默认不建，见 StateStore.__init__）

# --- 单根包 ---
st.upsert_pack("frds", "/volume1/video/download/FRDS",
               farm_root=FARM, max_depth=2)
st.register_dirs("frds", [
    ("Movie.A.2024", "/volume1/video/download/FRDS/Movie.A.2024"),
    ("Movie.B.2019", "/volume1/video/download/FRDS/Movie.B.2019"),
])

# --- 多根嵌套包（DC：两个标签目录，带"季层"） ---
dc_roots = ["/volume1/video/download/DC/01.绿箭侠", "/volume1/video/download/DC/02.闪电侠"]
st.upsert_pack("dc", dc_roots[0], roots=dc_roots, farm_root=FARM, max_depth=2)
st.register_dirs("dc", [
    ("Arrow.S01.Bluray", dc_roots[0] + "/Arrow.S01-S08/Arrow.S01.Bluray"),
    ("The.Flash.S01", dc_roots[1] + "/The.Flash.S01"),
])

# --- 没设农场根的老包（应保持老行为） ---
st.upsert_pack("legacy", "/volume1/video/download/MBF")
st.register_dirs("legacy", [("Old.Movie", "/volume1/video/download/MBF/Old.Movie")])

fails = []
def ck(label, got, want):
    ok = got == want
    print(("  ok  " if ok else " FAIL ") + f"{label}: {got!r}")
    if not ok:
        fails.append(f"{label}: got {got!r} want {want!r}")

print("== _farm_mirror ==")
ck("原路径→农场", _farm_mirror("/volume1/video/download/FRDS/Movie.A.2024", ["/volume1/video/download/FRDS"], FARM),
   FARM + "/Movie.A.2024")
ck("根自身→农场根", _farm_mirror("/volume1/video/download/FRDS", ["/volume1/video/download/FRDS"], FARM), FARM)
ck("多根取最深", _farm_mirror(dc_roots[0] + "/Arrow.S01-S08/Arrow.S01.Bluray", dc_roots, FARM),
   FARM + "/Arrow.S01-S08/Arrow.S01.Bluray")
ck("归不到 → None", _farm_mirror("/volume1/video/other/X", ["/volume1/video/download/FRDS"], FARM), None)
ck("无农场根 → None", _farm_mirror("/volume1/video/download/FRDS/Movie.A.2024", ["/volume1/video/download/FRDS"], ""), None)

print("== 农场键合并 ==")
fp = st.farm_dir_paths("frds")
ck("frds 镜像键", sorted(fp), sorted([FARM + "/Movie.A.2024", FARM + "/Movie.B.2019"]))
ck("legacy 无镜像键（未设根）", st.farm_dir_paths("legacy"), {})
ck("farm_root 访问器", st.farm_root("frds"), FARM)

print("== _resolve_searchee_to_pack（frds） ==")
roots = st.roots("frds")
dirs = {r["dir_name"] for r in st.movies("frds")}
dpaths = st.dir_paths("frds"); dpaths.update(fp)
farm = st.farm_root("frds")

def mk(paths):
    s = CrossSeedSnapshot()
    s.searchee_paths.update(paths)
    return s

def res(snap, name):
    return _resolve_searchee_to_pack(name, snap, roots, dirs, dpaths, farm)

ck("农场目录本身", res(mk({"a": FARM + "/Movie.A.2024"}), "a"), ("Movie.A.2024", "in_pack"))
ck("农场里的 .mkv（更深一层）", res(mk({"a": FARM + "/Movie.A.2024/Movie.A.mkv"}), "a"), ("Movie.A.2024", "in_pack"))
ck("原路径仍然认", res(mk({"a": "/volume1/video/download/FRDS/Movie.A.2024"}), "a"), ("Movie.A.2024", "in_pack"))
ck("别的包的农场项 → other_pack（不计数）",
   res(mk({"a": FARM + "/The.Flash.S01/The.Flash.S01E01.mkv"}), "a"), (None, "other_pack"))
ck("农场内但认不出 → other_pack",
   res(mk({"a": FARM + "/No.Such.Movie"}), "a"), (None, "other_pack"))
ck("农场外 → other_pack",
   res(mk({"a": "/volume1/video/download/OTHER/X"}), "a"), (None, "other_pack"))
ck("查不到路径 → 名字兜底",
   res(mk({}), "Movie.B.2019"), ("Movie.B.2019", "in_pack"))

print("== DC 多根 + 季层 ==")
droots = st.roots("dc")
ddirs = {r["dir_name"] for r in st.movies("dc")}
ddp = st.dir_paths("dc"); ddp.update(st.farm_dir_paths("dc"))
dfarm = st.farm_root("dc")

def dres(p):
    return _resolve_searchee_to_pack("a", mk({"a": p}), droots, ddirs, ddp, dfarm)

ck("季层镜像 → 季层单片", dres(FARM + "/Arrow.S01-S08/Arrow.S01.Bluray/Arrow.S01E01.mkv"),
   ("Arrow.S01.Bluray", "in_pack"))
ck("相邻根的同名季不串包", dres(FARM + "/The.Flash.S01/The.Flash.S01E01.mkv"), ("The.Flash.S01", "in_pack"))

print("== 老包（无农场根）不应认领农场路径 ==")
lroots = st.roots("legacy"); ldirs = {r["dir_name"] for r in st.movies("legacy")}
ldp = st.dir_paths("legacy"); ldp.update(st.farm_dir_paths("legacy"))
ck("legacy 看农场项 → other_pack",
   dres_legacy := _resolve_searchee_to_pack("a", mk({"a": FARM + "/Movie.A.2024"}), lroots, ldirs, ldp, st.farm_root("legacy")),
   (None, "other_pack"))

st.close()
print()
if fails:
    print(f"!!! {len(fails)} 个失败")
    for f in fails: print("   " + f)
    sys.exit(1)
print("全部通过")
