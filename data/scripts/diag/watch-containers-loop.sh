#!/bin/sh
# watch-containers-loop.sh —— 把 `watch-containers.sh` 包成**常驻循环**（容器入口）
#
# 为什么需要它
# ------------
# `watch-containers.sh` 是**一次性**的：跑一趟、报状态、退出。要它"每 10 分钟自己跑"
# 就得有个调度器。本仓原先的答案是 **DSM 计划任务** —— 但用户 2026-09-29 拍板
# **改成常驻容器**（不碰 DSM），于是需要这个循环入口。
#
# ★★ 为什么这是**可以**容器化的（而"在容器里拉起自己"不行）
# ----------------------------------------------------------
# 它跑在**自己的容器**（`reseed-watcher`）里，监视的是**另外四个**容器。
# 那四个停了 ⇒ 它**还在跑** ⇒ 它去拉起它们。★ 这与「自己拉起自己」无关。
#
# 唯一盖不住的形状是「**它自己也停了**」—— 那由 compose 的
# `restart: always` 兜（★ 注意：业务容器**不能**用 `always`，那会让
# `docker stop` 停不住；而**监视器**用它恰好对：**它就该停不住**）。
#
# 循环语义（★ 每一条都有理由，别随手改）
# --------------------------------------
#   · 跑一趟 `watch-containers.sh`，把它的退出码**记下来**，但**永不终止循环**。
#     理由：本进程的契约是"**一直在**"。一旦退出，`restart: always` 会重启它，
#     于是"某次 rc=1"变成"容器反复重启"—— 那是**把一次故障放大成重启风暴**。
#     ★ 判据：不管 rc 是 0/1/2，都继续下一轮。
#   · `rc=2`（查不了：没 docker / 读不出 daemon）**尤其**不能退出 ——
#     那是"**这台机器现在查不了**"，不是"我该死了"。下一轮可能就好了。
#   · 每轮之间 `sleep $INTERVAL`。间隔取 **600s（10 分钟）**：
#     告警有「同日同容器只投一次」的冷却 ⇒ 频率不影响告警量；而 drive-loop
#     一轮是**分钟级**，10 分钟足够覆盖「容器停了没人管」这个窗口。
#   · ★★ **每轮都打一行时间戳**（stdout，进 docker logs）。理由：
#     本容器最危险的失效是「**它自己在跑、但循环卡死了**」—— 那时容器
#     `restart: always` **不会**重启它（进程还在），而**没有任何读数会报**。
#     一行心跳是**唯一**能区分"在跑"与"卡住的假在跑"的东西。
#     （这正是选项 (C)「独立心跳」要补的洞 —— 本文件是它的**最小实现**。）
#
# ★ 退出码：本脚本**正常情况永不退出**（无限循环）。只有 `--once` 会退。
#
# 用法
# ----
#   sh watch-containers-loop.sh                       # 常驻（生产：容器入口）
#   sh watch-containers-loop.sh --once                 # 跑一趟就退（验证用）
#   sh watch-containers-loop.sh --interval 60          # 换间隔（秒）
#   sh watch-containers-loop.sh --dry-run              # 透传给 watch-containers.sh
#   WATCH_COMPOSE_DIR=/path sh watch-containers-loop.sh
#
# ★ 环境变量：
#   WATCH_COMPOSE_DIR  传给 watch-containers.sh 的 --compose（默认见下）
#   WATCH_INTERVAL     间隔秒数（默认 600）
#   WATCH_LOOP_SELFTEST=1  自测用：只跑 3 轮就退（钉循环语义）
set -u

# --------------------------------------------------------------------------- #
# 默认值 —— ★ 都是**实测过的 NAS 值**，不是猜的（2026-09-29 真机验证，见 §32.13）
# --------------------------------------------------------------------------- #
#   · 容器里 compose 目录用**与宿主完全相同**的路径（`/volume2/...`）：
#     因为本容器通过 `docker.sock` 让**宿主 daemon** 干活，而 compose 文件里
#     写的是**宿主路径**。挂到别的路径 ⇒ compose 解析出的挂载会指向错地方。
#   · `--compose` 的路径**不在容器里做存在性判断**：那是 watch-containers.sh
#     的守卫（且它必须看到"和宿主一样"的路径语义）。
DEFAULT_COMPOSE_DIR="${WATCH_COMPOSE_DIR:-/volume2/docker_ssd/reseed}"
# ★ 项目 B（qbittorrent-reseed）的 compose 目录 —— 2026-10-01 加。
#   它**不是**本项目栈，但 watch-containers.sh 现在也盯它（跨项目），故本循环要透传。
DEFAULT_COMPOSE_DIR_QB="${WATCH_COMPOSE_DIR_QB:-/volume2/docker_ssd/qbittorrent-reseed}"
DEFAULT_INTERVAL="${WATCH_INTERVAL:-600}"

SELF="$(dirname "$0")/watch-containers.sh"
[ -f "$SELF" ] || SELF="/app/scripts/diag/watch-containers.sh"

INTERVAL="$DEFAULT_INTERVAL"
ONCE=0
SELFTEST_LOOP="${WATCH_LOOP_SELFTEST:-0}"
PASSTHRU=""
# ★★ 2026-09-29（本机实测抓到的 bug）：`PASSTHRU` 里**不许再出现** `--compose`。
#   机理：本脚本每轮都会自己拼一个 `--compose "$DEFAULT_COMPOSE_DIR"`；
#   若调用方又透传一个 `--compose X`，则**命令行上有两个**，而
#   `watch-containers.sh` 的取词是"**最后一个赢**"（实测确认）
#   ⇒ 本脚本自己的默认值**被静默覆盖**，而报告头打印的却是被覆盖后的值 ——
#   看起来一切正常。★ 同族：`B.10` 第 14 条（读数一样、含义相反）。
#   ⇒ 处置：**吞掉**调用方的 `--compose`（记下它、自己再传一次），保证只有一个。
#   ★ 2026-10-01：`--compose-qb` 同族、同样处置（否则同一个坑在第二组上重演）。
HAVE_COMPOSE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --once)      ONCE=1 ;;
        --interval)  shift; INTERVAL="${1:-$DEFAULT_INTERVAL}" ;;
        --interval=*) INTERVAL="${1#*=}" ;;
        # ★ 显式接住 --compose / --compose=X，**不让它进 PASSTHRU**（见上）
        --compose)   shift; DEFAULT_COMPOSE_DIR="${1:-$DEFAULT_COMPOSE_DIR}"; HAVE_COMPOSE=1 ;;
        --compose=*) DEFAULT_COMPOSE_DIR="${1#*=}"; HAVE_COMPOSE=1 ;;
        # ★ --compose-qb 同族：接住并记下，最后**只重发一次**（防"两个 --compose-qb"）。
        --compose-qb)   shift; DEFAULT_COMPOSE_DIR_QB="${1:-$DEFAULT_COMPOSE_DIR_QB}"; HAVE_COMPOSE=1 ;;
        --compose-qb=*) DEFAULT_COMPOSE_DIR_QB="${1#*=}"; HAVE_COMPOSE=1 ;;
        # 其余参数**原样透传**给 watch-containers.sh（--dry-run / --selftest / …）
        *)           PASSTHRU="$PASSTHRU $1" ;;
    esac
    shift
done

# ★ 间隔合法性：非数字或 0 ⇒ 退回默认，并**说出来**（不静默）。
case "$INTERVAL" in
    ''|*[!0-9]*)
        echo "!! --interval 不是正整数（$INTERVAL）⇒ 退回默认 ${DEFAULT_INTERVAL}s" >&2
        INTERVAL="$DEFAULT_INTERVAL" ;;
    0)
        echo "!! --interval=0 ⇒ 退回默认 ${DEFAULT_INTERVAL}s（0 会变成忙等烧 CPU）" >&2
        INTERVAL="$DEFAULT_INTERVAL" ;;
esac

echo "=== watch-containers-loop.sh   $(date '+%Y-%m-%d %H:%M:%S') ==="
echo "    脚本     = $SELF"
echo "    compose  = $DEFAULT_COMPOSE_DIR"
echo "    compose-qb = $DEFAULT_COMPOSE_DIR_QB"
echo "    间隔     = ${INTERVAL}s"
echo "    透传参数 =${PASSTHRU}"
echo "    模式     = $([ "$ONCE" = 1 ] && echo '跑一趟就退' || echo '常驻')"
echo

ROUND=0
while :; do
    ROUND=$((ROUND + 1))
    # ★ 心跳行：容器日志里能一眼看出"轮次 + 时刻"（见文件头那条理由）。
    echo "---- 第 ${ROUND} 轮   $(date '+%Y-%m-%d %H:%M:%S') ----"

    # shellcheck disable=SC2086  # PASSTHRU 故意不加引号：要按空格拆成多个参数
    sh "$SELF" --compose "$DEFAULT_COMPOSE_DIR" --compose-qb "$DEFAULT_COMPOSE_DIR_QB" $PASSTHRU
    RC=$?
    # ★★ 退出码只**记录**，不据此退出（理由见文件头）。
    #    0 = 都在跑/已拉起；1 = 拉不起来（已投告警）；2 = 查不了。
    echo "---- 第 ${ROUND} 轮结束  rc=$RC  $(date '+%Y-%m-%d %H:%M:%S') ----"
    echo

    if [ "$ONCE" = 1 ]; then
        echo "★ --once ⇒ 退出（rc=$RC，原样带出）"
        exit "$RC"
    fi
    # 自测：跑满 3 轮就退，免得测试挂住
    if [ "$SELFTEST_LOOP" = 1 ] && [ "$ROUND" -ge 3 ]; then
        echo "★ WATCH_LOOP_SELFTEST=1 ⇒ 3 轮后退出"
        exit 0
    fi

    sleep "$INTERVAL"
done
