#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手动复现 cross-seed 对某个 PT 站发的那一次 Torznab 查询（**只读**）。

用途
----
SUMMARY §20.9.5 的退出条件里，有一格日志**分不开**：
「HDtime 完全没货」与「HDtime 有货但只有单集」在三处读数里**一模一样**
（`searched` 有、`matched` 空、`found` 0），而**处置相反**。
⇒ 那一格只能**手动搜一次**才能定，本脚本就是那次手搜。

★ 为什么走 Torznab、不走去站点网页搜：要判的是「**cross-seed 看到的候选里有没有季包**」，
  所以必须复现同一条路、同一个 query（口径不一样，答案没法比）。
★ `q` 必须**逐字节抄** verbose 里那行
  `Querying HDtime at … with { t: 'tvsearch', q: '…' }` 的 `q`，别自己拼季包名。

为什么读 **NAS 的 `.env`**
--------------------------
本地 `.env` 是**开发存根**（占位符），`TORZNAB_URLS` 里没有真的站点 URL。
生产那份在 NAS 上，只读直读 UNC。

★ 这个脚本能在 Windows 上跑，靠的是一条容易漏掉的规则（`.env.example` 里写着）：
  `TORZNAB_URLS` 里写的是**容器名** `prowlarr`（那是给 cross-seed 容器用的），
  而 **9696 已发布到宿主机**、且 apikey 是 Prowlarr **应用层**的（`/N/api` 不校验来源 IP）
  ⇒ 把 host 换成 **NAS 宿主机 IP** 即可。
  **这不是「换一条路」，是同一条 Torznab 路的宿主机入口。**
  ★ 2026-09-13 曾据此判定「探针从 Windows 跑不了」—— **那是错的**，就是漏了这条规则。

★ 路径一律用**正斜杠**：实测同一台机器上 `\\\\NAS\\share\\…` 这种反斜杠 UNC 在
  Python 里会 `isfile → False`，而 `//NAS/share/…` 正常。别改成反斜杠。

安全
----
* **绝不打印 URL**（URL 里带 apikey）。只打 host:port、状态码、条数、标题。
* **只读**：读 NAS `.env` 一次，发一次查询，不写任何东西。
* `--dump-attrs` / `--raw` 的输出**过一遍 `redact()`** —— ★ 因为 `--raw` 打的是
  原始响应，里面有 `<link>` / `<guid>`，**很多站把 passkey 直接编进下载 URL**
  （`…/download.php?id=…&passkey=<32位>`）。★ 只打**前 4000 字符**，
  且按「值的形状」抹掉凭据（同 `scan-secrets.py` / `04_probes.py` 的出口口径）。
* 站点退避期间会拿到 **429** —— 那时读数无意义（Prowlarr 本地就拒了），**别下任何结论**。
  退避到什么时候，看 NAS 的 `drive-loop/scripts/drive-loop.log` 里
  「索引器 <站> 要等到 …」那行。

用法
----
    python scripts/diag/torznab-probe.py "<原样 q>" [season] [--id N] [--env PATH]
    python scripts/diag/torznab-probe.py "<剧名>" 1 --dump-attrs      # ★ 季界侦察
    python scripts/diag/torznab-probe.py "<剧名>" 1 --raw             # ★ 原始响应头 4000 字符

    --id   TORZNAB_URLS 里 path 段的编号：1=HDtime 2=HDFans 3=BTSCHOOL 4=NanyangPT
           （默认 1）
    --env  生产 .env 的路径（默认走 NAS 的 UNC；给本地路径便于离线核对）
    --timeout N
           单次请求超时秒数（默认 **30**）。★ 退出码 **4** = 超时（**未验**，见下）。
    --any   ★★ **自动挑一个"现在没被退避"的站** —— 按 1/2/4/5 的顺序试，
           遇到被禁的就跳过。★ 为什么需要它：**退避时跑出来的读数是假的**
           （`ERR-SVC-12`），而人手记不住哪个站现在能用。
           ★★★ **它要两只时钟都说"能"才挑**（2026-09-21 补）：
           Prowlarr 的 `disabledTill`（A）+ cross-seed 的 `retry_after`（B）。
           ★ **只读 A 是假绿** —— 实测 A 说「窗口已过」而 B 说「还剩 7h」
           （HDFans），接着就 429。两个取**晚**的那个。
    --force
           ★ **跳过退避闸，强行发**（★ 只在你**确实知道**自己在干什么时用；
           它会让下面那个"退避期不发请求"的保护失效）。
    --dump-attrs
           ★★ 逐条把 **item 的全文**摆出来（title 之后的 link/guid/pubDate/enclosure/
           `torznab:attr` 全列）—— 用来回答「**响应里有没有可解析的季字段**」。
           为什么必须有这个开关：本脚本原先**只抠 `<title>`** ⇒ 后面那些元素**整段被丢掉**，
           于是「响应里没有季字段」这句话**当时压根没有判据**（拿不到 ≠ 没有，`B.10`）。
    --raw  ★ 原始响应体的**前 4000 字符**（脱敏后）。`--dump-attrs` 只给已知元素，
           这个给「有没有别的写法」（如 `torznab:attr` 之外的自定义命名空间）。

★★★ 退出码（**这份清单本身就是判据的一部分 —— 不同的码含义不同，别混**）

| 码 | 含义 | 能下结论吗 |
|---|---|---|
| **0** | 拿到了响应体 | ★ **能** |
| **1** | HTTP 错（429/401/5xx 等） | ★ **不能**（429 = 退避，读数无意义）|
| **2** | 读不到 `.env` | ★ **不能**（"读不到" ≠ "没有"）|
| **3** | ★★ **退避闸拦下了**（本地就拒了，**没出网**） | ★ **不能**（但也没浪费额度）—— ★ **两只时钟都说"能"才放行** |
| **4** | ★★ **超时** —— 请求发出去了、**没能等到答复** | ★★★ **不能**。★ 它与 429 **不是一回事**：<br>429 = 我知道自己被限流；**超时 = 我不知道发生了什么**。<br>★★ `B.10`：超时**不是**"响应里没有季字段"，是**这次没读到响应**。|
"""
from __future__ import annotations

import os
import pathlib
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

NAS_ENV = "//iSunker-DS423/docker_ssd/reseed/.env"
NAS_IP = "192.168.0.7"
#: ★ cross-seed 的库（UNC）。**只读**、`mode=ro`、且**绝不取 `url`/`apikey` 两列**。
NAS_CROSSSEED_DB = ("//iSunker-DS423/docker_ssd/reseed"
                    "/cross-seed/cross-seed.db")
#: 墙钟（`retry_after`）与本地时钟差多少秒算「同一个窗口」——见 `disarm_at`。
CLOCK_SKEW_SEC = 120

# ---- 脱敏（出口口径，与 scan-secrets.py / 04_probes.py 同一形状）----
# ★ 为什么 `--raw` 必须过它：原始响应里的 `<link>` / `<guid>` 常把 passkey 编进去。
# ★ 两条都要，缺一条就是漏洞：① 按**值的形状**抹（不只按键名）；
#   ② 已知的凭据参数名再抹一道（形状层对短值不生效）。
REDACT_SHAPE = re.compile(r"\b[0-9a-fA-F]{20,}\b")
REDACT_NAME = re.compile(
    r"(?i)((?:api_?key|passkey|torrent_pass|authkey|token|cookie|password)"
    r"[\s\"']*(?:[=:]|%3D)[\s\"']*)([^\s\"'&<>,;]+)")


def redact(text):
    """★ 出口统一脱敏 —— 打印与落盘都只走这一个函数。"""
    s = REDACT_NAME.sub(r"\1<redacted>", str(text))
    s = REDACT_SHAPE.sub("<hex20+>", s)
    return s


def items_of(body):
    """把响应体切成 item 块（**原始片段**，不是只有 title）。

    ★★ 这是一次真缺口修补：原来那版把每个 item 切到 `</title>` 就丢，
      于是 item 后面的 `link` / `guid` / `enclosure` / `torznab:attr` **全都看不见**
      ⇒ 「响应里有没有季字段」当时**没有判据**。
    ★ 用 `</item>` 收尾，且**不假设** item 里有 title（有的站 title 为空）。
    """
    out = []
    for chunk in body.split("<item>")[1:]:
        out.append(chunk.split("</item>", 1)[0])
    return out


def title_of(item_raw):
    """从原始 item 片段里抠 title（抠不到就返回空串，不编）。"""
    if "<title>" not in item_raw:
        return ""
    return item_raw.split("<title>", 1)[1].split("</title>", 1)[0].strip()


def attrs_of(item_raw):
    """★ 把 item 里**所有** `torznab:attr` 的 (name, value) 列出来。

    ★★ 这是 A 的核心判据：**判「没有」之前，先把整个 item 的 attr 列全看一眼** ——
      因为「站点不给」与「给了但名字不叫 season」（如 `rageid` / `tvdbid`）
      在只搜 `name="season"` 时**读数一模一样**（`B.10`：未验 ≠ 无）。
    """
    out = []
    for m in re.finditer(r"<torznab:attr\s+([^/>]*)/?>", item_raw):
        seg = m.group(1)
        n = re.search(r'name\s*=\s*"([^"]*)"', seg)
        v = re.search(r'value\s*=\s*"([^"]*)"', seg)
        out.append(((n.group(1) if n else "?"), (v.group(1) if v else "")))
    return out


def torznab_idx_of(url: str) -> str:
    """从 TORZNAB_URLS 的一条 URL 里取出 path 段编号（`…/2/api?…` ⇒ `"2"`）。"""
    p = urllib.parse.urlsplit(url)
    return p.path.strip("/").split("/")[0]


def torznab_all(env_path: str):
    """列出 TORZNAB_URLS 里**全部**条目的 `编号 → URL`。★ 返回值**绝不打印**。"""
    if not os.path.isfile(env_path):
        raise SystemExit("[!!] .env 不可达（SMB 断了？或路径给错）：%s" % env_path)
    for line in open(env_path, encoding="utf-8", errors="replace"):
        if line.startswith("TORZNAB_URLS="):
            out = {}
            for u in line.split("=", 1)[1].strip().split(","):
                u = u.strip()
                if u:
                    out[torznab_idx_of(u)] = u
            if out:
                return out
    raise SystemExit("[!!] .env 的 TORZNAB_URLS 里没有任何条目")


def _parse_iso(ts, now):
    """ISO8601（`…Z` 或带 offset）→ epoch 秒；解析不了返回 None。"""
    import datetime as _dt
    if not isinstance(ts, str):
        return None
    try:
        when = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:                                  # noqa: BLE001
        return None
    if when.tzinfo is None:                            # 没带时区 ⇒ 按 UTC 读（Prowlarr 给的是 Z）
        when = when.replace(tzinfo=_dt.timezone.utc)
    return when.timestamp()


def _with_cs(cs, i, v):
    """把 cross-seed 那一侧的读数并进一格（**没给就原样返回**，不编一个 0 出来）。"""
    out = dict(i)
    if cs is not None:
        out["cs"] = v
    return out


def disarm_at(indexer_ids, disabled_map, now, force=False, cs_map=None):
    """★★★ 退避闸（**纯函数**，可离线测）—— 挑出"现在能问"的站。

    ★★ 为什么必须在**探针里**做、而不是靠人先查一遍
    --------------------------------------------------
    退避时跑出来的读数是**假的**（`ERR-SVC-12`）——而 2026-09-21 实测：
    我**手动**查过两只时钟、确认 HDFans "窗口已过"，**下一条命令它还是 429**。
    ⇒ ★ 判据：**"这个站现在能不能问"必须由程序在读的那一刻算**，
      人手查一次的结果**在两次调用之间就会过期**。

    ★★ 两只时钟，缺一只就是假绿（2026-09-21 实测补上）
    --------------------------------------------------
    | 时钟 | 谁立的闸 | 存在哪 | 管什么 |
    |---|---|---|---|
    | **A** `disabledTill` | Prowlarr | `/api/v1/indexerstatus`（本地，不耗额度）| Prowlarr 何时肯**转发** |
    | **B** `retry_after` | cross-seed | `cross-seed.db` 的 `indexer` 表（本地，不耗额度）| cross-seed 何时肯**再问** |

    ★★★ **只读 A 是这根闸门上一轮的真漏洞**：2026-09-21 16:0x 实测
      **A 说 HDFans 的窗口已过（数组里没有它）而 B 的 `retry_after` 还没到** ——
      两个数据库各说各话，**取晚的那个**才是「现在能不能问」。
      ⇒ ★ 这条与 `prowlarr-indexerstatus.py` 末尾那句
        「两个时钟不一致时**以更长的为准**」是同一个判据，**现在它进了代码**。

    ★ 数据从哪来：两个都是**本地**接口/库（查它**不碰站点、不耗额度**）。
      —— 与 `scripts/diag/prowlarr-indexerstatus.py`（A）/
      `scripts/diag/check-indexer-timestamps.py`（B）同一口径（`ERR-SVC-17`）。

    参数
      `indexer_ids`  候选编号（字符串）
      `disabled_map` A 的读数：`{编号: disabledTill 或 None}`；`None`（整体）= A 读不到
      `cs_map`       B 的读数：`{编号: {"retry_after": int 毫秒 或 None}}`；
                     ★ **不传**（`None`）= 调用方**没查** B（向后兼容，行为同以前）
      `force`        跳过两只时钟

    返回 `(可用编号列表, 被禁的 [(编号, 原因)])`。
    ★★ 解析不出来的**一律按"被禁"处理** —— **判不出来时不许放行**
      （同 `rotate-crossseed-key.py` 的容器闸：unknown 不放行）。
    """
    ok, blocked = [], []
    for i in indexer_ids:
        if force:
            ok.append(i)
            continue

        # ---- 时钟 A：Prowlarr 本地禁用 ----
        if disabled_map is None:
            blocked.append((i, "Prowlarr 状态**读不到** ⇒ ★ 判不出，不放行（读不到 ≠ 没被禁）"))
            continue
        dt = disabled_map.get(i)
        if dt is not None:
            if not isinstance(dt, str):
                blocked.append((i, "disabledTill 是意外类型（%r）⇒ ★ 判不出，不放行" % (dt,)))
                continue
            a_when = _parse_iso(dt, now)
            if a_when is None:
                blocked.append((i, "disabledTill 解析不了（%r）⇒ ★ 判不出，不放行" % (dt,)))
                continue
            if a_when > now:
                remain = int(a_when - now)
                blocked.append((i, "Prowlarr 禁用至 %s（还剩 %d 分 %d 秒）"
                                % (dt, remain // 60, remain % 60)))
                continue

        # ---- 时钟 B：cross-seed 自己的退避（★ A 说"过"时它可能还说"没到"）----
        if cs_map is not None:
            row = cs_map.get(i)
            if row is None:
                # 两种情况，**读数一模一样，本函数分不开** ⇒ 一律不放行（保守方向）。
                # ① 这个站**从没搜出去过**（失败不记行 ⇒ 库里没有行）
                # ② 它不在 cross-seed 的 `indexer` 表里（`.env` 加了站但没重建容器）
                # ★ 这里**不猜**是哪种 —— 猜错就是发一个必然 429 的请求。
                blocked.append((i, "cross-seed 库里**没有这个站的记录** ⇒ ★ 分不清"
                                   "「从没搜过」还是「没登记」，不放行"))
                continue
            b_ms = row.get("retry_after")
            if b_ms is not None:
                if not isinstance(b_ms, (int, float)):
                    blocked.append((i, "cross-seed retry_after 是意外类型（%r）⇒ ★ 判不出，不放行"
                                    % (b_ms,)))
                    continue
                b_when = b_ms / 1000.0
                if abs(b_when - now) > CLOCK_SKEW_SEC and b_when > now:
                    remain = int(b_when - now)
                    blocked.append((i, "cross-seed retry_after 未到 %s（还剩 %d 分 %d 秒）"
                                    % (_local(b_when), remain // 60, remain % 60)))
                    continue
        ok.append(i)
    return ok, blocked


def _local(epoch):
    """epoch 秒 → 本地时间串（**读数**用，不是判据）。"""
    import datetime as _dt
    return _dt.datetime.fromtimestamp(epoch).strftime("%m-%d %H:%M:%S")


def fetch_disabled(env_path, idx, timeout=8):
    """★ 读 Prowlarr 本地的 `/api/v1/indexerstatus`（**不碰站点、不耗额度**）。

    返回 `(idx → disabledTill 或 None, 错误串或 None)`。
    ★ 只取 `indexerId` 与 `disabledTill` 两格（**不取 fields** —— 那里面有 cookie/passkey）。
    ★ 读不到时返回错误串 —— **绝不把它读成"没被禁"**（`B.10`：读不到 ≠ 没有）。
    """
    import json
    u = torznab_url(env_path, idx)                      # 拿任一条拿到 host/apikey
    p = urllib.parse.urlsplit(u)
    q = urllib.parse.parse_qs(p.query)
    key = (q.get("apikey") or [""])[0]
    if not key:
        return None, "TORZNAB_URLS 里这条没有 apikey 参数 ⇒ 查不了状态"
    base = "%s://%s:%d" % (p.scheme, NAS_IP, p.port or 80)
    req = urllib.request.Request(base + "/api/v1/indexerstatus",
                                 headers={"X-Api-Key": key,
                                          "User-Agent": "reseed-toolkit/probe"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:                              # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)
    if not isinstance(data, list):
        return None, "响应不是数组 ⇒ 判不出（不猜）"
    out = {}
    for row in data:
        if isinstance(row, dict) and "indexerId" in row:
            out[str(row["indexerId"])] = row.get("disabledTill")
    return out, None


def fetch_cs_retry(crossseed_db, timeout=8):
    """★ 读 cross-seed **本地**库的第二个时钟：`indexer` 表的 `retry_after`。

    返回 `(编号 → {"retry_after": 毫秒 或 None}, 错误串或 None)`。
    ★★ **只取 `id` / `status` / `retry_after` 三列** —— `url` 与 `apikey` **一律不取**
      （那两列里有全站共用的密钥；见 `check-indexer-timestamps.py` 同一纪律）。
    ★ 读不到时返回错误串 —— **绝不读成"没被拦"**（`B.10`）。
    ★ 打开方式必须是 `file:////<host>/<path>?mode=ro`：写成 `file://<host>/…`
      会被当成 URI 的 authority（2026-09-12 踩过）；也不要 `cp` 到本地
      （会丢掉 WAL 里还没 checkpoint 的改动，读到偏旧的快照）。
    """
    import sqlite3
    p = pathlib.Path(crossseed_db)
    if not p.is_file():
        return None, "cross-seed 库不可达（SMB 断了？或路径给错）"
    uri = "file:////" + p.as_posix().lstrip("/") + "?mode=ro"
    try:
        # ★ `timeout` 给短一点：这是个**辅助**时钟，它不通不该把主流程拖死。
        con = sqlite3.connect(uri, uri=True, timeout=timeout)
        with con:
            rows = list(con.execute(
                'SELECT id, status, retry_after FROM "indexer"'))
        con.close()
    except Exception as e:                              # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)
    out = {}
    for iid, _st, ra in rows:
        out[str(iid)] = {"retry_after": ra}
    return out, None


def torznab_url(env_path: str, idx: str) -> str:
    """取 TORZNAB_URLS 里 path 段为 `/<idx>/api` 的那条。返回值**绝不落盘/打印**。"""
    want = "/%s/api" % idx
    if not os.path.isfile(env_path):
        raise SystemExit("[!!] .env 不可达（SMB 断了？或路径给错）：%s" % env_path)
    for line in open(env_path, encoding="utf-8", errors="replace"):
        if line.startswith("TORZNAB_URLS="):
            for u in line.split("=", 1)[1].strip().split(","):
                if u.split("?", 1)[0].endswith(want):
                    return u.strip()
    raise SystemExit("[!!] .env 的 TORZNAB_URLS 里没有 path 为 %s 的条目" % want)


def to_host_entry(url: str, ip: str):
    """把 host 换成宿主机 IP。只回报 (原 host, 新 netloc)，**不回报 URL 本身**。"""
    p = urllib.parse.urlsplit(url)
    netloc = "%s:%d" % (ip, p.port or 80)
    new = urllib.parse.urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))
    return new, (p.hostname or "?"), netloc


def main(argv):
    env_path, idx, rest = NAS_ENV, "1", []
    dump_attrs = raw = any_idx = force = False
    timeout = 30
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--id" and i + 1 < len(argv):
            idx = argv[i + 1]; i += 2; continue
        if a == "--env" and i + 1 < len(argv):
            env_path = argv[i + 1]; i += 2; continue
        if a == "--timeout" and i + 1 < len(argv):
            try:
                timeout = int(argv[i + 1])
            except ValueError:
                raise SystemExit("[!!] --timeout 要一个整数秒")
            i += 2; continue
        if a == "--dump-attrs":
            dump_attrs = True; i += 1; continue
        if a == "--raw":
            raw = True; i += 1; continue
        if a == "--any":
            any_idx = True; i += 1; continue
        if a == "--force":
            force = True; i += 1; continue
        rest.append(a); i += 1

    if not rest:
        raise SystemExit(__doc__)
    q = rest[0]
    season = rest[1] if len(rest) > 1 else None

    # ---- ★★★ 退避闸（在**读的那一刻**算，不靠人手先查一遍）----
    # ★ 顺序：1/2/4/5 —— ★ 刻意**不含 3**（BTSCHOOL 在 `.env` 里？不确定就一并列出）
    CAND = ["1", "2", "4", "5", "3"]
    disabled, derr = fetch_disabled(env_path, idx, timeout=8)
    # ★★ 第二个时钟（cross-seed 的 `retry_after`）。★ 只在真的要用闸时才读它
    #   （`--force` 时无需读；也避免为一个纯离线用法去碰 NAS 的一句多余 IO）。
    cs_map = cs_err = None
    if not force:
        cs_map, cs_err = fetch_cs_retry(NAS_CROSSSEED_DB, timeout=8)
    if derr:
        print("★ 退避闸读不到 Prowlarr 本地状态：%s" % derr)
        print("  ★ 判据：**读不到 ≠ 没被禁**（`B.10`）⇒ ★ 不自动挑站。")
        if any_idx:
            print("  ⇒ `--any` **本轮不生效**（挑不出可信的候选）。若你确认现在能跑，")
            print("     用 `--id <编号>` 明确指定，或加 `--force`（★ 你自己担这个判断）。")
            return 3
    else:
        if cs_err:
            # ★ A 通了、B 没通 ⇒ **不许当成 A 单独说了算**（那正是上一轮的假绿）。
            print("★ 退避闸的**第二只时钟**（cross-seed `retry_after`）读不到：%s" % cs_err)
            print("  ★★ 判据：**只读 A 是假绿** —— 实测 A 说「窗口已过」而 B 说「还没到」")
            print("     （2026-09-21：HDFans）。⇒ ★ 读不到 B 时**不自动挑站**（`B.10`）。")
            if any_idx:
                print("  ⇒ `--any` **本轮不生效**。要跑就用 `--id <编号>` 明确指定，")
                print("     ★ 但**先用 `python scripts/diag/check-indexer-timestamps.py` 看一眼 B**。")
                return 3
        if any_idx:
            ok, blocked = disarm_at(CAND, disabled, time.time(), force=force,
                                    cs_map=None if cs_err else cs_map)
            if blocked:
                print("★ 退避闸拦下 %d 个站：" % len(blocked))
                for iid, why in blocked:
                    print("    id=%s  %s" % (iid, why))
            if not ok:
                print("★★ **现在没有一个站可问** —— 不发请求（★ 也没浪费额度）。")
                print("   ★ 等上面的窗口过去再跑。★ 退避期跑出来的读数是**假的**（`ERR-SVC-12`）。")
                print("   ★ 两只时钟都看过了：Prowlarr `disabledTill` + cross-seed `retry_after`。")
                return 3
            idx = ok[0]
            print("★ --any 挑中 id=%s（★ 已确认**两只时钟**此刻都没拦）" % idx)
        elif not force:
            _ok, blocked = disarm_at([idx], disabled, time.time(), force=False,
                                     cs_map=None if cs_err else cs_map)
            if blocked:
                print("★★★ **这个站现在被拦着 —— 不发请求**（`id=%s`）" % idx)
                for iid, why in blocked:
                    print("    %s" % why)
                print("   ★ 现在跑会拿到 429，而**退避时的读数是假的**（`ERR-SVC-12`）")
                print("   ★ 也没浪费站点额度（请求没出网）。")
                print("   ★ 换站：加 `--any`（自动挑一个可用的）；或等窗口过。")
                print("   ★ 若你确实要硬发：`--force`（★ 你自己担这个判断）。")
                return 3

    url, old_host, netloc = to_host_entry(torznab_url(env_path, idx), NAS_IP)
    print("入口：%s  →  %s    （路径 /%s/api，q=%r season=%r）"
          % (old_host, netloc, idx, q, season))

    params = {"t": "tvsearch", "q": q}
    if season:
        params["season"] = season
    full = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)

    req = urllib.request.Request(full, headers={"User-Agent": "reseed-toolkit/probe"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, body = r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        print("HTTP %s —— 站点/Prowlarr 侧拒绝了这次查询" % e.code)
        if e.code == 429:
            print("（429 = 限流/站点退避；此时读数无意义，**别下任何结论**）")
            print("  退避到什么时候：看 NAS 的 drive-loop.log 里「索引器 <站> 要等到 …」那行。")
        return 1
    except (TimeoutError, socket.timeout) as e:
        # ★★★ 超时**必须与 429 分开** —— 两者的含义完全不同：
        #   429 = 我知道自己被限流（Prowlarr 本地就拒了，没出网）；
        #   超时 = 请求**发出去了**、**没能等到答复** ⇒ ★ **不知道发生了什么**。
        # ★★ 判据（`B.10`）：超时**不是**"响应里没有季字段"，
        #   是**这次没读到响应** ⇒ ★ **未验**，一个字都不能往结论里写。
        print("HTTP —  （超时 %ds，%s）" % (timeout, type(e).__name__))
        print("★★★ 超时 ≠ 429，也 ≠ 「响应里没有季字段」：")
        print("    · 429 = Prowlarr 本地拒了，**没出网**；")
        print("    · 超时 = 请求**发出去了**，但**没等到答复** ⇒ ★ **不知道发生了什么**。")
        print("  ★ 三种可能，本读数**分不开**：① 站点慢；② Prowlarr 在排队；③ 站点/Prowlarr 挂了。")
        print("  ★★ `B.10`：这是一次**未验**，**不是**「没有」⇒ 别往结论里写。")
        print("  ★ 下一步：① 加 `--timeout 90` 再试一次；② 或先用 `--any` 换个站；")
        print("    ③ 或先看 `python scripts/diag/prowlarr-indexerstatus.py`。")
        return 4
    except Exception as e:
        print("请求失败：%s: %s" % (type(e).__name__, e))
        return 1

    print("HTTP %s" % status)
    items = items_of(body)
    titles = [title_of(it) for it in items]

    print("条数 = %d" % len(titles))
    for n, t in enumerate(titles[:25], 1):
        print("  %2d. %s" % (n, t[:120]))
    if len(titles) > 25:
        print("  … 其余 %d 条略" % (len(titles) - 25))

    if dump_attrs:
        print()
        print("=" * 72)
        print("★★ item 全文（★ 出口已过 redact()）—— 用来判「响应里有没有季字段」")
        print("=" * 72)
        if not items:
            print("★ 没有 item ⇒ 没有 attr 可看。（条数 0 的两种可能见下。）")
        want = ("season", "episode", "tvdbid", "rageid", "imdb", "tvtitle",
                "covers", "category", "size", "seeders", "grabs", "downloadvolumefactor")
        seen = set()
        for n, it in enumerate(items[:10], 1):
            print()
            print("-- item %d --" % n)
            # ★ 该 item 里出现过的**所有**元素名 —— 这样「有没有别的写法」一并答掉
            names = re.findall(r"<([A-Za-z_][\w:]*)[\s>/]", it)
            print("   元素名（去重，按首现序）：%s" % ", ".join(
                dict.fromkeys(names)))
            at = attrs_of(it)
            if not at:
                print("   ★ torznab:attr：**一个都没有**")
            else:
                print("   torznab:attr %d 条：" % len(at))
                for k, v in at:
                    seen.add(k)
                    mark = "  ★★" if k.lower() in want else "    "
                    print("%s %-22s = %s" % (mark, k, redact(v)[:100]))
        if items:
            print()
            print("★ 本轮 attr 里出现过的名字（去重）：%s" % (
                ", ".join(sorted(seen)) or "（无）"))
            print("★★ 判读（三条，缺一不可）：")
            print("   ① 上面**有没有** `season` / `episode` 这类字段？")
            print("   ② 若**没有** —— ★ 别急着写「不给」：先看元素名那一行，")
            print("      确认不是换了个名字（`rageid`/`tvdbid` 也算**季界可用的锚**）。")
            print("   ③ 真正的『season 在服务端生效』的判据是**两次对照**：")
            print("      同一剧 `season=1` 与 `season=2` 各跑一次，★ **比返回集**。")
            print("      返回集不同 ⇒ 服务端认了 season（哪怕响应体里没有季字段）。")

    if raw:
        print()
        print("=" * 72)
        print("★★ 原始响应前 4000 字符（★ 已过 redact()；URL/凭据一律不回显）")
        print("=" * 72)
        print(redact(body[:4000]))

    if not titles:
        print("★ 条数 0 有两种可能，本读数**分不开**：")
        print("   ① 站上确实没有；② 有货但**只有单集**（config.js 的")
        print("      includeSingleEpisodes=false 把单集静默过滤了，日志里查不到）。")
        print("   要分开得去站点网页再搜一次 —— 那是**另一个 query**，口径不同。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
