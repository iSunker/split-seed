#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 reseed_farm / reseed_singles 收进 /volume1/video/download/reseed/
=====================================================================

背景（为什么要动）
------------------
顶层现在并排躺着两个名字很像、角色却相反的目录：

    /volume1/video/download/
    ├── reseed_farm/       ← DATA_DIRS（cross-seed 的**输入**），475 个发布名
    ├── reseed_singles/    ← LINK_DIR（cross-seed 的**输出**）+ qB 分类
    ├── movies/  TV/  短剧/  ...

一个下划线一个连字符，一个进一个出，看的人容易混。收进一个 `reseed/` 之后
"这俩是一对"就一眼可见了。

★ 这是**加一层父目录**，不是**合并成一个目录** —— 两者仍然是兄弟，输入输出的
  分离完好无损，所以 SUMMARY §10.5.4 那三条（农场自检报错 / 垃圾 searchee /
  输入输出同树）一条都不适用。不要把它们并成同一个目录。

本脚本负责哪一半
----------------
qB（:3060）里现在有 **509 条**种子，save_path 全部落在 `reseed_singles/<站>` 下。
目录一挪，这 509 条的 save_path 就全部指向不存在的地方 → **做种中断**。

★ 509 条的构成（2026-09-12 实测，只按 tag 分）：
      408  我们的      tag `cross-seed`
      100  IYUU 的     tag `IYUU自动辅种`
        1  无主        （分类是 reseed-singles，tag 为空）
  **决定全量搬**，依据见 SUMMARY §18.8：IYUU 的排除列表是**空的**
  （`cn_folder` 零行、两个辅种任务的 `path_filter` 都是空串），而且它**不存目标目录、
  每次从 qB 现读 savePath**，所以搬迁对它完全透明、不需要改它任何配置。
  → 因此本脚本必须带 **`--all-tags`**：那 100 条与我们的内容常常是**同一份硬链接**，
    只搬 408 既做不到（搬文件必然一起搬）也没意义。

    本脚本 = qB 这一半（改 save_path / 改分类 savePath）
    NAS 上那一半 = mkdir / mv / 改配置（见下面「完整顺序」）

★ 为什么让 qB 自己搬文件，而不是先在 NAS 上 `mv` 再改 qB
  如果先在 NAS 上 `mv` 整个 reseed_singles，就会出现一个**中间态**：磁盘上文件
  已经在新位置，qB 还以为在旧位置。这中间态里 qB 的每一个种子都是"文件丢失"，
  而且这时候到底该谁去把状态对上，有两个说法（qB setLocation 时源已不存在，行为
  依版本而异）。让 qB 自己 setLocation，磁盘和它自己的记账就**始终一致**——
  它搬一次、记一次，不存在对不上的窗口。
  → 所以 reseed_singles **不要手动 mv**；reseed_farm 不在 qB 里，那个才是手动 mv。

完整顺序（NAS 上由你执行，本脚本是第 6 步）
-------------------------------------------
    0) 挑没有批次在跑的时间窗：
         - 看 drive-loop/scripts/.drive-loop.state 的 heartbeat_ts 是否还在跳
         - DSM 任务计划里把 drive-loop 那个任务**临时禁用**（否则它 15 分钟一次，
           可能在搬迁中途打 webhook）
    1) cd <compose 目录> && sudo docker compose stop cross-seed
         ★ 必须停：它正在往 reseed_singles/BTSCHOOL/ 写新链接，一边写一边搬会出事
    2) sudo mkdir -p /volume1/video/download/reseed
    3) sudo mv /volume1/video/download/reseed_farm /volume1/video/download/reseed/
         （同一个物理卷内 = rename，inode 不变，**硬链接关系完整保留**，瞬间完成）
    4) 改配置（三处，详见各自文件里的注释）：
         a. 本地 reseed-toolkit/.env 的 DATA_DIRS / LINK_DIR 两行
            → 然后 python scripts/gen-nas-env-update.py 重新生成 nas-update-env.sh
            → 把生成物同步到 NAS 后跑它（它自己会 --dry-run 校验 + 备份 + 重启）
         b. NAS 上 build-farm.sh 的 FARM 默认值一行
            （清单**不用重建**：清单名取自 basename $FARM，"reseed_farm"这层没变；
              清单内容是"名字 + **源**路径"，不含农场路径）
         c. drive-loop/hlink/state.db 的 pack.farm_root 3 行
            UPDATE pack SET farm_root='/volume1/video/download/reseed/reseed_farm'
             WHERE farm_root='/volume1/video/download/reseed_farm';
            （movie.path 存的是**源大包**路径，605 行全部不受影响）
    5) sudo docker compose up -d --force-recreate cross-seed
    6) **本脚本**：先 `--check`，再 `--limit 1 --all-tags --apply` 试跑一条，确认那条
        回到做种状态，最后 `--all-tags --apply` 全量
        ★ 正在校验/搬动中的种子会被**自动跳过**，留给复跑 —— 不是漏，是排序
          （理由见 main() 里 1b) 那段，2026-09-12 试跑踩到）。所以"全量"可能要跑两次：
          第二次在那批校验跑完之后，把它们（这时已经是 stalledUP）带走。
    7) 验证：
         - bash build-farm.sh --verify  退出码应为 0
         - qB 里随机抽几个种子，state 应回到 stalledUP（不是 missingFiles/error）
         - docker compose logs --tail=60 cross-seed

用法
----
    python scripts/migrate-reseed-dirs.py                       # = --check，只读，不改任何东西
    python scripts/migrate-reseed-dirs.py --limit 1 --all-tags --apply   # ★ 第一步：只搬 1 条
    python scripts/migrate-reseed-dirs.py --all-tags --apply    # 全量（509 条）

★ **`--all-tags` 是本次搬迁的既定用法，不是可选项**（见上「本脚本负责哪一半」）。
  不加它，脚本会挡下 IYUU 那 100 条，只搬我们的 408 条。
★ 脚本是**幂等**的：已在新根下的跳过；正在校验/搬动中的跳过。所以"没搬完"时
  **直接再跑一次**即可，不用记哪些搬过。`--include-checking` 只在明确知道后果时才用。

参数：--qbit-url（默认 http://192.168.0.7:3320）、--old-root、--new-root。
退出码：0 = 一切符合预期；1 = 发现意外情况（有 save_path 不在预期树里、目标已存在
        同名内容等），此时**不要**加 --apply，先看它列出来的东西。
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
import urllib.parse
import urllib.request

# ★ Windows 控制台默认 GBK，中文会变乱码；极端情况下直接 UnicodeEncodeError 把
#   进程打挂（本项目踩过，见 drive-loop-nas.sh 里 PYTHONIOENCODING 那段注释）。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001  —— 老 python 没有 reconfigure，乱码也比崩了强
    pass

#   ★ 2026-10-06 起保种实例改名/多实例：旧 qbittorrent-reseed:3060 →
#     qbit-reseed1:3320（宿主端口实测见 `docker ps`；3060 已无任何容器发布）。
DEFAULT_QBIT = "http://192.168.0.7:3320"
OLD_ROOT = "/volume1/video/download/reseed_singles"
FARM_OLD = "/volume1/video/download/reseed_farm"
NEW_ROOT = "/volume1/video/download/reseed/reseed_singles"
FARM_NEW = "/volume1/video/download/reseed/reseed_farm"
CATEGORY = "reseed-singles"


def api(url: str, path: str, data: dict | None = None, timeout: float = 60.0):
    """调 qB WebAPI。GET 用 data=None，POST 用 data=dict。

    ★ 必须带 Referer：qB 用它做 CSRF 检查（subnet_whitelist 模式下免密，但
      Referer 仍然要）。少了它会 403，而报错信息不会告诉你原因。
    """
    full = url.rstrip("/") + path
    if data is None:
        req = urllib.request.Request(full, headers={"Referer": url.rstrip("/")})
    else:
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(
            full, data=body, headers={"Referer": url.rstrip("/")}
        )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    return raw


def torrents(url: str) -> list[dict]:
    return json.loads(api(url, "/api/v2/torrents/info")) or []


def group_by_savepath(ts: list[dict]) -> dict[str, list[dict]]:
    g: dict[str, list[dict]] = collections.defaultdict(list)
    for t in ts:
        g[t["save_path"]].append(t)
    return dict(g)


def new_path_for(sp: str) -> str:
    """旧 save_path → 新 save_path。只做前缀替换，子结构（站点名）原样保留。"""
    if not sp.startswith(OLD_ROOT):
        raise ValueError(sp)
    return NEW_ROOT + sp[len(OLD_ROOT):]


#: ★ 校验中 / 搬动中的 state —— **一律不碰**。理由见 main() 里 1b) 那段注释。
IN_FLIGHT_STATES = frozenset({"checkingDL", "checkingUP", "moving", "allocating"})

#: ★ 判断"这条种子是不是我们（cross-seed）的"——**看 tags，不要看 category**。
#: 2026-09-12 的教训：`reseed_singles/` 是 cross-seed 和 IYUU Plus **共用**的目录。
#: cross-seed 会给它注入的每条打 `cross-seed` tag，IYUU 打 `IYUU自动辅种`，而 IYUU
#: 那 100 条**根本没有分类**（category 为空）。所以"无分类 = 我们自己早期的运行"这个
#: 推断是错的 —— 它恰好会把别人的东西认成自己的，然后一起搬走。
OUR_TAG = "cross-seed"


def tags_of(t: dict) -> set[str]:
    """qB 的 tags 是逗号分隔的字符串（不是数组）。"""
    return {x.strip() for x in (t.get("tags") or "").split(",") if x.strip()}


def is_ours(t: dict) -> bool:
    return OUR_TAG in tags_of(t)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="把 qB 里 reseed_singles 的种子 save_path 迁到新根下"
    )
    ap.add_argument("--qbit-url", default=DEFAULT_QBIT, help=f"默认 {DEFAULT_QBIT}")
    ap.add_argument("--old-root", default=OLD_ROOT)
    ap.add_argument("--new-root", default=NEW_ROOT)
    ap.add_argument("--apply", action="store_true", help="真的改（不加就是只读预检）")
    ap.add_argument(
        "--limit", type=int, default=0,
        help="**每个站点组各取前 N 条**（不是「总共 N 条」）。第一次务必用 --limit 1 ——"
             "本站 3 个站点组，所以实际会搬 3 条（每站一条），正好每个站都试到。",
    )
    ap.add_argument(
        "--all-tags", action="store_true",
        help="连**不是我们的**种子（如 IYUU 自动辅种）一起搬。默认只搬 tag 含 "
             f"`{OUR_TAG}` 的；其余只列出来、不动。",
    )
    ap.add_argument(
        "--include-checking", action="store_true",
        help="连**正在校验/移动中**的种子也搬。★ 默认不碰它们：作废已完成的校验，"
             "且 save_path 先变、数据后搬 → 必然变成 missingFiles。别加，除非明确知道后果。",
    )
    args = ap.parse_args()

    url, old_root, new_root = args.qbit_url, args.old_root, args.new_root

    print(f"qB            : {url}")
    print(f"旧根          : {old_root}")
    print(f"新根          : {new_root}")
    print(f"模式          : {'★ APPLY（会改）' if args.apply else '预检（只读）'}")
    print()

    # ---- 1) 读现状 -------------------------------------------------------
    all_ts = torrents(url)
    print(f"qB 种子总数 = {len(all_ts)}")

    # ★ 先按 tags 把"我们的"和"别人的"分开。**判据是 tags 不是 category**：
    #   IYUU 那 100 条 category 是空的，按 category 判断会把它们当成早期试点的残留。
    mine = [t for t in all_ts if is_ours(t)]
    other = [t for t in all_ts if not is_ours(t)]
    print("\n== 按 tag 分归属 ==")
    for tag, n in collections.Counter(
        ",".join(sorted(tags_of(t))) or "(无 tag)" for t in all_ts
    ).most_common():
        print(f"  {n:>4d}  [{tag}]")
    if other:
        # ★ 这句必须跟着 --all-tags 变 —— 带着 --all-tags 却打印「默认不动它们」，
        #   在搬迁现场是会误导人的（操作者会以为对方那批被留下了）。
        verb = "本次**一起搬**（`--all-tags`）" if args.all_tags else "本次**不动**"
        print(f"\n  ★ 有 {len(other)} 条**不是我们注入的**，{verb}：")
        for tag, n in collections.Counter(
            ",".join(sorted(tags_of(t))) or "(无 tag)" for t in other
        ).most_common():
            print(f"       {n:>4d}  [{tag}]")
        if args.all_tags:
            print("       ★ 对方是 IYUU Plus（tag `IYUU自动辅种`）。已确认它**不存目标目录、"
                  "\n         每次从 qB 现读 savePath**，且排除列表是空的 "
                  "（`cn_folder` 零行、\n         `path_filter` 为空串）→ 搬迁对它透明，"
                  "**不需要改它任何配置**（SUMMARY §18.8）。")
        else:
            print("       → 想让它们一起搬：加 --all-tags（但这会让对方的 save_path 失效，"
                  "\n         对方的配置也得跟着改）。")

    ts = all_ts if args.all_tags else mine
    if not args.all_tags and other:
        print(f"\n  本次只处理其中 {len(ts)} 条（tag 含 `{OUR_TAG}`）。")

    # ---- 1b) 两道"别碰"的闸 ------------------------------------------------
    # ★ 闸一：已经在新根下的 = 上一次跑搬过去的（或试跑搬的）。跳过，而不是当异常。
    #   没有这道闸，第二次跑会因为它"不在旧根下"而退出 1 —— 把一次正常复跑变成假警报。
    already = [t for t in ts if (t["save_path"] + "/").startswith(new_root + "/")]
    if already:
        print(f"\n  ✓ 已有 {len(already)} 条**已经在新根下**，跳过（幂等，重复跑安全）。")
        ah = {t["hash"] for t in already}
        ts = [t for t in ts if t["hash"] not in ah]

    # ★ 闸二：校验中 / 搬动中的，**默认一律不碰**。
    #   2026-09-12 试跑踩到：qB 一次只做一条校验，同一个 cross-seed 批次注入的
    #   11 条 BTSCHOOL 种子会长时间停在 checkingDL（其余排队 0%）。对它们调
    #   setLocation 有两个后果，而且是**叠加**的：
    #     · qB 立刻改 save_path，但数据还留在旧位置、要等搬完 —— 中间态里它的校验
    #       是按**新路径**跑的，必然找不到任何文件 → 最后落到 missingFiles；
    #     · 那条正在跑的校验（Preacher，201 GB，已到 71%）会被整个作废。
    #   正确做法是等它们校验完（变成 stalledUP），那时候再搬 —— 那是一次 rename，
    #   校验结果仍然有效，不用重跑。所以这里跳过不是"漏了"，是**排序**：
    #   下一次不带参数的复跑会自动把它们带上。
    if args.include_checking:
        inflight = []
    else:
        inflight = [t for t in ts if t["state"] in IN_FLIGHT_STATES]
    if inflight:
        print(f"\n  ⏸ 有 {len(inflight)} 条**正在校验/移动中**，本次跳过（复跑会自动带上）：")
        for st, n in collections.Counter(t["state"] for t in inflight).most_common():
            print(f"       {n:>4d}  [{st}]")
        print("       ★ 对它们 setLocation 会作废已完成的校验，且必然变成 missingFiles。"
              "\n         等校验跑完（stalledUP）再搬，那时是 rename，校验结果不作废。"
              "\n         要等它们，用： python scripts/wait-for-checks.py"
              "\n         ★ 判据是**这几条自己**不在途，不是「qB 校验队列空了」——"
              "\n           队列会反复被新注入的种子填上（cross-seed 还在跑批次），"
              "\n           而这道闸是**按种子**判的，所以复跑不必挑时机。见 SUMMARY §18.10。")
        ih = {t["hash"] for t in inflight}
        ts = [t for t in ts if t["hash"] not in ih]

    if not ts:
        print("\n本次没有要处理的 —— 都已在新根下，或都比完了。")
        return 0

    groups = group_by_savepath(ts)
    print("\n== 当前 save_path 分布（仅本次要处理的）==")
    for sp, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        cats = collections.Counter((t["category"] or "(无分类)") for t in items)
        flag = "" if sp.startswith(old_root) else "   ← ★ 不在旧根下！"
        print(f"  {len(items):>4d}  {sp}{flag}")
        print(f"        分类: {dict(cats)}")

    # ---- 2) 预检：任何一条不在旧根下都必须拦下来 -------------------------
    #     漏掉一条 = 一条种子的文件丢失。宁可停下来让人看一眼。
    outside = [t for t in ts if not (t["save_path"] + "/").startswith(old_root + "/")]
    if outside:
        print(f"\n[!!] 有 {len(outside)} 条种子的 save_path 不在 {old_root} 下，"
              f"本脚本不知道怎么处理它们：")
        for t in outside[:20]:
            print(f"     {t['save_path']}  |  {t['name'][:60]}")
        print("     → 先搞清楚这些是什么，再改 --old-root/--new-root 重跑。")
        return 1

    # ---- 3) 站分组 → 每个站一次 setLocation ---------------------------------
    #     500 条只有 3 个 save_path，所以是 3 次调用，不是 500 次。
    print("\n== 迁移计划 ==")
    plan = []
    for sp, items in sorted(groups.items()):
        np_ = new_path_for(sp)
        hashes = [t["hash"] for t in items]
        if args.limit:
            hashes = hashes[: args.limit]
        plan.append((sp, np_, hashes))
        print(f"  {len(hashes):>4d} 条  {sp}\n           → {np_}")

    if not args.apply:
        print("\n（预检结束，什么都没改。确认上面无误后加 --apply；"
              "第一次务必先 --limit 1 --all-tags --apply）")
        return 0

    # ---- 4) 落地：先 setLocation，再改分类 ---------------------------------
    print("\n== 执行 ==")
    rc = 0
    for sp, np_, hashes in plan:
        if not hashes:
            continue
        try:
            api(url, "/api/v2/torrents/setLocation",
                {"hashes": "|".join(hashes), "location": np_})
            print(f"  ✓ setLocation  {len(hashes):>4d} 条 → {np_}")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ setLocation 失败 {sp} → {np_}: {e}")
            rc = 1

    # 分类的 savePath 只影响**之后新增**的种子；已有种子的位置是上面
    # setLocation 干的。两边都改，新老才一致。
    #
    # ★ 2026-09-12：这里**不能**凭"调用没报错"就报成功，也不能凭"调用报错"就报失败。
    #   当天实测：刚下发完几百条 setLocation 之后，qB 4.6.5 对 editCategory 回
    #   **409 Conflict** —— 而它要设的就是**同一个值**（试跑时已经设过）。照字面打
    #   「✗ 失败」，操作者会以为分类没配对、白查一轮。所以两种结果都**回读实际值**
    #   再下结论：值对了就是成功，那个 409 只是噪音。
    #   同 §16.2.2 的教训：**别信调用的返回值，信回读到的状态。**
    want_ok = False
    try:
        api(url, "/api/v2/torrents/editCategory",
            {"category": CATEGORY, "savePath": new_root})
        want_ok = True
    except Exception as e:  # noqa: BLE001
        print(f"  · editCategory 报错（{e}）—— 回读确认实际值…")
    try:
        cats = json.loads(api(url, "/api/v2/torrents/categories")) or {}
        cur = (cats.get(CATEGORY) or {}).get("savePath")
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ 连分类列表都读不到: {e}")
        cur = None
    if cur == new_root:
        note = "" if want_ok else "（调用虽报错，但值本来就是对的）"
        print(f"  ✓ 分类 {CATEGORY} 的 savePath = {new_root} {note}")
    else:
        print(f"  ✗ 分类 {CATEGORY} 的 savePath 是 {cur!r}，应为 {new_root!r}")
        rc = 1

    # ---- 5) 回读验证 ------------------------------------------------------
    #     ★ 这里不假设 setLocation 是同步完成的：qB 是异步搬的，刚调用完
    #       可能还显示旧路径。回读只用来**展示**，不作为失败判据。
    print("\n== 回读（qB 异步搬，刚落完可能还没更新，属正常）==")
    ts2 = torrents(url)
    g2 = group_by_savepath(ts2)
    for sp, items in sorted(g2.items(), key=lambda kv: -len(kv[1])):
        mark = "✓" if sp.startswith(new_root) else "… 还在旧路径"
        print(f"  {mark} {len(items):>4d}  {sp}")

    print("\n下一步：等一两分钟再跑一次 --check 看是否全部落在新根下；"
          "\n        然后抽查几个种子的 state 是否回到 stalledUP（不是 missingFiles/error）。")
    return rc


if __name__ == "__main__":
    sys.exit(main())
