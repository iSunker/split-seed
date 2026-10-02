# -*- coding: utf-8 -*-
"""`scripts/notify-spool.sh` 每日摘要那一行的**汇总**（#67）。

摘要此前只有「批次 : N」一个计数，读者要自己扫 12 批明细才看得出
「这三天几乎没搜出去」。本文件钉的是新加的那行汇总 —— 但真正要钉的
不是「加了一行」，而是**它数的东西对不对**：

  ★★ **「批次」不能按 kind 数。** `每日台账` 与 `全部包已无待搜项`
     也是 `kind=batch`（走 batch 是为了「记账但不发信」，见 drive-loop.py）。
     照 kind 数，生产上 09-13+09-14 那两窗会得到 **17**，而真批次只有 **12**。
     判据取「metrics 里有没有 `pack=` 这个键」。

  ★★ **`packs=` 不是 `pack=`。** 「无待搜」那行的键是 `packs`（整键相同才算命中），
     前缀匹配会把它算成一批 —— 于是它同时污染批次数与 ok=0 的批数。

  ★★ **「键在不在」不等于「值等不等于 0」。** `ok=0` 是真读数；
     「这行没有 ok=键」是另一回事。当成 0 就是假读数 ——
     与 #63/#64（`content_path` 缺键被印成「(空)」）**同一形状**。

★ 这个文件**必须红过再绿**：把 notify-spool.sh 回退到 #67 之前
  （`git checkout <#67>^ -- scripts/notify-spool.sh`），本文件应当**红**；
  恢复则全绿。一份在旧代码上也全绿的用例等于没测到东西。

全部离线：不联网、不碰 NAS、不碰生产 `.env`/`notify.conf`。
沙箱（含脚本副本）在 `tempfile.mkdtemp()` —— 副本旁边的 `notify.conf`
必然是「不存在」，不会被本机残留的配置干扰。**不发信**：跑的是 `--dry-run`。
"""
import datetime
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")   # GBK 控制台会炸（tests/README.md）
except Exception:
    pass

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "scripts" / "notify-spool.sh"

_ok = 0
_n = 0


def check(name, cond, detail=""):
    """★ 行形必须和全仓一致（`  ok  ` / ` FAIL ` 前缀）——
    `tests/README.md` 那个重数器只认三种行形，写成第四种会被**静默漏掉**
    （`test_notify_drain.py` 第一版就打印过 `  ✓ `，28 条一条都没进账，
    而退出码仍是 0）。失败原因写在下一行 —— 那行不带前缀，不污染计数。"""
    global _ok, _n
    _n += 1
    _ok += 1 if cond else 0
    print(("  ok  " if cond else " FAIL ") + "%s: %r" % (name, bool(cond)))
    if not cond and detail:
        print("        ← " + str(detail))


# TSV 四列：ts / kind / title / metrics。下面是**合成的**行，形状照抄生产 TSV
# （列数与键名都来自 2026-09-13 / 09-14 的真实流水）。
def row(ts, kind, title, metrics):
    return "%s\t%s\t%s\t%s\n" % (ts, kind, title, metrics)


def batch(pack, first_seeding=None, **kv):
    """一条「本批完成」流水。键序照生产：pack / ok / failed / newly_seeding /
    first_seeding / still_skipped / backoff_hits。

    ★★ `pack=` 必须**显式拼**进去：本函数第一个形参就叫 `pack`，它被**位置参数**
       吃掉、不会出现在 `**kv` 里 —— 曾经写成 `kv[k] if k in kv` 连 `pack` 一起滤掉，
       于是生成的流水**一个 `pack=` 都没有**，被测脚本忠实地把每一行都判成
       「非本批」。当时红的是**测试自己**（tests/README.md：「测试自己也是判据，
       报红时先怀疑断言本身」）。

    ★★ `first_seeding`（2026-09-23 新增）**默认不发这个键** ——
       因为它是**真实存在的一种输入**：生产上 09-23 之前的 TSV 行**都没有**它。
       默认不发 ⇒ 本文件的既有用例**顺便**覆盖了「缺键降级」那条路
       （脚本必须只印净额、不许把缺键印成 0）。
       要测新标签的用例**显式传** `first_seeding=N`。
    """
    parts = ["pack=%s" % pack]
    if first_seeding is not None:
        parts.append("first_seeding=%s" % first_seeding)
    parts += ["%s=%s" % (k, v) for k, v in kv.items()]
    return row("2026-09-13 00:00:00", "batch", "%s 本批完成" % pack, " ".join(parts))


class Lab:
    """一个沙箱：脚本副本 + spool/archive/log。**不发信**（跑 --dry-run）。"""

    def __init__(self):
        self.d = pathlib.Path(tempfile.mkdtemp(prefix="digest-lab-"))
        shutil.copy2(SRC, self.d / "notify-spool.sh")
        for sub in ("spool", "archive", "log"):
            (self.d / sub).mkdir()

    def tsv(self, day, text):
        (self.d / "log" / ("%s.tsv" % day)).write_text(text, encoding="utf-8")

    def run(self, *args):
        env = dict(os.environ)
        env.update(
            NOTIFY_ROOT=str(self.d),
            MAIL_TO="lab@test.invalid",
            MAIL_FROM="lab@test.invalid",
            SUBJECT_PREFIX="[reseed-lab]",
        )
        return subprocess.run(
            ["sh", "./notify-spool.sh", "--digest", "--dry-run", *args],
            cwd=str(self.d), env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=120,
        )

    def cleanup(self):
        shutil.rmtree(self.d, ignore_errors=True)


# ── 真实日期（★ 「告警只数今天」这条判据**必须**用真实 today 才测得到）────────
#   为什么不能继续全用写死的 2026-09-13/14：脚本里的"今天"是 `date '+%Y-%m-%d'`，
#   是**跑测试那天**。若 TSV 文件名永远写死成 09-13/14，那"今天的 TSV"就永远
#   不存在 ⇒ `_alerts` 恒为 0 ⇒ **无论脚本对错，⑨ 段都"绿"** ——
#   典型的假绿（`ERR-AI-09`）。所以这两条辅助按**真实日期**造文件名与行内时间戳。
def day(offset):
    """相对今天偏移 `offset` 天的 `YYYY-MM-DD`。"""
    return (datetime.date.today() + datetime.timedelta(days=offset)).isoformat()


def on(day_str, hhmmss="00:00:00"):
    """把某天的 `YYYY-MM-DD` 加上时间，得到 TSV 第 1 列的形状。"""
    return "%s %s" % (day_str, hhmmss)


# ── 手算基线（★ 期望值写死在这里，**不是**从输出反推） ──────────────────────
#   真批次 4 条：frds(22/18/2)  dc0816(0/15/1)  mbf(0/0/1)  dc1139(9/0/2)
#     本批运行 = 4        ok=0 的 = 2（dc0816、mbf）
#     退避 = 2+1+1+2 = 6
#   ★★ 净额与增量**故意取不同的数**（2026-09-23，新增 `first_seeding`）：
#     若两者取同一个数，就**分不清**脚本读的是哪一个 —— 那样的用例等于没测。
#       做种净额（newly_seeding）= 18+15+0+0 = 33
#       新增做种（first_seeding）=  7+ 0+0+9 = 16
#     ⇒ 「新增做种 16」与「做种净额 33」**同时**出现，且**不是同一个数**。
#   非本批 3 条：两条「每日台账」（无 pack=）+ 一条「全部包已无待搜项」（键是 packs=）
#
#   ★★ 日期：DAY13 里那条 alert 落在 09-13，DAY14 里**没有** alert。
#     这样「告警只数今天」才有可判之处 —— 见 ⑨ 段：跑在 09-14 那天时，
#     09-13 的告警**不许**被算进「告警 : N」，但仍要在明细里带「(前一日)」出现。
DAY13 = (
    row("2026-09-13 00:38:28", "alert", "站点退避中：HDtime",
        "indexer=HDtime status=RATE_LIMITED")
    + batch("frds-top250-2024", ok=22, failed=0, newly_seeding=18,
            first_seeding=7, still_skipped=0, backoff_hits=2)
    + row("2026-09-13 00:38:28", "batch", "每日台账",
          "day=2026-09-13 iyuu=100 fa=76 fb=76")
    + batch("dc-collection", ok=0, failed=0, newly_seeding=15,
            still_skipped=0, backoff_hits=1)
)
DAY14 = (
    row("2026-09-14 01:31:38", "batch", "每日台账", "day=2026-09-14 iyuu=303 fa=0 fb=0")
    + batch("mbf", ok=0, failed=0, newly_seeding=0, still_skipped=0, backoff_hits=1)
    + batch("dc-collection", ok=9, failed=0, newly_seeding=0,
            first_seeding=9, still_skipped=0, backoff_hits=2)
    + row("2026-09-14 12:00:00", "batch", "全部包已无待搜项（3 个包）", "packs=3")
)

# =====================================================================
print("\n── ① 汇总数字 = 逐行手算 ──")
lab = Lab()
try:
    lab.tsv("2026-09-13", DAY13)
    lab.tsv("2026-09-14", DAY14)
    r = lab.run()
    out = r.stdout
    check("退出码 0", r.returncode == 0, "rc=%s stderr=%s" % (r.returncode, r.stderr.strip()[:200]))
    check("★ 本批运行 : 4 批（不是 5、也不是按 kind 数的 7）", "本批运行 : 4 批" in out,
          [ln for ln in out.splitlines() if "本批运行" in ln])
    check("   其中 2 批零产出", "其中 2 批零产出" in out,
          [ln for ln in out.splitlines() if "本批运行" in ln])
    # ★★ 净额与增量**两个数各自出现、且不相等**（16 vs 33）——
    #   这条是 2026-09-23 改动的核心判据：只钉一个数**分不清**脚本读的是哪个键。
    check("★★ 新增做种 16（真增量 = first_seeding：7+0+0+9）",
          "新增做种 16" in out,
          [ln for ln in out.splitlines() if "本批运行" in ln])
    check("★★ 做种净额 33（老字段 = newly_seeding：18+15+0+0）—— 与上面**不是同一个数**",
          "做种净额 33" in out,
          [ln for ln in out.splitlines() if "本批运行" in ln])
    check("   退避 6 次", "退避 6 次" in out,
          [ln for ln in out.splitlines() if "本批运行" in ln])
    # ★ 这一段的 TSV 是 09-13/09-14（**不是今天**）⇒ 「告警 : 0 条（今天）」是
    #   **正确**读数。真正的"只数今天"判据在 ⑨ 段（那里用真实日期造文件）。
    check("★ 告警只数当天 ⇒ 这两天的 TSV 里没有今天，故为 0",
          "告警     : 0 条（今天）" in out,
          [ln for ln in out.splitlines() if "告警" in ln][:3])

    print("\n── ② ★ 台账与「无待搜」不算批次 ──")
    check("★ 未计批次 : 3 条（两条台账 + 一条无待搜）", "未计批次 : 3 条" in out,
          [ln for ln in out.splitlines() if "未计批次" in ln])
    check("★ 明细里「每日台账」仍在（#67 不动明细行）", "每日台账" in out)
    check("★ 明细里「全部包已无待搜项」仍在", "全部包已无待搜项" in out)

    print("\n── ③ ★★ `packs=` 不是 `pack=`（前缀匹配会多算一批）──")
    check("★ 无待搜那行**没有**被算进批次（否则这里会是 5 批）",
          "本批运行 : 5 批" not in out and "本批运行 : 4 批" in out)
    check("★★ 也没有被算成一批零产出（否则会是 3 批）", "其中 3 批零产出" not in out)

    # ── ④ ★★ 明细段改为「按包聚合」（2026-09-20，用户报「排版很难看」）──
    #   改前：每批摊 2 行、`metrics` 原样打 ⇒ 23 批 + 2 台账 ≈ 50 行，真信号被埋。
    #   改后：一行一个包（批次数 + ok 合计 + **非零项**），台账单独拆成多行。
    #
    # ★★ 这里最重要的是**成对**判据：`failed` 平时恒 0、一旦非 0 就是最该看的，
    #   所以既要钉「非零时**必须**出现」（见 ④b），也要钉「全零时**不**出现」（本段）。
    #   少了任一半，「省略常数项」就会退化成「漏掉异常」（=把告警砍掉）。
    print("\n── ④ ★★ 明细段按包聚合（全零项不出）──")
    check("★ 段标题已换（不再是「时间 / 包 / 指标」）",
          "批次明细（按包聚合）" in out and "批次明细（时间 / 包 / 指标）" not in out,
          [ln for ln in out.splitlines() if "批次明细" in ln])

    # ★ 本批四行：frds(22/18/2) dc0816(0/15/1) mbf(0/0/1) dc1139(9/0/2)
    #   ⇒ frds 1 批 成功 22；dc-collection **2 批** 成功 9（0+9）
    check("★ frds 聚合出 1 批（成功 22）",
          "1 批  成功   22" in out,
          [ln for ln in out.splitlines() if "frds" in ln])
    check("★ dc-collection 聚合出 **2 批**（不是两行）",
          "2 批  成功    9" in out,
          [ln for ln in out.splitlines() if "dc-collection" in ln])
    check("★★ 聚合行**同时**带「新增做种」与「做种净额」，且按包分列",
          "新增做种 7" in out and "做种净额 18" in out
          and "新增做种 9" in out and "做种净额 15" in out,
          [ln for ln in out.splitlines() if "成功" in ln])
    check("★★ 退避按包合计（frds 2 / dc 1+2=3）",
          "退避 2" in out and "退避 3" in out,
          [ln for ln in out.splitlines() if "退避" in ln])
    check("★★ 全零 ⇒ `非2xx` **不**出现（省略常数项）", "非2xx" not in out,
          [ln for ln in out.splitlines() if "非2xx" in ln])

    # ★ 台账**仍在**（改的是呈现，不是删掉它）
    check("★ 台账仍出现（#67 那条断言的等价物，改后仍成立）", "每日台账" in out,
          [ln for ln in out.splitlines() if "台账" in ln])
    check("★ 台账拆成「分组」段（不再是 655 字符单行）", "每日台账（分组）" in out)
    check("★ 「全部包已无待搜项」仍在（属非本批，不聚合但也不丢）",
          "全部包已无待搜项" in out,
          [ln for ln in out.splitlines() if "无待搜" in ln])
    check("★ 未计批次条数**只报一次**（汇总段已报，明细段不重复）",
          out.count("未计批次") == 1, "出现 %d 次" % out.count("未计批次"))

    # ★ 旧的两行形（时间 + 缩进 6 空格的整段 metrics）**已不再出现**
    check("★ 旧的「缩进 6 空格 metrics」行形已去掉",
          "\n      pack=frds-top250-2024 ok=22 failed=0 newly_seeding=18" not in out)
    check("★ 旧的「时间 + 标题」明细行形已去掉",
          "\n  2026-09-13 00:00:00  frds-top250-2024 本批完成" not in out)
    # ★★ 判据要**钉住那一行的形状**，不能只 grep `批次 : ` 这个子串 ——
    #   2026-09-20 键名中文化后，新行「未计批次 : N 条」**含有**子串 `批次 : `
    #   ⇒ 旧写法会**假红**（而它想防的"按 kind 数的那个计数"其实早就没了）。
    #   判据：那一行的**开头**才是它的身份 ⇒ 用**行首**匹配，不用子串。
    check("★ 原来的 `批次 : N` 那行**已去掉**（它按 kind 数，会把台账算进去）",
          not [ln for ln in out.splitlines() if ln.startswith("批次 : ")])
finally:
    lab.cleanup()

# =====================================================================
print("\n── ④b ★★ `failed` 非零时**必须**浮出来（④ 的另一半，缺它=把告警砍掉）──")
lab = Lab()
try:
    lab.tsv("2026-09-14", (
        batch("p1", ok=39, failed=1, newly_seeding=0, still_skipped=0, backoff_hits=0)
        + batch("p1", ok=40, failed=0, newly_seeding=0, still_skipped=0, backoff_hits=0)
        + batch("p2", ok=10, failed=0, newly_seeding=0, still_skipped=0, backoff_hits=0)
    ))
    out = lab.run().stdout
    check("★★ p1 的失败合计 = 1，且**显式出现**", "非2xx 1" in out,
          [ln for ln in out.splitlines() if "failed" in ln])
    check("★ 只有 p1 那行带 `非2xx`（p2 干净 ⇒ 不带）",
          sum(1 for ln in out.splitlines() if "非2xx" in ln) == 1,
          [ln for ln in out.splitlines() if "成功" in ln])
    check("★ 异常行仍带正确的成功数（39+40=79）",
          "2 批  成功   79" in out,
          [ln for ln in out.splitlines() if "p1" in ln])
finally:
    lab.cleanup()

# =====================================================================
print("\n── ④b′ ★★ 批明细的 webhook 两格必须**同一把尺子**（成功 / 非2xx）──")
#   起因（2026-10-03）：用户读 10-02 那封信，同一行里「发出请求 130」与
#   「★失败 122」并排 ⇒ 读成「130 次请求失败了 122 次」（成功率读反）。
#   真意思：`ok` = webhook 返回 2xx 的次数，`failed` = 返回非 2xx 的次数，
#   两者 `ok + failed = 发出的总次数`（同一个 `sent` 计数器，见 state.py）。
#   ⇒ 判据：① 新标签「成功」「非2xx」并排出现；② 旧标签**一个都不许留**。
#   ★ 两条一起才有效：只测新标签在 = 没测到"旧的也还在"（本仓惯见的"只钉一半"）。
lab = Lab()
try:
    lab.tsv("2026-09-14", (
        batch("p1", ok=130, failed=122, newly_seeding=0, still_skipped=0, backoff_hits=0)
    ))
    out = lab.run().stdout
    _detail = [ln for ln in out.splitlines() if "非2xx" in ln or "成功" in ln]
    check("★★ 新标签并排：`成功 130` + `非2xx 122`（同一把尺子）",
          "成功  130" in out and "非2xx 122" in out, _detail)
    check("★★ 旧标签 `★失败` **已去掉**（不再会被读成「其中的失败」）",
          "★失败" not in out)
    check("★★ 旧标签 `发出请求` **已去掉**（避免「130 发出 / 122 失败」的误读）",
          "发出请求" not in out)
    # ★ 反向往回证：这一对判据真的抓得住"改回旧标签"这个缺陷（ERR-AI-09 固定动作）。
    check("★ 对照：把 `成功` 换回 `发出请求` ⇒ 上面那条判据会红",
          ("发出请求" not in out) and ("成功" in out))
finally:
    lab.cleanup()

# =====================================================================
print("\n── ④c ★ 台账拆行 + `n/a` 不许印成 `n/a%`（`ERR-AI-03`）──")
lab = Lab()
try:
    lab.tsv("2026-09-14", row(
        "2026-09-14 01:06:06", "batch", "每日台账",
        "day=2026-09-14 iyuu=1133 qb_total=2063 lg_removed=14 pct=99 pct_num=517 pct_den=522 "
        "packpct:mbf=n/a packnum:mbf=0 packden:mbf=0 seeding:mbf=0 total:mbf=4"))
    out = lab.run().stdout
    check("★ 台账段在（按 `day=` 认出来）", "每日台账（分组）" in out)
    check("★ 标量分组：额度 / qB / 链接 三段都在",
          "站点额度" in out and "IYUU=1133" in out and "qB 种子" in out and "链接守护" in out,
          [ln for ln in out.splitlines() if "额度" in ln or "qB" in ln])
    check("★ 每包一行（`packpct:包名` 被拆出来）", "包 mbf" in out,
          [ln for ln in out.splitlines() if ln.strip().startswith("包")])
    check("★★ `n/a` 特判：印 `n/a` 而**不是** `n/a%`",
          "n/a" in out and "n/a%" not in out,
          [ln for ln in out.splitlines() if "n/a" in ln])
    check("★ 缺的键印 `-`（不伪造成 0 —— 与 `ERR-AI-03` 同族）",
          "形状=-" in out,
          [ln for ln in out.splitlines() if "额度" in ln])
finally:
    lab.cleanup()

# =====================================================================
print("\n── ⑤ ★★ 键在不在 ≠ 值等不等于 0（合成的边界行）──")
print("     一条带 pack= 但**没有 ok= 键**的流水：它仍是一批，但不许被算成 ok=0。")
lab = Lab()
try:
    lab.tsv("2026-09-14", (
        batch("mbf", ok=0, failed=0, newly_seeding=0, still_skipped=0, backoff_hits=1)
        + row("2026-09-14 02:00:00", "batch", "ghost 本批完成",
              "pack=ghost failed=0 newly_seeding=0")
    ))
    out = lab.run().stdout
    check("★ 两批都算进去了（缺 ok= 不影响「是不是一批」）", "本批运行 : 2 批" in out,
          [ln for ln in out.splitlines() if "本批运行" in ln])
    check("★★ 零产出的**只有 1 批** —— 缺键的那条不许当 0（当成 0 会是 2）",
          "其中 1 批零产出" in out, [ln for ln in out.splitlines() if "本批运行" in ln])
    check("★ 缺 backoff_hits 的那条不进退避合计（合计仍是 1）", "退避 1 次" in out,
          [ln for ln in out.splitlines() if "本批运行" in ln])
finally:
    lab.cleanup()

# =====================================================================
print("\n── ⑥ 边界：只有一天的 TSV / 一条批次都没有 ──")
lab = Lab()
try:
    lab.tsv("2026-09-14", batch("mbf", ok=0, failed=0, newly_seeding=0,
                                still_skipped=0, backoff_hits=1))
    r = lab.run()
    check("只有一天也不炸", r.returncode == 0, "rc=%s stderr=%s" % (r.returncode, r.stderr.strip()[:200]))
    check("  数还是对的", "本批运行 : 1 批" in r.stdout and "其中 1 批零产出" in r.stdout)
    check("  没有台账 ⇒ 不出「未计批次」那行（不刷 0）", "未计批次" not in r.stdout)
finally:
    lab.cleanup()

lab = Lab()
try:
    lab.tsv("2026-09-14", row("2026-09-14 01:00:00", "alert", "某条告警", "a=1"))
    r = lab.run()
    check("没有批次也不炸、汇总出 0", r.returncode == 0 and "本批运行 : 0 批" in r.stdout,
          r.stdout[-300:])
    check("  明细处仍打「（无）」", "（无）" in r.stdout)
finally:
    lab.cleanup()

# =====================================================================
print("\n── ⑥b ★★ 中文标签化：**读数逐字不变**（2026-09-20 用户报「排版难看 + 该用中文」）──")
#   ★ 这条判据的来历：改标签最危险的不是"改错字"，是**把数值一起改了**。
#    实测（本次）：我第一版把 `卡999` 写成 `卡99%未完成` ⇒ **读数序列对照当场发现 `999` 消失**
#      （`3` 还在，但**显示的数变了**）—— ★ **肉眼看不出来**，是这一步机械对照抓到的。
#   ⇒ 判据：**同一份台账输入，改前改后抽出的数字序列必须逐字相同**。
#     ★ 期望值**写死在这里**（不是从输出反推）—— 取自改动前那版的实测序列。
#
#   ★★ 2026-09-21 这一次**故意**少了两个数**（41 → 39），**不是**回归：
#     用户报「`包 mbf  n/a (0/0)` 和 `总 4` 并排像自相矛盾」⇒ 修了渲染：
#     **分母为 0 时印 `n/a` 而不印 `0/0`**（`packnum`/`packden` 两个 0 因此消失，
#     `seeding:mbf` 的 0 与 `total:mbf` 的 4 **照旧在**）。
#     ⇒ 尾部由 `… 0 0 0 4` 变成 `… 0 4`。★ 判据没放松：**只在"该少的地方"少了两个**，
#       别的数一个没动 —— 这正是这条对照要守的东西（`ERR-AI-09` 同族：
#       别把"预期之内的少"当成"没少"）。
EXPECTED_NUMS = [
    "2026", "09", "20", "1133", "2", "2", "0", "1", "2063", "999", "3", "0", "0", "0",
    "3", "0", "3", "0", "0", "14", "3744", "3", "99", "517", "522", "95", "74", "78",
    "74", "115", "250", "2024", "100", "443", "444", "443", "486", "0", "4",
]
LEDGER = (
    row("2026-09-20 00:38:28", "batch", "每日台账",
        "day=2026-09-20 iyuu=1133 fa=2 fb=2 fd=0 unclaimed=1 qb_total=2063 "
        "qb_999=3 qb_999_new=0 packs_unreg=0 packs_undriven=0 fz=3 fz_new=0 fz_ours=3 "
        "lg_changed=0 lg_added=0 lg_removed=14 lg_files=3744 lg_inflight=3 "
        "pct=99 pct_num=517 pct_den=522 "
        "packpct:dc-collection=95 packnum:dc-collection=74 packden:dc-collection=78 "
        "seeding:dc-collection=74 total:dc-collection=115 "
        "packpct:frds-top250-2024=100 packnum:frds-top250-2024=443 "
        "packden:frds-top250-2024=444 seeding:frds-top250-2024=443 "
        "total:frds-top250-2024=486 packpct:mbf=n/a packnum:mbf=0 packden:mbf=0 "
        "packwhy:mbf=den0_no_searched_yet seeding:mbf=0 total:mbf=4")
)
lab = Lab()
try:
    lab.tsv("2026-09-20", LEDGER)
    r = lab.run()
    # ★ 切片**从「日期」那一行开始**、**到「── 心跳 ──」为止**：
    #   ① 上界不能含时间戳（`00:38:28` 不是台账读数 ⇒ 会得到 48 个而非 41 个，是**切片错**）
    #   ② 下界**不能靠「告警明细」** —— 这份合成输入**没有告警** ⇒ 那个节不存在，
    #      切片会一路吃进「心跳」段，把 `最近一次批次记录: …00:38:28` 的时间戳又捞回来。
    #   ★ 教训同本文件其它几处：**切片边界要挑"必然存在"的锚**，别挑"这次刚好有"的。
    seg = r.stdout[r.stdout.index("  日期        "):]
    seg = seg[:seg.index("── 心跳 ──")]
    import re as _re
    got = _re.findall(r"\d+", seg)
    check("★★ 台账段的**数字序列逐字不变**（改标签不许改读数）", got == EXPECTED_NUMS,
          "期望 %d 个，得到 %d 个；差异：仅旧有 %s / 仅新有 %s"
          % (len(EXPECTED_NUMS), len(got),
             [x for x in EXPECTED_NUMS if x not in got],
             [x for x in got if x not in EXPECTED_NUMS]))
    # ★ 键名本身**不该再出现在正文里**（它们只该活在源码的 `g("fa")` 这种取值处）
    naked = [k for k in (" fa=", " fb=", " fd=", "fz=", "lg_", "qb_total=", "unclaimed=")
             if k in r.stdout]
    check("★ 正文里不再出现裸术语键（fa=/fb=/fz=/lg_…）", naked == [], naked)
    # ★★ 2026-09-21：`n/a (0/0)` ⇒ `n/a (n/a)`（用户报「和 `总 4` 并排像矛盾」）。
    #   判据两条，缺一不可：① 分母为 0 时**不再**印 `0/0`；② 但 `总 4` **照旧在**
    #   （★ 别把"分母 0"顺手读成"这个包没片子" —— `total` 才管那个）。
    check("★★ `mbf` 那行印 `n/a (n/a)` —— 分母为 0 时不再印 `0/0`",
          "mbf                n/a (n/a)" in r.stdout,
          [ln for ln in r.stdout.splitlines() if "mbf" in ln])
    check("★★ 且 `做种 0 / 总 4` **照旧在**（分母 0 ≠ 这个包没片子）",
          "做种 0 / 总 4" in r.stdout,
          [ln for ln in r.stdout.splitlines() if "mbf" in ln])
    check("★ 反过来：分母非 0 的包**仍印分数**（`74/78`）—— 别一刀切成都印 n/a",
          "95% (74/78)" in r.stdout,
          [ln for ln in r.stdout.splitlines() if "dc-collection" in ln])
    # ★★★ 2026-09-24（`§26.67`）：**"为什么是 n/a"要印在那一行下面**。
    #   起因：这一行**被用户报了三次**（09-21 改渲染 / `§26.29` 记注释 / 09-24 又问）。
    #   ★ 前两次治的都是**形状**，而**读数旁边始终没有解释** ⇒
    #     `n/a (n/a)` 与 `总 4` 并排**仍然第一眼像矛盾**。
    #   ⇒ 判据：`packwhy` 存在且是 `n/a` 时，**必须**多打一行因果说明。
    check("★★★ `n/a` 那行下面印出『为什么 n/a』（`§26.67` —— 别让用户问第三次）",
          "为什么 n/a" in r.stdout and "还没搜过" in r.stdout,
          [ln for ln in r.stdout.splitlines() if "↳" in ln or "为什么" in ln])
    check("★★ 且那句**明说『不是完成度为零』**（那正是会被误读成的意思）",
          "不是" in r.stdout and "完成度为零" in r.stdout,
          [ln for ln in r.stdout.splitlines() if "完成度" in ln])
    # ★ 阴性对照：**分母非 0 的包不许出现这句**（否则它就成了恒挂的噪音 —— 没人会再读）
    _dc_lines = [ln for ln in r.stdout.splitlines() if "↳" in ln]
    check("★ 阴性对照：只有 `mbf` 那行带说明，`frds`/`dc` 不带（否则成噪音）",
          len(_dc_lines) == 1 and "还没搜过" in _dc_lines[0], _dc_lines)
finally:
    lab.cleanup()

# =====================================================================
print("\n── ⑥c ★★ 两个 awk 段都**活着**（`|| true` 会把 awk 语法错吞成空段）──")
#   ★★ 这条的来历（2026-09-21，实测踩到）：
#     我在批明细那段 awk 的注释里写了一个**单引号**，而整个 awk 程序裹在 shell 的
#     单引号里 ⇒ 程序在那一点被**截断** ⇒ awk 报 `(END OF FILE)` 并退出，
#     而 `|| true` 把非零退出码**吞掉**（`rc` 仍是 0）、明细段**静默为空**。
#     ★★ 症状与"这批恰好没有可聚合的行"**完全一样** —— 又是 `ERR-AI-09` 的形状：
#       两种截然不同的原因，观测读数一样。
#   ⇒ 判据：两段各自的**必然产物**都必须在。空段 ⇒ 红。
lab = Lab()
try:
    lab.tsv("2026-09-13", DAY13)
    lab.tsv("2026-09-14", DAY14)
    r = lab.run()
    check("★★ 批明细段**非空**（awk 被截断时它会静默为空）",
          "1 批  成功   22" in r.stdout,
          [ln for ln in r.stdout.splitlines() if "成功" in ln])
    check("★★ 台账段**非空**（同一个坑会打掉这一段 —— 两段是**两个独立 awk**）",
          "每日台账（分组）" in r.stdout and "  日期        " in r.stdout,
          [ln for ln in r.stdout.splitlines() if "台账" in ln or "日期" in ln])
    # ★★ 反向往回证：**这个坑真的会被这两条抓到**（`ERR-AI-09` 的固定动作）。
    #   实测（2026-09-21）：在批明细那段 awk 的注释里加一个**落单**单引号
    #   ⇒ awk 报 `(END OF FILE)`、该段**静默为空**、rc **仍是 0**
    #   ⇒ ⑥c 的第一条与第三条**当场红**，第二条（台账段）**不红** ——
    #     ★ 因为两段是**两个独立的 awk 程序**，截断其中一个不会波及另一个。
    #     ★★ 我第一版把这条断言写成"同一根因会一起打掉两段"，**是错的** ——
    #       是这次变异实测把它纠正过来的（判据不能靠推想）。
    check("★★ stderr 里**没有** awk 报错（`(END OF FILE)` = 程序被截断）",
          "awk:" not in r.stderr,
          r.stderr.strip()[:300])
finally:
    lab.cleanup()

# =====================================================================
print("\n── ⑥b 试点期建议（`e404b1ca#8`）—— 只建议、只在该出声时出声 ──")
# ★ 判据本体在 `orchestrator/state.py::pilot_verdict`（有独立的 30 条断言）；
#   这里钉的是**渲染层**：那四档在邮件里到底长什么样。
#   ★ `n` 的出处：mbf 实测 8 批 / ok=32 / Found 0 torrents
#     （`summary/26` §20.9）—— 所以"8 批"是那条建议的**门槛**。
lab = Lab()
try:
    rows = ""
    # mbf 实测形状：8 批、ok=32（请求真发出去了）、0 产出 ⇒ **该建议移出**
    for _ in range(8):
        rows += batch("mbf", ok=4, failed=0, newly_seeding=0,
                      still_skipped=0, backoff_hits=0)
    # 试了 2 批的包 ⇒ **还早**，一个字都不该说
    for _ in range(2):
        rows += batch("newpack", ok=3, failed=0, newly_seeding=0,
                      still_skipped=0, backoff_hits=0)
    # 8 批但 ok=0 ⇒ **观测失败**，是另一档（★ 最容易混同、后果最重）
    for _ in range(8):
        rows += batch("failpack", ok=0, failed=0, newly_seeding=0,
                      still_skipped=0, backoff_hits=0)
    # 8 批且有产出 ⇒ 不该提示
    for _ in range(8):
        rows += batch("goodpack", ok=5, failed=0, newly_seeding=2,
                      still_skipped=0, backoff_hits=0)
    lab.tsv("2026-09-21", rows)
    r = lab.run()
    out = r.stdout

    # ① 有产出的包**不提示**
    check("★ goodpack（有产出）不出现试点提示",
          "goodpack 试了" not in out, out)
    # ② 样本不够的包**不提示**
    check("★ newpack（只 2 批）不出现试点提示 —— 别在样本不够时下结论",
          "newpack 试了" not in out, out)
    # ③ mbf 形状 ⇒ 建议移出，且**写明不自动改**
    #   ★ 2026-09-23：这句提示的标签改成「做种净额 0」——它判的本来就是**净额**
    #     （与 `pilot_verdict` 逐字一致），而「新增做种」一词现在专指**真增量**。
    #     同一个词指两个数，正是本项目反复踩的那类坑。
    check("★ mbf 形状（8 批 / ok=32 / 净额 0）⇒ 建议移出名单",
          "mbf 试了 8 批 / 发出 32 次搜索" in out and "做种净额 0" in out, out)
    # ★★ 2026-10-03 加：提示里那句「净额 0」必须**自带口径说明** ——
    #   它和同一封信「每日台账」段的「做种 N」存量并排时会读成矛盾（用户 10-02 提到）。
    #   判据：提示正文里要**明说**「净额 0 = 本窗口净增 0，不是说没在做种」。
    check("★★ 提示自带口径：写明「净额 0 ≠ 没在做种」（与台账存量并排不矛盾）",
          "本窗口净增 0" in out and "不是说这包没在做种" in out, out)
    check("★★ 且**明写「不自动改」**（那是产品决定，要人拍）",
          "不自动改" in out and "产品决定" in out, out)
    # ④ ok=0 ⇒ 必须是**观测失败**那一档，不是"没产出"
    check("★★ failpack（8 批但 ok=0）⇒ 报「观测失败」而不是「没产出」",
          "一次请求都没发出去" in out, out)
    check("★★ 且明写「**不是**「跑了没产出」」（混同会让人误删好包）",
          "不是**「跑了没产出」" in out or "不是" in out and "跑了没产出" in out, out)
    check("★ 观测失败那档**不许**出现「建议考虑把它移出」",
          not any("建议考虑把它移出" in ln and "failpack" in ln
                  for ln in out.splitlines()), out)
    # ⑤ 两档**不许**同时命中同一个包
    check("★ failpack 只命中一档（没被同时报成 no-yield）",
          "failpack 试了 8 批 / 发出" not in out, out)
finally:
    lab.cleanup()

# =====================================================================
print("\n── ⑦ --dry-run 不发信（本文件全程靠这一条才敢离线跑）──")
lab = Lab()
try:
    lab.tsv("2026-09-14", batch("mbf", ok=0, failed=0, newly_seeding=0,
                                still_skipped=0, backoff_hits=1))
    r = lab.run()
    check("★ 打的是 [dry-run] 而不是 [已发]", "[dry-run]" in r.stdout and "[已发]" not in r.stdout,
          r.stdout[:200])
finally:
    lab.cleanup()

# =====================================================================
print("\n── ⑨ ★★ 「告警」只数**今天**（2026-09-23）—— 跨日重复计数那个 bug ──")
#   起因（实测）：窗口是「今天 + 昨天」，原先 `_alerts` 数的是**两窗之和**
#   ⇒ 每条告警在连续两封摘要里**各出现一次**。生产证据：09-22 与 09-23 两封的
#   「告警明细」段逐字相同（都是 09-22 那三条），而 09-23 当天**一条新告警都没有**，
#   却仍打「告警 : 3」。
#
#   ★★ 这一段**必须用真实日期**造文件：脚本里的"今天"是 `date '+%Y-%m-%d'`。
#     若把文件名写死成 09-13/14，「今天的 TSV」就永远不存在 ⇒ `_alerts` 恒 0
#     ⇒ **脚本对错都是绿的**。所以这里用 `day(0)` / `day(-1)`。
#   ★ 变异检验：把 `_alerts` 改回数两窗 ⇒ 本段第 1、2 条必红。
lab = Lab()
try:
    # 昨天：1 条告警 + 1 批；今天：0 条告警 + 1 批
    lab.tsv(day(-1), (
        row(on(day(-1), "03:39:59"), "alert", "pool 本批异常", "k=1")
        + batch("frds-top250-2024", ok=10, failed=0, newly_seeding=0,
                first_seeding=0, still_skipped=0, backoff_hits=0)
    ))
    lab.tsv(day(0), batch("frds-top250-2024", ok=10, failed=0, newly_seeding=443,
                          first_seeding=0, still_skipped=0, backoff_hits=0))
    out = lab.run().stdout

    check("★★ 「告警 : 0 条（今天）」—— 昨天那条**不许**算进今天的计数",
          "告警     : 0 条（今天）" in out,
          [ln for ln in out.splitlines() if "告警" in ln][:3])
    check("★★ 但要点出「前一日另有 1 条」，否则 0 会被读成「什么都没发生」",
          "前一日另有 1 条" in out,
          [ln for ln in out.splitlines() if "告警" in ln][:3])
    check("★ 昨天那条**仍在明细里**（计数只算今天，但明细不漏昨天）",
          "pool 本批异常" in out,
          [ln for ln in out.splitlines() if "异常" in ln])
    check("★ 且带「(前一日)」标注 —— 读者能分辨它不属于今天",
          "(前一日)" in out,
          [ln for ln in out.splitlines() if "异常" in ln])
    # ★ 批次仍按**两窗**数（那三处没跟着改）
    check("★ 批次仍按两窗 ⇒ 本批运行 : 2 批（昨天 1 + 今天 1）",
          "本批运行 : 2 批" in out,
          [ln for ln in out.splitlines() if "本批运行" in ln])
finally:
    lab.cleanup()

# ── ⑨b ★★ 「做种净额 443 / 新增做种 0」这个**正确读数**要能一眼读懂 ──
#   这正是生产 09-23 那封信的形状：净额满（一直有 443 部在做种）、
#   增量 0（今天没有新片转正）。改前只有一个数叫「新增做种 443」，
#   读起来像"今天新加了 443 部"—— 于是用户合理地质疑数据有错。
lab = Lab()
try:
    lab.tsv(day(0), batch("frds-top250-2024", ok=37, failed=3, newly_seeding=443,
                          first_seeding=0, still_skipped=0, backoff_hits=3))
    out = lab.run().stdout
    check("★★ 净额与增量**并排**出现（443 与 0 同时可读）",
          "做种净额 443" in out and "新增做种 0" in out,
          [ln for ln in out.splitlines() if "成功" in ln])
    check("★ 非2xx 与退避照旧不为它让路（非2xx 3 / 退避 3）",
          "非2xx 3" in out and "退避 3" in out,
          [ln for ln in out.splitlines() if "成功" in ln])
finally:
    lab.cleanup()

# ── ⑨c ★★ 缺 `first_seeding` 键 ⇒ **降级**：只印净额，不许把缺键印成 0 ──
#   这是老 TSV 行（09-23 之前发的）真实存在的样子。
#   ★ 与 ⑤ 段那条「键在不在 != 值等不等于 0」同一纪律：把"没有这个读数"
#     印成「新增做种 0」= **伪造读数**（`ERR-AI-03` / `A.12.1`）。
lab = Lab()
try:
    lab.tsv(day(0), batch("oldpack", ok=20, failed=0, newly_seeding=5,
                          still_skipped=0, backoff_hits=0))
    out = lab.run().stdout
    check("★ 有净额（做种净额 5）", "做种净额 5" in out,
          [ln for ln in out.splitlines() if "成功" in ln])
    check("★★ 缺键 ⇒ **不印**「新增做种」（绝不印成 0）",
          "新增做种" not in out,
          [ln for ln in out.splitlines() if "新增做种" in ln] or out[-400:])
finally:
    lab.cleanup()

print("\n" + ("✓ %d 条断言全过 —— 汇总数的是「本批运行」，台账与无待搜不进账" % _n
              if _ok == _n else
              "✗ 有对照没过（%d/%d）—— 别拿它当推前闸门" % (_ok, _n)))
sys.exit(0 if _ok == _n else 1)
