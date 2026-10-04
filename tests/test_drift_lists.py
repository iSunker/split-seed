# -*- coding: utf-8 -*-
"""两道闸门 —— 都长在**漂移哨兵自己**身上（`ERR-AI-09` 那一族）。

## 为什么这个文件必须存在

本会话（2026-09-24）实测到**两件事**，它们**都不是**代码坏了，而是**账没记**：

  ① 哨兵报「已跟踪但未登记 **2** 个」：`scripts/diag/q2.py` · `scripts/scan-tv-packs.py`
     —— 两个都**没进** `deploy.sh` 的 `FILES`、也**没进** `LOCAL_ONLY`。
     实测 `q2.py` 是 **09-21 就提交**的（`f9c67bb`），**哨兵红了 3 天没人看**。
  ② 哨兵 A 方向报「NAS 上未知 **3** 个」：`q.py` · `p.sh` · `_drive-loop-img.tar.gz`
     —— 前两个是**只存在于 NAS 上的一次性探针**（本机手工粘的），
     第三个是镜像 tar。它们**一个都不在版本库**。

★ 这两件事的**共同形状**（本文件存在的全部理由）：

> **「常红的闸门 = 没有闸门。」**
> 一个每天报 2~3 条噪音的哨兵，和它不存在几乎等价 —— 因为它真抓到东西时，
> 那一条会**混在同样的噪音里**，没人会多看一眼。

★★ 更值得记的是**为什么没有测试挡住它**：哨兵那两份清单（`LOCAL_ONLY` / `KNOWN_NAS`）
  是**手写数据**，而此前**没有任何断言要求它们和新文件同步**。
  这不是"逻辑写错了"，是**缺少一个"改完记得两边都改"的提醒**。

## 一段分两层（为什么不是一条）

真做起来会发现「未登记」有**两种性质**，哨兵眼里长得一样，处置**完全相反**：

| 形状 | 例子 | 正确处置 |
|---|---|---|
| **本机工具，忘登记了** | `scan-tv-packs.py` | 加进 `LOCAL_ONLY`（它**可以**登记，只是没人做） |
| **故意不部署 / 只活在 NAS 上** | `q.py` / `q2.py` / `p.sh` | 也得登记 —— 但理由写清"**故意不部署**" |

⇒ 所以判据不是"文件必须进白名单"，而是**「每个已跟踪文件都必须被明确表态过」**：
   要么在 `FILES`（会部署）、要么在 `LOCAL_ONLY`（声明不部署）。**没有第三条路。**

## 本文件钉什么

1. **每个测试脚本都在 `COUNTS.json` 里** —— 新测试忘了登记 ⇒ 计数闸门看不见它。
   （这一条钉的是**本文件自己**：它也得进 `notes`，否则它就成了下一个 `q2.py`。）
2. **`LOCAL_ONLY` / `KNOWN_NAS` 里没有"死规则"** —— 每条正则至少匹配一个文件。
   ★★ 死规则比缺规则更坏：它**看起来**已经登记了，所以下一个人不会再去加。
3. **NAS 侧那三个手工件有登记**（`q.py` / `q2.py` / `p.sh`）——
   它们的**真实落点**必须写在理由里（`q.py` = compose 根、`q2.py` = `docker cp` 进容器）。
   ★ 这一条是**把本会话踩的坑钉住**：我去 grep `q.py` 怎么上 NAS 时，
     仓库里**一处**都没写，只看到它 docstring 里的 `docker exec`；照那条路走会撞
     `No such file` —— 因为**真正的落点是 `cat > q.py` 粘到 compose 根**。
4. **受管集合（`DEPLOY_MANIFEST`）与 `LOCAL_ONLY` 不相交** —— 同一个文件不许两边都在
   （哨兵 B 方向按"先查受管集合再查 LOCAL_ONLY"判，两边都在 = 它会静默取前者，
     而人看清单会以为它"不部署" ⇒ **两处声明打架**）。

★ 行形只用 `  ok  ` / ` FAIL `（`tests/README.md` 那个重数器只认这三种）。
★ 全部离线：只读仓库里的 4 个文本文件，不碰 NAS、不碰网络。
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
# ★ 2026-10-05（B2）：容器代码收进 data/ ⇒ orchestrator / drive-loop 源码
#   都在 data/ 下。把 data/ 也加进 import 搜索路径（本文件上一行插的是仓库根）。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "data"))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

# ★ 2026-10-05：受管集合不再是「解析 deploy.sh 的 FILES」—— `deploy.sh` 已删除，
#   登记搬进哨兵内的常量 `DEPLOY_MANIFEST`。本测试直接读那个常量（经下面载入的 `M`）。
DRIFT_SRC = REPO / "scripts" / "diag" / "check-deploy-drift.py"
_ok = 0
_n = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _ok, _n
    _n += 1
    _ok += 1 if cond else 0
    print(("  ok  " if cond else " FAIL ") + "%s" % name)
    if not cond and detail:
        print("        ← " + detail)


# --------------------------------------------------------------------------- #
# 载入哨兵本体（★ 必须先 `sys.modules[name] = mod` 再 exec_module ——
#   见 `tests/README.md`「importlib 载进来的模块…」）。
# --------------------------------------------------------------------------- #
_name = "check_deploy_drift_for_lists"
_spec = importlib.util.spec_from_file_location(_name, DRIFT_SRC)
M = importlib.util.module_from_spec(_spec)
sys.modules[_name] = M
_spec.loader.exec_module(M)

# --------------------------------------------------------------------------- #
print("== ① ★★ 每个已跟踪文件都必须被**明确表态**过（DEPLOY_MANIFEST 或 LOCAL_ONLY）==")

# ★ 前提：两边至少有一个非空，否则下面那条 `all(...)` 会**空集通过**
#   （"没有文件"也能让"所有文件都登记了"成立 —— `ERR-AI-03` 的形状）。
check("前提：哨兵内置了非空的 DEPLOY_MANIFEST",
      len(getattr(M, "DEPLOY_MANIFEST", []) or []) > 0,
      "DEPLOY_MANIFEST 空 ⇒ 下面 ① 的『都登记了』什么也证明不了")
check("前提：LOCAL_ONLY 非空", len(M.LOCAL_ONLY) > 0, "清单空 ⇒ 同上是空集通过")

# ★★ 2026-10-05：`deploy.sh` 已删除 ⇒ 受管集合不再解析自它，而是哨兵内的常量
#   `DEPLOY_MANIFEST`：每条是 `(本地相对路径, 生产相对路径)` 二元组。
#   下面是「会部署的**源路径**」（左半边）。
_DEPLOYED = {s for s, _d in M.DEPLOY_MANIFEST}
check("前提：DEPLOY_MANIFEST 里有源路径", len(_DEPLOYED) > 0, repr(sorted(_DEPLOYED))[:200])

_local_rx = [rx for rx, _why in M.LOCAL_ONLY]
# ★★ 2026-10-05（B3）：第三条路 —— **走镜像**（IMAGE_ONLY）。哨兵 B 方向已加这一类
#   （判据：既不进 FILES、也不进 LOCAL_ONLY，但**在容器里跑**，靠 build 进镜像）。
#   ⇒ 本测试的 `_declared()` 必须跟着认它，否则会把 data/orchestrator/* 误报成"未表态"。
_image_rx = [rx for rx, _why in getattr(M, "IMAGE_ONLY", [])]


def _declared(rel: str) -> bool:
    return (rel in _DEPLOYED
            or any(re.search(rx, rel) for rx in _local_rx)
            or any(re.search(rx, rel) for rx in _image_rx))


# ★★ 哨兵 B 方向**只看已跟踪文件**（`git ls-files`）—— 这里照抄同一条口径：
#   非 git 文件不在它管辖内（那份名单是 `GENERATED_OK`，另一条路）。
_git = REPO / ".git"
check("前提：这是个 git 工作树（哨兵 B 方向的口径就是它）", _git.exists(),
      "没有 .git ⇒ 下面的清单只能靠人工，别硬跑")


def _tracked() -> list[str]:
    """已跟踪文件名（★ **关掉 git 自己的 C-quoting**）。

    ★★ 为什么不能直接 `subprocess.run(["git","ls-files"], text=True)`（我第一版就是这么写的）：
      本仓有**中文文件名**（`summary/00-卡片索引.md` 等），而 `core.quotePath` 默认 true
      时 git 会把非 ASCII 路径输出成 `"summary/00-\\345\\215\\241..."`（C 风格转义 + 双引号）。
      ⇒ 拿到的**根本不是路径**，而 `str.startswith("summary/")` 自然为假 ⇒
        那条 `^summary/` 规则被判成"死规则"（**假红**）。实测：167 个已跟踪文件里，
        只要含中文，`grep -c "^summary/"` 就是 **0**。
      ★ 这与本文件 ① 段那条「口径必须同形」是同一族的坑，只是换了层：
        这次错的是**取数方式**（`ERR-AI-07` 形状：指针指对了，编码把值改了）。
      ⇒ `-z` 让 git 用 NUL 分隔且**不转义**；`encoding="utf-8"` 明确解码。
    """
    import subprocess
    out = subprocess.run(["git", "ls-files", "-z"], cwd=str(REPO),
                         capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    return [p for p in out.stdout.split("\0") if p.strip()]


_tracked_files = _tracked()
check("前提：git ls-files 有输出", len(_tracked_files) > 50,
      "拿不到已跟踪清单（%d 个）⇒ 别在这儿报绿" % len(_tracked_files))

# ★ 只判 `scripts/` 下与根目录的 `.py`/`.sh` —— 文档与 tests/ 由哨兵自己的
#   那几条宽规则兜（`^tests/`、`^summary/`），在这边重判一遍就是**两处口径**。
#   ★ 判据是"**没有被任何规则匹配到**"（与哨兵 B 方向逐字同形），不是"在白名单里"。
_undeclared = []
for rel in _tracked_files:
    if not (rel.endswith(".py") or rel.endswith(".sh")):
        continue
    if _declared(rel):
        continue
    _undeclared.append(rel)

check("★★ 所有已跟踪的 .py/.sh 都被表态过（DEPLOY_MANIFEST 或 LOCAL_ONLY）",
      not _undeclared,
      "未表态 %d 个：%s\n        ← 处置二选一：①会部署就加进哨兵的 DEPLOY_MANIFEST；"
      "②不部署就加进 LOCAL_ONLY（**写明理由**）"
      % (len(_undeclared), ", ".join(sorted(_undeclared)[:20])))

# --------------------------------------------------------------------------- #
print("\n== ② ★★ 两份清单里**不许有死规则**（死规则比缺规则更坏）==")
#   死规则 = 那条正则**匹配不到任何已跟踪文件**。
#   ★ 它比"缺规则"更坏的**唯一**理由：缺规则会让人（或哨兵）**报出来**，
#     而死规则**看起来已经登记了** ⇒ 下一个人不会再去看它一眼。
#     本项目已在 `ALERT 只打在杂物上` 那条反向结构检查上吃过同形的亏。

# ★★ 例外一条，且**只此一条**：`^summary/` 是**目录前缀**规则 —— 它匹配的是
#   `summary/xx.md` 这类**路径**，而本文件上面把口径收窄成了「`.py`/`.sh`」。
#   ⇒ 在这份收窄后的清单里它必然"匹配不到"，但那**不是死规则**，是**口径不同**
#     （判"死"要拿**同口径**的清单判，否则就是在量错对象 —— `A.11`）。
#   ★ 用路径前缀判它活着：目录里至少有一个已跟踪文件。
_DEAD_WHITELIST = {r"^summary/"}           # ★ 加新条目必须写清"为什么它不是死规则"


def _alive(rx: str, why: str) -> bool:
    if rx in _DEAD_WHITELIST:
        # 目录前缀规则：目录下还有已跟踪文件就算活着
        prefix = rx.rstrip("$").lstrip("^")
        return any(f.startswith(prefix) for f in _tracked_files)
    return any(re.search(rx, f) for f in _tracked_files)


_dead_local = [(rx, why) for rx, why in M.LOCAL_ONLY if not _alive(rx, why)]
check("★ LOCAL_ONLY 没有死规则（白名单 %d 条按目录前缀判）"
      % len(_DEAD_WHITELIST), not _dead_local,
      "匹配不到任何已跟踪文件 %d 条：\n        ← %s"
      % (len(_dead_local), "\n        ← ".join(rx for rx, _ in _dead_local[:10])))

#   ★ `KNOWN_NAS` 判死**不能**用本机文件清单（它管的是 NAS 上的文件）——
#     那不是同一个文件系统（`A.11`：判据指向了错的对象）。
#     但它的**形状**可以静态判：正则必须**语法合法**且**不是空匹配**。
#   ★ 规则是三元的 `(正则, 说明[, 严重度])`（只有杂物那条带 `ALERT`）——
#     所以解包必须用 `rule[0]` / `rule[1]`，**不能** `for rx, why in ...`。
_bad_nas_rx = []
for _rule in M.KNOWN_NAS:
    rx, why = _rule[0], _rule[1]
    try:
        c = re.compile(rx)
    except re.error as e:  # noqa: PERF203
        _bad_nas_rx.append((rx, "编译失败: %s" % e))
        continue
    # ★ 空匹配（`re.compile("")` 或 `.*`）会把**所有**文件都吃掉 ——
    #   那等于把整个 A 方向关掉（"没有未知文件"永远成立）。
    if c.match("") is not None and c.pattern != r"^_AW7IV~D$":
        _bad_nas_rx.append((rx, "能匹配空串 ⇒ 会吃掉一切（A 方向失效）"))
check("★ KNOWN_NAS 每条正则都能编译、且不吃空串", not _bad_nas_rx,
      "%s" % (_bad_nas_rx[:5],))

# --------------------------------------------------------------------------- #
print("\n== ③ ★ NAS 侧手工件的**真实落点**必须写在理由里 ==")
#   本会话实测踩的坑：想找 `q.py` 是怎么上 NAS 的，仓库里**一处都没有**，
#   只看得到它 docstring 里的 `docker exec` —— 而真正的落点是 `cat > q.py`
#   粘到 **compose 根**。⇒ 下面三条把落点钉住，防下一个人照 docstring 走错。
_nas_reasons = {r[0]: r[1] for r in M.KNOWN_NAS}
for _rx, _need in ((r"^q\.py$", "compose"),
                   (r"^q2\.py$", "docker cp"),
                   (r"^p\.sh$", "宿主")):
    _why = _nas_reasons.get(_rx, "")
    check("★ %s 有登记，且理由写明了落点（含「%s」）" % (_rx, _need),
          bool(_why) and _need in _why,
          "理由=%r ← 没写落点的话，下一个人会照 docstring 的 `docker exec` 去试，"
          "然后撞 `No such file`（本会话实测）" % (_why,))

# --------------------------------------------------------------------------- #
print("\n== ④ ★ 受管集合与 LOCAL_ONLY **不许相交**（两处声明打架）==")
#   哨兵 B 方向是"先查受管集合再查 LOCAL_ONLY"⇒ 两边都在时它会**静默取前者**，
#   而人读清单会以为这个文件"不部署" ⇒ **两处口径不一致**，正是本仓最忌的形状。
_overlap = sorted(f for f in _DEPLOYED
                  if any(re.search(rx, f) for rx in _local_rx))
check("★ 没有文件同时出现在受管集合和 LOCAL_ONLY", not _overlap, repr(_overlap))

# --------------------------------------------------------------------------- #
print("\n== ⑤ ★★ 本文件自己也得在 `COUNTS.json` 里（它是今天第 N 个 q2.py 的候选）==")
#   ★ 这条**故意钉自己**：新测试忘了登记 ⇒ 计数闸门（`test_readme_counts.py`）
#     看不见它 ⇒ 它会成为下一个"提交了三天没人知道"的文件。
_counts = REPO / "tests" / "COUNTS.json"
check("前提：tests/COUNTS.json 在", _counts.is_file(), str(_counts))
if _counts.is_file():
    _c = json.loads(_counts.read_text(encoding="utf-8"))
    _notes = _c.get("notes") or {}
    check("★★ test_drift_lists.py 在 COUNTS.json 的 notes 里",
          "test_drift_lists.py" in _notes,
          "不在 ⇒ 它是**未登记**的测试，与 `q2.py` 同一个形状")

# --------------------------------------------------------------------------- #
print()
if _ok != _n:
    print("!!! %d/%d 失败" % (_n - _ok, _n))
    sys.exit(1)
print("全部通过（%d 条）" % _n)
