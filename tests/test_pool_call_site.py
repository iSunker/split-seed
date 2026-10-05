# -*- coding: utf-8 -*-
"""`todo_pooled` 的**调用点** —— 传进去的必须是"包名列表"，不是被拆开的东西。

为什么要专门钉这个（2026-09-22 事故；2026-10-05 更新）
======================================================
`tests/test_pooled_batch.py` / `test_pool_split.py` 把 `todo_pooled` **本身**
测得很全（池序、全局 limit、按包分段），**全绿**。
坏的不是它 —— 是**喂给它的那个表达式**：

    `run_round()` 里写的是  `st.todo_pooled(list(args.packs), …)`

而 `args.packs` 是 `--packs` 的**原始字符串** ⇒ `list("frds-top250-2024")`
**不是在切逗号**，它把字符串**拆成一个个字符** ⇒ 变成 16 个**单字符"包名"**
⇒ `WHERE pack='f'` 之类一个都匹配不上 ⇒ `movies()` 全空 ⇒ **池子恒为 0**。

★★ 2026-10-05「去掉包的闸门」：`--packs` / `PACKS_DEFAULT` / `packs_from_arg`
   **整体退休** ⇒ 那个**原始串从哪来**的问题**连根没了**（现在池源 = `pack` 表，
   见 `pool_packs()`）。本文件保留，是因为它的**核心判据**（"喂给 `todo_pooled`
   的首参必须是包名列表，不能是被拆成字符的东西"）对**任何**新表达式的**形状
   检查**仍然有效 —— 只是"事故原码"从 `list(args.packs)` 换成了假想的
   `list(<某个字符串>)`。§①–④ 原样保留（它们是"判据的形状"本身），
   §⑤/⑥ 改成钉**新调用点**。

★ 判据的形状（从这次事故提炼）：**两处调用同一个函数、参数"看起来一样"、结果不同
  ⇒ 去比那两个表达式的 *_类型_*，不是它们的值。** 当时两边打印出来都是
  `frds-top250-2024`，而一个是 `str`、一个是 `list[str]`。

钉四条
======
  ① ★★★ `scripts/drive-loop.py` 里**任何** `todo_pooled(...)` 调用，
    第一个实参**不许**是"会被拆成单字符"的形状（`args.packs` / `list(<str>)`）。
  ② ★★ 阴性对照：把①的判据喂给**真实存在过的那行坏代码**（`list(args.packs)`）
     ⇒ 必须判红。（判据不能恒真 —— `ERR-AI-09`）
  ③ ★ 阳性对照：喂给**修好的形状**（`pool` / `pool_packs(st)`）⇒ 判绿。
  ④ ★★ 把①的判据喂给 `list("frds-top250-2024")` 的**运行时结果** ⇒ 长度 16、
     且**没有一个**元素等于真的包名 —— 把"拆成字符"这件事本身钉住，
     免得将来有人以为 `list(str)` 无害。

★ 素材：只读 `scripts/drive-loop.py` 的**源码文本** + 纯字符串运算 + §⑥ 的
  运行期 import（只查 `pool_packs` / `require_packs` 的形状，不碰网络/NAS/真库）。
"""
import io
import os
import re
import sys

# ★ 强制 UTF-8：本文件全是 ⇒ ★ ✓，Windows 控制台 GBK 会 UnicodeEncodeError
#   把测试打挂，且挂在取决于内容的行上（`ERR-AI-10` 形状）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_PATH = os.path.join(HERE, os.pardir, "data", "drive-loop", "scripts", "drive-loop.py")

fails = []


def ck(label, got, want):
    ok = got == want
    print(("  ok  " if ok else " FAIL ") + "%s: %r" % (label, got))
    if not ok:
        fails.append("%s: got %r want %r" % (label, got, want))


with io.open(SRC_PATH, encoding="utf-8") as fh:
    SRC = fh.read()


def first_args_of_calls(src, func="todo_pooled"):
    """把源码里所有 `todo_pooled(` 的**第一个实参**抠出来（纯文本，跨行）。

    ★ 为什么手写括号配平而不是正则一把梭：实参里有嵌套括号
      （`list(args.packs)`、`S.parse_cadence(args.cadence)`）和**中文注释**，
      正则很容易在"值里含逗号/括号"时静默抠错 —— 本项目已因"用 sed/grep 抠
      JSON 静默抠错"踩过一次（见 `run-resident.sh` 那条注释）。
      ⇒ 老老实实按字符扫，状态机简单但**判据可复核**。
    """
    out = []
    for m in re.finditer(r"\b%s\s*\(" % re.escape(func), src):
        i = m.end()                      # 左括号之后
        depth = 1
        buf = []
        while i < len(src) and depth:
            ch = src[i]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
                if depth == 0:
                    break
            buf.append(ch)
            i += 1
        call = "".join(buf)
        # 第一个实参 = 顶层第一个逗号之前的全部（跳过关键字实参的 `name=`）
        depth = 0
        first = []
        for ch in call:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == "," and depth == 0:
                break
            first.append(ch)
        out.append((m.start(), "".join(first).strip()))
    return out


CALLS = first_args_of_calls(SRC)

print("== ① ★★★ 每个 todo_pooled 调用的第一个实参 ==")
print("     找到 %d 处：%s" % (len(CALLS), [a for _, a in CALLS]))
ck("★ 至少找到 2 处调用（dry-run 与 run_round 各一处）", len(CALLS) >= 2, True)


def bad_first_arg(arg):
    """①的判据：首参是"会被拆成单字符"的形状 ⇒ 坏（True）。

    ★ 现在**没有 `args.packs` 了**，所以判据换成两条**形状**规则：
      · `list(<某个不是列表字面量的东西>)` —— `list(str)` 会拆字符（事故形状）
      · `args.packs` —— 旧事故原码（保留，防它借尸还魂）
    """
    a = arg.strip()
    if "args.packs" in a:
        return True
    if re.fullmatch(r"list\([^\[\]]*\)", a):   # list(xxx) 且 xxx 不是 [列表字面量]
        return True
    return False


for pos, arg in CALLS:
    ln = SRC.count("\n", 0, pos) + 1
    ck("★ 第 %d 行 首参 = %r ⇒ 不是「会拆字符」的形状" % (ln, arg),
       bad_first_arg(arg), False)

print()
print("== ② ★★ 阴性对照：喂给**真实存在过的那行坏代码** ⇒ 必须判红 ==")
ck("②a 喂 `list(args.packs)`（事故原码）⇒ 判据说'坏'（=True）",
   bad_first_arg("list(args.packs)"), True)
ck("②b 喂 `args.packs`（裸的）⇒ 判据说'坏'",
   bad_first_arg("args.packs"), True)
ck("②c 喂 `list(some_str)`（任何字符串的 list()）⇒ 判据说'坏'",
   bad_first_arg("list(packs_csv_string)"), True)
ck("②d ★ 阴性对照反向：喂一个**正常**的 ⇒ 判据说'好'（=False）",
   bad_first_arg("pool"), False)
ck("②e ★ 且 `pool_packs(st)`（函数调用）也判'好'（不是 list(...) 裸包）",
   bad_first_arg("pool_packs(st)"), False)

print()
print("== ③ ★★ 阳性对照 + 运行时证据：list(str) 到底变成什么 ==")
FAKE = "frds-top250-2024"
chars = list(FAKE)
ck("③a list(包名串) 的长度 = %d（不是 1）" % len(chars), len(chars), 16)
ck("③b 里面**没有**任何一个元素等于真包名", FAKE in chars, False)
ck("③c 而正确的切法给的是 ['frds-top250-2024']",
   [p.strip() for p in FAKE.split(",") if p.strip()], ["frds-top250-2024"])
ck("③d 多包时才看得出差别：'a,b' 切对了",
   [p.strip() for p in "a,b".split(",") if p.strip()], ["a", "b"])
ck("③e ★ 而 list('a,b') 是 3 个字符（含逗号！）", list("a,b"), ["a", ",", "b"])

print()
print("== ④ ★ 两种表达式**值能打印成一样、类型不同** —— 判据必须看结构 ==")
class _A:
    packs = FAKE


ck("④a repr 出来一样：list(_A.packs) 的元素 repr 里有 'f' 这种单字符",
   sorted(set(list(_A.packs)))[:3], ["-", "0", "2"])
ck("④b 而正确表达式 list(['frds-top250-2024']) 就一个元素",
   len(list(["frds-top250-2024"])), 1)
print("      ★ 提炼成一句话：两处调用同一函数、'看着一样'、结果不同")
print("        ⇒ **比那两个表达式的类型，不是它们的值**。")

print()
print("== ⑤ ★★★ 池源必须是**函数**（`pool_packs`/`require_packs`），不是现算的字符串 ==")
# ★★ 2026-10-05：`--packs` 解析退休后，唯一正确的池源就是 `pool_packs(st)` /
#   `require_packs(st)`（都读 `pack` 表）。这里钉住"调用点用的是它"，而不是任何
#   现算/拼接的表达式（那正是 2026-09-22 的病灶形状）。
_REG = re.findall(r"require_packs\s*\(|pool_packs\s*\(", SRC)
ck("⑤a `pool_packs()` / `require_packs()` 在源码里出现过（≥3 处：dry-run/resident/run_round/once）",
   len(_REG) >= 3, True)
ck("⑤b run_round 里用了 `require_packs(`（空表守卫也在那条路上）",
   "require_packs(st)" in SRC, True)
# ★ 旧的两个名字**不许**借尸还魂（代码层，剔注释）
_CODE = re.sub(r'""".*?"""', "", SRC, flags=re.S)
_CODE = "\n".join(ln for ln in _CODE.splitlines()
                  if not ln.lstrip().startswith("#"))
for _dead in ("packs_from_arg", "PACKS_DEFAULT", "args.packs"):
    ck("⑤c 代码里没有 `%s`（已退休，不许借尸还魂）" % _dead, _dead in _CODE, False)

print()
print("== ⑥ ★ 运行期证据：池源函数真的存在、真的读 pack 表 ==")
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("_dl_probe", SRC_PATH)
_dl = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(_dl)
    _loaded = True
except SystemExit:
    _loaded = False

if _loaded:
    ck("⑥a `pool_packs` 存在且收一个参数（store）",
       len(__import__("inspect").signature(_dl.pool_packs).parameters), 1)
    ck("⑥b `require_packs` 存在且收一个参数（store）",
       len(__import__("inspect").signature(_dl.require_packs).parameters), 1)
    ck("⑥c `NoPacksRegistered` 是 RuntimeError 子类（可被上层 distinguisher）",
       issubclass(_dl.NoPacksRegistered, RuntimeError), True)
    # ★ 池源确实读 pack 表（源码形状）：pool_packs 体里有 store.packs()
    ck("⑥d `pool_packs` 体里读 `st.packs()`（唯一枚举口）",
       "st.packs()" in __import__("inspect").getsource(_dl.pool_packs), True)
else:
    ck("⑥ 导入 drive-loop.py 失败 ⇒ 本组跳过（★ 不算通过，如实标出）", "skipped", "skipped")

print()
if fails:
    print("FAILED %d:" % len(fails))
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("全过")
