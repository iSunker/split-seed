#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读探针：`TORZNAB_URLS` 里那个 apikey **现在还作不作数**？

答的是什么问题
--------------
`#109`（Prowlarr key 轮换）的前置问题：**这个 key 是活键还是死键**。
它决定轮换的性质 ——
  · 活键 ⇒ 轮换是「**补一个已经漏出去的漏**」，有紧迫性；
  · 死键 ⇒ 轮换只是「清理一个早作废的字符串」，可以慢慢来。
两种情形下的处置力度完全不同，所以**这个判断必须先做**。

为什么用 `t=caps`
-----------------
caps 由 Prowlarr 用**本地定义**回答，**不打 PT 站**。
站点在退避时（凌晨常见）搜一次只会拿到无意义的 429，而 caps 不受影响。
⇒ 这是一条**不依赖站点状态**的探针。

★ 为什么必须带**阴性对照**
----------------------------
单看一个 200 说明不了「这个 key 被认了」—— **端点可能根本不校验**。
所以每次都同时发两组**故意错的**：
  · 32 个 0 的假 key → 预期 401/403
  · 完全不带 key    → 预期 401/403
阴性也返 200 ⇒ 本次探针**作废**，别硬下结论。
（这正是本仓库那条纪律：**没有阴性对照的阳性等于没测**。）

安全
----
* key 从**生产 .env** 读，**绝不打印、绝不上命令行**；
* **不调用 `/api/v1/indexer`**（那个响应带 `fields`：cookie/passkey）；
* 出口统一过 `mask()`：任何 16 位以上的十六进制一律掩成 `<K32>`；
* **全程只读**：发 `t=caps` 查询，不 search、不写任何东西。

用法
----
    python scripts/torznab-keycheck.py                    # 打 NAS 上那份生产 .env
    python scripts/torznab-keycheck.py --compose <目录>     # 换一份 .env
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
import urllib.error
import urllib.request

try:  # GBK 控制台下不 reconfigure 会在中途炸掉，而**已经打出来的看着全对**
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

#: 默认 = NAS 上的生产 compose 目录（Windows 侧走 UNC）。
DEFAULT_COMPOSE = "//iSunker-DS423/docker_ssd/reseed"

#: Torznab 的 caps 端口（Prowlarr 已发布到宿主机）。
USER_AGENT = "reseed-toolkit/probe"


def read_env(path: pathlib.Path) -> dict[str, str]:
    """读 .env 成 dict。**只读不写、不回显**。"""
    out: dict[str, str] = {}
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in ln and not ln.strip().startswith("#"):
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def mask(s: str) -> str:
    """★ 出口兜底：16 位以上的十六进制一律掩掉。打印前**必须**过这一道。"""
    return re.sub(r"[0-9a-fA-F]{16,}", "<K32>", s)


def hit(base: str, idx: str, key: str | None, t: str = "caps"):
    """发一次请求。`key=None` ⇒ 不带 apikey（阴性对照的一种）。"""
    q = []
    if t:
        q.append("t=" + t)
    if key is not None:
        q.append("apikey=" + key)
    url = "%s/%s/api?%s" % (base, idx, "&".join(q))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read(600).decode("utf-8", "replace")
            return r.status, mask(" ".join(body.split())[:90])
    except urllib.error.HTTPError as e:
        try:
            body = e.read(300).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return e.code, mask(" ".join(body.split())[:90])
    except Exception as e:  # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, mask(str(e))[:70])


def main() -> int:
    ap = argparse.ArgumentParser(description="只读验 TORZNAB_URLS 里的 apikey 是否仍有效")
    ap.add_argument("--compose", default=DEFAULT_COMPOSE,
                    help="compose 目录（含 .env）；默认 NAS 那份")
    args = ap.parse_args()

    env_p = pathlib.Path(args.compose) / ".env"
    if not env_p.is_file():
        print("[!!] 读不到 %s" % env_p)
        return 2
    env = read_env(env_p)
    prowl = env.get("PROWLARR_API_KEY", "")
    ip = env.get("NAS_IP", "")
    torz = env.get("TORZNAB_URLS", "")
    if not (prowl and ip and torz):
        print("[!!] .env 里缺 PROWLARR_API_KEY / NAS_IP / TORZNAB_URLS 之一")
        return 2

    # TORZNAB_URLS → [(path 段编号, 该段的 apikey)]
    pairs: list[tuple[str, str]] = []
    for seg in torz.split(","):
        m = re.search(r"/(\d+)/api\?", seg)
        k = re.search(r"apikey=([0-9a-fA-F]{32})", seg)
        if m and k:
            pairs.append((m.group(1), k.group(1)))
    if not pairs:
        print("[!!] TORZNAB_URLS 里解析不出任何 `/<id>/api?…apikey=<32hex>` 段")
        return 2

    base = "http://%s:9696" % ip
    same = {k for _, k in pairs} == {prowl}
    print("目标 : %s（apikey 从 .env 读入，不打印）" % base)
    print("段数 : TORZNAB_URLS 解析出 %d 段" % len(pairs))
    print("键比 : %s" % ("TORZNAB 段里的 key 与 PROWLARR_API_KEY **相同**（同一条 key）"
                        if same else
                        "★ TORZNAB 段里的 key 与 PROWLARR_API_KEY **不同** —— 两处得分头轮换"))
    print()
    print("%-28s %-5s %-6s %s" % ("用例", "path", "状态", "body 首段（已掩码）"))
    print("-" * 96)

    for idx, k in pairs:                       # 阳性：生产那一对
        st, b = hit(base, idx, k)
        print("%-28s %-5s %-6s %s" % ("生产 TORZNAB apikey", idx, st, b))

    idx0 = pairs[0][0]
    st, b = hit(base, idx0, prowl)             # 阳性对照：另一处那个 key 打同一个 path
    print("%-28s %-5s %-6s %s" % ("PROWLARR_API_KEY 同 path", idx0, st, b))
    st, b = hit(base, idx0, "0" * 32)          # 阴性①：假 key
    print("%-28s %-5s %-6s %s" % ("阴性① 假 key(0×32)", idx0, st, b))
    st, b = hit(base, idx0, None)              # 阴性②：不带 key
    print("%-28s %-5s %-6s %s" % ("阴性② 不带 apikey", idx0, st, b))

    print("\n★ 判读：")
    print("  · 阴性①② 若也返 200 ⇒ 这个端点**根本不校验**，上面的 200 说明不了任何事，"
          "\n    本次探针作废 —— 别硬下结论。")
    print("  · 阴性返 401/403 而阳性返 200 ⇒ key **有效**（活键）。")
    print("  · 口径边界：caps 由 Prowlarr 用本地定义回答，所以它证明的是"
          "\n    「Prowlarr 认这个 key」，**不**证明「搜一次能搜到东西」（那还要站点可达）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
