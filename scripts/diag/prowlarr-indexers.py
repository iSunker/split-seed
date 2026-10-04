#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""列 Prowlarr 全部索引器的 **id → 站名**（只读）。

为什么必须有这个小工具
----------------------
`ERR-SVC-02` 说「`indexerId` 会变，增删索引器后必须重新核对」，而三条文档
（`README.md`、`summary/06`、`summary/13`）给的处方是一条**裸 `curl`**
打 `GET /api/v1/indexer` —— 而该端点的响应**每一条都带 `fields`**，
那里面有 **cookie / passkey**（`ENVIRONMENT.md` 开头就写着「绝不打印
`indexer.fields`」）。于是同一个仓库里：

  · 一条规矩说「绝不打印 `indexer.fields`」；
  · 三条照着敲的处方**逐字把它打出来**。

`ENVIRONMENT.md` 后来加了「管道是必须的」警示，但：
  · 管道只挡住「裸跑」，挡不住「为了看一眼结构」而 `| head` / `| jq`；
  · 而且**没有任何脚本能替代它** —— `prowlarr-indexerstatus.py` 只列
    **有失败状态**的站、`check-indexer-timestamps.py` 只列 cross-seed 已知的站，
    两者都答不了「全部 indexer 的 id→name」。没有替代品，处方就只能继续裸跑。

本脚本就是那个替代品：**同一个端点，但只取白名单字段**，
`fields` 里的东西**从不进内存、更不进 stdout**。

用法
----
    python scripts/prowlarr-indexers.py                       # 打 NAS 上那份生产 .env 的站
    python scripts/prowlarr-indexers.py --compose <目录>       # 换一份 .env
    python scripts/prowlarr-indexers.py --torznab             # 加一列「本仓 TORZNAB_URLS 里用了没」

★ 硬口径（改这个脚本时**必须守住**）：
  · key 从**生产 .env** 读，**绝不打印、绝不上命令行**（`--api-key` 这类参数**不许加**）；
  · **只取白名单字段**：`id` / `name` / `enable`。其余 25 个键里就有 `fields`；
  · 站名**不从 `fields` 里找** —— 只认响应顶层的 `name`；
  · 本脚本**只读**：不写 `.env`、不碰 qB、不碰 state.db。

★ 与 `prowlarr-indexerstatus.py` 的分工（别重复造）：
  · 本脚本答「**有哪些站、id 是几**」（全量）；
  · 那个答「**哪个站被本地禁了**」（只列有状态的）。

★ **本脚本不进受管集合（`DEPLOY_MANIFEST`）**（2026-09-17 拍板，**别再顺手加**）：
  受管集合现有 **11 条**（旧 `deploy.sh` 白名单 28 条，随代码入镜像大幅缩减）——
  都是「NAS 要执行/要读」的**配置与运维脚本**（`compose.yaml`、`cross-seed/`、`hlink/`、
  `build-farm.sh`、`rm-staging.sh`、`fix-statedb-farm-root.py`、`nas-update-env.sh`、
  `.dockerignore`、`scripts/diag/watch-containers*.sh`）。
  而本脚本与它同族的四个（`prowlarr-indexerstatus.py` / `torznab-keycheck.py` /
  `check-indexer-timestamps.py` / `check-deploy-drift.py`）是 **Windows 侧只读探针**：
  **从本机读 UNC 上的 .env**，再打本机到 Prowlarr 的 HTTP。
  ⇒ **NAS 上没有、也不该有调用者**。加进去只会同步一个没人跑、还要维护两边一致的副本。
  ★ 本条正确性不靠受管集合 —— 靠 **git**（它是被跟踪的源码）。

★ **它不打站点**（2026-09-17 用户问到，记清楚）：
  只发一个 `GET http://<NAS>:9696/api/v1/indexer`，Prowlarr 用**本地库**回答
  ⇒ **不碰 PT 站、不碰 tracker、不出外网**。
  ★ 但「请求不碰站点」≠「响应无害」：这个端点的响应**带 `fields`（cookie/passkey）**，
     那是 `#83` 管的风险面（凭据外泄），与本脚本无关 —— 本脚本正是为消除它而写的。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import urllib.error
import urllib.request

try:  # GBK 控制台下不 reconfigure 会在中途炸掉，而**已经打出来的看着全对**
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 —— 老 python 没有 reconfigure，乱码也比崩了强
    pass

#: 默认 = NAS 上的生产 compose 目录（Windows 侧走 UNC）。
DEFAULT_COMPOSE = "//iSunker-DS423/docker_ssd/reseed"

#: ★ 白名单：只取这三格。多取一格就可能把 `fields`（cookie / passkey）带出来。
ALLOW = ("id", "name", "enable")


def read_env(path: pathlib.Path) -> dict[str, str]:
    """读 .env 成 dict。**只读不写、不回显**。"""
    out: dict[str, str] = {}
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def pick(item: dict) -> dict:
    """★ 只挑白名单字段 —— 这是本脚本存在的**全部理由**。

    不要改成 `dict(item)` / `item.pop("fields")` / `{k:v for k,v in item.items()
    if k != "fields"}` —— 前两个的理由已在 `ENVIRONMENT.md` 记过，第三个的问题是
    **新字段随时可能被 Prowlarr 加上**（`fields` 就是这么来的），
    而 `!= "fields"` 只挡今天知道的那一个 ⇒ 白名单，不是黑名单。
    """
    return {k: item.get(k) for k in ALLOW}


def torznab_ids(env: dict[str, str]) -> set[str]:
    """从 `TORZNAB_URLS` 里抠出用到的 indexerId（**只看 id，不回显 URL**）。"""
    raw = env.get("TORZNAB_URLS", "")
    return set(re.findall(r":\d+/(\d+)/api", raw)) | set(re.findall(r"/(\d+)/api", raw))


def main() -> int:
    ap = argparse.ArgumentParser(description="列 Prowlarr 索引器的 id → 站名（只读）")
    ap.add_argument("--compose", default=DEFAULT_COMPOSE,
                    help="生产 compose 目录（读它的 .env）")
    ap.add_argument("--torznab", action="store_true",
                    help="加一列「本仓 TORZNAB_URLS 里用了没」")
    args = ap.parse_args()

    root = pathlib.Path(args.compose)
    env_path = root / ".env"
    if not env_path.is_file():
        print(f"✗ 读不到 .env：{env_path}")
        return 2
    env = read_env(env_path)

    key = env.get("PROWLARR_API_KEY", "")
    if not key:
        print("✗ .env 里没有 PROWLARR_API_KEY")
        return 2

    base = env.get("PROWLARR_URL") or f"http://{env.get('NAS_IP', '192.168.0.7')}:9696"
    req = urllib.request.Request(base.rstrip("/") + "/api/v1/indexer")
    req.add_header("X-Api-Key", key)          # ★ key 走 header，不上命令行
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"✗ Prowlarr 返回 {e.code}（key 错？或地址不对？）")
        return 2
    except Exception as e:                    # noqa: BLE001
        print(f"✗ 调不到 Prowlarr：{type(e).__name__}: {e}")
        return 2

    if not isinstance(data, list):
        print(f"✗ 响应不是数组（是 {type(data).__name__}）—— 端点变了？")
        return 2

    # ★★ 在这里就把 fields 丢掉：下游只会拿到白名单三格。
    rows = [pick(i) for i in data if isinstance(i, dict)]
    rows.sort(key=lambda r: r.get("id") or 0)

    used = torznab_ids(env) if args.torznab else None
    print(f"Prowlarr 索引器（{base}）：共 {len(rows)} 个")
    head = f"  {'id':>3}  {'站名':<18}  {'启用':<4}"
    if args.torznab:
        head += "  TORZNAB_URLS"
    print(head)
    for r in rows:
        line = f"  {str(r.get('id')):>3}  {str(r.get('name'))[:18]:<18}  " \
               f"{'✓' if r.get('enable') else '✗':<4}"
        if args.torznab:
            line += f"  {'用了' if str(r.get('id')) in used else '——'}"
        print(line)
    if args.torznab:
        missing = [i for i in used if i not in {str(r.get("id")) for r in rows}]
        if missing:
            # ★ 这是 ERR-SVC-02 的正解形态：`.env` 指着一个 Prowlarr 里**不存在**的 id。
            print(f"\n★★ TORZNAB_URLS 里的 id {sorted(missing)} 在 Prowlarr 里**不存在** —— "
                  f"ID 错位过（见 ERR-SVC-02）。")
    print("\n（★ 只取了 id/name/enable；响应里的 fields（cookie/passkey）没有取。"
          "这份输出可以安全粘贴。）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
