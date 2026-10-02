# -*- coding: utf-8 -*-
"""`scan-tv-packs` 的分类判据 —— 钉住「该下钻到哪」+「拿不准要说出来」。

## 为什么这个文件必须存在（`ERR-AI-09`）

本脚本的产物是**建议**，而它的**失败方式**正是本项目最忌的那种：
**判浅了 ⇒ cross-seed 拿"分类目录名"去搜 ⇒ 搜不到却烧额度**（`§10.2` 那句）；
**判深了 ⇒ 一个包被拆成互不相干的碎片**；**判不出来却硬猜 ⇒ 静默漏**。

⇒ 三条判据各配一个夹具，**并逐条种变异证明会红**（写在每个 `== N ==` 段的
  结尾注释里）。★ 变异测试的意义在这里尤其大 —— 第一版就有一个**死代码**：
  `同名多版本` 检测被写在下钻分支里，而 `Avatar` 那些条目**直接挂片子、
  走"情形 A"早早 return** ⇒ **检测从未执行**，在真 `TV/` 上跑一遍才发现
  （9 个条目里该出 6 个，出了 0 个）。**只跑夹具会漏掉这个**，所以本文件的
  夹具**刻意照真数据的形状搭**（`siblings` 一起传）。

## 本文件钉什么

1. **`Complete` 包 / 单季包 → depth 1** —— 片**直接挂**在包根下（实测三种形状）。
   ⇒ ★ 变异：把 `has_direct_video` 判据改成"必须有子目录" ⇒ 必红。
2. **`分类层 → 发布名` → depth 2** —— 子目录里多数像发布名。
   ⇒ ★ 变异：把 `frac >= 0.8` 改成 `frac >= 0.0` ⇒ `拿不准` 那条必红。
3. **★★★ 混着分类目录 + 包 ⇒ 拿不准，绝不猜** —— 这是 `§19.1` 的落点。
   真 `TV/` 下实测混着 `儿童` / `欧美剧` 两个**分类目录**（且是空的）。
   ⇒ ★ 变异：把"拿不准"分支改成"返回 depth 1" ⇒ 必红。
4. **★ `同名多版本` 必须在「直接挂片子」那条路上也生效**（第一版的死代码）。
   ⇒ ★ 变异：把同级检测挪回下钻分支 ⇒ 必红（**这正是当初的真缺陷**）。
5. **★ 「同名多版本」不算"需要人看"** —— 它占真数据 69/200，
   算进异常会把 7 个**真**异常淹没（「永远红的闸门 = 没人看的闸门」）。
   ⇒ ★ 变异：把 `_NEEDS_HUMAN_FLAGS` 里加回 `同名多版本` ⇒ 必红。
6. **★ 空目录要被报出来**（真数据 7 个；其中一个名字说该有片子）。
   ⇒ ★ 变异：`subdirs` 空时直接返回 depth 1 ⇒ 必红。
7. **`中文标签层` 在"名字仍像发布名"时**不该算异常（`[红楼梦]…S01…` 是正常形状）。
   ⇒ ★ 变异：无条件把它留在 `flags` ⇒ 必红。

★ 行形用既有的 `  ok  ` / ` FAIL `（`tests/README.md` 那个重数器只认三种行形）。
★ 全部离线：夹具是 `tempfile` 里现造的空目录树，**不碰 NAS、不需要真片子**
  （判据只看"有没有视频后缀的文件"，空文件即可）。
"""
import importlib.util
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

# ★ 文件名带连字符 ⇒ 只能走 spec_from_file_location（照抄 `test_notify_quota.py`）。
_spec = importlib.util.spec_from_file_location(
    "scan_tv_packs", str(REPO / "scripts" / "scan-tv-packs.py"))
stp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stp)

_ok = 0
_n = 0


def check(name, cond, detail=""):
    global _ok, _n
    _n += 1
    _ok += 1 if cond else 0
    print(("  ok  " if cond else " FAIL ") + "%s: %r" % (name, bool(cond)))
    if not cond and detail:
        print("        ← " + detail)


def mk(root: pathlib.Path, *parts, files=(), dirs=()):
    """在 `root` 下造一个目录树：`parts` 是路径段，`files`/`dirs` 是它下面的内容。

    ★ 文件用**空文件 + 真后缀**即可 —— 判据(`has_direct_video`)只看后缀。
    """
    d = root.joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)
    for f in files:
        (d / f).write_text("", encoding="utf-8")
    for s in dirs:
        (d / s).mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------- #
print("== ① `Complete` 包 / 单季包：片子直接挂在包根 ⇒ depth 1 ==")
# 实测形状（2026-09-24，真 TV/）：
#   `[夺爱]…Complete…/` 里直挂 `…Ep01…mp4`；`Arcane…S02…/` 里直挂 `…S02E01…mkv`
_t = pathlib.Path(tempfile.mkdtemp())
mk(_t, "[夺爱].Duo.Ai.2011.Complete.WEB-DL.4K.HEVC.AAC-CMCTV",
   files=["[夺爱].Duo.Ai.2011.Ep01.WEB-DL.4K.HEVC.AAC-CMCTV.mp4",
          "[夺爱].Duo.Ai.2011.Ep02.WEB-DL.4K.HEVC.AAC-CMCTV.mp4"])
mk(_t, "Arcane.League.of.Legends.S02.2160p.BluRay.x265.10bit-WiKi",
   files=["Arcane.League.of.Legends.S02E01.2160p.BluRay-WiKi.mkv"])

v1 = stp.classify("[夺爱].Duo.Ai.2011.Complete.WEB-DL.4K.HEVC.AAC-CMCTV",
                  _t / "[夺爱].Duo.Ai.2011.Complete.WEB-DL.4K.HEVC.AAC-CMCTV")
check("★ Complete 包 ⇒ depth 1", v1.ok and v1.picked == 1,
      "got picked=%r flags=%r" % (v1.picked, v1.flags))
check("★ Complete 包：不算异常", not stp._needs_human(v1),
      "flags=%r —— 这是最常见的正常形状" % (v1.flags,))

v2 = stp.classify("Arcane.League.of.Legends.S02.2160p.BluRay.x265.10bit-WiKi",
                  _t / "Arcane.League.of.Legends.S02.2160p.BluRay.x265.10bit-WiKi")
check("★ 普通单季 ⇒ depth 1", v2.ok and v2.picked == 1,
      "got picked=%r" % (v2.picked,))
# ★★ 变异：把 `classify` 里 `has_direct_video(path)` 那个判据改成
#    `if kids:`（即"必须有子目录才算发布名层"）⇒ 上面两条必红
#    （picked 会变成 None/拿不准 —— 而这是真数据里的主流形状，全 200 个都得错）。

# --------------------------------------------------------------------------- #
print("\n== ② `分类层 → 发布名`（DC 那种）⇒ depth 2 ==")
_t2 = pathlib.Path(tempfile.mkdtemp())
mk(_t2, "DC相关剧集全系列大合集", dirs=["Arrow.S01-S08.1080p-BluRay",
                                        "The.Flash.S01.1080p-BluRay",
                                        "Superman.2013.1080p-BluRay"])
v3 = stp.classify("DC相关剧集全系列大合集", _t2 / "DC相关剧集全系列大合集")
check("★ 子目录多数像发布名 ⇒ depth 2", v3.ok and v3.picked == 2,
      "got picked=%r note=%r" % (v3.picked, v3.note))
# ★★ 变异：把 `frac >= 0.8` 改成 `frac >= 0.0` ⇒ 下面 ③ 那条必红
#    （3 个子目录、0 个像发布名，也会被当成"多数像发布名"）。

# --------------------------------------------------------------------------- #
print("\n== ③ ★★★ 分类目录混在其中 ⇒ **拿不准**，绝不猜（§19.1 的落点）==")
# 真 TV/ 下实测：`儿童` / `欧美剧` 两个**分类目录**（且是空的）与 200 个包并列。
# ⇒ 谁照报告把 TV/ 全注册，它们会被当成"一个包"，而名字是"欧美剧"——
#   搜不到任何东西却照样烧额度（§10.2 那句话）。
_t3 = pathlib.Path(tempfile.mkdtemp())
mk(_t3, "欧美剧", dirs=["Some.Unknown.Thing", "Another.Unknown.Thing",
                        "Third.Unknown"])
v4 = stp.classify("欧美剧", _t3 / "欧美剧")
check("★★★ 全都不像发布名 ⇒ picked=None（不猜）", v4.picked is None,
      "got picked=%r —— 猜一个数就是 §19.1 说的『静默失败』" % (v4.picked,))
check("★★★ 标记里必须有 `拿不准`", "拿不准" in v4.flags,
      "flags=%r" % (v4.flags,))
check("★★★ 拿不准 ⇒ 算「需要人看」", stp._needs_human(v4))
# ★★ 变异：把 情形 C 的 `v.flags.append("拿不准"); return _with_extra(v)`
#    改成 `v.picked = 1; return _with_extra(v)` ⇒ 上面两条必红。
# ★ 同时这条也是 ② 的**反面哨兵**：② 说"多数像 ⇒ 下钻"，
#   ③ 说"一个都不像 ⇒ 不猜"，两条一起才说明 `frac` 阈值**真的在起作用**。

# --------------------------------------------------------------------------- #
print("\n== ④ ★★ `同名多版本` 在「直接挂片子」那条路上也必须生效 ==")
# ★ 这是**第一版的真缺陷**（死代码）：检测写在下钻分支里，而 Avatar 那些
#   条目直接挂片子、走情形 A 早早 return ⇒ 检测从未执行。
#   实测：真 TV/ + `--only Avatar` ⇒ 9 个条目该出 6 个 `同名多版本`，出了 **0** 个。
_t4 = pathlib.Path(tempfile.mkdtemp())
AV = ["Avatar.The.Last.Airbender.2024.S01.2160p.NF.WEB-DL.DV.H.265-DepWeb",
      "Avatar.The.Last.Airbender.2024.S01.2160p.NF.WEB-DL.H.265-DepWeb",
      "Avatar.The.Last.Airbender.2024.S02.2160p.NF.WEB-DL.DV.H.265-DepWeb"]
for a in AV:
    mk(_t4, a, files=[a + ".S01E01.mkv"])
_sib = AV   # ★★ 同级列表**必须**一起传 —— 这正是第一版漏掉的那一步
v5 = stp.classify(AV[0], _t4 / AV[0], siblings=_sib)
check("★★ 直接挂片子 + 有同剧兄弟 ⇒ 标 `同名多版本`",
      "同名多版本" in v5.flags,
      "flags=%r —— 第一版这里恒为空（死代码）；变异：把检测挪回下钻分支 ⇒ 必红"
      % (v5.flags,))
check("★★ 深度仍是 1（多版本与深度无关）", v5.picked == 1,
      "got %r —— 多版本是**同级比较**，不该影响下钻层数" % (v5.picked,))
check("★★ 旁证里说得出是哪些兄弟", "同级另有" in v5.note and "Avatar" in v5.note,
      "note=%r" % (v5.note,))

# 反向：**没有**兄弟时不许瞎标
v6 = stp.classify(
    "Arcane.League.of.Legends.S02.2160p.BluRay.x265.10bit-WiKi",
    _t / "Arcane.League.of.Legends.S02.2160p.BluRay.x265.10bit-WiKi",
    siblings=["另外一个完全不相干的包.S01.1080p-WiKi"])
check("★ 没有同剧兄弟 ⇒ 不标（别瞎标）", "同名多版本" not in v6.flags,
      "flags=%r" % (v6.flags,))

# --------------------------------------------------------------------------- #
print("\n== ⑤ ★ `同名多版本` 不算「需要人看」（否则真异常被淹没）==")
# 真数据：200 个条目里 69 个是这类，而**真异常只有 11 个**。
# 若把 69 算成异常 ⇒ 一眼看去"80 个要处理"，那 7 个空目录就找不着了。
hello = stp.classify(AV[0], _t4 / AV[0], siblings=_sib)
check("★★ 多版本条目**不**算 `_needs_human`", not stp._needs_human(hello),
      "flags=%r —— 它是**提示**不是缺陷" % (hello.flags,))
check("★★ `同名多版本` 不在 `_NEEDS_HUMAN_FLAGS` 里",
      "同名多版本" not in stp._NEEDS_HUMAN_FLAGS,
      "got %r —— 变异：加回去 ⇒ 上面那条必红；真数据下报告会从 11 行噪音涨到 80 行"
      % (stp._NEEDS_HUMAN_FLAGS,))

# --------------------------------------------------------------------------- #
print("\n== ⑥ ★ 空目录必须报出来（真数据 7 个）==")
_t5 = pathlib.Path(tempfile.mkdtemp())
mk(_t5, "LOVELY_RUNNER_01-04")            # 真数据里那个空目录
v7 = stp.classify("LOVELY_RUNNER_01-04", _t5 / "LOVELY_RUNNER_01-04")
check("★ 空目录 ⇒ 不猜（picked=None）", v7.picked is None, "got %r" % (v7.picked,))
check("★ 空目录标记 + 算需要人看",
      "空目录" in v7.flags and stp._needs_human(v7),
      "flags=%r" % (v7.flags,))
# ★★ 变异：`if not kids:` 分支改成 `v.picked = 1; return _with_extra(v)`
#    ⇒ 上面两条必红（空目录会被当成"depth 1 的包"静默注册进去）。

# --------------------------------------------------------------------------- #
print("\n== ⑦ ★ `中文标签层` 在名字仍像发布名时不算异常 ==")
_t6 = pathlib.Path(tempfile.mkdtemp())
mk(_t6, "[红楼梦].Dream.of.the.Red.Chamber.S01.1987.2160p.WEB-DL.H265-OurTV",
   files=["[红楼梦].Dream.of.the.Red.Chamber.S01E01.1987.2160p.WEB-DL.H265-OurTV.mkv"])
v8 = stp.classify("[红楼梦].Dream.of.the.Red.Chamber.S01.1987.2160p.WEB-DL.H265-OurTV",
                  _t6 / "[红楼梦].Dream.of.the.Red.Chamber.S01.1987.2160p.WEB-DL.H265-OurTV")
check("★ 中文标签 + 直挂片子 ⇒ depth 1", v8.picked == 1, "got %r" % (v8.picked,))
check("★★ 名字仍像发布名 ⇒ **不**报异常（这是正常形状）",
      not stp._needs_human(v8),
      "flags=%r —— 真 TV/ 里十来个这种，全报的话又是噪音" % (v8.flags,))
# ★★ 变异：把"移除 `中文标签层`"那三行删掉 ⇒ 上面那条必红。

# ---- 附：`looks_like_release` 的分层判据（强弱信号）------------------------ #
print("\n== ⑧ ★ `looks_like_release`：季集号是强信号，画质/组名单独不够 ==")
check("★ 有季集号 ⇒ 像（强信号单独就够）",
      stp.looks_like_release("Show.S02E01.1080p-GRP")[0])
check("★ 只有画质+组名 ⇒ 也算（两条弱信号同时命中）",
      stp.looks_like_release("Some.Movie.1080p.BluRay-WiKi")[0])
check("★★ 只有年份+中文 ⇒ **不算**（弱信号只有一条；避免把分类层当发布名）",
      not stp.looks_like_release("欧美剧")[0],
      "got %r" % (stp.looks_like_release("欧美剧"),))
check("★ 纯中文无名 ⇒ 不算", not stp.looks_like_release("儿童")[0])


# --------------------------------------------------------------------------- #
print("\n== 9 ★★ `.iso` 是片子（第一版把它当空目录 —— 误报）==")
# 真数据：`LOVELY_RUNNER_01-04/` 里躺着 **4 个 `.iso`、156 GB**，第一版报「空目录」。
_t7 = pathlib.Path(tempfile.mkdtemp())
mk(_t7, "LOVELY_RUNNER_01-04", files=["LOVELY_RUNNER_01.iso",
                                      "LOVELY_RUNNER_02.iso"])
v9 = stp.classify("LOVELY_RUNNER_01-04", _t7 / "LOVELY_RUNNER_01-04")
check("★★ `.iso` 直挂 ⇒ depth 1（不是空目录）", v9.picked == 1,
      "got %r —— 第一版这里报『空目录』：156 GB 的片子被当成空" % (v9.picked,))
check("★★ 不许标 `空目录`", "空目录" not in v9.flags, "flags=%r" % (v9.flags,))
# 大小写：真数据里是 `LES_MISERABLES_...ISO`（大写）
_t7b = pathlib.Path(tempfile.mkdtemp())
mk(_t7b, "Les Mis 2018", files=["LES_MISERABLES_2018_S01D1_DIY_3201.ISO"])
check("★ 大写 `.ISO` 也认",
      stp.classify("Les Mis 2018", _t7b / "Les Mis 2018").picked == 1)
# ★★ 变异：把 `VIDEO_EXT` 里的 `.iso` 去掉 ⇒ 上面三条必红。

# --------------------------------------------------------------------------- #
print("\n== 10 ★★ 原盘结构（`BDMV/`）不是空目录（第二个误报）==")
# 真数据：`Robot Chicken S05...` / `Shorts from Golestan Studio` 都只有
# `BDMV/` + `CERTIFICATE/`，第一版报「空目录」—— 因为 `subdirs()` 把它们滤掉了。
_t8 = pathlib.Path(tempfile.mkdtemp())
mk(_t8, "Robot Chicken S05 1080i Blu-ray VC-1 TrueHD 5.1-Fluffy",
   dirs=["BDMV", "CERTIFICATE"])
v10 = stp.classify("Robot Chicken S05 1080i Blu-ray VC-1 TrueHD 5.1-Fluffy",
                   _t8 / "Robot Chicken S05 1080i Blu-ray VC-1 TrueHD 5.1-Fluffy")
check("★★ 只有 BDMV/ ⇒ depth 1（不是空目录）", v10.picked == 1,
      "got %r —— 第一版报『空目录』，因为 subdirs() 把 bdmv 滤掉了" % (v10.picked,))
check("★★ 标 `原盘结构`", "原盘结构" in v10.flags, "flags=%r" % (v10.flags,))
check("★★ `原盘结构` 只是提示，不算异常", not stp._needs_human(v10),
      "flags=%r —— 原盘是正常形态，不该混进『需要你看』" % (v10.flags,))
# ★★ 变异：把 情形 A2（`has_disc_structure` 那段）删掉 ⇒ picked 变 None ⇒ 必红。

# --------------------------------------------------------------------------- #
print("\n== 11 ★ 真·空目录仍然必须报（别把上面两条修成什么都不报）==")
_t9 = pathlib.Path(tempfile.mkdtemp())
mk(_t9, "儿童")
mk(_t9, "欧美剧")
for _nm in ("儿童", "欧美剧"):
    _vv = stp.classify(_nm, _t9 / _nm)
    check("★ 真·空目录 `%s` ⇒ 不猜 + 算异常" % _nm,
          _vv.picked is None and "空目录" in _vv.flags and stp._needs_human(_vv),
          "got picked=%r flags=%r" % (_vv.picked, _vv.flags))
# ★ ⑨⑩⑪ 是**一对反面哨兵**：⑨⑩ 说"有片子别报空"，⑪ 说"真空的必须报" ——
#   三条一起才说明判据**分得清**这两件事（若为了修误报把 `空目录` 判据整个删掉，⑪ 必红）。

# --------------------------------------------------------------------------- #
print("\n== 12 ★★ `--emit-roots`：稳的 / 名字存疑两段 + 判不出的不进清单 ==")
# 判据（用户 2026-10-03 拍）：① 纯 root 行 · ② 分两段 · ③ 不去重（透 scan_pack 的 dups）。
# ★ 这一段的**价值**在于：`--emit-roots` 吐的清单会**直接粘进 .env**，
#   粘错了就是"某个包永远不被搜"（静默漏）—— 所以三件事都得钉住。
_t10 = pathlib.Path(tempfile.mkdtemp())
mk(_t10, "Arcane.S02.2160p.BluRay-WiKi", files=["Arcane.S02E01.mkv"])
mk(_t10, "Avatar.S01.2160p.NF.WEB-DL.DV.H.265-DepWeb", files=["Avatar.S01E01.mkv"])
# 名字不像发布名（有片子、depth=1 对，但搜索名不好用）
mk(_t10, "LOVELY_RUNNER_01-04", files=["d1.iso"])
# 判不出 ⇒ **不进清单**
mk(_t10, "儿童")                                     # 空目录
mk(_t10, "The.Men.Who.Built.America", dirs=["MEN_D1"])  # 拿不准

_vs = [stp.classify(n, _t10 / n,
                    siblings=["Arcane.S02.2160p.BluRay-WiKi",
                              "Avatar.S01.2160p.NF.WEB-DL.DV.H.265-DepWeb",
                              "LOVELY_RUNNER_01-04", "儿童",
                              "The.Men.Who.Built.America"])
       for n in sorted(p.name for p in _t10.iterdir())]
_txt = stp._emit_roots(_vs, dups=[])

# --- ① 两段切分正确（用段标题上的条数钉，不靠人眼）---
check("★ 第一段（稳的）标 2 条",
      "# ---- 第一段：稳的（2 条）----" in _txt, "段标题没对上")
check("★ 第二段（名字存疑）标 1 条",
      "# ---- 第二段：名字存疑（1 条" in _txt, "段标题没对上")

# --- ② 稳的在第一段、存疑在第二段（**顺序也钉** —— 分段的全部意义就在这）---
_i1 = _txt.index("# ---- 第一段")
_i2 = _txt.index("# ---- 第二段")
_i3 = _txt.index("# ---- 不进清单")
check("★ Arcane 落在第一段",
      _i1 < _txt.index("Arcane.S02.2160p.BluRay-WiKi") < _i2,
      "落错段了 —— 稳的被分到存疑段")
check("★ LOVELY_RUNNER 落在第二段",
      _i2 < _txt.index("LOVELY_RUNNER_01-04") < _i3,
      "名字不像发布名的没被单独成段 ⇒ 人没法『只贴稳的那段』")

# --- ③ 判不出的**不进清单**（但要留痕，绝不静默丢）---
check("★★ 空目录没进清单（只在『不进清单』小节里带 # 出现）",
      "\n" + str(_t10 / "儿童") not in _txt and "#   " + str(_t10 / "儿童") in _txt,
      "空目录被当成可喂条目了 —— 它没有 depth，喂进去只会出错")
check("★★ 拿不准的也没进清单",
      "\n" + str(_t10 / "The.Men.Who.Built.America") not in _txt,
      "拿不准的被喂进去了 —— 违反 §19.1『判不出别猜』")

# --- ④ 纯 root 行：**必须没有 markdown 表格符**（否则粘进 .env 是脏数据）---
check("★ 清单是纯 root 行（没有 `| ` 表格符）",
      "| `" not in _txt, "混进了 markdown —— 粘进 .env 会变成脏行")
check("★ 每一条可喂行都是绝对路径（`/` 或盘符开头）",
      all(ln.startswith("/") or (len(ln) > 1 and ln[1] == ":")
          for ln in _txt.splitlines()
          if ln and not ln.startswith("#")),
      "有非路径行混进清单体")

# --- ⑤ 不去重，但 dups **要透出来**（判据只有一份，别在这另写一套）---
_txt_dup = stp._emit_roots(_vs, dups=["Duplicated.Name", "Duplicated.Name"])
check("★★ dups 被透出（含去重后的个数说明）",
      "Duplicated.Name" in _txt_dup and "报重名" in _txt_dup,
      "dups 没透出来 —— 将来真有重名时会静默丢")
# ★★ 变异：把 `_emit_roots` 里 stable/dubious 的 `_DUBIOUS_FLAG not in v.flags`
#    条件对调（即两段互换）⇒ 上面『Arcane 落在第一段』『LOVELY_RUNNER 落在第二段』必红。
# ★★ 变异：把 `picked is None` 的条目也 append 进 stable ⇒
#    『空目录没进清单』『拿不准的也没进清单』两条必红。
# ★★ 变异：把 `_emit_roots` 里 `if dups:` 那段删掉 ⇒ 『dups 被透出』必红。


# --------------------------------------------------------------------------- #
print()


if _ok != _n:
    print("!!! %d/%d 失败" % (_n - _ok, _n))
    sys.exit(1)
print("全部通过（%d 条）" % _n)
