#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scan-tv-packs —— **只读**扫描 `TV/` 这棵树，报告「每个条目该从第几层找发布名」。

为什么需要它（`summary/10` §10.2 那个 DC 坑的一般化）
------------------------------------------------------
`cross-seed` 用**目录名**去站点搜索。若 dataDir 给得太浅，那层**中文标签**
（`01.蝙蝠侠1：侠影之谜 (2005)`）会被当成 searchee 名 ⇒ **搜不到任何东西，
却照样消耗查询额度**。若给得太深，又会把一个包拆成互不相干的碎片。

`reseed-state.py init` 已经能扫（`orchestrator.state.scan_pack`）——
**它缺的不是"识别算法"，是"该喂它什么参数"**：包根是谁、`--depth` 给几。
本脚本补的就是这一步，**且只报告、不注册**。

★★ 为什么是「报告」而不是「自动注册」（`summary/19` §19.1）
----------------------------------------------------------
那条铁律：**包是「声明」的，不是「识别」的** ——
因为**识别错了 = 静默失败**：那个包**永远不会被搜**，而日志一切正常。

⇒ 本脚本把「识别」的产物**摊开给人看**：判得出的行给结论，
**判不出的行显式标「拿不准」**，绝不猜。
**判错的代价因此从「某个包永远不被搜」变成「报告里一行不对」** ——
这就是 §19.1 那句「**不要更聪明的识别，要能对账的反馈**」的具体落法。

三种实测形状（2026-09-24 在真 `TV/` 上看到的，见 SUMMARY「大包识别」节）
----------------------------------------------------------------------
    ① `Complete` 包     `[夺爱].Duo.Ai.2011.Complete.../`  里直接挂 `…Ep01…mp4`
    ② 普通单季          `Arcane…S02…-WiKi/`              里直接挂 `…S02E01…mkv`
    ③ 中文标签单季      `[红楼梦].Dream…S01…-OurTV/`      里直接挂 `…S01E01…mkv`
    ⇒ ①②③ 的**发布名层都是包根本身** ⇒ `--depth 1` 够。
    ④ 同名多版本        `Avatar…2024.S01…DV…` / `…S01…HDR…` / `…S02…DV…`
      ⇒ 每个目录**各自是一个发布名** ⇒ 也是 `--depth 1`，
        但**它们是同一部剧的多个版本**，报告里要标出来（见 `--flag-multi-version`）。

**唯一真正需要下钻的**是「一个目录里再套发布名目录」（DC 那种）。本脚本对
这种形状会**算出来**并报告深度，但**不替你决定**。

用法
----
    # 只看异常行（默认；200 个条目干净的话就几行）
    python scripts/scan-tv-packs.py --tv //NAS/video/download/TV

    # 全量（每个条目一行）+ 落盘
    python scripts/scan-tv-packs.py --tv //NAS/video/download/TV --all \
        --out _tmp-20260924/tv-scan.md

    # 只扫几个（先核对规则）
    python scripts/scan-tv-packs.py --tv //NAS/video/download/TV --only 夺爱,Avatar,Arcane

★ **本脚本永不写任何东西**（除 `--out` 指定的报告文件）：不注册、不改 `.env`、
  不碰 `state.db`、不发任何搜索请求。它连网络都不碰。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestrator import state as S  # noqa: E402

#: 默认扫描深度上界。再深的形状会报「超过 N 层，请人工看」——
#: **不是**「扫描不到」，是「这套启发式管不了那么深」，要人接手。
DEFAULT_MAX_PROBE = 4

#: 视频文件后缀（与 `orchestrator.state.VIDEO_EXTENSIONS` 同源，这里只用于判定
#: 「这个目录直接挂着片子吗」）。
#:
#: ★★ 2026-09-24 加 `.iso` / `.img` 等**光盘镜像** —— 实测踩到的洞：
#:   `LOVELY_RUNNER_01-04/` 里躺着 **4 个 `.iso`、一共 156 GB**，而本脚本
#:   第一版报它「**空目录**」；`Les Misérables 2018…` 里是 `LES_MISERABLES_…ISO`
#:   （**大写**）同样被漏。
#: ★ 这是一个**误报**（false positive），而误报的代价在这儿很大：
#:   它把一个"有 156 GB 片子的正常包"标成"异常" ⇒ 人去查那个目录 ⇒ **白白花时间**，
#:   而且**真异常（`儿童`/`欧美剧` 两个分类目录）反而被这些噪音淹掉**。
#:   ★ 注意 `orchestrator.state.VIDEO_EXTENSIONS` 是**照抄 cross-seed 源码**的，
#:     那份清单里**没有** `.iso`（cross-seed 自己也不把 iso 当视频）——
#:     所以这里**不能直接改 `S.VIDEO_EXTENSIONS`**（那会改掉"扫 searchee"的语义、
#:     影响 `scan_pack` 与状态机）。**只在本脚本的"这里有没有片子"这个判据上扩。**
VIDEO_EXT = set(S.VIDEO_EXTENSIONS) | {
    ".iso", ".img", ".m2ts", ".ts", ".vob",  # 光盘镜像 / 原盘主视频流
}

#: 原盘结构目录名。★ 它们**不在** `IGNORED_DIRS` 的意义上"该被忽略" ——
#: 恰恰相反：**看到它们说明这里有片子**。第一版把它们（连同 `bdmv`/`certificate`）
#: 一律当"忽略"，于是「里面全是原盘」看起来和「空的」一模一样 ⇒ **又一个误报**
#: （`Robot Chicken S05…` / `Shorts from Golestan Studio` 都是这样）。
#: ⇒ 保留 `S.IGNORED_FOLDER_SUBSTRINGS` 作为"不下钻进这些目录"的规则（那是对的，
#:   原盘里几万个碎文件不该被当成 searchee），**但"这里有没有内容"要单独判**。
_DISC_STRUCT_DIRS = ("bdmv", "video_ts", "certificate", "bdrom")

# ---- 「发布名」的形状判据 -------------------------------------------------- #
#: 季集号：`S01` / `S01E05` / `s1` / `第01季` / `Ep01` / `EP.05`。
#: ★ 这是**唯一**可靠的强信号 —— 有它说明这一层命名已经是"可搜索的发布名"。
_RE_SEASON_EP = re.compile(
    r"(?i)(?:^|[^A-Za-z0-9])(?:s\d{1,2}(?:e\d{1,3}|-s\d{1,2})?|e\d{1,3}|ep\.?\d{1,3}"
    r"|第\s*\d{1,3}\s*季|season\s*\d{1,2})(?:[^A-Za-z0-9]|$)")

#: 「像发布名」的弱信号（发布组 / 画质 / 来源）。**单靠它不足以定论**，
#: 但与季集号一起出现时是很强的旁证。
_RE_RELEASE_HINT = re.compile(
    r"(?i)(1080p|2160p|720p|4K|WEB-DL|WEBRip|BluRay|Blu-ray|REMUX|HEVC|H\.?264|H\.?265"
    r"|x264|x265|AVC|AAC|FLAC|DDP|Atmos|DV|HDR|10bit)")

#: 发布组尾巴：`-WiKi` / `-FRDS` / `-QHstudIo`（★ 大写字母混排，且是最后一个 `-` 段）。
_RE_GROUP_TAIL = re.compile(r"-[A-Za-z0-9]{2,}$")

#: 中文方括号标签前缀：`[夺爱]` / `[红楼梦]` —— 是**标签**不是发布名。
#: ★ 它是 `§10.2` 那个坑的同族：**这层目录名不能直接拿去搜**。
_RE_CN_TAG = re.compile(r"^\[[^\]]+\]\s*")

#: 忽略的目录名（与 `orchestrator.state.IGNORED_FOLDER_SUBSTRINGS` 同源）
IGNORED_DIRS = set(S.IGNORED_FOLDER_SUBSTRINGS) | {"@eaDir", ".DS_Store"}


def _say(*a) -> None:
    print(*a, flush=True)


def _safe_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# 「发布名」判定 —— 本脚本的心脏
# --------------------------------------------------------------------------- #
def looks_like_release(name: str) -> tuple[bool, list[str]]:
    """这个名字**像不像一个可以直接拿去搜的发布名**？

    返回 `(是否像, 命中的证据列表)`。★ 证据**必须**能打印出来 ——
    报告里每一行的结论都要能追到「凭什么这么判」（`A.12.1`：别只给读数不给来源）。

    ★★ 判据的分层（这是本函数最要紧的设计）：
      * **季集号**（`S02E01` / `S01` / `Ep20` / `第01季`）= **强信号**，单独就够；
      * 「画质/来源/组名」= **弱信号**，**单独不够** ——
        因为它们也可能出现在**分类层**（如 `DC系列剧集`、某个 `Movies` 目录）。
      * 两条弱信号**同时**命中（如 `1080p` + `-WiKi`）也算，因为那种组合
        几乎只出现在发布名上。

    ★ 刻意**不**做的事：不去判「这是不是**一部剧**」（那需要外部知识，
      正是 `§19.1` 说做不到的东西）。这里只判「这一层的**名字**能不能拿去搜」。
    """
    if not name:
        return False, []
    ev: list[str] = []
    if _RE_SEASON_EP.search(name):
        ev.append("季集号")
    if _RE_RELEASE_HINT.search(name):
        ev.append("画质/来源")
    if _RE_GROUP_TAIL.search(name):
        ev.append("发布组")
    strong = "季集号" in ev
    weak = sum(1 for e in ev if e in ("画质/来源", "发布组"))
    return (strong or weak >= 2), ev


def has_direct_video(path: Path) -> bool:
    """这个目录里**直接挂着**视频文件吗？（不递归）

    ★ `VIDEO_EXT` 已含 `.iso` 等光盘镜像（见那里的注释）。
    ★ 后缀比较**不分大小写** —— `.ISO` 与 `.iso` 都要认（实测 `LES_MISERABLES_…ISO`）。
    """
    try:
        with os.scandir(path) as it:
            for e in it:
                if e.is_file() and os.path.splitext(e.name)[1].lower() in VIDEO_EXT:
                    return True
    except OSError:
        return False
    return False


def has_disc_structure(path: Path) -> bool:
    """这个目录里是**原盘结构**吗（`BDMV/` / `VIDEO_TS/` / `CERTIFICATE/`）？

    ★★ 为什么必须单独判（2026-09-24 实测的第二个误报）：
      `IGNORED_DIRS` 里有 `bdmv`/`certificate` —— 那是**对的**（原盘里几万个碎文件
      不该被当成 searchee，`cross-seed` 自己也忽略它们）。但第一版的
      `subdirs()` **只返回"没被忽略的"子目录**，于是「一个只有 `BDMV/` 的目录」
      返回空列表 ⇒ 走到 `if not kids:` ⇒ 报「**空目录**」。
      实测：`Robot Chicken S05…` 与 `Shorts from Golestan Studio` 都是这样 ——
      **里面有原盘、被报成空的**。

    ★ 判据：看**真实**子目录里有没有原盘结构名（这一步**故意绕过** `IGNORED_DIRS`）。
    """
    try:
        with os.scandir(path) as it:
            for e in it:
                if e.is_dir() and e.name.lower() in _DISC_STRUCT_DIRS:
                    return True
    except OSError:
        return False
    return False


def subdirs(path: Path) -> list[str]:
    """下一层目录名（排除 @eaDir / sample 之类）。"""
    out: list[str] = []
    try:
        with os.scandir(path) as it:
            for e in it:
                if not e.is_dir():
                    continue
                if e.name in IGNORED_DIRS or e.name.startswith("."):
                    continue
                if any(s in e.name.lower() for s in IGNORED_DIRS):
                    continue
                out.append(e.name)
    except OSError:
        return []
    return sorted(out)


# --------------------------------------------------------------------------- #
# 单个条目的判定
# --------------------------------------------------------------------------- #
class Verdict:
    """一个 `TV/` 直接子项的体检结论。"""

    __slots__ = ("name", "path", "release_depth", "picked", "flags", "note",
                 "note_extra")

    def __init__(self, name: str, path: Path) -> None:
        self.name = name
        self.path = path
        #: 发布名在第几层（1 = 条目本身就是发布名层；None = 拿不准）
        self.release_depth: int | None = None
        #: 该下钻到哪（= 要喂给 `scan_pack` 的 `--depth`）；None = 拿不准
        self.picked: int | None = None
        self.flags: list[str] = []
        self.note = ""
        #: 跨条目旁证（「同级另有 N 个同剧版本」）—— 由 `_with_extra` 并进 `note`。
        #: ★ 单列一个槽而不是直接拼 `note`：开头算出的旁证会被后面**覆盖式**赋值的
        #:   `note = "…"` 冲掉（第一版就是这么丢的）。
        self.note_extra = ""

    @property
    def ok(self) -> bool:
        return self.picked is not None


def classify(name: str, path: Path, *,
             siblings: list[str] | None = None,
             max_probe: int = DEFAULT_MAX_PROBE) -> Verdict:
    """给 `TV/` 的一个直接子项下结论。

    ★★ 这里是「拿不准就报出来」的落点 —— 三种出口：
      1. 判得出 ⇒ `picked` 给出建议的 `--depth`，`note` 写明**凭什么**；
      2. 判不出 ⇒ `picked = None` + `flags` 里标 `拿不准`，**绝不猜**；
      3. 超过 `max_probe` ⇒ 标 `太深` —— 也归入"判不出"，因为**这套启发式
         管不了那么深**（§19.1：宁可说"不知道"）。

    ★★ `siblings` 必须传「**同级的其它条目名**」（即 `TV/` 下所有直接子项名）。
      为什么：`同名多版本` 是**同级比较**，与"这个目录多深才到发布名"**无关** ——
      第一版把它写在下钻分支里，而 `Avatar…S01…DV` / `…HDR` 这些**直接挂片子、
      走的是"情形 A"早早 return** ⇒ **检测从未执行**（死代码）。
      实测在真 `TV/` 上跑一次才发现：9 个条目、`同名多版本` 一个都没出。
      ⇒ 提出来，放在**所有分支之前**。
    """
    v = Verdict(name, path)
    name_is_release, ev = looks_like_release(name)

    # ---- ★ 跨条目：同级里有没有「同一部剧的另一个版本」 ------------------- #
    #   位置特意放在**最前**：它不依赖本目录的内部形状（见 docstring 的更正说明）。
    if siblings:
        sib = [s for s in siblings if s != name and _norm_show(s) == _norm_show(name)]
        if sib:
            v.flags.append("同名多版本")
            v.note_extra = "同级另有 %d 个同剧版本：%s" % (
                len(sib), "；".join("`%s`" % s for s in sib[:3]))

    # ---- 中文方括号标签：是标签不是发布名，但要**能搜**（去掉标签后仍是发布名）--
    if _RE_CN_TAG.match(name):
        v.flags.append("中文标签层")

    # ---- 情形 A：条目自己直接挂着视频文件（含 `.iso`）--------------------- #
    #   ① `Complete` 包（Ep 文件直挂）② 单季包（SxxExx 文件直挂）③ 中文单季
    #   ④ **光盘镜像**（`.iso` 直挂，如 `LOVELY_RUNNER_01-04`）
    #   ⇒ 都是"发布名层 = 条目本身" ⇒ depth 1
    if has_direct_video(path):
        v.release_depth, v.picked = 1, 1
        if name_is_release:
            v.note = "直接挂片子 ⇒ 发布名层就在这一层（证据：%s）" % "/".join(ev)
        else:
            # ★ 名字不像发布名，可里面就是片子 —— 这**不是错误**（如中文标签包），
            #   但要标出来：cross-seed 拿**这个目录名**去搜，命中率可能低。
            v.flags.append("名字不像发布名")
            v.note = ("直接挂片子 ⇒ 深度 1；★ 但目录名缺发布名特征，"
                      "cross-seed 会拿这个名字去搜（命中率存疑）")
        _merged = _with_extra(v)
        # 中文标签 + 直接挂片子：最常见的正常形状，不算异常
        if "中文标签层" in _merged.flags and name_is_release \
                and "同名多版本" not in _merged.flags:
            _merged.flags.remove("中文标签层")   # 名称里含标签但仍像发布名 ⇒ 正常
        return _merged

    # ---- 情形 A2：原盘结构（`BDMV/` 等）⇒ 也是"片子在这一层" ------------- #
    #   ★ 与情形 A 同样给 depth 1，但**单独一条分支**、且**带标记** ——
    #     因为原盘的"发布名"往往就是那个**光盘/分卷名**（`…D1` / `…_DIY_3201`），
    #     未必像标准发布名。这是**提醒**，不是异常。
    if has_disc_structure(path):
        v.release_depth, v.picked = 1, 1
        v.flags.append("原盘结构")
        v.note = "含 BDMV/VIDEO_TS ⇒ 片子就在这一层（depth 1）；★ 原盘通常按光盘分卷"
        return _with_extra(v)

    # ---- 情形 B：下面全是目录 ⇒ 可能要下钻 ------------------------------- #
    kids = subdirs(path)
    if not kids:
        v.flags.append("空目录")
        v.note = "里面没有子目录也没有片子 —— 值得看一眼"
        return _with_extra(v)

    # 子目录里"像发布名"的比例
    like = [k for k in kids if looks_like_release(k)[0]]
    frac = len(like) / len(kids)

    if frac >= 0.8 and len(kids) <= 40:
        # ⇒ 这些子目录就是发布名层 ⇒ depth 2。★ 典型：DC 那种「分类层 → 发布名」。
        v.release_depth, v.picked = 2, 2
        v.note = ("子目录 %d 个中 %d 个像发布名 ⇒ 发布名在第 2 层（depth 2）"
                  % (len(kids), len(like)))
        return _with_extra(v)

    if len(kids) == 1:
        # 单链下钻：`包根/唯一子目录/…` —— 再探一层。
        if max_probe <= 1:
            v.flags.append("太深")
            v.note = "单链下钻超过 %d 层，拿不准" % DEFAULT_MAX_PROBE
            return _with_extra(v)
        inner = classify(kids[0], path / kids[0], max_probe=max_probe - 1)
        if inner.picked is not None and inner.release_depth is not None:
            v.release_depth, v.picked = inner.release_depth + 1, inner.picked + 1
            v.note = "单链下钻一层后：" + inner.note
            v.flags += [f for f in inner.flags if f not in v.flags]
            return _with_extra(v)
        v.flags.append("拿不准")
        v.note = "单链下钻后仍判不出：" + inner.note
        return _with_extra(v)

    # ---- 情形 C：混合 / 分类层 ⇒ **拿不准**（§19.1：宁可说不知道）-------- #
    v.flags.append("拿不准")
    v.note = ("子目录 %d 个、只有 %d 个像发布名（%.0f%%），且不是一个 —— "
              "这层可能是分类层，也可能是混合；**请人工看**"
              % (len(kids), len(like), frac * 100))
    return _with_extra(v)


def _with_extra(v: Verdict) -> Verdict:
    """把开头的同级结论（`note_extra`）并进 `note` —— 它是**旁证**，不该丢掉。"""
    extra = getattr(v, "note_extra", "")
    if extra:
        v.note = (v.note + " ｜ " + extra) if v.note else extra
        v.note_extra = ""
    return v


def _norm_show(s: str) -> str:
    """把发布名压成「同一部剧」的指纹：去季号、去画质/组，只留剧名+年份。"""
    s = _RE_CN_TAG.sub("", s)
    s = re.sub(r"(?i)\bS\d{1,2}(E\d{1,3})?\b", "", s)
    s = re.sub(r"(?i)\b(1080p|2160p|720p|4K|WEB-DL|WEBRip|BluRay|Blu-ray|REMUX"
               r"|HEVC|H\.?264|H\.?265|x264|x265|AVC|AAC|FLAC|DDP\d?\.?\d?|Atmos"
               r"|DV|HDR|10bit|\d+\.\d)\b", "", s)
    s = re.sub(r"-\s*[A-Za-z0-9]+$", "", s)
    s = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", ".", s).strip(".").lower()
    return s


def _multi_version_siblings(kids: list[str]) -> bool:
    """子目录是不是「同一部剧的多个版本」（如 DV / HDR 两版并列）？

    ★ 判据：压掉季号/画质之后，**至少两个**子目录指纹相同而原名不同。
      这正是 `Avatar.The.Last.Airbender.2024.S01…DV…` 与 `…S01…HDR…` 的形状 ——
      它们是**两个独立的种子**，报告里要提醒（别当成"重复"删掉一个）。
    """
    seen: dict[str, set[str]] = {}
    for k in kids:
        seen.setdefault(_norm_show(k), set()).add(k)
    return any(len(v) >= 2 for v in seen.values())


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
#: ★ 哪些标记算「**真需要人看**」。
#:
#: ★★ `同名多版本` **刻意不在这里** —— 它**不是缺陷**，是**提示**：
#:   `TV/` 200 个条目里实测有 **69** 个落在这类（同一部剧的 DV/HDR 版、
#:   或不同季并列）。若把它算进"异常"，7 个真异常（空目录）会被 69 行噪音淹没 ——
#:   **满屏的"需要你看" = 没人看**（`CLAUDE.md` 说 `06_sources` 不做自动判是同理：
#:   「永远红的闸门 = 没人看的闸门」）。
#:   ⇒ 它单列一节（"同剧多版本（提示，非异常）"），异常表里不出现。
#:
#: ★★ `原盘结构` 也**不在这里**（2026-09-24 加）—— 它是**正常的**（片子是原盘而已），
#:   与 `同名多版本` 一样归"提示"。★ 加它之前，`Robot Chicken S05…` 这类
#:   **被误报成"空目录"**（见 `has_disc_structure` 的注释）。
_NEEDS_HUMAN_FLAGS = ("拿不准", "太深", "空目录", "名字不像发布名")

#: ★ 提示类标记（**不是**异常，报告里单列）。
_HINT_FLAGS = ("同名多版本", "原盘结构")


def _needs_human(v: Verdict) -> bool:
    """这个条目**真的**要人看吗？（提示类标记不算 —— 见 `_NEEDS_HUMAN_FLAGS`）"""
    return (not v.ok) or any(f in _NEEDS_HUMAN_FLAGS for f in v.flags)


def _is_hint(v: Verdict) -> bool:
    """只是**提示**（不是异常）吗？—— `同名多版本` / `原盘结构` 两种。"""
    return (not _needs_human(v)) and any(f in _HINT_FLAGS for f in v.flags)


def _render(verdicts: list[Verdict], tv: str, *, show_all: bool) -> str:
    bad = [v for v in verdicts if _needs_human(v)]
    hint = [v for v in verdicts if _is_hint(v)]
    good = [v for v in verdicts if not _needs_human(v) and v not in hint]

    L: list[str] = []
    L.append("# TV 大包识别扫描报告（**只读** —— 未注册、未改任何配置）")
    L.append("")
    L.append("- 扫描根：`%s`" % tv)
    L.append("- 生成于：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    L.append("- 条目总数：**%d**（判得出 **%d** · 需要你看 **%d** · "
             "同剧多版本（提示）**%d**）"
             % (len(verdicts), len(good) + len(hint), len(bad), len(hint)))
    L.append("- ★ 本报告只是**建议**。注册要人点头（`summary/19` §19.1："
             "**包是声明的，不是识别的**）。")
    L.append("")

    if bad:
        L.append("## ★ 需要你看的行（%d）" % len(bad))
        L.append("")
        L.append("| 条目 | 建议 depth | 标记 | 依据 |")
        L.append("|---|---|---|---|")
        for v in bad:
            d = str(v.picked) if v.picked is not None else "**拿不准**"
            L.append("| `%s` | %s | %s | %s |"
                     % (_esc(v.name), d, " · ".join(v.flags) or "—", _esc(v.note)))
        L.append("")

    if hint:
        L.append("## 同剧多版本（提示，**非异常** —— 别当成重复删掉）（%d）" % len(hint))
        L.append("")
        L.append("这些是**同一部剧的多个版本**（DV / HDR / 不同季 / 不同组），"
                 "每个**各自是一个独立发布名**、都该登记。列在这里只是提醒"
                 "「它们是一家人」。")
        L.append("")
        L.append("| 条目 | depth | 同剧的其它版本 |")
        L.append("|---|---|---|")
        for v in hint:
            L.append("| `%s` | %s | %s |"
                     % (_esc(v.name), v.picked if v.picked is not None else "?",
                        _esc(getattr(v, "note_extra", "") or
                             (v.note.split("｜", 1)[1].strip() if "｜" in v.note else "—"))))
        L.append("")

    if show_all:
        L.append("## 全部条目（%d）" % len(verdicts))
        L.append("")
        L.append("| 条目 | depth | 标记 |")
        L.append("|---|---|---|")
        for v in verdicts:
            L.append("| `%s` | %s | %s |"
                     % (_esc(v.name), v.picked if v.picked is not None else "?",
                        " · ".join(v.flags) or "—"))
        L.append("")
    elif good:
        L.append("## 判定干净、无提示的条目：%d 个（加 `--all` 看全部）" % len(good))
        L.append("")

    L.append("---")
    L.append("")
    L.append("### 怎么用这份报告")
    L.append("")
    L.append("报告说 `depth = N` ⇒ 那个条目可以这样登记（**示例，别照抄路径**）：")
    L.append("")
    L.append("```")
    L.append("python scripts/reseed-state.py init --pack <你起的名字> \\")
    L.append("    --root /volume1/video/download/TV/<条目名> \\")
    L.append("    --local-root //NAS/video/download/TV/<条目名> \\")
    L.append("    --depth N")
    L.append("```")
    L.append("")
    L.append("★ `--depth N` 是 `scan_pack` 的**下钻层数**，含义见"
             " `orchestrator/state.py` 的 `find_nested_roots()`。")
    L.append("★ 标 **拿不准** 的行**不要**硬填一个数 —— 先人工看那个目录，")
    L.append("  然后**把结论补进本文档**（这正是 §19.1 要的「能对账的反馈」）。")
    return "\n".join(L) + "\n"


#: ★ 「名字存疑」这个分段的判据 —— **复用扫描器已经算出来的那个标记**，
#:   不另立一套（`A.11`：同一事实只在一处）。含义：**有片子、depth 也对，
#:   但目录名拿去站点搜多半搜不到**（`LOVELY_RUNNER_01-04` 那种范围号、
#:   `蓝色星球2` 那种纯中文）⇒ 它们会长期躺在 `UNMATCHED`。
#:   ⇒ `--emit-roots` 把它单独成段，让人**可以只贴稳的那段**。
_DUBIOUS_FLAG = "名字不像发布名"


def _emit_roots(verdicts: list[Verdict], *, dups: list[str]) -> str:
    """吐**纯 root 行**（一行一条），给 `.env` 的 `FARM_SOURCES=` 用。

    ★★ 为什么只吐 root 行、不吐 `init --root …` 现成命令（用户 2026-10-03 拍）：
      ① 那命令会有 200 个 `--root` ⇒ 一行几千字符，粘终端**必被截断或粘错**
         （`summary/26` 记过一次同类事故）；
      ② `reseed-state.py init` **本来就有 `--roots-from-env`** —— 就是为"别手打
         200 个 `--root`"设计的。吐命令等于把一条已有的路**重造一遍**。

    ★ 分两段（用户拍）：**稳的** / **名字存疑**（判据 = `_DUBIOUS_FLAG`）。
      作用：**只贴稳的那段**是个合法选择 —— 存疑的 7 条可以以后再人工看。
      分成两段而不混在一起，是为了让人**能只贴一部分**（混在 200 行里就没法挑）。

    ★★ **判不出 `picked=None` 的条目一律不吐** —— 它们**没有 depth，进不了
      `FARM_SOURCES`**（`scan_pack` 不知道喂它什么深度）。它们会出现在段尾的
      「不进清单」小节里，**绝不静默丢**（`ERR-AI-03`：不给 ≠ 没有）。

    ★ **不去重**：实测真 `TV/` 200 个条目**0 重复**（2026-10-03 量过）。
      真出现重复时，**透出 `scan_pack()` 自己的 `dups`**（`dups` 参数），
      **不在这里另写一套判重** —— 判据只有一份。
    """
    stable = [v for v in verdicts if v.picked is not None and _DUBIOUS_FLAG not in v.flags]
    dubious = [v for v in verdicts if v.picked is not None and _DUBIOUS_FLAG in v.flags]
    unknown = [v for v in verdicts if v.picked is None]

    L: list[str] = []
    L.append("# FARM_SOURCES 候选（由 scan-tv-packs --emit-roots 生成，**只读**）")
    L.append("# ★ 用法：整段粘进 .env 的 FARM_SOURCES=（逗号分隔；一行一条也行）。")
    L.append("# ★ 这是**候选**：贴之前请自己读一眼 —— 包是「声明」的，不是「识别」的（§19.1）。")
    L.append("# ★ 量：稳的 %d 条 + 名字存疑 %d 条 = 可喂 %d 条；另有 %d 条判不出，未列。"
             % (len(stable), len(dubious), len(stable) + len(dubious), len(unknown)))
    L.append("")
    L.append("# ---- 第一段：稳的（%d 条）----" % len(stable))
    for v in stable:
        L.append(str(v.path))
    L.append("")
    L.append("# ---- 第二段：名字存疑（%d 条，也 depth=%d，但搜索名可能不好用；"
             "贴不贴你自己定）----" % (len(dubious), 1))
    for v in dubious:
        L.append(str(v.path))

    if unknown:
        L.append("")
        L.append("# ---- 不进清单：判不出 depth 的（%d 条）—— 需人工看 ----" % len(unknown))
        for v in unknown:
            L.append("#   %s   [%s]" % (v.path, "、".join(v.flags) or "拿不准"))

    if dups:
        L.append("")
        L.append("# ⚠ scan_pack 报重名 %d 个（两个根下同名，只会保留先出现的那个）：" % len(dups))
        for d in sorted(set(dups)):
            L.append("#   %s" % d)
    return "\n".join(L) + "\n"


def _esc(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ")


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="只读扫描 TV/ 树，报告每个条目该从第几层找发布名（不注册、不发搜索）")
    ap.add_argument("--tv", required=True,
                    help="TV 根目录（NAS 路径或本机/UNC 路径，只要能列目录）")
    ap.add_argument("--out", default="",
                    help="把报告写到这个文件（默认只打 stdout）。★ 建议放 _tmp-*/ 下")
    ap.add_argument("--all", action="store_true",
                    help="连判定干净的条目也逐行列出（默认只列有问题的）")
    ap.add_argument("--only", default="",
                    help="只扫名字包含这些子串的条目，逗号分隔（先核对规则用）")
    ap.add_argument("--max-probe", type=int, default=DEFAULT_MAX_PROBE,
                    help="下钻探测的最大层数（默认 %d），超过就报「拿不准」"
                         % DEFAULT_MAX_PROBE)
    ap.add_argument("--emit-roots", action="store_true",
                    help="★ 不吐报告，改吐**纯 root 行**（分「稳的」/「名字存疑」两段），"
                         "给 .env 的 FARM_SOURCES= 用。判不出 depth 的条目**不进清单**。")
    args = ap.parse_args(argv)

    _safe_stdout()
    tv = args.tv
    if not os.path.isdir(tv):
        _say("[!!] 列不了目录：%s" % tv)
        _say("     ★ 这不是「没有包」，是「读数拿不到」（ERR-AI-03）。")
        return 2

    only = [s.strip() for s in args.only.split(",") if s.strip()]
    try:
        with os.scandir(tv) as it:
            names = sorted(e.name for e in it if e.is_dir()
                           and e.name not in IGNORED_DIRS and not e.name.startswith("."))
    except OSError as e:
        _say("[!!] 列目录失败：%s" % e)
        return 2

    if only:
        names = [n for n in names if any(o in n for o in only)]

    # ★★ 同级列表**必须**一起传进 `classify` —— 「同名多版本」要靠它
    #   （与目录内部形状无关，见 `classify` 的 docstring）。
    verdicts = [classify(n, Path(tv) / n, siblings=names, max_probe=args.max_probe)
                for n in names]

    # ★★ `--emit-roots` 是个**独立出口** —— 它和报告走的是**同一个** `verdicts`，
    #   所以两边的结论**必然一致**（不会出现"报告说 191、清单里 189"）。
    #   ★ 不去重：本脚本用 `os.scandir` 直扫一层，**没有** `scan_pack()` 的
    #     `dups` 概念（那是 cross-seed searchee 枚举的重名，本脚本不做那一步）。
    #     ⇒ 传空 `dups`。真出现重复（同一个 `TV/` 下不可能有同名目录，OS 不允许），
    #     该做的是让 `scan_pack` 那侧报，**不在这里另写判重**。
    if args.emit_roots:
        out = _emit_roots(verdicts, dups=[])
        if args.out:
            outp = Path(args.out)
            outp.parent.mkdir(parents=True, exist_ok=True)
            outp.write_text(out, encoding="utf-8")
            _say("清单已写入：%s" % outp)
        else:
            # ★ 用 write 不用 print：`out` 自带结尾换行，print 会多空一行。
            sys.stdout.write(out)
            sys.stdout.flush()
        n_stable = sum(1 for v in verdicts
                       if v.picked is not None and _DUBIOUS_FLAG not in v.flags)
        n_dub = sum(1 for v in verdicts
                    if v.picked is not None and _DUBIOUS_FLAG in v.flags)
        n_unk = sum(1 for v in verdicts if v.picked is None)
        _say("")
        _say("清单：稳的 %d + 名字存疑 %d = %d 条可喂；判不出 %d 条未列。"
             % (n_stable, n_dub, n_stable + n_dub, n_unk))
        return 0

    report = _render(verdicts, tv, show_all=args.all)

    if args.out:
        outp = Path(args.out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(report, encoding="utf-8")
        _say("报告已写入：%s" % outp)

    # 摘要始终打 stdout（即使写了文件 —— 让人一眼看到有没有"拿不准"）
    # ★ 判据复用 `_needs_human()`，**不在这里重写一份** ——
    #   两处各写一份就是"同一事实散在多处"（`03_terms` 那条闸门要防的正是它）。
    n_bad = sum(1 for v in verdicts if _needs_human(v))
    n_hint = sum(1 for v in verdicts if _is_hint(v))
    _say("扫了 %d 个条目：判得出 %d，需要你看 %d（另有 %d 个同剧多版本提示）"
         % (len(verdicts), len(verdicts) - n_bad, n_bad, n_hint))
    if not args.out:
        _say("")
        _say(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
