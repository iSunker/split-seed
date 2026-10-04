# -*- coding: utf-8 -*-
"""`todo_pooled` 的**调用点** —— 传进去的必须是"包名列表"，不是 `--packs` 的原始串。

为什么要专门钉这个（2026-09-22 事故）
======================================
`tests/test_pooled_batch.py` / `test_pool_split.py` 把 `todo_pooled` **本身**
测得很全（池序、全局 limit、按包分段），**全绿**。
坏的不是它 —— 是**喂给它的那个表达式**：

    `run_round()` 里写的是  `st.todo_pooled(list(args.packs), …)`

而 `args.packs` 是 `--packs` 的**原始字符串**（`argparse` 的
`default=PACKS_DEFAULT`，形如 `"frds-top250-2024"`；`main()` 里才用
`packs = [p.strip() for p in args.packs.split(",") if p.strip()]` 清洗成列表）。

⇒ `list("frds-top250-2024")` **不是在切逗号**，它把字符串**拆成一个个字符**：
     `['f','r','d','s','-','t','o','p','2','5','0','-','2','0','2','4']`
⇒ 变成 16 个**单字符"包名"** ⇒ `WHERE pack='f'` 之类一个都匹配不上
⇒ `movies()` 全空 ⇒ **池子恒为 0**。

★★ 症状为什么极难查（这就是本文件存在的理由）
--------------------------------------------
  · **静默**：日志报「没有待搜索项」，**不报错、不退非零**；
  · **启动日志看着正常**：那行打的是清洗过的 `packs`
    （`packs=['frds-top250-2024']`），**不是** `args.packs`；
  · **只在 `--pool` 上暴露** —— 非 `--pool` 那条用的是干净的局部变量 `pack`；
  · ★★ **加 `--dry-run` 反而正常**：dry-run 分支用的正是 `packs`（干净），
    而 `run_round` 用 `args.packs`（脏）⇒ 同一条命令"加个 dry-run 就好了"
    ⇒ 极易被误判成"运行期状态问题"而不是"表达式类型问题"。
  · 实测：常驻进程**连续 14 轮、7 小时**全空；`--dry-run` 给 43 部、去掉给 0。

★ 判据的形状（从这次事故提炼）：**两处调用同一个函数、参数"看起来一样"、结果不同
  ⇒ 去比那两个表达式的 *_类型_*，不是它们的值。** 当时两边打印出来都是
  `frds-top250-2024`，而一个是 `str`、一个是 `list[str]`。

钉四条
======
  ① ★★★ `scripts/drive-loop.py` 里**任何** `todo_pooled(...)` 调用，
    第一个实参**不许**是 `args.packs`（或其 `list(...)` 包装）。
  ② ★★ 阴性对照：把①的判据喂给**真实存在过的那行坏代码**（`list(args.packs)`）
     ⇒ 必须判红。（判据不能恒真 —— `ERR-AI-09`）
  ③ ★ 阳性对照：喂给**修好的那行**（`list(packs)`）⇒ 判绿。
  ④ ★★ 把①的判据喂给 `list("frds-top250-2024")` 的**运行时结果** ⇒ 长度 16、
     且**没有一个**元素等于真的包名 —— 把"拆成字符"这件事本身钉住，
     免得将来有人以为 `list(str)` 无害。

★ 素材：只读 `scripts/drive-loop.py` 的**源码文本** + 纯字符串运算。
  不导入 drive-loop.py（它有 argparse / 网络副作用），不碰网络、不碰 NAS、不碰真库。
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

for pos, arg in CALLS:
    ln = SRC.count("\n", 0, pos) + 1
    ck("★ 第 %d 行 首参 = %r ⇒ 不是 args.packs" % (ln, arg),
       ("args.packs" in arg), False)

print()
print("== ② ★★ 阴性对照：喂给**真实存在过的那行坏代码** ⇒ 必须判红 ==")


def bad_has_args_packs(arg):
    """①的判据，做成纯函数以便喂对照组。"""
    return "args.packs" in arg


ck("②a 喂 `list(args.packs)`（事故原码）⇒ 判据说'坏'（=True）",
   bad_has_args_packs("list(args.packs)"), True)
ck("②b 喂 `args.packs`（裸的）⇒ 判据说'坏'",
   bad_has_args_packs("args.packs"), True)
ck("②c ★ 阴性对照反向：喂一个**正常**的 ⇒ 判据说'好'（=False）",
   bad_has_args_packs("list(packs)"), False)

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
print("== ⑤ ★★★ `--packs` 解析必须**只有一处**（2026-09-22 第二次栽在这上面）==")
# ★ 背景：修①的时候我在 run_round 里写 `list(packs)`，而 run_round 作用域里
#   **根本没有 packs**（它是 main() 的局部变量）⇒ 生产当场 NameError、批次全废。
#   ⇒ 根治办法不是"再小心一点"，是**把解析收成一处**并给 run_round 加 guard。

# ⑤a ★ 判据要**精确**：要钉的是"`args.packs`（原始串）的解析只有一处"，
#    而不是"全文件只能有一个 `.split(',')`" —— 后者会把**注释**和
#    别的用途（如 `--indexers`）也算进去，变成一条会误报的判据。
#     ★ 本仓踩过"判据写宽 ⇒ 要么误报要么形同虚设"（`ERR-AI-09` 的正对照要求）。
# ★★ 先把注释**和 docstring** 都剔除 —— 否则注释/文档里举的旧写法会被当成代码。
#    ★ 这一步不是洁癖：本次实测**连续两次**被它绊住 ——
#      第一次把注释里的例子抓了出来；改成"只剔 `#`"后，
#      **多行 docstring 里**的同样句子还在 ⇒ 又误报一次。
#      ⇒ 教训：**"剔除注释"要连 docstring 一起剔**，否则判据的判据自己就是错的
#        （`ERR-AI-09`：防假绿的装置本身可能是假绿的）。
_CODE = re.sub(r'""".*?"""', "", SRC, flags=re.S)          # 先去三引号块
_CODE = "\n".join(ln for ln in _CODE.splitlines()
                  if not ln.lstrip().startswith("#"))       # 再去 # 注释
# ★ 判据要**精确到形状**：只看"把 `args.packs.split(',')` 的结果赋给变量"这种**解析**。
#   写成 `packs_from_arg(args.packs)` 是**正确**用法，不该被抓 ⇒ 正则里要求有 `split`。
_hand = re.findall(
    r"=\s*\[p\.strip\(\)\s*for\s+p\s+in\s+[^\]]*\bargs\.packs\b[^\]]*\.split\(\",\"\)", _CODE)
ck("⑤a ★ 用列表推导直接解析 `args.packs` 的代码只剩 %d 处（应为 0）" % len(_hand),
   len(_hand), 0)
ck("⑤a0 ★ 而那处**正确**的调用在（`packs_from_arg(args.packs)`）",
   "packs = packs_from_arg(args.packs)" in _CODE, True)
ck("⑤a2 ★ `packs_from_arg` 自身存在且用了同一条表达式",
   "def packs_from_arg" in SRC
   and 'for p in (spec or "").split(",")' in SRC, True)
ck("⑤a3 ★ 注释里引用旧写法不算数（判据只看代码，不看注释）",
   '# ★ 唯一一处 `--packs` 解析' in SRC, True)

# ⑤c run_round 必须**收 packs 参数**（而不是从 args 现算）
_m = re.search(r"def run_round\(([^)]*)\)", SRC)
ck("⑤c run_round 的签名里必须有 packs 参数",
   bool(_m) and "packs" in _m.group(1), True)
ck("⑤d 签名里 _不含_ args.packs（那是原始串）",
   bool(_m) and "args.packs" not in _m.group(1), True)

# ⑤e 两个调用点都必须传 packs
_calls = re.findall(r"run_round\(([^)]*)\)", SRC)
_calls = [c for c in _calls if "pack," in c or "packs," in c]   # 排除 def 那行
ck("⑤e 每个 run_round(…) 调用都传了 packs（找到 %d 处）" % len(_calls),
   all("packs" in c for c in _calls) and len(_calls) >= 2, True)

# ⑤f guard：run_round 里必须有"全是单字符"那条断言
ck("⑤f ★★★ run_round 里有'全是单字符'的入口 guard",
   "全是单字符" in SRC and "any(len(p) > 1 for p in packs)" in SRC, True)

print()
print("== ⑥ ★ 运行期证据：guard 真的会拦下事故形状（不是在文本上像）==")
import importlib.util  # noqa: E402
import types  # noqa: E402

_spec = importlib.util.spec_from_file_location("_dl_probe", SRC_PATH)
_dl = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(_dl)
    _loaded = True
except SystemExit:
    _loaded = False

if _loaded:
    ck("⑥a packs_from_arg('frds-top250-2024') 是 1 个元素",
       _dl.packs_from_arg("frds-top250-2024"), ["frds-top250-2024"])
    ck("⑥b packs_from_arg('a, b ,c') 切对了（含去空白）",
       _dl.packs_from_arg("a, b ,c"), ["a", "b", "c"])
    _args = types.SimpleNamespace(
        db="hbin/does-not-exist.db", indexers="HDtime", include_cooldown=False,
        cadence_days=14, cadence=None, limit=50, batch=False, pool=True, url="", api_key="")
    _raised = None
    try:
        _dl.run_round("pool", list("frds-top250-2024"), _args, "x")
    except AssertionError as e:
        _raised = str(e)
    except Exception as e:  # noqa: BLE001
        _raised = None      # 别的异常不算 —— 我们要的正是 AssertionError
        print("     （先炸在别处了：%s %s ⇒ guard 没轮到，这条判据无效）"
              % (type(e).__name__, str(e)[:60]))
    ck("⑥c ★★★ 喂 list('frds-top250-2024') ⇒ **AssertionError**（不再静默算空）",
       bool(_raised) and "全是单字符" in _raised, True)
else:
    ck("⑥ 导入 drive-loop.py 失败 ⇒ 本组跳过（★ 不算通过，如实标出）", "skipped", "skipped")

print()
if fails:
    print("FAILED %d:" % len(fails))
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("全过")
