#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行号引用（`file.py:123`）的**存在性**检查 —— 只报"铁定失效"，不猜"指得准不准"。

为什么单独立这一份（2026-09-20）
--------------------------------
前四份投影管的锚点全是**符号级**的（`§N` / `ERR-XX-NN` / `「标题」`）——
**符号不会漂**。而文档里还有一族 **位置级** 引用：`drive-loop.py:1487`。
它**天然会漂**，本仓已经吃过两次亏，`README` 里也已立过规矩：

    「文档里写的 `drive-loop.py:1487` 被我加几行注释推成了 1510
      —— 所以**按符号引用，不按行号**。」

★ 那条规矩立了，但**没有任何验证器守着它** —— 这就是本脚本补的洞。

★★ 判据刻意**只管两件铁定的事**（宁可少报，不报假警报）
-----------------------------------------------------------
  ① **目标文件不存在**（路径整个没了 / 拼错）
  ② **行号 > 文件总行数**（引用指向"文件外面"，绝无可能还有意义）

★ **刻意不做**的事：**不判**"行号还指不指得对那一行"。
  那个判据**必然假红** —— 内容随时会被改（加注释、改措辞），而"内容变了"
  和"引用坏了"是两回事。本仓已经为这类"看起来更严"的判据付过代价
  （见 `B.10`：「判据写错的样子和代码写错长得一样」）。
  ⇒ 位置级引用的**正确归宿是换成符号引用**（那是改文档的活，见本文件末尾"后续"），
    本脚本只负责**把已经烂掉的那些抓出来**，不假装能维护它们。

★ 为什么这是**真缺口**而不是"洁癖"：2026-09-20 本轮实测 ——
  我自己写 `summary/26` 时引了 `notify-spool.sh:746`，**当次会话内**因为改同一个文件
  就漂到了 `:760`（引用变成指向一句注释）。**没人证明同步 = 一定会漂。**

用法：python 05_linerefs.py <仓库根>        （默认仓库根）
输出：<脚本目录>/out/linerefs.tsv
退出码：0 = 无铁定失效；1 = 有（可直接当闸门用）
"""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent.parent
DOCS_ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else REPO).resolve()
OUT = TOOLS / "out"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mdwalk import doc_files, iter_lines as walk  # noqa: E402

#: `file.ext:123` / `file.ext:123-145`（取起点 123）。
#: ★ 只认**带扩展名**的形态 —— 裸 `:123` 在散文里（"见 §26.2:74"这种）误报率太高。
#: ★ 目录也允许（`scripts/diag/chk58.sh`）—— 用 `[\w./-]+` 收住。
REF_LINE = re.compile(r"\b([A-Za-z_][\w./-]*\.(?:py|sh|md|yml|js|tsv)):(\d+)(?:-\d+)?")

#: 建索引时要跳过的目录（与 mdwalk 的口径一致：不是文档，也不该被引用到）。
SKIP_DIRS = {".git", ".deploy-backup", ".claude", "__pycache__",
             ".venv-html-to-docx", "node_modules", "out"}

#: ★★ **NAS 侧的部署名** —— 文档会引它们，但它们**在仓库里不存在**。
#:   来源：`deploy.sh` 的 FILES 映射（本地路径 :: NAS 相对路径）。凡两侧**不同名**的，
#:   两侧都是**合法的引用目标**（取决于文档在说"仓库里"还是"NAS 上"）。
#: ★ 这条是**实测补上的假警报**：`run-resident.sh` 被引 3 次、仓库里找不到 ⇒ 我第一版
#:   报「NO-FILE」，而它**是** `drive-loop/run-resident.sh`（本地叫
#:   `scripts/drive-loop-resident.sh`）—— 94 处这么引，全对。
#:   ⇒ 判据不能只看"仓库里有没有"（同 `B.10` 第 1 条：「某台机器上没有」≠「环境里都没有」）。
#: ★ 维护方式：**只在两侧不同名时才需要登记**。`deploy.sh` 里同名的那一堆不用列。
DEPLOY_ALIASES = {
    # ★★ 2026-10-05（B2/B3）：本地 compose 已改名 `docker-compose.yml` → `compose.yaml`。
    #   索引按**文件名**建键 ⇒ 旧文档（`summary/*` 等历史记录）里引的
    #   `docker-compose.yml:NNN` 会找不到文件。★ 那些是**过程记录、不追改**
    #   （`A.11`）⇒ 用别名把它们**指向现在的本地文件**（同一份，只是改了名），
    #   让行号判据仍能对其生效。
    "docker-compose.yml": "compose.yaml",
    # NAS 侧的部署名（`deploy.sh` 的 FILES 映射）—— 文档引它们时仓库里可能不同名。
    # ★ 2026-10-05：源文件已随 data/ 收拢到 `data/drive-loop/`；别名给**新路径**。
    "run.sh": "data/drive-loop/run.sh",
    "run-resident.sh": "data/drive-loop/run-resident.sh",
}


def build_index(root: Path) -> dict:
    """`文件名 → [完整路径…]`。

    ★ 用**文件名**做键（文档里就只写文件名，如 `drive-loop.py`），
      因为那是引用里唯一能拿到的信息。
    ★ 同名多份时**全部保留**并标记歧义 —— 本仓确实有同名副本
      （`<compose>/drive-loop/scripts/drive-loop.py` 与镜像里那份），
      此时**报最宽松的那个**（行号能以任何一份成立就算成立），
      避免把"引用的其实是你没想到的那一份"当成失效。
    """
    idx = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() not in (".py", ".sh", ".md", ".yml", ".yaml", ".js", ".tsv"):
            continue
        idx.setdefault(p.name, []).append(p)
    return idx


def resolve(name: str, idx: dict) -> list:
    """把文档里的引用名解析成候选路径。

    ★ 两跳：① 直接按文件名找；② 找不到时，**再试 NAS 侧的部署名反查**
      （`run-resident.sh` → `scripts/drive-loop-resident.sh`）。
    ★ 反查命中 ⇒ 用本地那份做行数判据（同一份文件，只是部署时改了名）。
    """
    cands = idx.get(Path(name).name)
    if cands:
        return cands
    local = DEPLOY_ALIASES.get(Path(name).name)
    if local:
        return idx.get(Path(local).name, [])
    return []


def count_lines(p: Path) -> int:
    try:
        return len(p.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return -1


# ★★ **已知失效、但故意不修**的引用 —— 「过程记录不追改」（`A.11`）。
#
# 判据：**搬迁走的文件，其行号引用必然越界**，而**正文不该被改写**去迁就行号。
# 2026-09-20 README 拆分（`94a3d0f`）时实测：README 1788 → 600 行 ⇒
# 4 处 `README.md:NNNN` 全部越界。但**其中 3 处的文字早已改指符号锚**
# （`ENVIRONMENT.md A.18.1.4` / `A.19`），**剩下的命中是"改指说明本身"里
# 为了留证据而抄下的旧行号**，以及 `INDEX-USAGE` 明写的
# 「过程记录里的话不追改」。⇒ 记进这张表，让它**报出来但不装作是新问题**。
#
# ★ 表里每一条都要写清**为什么留着** —— 不静默跳过（同 `B.10` 第 14 条）。
#
# ★★★ **2026-09-20 修一个我自己的设计缺陷**（★ 这条比表本身重要）：
#   本表原先的键是 `(文件, 行号, 目标)` —— **键里含有行号**。于是**任何在它上面的增删行**
#   都会让这条豁免**失配**，把"已知"变成"新出现"。
#   实测：本会话（`e404b1ca`）在 `summary/26` 末尾追加 65 行 ⇒ 三条豁免从
#   `806/2800/2803` 漂到 `815/2809/2812` ⇒ 闸门**当场变红**，而**正文一个字都没错**。
#   ⇒ 这与本脚本头注批评的**同一件事**：**行号会漂，不该拿它当身份**。
#   ⇒ 改成**按目标 + 「为什么留着」的关键词**匹配（行号只作参考、不参与判定）。
#     判据：**豁免的键必须是"不随编辑漂移"的东西**（目标名 + 理由），不是位置。
KNOWN_STALE = {

    ("26-跨会话任务盘面与旧会话清场.md", "README.md:1385"):
        "迁移注记：为留证据抄下的旧行号，文字已改指 ENVIRONMENT.md A.19",
    ("26-跨会话任务盘面与旧会话清场.md", "README.md:1225"):
        "迁移注记：同上，已改指 A.18.1.4",
    ("30-qb内存排查是一个重复推导.md", "README.md:1856"):
        "迁移注记：同上，已改指 A.18.1.5",
    # ★ 2026-09-20 README 拆分：`tests/README.md` 的读数史**整段搬进** `summary/31`，
    #   里面那条「README.md:1225」是**逐字搬来的历史读数**（当时的行号）。
    #   正文不许改（`A.11`「过程记录不追改」）⇒ 记在这里。
    ("31-测试计数的口径与历史.md", "README.md:1225"):
        "整段搬来的读数史：原行号留档，正文不改（A.11 过程记录不追改）",

    # ★★ 2026-10-05（Task C：通知模块剥离）：`notify.py` / `notify-spool.sh` 已从仓库
    #   **删除**（整体剥离到独立项目，见 `重构方案.md` §11）⇒ 所有引用它们的行号引用
    #   都成了 NO-FILE。这些引用**全在历史记录里**（`summary/*` = 会话存档；
    #   `ENVIRONMENT.md` 里那两条也是"当时的现场记录"）—— **正文不许改**
    #   （`A.11`「过程记录不追改」）⇒ 逐条登记。
    #   ★ 键 = (文档, "目标:行号")，与上表同格式（代码里 `r[2]` 含行号）。
    ("18-目录布局调整.md", "notify-spool.sh:334"):
        "历史存档：notify-spool.sh 已随通知模块剥离而删除（Task C）；正文不追改",
    ("20-观测层语义收敛.md", "notify.py:95"):
        "历史存档：notify.py 已随通知模块剥离而删除（Task C）；正文不追改",
    ("23-卷归属复核与SMB探针分辨力.md", "notify.py:198"):
        "历史存档：notify.py 已删除（Task C）；正文不追改",
    ("23-卷归属复核与SMB探针分辨力.md", "notify-spool.sh:713"):
        "历史存档：notify-spool.sh 已删除（Task C）；正文不追改",
    ("23-卷归属复核与SMB探针分辨力.md", "notify-spool.sh:738"):
        "历史存档：notify-spool.sh 已删除（Task C）；正文不追改",
    ("26-跨会话任务盘面与旧会话清场.md", "notify.py:97"):
        "历史存档：notify.py 已删除（Task C）；正文不追改",
    ("26-跨会话任务盘面与旧会话清场.md", "notify-spool.sh:163"):
        "历史存档：notify-spool.sh 已删除（Task C）；正文不追改",
    ("26-跨会话任务盘面与旧会话清场.md", "notify-spool.sh:65"):
        "历史存档：notify-spool.sh 已删除（Task C）；正文不追改",
    ("26-跨会话任务盘面与旧会话清场.md", "notify-spool.sh:72"):
        "历史存档：notify-spool.sh 已删除（Task C）；正文不追改",
    ("26-跨会话任务盘面与旧会话清场.md", "notify-spool.sh:746"):
        "历史存档：notify-spool.sh 已删除（Task C）；正文不追改",
    ("26-跨会话任务盘面与旧会话清场.md", "notify-spool.sh:866"):
        "历史存档：notify-spool.sh 已删除（Task C）；正文不追改",
    ("26-跨会话任务盘面与旧会话清场.md", "scripts/notify.py:233"):
        "历史存档：notify.py 已删除（Task C）；正文不追改",
    ("26-跨会话任务盘面与旧会话清场.md", "scripts/notify-spool.sh:850"):
        "历史存档：notify-spool.sh 已删除（Task C）；正文不追改",
    ("ENVIRONMENT.md", "notify-spool.sh:671"):
        "现场记录：notify-spool.sh 已删除（Task C）；该处是当时的操作留档，正文不追改",
}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    idx = build_index(DOCS_ROOT)
    files = doc_files(DOCS_ROOT)
    if not files:
        print("[!!] %s 下没有 .md —— 文档目录传对了吗？" % DOCS_ROOT)
        return 0

    rows = []          # (src, line, ref, status, detail)
    total, unres, over = 0, 0, 0

    for f in files:
        # ★ 用 iter_lines（跳过围栏内）—— 代码示例里的 `foo.py:12` 是示例，不是引用。
        #   口径与 02_refs 的族⑥一致。
        for lineno, text in walk(f):
            for m in REF_LINE.finditer(text):
                total += 1
                name, ln = m.group(1), int(m.group(2))
                cands = resolve(name, idx)
                if not cands:
                    unres += 1
                    rows.append((f.name, lineno, "%s:%d" % (name, ln),
                                 "NO-FILE", "目标文件在仓库里找不到（也不是部署名）"))
                    continue
                # 同名多份 ⇒ 取**最宽松**的判据（任一份容得下就算过）
                best = max(count_lines(c) for c in cands)
                if ln > best:
                    over += 1
                    rows.append((f.name, lineno, "%s:%d" % (name, ln),
                                 "OVER-EOF",
                                 "最长的同名文件只有 %d 行" % best))
                else:
                    rows.append((f.name, lineno, "%s:%d" % (name, ln), "OK", ""))

    with (OUT / "linerefs.tsv").open("w", encoding="utf-8") as fp:
        fp.write("src\tline\tref\tstatus\tdetail\n")
        for r in rows:
            fp.write("\t".join(map(str, r)) + "\n")

    bad = [r for r in rows if r[3] != "OK"]
    # ★ 键 = **(文件, 目标)** —— **不含行号**（行号会随编辑漂，见 KNOWN_STALE 头注）。
    #   同一文件里同一条目标出现多次（如两处都引 `README.md:1225`）⇒ **都算已知**。
    known = [r for r in bad if (r[0], r[2]) in KNOWN_STALE]
    fresh = [r for r in bad if (r[0], r[2]) not in KNOWN_STALE]
    print("扫描 %d 份文档，%d 条行号引用" % (len(files), total))
    print("  OK          : %d" % (total - unres - over))
    print("  NO-FILE     : %d   （目标文件不存在）" % unres)
    print("  OVER-EOF    : %d   （行号超出文件总行数）" % over)
    print("    其中**已知且故意保留**：%d（搬迁注记 / 过程记录不追改）" % len(known))
    print()
    if known:
        print("· 已知失效（**不修**，每条都写清了理由 —— 不静默跳过）：")
        for r in known:
            print("    %s:%s  →  %s\n        %s" % (r[0], r[1], r[2], KNOWN_STALE[(r[0], r[2])]))
        print()
    if fresh:
        print("★ **新出现**的铁定失效（%d 条）—— 这个数才是闸门：" % len(fresh))
        for r in fresh:
            print("  %s:%s  →  %s   [%s] %s" % (r[0], r[1], r[2], r[3], r[4]))
        print()
        print("★ 修法：**换成符号引用**（按名字/章节锚，不按行号）—— 见本文件头注。")
    else:
        print("[ok] 无**新出现**的铁定失效行号引用。")
    print("→ %s" % (OUT / "linerefs.tsv"))
    return 1 if fresh else 0


if __name__ == "__main__":
    sys.exit(main())
