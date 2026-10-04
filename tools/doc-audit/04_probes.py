#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读探针：把文档声称的事实，从生产上真读一遍。

★★ 安全（改自原稿，原稿有泄露）：
   原稿 `SELECT id, url, name, status FROM indexer` 并**把结果整行打印** ——
   `cross-seed.db` 的 `indexer.url` 列里每条都带 `apikey=<Prowlarr 应用级 key>`，
   那个 key 能开 Prowlarr 上的所有 PT 站。本稿：
     ① 不选 `url` 列（判「有哪些站、状态如何」根本不需要那个值）；
     ② 所有输出过一遍 `redact()` —— 按**值的形状**兜底，不只按键名；
     ③ 落地到 out/probes.tsv 的也同样是脱敏后的文本。

用法：python 04_probes.py
输出：<脚本目录>/out/probes.tsv
"""
import re
import shutil
import sqlite3
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

TOOLS = Path(__file__).resolve().parent
OUT = TOOLS / "out"

NAS = "//iSunker-DS423"
COMPOSE_UNC = "%s/docker_ssd/reseed" % NAS   # ★ 2026-10-05（B2）：改名 + 收进 data/

# ---- 脱敏纪律（**两条都必须有，缺一条就是漏洞**）----
#   ① **不选可能含凭据的列**。判「哪个站、什么状态」靠 id / name / status 就够，
#      `url` 对判读零价值，而它每条都带 `apikey=<Prowlarr 应用级 key>`。
#      —— 能不取就不取，这是第一道，也是最有效的一道。
#   ② **出口统一过 redact()**。万一某个探针**必须**碰这类列（或异常信息里带出来），
#      在打印和落盘之前把它按**值的形状**抹掉。这是兜底，不是主防线。
#   判据是「只读 + 不回显凭据 + 只打聚合」，与 check-deploy-drift.py 里
#   qb-census / linkguard 那两条是同一个形状。
REDACT = re.compile(r"(apikey=)[^,&]+")
# 再按**值长什么样**兜一层（不只按键名叫什么）—— 键名匹配会漏掉嵌套在
# options / fields / 异常信息里的凭据。
REDACT_MORE = [
    (re.compile(r"(?i)(passkey|password|passwd|token|cookie)(\s*[=:]\s*)([^\s,;&\"']+)"),
     r"\1\2<redacted>"),
    (re.compile(r"\b[0-9a-fA-F]{32,64}\b"), "<hex32+>"),
    (re.compile(r"(?i)Bearer\s+\S+"), "Bearer <redacted>"),
]


def redact(text):
    s = REDACT.sub(r"\1<redacted>", str(text))
    for rx, rep in REDACT_MORE:
        s = rx.sub(rep, s)
    return s


# ---- 探针 ----
def qb_version():
    with urllib.request.urlopen("http://192.168.0.7:3060/api/v2/app/version", timeout=5) as r:
        return r.read().decode().strip()


def _ro(db):
    """只读打开生产库。

    ★ 踩过的坑：不能写 `sqlite3.connect("file:%s?mode=ro" % unc, uri=True)` ——
      UNC 的 `//iSunker-DS423` 会被当成 URI 的 authority，
      报 `invalid uri authority: iSunker-DS423`。要么 `file://` + 路径（多两个斜杠），
      要么退回「普通连接 + `PRAGMA query_only=1`」（**原始方案**）。
      两条路都真只读；URI 那条更硬（连接层就拒绝写 / 不走 WAL 恢复）。
    """
    p = db.replace("\\", "/")
    try:
        con = sqlite3.connect("file://" + p + "?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError:
        con = sqlite3.connect(p, timeout=5)
    con.execute("PRAGMA query_only=1")
    return con


def active_indexers():
    """等价于 docker inspect 的 TORZNAB_URLS（Windows 侧读不到容器）。

    ★ 不选 `url` 列 —— 它每条都带 apikey，对判读零价值（见文件头「脱敏纪律 ①」）。
    ★ 同时读 `retry_after`：`status` 是**残留标签**，限流窗口过去后不会被擦掉，
      只看它会把「曾经被限流」误判成「现在还在被限流」（SUMMARY §18.11.2）。
      真判据是 `retry_after` 有没有到点。
    """
    now_ms = int(time.time() * 1000)
    con = _ro(COMPOSE_UNC + "/data/cross-seed/cross-seed.db")
    try:
        rows = con.execute(
            "SELECT id, name, status, retry_after FROM indexer ORDER BY id").fetchall()
    finally:
        con.close()
    out = []
    for i, name, status, retry in rows:
        if retry and retry > now_ms:
            out.append((i, name, status, "退避中，剩 %d 分钟" % ((retry - now_ms) // 60000)))
        elif retry:
            out.append((i, name, status, "限流窗口已过"))
        else:
            out.append((i, name, status, "—"))
    return out


def stages():
    con = _ro(COMPOSE_UNC + "/data/drive-loop/state/hlink/state.db")
    try:
        return dict(con.execute("SELECT stage, COUNT(*) FROM movie GROUP BY stage").fetchall())
    finally:
        con.close()


def indexer_timestamps():
    """各索引器真的搜出去过几行 —— 判「闸门开了没」。"""
    con = _ro(COMPOSE_UNC + "/data/cross-seed/cross-seed.db")
    try:
        return con.execute(
            "SELECT i.name, COUNT(t.searchee_id) FROM indexer i "
            "LEFT JOIN timestamp t ON t.indexer_id = i.id GROUP BY i.id ORDER BY i.id"
        ).fetchall()
    finally:
        con.close()


def volume_usage():
    """磁盘水位 —— 三份文档里 /volume1 可用空间记了三个不同的值，这条是判据。
    只碰已知共享名；不存在的共享会抛错，由上层记下。"""
    out = {}
    for label, unc in (("/volume2 (docker_ssd)", NAS + "/docker_ssd"),
                       ("/volume1 (video)", NAS + "/video")):
        try:
            u = shutil.disk_usage(unc)
            out[label] = "总 %.2f TiB / 已用 %.2f TiB / 可用 %.2f GiB" % (
                u.total / 2**40, u.used / 2**40, u.free / 2**30)
        except Exception as e:
            out[label] = "%s: %s" % (type(e).__name__, redact(e))
    return out


# ---- 2026-09-20 新增三条：补 ENVIRONMENT.md 里**探针没覆盖**的读数 ----
#   起因：用户问「把 ENVIRONMENT.md 的读数也降级成派生数据」。查下来结论是
#   **不能那么做**（读数会动 + 没有第二处重复 ⇒ 固化进正文反而制造更隐蔽的漂，
#   见 INDEX-USAGE.md「读数不是派生数据」那条）。正确的处置是**扩探针**，
#   让「文档声称 vs 生产实读」这件事**当场可判**。
#   ★ 这三条补的正是原先的**覆盖缺口**（`A.19.1.1` 那三句的判据此前不存在）：
#       · 状态机总部数（文档写「605 部」）—— 原 `stages()` 只给 stage 分布，**求和不等于那个数**
#       · 农场条数（文档写「475/475」）
#       · qB 本项目种子数（文档写「509/509」）—— 原探针**没有**这条，且它必须按 category 收窄
#   ★ 判据形状与既有五条**逐字同族**：只读 / 不选凭据列 / 出口过 redact / 只打聚合。

def state_db_total():
    """状态机**总部数** —— 文档 `A.19.1.1` 写「`state.db` ✅ 605 部」。

    ★ 为什么单列一条、不复用 `stages()`：`stages()` 给的是**分布**，
      而文档声称的是**总数**。两者**不是同一个数** —— 我实测过：
      `SEEDING 517 + MATCHED 1 + PENDING 4 + UNMATCHED 83 = 605`，
      刚好对上；但若只把 `SEEDING` 当「部数」（那是最容易犯的读法），
      就会得到 517 而**看着完全正常**（同 `B.10` 第 14 条那个形状）。
      ⇒ 总数就得真 `COUNT(*)`，不许拿分布的某一格代替。
    """
    con = _ro(COMPOSE_UNC + "/data/drive-loop/state/hlink/state.db")
    try:
        (n,) = con.execute("SELECT COUNT(*) FROM movie").fetchone()
        return "%d 部（COUNT(*) FROM movie）" % n
    finally:
        con.close()


def farm_count():
    """农场条数 —— 文档 `A.19.1.1` 写「`reseed_farm` ✅ 475/475」。

    ★★ 口径必须**逐字**照 `summary/10` 那张实测表（不然会自己造一个假的"漂"）：
      「dataDir 直接子项合计 = **475**（**474 目录 + 1 个散文件**）」
      ⇒ 口径 = **`os.listdir` 的直接子项数**（目录 + 散文件**都算**），**不递归**。
      ★ 我第一版写成 `sum(1 for p in iterdir() if p.is_dir())` ⇒ 读回 **474**，
        差 1，**看起来像文档过时了** —— 其实是我的口径漏了那个散文件。
        这正是「数出来的数看着完全正常、但答的不是同一个问题」（同 `B.10` 第 14 条）。
      ⇒ 判据：**先对齐口径，再谈对不上**；口径错了，差多少都是假的。
    ★ 只数、**不列举名字**（资源名不入库，同 `qb-census-savepath.py` 的脱敏口径）。
    ★ 走 UNC 直读，**不 `cp` 副本**；SMB 挂不上时由 `_timed` 记成 error，不抛。
    """
    root = "%s/video/download/reseed/reseed_farm" % NAS
    try:
        items = list(Path(root).iterdir())
    except Exception as e:
        return "%s: %s" % (type(e).__name__, redact(e))
    dirs = sum(1 for p in items if p.is_dir())
    files = len(items) - dirs
    return "%d 条 = %d 目录 + %d 散文件（直接子项，不递归；口径同 summary/10）" % (
        len(items), dirs, files)


def qb_torrent_count():
    """qB(:3060) 里**本项目的**种子条数 —— 文档 `A.19.1.1` 写「qB :3060 的 **509/509** 全部落在新根下」。

    ★★ 口径陷阱（我第一版在这里栽了**第二次**，与 `farm_count()` 同一个形状）：
      第一版读 `torrents/info` 的**总条数** ⇒ 得 **2141**，看着像"文档漂了 4 倍"。
      实读构成后才发现：`:3060` 是**共享** qB，里面有两伙人 ——
        · `category == QBIT_CATEGORY`（本仓 `.env` 是 `reseed-singles`）→ **本项目的**
        · 其余（实测 **1211** 条，`category` 为空）→ **别人的**（IYUU 等），**从来不在 509 的范围里**
      ⇒ 「509/509」说的**不是 qB 总条数**，是**本项目自己那一类**（`summary/11` 那张表里
        509 是「待搜」列，605 是单片总数 —— 两个数**本来就不是一回事**）。
      ⇒ 判据：**先确定"这个数管的是哪个集合"，再数**。数错了集合，差多少倍都是假的。
      ★ 同 `A.4`：`:3060` 是独立栈但**与别人共用** ⇒ 凡读它，**必须按 category 收窄**。

    ★ 落点分布**不在这里打** —— 那是 `scripts/diag/qb-census-savepath.py` 的职分
      （它打路径前 N 段）。两条都打就又变成"同一口径两处声明"（这个仓库反复吃过的亏）。
    ★ 登录凭据从**本地 `.env`** 读，**绝不回显**（同 `qb-census-savepath.py` / `torznab-probe.py`）。
    ★★ `QBIT_PASSWORD` **允许为空** —— 本仓 `.env` 就是这么配的（第 14 行注释：
      「subnet_whitelist 时 `QBIT_PASSWORD` 可留空」，由 `QBIT_AUTH_MODE` 走子网白名单）。
      ⇒ 第一版把"空口令"判成"够不着"是**错的**：空口令**本身就是一种有效配置**，
        该让**服务端**回话（登录成功 / 被拒），而不是我们本地先替它拒绝。
        —— 判据：**"读不到"必须与"读到了但是 0"分开**，而"配置就是空"不属于"读不到"。
    """
    import json
    import urllib.parse
    env = {}
    for line in (Path(__file__).resolve().parents[2] / ".env").read_text(
            encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    user = env.get("QBIT_USER", "admin")
    pw = env.get("QBIT_PASSWORD", "")      # ★ 空是合法的（subnet_whitelist）
    cat = env.get("QBIT_CATEGORY", "reseed-singles")
    url = "http://192.168.0.7:3060"
    data = urllib.parse.urlencode({"username": user, "password": pw}).encode()
    req = urllib.request.Request(url + "/api/v2/auth/login", data=data)
    with urllib.request.urlopen(req, timeout=8) as r:
        if r.read().decode().strip() != "Ok.":
            return "登录被拒 ⇒ 够不着（凭据不对，不是「0 条」）"
        ck = r.headers.get("Set-Cookie", "").split(";")[0]
    req2 = urllib.request.Request(url + "/api/v2/torrents/info")
    req2.add_header("Cookie", ck)
    with urllib.request.urlopen(req2, timeout=20) as r:
        ts = json.loads(r.read().decode())
    mine = [t for t in ts if (t.get("category") or "") == cat]
    return "本项目 category `%s` **%d 条** / 该 qB 总 %d 条（差额 %d 是**别人的**，不在范围）" % (
        cat, len(mine), len(ts), len(ts) - len(mine))


def _timed(fn, secs=20):
    box = {}

    def run():
        try:
            box["v"] = fn()
        except Exception as e:                     # noqa: BLE001
            box["e"] = "%s: %s" % (type(e).__name__, e)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(secs)
    if t.is_alive():
        return None, "TIMEOUT after %ds（SMB 可能卡住，别重试太多次）" % secs
    if "e" in box:
        return None, box["e"]
    return box.get("v"), None


PROBES = [
    ("qB :3060 版本", qb_version),
    ("active 索引器（id/name/status/retry_after，无 url）", active_indexers),
    ("状态机 stage 分布", stages),
    ("索引器时间戳", indexer_timestamps),
    ("卷水位", volume_usage),
    # ★ 2026-09-20 补的三条（覆盖缺口，见各自 docstring）
    ("状态机总部数（A.19.1.1「605 部」的判据）", state_db_total),
    ("农场条数（A.19.1.1「475/475」的判据）", farm_count),
    ("qB 本项目种子数（A.19.1.1「509/509」的判据，按 category 收窄）", qb_torrent_count),
]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    lines = ["probe\tactual\terror"]
    for name, fn in PROBES:
        v, err = _timed(fn)
        if err:
            print("[!!] %s: %s" % (name, redact(err)))
            lines.append("%s\t\t%s" % (name, redact(err)))
        else:
            print("[ok] %s: %s" % (name, redact(v)))
            lines.append("%s\t%s\t" % (name, redact(v).replace("\n", " ")))
    (OUT / "probes.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n→ %s" % (OUT / "probes.tsv"))


if __name__ == "__main__":
    main()
