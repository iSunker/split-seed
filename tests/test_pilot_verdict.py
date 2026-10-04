# -*- coding: utf-8 -*-
"""**试点期建议** —— 报出「这个包试了几轮 / 拿到几个读数」（`e404b1ca#8`）。

为什么要钉这个（用户 2026-09-20 问：「**n 轮后 0 读数，这个 n 怎么来的？**」）
==========================================================================
⇒ 立规矩：**说不出就别说**。`n` **不是拍的**，是**从 `mbf` 反推**的：

    mbf 被驱动的实测读数（`SUMMARY §20.9`）：**8 批 / `ok=32` / `Found 0 torrents`**

  · 8 批 —— 它在 `PACKS_DEFAULT` 里（index 1），轮换到它 8 次
  · ok=32 —— 8 批里共发出 32 次搜索请求（**≠ 0** ⇒ 请求真的发出去了，
    所以"0 读数"**不是**"没跑成"）
  · `Found 0 torrents` —— 站点侧一个单种都没找到

★ `n = 8` 的出处：**"一批" = "轮换到它一次"**；取 8 是因为 mbf 恰好在第 8 批
  之后拿到了它那份**完整的**读数 —— 即 **"再看下去也不会有新信息"的那个点**。
  ★ 要改这个默认，**必须换一个同样有出处的数**并记下现场（见
  `state.py::PILOT_ROUNDS_DEFAULT` 那段）。

★ 本文件钉五个分界：

  ① ★★ **样本不够 ⇒ `too-early`** —— 试点期的第一条纪律是**别在样本不够时
     下结论**。`rounds < min_rounds` 就只说"还早"。
  ② ★★ **`ok == 0` ⇒ `error`（观测失败），与"没产出"是两回事** ——
     把"一次请求都没发出去"读成"跑了没产出"会让人**把好包误删**
     （`n/a ≠ 0 ≠ 没事`，`ERR-AI-03`）。★ 这条最容易漏、后果最重。
  ③ ★★ **读数 = `newly_seeding`，不是 `ok`、也不是 `matched`** ——
     `mbf` 的 `ok=32` 而 0 产出 ⇒ 用 `ok` 会把 mbf **判成有产出**（正好判反）；
     `matched` 来自 cross-seed 记账（§26.33 那个 30 倍偏差的源头），
     它**漏记**时会把有产出的包判成没产出。
  ④ ★★ **`n` 来自常量，不在调用处硬编码** —— 判据可测、出处可查。
  ⑤ ★★ **只建议**：函数**只返回一个字符串**，**没有任何** `PACKS_DEFAULT`
     的写入路径。改名单是**产品决定**（`drive-loop.py:103-132` 那段注释里
     写得很硬：「它**不是**一行等着被消除的代码债，**是一个产品决定**」）。

★★ 变异验证（**实测过，数字是当场跑出来的**）：
  · 把 `ok <= 0` 那条判据删掉（= 把观测失败当没产出）
    ⇒ 红 **4** 条（②那三条 + ⑤一条）；
  · 把读数判据从 `newly_seeding` 换成 `ok`（= 用"问了几次"当"拿到什么"）
    ⇒ 红 **5** 条（★ 含③那两条 —— mbf 形状被**判成 keep**，正好判反）；
  · 把 `rounds < min_rounds` 那条删掉（= 样本不够也下结论）
    ⇒ 红 **6** 条（①那三条 + ④两条 + ⑤一条）。

素材全部合成：这是**纯函数**，没有 SQLite、没有网络、没有 NAS。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
# ★ 2026-10-05（B2）：容器代码收进 data/ ⇒ orchestrator / drive-loop 源码
#   都在 data/ 下。把 data/ 也加进 import 搜索路径（本文件上一行插的是仓库根）。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "data"))

from orchestrator.state import PILOT_ROUNDS_DEFAULT, pilot_verdict   # noqa: E402

# ★ 输出强制 UTF-8：GBK 控制台下会中途炸掉，而**已过的断言看着全是 ok** —— 极易误判。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

fails = []


def ck(label, got, want):
    ok = got == want
    print(("  ok  " if ok else " FAIL ") + f"{label}: {got!r}")
    if not ok:
        fails.append(f"{label}: got {got!r} want {want!r}")


#: mbf 的**实测**读数（`SUMMARY §20.9`）：8 批 / ok=32 / Found 0 torrents
MBF_ROUNDS, MBF_OK, MBF_YIELD = 8, 32, 0

print("== ① ★★ 样本不够 ⇒ too-early（别在样本不够时下结论）==")

ck("0 批 ⇒ 还早", pilot_verdict(0, 0, 0), "too-early")
ck("7 批（差一批）⇒ 还早", pilot_verdict(7, 0, 28), "too-early")
ck("★ 7 批即使 0 产出也**不下结论**", pilot_verdict(7, 0, 28), "too-early")
ck("8 批 ⇒ 不再是 too-early", pilot_verdict(8, 0, 32), "no-yield")
ck("★ 默认阈值 == 8（= mbf 那个有出处的数）", PILOT_ROUNDS_DEFAULT, 8)

print()
print("== ② ★★ `ok == 0` ⇒ error（观测失败 ≠ 没产出）==")

ck("8 批但 ok=0 ⇒ error（**不是** no-yield）", pilot_verdict(8, 0, 0), "error")
ck("★ 反面对照：error **不等于** no-yield（混同会误删好包）",
   pilot_verdict(8, 0, 0) != "no-yield", True)
# ★ 即使"批数很多"也一样 —— 批数多不代表请求发出去了
ck("20 批 ok=0 ⇒ 仍是 error（多跑几轮不改这个结论）",
   pilot_verdict(20, 0, 0), "error")

print()
print("== ③ ★★ 读数 = newly_seeding（不是 ok、不是 matched）==")

# ★ mbf 的**实测形状**：ok=32（问了很多次）而 newly_seeding=0（什么都没拿到）
ck("★ mbf 实测形状（8 批 / ok=32 / 0 产出）⇒ no-yield",
   pilot_verdict(MBF_ROUNDS, MBF_YIELD, MBF_OK), "no-yield")
# ★ 反面：若用 ok 当读数，mbf 会被判成 keep —— 钉住"不是 keep"
ck("★ 反面对照：**不是** keep（用 ok 当读数就会判反）",
   pilot_verdict(MBF_ROUNDS, MBF_YIELD, MBF_OK) != "keep", True)
# 有产出 ⇒ keep
ck("8 批 / ok=40 / 新增做种 16 ⇒ keep", pilot_verdict(8, 16, 40), "keep")
ck("哪怕只有 1 部产出也是 keep（不是 0 就算没产出）",
   pilot_verdict(8, 1, 40), "keep")

print()
print("== ④ ★★ 阈值可从参数给（判据可测），默认来自常量 ==")

ck("min_rounds=3 时，3 批就够了", pilot_verdict(3, 0, 12, min_rounds=3), "no-yield")
ck("同一输入、默认阈值下是 too-early", pilot_verdict(3, 0, 12), "too-early")
ck("★ 同一输入、只差 min_rounds ⇒ 结果不同（分辨力就在这个参数上）",
   pilot_verdict(3, 0, 12) != pilot_verdict(3, 0, 12, min_rounds=3), True)

print()
print("== ⑤ ★★ 只建议：返回的是个字符串，没有别的出口 ==")

# ★ 返回值**必须**是这四档之一 —— 调用方只该拿它去**渲染文案**，
#   不该有任何"按它自动改配置"的路径。
for rounds, ns, ok, want in [
    (0, 0, 0, "too-early"), (8, 0, 0, "error"),
    (8, 0, 32, "no-yield"), (8, 5, 32, "keep"),
]:
    ck(f"rounds={rounds} ns={ns} ok={ok} ⇒ {want}",
       pilot_verdict(rounds, ns, ok), want)

ck("返回值是 str（不是 bool / 不是 None）",
   isinstance(pilot_verdict(8, 5, 32), str), True)

print()
if fails:
    print("FAILED %d:" % len(fails))
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("全过")
