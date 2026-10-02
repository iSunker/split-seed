#!/bin/sh
# =====================================================================
# `#58` drive-loop 的 **容器内常驻入口** —— 容器自带 `while` 循环（C 方案）
#
# 它和谁是什么关系
# ----------------
#   `drive-loop/run.sh`          （仓库里：`scripts/drive-loop-nas.sh`）
#       DSM 任务计划每 15 分钟唤醒一次的那份，**容器外**跑，带 `--once`。
#   `drive-loop/run-resident.sh` （仓库里：**本文件**）
#       ★ 容器**内**常驻跑的那份。**不带** `--once` —— 循环在 python 里，
#         DSM 那侧只管「容器还活着吗」。
#   `scripts/diag/drive-loop-docker.sh`
#       **已作废**的 `docker run` 草案（取代它的是 `compose.yaml` 里的
#       `drive-loop` 服务）。留着是为了留痕，不部署。见文件头。
#
# ★ 为什么另写一份而不是给 `run.sh` 加开关
# ----------------------------------------
#   `run.sh` 里 `--once` 是**硬编码**的，而且它**刻意不传** `--url` / `--qbit-url`
#   （理由写在那边：宿主机跑，默认值就是 NAS 的局域网 IP）。
#   容器里这两条**都错** —— 见下面「三处容器方言」。
#   给 `run.sh` 加条件分支 = 让"宿主版"和"容器版"共用一条会分叉的路；
#   另写一份 = 两条路各自平坦，谁都不用读对方的条件。
#   ★ 代价是 `--indexers` / `--limit` 这类参数有了**第二个声明点**，
#     这正是 2026-09-12 卡住 224 部片子的那个形状（站点加进 `run.sh` 却漏在这边）。
#     ⇒ 缓解办法只有一条：**改一处就改两处**，且 `tests/test_drive_loop_service.py`
#       里有一条断言钉 `--indexers` 的名单与 `run.sh` **逐字相同**。
#       改站名时两边一起改，测试会当场红。
#
# 三处容器方言（与 `run.sh` 的**全部**差别就这三条）
# ------------------------------------------------
#   (a) **不带 `--once`** —— 常驻就是不要它。带上它 = 跑一批就退出 = `restart:
#       unless-stopped` 会把它拉起来再跑一批，变成一个"每批重启一次"的怪东西，
#       而且 `--once` 那条路的**跨进程闸门**（`.drive-loop.state` 的 pid + 心跳、
#       以及 `last_sleep_sec` 的批次间隔）会被反复走一遍 —— 站点退避时算出来的
#       1.5~2 小时在这里会退化成"每批都跑"。
#   (b) **传** `--url http://cross-seed:2468 --qbit-url http://qbittorrent-reseed:3060`
#       —— 容器内必须**服务名直连**。群晖上「容器 → 宿主 LAN IP → DNAT 回另一容器」
#       这条回环实测 **timeout**（记录在 `compose.yaml` 头部与 README 坑 4）。
#       ★ TORZNAB 那侧**不用改**：`TORZNAB_URLS` 实测已经是
#         `http://prowlarr:9696/N/api?apikey=...`（服务名），照旧可用。
#   (c) `PY=python` / `SUDO=` —— 镜像（`python:3.12-slim`）里没有 `/usr/bin/python3`
#       （那是 DSM 自带的），python 在 PATH 上；也没有 `sudo`，而 `build-farm.sh`
#       用的是 `${SUDO-sudo}`，给空串它就直接跑那条命令（那步本来也 `|| true`）。
#       ★ 这两个由 `compose.yaml` 的 `environment:` 传进来，本脚本只做默认值兜底。
#
# ★ 留痕：`[resident]` 标记
# ------------------------
#   两条路**共用**同一个 `attempts.log`（都在 `<compose>/drive-loop/` 下）。
#   脚本自己留痕的那四个信号（没有 start / 有 start 无 exit / exit≠0 / exit=0）
#   在**常驻**模式下语义不同，必须能分清是谁写的：
#     · 常驻模式下一次 start/exit **不是"一轮批"**，而是"这个容器从起到死"。
#       ⇒ **别拿 exit 计数去数批次** —— 批次看 `drive-loop.log` 的「[第 N 轮]」。
#     · 常驻进程被 `docker stop` / 容器被杀 ⇒ 有 start 无 exit，这是**正常**的
#       （`unless-stopped` 重启后是**新的一对** start/exit）。
#     ⇒ 「钉一个全局不变量：**这一层只出站，不出兵**」——
#        grep 全文件，`--once` 只准出现在注释里，**绝不准出现在命令行上**。
#        测试就照这条钉（注释不算，靠的是"参数区段里没有它"）。
#   ⇒ `[resident]` / `[once]` 前缀让"谁写的"一眼可辨。
#     ★ `run.sh` 那侧**不改**（它在生产跑着，不改生产）。判别退化为
#       「有 `[resident]` = 容器版常驻；没有 = 宿主 `--once`」—— 这条不对称是
#       **有意接受**的，写在这里以免下一个人以为漏改了。
#
# ★ 这里**不设** `NOTIFY_SPOOL` / `--notify-spool`
# ------------------------------------------------
#   `scripts/notify.py` 的默认值就是 `<compose>/notify/spool`，而 compose 服务
#   把这个目录**整目录 1:1 挂进去**了 ⇒ 默认值**正好对**。
#   另设一个环境变量 = 多一个"改了代码默认值但这边还是旧的"漂移点（同 `run.sh`
#   里"刻意不写 --url"的理由）。
#
# ★ 与 `run.sh` **同样要守**的一条（原文抄来，别以为容器里就不适用）
# ---------------------------------------------------------------
#   `--indexers` 是**手工维护**的名单，加了站却忘了改这里 = 新站永远搜不到。
#   加站的正确顺序见 `run.sh` 头部那一大段（四步，缺一步都会静默错记）。
#
# 用法
# ----
#   sh run-resident.sh                # 常驻（compose 服务的 command 就是这条）
#   sh run-resident.sh --dry-run      # 只看计划，不发请求
#   sh run-resident.sh --limit 5      # 调试用
#   其余参数原样透传给 drive-loop.py；脚本里已给的参数在后面再给一次，以你给的为准
#   （argparse 取最后一次）。
#
# ★ 别在宿主机上跑本脚本 —— 它传的两个 URL 在宿主上是**解析不了的服务名**。
# =====================================================================
set -eu

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
COMPOSE_DIR=$(dirname -- "$SELF_DIR")

#: python 解释器。★ 容器里是 PATH 上的 `python`（镜像 `python:3.12-slim`）；
#: `/usr/bin/python3` 是 DSM 自带的，容器里**没有**。默认值给容器那个，
#: 宿主机上要跑就得显式 `PY=/usr/bin/python3 sh run-resident.sh`（不推荐，见文件头）。
PY="${PY:-python}"
SCRIPT="$SELF_DIR/scripts/drive-loop.py"
ATTEMPTS="$SELF_DIR/attempts.log"

#: ★ 容器的 TZ 由 compose 的 `environment:` 给（`TZ=Asia/Shanghai`），但常驻进程
#:   可能被 `docker exec sh` 之类的方式拉起来，那条路不走 compose 的 environment。
#:   所以这里**还是显式兜一次** —— 与 `run.sh` 同一个理由：时间戳必须是本地时间，
#:   否则排查"它几点死的"时要心算减 8 小时。
export TZ="${TZ:-Asia/Shanghai}"

#: ★ 让 python 的 stdout/stderr 强制 UTF-8。片名里全是中文，而 Python 在非 UTF-8
#:   locale 下打印中文会直接 UnicodeEncodeError **把进程打挂**。镜像的默认 locale
#:   是 POSIX（`C`）——**正是**会出问题的那种，所以这行在容器里比在宿主上更要紧。
export PYTHONIOENCODING=utf-8

if ! command -v "$PY" >/dev/null 2>&1; then
  echo "[!!] 找不到可执行的 python：$PY" >&2
  echo "     ★ 本脚本是**容器内**入口；宿主上请用 drive-loop/run.sh。" >&2
  echo "     容器里确认： docker compose run --rm --entrypoint sh drive-loop -c 'command -v python'" >&2
  echo "[$(date '+%F %T')] [resident] exit=127 (no python: $PY)" >> "$ATTEMPTS"
  exit 127
fi
if [ ! -f "$SCRIPT" ]; then
  echo "[!!] 找不到 $SCRIPT —— deploy.sh 同步过了吗？" >&2
  echo "[$(date '+%F %T')] [resident] exit=127 (no script: $SCRIPT)" >> "$ATTEMPTS"
  exit 127
fi

# ---- 闸 ⓪：读到的**是不是这一版代码**（2026-09-18 立，SUMMARY §26.15）----
# ★★ 为什么需要它 —— 一次真实的静默事故：
#   `drive-loop.py` 在**两个地方**各有一份：镜像里 `COPY` 的 `/app/scripts/` 那份，
#   与挂载进来的 `<compose>/drive-loop/scripts/` 那份。生产**应当**读挂载那份
#   （`$SCRIPT` 就是这么算的），而 2026-09-18 实测：Part B 的新渲染**从未生效** ——
#   全 7 天日报的 metrics 里 `packpct` 都是 0，三个位置的 `.py` 是**三个不同文件**
#   （镜像那份连 `PHASE_IDLE` 都没有）。
#   ⇒ 症状是"日报少了几个数"，**不报错、不退非零、日志一切正常** —— 最难查的那种。
#
# ★ 判据选 `PHASE_IDLE` 的理由：它同时是
#   ① 「镜像早于 §26.5」的充分标志（那次修复新加的常量，旧版**不含**它）；
#   ② 一个**纯字符串**判据 —— 不需要 python、不解析 JSON、不依赖任何外部命令
#      （本仓踩过"用 sed/grep 抠 JSON 静默抠错"，而这里连 JSON 都不用碰）。
#   ★ 这是**哨兵不是闸门**：它只证明"不是那一版很旧的"，不证明"是最新版"。
#     真判据仍是回读 md5（见 Dockerfile 里那两条纪律）。
#   ★ 用 `grep -q` 且取反 —— 注意本文件是 `set -eu`，`grep -q` 没找到时返回 1，
#     所以必须写在 `if` 条件里（`if` 的条件不受 `set -e` 约束），别写成裸语句。
if ! grep -q 'PHASE_IDLE' "$SCRIPT" 2>/dev/null; then
  echo "[!!] $SCRIPT 里没有 PHASE_IDLE —— 这是 §26.5 之前的旧版代码。" >&2
  echo "     ★ 不是「没同步」：文件在、但**版本旧**。多半是镜像里 COPY 的旧副本被读到了。" >&2
  echo "     ⇒ 重建镜像并 docker load 到 NAS（见 scripts/drive-loop.Dockerfile 顶部两条纪律），" >&2
  echo "       或核对挂载有没有盖住 /app。判据（NAS，单行，★ 只比 notify.py）：" >&2
  echo "       docker compose --profile drive-loop exec drive-loop md5sum /app/scripts/notify.py drive-loop/scripts/notify.py" >&2
  echo "       ★ 不要拿 drive-loop.py 的两份 md5 相比：镜像是 CRLF、挂载是 LF ⇒ 恒假（SUMMARY §26.56 四）" >&2
  echo "[$(date '+%F %T')] [resident] exit=3 (stale code: $SCRIPT)" >> "$ATTEMPTS"
  exit 3
fi

echo "[$(date '+%F %T')] [resident] start  py=$PY" >> "$ATTEMPTS"

# ★★ 前置闸门：**两条路不许同时在跑**（`§26.5` 前提 ②，README 坑 4）
# ---------------------------------------------------------------------
#   常驻与 `--once` **没有任何跨进程互斥**：`batch_alive()` 只看
#   `.drive-loop.state`，而两条路写的是**不同的东西**（一个 `mode=resident`、
#   一个不写 mode），**从状态文件上根本看不出有两个在跑**。
#   两边同时发 webhook ⇒ 一次 429 可能废掉几百条。
#
#   ⇒ 这一道是**把"人工记得先停 DSM 任务"变成"机器判定"**：
#     进常驻之前读一次状态，若 `mode` **不是** `resident` 而 `running_pid` 非空，
#     说明**另一条路正在跑** —— 直接退出，不抢。
#
#   ⇒ 本题的化简：**只看 `mode` 这一个键**。它是 `drive-loop.py` 里
#     **常驻分支独有**的（`once_round()` 写状态时**不带** mode）——
#     于是 `mode == "resident"` ⟺ 上一次是常驻。
#     ★ 判据的代价必须写明：**上次是常驻但已被杀**（收尾没跑到）⇒ 判"残留、接管"。
#       这是对的（它确实没在跑了 —— 容器死了）——**只要** `mode` 真的是常驻分支
#       写的。⇒ 所以下面**为真时拦、读不出来时也拦**，只放行"确证是常驻残留"与
#       "确证已收工"两种情形。
#
#   ★ 这条闸门**不是**万无一失的：它挡的是"**宿主 `--once` 那份还在跑**"这个
#     **已知**情形（`mode` 缺失或不是 resident）。它**挡不住**"两条常驻容器同时起来"
#     —— 那种情形要靠 compose 的单实例语义（同一 `container_name` 起不了两个）
#     与 `profiles` 锁（见 `compose.yaml` 里 `drive-loop` 服务那段）。
STATE="$SELF_DIR/scripts/.drive-loop.state"
if [ -f "$STATE" ]; then
    # 只读两个字段；用 python 而不是 grep/sed —— 状态文件是 JSON，
    # 而 `sed` 抠 JSON 在"键顺序变了 / 值被格式化"时会静默抠错（本项目已踩过一次）。
    OTHER=$("$PY" - "$STATE" <<'PYEOF' 2>/dev/null || echo "?"
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        st = json.load(fh)
except Exception:
    print("?")          # ★ 读不出来**必须**是个能被识别的第三种值，不是"没问题"
    raise SystemExit(0)
if st.get("running_pid") is None:
    print("idle")       # 已收工 ⇒ 放行
elif st.get("mode") == "resident":
    print("resident")   # 上一次常驻的残留（容器重启后到这儿）⇒ 放行，接管
else:
    print("once")       # ★ 宿主那份还没收工 ⇒ 拦下
PYEOF
)
    case "$OTHER" in
        idle|resident)
            : ;;                       # 放行
        once)
            echo "[!!] 检测到**另一个 drive-loop 还在跑**（.drive-loop.state 的 mode 不是 resident、而 pid 非空）。" >&2
            echo "     ⇒ 这多半是 DSM 任务计划里那个跑 drive-loop/run.sh 的任务**还没停**。" >&2
            echo "     ★ 两条路没有互斥闸门，同时发 webhook 一次 429 可能废掉几百条（README 坑 4）。" >&2
            echo "     ⇒ 先在 DSM → 控制面板 → 任务计划里停掉那个任务，再起本容器。" >&2
            echo "[$(date '+%F %T')] [resident] exit=3 (another driver alive: once)" >> "$ATTEMPTS"
            exit 3
            ;;
        *)
            echo "[!!] 状态文件读不出来（$STATE）—— 当作**不能确认**处理，不抢。" >&2
            echo "     ★ 「读不出来」不是「没问题」（B.10 第 14 条）：空输出/解析失败都不许当绿灯。" >&2
            echo "     ⇒ 先看一眼它是什么（只读）：  cat $STATE" >&2
            echo "       · 文件是**空的**（0 字节）⇒ 上次写盘写了一半就被 kill —— 删掉它再起。" >&2
            echo "       · 内容是**半个 JSON / 乱码** ⇒ 同上，删掉再起。" >&2
            echo "       · 但**先确认真没人跑**：  pgrep -f drive-loop.py" >&2
            echo "         （★ 别用 ps|grep —— BusyBox 的 ps 只显示 comm，怎么 grep 都是空的）" >&2
            echo "[$(date '+%F %T')] [resident] exit=3 (state unreadable)" >> "$ATTEMPTS"
            exit 3
            ;;
    esac
    echo "[$(date '+%F %T')] [resident] preflight ok (state=$OTHER)" >> "$ATTEMPTS"
else
    echo "[$(date '+%F %T')] [resident] preflight ok (no state file)" >> "$ATTEMPTS"
fi

# ★ 两个 URL **必须**给：容器里没有"宿主机默认值"这回事（见文件头 (b)）。
#   ★ 也**必须**不带 `--once`（见文件头 (a)）—— `drive-loop.py` 的常驻分支
#     靠它区分；带上就变成"跑一批退出"。
#
# ★★ `--pool`（`e404b1ca#6` ①，**用户 2026-09-21 拍：开**）
# ------------------------------------------------------------
#   跨包合池：`--limit` 从"**每包** 50"变成"**三包合起来** 50"（全局）。
#   ★ 为什么在这里开而不改 `drive-loop.py` 的默认值：它是**产品决定**
#     （额度怎么分配），不是纯优化 ⇒ 留在入口脚本里显式写，改默认值要人拍
#     （同 `drive-loop.py:2961` 那段注释的道理）。
#
#   ★ 实测收益（`§26.34` 一）：三包待搜 **565**，合池后前 50 **全是 frds 的 SKIPPED 欠账**
#     ⇒ 欠账 232 部约 **5 批**（~3.8 小时）清零，不再"轮到它才清"。
#
#   ★★ 代价（**必须写在这里**，别只写在 python 那侧）：
#     小包（`mbf` / `dc-collection`）**再也不会独占一整批** ⇒
#     它的读数是"**混在批里**"拿到的 —— 而 `e404b1ca#8` 的**试点期建议**判据
#     （`pilot_verdict`：看某包"跑了 N 批 / 发出多少次 / 产出多少"）**会因此变钝**：
#     该包不再有"自己的一批"，`rounds` 涨得比开池前慢，读数里混着别的包的片子。
#     ⇒ **不是判据错了，是它的输入变了**（同 `§26.33` 那个 30 倍的形状）。
#     ⇒ 若哪天要重新评估某个小包"要不要留在 PACKS_DEFAULT"，**先临时去掉 `--pool`
#       再采几批**，别拿混着的读数下结论。
#
# ★ 与 `run.sh`（DSM 那份，`--once`）**刻意不同**：那份**不带** `--pool`。
#   两条路本来就各跑各的（那份已停用，见 `§26.5.2`），且这一条是"容器常驻"专有。
#   ★ 但**参数有了第二个声明点就要有判据** —— 见 `tests/test_drive_loop_service.py`
#     钉 `--indexers` 的那条（本文件的 `--indexers` 与 `run.sh` 逐字相同）。
#
# ★★ `--limit` 从 **50 → 500**（2026-09-22，用户拍「调大 --limit（额度换速度）」）
# ------------------------------------------------------------
#   为什么是 50 挡事：合池下池子是「**每次从头取前 N**」、`--limit` 是**全局**的，
#   而全池欠账实测 **565** 部（`§26.34` 一）⇒ `50` 意味着**每一批都重跑池子的前 50**，
#   剩下的欠账**永远不会被取到**。★ 于是 `§26.34` 那笔"5 批清零"的收益
#   **只在第一批成立** —— 这是我此前把它当成"已解决"的误读。
#   500 ⇒ 一批=把全池清一遍，之后每批只剩**真正新到期**的片子。
#   ★ 这不是"把请求数抬 10 倍"：`ok` 是**发出多少条 webhook**（每条 1 次搜索），
#     取多少部片子由池子决定，池子空了就自然停。
#   ★ 代价：轮到"全轮空"那天，判定全完成要去数**空闲轮**（3 轮 × 45 分 ≈ 2.25h）；
#     上界由 `--max-rounds` 兜着。★ 想临时压回去：`sh run-resident.sh --limit 50`
#     （`"$@"` 在后面，argparse 取最后一次）。
#
# ★ `DRIVE_LIMIT` / `DRIVE_PACKS` 是**运行时可覆盖口**（compose 的 `environment:`
#   传进来，见 `docker-compose.yml`）。默认值两边一致；不给 env 就是这里的值。
# ★ 要临时换名单而不改代码：`DRIVE_PACKS=xxx` 传进容器即可（`--packs` 默认读它）。
#
# `"$@"` 放最后：用户在命令行给的参数覆盖上面的。
# ★ 要临时关掉合池：`sh run-resident.sh` 传不了"关"（`--pool` 是 store_true）
#   ⇒ 直接改这一行，或用 `docker compose run` 覆盖整条 command。
RC=0
"$PY" "$SCRIPT" \
  --url "http://cross-seed:2468" \
  --qbit-url "http://qbittorrent-reseed:3060" \
  --env "$COMPOSE_DIR/.env" \
  --indexers HDtime,HDFans,NanyangPT,BTSCHOOL \
  --limit "${DRIVE_LIMIT:-500}" \
  --pool \
  "$@" || RC=$?

echo "[$(date '+%F %T')] [resident] exit=$RC" >> "$ATTEMPTS"
# ★ 退出码语义（与 `run.sh` 那条"120 是例外"**不同**）：
#   常驻模式**正常收工**只有一条路 —— `--max-rounds` 到了（默认 0 = 无限 ⇒ 永不收工）。
#   ⇒ 所以这里**任何**退出码都值得看一眼，包括 0：
#     0    = 到 `--max-rounds` 了（有人显式给了它）；或 `--dry-run` 那一轮跑完。
#     ≠0   = 见 drive-loop.log 的 traceback。★ 120 仍然是"退出时刷 std 流失败"，
#            与批次成败**无关**（那条在 `run.sh` 头部有实测记录，这里同样适用）。
#   容器的 `restart: unless-stopped` 会把它拉起来 —— **这既是兜底也是陷阱**：
#   若退出原因是配置错（比如 URL 打不通），它会**每 10 秒重启一次**、刷满
#   attempts.log。⇒ 排查时先看 `docker ps` 的 STATUS 有没有 "Restarting"。
exit "$RC"
