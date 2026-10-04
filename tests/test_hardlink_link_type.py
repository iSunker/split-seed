# -*- coding: utf-8 -*-
"""钉住 `orchestrator/hardlink.py` 的 link_type 分派（#77）。

背景：`prestage()` 原先**写死 `os.link`**，而 `matcher.link_type` 在
`config.py` 里只被定义和校验、全仓库没有第二处引用 —— 一个**死键**。
于是 config.yml 里写 `reflink` 也只是好看，cross-seed 那边改成 COW 也白搭：
两条建链接的路径里，prestage 这条仍然是硬链接（同 inode ⇒ 写穿源文件）。

★ 这个模块最容易被悄悄改坏的地方：
    ① **`reflink` 失败时留下半成品**：`fcntl.ioctl(FICLONE)` 失败时内核可能
       已经把 dst 建出来了。不清掉的话，下次跑会命中「已存在则跳过」，
       留下一个 **0 字节的假链接** —— 而「跳过」看起来完全正常。
       用例见 §②（用 monkeypatch 让 ioctl 抛，验证 dst 被清掉）。
    ② **认不出 `reflink` 就静默退回硬链接**：那正是这个 bug 的形状本身。
       用例见 §①（未知值必须抛，不许 fallback）。
    ③ **断链的 symlink 被当成「已存在」**：`Path.exists()` 对断链为假，
       于是每次跑都重建一遍。用例见 §④。

reflink 的**真**创建需要 BTRFS/XFS + Linux，这里测不了（也不该假装测了）——
§② 用桩验证失败路径，§⑤ 验证 Windows 上给出的是清楚的报错而不是怪异异常。
"""
from __future__ import annotations

import contextlib
import io
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

from orchestrator import hardlink as H   # noqa: E402

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


TD = pathlib.Path(tempfile.mkdtemp(prefix="hardlink-"))
SRC = TD / "src"
DST = TD / "dst"
(SRC / "Sub").mkdir(parents=True)
(SRC / "a.mkv").write_bytes(b"aaaa")
(SRC / "Sub" / "b.mkv").write_bytes(b"bbbb")

print("=== ① 分派：认得出的三种 + 认不出必须抛（绝不静默退回硬链接） ===")
ck("LINK_TYPES 就是那三个", H.LINK_TYPES == ("hardlink", "symlink", "reflink"),
   repr(H.LINK_TYPES))
try:
    H.make_link(SRC / "a.mkv", DST / "x", "banana")
    ck("★ 未知 link_type 必须抛 OSError", False, "没抛")
except OSError as e:
    ck("★ 未知 link_type 抛 OSError（不 fallback —— fallback 就是这个 bug 本身）",
       "banana" in str(e), str(e))
ck("……且没建出文件", not (DST / "x").exists())

print("\n=== ② reflink：失败时**绝不留下半成品** ===")
# 桩掉 fcntl 里的 ioctl，让它抛 —— 模拟「文件系统不支持 reflink」
_real_fcntl = None


class _FakeFcntl:
    @staticmethod
    def ioctl(fd, op, arg):
        raise OSError(22, "Invalid argument")      # ENOTSUP/EINVAL 的形状


try:
    import fcntl as _real_fcntl
except ImportError:
    _real_fcntl = None                               # Windows：本来就没有

import types
_stub = types.ModuleType("fcntl")
_stub.ioctl = _FakeFcntl.ioctl
sys.modules["fcntl"] = _stub
try:
    H._reflink(SRC / "a.mkv", DST / "half.bin")
    ck("ioctl 失败必须抛出来", False, "没抛")
except OSError:
    ck("★ ioctl 失败抛 OSError", True)
ck("★★ 半成品被清掉了（留着会让下次「已存在则跳过」留下 0 字节假链接）",
   not (DST / "half.bin").exists(), repr(list(DST.glob("*")) if DST.exists() else []))

if _real_fcntl is not None:
    sys.modules["fcntl"] = _real_fcntl
else:
    del sys.modules["fcntl"]

print("\n=== ③ 硬链接真的同 inode；symlink 是真的符号链接 ===")
if not DST.exists():
    DST.mkdir(parents=True)
H.make_link(SRC / "a.mkv", DST / "hard.mkv", "hardlink")
ck("hardlink: 同 inode（st_ino 相等）",
   os.stat(SRC / "a.mkv").st_ino == os.stat(DST / "hard.mkv").st_ino,
   f"{os.stat(SRC / 'a.mkv').st_ino} vs {os.stat(DST / 'hard.mkv').st_ino}")
ck("  内容对得上", (DST / "hard.mkv").read_bytes() == b"aaaa")

_sym_ok = True
try:
    H.make_link(SRC / "a.mkv", DST / "sym.mkv", "symlink")
except OSError as e:                                 # Windows 可能没权限
    _sym_ok = False
    print(f"  --   symlink 跳过（本机不允许：{e}）")
if _sym_ok:
    ck("symlink: is_symlink() 为真", (DST / "sym.mkv").is_symlink())
    ck("  ★ 指向**绝对**路径（相对路径会在别的工作目录下断掉）",
       os.path.isabs(os.readlink(DST / "sym.mkv")), os.readlink(DST / "sym.mkv"))
    ck("  读得到源内容", (DST / "sym.mkv").read_bytes() == b"aaaa")

print("\n=== ④ prestage：按 link_type 建、跳过已存在、（断链 symlink 也当已存在） ===")
for sub in ("p_hard", "p_sym", "p_skip", "p_dry"):
    d = DST / sub
    if d.exists():
        import shutil
        shutil.rmtree(d)

st = H.prestage(str(SRC), str(DST / "p_hard"), link_type="hardlink")
ck("hardlink：2 个文件都建了", st.linked == 2 and st.failed == 0, repr(st))
ck("  目录结构保持（Sub/b.mkv 在）", (DST / "p_hard" / "Sub" / "b.mkv").is_file())

st = H.prestage(str(SRC), str(DST / "p_hard"), link_type="hardlink")
ck("★ 已存在则跳过（skipped 计数，且不重写）", st.linked == 0 and st.skipped == 2, repr(st))

st = H.prestage(str(SRC), str(DST / "p_dry"), link_type="reflink", dry_run=True)
ck("dry-run：只报数、不落盘", st.linked == 2 and not (DST / "p_dry").exists(), repr(st))

try:
    H.prestage(str(SRC), str(DST / "p_bad"), link_type="banana")
    ck("★ prestage 也拒未知 link_type（在建任何东西之前）", False, "没抛")
except OSError as e:
    ck("★ prestage 先校验 link_type 再动手", "banana" in str(e), str(e))
ck("  且一个文件都没建", not (DST / "p_bad").exists())

try:
    H.prestage(str(TD / "没有这个目录"), str(DST / "p_x"), link_type="hardlink")
    ck("源目录不存在要抛", False, "没抛")
except FileNotFoundError as e:
    ck("源目录不存在抛 FileNotFoundError", True)

if _sym_ok:
    # 先手工造一个**断链**的 symlink：Path.exists() 对它为假，
    # 只按 exists() 判会每次重建 —— 那正是 ③ 记的坑
    target = DST / "p_sym" / "a.mkv"
    target.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(str(TD / "并不存在"), target)
    ck("  前提：断链 symlink 的 exists() 为假", not target.exists())
    st = H.prestage(str(SRC), str(DST / "p_sym"), link_type="symlink")
    ck("★★ 断链 symlink 被当作「已存在」跳过（否则每跑一次多建一层）",
       st.skipped >= 1 and target.is_symlink() and
       os.readlink(target) == str(TD / "并不存在"),
       repr((st, os.readlink(target))))

print("\n=== ⑤ Windows 上 reflink 是「每个文件失败」，不是把整次预拆打挂 ===")
if os.name != "posix":
    d = DST / "p_reflink"
    st = H.prestage(str(SRC), str(d), link_type="reflink")
    ck("★ Windows 上 reflink 每个文件都计入 failed（不抛到外面）",
       st.failed == 2 and st.linked == 0, repr(st))
    ck("  且没留下任何半成品", not any(d.glob("**/*.mkv")) if d.exists() else True,
       repr(list(d.glob("**/*")) if d.exists() else []))
    try:
        H._reflink(SRC / "a.mkv", DST / "nope")
        ck("★ _reflink 在无 fcntl 的平台上抛 OSError", False, "没抛")
    except OSError as e:
        ck("★★ 报错说清楚是平台问题（而不是一个看不懂的 ImportError）",
           "fcntl" in str(e), str(e))
else:
    print("  --   在 posix 上跑，跳过 Windows 专项")

print(f"\n{'=' * 60}")
print(f"断言 {_ok + _bad} 条：{_ok} 过 / {_bad} 失败")
sys.exit(0 if _bad == 0 else 1)
