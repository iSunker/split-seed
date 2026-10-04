# -*- coding: utf-8 -*-
"""钉住索引器自检 `check_indexers()`（drive-loop.py）。

全离线：`S.read_crossseed_db` 与 `emit` 都打桩，不碰 NAS、不碰真库。

★ 为什么值得单开一个文件：这个自检守的是**两份分开维护的名单**对不上 ——
  cross-seed 按容器里的 `TORZNAB_URLS` 全站搜索，`--indexers` 只决定状态机把
  "搜过"记到**哪个站名下**。对不上时**不报错、只是数算错**：要么反复重搜
  （白烧站点额度），要么永远不重搜。2026-09-12 真栽过一次（HDtime 早就在
  Prowlarr 和 .env 里了，`run.sh` 的 `--indexers` 却只有两个站，224 部
  UNMATCHED 一部都没往新站上重搜）。
  ★ 所以它属于「跑在无人值守的循环里、坏了没人会知道」那一类 —— 正是最该钉的。

它钉的几件事（每一项都在下面有专门的节）：
  ① `_norm_indexer_name` 的括号归一 —— cross-seed 的站名带 caps 后缀
     （`NanyangPT (南洋)`），`--indexers` 是人手写的短名。不归一就会**每批误报**。
  ② 三个分支各自的 key / metrics / 文案（`missing` / `extra` / `unnamed`）。
  ③ ★ 修法文案不许再出现「两处都要改」—— 电脑端已退役，`--indexers` 现在
     只有 `<compose>/drive-loop/run.sh` 这一处。这条是那次文案修正的钉子。
  ④ 自检自己**绝不能把跑批拖垮**：没 `--db-path` 要静默跳过，读库炸了要吞。
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8")        # 见 tests/README.md：GBK 控制台会炸

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
DRIVE_LOOP = ROOT / "data" / "drive-loop" / "scripts" / "drive-loop.py"

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


def _load(path: pathlib.Path, name: str):
    """按 tests/README.md 的规矩：importlib 载**真的那份**，且**先登记进
    `sys.modules`** —— 否则被测文件里有 `@dataclass` 就炸，报一个毫不相干的
    `AttributeError: 'NoneType' object has no attribute '__dict__'`。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod                    # ★ 见上
    with contextlib.redirect_stdout(io.StringIO()), \
         contextlib.redirect_stderr(io.StringIO()):
        try:
            spec.loader.exec_module(mod)
        except SystemExit:
            pass
    return mod


D = _load(DRIVE_LOOP, "drive_loop_ci")
D.LOG.disabled = True                          # 自检会 LOG.warning，测试里别刷屏

# --------------------------------------------------------------------------- #
# 打桩：read_crossseed_db 给一份假的快照，emit 把事件收进列表
# --------------------------------------------------------------------------- #
class _Snap:
    """`CrossSeedSnapshot` 的替身 —— `check_indexers` 只读它的 `.indexers`。"""

    def __init__(self, indexers):
        self.indexers = list(indexers)


def _run(live, mine, *, db_path="x.db", boom=False):
    """跑一遍 `check_indexers`，返回它 emit 出去的事件列表。"""
    sent: list[dict] = []

    def _fake_emit(kind, title, body="", *, key=None, metrics=None):
        sent.append(dict(kind=kind, title=title, body=body, key=key, metrics=metrics or {}))
        return True

    def _fake_read(path):
        if boom:
            raise FileNotFoundError(f"没有这个库：{path}")
        return _Snap(live)

    D.S.read_crossseed_db = _fake_read
    D.emit = _fake_emit
    D.check_indexers(argparse.Namespace(db_path=db_path, indexers=mine))
    return sent


print("=== ① `_norm_indexer_name`：括号后缀 / 大小写 / 空白 ===")
N = D._norm_indexer_name
ck("★ 半角括号后缀被砍掉（cross-seed 的 caps 名 → 手写短名）", N("NanyangPT (南洋)") == "nanyangpt", N("NanyangPT (南洋)"))
ck("★ 全角括号同样处理（中文站名很常见）", N("BTSCHOOL（校内）") == "btschool", N("BTSCHOOL（校内）"))
ck("大小写归一", N("HDtime") == "hdtime", N("HDtime"))
ck("两端空白被剥掉", N("  HDFans  ") == "hdfans", repr(N("  HDFans  ")))
ck("没括号的原样（只降大小写）", N("HDFans") == "hdfans", N("HDFans"))
ck("括号里带空格也不残留", N("HDtime (HD 时空)") == "hdtime", N("HDtime (HD 时空)"))

print("\n=== ② ★ 归一化真的能挡住误报（这条不成立，下面全白测） ===")
_ = _run(["HDFans", "NanyangPT (南洋)"], "HDFans,NanyangPT")
ck("★ 带后缀的站名 == 手写短名 → 一致，零告警", _ == [], repr(_))
_ = _run(["HDtime", "HDFans", "NanyangPT (南洋)"], "HDtime,HDFans,NanyangPT")
ck("三个站也一致", _ == [], repr(_))

print("\n=== ③ missing：cross-seed 会搜，但 --indexers 没列 ===")
s = _run(["HDtime", "HDFans", "NanyangPT (南洋)"], "HDFans,NanyangPT")
ck("报了一个", len(s) == 1, repr(s))
ck("kind 是 alert（要人看见，不是记账）", s[0]["kind"] == "alert", s[0]["kind"])
ck("key 带了站名（冷却按站粒度）", s[0]["key"] == "indexer-missing:HDtime", s[0]["key"])
ck("metrics 里是缺的个数", s[0]["metrics"] == {"missing": 1}, repr(s[0]["metrics"]))
ck("★ 指名道姓说了是 HDtime", "HDtime" in s[0]["body"], s[0]["body"])
ck("★ 建议里给的是**带 caps 后缀的真名**（照抄就能用）",
   "HDtime,HDFans,NanyangPT (南洋)" in s[0]["body"], s[0]["body"])
ck("后果写清了是「漏记」→ 重复搜 / 永不重搜",
   "漏记" in s[0]["body"] and "重搜" in s[0]["body"], s[0]["body"])
ck("★ 指向 run.sh 那一处", "<compose>/drive-loop/run.sh" in s[0]["body"], s[0]["body"])
ck("★ 明说电脑端已退役（别再去找第二次）", "电脑端已退役" in s[0]["body"], s[0]["body"])
ck("★ 不许再出现「两处都要改」", "两处都要改" not in s[0]["body"], s[0]["body"])

s2 = _run(["HDtime", "BTSCHOOL", "HDFans"], "HDFans")
ck("★ 缺两个时 key 用逗号按 db 顺序连（一个 key 管一次告警）",
   s2[0]["key"] == "indexer-missing:HDtime,BTSCHOOL", s2[0]["key"])
ck("metrics 数得对", s2[0]["metrics"] == {"missing": 2}, repr(s2[0]["metrics"]))

print("\n=== ④ extra：--indexers 列了，但 cross-seed 根本不会搜 ===")
s = _run(["HDFans"], "HDFans,HDtime")
ck("报了一个", len(s) == 1, repr(s))
ck("kind 是 alert", s[0]["kind"] == "alert", s[0]["kind"])
ck("key 前缀是 extra", s[0]["key"] == "indexer-extra:hdtime", s[0]["key"])
ck("metrics 里是多出来的个数", s[0]["metrics"] == {"extra": 1}, repr(s[0]["metrics"]))
ck("标题说的是「多了」不是「漏了」", "多了" in s[0]["title"], s[0]["title"])
ck("后果写清了：被记成「搜过」→ 片子永远不会被搜",
   "搜过" in s[0]["body"] and "永远不会被搜" in s[0]["body"], s[0]["body"])
ck("★ 指向 run.sh 那一处", "<compose>/drive-loop/run.sh" in s[0]["body"], s[0]["body"])
ck("★ 明说电脑端已退役", "电脑端已退役" in s[0]["body"], s[0]["body"])
ck("★ 不许再出现「两处都要改」（本次修掉的陈旧文案）",
   "两处都要改" not in s[0]["body"], s[0]["body"])
ck("★ 但必须警告「别把没重建容器的情况删掉」",
   "容器没重建" in s[0]["body"] and "force-recreate" in s[0]["body"], s[0]["body"])

print("\n=== ⑤ unnamed：active 但拉不到名字（TORZNAB_URLS 里那个 #N） ===")
s = _run(["prowlarr#1", "HDFans"], "HDFans")
ck("报了一个", len(s) == 1, repr(s))
ck("kind 是 alert", s[0]["kind"] == "alert", s[0]["kind"])
ck("★ key 直接用那个标签（#N 就是 TORZNAB_URLS 里的 N）",
   s[0]["key"] == "indexer-unnamed:prowlarr#1", s[0]["key"])
ck("metrics 里带的是标签而不是个数",
   s[0]["metrics"] == {"indexer": "prowlarr#1"}, repr(s[0]["metrics"]))
ck("★ 正文点明「#N = /N/api，不是 Prowlarr 界面序号」（别找错站）",
   "/N/api" in s[0]["body"], s[0]["body"])
ck("给了解析命令", "docker inspect reseed-cross-seed" in s[0]["body"], s[0]["body"])
# ★★ 查证命令必须**自带脱敏**（2026-09-13）：`TORZNAB_URLS` 里每条 URL 各带一个
#    `apikey=`，值就是 Prowlarr 的应用级 key —— 而 Prowlarr 存着所有 PT 站 cookie。
#    原样 grep 出去 = 把凭据打进终端/聊天。这一格钉的是"命令本身就脱敏"，
#    而不是"我们自己记得别看"（靠人记得的那种防护，早就漏过了）。
ck("★★ 解析命令自带 sed 脱敏（不许把 apikey 原样打出来）",
   "sed -E" in s[0]["body"] and "<redacted>" in s[0]["body"], s[0]["body"])
ck("★ sed 必须咬住 apikey 的**值**（`(apikey=)[^,&]+`）",
   "(apikey=)[^,&]+" in s[0]["body"], s[0]["body"])
ck("★ 而且必须明说「别原样粘贴」——光有命令拦不住手快",
   "别把原样输出" in s[0]["body"], s[0]["body"])
ck("★ unnamed 不该被算成 missing（它压根没法比名字）",
   "missing" not in s[0]["key"], s[0]["key"])

print("\n=== ⑥ 三类一起出现：各自独立报，互不吞 ===")
s = _run(["prowlarr#1", "HDtime", "HDFans"], "HDFans,HDtime")
ck("★ 缺的「HDtime」不该报（它在 db 里，只是多了个 #1）",
   all("missing" not in e["key"] for e in s), repr([e["key"] for e in s]))
ck("★ 多的 HDtime 也不该报（db 里有它）",
   all("extra" not in e["key"] for e in s), repr([e["key"] for e in s]))
ck("只有 unnamed 那条", len(s) == 1 and "unnamed" in s[0]["key"],
   repr([e["key"] for e in s]))

print("\n=== ⑦ ★ 自检自己绝不能拖垮跑批 ===")
sent = []
D.emit = lambda *a, **k: sent.append(a) or True
D.S.read_crossseed_db = lambda p: (_ for _ in ()).throw(AssertionError("不该被调用"))
D.check_indexers(argparse.Namespace(db_path="", indexers="HDFans"))
ck("★ 没 --db-path → 静默跳过，绝不读库", sent == [], repr(sent))

s = _run(["HDFans"], "HDFans", boom=True)
ck("★ 读库炸了 → 吞掉，不抛（自检失败不该挡住正事）", s == [], repr(s))

# 还原（这个进程里别的节可能还要用真的）
D.LOG.disabled = False

print(f"\n{'=' * 60}")
print(f"断言 {_ok + _bad} 条：{_ok} 过 / {_bad} 失败")
sys.exit(0 if _bad == 0 else 1)
