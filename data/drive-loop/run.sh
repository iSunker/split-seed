#!/bin/sh
# =====================================================================
# drive-loop 的 **NAS 侧**入口 —— 由 DSM 任务计划每 15 分钟唤醒一次
#
#   DSM → 控制面板 → 任务计划 → 新增 → 计划的任务
#     任务类型: 用户定义的脚本
#     用户    : root          （要读 compose 目录里的 .env）
#     计划    : 每天，频率「每 15 分钟」，首次 00:00 / 最后 23:59
#     脚本    : sh /volume2/docker_ssd/reseed/drive-loop/run.sh
#   ★ 建议不要勾「发送运行详情」—— 每 15 分钟一封邮件会淹掉真正的告警。
#     要看结果就 ssh 上去跑一次，或直接看下面那两个日志文件。
#
# 为什么需要这一层（为什么不直接把参数写进 DSM 任务的脚本框）
# ---------------------------------------------------------
# drive-loop.py 的默认值是为「从 Windows 经 SMB 驱动」写的。搬到 NAS 后有
# 几处不一样，集中放在这里，免得散进代码：
#   ① --env：脚本住在 <compose>/drive-loop/，而 .env 在**上一级**。
#      （cross-seed.db 和 info 日志不用管 —— drive-loop.py 自己会优先挑
#       NAS 原生路径，见那里的 CROSSSEED_DIRS）
#   ② --once：配合计划任务，一次唤醒只跑一批
#   ③ 环境：TZ 和 PYTHONIOENCODING —— 计划任务的执行环境是「干净」的，
#      没有登录 shell 那些变量，不显式给就会出怪事（详见下面各自的注释）
#   ④ 留痕：见下
#
# ★ 为什么除了 drive-loop.log 还要再记一份 attempts.log
#   drive-loop.log 只收 logging 模块的输出。如果 python **自己起不来**
#   —— 最可能就是 NAS 系统 python3 版本不对（SyntaxError / 注解求值 TypeError），
#   或者 PY 路径写错 —— 那 logging 根本没配置，drive-loop.log 一个字节都不会写。
#   表现是「任务跑了，但什么都没发生，也没有任何报错」。
#   ★ 这正是 Windows 上那两个坑的形状（<StopOnIdleEnd> / 包装器行尾，见 SUMMARY §14），
#     所以 shell 这一层必须自己留痕。四个信号分得很清楚：
#        没有 start 行  → 任务压根没被触发（任务没启用 / 计划窗口不对 / 脚本路径错）
#        有 start 无 exit → ★ **先别下结论**，有两种可能，必须区分开：
#                          ① 批次还在跑（正常）。50 部 × --interval 30s ≈ 25 分钟起，
#                             再加回灌前的 --settle 90s，跑上大半小时是常态。
#                          ② 进程树被强杀（Windows 上的 TerminateProcess）。
#                          区分方法 —— 看状态文件里的心跳在不在往前跳（每 60s 一次）：
#                            cat scripts/.drive-loop.state    # 隔 1 分钟再看一眼 heartbeat_ts
#                            心跳在涨 = 活着在跑；心跳冻住 = 残留/被杀。
#                          ★ 别用 `ps | grep drive-loop.py` 判断死活 —— BusyBox 的 ps
#                            只显示 comm（截断到 15 字符，也就是 "python3"），
#                            **看不到命令行参数**，怎么 grep 都是空的。
#                            （2026-09-12 凌晨在这上面白绕了一圈。）
#                            要按命令行查进程请用： pgrep -f drive-loop.py
#        有 exit 且 ≠ 0  → 看这个退出码。★ **120 是例外，别据此判批次失败。**
#                          CPython 退 120 只有一条路：`Py_FinalizeEx() < 0`，
#                          即**退出时刷新 std 流失败** —— 与批次成败无关。
#                          2026-09-13 实测：11:30 那轮 mbf **4/4 全部成功**
#                          （「发送完毕：成功 4 / 失败 0」+「--once 完成。」）**也退 120**。
#                          ⇒ **判批次成败看 drive-loop.log 的「--once 完成。」与批次统计，
#                            不看这个退出码。**
#                          分布（只报读数，成因未定）：09-12 全天 68 轮**无一个非 0**；
#                          09-13 从 09:15 起成片出现（跳过轮与批次轮都有，共 11 次），
#                          而同为跳过轮的 12:00 却是 0 —— 两轮日志逐行同构、耗时同档，
#                          **目前没有能把它们分开的判据**。别硬凑一个原因。
#
# 用法
# ----
#   sh run.sh                # 跑一批就退出（计划任务用的就是这条）
#   sh run.sh --dry-run      # 只看计划，不发请求
#   sh run.sh --limit 5      # 临时改小批量（调试用）
#   其余参数原样透传给 drive-loop.py；脚本里已给的参数如果在后面再给一次，
#   以你给的为准（argparse 取最后一次）。
# =====================================================================
set -eu

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
COMPOSE_DIR=$(dirname -- "$SELF_DIR")

#: python 解释器。DSM 自带的在 /usr/bin/python3（实测 3.8.15）。
#: drive-loop.py 顶部有 `from __future__ import annotations`，
#: 就是为它加的 —— 见该文件的说明。可用 PY=... 覆盖。
PY="${PY:-/usr/bin/python3}"
SCRIPT="$SELF_DIR/scripts/drive-loop.py"
ATTEMPTS="$SELF_DIR/attempts.log"

#: ★ 计划任务的执行环境没有 TZ，不显式给的话日志时间戳会是 UTC，
#:   看日志时得在心里减 8 小时 —— 排查时间线（"它几点死的"）时很容易搞错。
export TZ="${TZ:-Asia/Shanghai}"

#: ★ 让 python 的 stdout/stderr 强制 UTF-8。
#:   片名里全是中文，而 Python 在非 UTF-8 locale 下打印中文会直接
#:   UnicodeEncodeError **把进程打挂**（在 Windows 上已经踩过一次）。
#:   计划任务的 locale 不一定是 UTF-8，所以这里显式钉死。
export PYTHONIOENCODING=utf-8

if [ ! -x "$PY" ]; then
  echo "[!!] 找不到可执行的 python3：$PY" >&2
  echo "     先确认 NAS 上有没有：  ls -l /usr/bin/python3*" >&2
  echo "     没有的话见 README「NAS 侧一次性配置」里 python3 那段列的后备方案。" >&2
  echo "[$(date '+%F %T')] exit=127 (no python: $PY)" >> "$ATTEMPTS"
  exit 127
fi
if [ ! -f "$SCRIPT" ]; then
  echo "[!!] 找不到 $SCRIPT —— 镜像/挂载里的代码没同步好吗？" >&2
  echo "[$(date '+%F %T')] exit=127 (no script: $SCRIPT)" >> "$ATTEMPTS"
  exit 127
fi

echo "[$(date '+%F %T')] start  py=$PY" >> "$ATTEMPTS"

# ★ --once 配合计划任务：一次唤醒只跑一批。
#   跨进程节流 + 包轮换都在 drive-loop.py 的 once_round() 里，靠
#   scripts/.drive-loop.state 持久化。闸门有两项：
#     ① 上一批还在跑（pid + 心跳）→ 跳过
#     ② 距上次结束不足 max(30 分钟, 上一批算出的间隔) → 跳过
#   ★ ②这一项里「上一批算出的间隔」是 2026-09-12 接上的：站点被限流时
#     next_sleep() 会给 1.5~2 小时，这里就真的会等那么久（状态文件里的
#     last_sleep_sec）。在那之前它只是日志里的一行字，没人用。
#
# ★ 这里刻意**不写** --url / --qbit-url：
#   那两个默认值就是 NAS 的局域网 IP（http://192.168.0.7:2468 / :3060），
#   从 NAS 宿主机跑和从 Windows 跑走的是同一条路，不需要改。
#   重复写一份只会多一个「改了代码默认值但这边还是旧的」的漂移点。
#
# `"$@"` 放最后：用户在命令行给的参数覆盖上面的。
#
# ★ --indexers 是**手工维护**的名单，加了站却忘了改这里 = 新站永远搜不到。
#   状态机只认这份名单：名单里没有的站，对每部片子来说「那个站从没搜过」
#   这件事根本不会被表达出来 → 那些片子永远不会因为「来了新站」而被重新
#   排期，而 Prowlarr / cross-seed 那边看起来一切正常。2026-09-12 就是这么
#   卡住的：HDtime 早就在 Prowlarr(id=1) 和 .env 的 TORZNAB_URLS 里了，
#   但这里只有两个站，224 部 UNMATCHED 一部都没往新站上重搜。
#
# ★ 加站的正确顺序（缺一步都会静默错记）：
#     ① Prowlarr 里启用 + Test 通过
#     ② .env 的 TORZNAB_URLS 加上该站的 /N/api，然后
#        cd <compose 目录> && sudo docker compose up -d --force-recreate cross-seed
#        —— 容器不重建，env_file 的改动不会生效
#     ③ 确认 cross-seed.db 的 timestamp 表里开始出现该站的行。那是「真的搜
#        出去了」的硬证据，比翻日志靠谱（失败的搜索不会留下 timestamp 行）
#        —— 这一步别手敲 SQL：python scripts/diag/check-indexer-timestamps.py \
#             --expect <新名单>   （只读；退出 0 = 闸门开。--wait 可等下一批跑完）
#     ④ 最后才改这一行
#   ★ 顺序颠倒的代价：站还没通就把名字写进来 → 状态机把片子记成"在那站搜过
#     了"并压上 14 天冷却，实际一次都没发出去。而「来了新站」的触发是**一次
#     性**的（due_indexers 里「从没搜过 → 该搜」），白烧一次就得再等一个周期。
#   drive-loop.py 的 check_indexers() 会拿 cross-seed.db 做自检，
#   对不上时只在日志里吭一声，不拦。
#
# ★ HDtime（Prowlarr id=1）已**于 2026-09-12 17:00 加进名单**。四步全部走完：
#     ① ✅ 已在 Prowlarr 换新 cookie；`POST /api/v1/indexer/test` 返回 `{}`（= 通过，
#        失败会回 400 带 errorMessage）。
#     ② ✅ 已在生产 .env 的 TORZNAB_URLS 加回 `/1/api`（用 scripts/diag/add-torznab-indexer.py
#        —— apikey 从同文件现有条目**原样抄来**，与 PROWLARR_API_KEY 同一个值，
#        全程没有经过人眼；写回已做字节级校验），并于 16:29 `--force-recreate` 重建容器
#        —— 启动日志里 `http://prowlarr:9696/1/api failed to respond` 那一条**反而是**
#        「`/1` 已经进了容器」的证据（报错是按容器内实际的索引器列表逐个报的）。
#     ③ ✅ `cross-seed.db` 的 `timestamp` 表里 HDtime **已有 60 行**（本行改动前实测）。
#        `timestamp` 主键是 (searchee_id, indexer_id) 且**失败的搜索不记行** ——
#        有行 = 真的发出去并被应答了。复核命令（只读、只打计数与站名）：
#            python scripts/diag/check-indexer-timestamps.py \
#              --expect HDtime,HDFans,NanyangPT,BTSCHOOL
#        它顺带回答另一个常被问的问题：`indexer.status == RATE_LIMITED` **不代表**
#        还在被限流 —— 那个字段限流窗口过去后不会被擦掉。真判据是 `retry_after`
#        （epoch 毫秒）：2026-09-12 17:00 实测四个站的 retry_after **全部已过期**，
#        所以那三个 RATE_LIMITED 是**陈旧标记**（BTSCHOOL 的 status 已自己翻回 OK）。
#     ④ ✅ 就是下面这一行。
#   ★ 顺序颠倒的代价（上面「加站的正确顺序」已写）：站还没通就把名字写进来 →
#     状态机把片子记成"在那站搜过了"并压上 14 天冷却，实际一次都没发出去。
RC=0
"$PY" "$SCRIPT" \
  --once \
  --env "$COMPOSE_DIR/.env" \
  --indexers HDtime,HDFans,NanyangPT,BTSCHOOL \
  --limit 50 \
  "$@" || RC=$?

echo "[$(date '+%F %T')] exit=$RC" >> "$ATTEMPTS"
exit "$RC"
