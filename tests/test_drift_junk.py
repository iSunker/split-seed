# -*- coding: utf-8 -*-
r"""回归：漂移哨兵对 `.env` 备份/变体的分类 —— **别再靠猜文件名**。

钉住的现场（2026-09-13）
-----------------------
`check-deploy-drift.py` 的 `KNOWN_NAS` 里那条杂物规则原本写作：

    (r"^\\.env\\.bak\\..*", "杂物·.env 备份")

`.` 后面那个 `\\..*` 要求**必须**跟一个点 + 后缀，于是**裸名 `.env.bak`**
（不带时间戳）**不匹配** —— 而它恰恰是 `rm-staging.sh` 那条窄口
（8 个凭据副本只有 7 个匹配）里的第 8 个。
不匹配的后果：它掉进 `unknown` ⇒ 哨兵**误报**「NAS 上有没登记过的文件」。

★ 同一类坑（"保证取决于我们猜没猜对文件名"）当天咬了**三处**：
  `rm-staging.sh`（删除口径）、`.gitignore`（忽略口径）、本哨兵（分类口径）。
  所以这里把**命名变体整个钉住**，而不是只钉当时那一例。

判据：`classify()` 是**先匹配先赢**（`re.search` 逐条扫 `KNOWN_NAS`）。
  · `.env`           —— 本体，必须落在上一条「真实凭据」，**不许**被杂物吃掉；
  · `.env.example`   —— 模板，压根不部署，**不许**归进杂物；
  · 其余 `.env.` / `.env_` 前缀的，**都**该归进杂物。

★★ 2026-10-05（#37 现场）：上面那条「其余 `.env.` / `.env_`」原先**只锚根**
  （`^\.env[._]`），于是 `子目录/.env.<后缀>`（实测：`.fix-backup/<ts>/.env.before_torznab`，
  **一份含真凭据的副本**）**匹配不到** ⇒ 既没被当凭据报（ALERT 不响）、又掉进"未知"。
  ⇒ 已放宽成 `(^|/)\.env[._]`（**任何一层**），并在此补 13/14/15 三条把
     「子目录 env 副本算杂物且带告警」「子目录 `.env.example` 仍不归杂物」钉住。
  同批还登记了 #37 的另两类**过程残留**（`.deploy-backup/`、`.fix-backup/`、`*.bak-<后缀>`）——
  它们此前全掉进"未知"（哨兵报 9 个），见 16–19。
"""
import importlib.util
import pathlib
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "scripts" / "diag" / "check-deploy-drift.py"


def _load():
    name = "check_deploy_drift_mod"
    spec = importlib.util.spec_from_file_location(name, SRC)
    mod = importlib.util.module_from_spec(spec)
    # ★ 必须先 `sys.modules[name] = mod` 再 exec_module —— 见 tests/README
    #   「importlib 载进来的模块…」（带上这个文件后照样得这么写）。
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


M = _load()


def kind_of(rel: str) -> str:
    """→ 'managed' / 'known' / 'clutter'（known 里那类"杂物"）/ 'unknown'。"""
    k, rule = M.classify(rel, set(), M.KNOWN_NAS)
    if k == "managed":
        return "managed"
    if k == "known":
        return "clutter" if (rule and M.CLUTTER.search(rule[1])) else "known"
    return "unknown"


def rule_of(rel: str):
    """命中那条规则本身 —— 用来判严重度标记。"""
    return M.classify(rel, set(), M.KNOWN_NAS)[1]


fails = []


def check(n, label, rel, want):
    got = kind_of(rel)
    ok = got == want
    print("[%d] %-28s %-40s: %s" % (n, label, rel, "PASS" if ok else "FAIL"))
    if not ok:
        print("      want=%s  got=%s" % (want, got))
        fails.append(n)


print("== 漂移哨兵：`.env*` 分类（先匹配先赢）==")

# 1) 本体：必须仍是「真实凭据」，不能被放宽后的杂物规则吃掉
check(1, "本体凭据不被杂物吃", ".env", "known")

# 2) 模板：不许归进杂物（新版规则里的负向前瞻 `(?!example$)` 钉的就是这格）
check(2, "模板不归杂物", ".env.example", "unknown")

# 3) ★ 现场那一例：裸名 `.env.bak`（无时间戳）—— 旧规则漏的就是它
check(3, "裸名备份", ".env.bak", "clutter")

# 4) 旧规则**唯一**认得的那一形，放宽后当然还得认得
check(4, "带时间戳备份", ".env.bak.20260911-204109", "clutter")

# 5) 下划线变体（`.gitignore` 那条窄口实测漏过它）
check(5, "下划线变体", ".env_bak", "clutter")

# 6) 另一个常见写法
check(6, "backup 变体", ".env.backup", "clutter")

# 7) 误伤检查：不相关的东西不该被判成杂物
check(7, "无关文件不误伤", "hlink/.reseed_farm.manifest.tsv", "known")

print()
print("== 严重度：`.env*` 备份是「形似凭据泄漏」，`__pycache__` 不是 ==")


def check_alert(n, label, rel, want):
    got = M.is_alert(rule_of(rel))
    ok = got is want
    print("[%d] %-28s %-40s: %s" % (n, label, rel, "PASS" if ok else "FAIL"))
    if not ok:
        print("      want is_alert=%s  got=%s" % (want, got))
        fails.append(n)


# 8) 备份必须带 ALERT —— 否则哨兵只把它混进「杂物 N 个」，真信号被噪音吞掉
check_alert(8, "备份带告警标记", ".env.bak", True)
check_alert(9, "时间戳备份也带", ".env.bak.20260911-204109", True)
check_alert(10, "下划线变体也带", ".env_bak", True)

# 11) `__pycache__` **不许**带 —— 它是字节码，不是凭据。带上了就等于天天喊狼来了
check_alert(11, "字节码不带告警", "drive-loop/scripts/__pycache__/x.cpython-312.pyc", False)

# ★★ 2026-10-05（#37 现场）：`.env` 副本**在子目录里**也算凭据。
#   旧规则 `^\.env[._]` 只锚根 ⇒ `.fix-backup/<ts>/.env.before_torznab`
#   既不被当凭据报、又掉进"未知"。这条把「凭据在子树里」钉住。
check(13, "子目录 env 副本算杂物", ".fix-backup/b6-20261005-002837/.env.before_torznab", "clutter")
check_alert(14, "子目录 env 副本带告警", ".fix-backup/b6-20261005-002837/.env.before_torznab", True)

# 15) 子目录里的 `.env.example` 仍**不许**归杂物（负向前瞻要跨目录仍然成立）
check(15, "子目录模板不归杂物", "data/x/.env.example", "unknown")

# ★★ 2026-10-05（#37）：部署/修复备份目录 → 杂物（该清，但先登记，别误报未知）
check(16, ".deploy-backup 归杂物", ".deploy-backup/20261004-193950/compose.yaml", "clutter")
check(17, ".fix-backup 归杂物", ".fix-backup/b6-20261005-002837/p.sh.legacy", "clutter")

# ★★ 2026-10-05（#37）：`*.bak-<后缀>` 备份（后缀是时间戳或 git sha）→ 杂物
check(18, "bak-时间戳 归杂物", "build-farm.sh.bak-2026-10-05", "clutter")
check(19, "bak-shasum 归杂物", "compose.yaml.bak-cb2caeb", "clutter")

# 20) 反向：`.env.bak-*` 仍优先撞 ALERT（凭据优先），不是落到普通 bak 规则
check_alert(20, "env bak- 仍带告警", ".env.bak-2026-10-05", True)

# 12) ★ 反向结构检查：ALERT 只能打在「杂物」上。
#     打在某条非杂物规则上，那段 if alert_items 永远不会触发 ——
#     一条"看着像装好了告警、其实从不响"的路，正是本文件 2–3 行那段要防的形状。
ALERT_NOT_CLUTTER = [(r[0], r[1]) for r in M.KNOWN_NAS
                     if M.is_alert(r) and not M.CLUTTER.match(r[1])]
ok = len(ALERT_NOT_CLUTTER) == 0
print("[%d] %-28s %-40s: %s" % (12, "ALERT 只打在杂物上", "KNOWN_NAS 全表",
                                "PASS" if ok else "FAIL"))
if not ok:
    for rx, why in ALERT_NOT_CLUTTER:
        print("      ★ ALERT 打在非杂物规则上，永不触发: %s  %s" % (rx, why))
    fails.append(12)

print()
if fails:
    print("FAIL：第 %s 项不过（测试自己也可能错，先看 want/got）" % fails)
    sys.exit(1)
print("ok：20/20 通过")
sys.exit(0)
