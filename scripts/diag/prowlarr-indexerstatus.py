#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读读 Prowlarr 的 `/api/v1/indexerstatus`：**它是不是在本地禁用某个站**。

答的是 `ERR-SVC-17` 的那个问题
--------------------------------
一个站的 `retry_after` 长得一次比一次长时，**光看那个数分不出是三种机制的哪一种**：

  | 机制 | 谁设的 | 本脚本能分辨吗 |
  |---|---|---|
  | ① 站点真发 `Retry-After` | 站点 | 数组里**没有**该站 ⇒ ① |
  | ② **Prowlarr 本地禁用** | Prowlarr | 数组里**有**该站且 `disabledTill` 非空 ⇒ ② |
  | ③ cross-seed 自己的退避阶梯 | cross-seed | 用 `check-indexer-timestamps.py` 看 |

2026-09-14 定 HDtime 那一例就是靠它：数组**非空**且只有 HDtime 一条、
`disabledTill` 落在未来 ⇒ ②。而 cross-seed 收到的那个 429 是 **Prowlarr 发的**，
不是站点限流（站点对 Prowlarr 返的是 5xx，Prowlarr 侧日志里 429 一条都没有）。

为什么不是一条 `curl`
--------------------
`ENVIRONMENT.md` 早先给的处方是一条裸 `curl -H "X-Api-Key: <KEY>"` —— 两个毛病：
  · 它把 **key 摆在命令行上**（进程表 / shell 历史 / 聊天粘贴都会漏）；
  · 返回的裸 JSON 里只有 `indexerId`，**要靠人肉对到站名**。

用法
----
    python scripts/prowlarr-indexerstatus.py                 # 打 NAS 上那份生产 .env 的站
    python scripts/prowlarr-indexerstatus.py --compose <目录>  # 换一份 .env（默认 NAS 的 compose 目录）

★ 硬口径（改这个脚本时**必须守住**）：
  · key 从**生产 .env** 读，**绝不打印、绝不上命令行**；
  · **不调用 `/api/v1/indexer`** —— 那个响应带 `fields`（cookie / passkey）；
  · 只打**白名单字段**：indexerId / escalationLevel / disabledTill / 两个失败时刻 / id；
  · 站名靠 `cross-seed.db` 的 `indexer` 表对应，**只取 id, name** —— 不取 url / apikey。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sqlite3
import sys
import urllib.error
import urllib.request

try:  # GBK 控制台下不 reconfigure 会在中途炸掉，而**已经打出来的看着全对**
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 —— 老 python 没有 reconfigure，乱码也比崩了强
    pass

#: 默认 = NAS 上的生产 compose 目录（Windows 侧走 UNC）。
DEFAULT_COMPOSE = "//iSunker-DS423/docker_ssd/reseed"

#: ★ 白名单：只取这几格。多取一格都可能把凭据或无关内容带出来。
ALLOW = ("indexerId", "escalationLevel", "disabledTill",
         "mostRecentFailure", "initialFailure", "id")


def read_env(path: pathlib.Path) -> dict[str, str]:
    """读 .env 成 dict。**只读不写、不回显**。"""
    out: dict[str, str] = {}
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in ln and not ln.strip().startswith("#"):
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def indexer_names(db: pathlib.Path) -> dict[int, str]:
    """id → 站名。★ 只取 id, name 两列（url / apikey 那两列带凭据）。"""
    names: dict[int, str] = {}
    try:
        con = sqlite3.connect(str(db))
        con.execute("PRAGMA query_only = ON")
        try:
            for i, nm in con.execute("SELECT id, name FROM indexer"):
                names[i] = nm
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001 —— 读不到就只打 id，不因此中断
        print("（cross-seed.db 站名映射读不到：%r —— 只打 id）" % (e,))
    return names


def main() -> int:
    ap = argparse.ArgumentParser(description="只读读 Prowlarr indexerstatus（ERR-SVC-17 的 ② 机制）")
    ap.add_argument("--compose", default=DEFAULT_COMPOSE,
                    help="compose 目录（含 .env 与 cross-seed/cross-seed.db）；默认 NAS 那份")
    args = ap.parse_args()

    root = pathlib.Path(args.compose)
    env = read_env(root / ".env")
    key = env.get("PROWLARR_API_KEY")
    ip = env.get("NAS_IP")
    base = env.get("PROWLARR_URL") or ("http://%s:9696" % (ip or ""))
    if not key:
        raise SystemExit("[!!] 生产 .env 里没有 PROWLARR_API_KEY")
    if "//:" in base or base.endswith("//"):
        raise SystemExit("[!!] PROWLARR_URL / NAS_IP 都取不到，拼不出目标地址")

    # 容器主机名 → NAS IP：**只换 authority**，端口 / 路径不动
    if ip:
        base, n = re.subn(r"^(https?://)[^/:]+", r"\g<1>" + ip, base, count=1)
        print("目标：NAS IP + 原端口（替换 authority %d 次）；key 已从 .env 读入（不打印）" % n)
    else:
        print("目标：.env 里的 PROWLARR_URL 原样；key 已从 .env 读入（不打印）")

    req = urllib.request.Request(base.rstrip("/") + "/api/v1/indexerstatus",
                                 headers={"X-Api-Key": key,
                                          "User-Agent": "reseed-toolkit/probe"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            status = r.status
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        print("HTTP %s —— 拒绝（401 = key 不对；404 = 端点不存在/版本不同）" % e.code)
        return 0
    except Exception as e:  # noqa: BLE001
        print("请求失败：%s: %s" % (type(e).__name__, e))
        return 0

    print("HTTP %s   数组长度 = %d" % (status, len(data) if isinstance(data, list) else -1))
    if not isinstance(data, list):
        raise SystemExit("响应不是数组 —— 不猜测内容")

    names = indexer_names(root / "cross-seed/cross-seed.db")
    print("\n%-4s %-22s %-12s %-21s %s"
          % ("id", "站名", "escalation", "disabledTill", "mostRecentFailure"))
    for row in data:
        if not isinstance(row, dict):
            continue
        safe = {k: row.get(k) for k in ALLOW}          # ★ 白名单：只取这几格
        iid = safe.get("indexerId")
        print("%-4s %-22s %-12s %-21s %s"
              % (iid, names.get(iid, "（cross-seed 里无此 id）"),
                 safe.get("escalationLevel"), safe.get("disabledTill"),
                 safe.get("mostRecentFailure")))

    print("\n★ 判读：数组里**有**该站且 disabledTill 非空 ⇒ ② Prowlarr 本地禁用（与站点无关）；"
          "\n   数组为空 / 没有该站 ⇒ ① 站点真发、Prowlarr 只是转发。"
          "\n★ 两个时钟不一致时**以更长的为准**：disabledTill 管 Prowlarr 何时肯转发，"
          "\n   cross-seed 的 retry_after 管它何时肯再问 —— 两个都读，取晚的那个。"
          "\n   cross-seed 那侧用 `python scripts/check-indexer-timestamps.py`。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
