"""命令行入口。

子命令：
  preflight  只读预检：配置合法性、各任务同卷/单片枚举、qB 与 cross-seed 连通性。
  run        对启用的任务执行匹配→注入→等待→汇报（--job 只跑一个，--dry-run 不触发）。
  status     连 :3060，按分类列出当前单种的做种状态汇总。
  state      只读：从 sidecar 状态库读「登记了多少部 / 各阶段多少 / 还有多少待搜」。
  prestage   可选：把某任务的大包整体硬链接到 linkDir（参考脚本的加固版）。

用法（容器内默认读 /config/config.yml，可用 --config 或环境变量 RESEED_CONFIG 覆盖）：
  python -m orchestrator.main preflight
  python -m orchestrator.main run --job frds-top250-2024
  python -m orchestrator.main status
  python -m orchestrator.main state --pack frds-top250-2024
  python -m orchestrator.main prestage --job frds-top250-2024 --dry-run

★ `status` 与 `state` 是**两件不同的事**，名字像但不是一回事，别混：
    status ← qBittorrent 的**当下快照**（此刻这个分类里有多少种在做种）
    state  ← 我们自己的 **sidecar 状态库**（哪些片搜过、匹配到没、还欠多少）
  对不上是**正常且有意义**的：状态机说"没做种"而 qB 里在做种，正是 §17.5.1 那类 bug 的症状。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from . import hardlink, safety
from . import state as S
from .config import AppConfig, ConfigError, JobConfig, load_config
from .crossseed_client import CrossSeedClient, CrossSeedError
from .matcher import MatchRun, build_matcher
from .qbit_client import QbitClient, QbitError, classify

DEFAULT_CONFIG = os.environ.get("RESEED_CONFIG", "/config/config.yml")
# ★ 状态库的默认落点与 config.yml **同目录**（compose 里 `./hlink:/config`）。
#   可用 --db 或环境变量 RESEED_STATE_DB 覆盖。
DEFAULT_STATE_DB = os.environ.get("RESEED_STATE_DB", "/config/state.db")
log = logging.getLogger("reseed")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_preflight(cfg: AppConfig) -> int:
    print("=== 预检 ===")
    all_ok = True

    # 各任务：同卷/单片枚举
    for job in cfg.enabled_jobs():
        pf = safety.preflight_job(job.name, job.source_dir, cfg.matcher.link_dir,
                                  job.include, job.exclude)
        mark = "OK " if pf.ok else "!! "
        print(f"[{mark}] 任务 {job.name}")
        print(f"       源目录: {pf.source_dir}  (存在={pf.source_exists}, 同卷={pf.same_volume_as_link})")
        print(f"       识别到单片子目录: {pf.single_count} 个")
        if pf.single_count:
            preview = ", ".join(pf.singles[:5]) + (" ..." if pf.single_count > 5 else "")
            print(f"       示例: {preview}")
        for p in pf.problems:
            all_ok = False
            print(f"       - 问题: {p}")

    # 连通性：qBittorrent（硬性）
    print("--- 连通性 ---")
    qbit = QbitClient(cfg.qbittorrent.url, auth_mode=cfg.qbittorrent.auth_mode,
                      username=cfg.qbittorrent.username, password=cfg.qbittorrent.password)
    try:
        ver = qbit.connect()
        print(f"[OK ] qBittorrent {ver} @ {cfg.qbittorrent.url}")
    except QbitError as e:
        all_ok = False
        print(f"[!! ] qBittorrent 连接失败: {e}")

    # 连通性：cross-seed（软性，仅提示）
    if cfg.matcher.engine == "cross-seed":
        cs = CrossSeedClient(cfg.matcher.crossseed_url, api_key=cfg.matcher.crossseed_api_key)
        if cs.ping():
            print(f"[OK ] cross-seed 可达 @ {cfg.matcher.crossseed_url}")
        else:
            print(f"[?? ] cross-seed 暂不可达 @ {cfg.matcher.crossseed_url}（部署后再确认）")

    print("=== 预检结果:", "通过" if all_ok else "有问题需处理", "===")
    return 0 if all_ok else 2


def _print_run(run: MatchRun) -> None:
    print(f"--- 任务 {run.job_name} [{run.engine}] ---")
    print(f"    单片总数: {run.singles_total}  触发搜索: {run.searches_triggered}")
    print(f"    分类内: 基线 {run.baseline_count} → 新增 {run.new_count} "
          f"(做种 {run.seeding} / 校验中 {run.checking} / 报错 {run.errors})")
    if run.note:
        print(f"    备注: {run.note}")
    for sr in run.results:
        state = sr.state or "-"
        prog = f"{sr.progress*100:.0f}%" if isinstance(sr.progress, (int, float)) else "-"
        tag = "✓" if sr.matched_torrent else ("→" if sr.search_triggered else "·")
        matched = sr.matched_torrent or "(未对应到新种)"
        print(f"      {tag} {sr.single_name}  [{state} {prog}]  {matched}")


def cmd_run(cfg: AppConfig, job_name: str | None, dry_run: bool) -> int:
    matcher = build_matcher(cfg)
    jobs = [cfg.job(job_name)] if job_name else cfg.enabled_jobs()
    if not jobs:
        print("没有可运行的任务（检查 enabled）")
        return 2

    # 运行前做一次预检，任一硬问题则中止
    for job in jobs:
        pf = safety.preflight_job(job.name, job.source_dir, cfg.matcher.link_dir,
                                  job.include, job.exclude)
        if not pf.ok:
            print(f"[中止] 任务 {job.name} 预检未通过：")
            for p in pf.problems:
                print(f"   - {p}")
            return 2

    print(f"=== 运行{'（dry-run）' if dry_run else ''} ===")
    rc = 0
    for job in jobs:
        try:
            run = matcher.find_and_seed(job, dry_run=dry_run)
            _print_run(run)
            if job.iyuu_handoff:  # ★预留：本版本不触发 IYUU 扩散
                print("    (预留) iyuu_handoff=true，本版本不触发 IYUU 扩散")
        except (QbitError, CrossSeedError, NotImplementedError) as e:
            rc = 3
            print(f"[错误] 任务 {job.name}: {e}")
    return rc


def cmd_status(cfg: AppConfig, category: str | None) -> int:
    qbit = QbitClient(cfg.qbittorrent.url, auth_mode=cfg.qbittorrent.auth_mode,
                      username=cfg.qbittorrent.username, password=cfg.qbittorrent.password)
    try:
        qbit.connect()
        cat = category or cfg.qbittorrent.category
        torrents = qbit.torrents(category=cat)
    except QbitError as e:
        print(f"[错误] {e}")
        return 3

    buckets: dict[str, int] = {}
    for t in torrents:
        buckets[classify(t.get("state", ""))] = buckets.get(classify(t.get("state", "")), 0) + 1
    print(f"=== 分类 {cat!r} 状态汇总（共 {len(torrents)} 个种）===")
    for k in ("seeding", "checking", "downloading", "paused", "error"):
        if buckets.get(k):
            print(f"    {k}: {buckets[k]}")
    for t in torrents:
        prog = t.get("progress", 0) or 0
        print(f"    [{classify(t.get('state',''))} {prog*100:.0f}%] {t.get('name','?')}")
    return 0


def cmd_state(pack: str | None, db: str, indexers: str | None, include_cooldown: bool,
              cadence_days: int, cadence: str | None, want_trend: bool,
              verbose: bool) -> int:
    """只读汇报 sidecar 状态库。与 `status`（qB 快照）**无关**，见模块 docstring。"""
    # ★★★ 先判存在，再开库 —— 这一步**不能省**。
    #   `StateStore.__init__` 会 `mkdir(parents=True)` 再 `sqlite3.connect()`，
    #   而 sqlite 连一个不存在的路径**不会报错，会凭空建一个 0 字节的库**。
    #   实测踩过：打错一层路径，就在**媒体目录**里留下了一个空的 state.db，
    #   而它看起来"命令跑成功了"。只读命令**绝不能**有这种副作用。
    if not Path(db).is_file():
        print(f"[错误] 状态库不存在: {db}")
        print("       本命令**只读**，不会替你建一个空库（空库会伪装成「一部都没登记」）。")
        print("       库的真实位置由 drive-loop 的运行目录决定，常见的是：")
        print("         <compose>/drive-loop/hlink/state.db")
        print("       ★ 注意 compose 里 `./hlink:/config` 挂的是 <compose>/hlink，")
        print("         那是**编排器自己的配置目录**，里面只有 config.yml、没有状态库 ——")
        print("         所以**容器内默认的 /config/state.db 是不存在的**（要挂载才看得到）。")
        print("       在 NAS 上直接跑时，用：--db ../drive-loop/hlink/state.db")
        return 2

    idx = [s.strip() for s in indexers.split(",") if s.strip()] if indexers else None
    meanings = {
        S.STAGE_SEEDING: "已在 qB 里（终点）",
        S.STAGE_MATCHED: "匹配到单种，等 qB 确认",
        S.STAGE_UNMATCHED: "真搜过，但没匹配到",
        S.STAGE_SKIPPED: "★被退避跳过 —— 必须重搜",
        S.STAGE_PENDING: "还没搜过",
        S.STAGE_ERROR: "异常",
    }
    cad_by_idx = S.parse_cadence(cadence)

    with S.StateStore(db) as st:
        if pack:
            rows = [st.pack(pack)]
            if rows[0] is None:
                print(f"[错误] 未登记的包: {pack}（先 reseed-state.py init）")
                return 2
        else:
            rows = st.packs()
            if not rows:
                print("状态库里一个包都没有（先 reseed-state.py init）")
                return 0

        for p in rows:
            name = p["name"]
            sm = st.summary(name)
            print(f"=== 包 {name} ===")
            print(f"    源目录: {p['root']}")
            print(f"    登记 {sm['TOTAL']} 部")
            for k in S.ALL_STAGES:
                print(f"    {k:<13} {sm[k]:>5}   {meanings[k]}")

            pairs = st.todo_detail(name, indexers_now=idx,
                                  include_cooldown=include_cooldown,
                                  cadence_days=cadence_days,
                                  cadence_by_indexer=cad_by_idx)
            # ★★★ 口径必须**写在读数旁边**（`§26.65`；起因 `ERR-SVC-19` / 表 3 #5）。
            #   同一个系统有**两个"待搜数"**，实测差 **13 倍**（`state` 报 3 部，
            #   而下一批实际发了 40 条 webhook）—— ★ **两个数都对，量的是两件事**：
            #     · 本行（带 `--indexers`）  = **发信口径**：下一批会发几条
            #     · 不带 `--indexers`（库视角）= 「这个包还有几部没走完流程」
            #   ⇒ 只印一个数、不说它是什么口径，就会被拿去解释"批批 40 条"
            #     并推出「**在重复搜**」这个**错结论**（2026-09-19 真实发生）。
            if idx:
                print(f"    → 待搜索: {len(pairs)} 部"
                      f"（★ **发信口径**：按 `--indexers` = {','.join(idx)} 算；"
                      f"周期 每站 {cadence_days} 天）")
            else:
                print(f"    → 待搜索: {len(pairs)} 部"
                      f"（★ **库视角，不是发信口径** —— 没传 `--indexers`；"
                      f"周期 每站 {cadence_days} 天）")
                # ★ 这条**警告不能省**：库视角会漏掉「**从没搜过**的站」这一整格
                #   （`pool` 是 `sorted(indexer_seen)`，**没有时间戳的站根本不在池里**
                #   ⇒ 连"缺失"都看不见）。长期退避的站 / 刚加的站**只在这里隐身**。
                print("       ⚠ 这个数**不能**用来判「下一批会发几条 webhook」——")
                print("         想判发信口径：① 传 `--indexers <站名,站名>`；")
                print("         ② 或去读 `drive-loop.log` 里 `[第 N 轮] 计划 M 条`。")
                print("         ★ 两个数可以差很多（实测差过 13 倍），而**两个都对**：")
                print("           它们量的不是同一件事，别拿这个去推「在重复搜」。")
            if verbose:
                for r, due in pairs:
                    print(f"       [{r['stage']:<10}] 该搜: {','.join(due) or '-':<18}"
                          f" {r['dir_name']}")

        if want_trend:
            print()
            print(st.trend(weeks=8).render())
    return 0


def cmd_prestage(cfg: AppConfig, job_name: str | None, dry_run: bool) -> int:
    jobs = [cfg.job(job_name)] if job_name else cfg.enabled_jobs()
    rc = 0
    link_type = cfg.matcher.link_type
    # ★ 别让 reflink 的失败在几百个文件里静默累计：prestage 内部每个文件各自
    #   try/except，最后只汇总 failed 计数。同卷校验已在 prestage 里做；
    #   这里先把「这台机器/这个卷根本建不了这种链接」的硬错挡住。
    if link_type == "reflink" and os.name != "posix":
        print(f"[错误] link_type=reflink 需要 Linux(FICLONE ioctl)，当前平台是 {os.name}")
        return 3
    for job in jobs:
        try:
            hardlink.prestage(job.source_dir, cfg.matcher.link_dir,
                              link_type=link_type, dry_run=dry_run)
        except OSError as e:
            rc = 3
            print(f"[错误] 任务 {job.name} 预拆失败: {e}")
    return rc


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reseed-orchestrator", description="大包拆包·单种保种编排器")
    p.add_argument("--config", default=DEFAULT_CONFIG, help=f"配置文件路径（默认 {DEFAULT_CONFIG}）")
    p.add_argument("-v", "--verbose", action="store_true", help="调试日志")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("preflight", help="只读预检")

    pr = sub.add_parser("run", help="匹配→注入→等待→汇报")
    pr.add_argument("--job", help="只运行指定任务（默认全部启用的）")
    pr.add_argument("--dry-run", action="store_true", help="只枚举/汇报，不触发搜索与注入")

    ps = sub.add_parser("status", help="按分类汇总做种状态（连 qB，看当下快照）")
    ps.add_argument("--category", help="覆盖默认分类")

    pst = sub.add_parser("state", help="只读汇报 sidecar 状态库（不连网、不读 config.yml）")
    pst.add_argument("--pack", help="只看这一个包（默认列出全部）")
    pst.add_argument("--db", default=DEFAULT_STATE_DB,
                     help=f"sidecar 状态库路径（默认 {DEFAULT_STATE_DB}）")
    pst.add_argument("--indexers",
                     help="★ 当前生效的索引器名，逗号分隔 —— 传了才是**发信口径**"
                          "（= 下一批会发几条 webhook）。不传则报**库视角**"
                          "（= 这包还有几部没走完流程），两者实测差过 13 倍，别混")
    pst.add_argument("--include-cooldown", action="store_true",
                     help="把还在周期内的也算作待搜")
    pst.add_argument("--cadence-days", type=int, default=S.DEFAULT_CADENCE_DAYS,
                     help=f"每站的搜索周期天数（默认 {S.DEFAULT_CADENCE_DAYS}）")
    pst.add_argument("--cadence", help="按站覆盖周期，如 'SiteA=7,SiteB=30'")
    pst.add_argument("--trend", action="store_true", help="附上新增做种趋势（§16.3）")
    # ★ 用 --detail 而不是 -v/--verbose：顶层已经有一个 -v 了，子解析器再定义一个同名
    #   会把顶层那个**覆盖成默认值** —— `main -v state` 就会静默丢掉 -v。
    #   两个同名参数谁生效取决于写法，是那种"看起来能跑、偶尔不灵"的坑，直接换名。
    pst.add_argument("--detail", action="store_true", help="逐部列出待搜清单")

    pp = sub.add_parser("prestage", help="可选：把大包整体硬链接到 linkDir")
    pp.add_argument("--job", help="只处理指定任务")
    pp.add_argument("--dry-run", action="store_true", help="只打印将做的操作")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)

    # ★ `state` 在**读配置之前**分派：它只读本地那个 sidecar 库，既不连网也不碰 config.yml，
    #   没理由因为"配置文件坏了/不在"就跑不了 —— 出故障时恰恰最需要它还能用。
    if args.cmd == "state":
        return cmd_state(args.pack, args.db, args.indexers, args.include_cooldown,
                         args.cadence_days, args.cadence, args.trend, args.detail)

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"[配置错误] {e}")
        return 1

    if args.cmd == "preflight":
        return cmd_preflight(cfg)
    if args.cmd == "run":
        return cmd_run(cfg, args.job, args.dry_run)
    if args.cmd == "status":
        return cmd_status(cfg, args.category)
    if args.cmd == "prestage":
        return cmd_prestage(cfg, args.job, args.dry_run)
    return 1


if __name__ == "__main__":
    sys.exit(main())
