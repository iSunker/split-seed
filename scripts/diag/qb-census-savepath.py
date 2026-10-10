#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qB(:3320) 落点普查 —— **只读**。回答「某一类种子集体异常时，它们是落在哪儿的」。

什么时候用它
------------
2026-09-13 立此脚本的现场：24 条 IYUU 种子停在 `error`，而「为什么」一直定不下来。
当时的假设是「IYUU 把 save_path 指到了容器没挂的 `/downloads`」—— 这个假设
**被本脚本的第一版当场判死**：那 24 条与 905 条健康的 `save_path` 完全同形，
全 932 条都是 `/volume1/video/download/reseed/<X>/<Y>`，段数全部 = 6。
⇒ **落点不是分界**。判据必须先跑，结论才谈得上。

★ 为什么把「前 2 段」和「前 4 段」放在同一份里
  第一版只打前 2 段，结果 `/volume1/video` 底下全被折叠成一个桶，等于没读
  （这一条差点让「不是分界」这个正确结论看起来像"没读出来"）。
  ⇒ 两档一起打，并附**段数分布** —— 段数本身就能回答"有没有指到陌生的根"。
  合并成一份而不是两份，是因为两份会**各自漂开**：这个仓库已经反复吃过
  「同一个口径有两处声明」的亏（见 run.sh 的头部）。

脱敏口径（**硬的**）
-------------------
  · 绝不打印 torrent 名 / tracker / content_path / 完整 save_path。
  · 路径只打**前 N 段**（N=2 或 4）。第 5 段起才是资源名，故止步于 4。
    已知目录树：`download/{bilibili,jiayuan,knowledge,movies,music,reseed,
    shijian,temp,TV,短剧,可删,软件}`。
  · 属性接口里凡带路径的字段一律过 `depth()` 再打。
  · 本脚本从本地 `.env` 读 qB 的账号口令用于登录，**绝不回显**它们
    （与 scripts/torznab-probe.py 同一形状）。

用法
----
    python scripts/qb-census-savepath.py            # 默认打 error 类 vs 全量
    python scripts/qb-census-savepath.py --state error,stalledDL

退出码：0 = 读到；1 = 登录/接口失败。
"""
import collections
import json
import pathlib
import sys
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ★ 别用字符串切路径：`__file__.rsplit("\\",1)[0].rsplit("/",1)[0]` 在 Windows 上
#   **第二刀切不掉任何东西**（只剩反斜杠了），于是 REPO 停在 scripts/ —— 一个
#   看起来正常、其实指错一层的路径。pathlib 两个平台都对。
REPO = pathlib.Path(__file__).resolve().parent.parent
ENV = {}
try:
    with open(REPO / ".env", encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                ENV[k.strip()] = v.strip().strip('"').strip("'")
except OSError as e:
    print("✗ 读不到 .env：%s" % e, file=sys.stderr)
    sys.exit(1)

# ★ 2026-10-06 起保种实例改名/多实例：旧 qbittorrent-reseed:3060 → qbit-reseed1:3320
#   （宿主端口实测见 `docker ps`；3060 已无任何容器发布）。
BASE = "http://%s:3320" % ENV.get("NAS_IP", "192.168.0.7")
_op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())


def call(path, data=None):
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(BASE + path, data=body)
    if body:
        req.add_header("Referer", BASE)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    return _op.open(req, timeout=30).read()


def depth(p, n):
    """路径前 n 段。**真空串**给「(空)」。

    ★ 2026-09-14 改（#63）：旧注释写「空**本身就是信号**（qB 没建立起内容路径）」
    —— 错。空有两个来源，`depth()` 这一层**区分不了**：接口真返回了空串，还是
    那个键**压根不存在**（`dict.get` 给 None）。后者不是信号，是**取错了接口**。
    区分放在调用处：**缺键有它自己的字面**（见 main() 里那份 dump）。
    """
    seg = [s for s in (p or "").replace("\\", "/").split("/") if s]
    return "/" + "/".join(seg[:n]) if seg else "(空)"


def nseg(p):
    return len([s for s in (p or "").replace("\\", "/").split("/") if s])


def bucket(title, ts, n):
    print("\n=== %s（%d 条）—— save_path 前 %d 段 ===" % (title, len(ts), n))
    rows = collections.Counter(depth(t.get("save_path"), n) for t in ts)
    for k, v in sorted(rows.items(), key=lambda kv: -kv[1])[:12]:
        print("  %6d  %s" % (v, k))
    if len(rows) > 12:
        print("  …（还有 %d 个桶）" % (len(rows) - 12))


def main():
    argv = sys.argv[1:]
    want = ["error"]
    for i, a in enumerate(argv):
        if a == "--state" and i + 1 < len(argv):
            want = [s.strip() for s in argv[i + 1].split(",") if s.strip()]

    try:
        call("/api/v2/auth/login", {"username": ENV.get("QBIT_USER", "admin"),
                                    "password": ENV.get("QBIT_PASSWORD", "")})
        pref = json.loads(call("/api/v2/app/preferences"))
        info = json.loads(call("/api/v2/torrents/info"))
    except Exception as e:
        print("✗ 登录或取数失败：%s" % e, file=sys.stderr)
        return 1

    print("=== qB 自身的落点设置 ===")
    for k in ("save_path", "temp_path", "temp_path_enabled"):
        print("  %-20s %s" % (k, pref.get(k)))
    print("  ★ free_space_on_disk 不在这里 —— 它在 /api/v2/sync/maindata 的 "
          "server_state 下（本脚本不取）。")

    print("\n=== 全量 %d 条的 state 分布 ===" % len(info))
    for k, v in sorted(collections.Counter(t.get("state", "?") for t in info).items(),
                       key=lambda kv: -kv[1]):
        print("  %6d  %s" % (v, k))

    sel = [t for t in info if t.get("state") in want]
    print("\n=== 选中 %s：%d 条 ===" % ("/".join(want), len(sel)))

    bucket("选中", sel, 2)
    bucket("选中", sel, 4)
    print("  -- 段数分布（选中）--")
    for k, v in sorted(collections.Counter(nseg(t.get("save_path")) for t in sel).items()):
        print("  %6d  段数=%d" % (v, k))

    bucket("全量对照", info, 4)

    print("\n=== 交叉：各前 4 段桶下 选中 / 总数 ===")
    tot = collections.Counter(depth(t.get("save_path"), 4) for t in info)
    hit = collections.Counter(depth(t.get("save_path"), 4) for t in sel)
    for k in sorted(tot, key=lambda x: -tot[x])[:12]:
        print("  %6d / %-6d  %s" % (hit.get(k, 0), tot[k], k))

    if sel:
        # ★ 挑一条看数值属性。**按 hash 排序取第一条**，不取 sel[0] ——
        #   torrents/info 的顺序不保证，取 sel[0] 会让同一现场每次挑到不同种子，
        #   于是"上次读到的那个数"对不上，"排查"变成"追一个会动的靶子"。
        #
        # ★★ 2026-09-14 修（#63）：「**键不存在**」与「**值是空**」必须印成两种字面。
        #   旧版一律 `v = pr.get(k)` 再 `depth(v, 4)` ⇒ 缺键给 None ⇒ 印成「(空)」，
        #   注释又把它解释成"qB 没能建立起内容路径"。实测 qB v4.6.5 的
        #   `/api/v2/torrents/properties` **压根不返回 content_path** —— 那不是信号，
        #   是**取错了接口**（它长在 torrents/info 上）。假读数比不读数更坏：
        #   它让人去查「qB 为什么不知道」，而真相是「我们问错了接口」。
        #   判据：**两种形态在输出里分得开** ——「(字段不存在)」只对应缺键。
        h = sorted(t["hash"] for t in sel)[0]
        try:
            pr = json.loads(call("/api/v2/torrents/properties?hash=" + h))
            print("\n=== 挑 1 条（%s…，路径照脱敏）的属性 ===" % h[:8])
            for k in ("save_path", "download_path", "content_path", "total_size",
                      "total_downloaded", "seeds", "peers", "dl_speed"):
                if k not in pr:              # ★ 缺键 ≠ 空值
                    print("  %-18s (字段不存在)" % k)
                    continue
                v = pr.get(k)
                if k in ("save_path", "download_path", "content_path"):
                    v = depth(v, 4)          # 只有真空串才给 "(空)"
                print("  %-18s %s" % (k, v))
            if "content_path" not in pr:
                print("  ★ content_path **不在这个接口的返回里** —— 要读它得走 "
                      "/api/v2/torrents/info 的同名键（本脚本上面已经取过 info）。")
            print("  ★ total_downloaded / total_size 才是「下了多少」的真相：")
            print("    progress 是四舍五入到 4 位的，0.0000 不等于零字节。")
        except Exception as e:
            print("\n  （属性接口失败：%s）" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
