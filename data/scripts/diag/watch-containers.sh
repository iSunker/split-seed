#!/bin/sh
# watch-containers.sh —— 补齐「**容器根本没被拉起来**」这个缺口（NAS 侧看护）
#
# 它在回答什么问题
# ----------------
# 2026-09-27 18:35–19:04 之间，`reseed-cross-seed` 与 `reseed-drive-loop` 先后停了，
# 之后 **27 小时没有任何重启记录**。事后的日报读起来像「没产出」，其实是「没跑」：
#   · `info.current.log` 末行 = `[server] Server stopped`（干净停机，不是崩溃）
#   · `drive-loop.log` 末行 = `等待 45.0 分钟后跑下一批` —— 该醒没醒
#   · `.drive-loop.state` 的 `heartbeat_ts` 停在 27 小时前
#   · `notify/log/` 里 **没有当天的 .tsv**（一批都没跑）
#
# ★★ 现有两套机制**都覆盖不到这一种**，本脚本专治它：
#   · `restart: unless-stopped`（compose）—— **只对「容器退出」生效**，
#     对「从未启动 / 被 stop 掉后没人拉」无效；
#   · `healthcheck` + `autoheal` 侧车 —— 判的是 `.drive-loop.state` 的心跳，
#     而**容器没起来 ⇒ 没有任何进程执行 healthcheck** ⇒ autoheal 无从得知。
#     （compose 里 `autoheal` 那段的注释已自认：「这条判据**查不出**「容器从没起来过」」。）
#   ⇒ 一句话：**autoheal 治「卡死」，本脚本治「没起」。** 两者不重叠、不打架（见下「动作分档」）。
#
# 动作分档（★ 这是与 autoheal 的边界，别越界）
# ------------------------------------------------
#   · `State.Status ∈ {exited, dead, created, paused}` 或**容器不存在**
#       ⇒ **可拉起**：`docker compose ... up -d`。这正是本脚本的存在理由。
#   · `State.Status == running` 但 health 非 healthy
#       ⇒ **只报，不动手**。那是 autoheal 的地盘（它会杀容器让 restart 策略接管）；
#         两边同时动手会**互相打架**（本脚本 up -d、autoheal 杀）⇒ 抖动。
#
# ★★ 为什么判据用 `docker inspect` 而**不是** `docker ps`
# ------------------------------------------------------
#   `docker ps` 会骗人：实测见过 `State=exited, ExitCode=137, Pid=0` 的容器
#   仍显示成 "Up"（见 `scripts/diag/rotate-crossseed-key.py` 里那段记录）。
#   权威字段是 `docker inspect` 的 `State.Status`；本脚本把它与 `State.Pid` /
#   `State.ExitCode` 一起报出来，好让看信的人一眼能定位。
#
# 规约（照 `chk58.sh` / `drive-loop-mount-selfcheck.sh` —— 本仓 NAS 侧脚本的模板）
# ------------------------------------------------------------------------------
#   · POSIX `sh`（DSM 计划任务用 BusyBox ash，不是 bash，见 ERR-OS-08）；
#   · 退出码：0 = 全过 / 1 = 有不通过（✗）/ 2 = 查不了（缺 docker / 缺目录）；
#   · 双计数器 `RC`（硬失败）+ `UNK`（查不出）—— **「查不出」绝不置绿**（B.10 第 14 条）；
#   · ★★ **给 shell 看的字符串里只用 ASCII 标点**，注释里也**不写反引号**：
#     dash/ash 的取词器会误解成对的全角括号与反引号，把 case/for 结构截断，
#     而报错出现在**下一段**（`chk58.sh` 的本机实测：报在第 102 行，真因在那一行）。
#   · `PYTHONIOENCODING=utf-8`（本脚本用 python heredoc 读 docker inspect 的 JSON）——
#     否则 GBK 控制台下 `✗` 行会让判据**在最该报的时候哑掉**（`summary/29.3` 的实测）。
#
# 用法（**在 NAS 上跑**：DSM → 控制面板 → 终端机和 SNMP，或 Container Manager「终端」）
# --------------------------------------------------------------------------------
#   sudo sh watch-containers.sh              # 真查；发现不在跑就拉起 + 投告警
#   sudo sh watch-containers.sh --dry-run    # 只查只报，**不拉起、不写 spool**
#   sh watch-containers.sh --selftest        # 本机自测（用假 docker，**只走 dry-run**）
#   sh watch-containers.sh --selftest-live   # 本机自测**真动作路径**（假 docker + DRY=0）
#   sudo sh watch-containers.sh --compose <dir>
#
# ★ 退出码语义：0 = 全都在跑（或已成功拉起）；1 = 有拉不起来的；2 = 查不了
#
# ★★ 盯哪些容器（**数量以 `CONTAINERS` + `CONTAINERS_QB` 两处声明为准，别在正文里抄**）
#   · **项目 A**（本项目 compose）：`restart: unless-stopped` 的 4 个常驻服务；
#   · **项目 B**（另一个 compose，`qbittorrent-reseed`）：1 个，2026-10-01 加。
#   ⇒ 共 5 个。为什么不盯 `reseed-orchestrator` / `reseed-autoheal` 见 CONTAINERS 处。
#
# ★★ 验证状态（2026-09-28，**别把本机验过当成已上线**）
#   ✅ 本机验过（假 docker，`--selftest-live`）：四种退出码 0/0/1/2 各有正例；
#      「读不出 / 空数组 / 有内容」三种读数长得不一样；冷却不重复投；事件文件被
#      **NAS 上那份真的 `notify-spool.sh`**（UNC 直读 `field_of`）逐字段解析通过。
#   ✅ 对着 **NAS 的 `compose.yaml`** 核过：本栈六个 `container_name` 里只盯
#      `restart: unless-stopped` 的**四个**常驻服务（另两个为何不盯见下面 CONTAINERS 处）；
#      ★ 2026-10-01 又加了**项目 B**的 `qbittorrent-reseed`（见 CONTAINERS_QB 处）。
#   ⛔ **还没上过 NAS** ⇒ 以下**未验**（都是真机才能证的）：
#      ① 真 `docker inspect` 的字段形状（`State.Status/Pid/ExitCode/Health.Status`）——
#         夹具是我按文档造的，可能与真 daemon 有出入；
#      ② `--profile drive-loop` 能否真把 `reseed-drive-loop` 拉起来、拉起来后是否真
#         变 healthy（夹具只是「换个文件」）；
#      ③ spool 投进 NAS 后**真的会被排空并发信**（本机只证到"字段能解析"这一步）；
#      ④ 计划任务下的 `$PATH` / `sudo` / 宿主时间（DSM 计划任务的 env 与交互 shell 不同）。
#   ⇒ 纪律：先在 NAS 上 `--dry-run` 看读数对不对，再手跑一次真的，**最后**才挂计划任务。
#
# ★★ 告警时机（**产品决定，已拍板**）：**只有「拉起之后仍然不在跑」才投告警**。
#   成功拉起 ⇒ **完全静默**（不发信、rc=0）。
#   · 理由：一次偶发停机被自动救回来后，发信只是噪音，且会训练运维忽略这封信。
#   · 代价（**认下来**）：容器「停过又起来了」这件事**事后收不到信** —— 只能从
#     rc / 计划任务日志看出来。若要事后有感知，看每日摘要里那段心跳。
#   · ★ 曾写错过：`for c in $STILL $NEED_UP` 会给**本来好好跑着**的容器也各发一封，
#     且 `drive-loop` 重复出现 ⇒ 同趟跑五遍。判据就是本仓那条「读数一样、含义相反」。
#
# ★ 本脚本**会写两个地方**（不是只读件 —— 别与 chk58.sh 的只读契约混为一谈）：
#     ① `docker compose ... up -d`（拉起容器）
#     ② `$NOTIFY_ROOT/spool/` 下投一个告警事件文件（交给「排空 spool」任务发信）
#   ★ 除此之外**不写任何文件**，也**不删/不改** spool 里已有的东西。
#   ★ 冷却状态用**文件名**记（见下），不写新的状态文件 —— 少一个要挂载的路径。

set -u

export PYTHONIOENCODING=utf-8

COMPOSE_DIR="${COMPOSE_DIR:-/volume2/docker_ssd/reseed}"
NOTIFY_ROOT="${NOTIFY_ROOT:-$COMPOSE_DIR/notify}"
# ★★ 2026-10-04：`NOTIFY=0` ⇒ **不往 spool 投任何告警**（拉起动作照做）。
#   起因：通知模块整体剥离（见本仓 `重构方案.md` §11）。**默认仍为 1** —— 保留写事件
#   的能力，这样 NAS 上只要还留着 `notify-spool.sh`，巡检告警就还发得出去；
#   等新通知项目接管后再在 compose 里把 `NOTIFY=0` 显式设上。
#   ★ 判据：`NOTIFY` 只管「发不发」，**不管「拉不拉」** —— 两者必须能分开取证，
#     否则"关告警"会连带把"自动拉起"也关掉（含义相反）。
NOTIFY="${NOTIFY:-1}"
DRY=0
SELFTEST=0
# ★ 自测用的假 docker（见 --selftest）。生产上必须是空串。
FAKE_DOCKER="${FAKE_DOCKER:-}"
# ★★ daemon 身份硬门（可选）。**默认空 = 不启用**（见下面 daemon 身份校验那一段）。
#   ★ 本行**必须在 set -u 下先声明** —— 真机实测栽过：忘了声明，
#     于是 `[ -n "$WATCH_EXPECT_DAEMON" ]` 触发 `unbound variable`，
#     脚本**在探完身份之后、判容器之前**直接死掉（而且是**静默**的：
#     退出码为 0，看日志只有一行报错）。⇒ 与 `FAKE_DOCKER` 同一处置。
WATCH_EXPECT_DAEMON="${WATCH_EXPECT_DAEMON:-}"

# ★★ 第二组容器：`qbittorrent-reseed` —— **不在**本项目（COMPOSE_DIR）的 compose 里，
#   它在**另一个 compose 项目**（自己的目录）。两个致命理由，所以**不能**并进上面
#   `CONTAINERS`：
#     ① 动作用 `cd "$COMPOSE_DIR" && docker compose up -d <服务名>`。qb 的服务不在
#        本项目里 ⇒ 会报 `no such service: qbittorrent-reseed`（就是 §32.3 ⑤
#        「容器名 vs 服务名」那条的同族，往上再错一层）。⇒ 必须在**它自己的目录**里 up。
#     ② 本项目那条 up 带 `--profile drive-loop`，而 qb 那栈**没有 profile**（标志无意义，
#        但会误导读的人以为用它管 profile）。
#   ⇒ 处置：**第二组变量 + 第二次 up -d**，而不是把名字塞进第一组。
#   ★★ 为什么值得盯 qb（2026-10-01 实测）：qb-reseed 于 11:48 被外部 SIGTERM 停掉后
#      **停摆 3h47m** 无人拉起 —— `restart: unless-stopped` **不覆盖外部 `docker stop`**。
#   ★★ 顺序即依赖：下面动作段**先** up 项目 B（qb），**后** up 项目 A —— 因为
#      `reseed-cross-seed` 启动时必须能登录 qB，否则它会 exit 0 后被 restart 拉起、
#      **每 ~5 分钟空转一次**（2026-10-01：154 条启动 banner、RestartCount=44）。
#      跨项目依赖 compose 的 `depends_on` 表达不了 ⇒ 由**本脚本的顺序**来编码。
#      ★ 这条顺序**必须**有断言钉住（见 tests/test_watch_containers.py），
#        只写在注释里 = 只是注释，不是保证。
COMPOSE_DIR_QB="${COMPOSE_DIR_QB:-/volume2/docker_ssd/qbittorrent-reseed}"
# ★ 项目 B 无 profile ⇒ 显式空串（不继承 COMPOSE_PROFILE）。别删这一行：
#   它让"为什么第二条 up 不带 --profile"在代码里可见，而不是靠读注释回忆。
COMPOSE_PROFILE_QB=""

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)  DRY=1 ;;
        --selftest) SELFTEST=1; DRY=1 ;;
        # ★★ `--selftest-live`：用假 docker，但**允许 DRY=0** —— 否则「拉起 + 复读 +
        #   投告警」这条**最要紧**的路径根本无法自测（`--selftest` 强制 DRY=1，
        #   而复读那段是 `[ "$DRY" = 0 ]` 才进的 ⇒ 永远走不到）。
        #   本机自测实测踩过：改了告警段的范围（`$STILL` 而非 `$STILL $NEED_UP`），
        #   却因为跑不出这条路径而**看不出对错** —— 正是本仓反复钉的「没验 = 不算过」。
        #   ★ 生产上**绝不能用**（它会真去 up -d）；只给本机自测用。
        --selftest-live) SELFTEST=1 ;;
        --compose)  shift; COMPOSE_DIR="${1:-}" ;;
        --compose-qb) shift; COMPOSE_DIR_QB="${1:-}" ;;
        --notify)   shift; NOTIFY_ROOT="${1:-}" ;;
        # ★ 关掉告警投递（拉起照做）。剥离通知模块后可用 `NOTIFY=0` 或本开关。
        --no-notify) NOTIFY=0 ;;
        # ★ 半角括号 + ASCII 标点，理由见文件头（dash 取词器会在全角括号上截断）。
        *) echo "未知参数: $1   (可用: --dry-run | --selftest | --selftest-live | --compose <dir> | --compose-qb <dir> | --notify <dir> | --no-notify)" >&2; exit 2 ;;
    esac
    shift
done

COMPOSE_DIR=$(printf '%s' "$COMPOSE_DIR" | tr -d '\r\n' | sed 's/[[:space:]]*$//')
COMPOSE_DIR_QB=$(printf '%s' "$COMPOSE_DIR_QB" | tr -d '\r\n' | sed 's/[[:space:]]*$//')
NOTIFY_ROOT=$(printf '%s' "$NOTIFY_ROOT" | tr -d '\r\n' | sed 's/[[:space:]]*$//')

# ★★ 机器守卫：本脚本要在 **NAS 上**跑。在开发机（Windows + Docker Desktop）上跑，
#    `docker inspect` 查的是**本机**的 daemon —— 那是一台与 NAS **毫无关系**的机器，
#    却会给出一个像真的读数（`chk58.sh` 本机自测时**实测撞上**这个假绿）。
#    ⇒ 判据三条：① 不许 Windows 盘符；② 不许多余的 UNC/反斜杠形状；③ 必须是绝对路径。
#       任一不满足 ⇒ 退出码 2（未验）。
#    ★★ 2026-09-28 补第 ②③ 条（**在真机上实测撞出来的洞**）：
#      原来的两条是 `[A-Za-z]:[/\\]*` + `/*`，而 **UNC 路径 `//NAS/share` 以 `/` 开头
#      ⇒ 它**通过了**「绝对路径」那条。实测（在开发机上拿 NAS 的 UNC 路径跑不带
#      --selftest 的版本）：
#        · `docker inspect` 打到的是**本机 Docker Desktop** ⇒ 四个容器全报「不存在」
#        · 于是脚本**真的执行了 `docker compose ... up -d`** —— 打到的是**本机的 daemon**
#          （Docker Desktop 回 `no such service: reseed-cross-seed` 才没造成实际改动）
#        · 还试图往 NAS 的 UNC 路径投告警
#      ⇒ 这就是本仓反复钉的「**假绿 / 打错机器**」，而且这次它**越过了守卫本身**。
#      真实的生产路径是 **`/volume2/...`（单斜杠）** ⇒ 把 `//` 判成非 NAS 路径是安全的。
#    ★★ 守卫**能**挡的与**挡不了**的（写清楚，别高估它）：
#       挡得了：Windows 盘符、UNC、反斜杠、相对路径 —— 四种「明显是开发机」的写法。
#       挡不了：一个**长得像 NAS 的绝对路径**。实测：在开发机上传
#         `--compose /volume2/docker_ssd/reseed` 会**通过**守卫，
#         然后 `docker inspect` 打到**本机 Docker Desktop** ⇒ 四个容器全报「不存在」
#         ⇒ 读数是「四个都没起」，而**真因是"问错了机器"**。
#       ⇒ 这是**读数一样、含义相反**的同一族（B.10 第 14 条），守卫靠**路径**判不出来
#         —— 路径就是生产路径。真要根治得**验 daemon 身份**（如比对 `docker info`
#         的 Name 是不是 iSunker-DS423），那是一个**设计改动**，本轮**没做**。
#       ⇒ 现阶段的兜底是**人的纪律**：只在这三种情形跑本脚本 ——
#         ① 真在 NAS 的 SSH/终端里（`hostname` 会回 iSunker-DS423）；
#         ② 计划任务里；
#         ③ 开发机上**必须**带 `--selftest` / `--selftest-live`（那两条豁免守卫）。
#    ★ 例外：`--selftest` 走假 docker，故意允许非 NAS 路径（它不碰真 daemon）。
if [ "$SELFTEST" = 0 ]; then
    # ★ 两组目录（本项目 + 项目 B）都要过同一守卫 —— 否则 `--compose-qb <UNC>`
    #   会成为绕过守卫的新入口（它能把一个未验的路径带进"第二次 up -d"）。
    for _cd_var in "$COMPOSE_DIR" "$COMPOSE_DIR_QB"; do
    case "$_cd_var" in
        [A-Za-z]:[/\\]*)
            echo "!! compose 目录看起来是 **Windows 路径**（$_cd_var）——" >&2
            echo "   本脚本要在 **NAS 上**跑（它读 NAS 的 docker daemon）。" >&2
            echo "   ⇒ 这是『未验』，不是通过。上 NAS 再跑。" >&2
            exit 2 ;;
        //*)
            echo "!! compose 目录看起来是 **UNC 路径**（$_cd_var）——" >&2
            echo "   ★★ UNC 是**开发机**看的路径（\\\\NAS\\share 那种），而 NAS 上的真路径是" >&2
            echo "      **单斜杠**的 /volume2/... 。带 UNC 跑，docker 命令会打到**本机 daemon**" >&2
            echo "      ⇒ 读数与 NAS 无关，却长得像真的（**实测撞过**：会真的执行 up -d）。" >&2
            echo "   ⇒ 这是『未验』，不是通过。上 NAS 再跑（用 /volume2/... 那种路径）。" >&2
            exit 2 ;;
        *\\*)
            echo "!! compose 目录里含**反斜杠**（$_cd_var）—— 这不是 NAS 上的 POSIX 路径。" >&2
            echo "   ⇒ 这是『未验』，不是通过。上 NAS 再跑。" >&2
            exit 2 ;;
        /*) : ;;
        *)
            echo "!! compose 目录不是绝对路径（$_cd_var）——" >&2
            echo "   ★★ 在开发机上它会拿**本机**的 docker 去查，得到一个与 NAS 无关的读数（实测撞过）。" >&2
            echo "   ⇒ 改传绝对路径（NAS 上是 /volume2/docker_ssd/reseed）。" >&2
            exit 2 ;;
    esac
    done
fi

RC=0
UNK=0
bad() { printf '  ✗ %s\n' "$*"; RC=1; }
# ★★ `down()` 与 `bad()` 长得一样（都是 ✗），但**不置 RC** —— 这是本脚本退出码语义的
#   关键（见文件头：0 = 全都在跑**或已成功拉起**；1 = 有**拉不起来**的）。
#   动机（本机自测实测）：容器停 → 检出 → `up -d` 成功 → 复读通过，这一趟是**成功**的，
#   可 `bad()` 早在「检出」那一步就把 RC 置了 1 ⇒ 收尾报「有不通过项」并 exit 1。
#   后果有两层，都不轻：
#     ① NAS 计划任务每 5 分钟跑一趟，每次都记成失败 —— 真失败时反而看不出（狼来了）；
#     ② 「已经处理好」与「拉不起来」在退出码上**长得一样**，运维没法按 rc 分流告警。
#   ⇒ 读数用 ✗（醒目），**但只有「没能修好」才置 RC**。修好与否在复读那段（line 383）判。
down() { printf '  ✗ %s\n' "$*"; }
ok()  { printf '  ✓ %s\n' "$*"; }
na()  { printf '  ─ %s\n' "$*"; }
# ★ `say`/`warn` 的 `|| true` 是强制的：2026-09-13 那场邮件风暴的机理就是
#   `set -e` + 一条打印语句写进满盘 ⇒ 整趟带走（288 封/天）。
#   本脚本**不用 set -e**，但保持同一纪律：打印绝不决定结果。
say()  { printf '  %s\n' "$*" || true; }
warn() { printf '[!] %s\n' "$*" >&2 || true; }

# ------------------------------------------------------------------ #
# 容器清单：项目 A（4 个）—— 项目 B 见下面 CONTAINERS_QB
# ------------------------------------------------------------------ #
# ★ 用**空格分隔的一行字符串**而不是数组 —— POSIX sh 没有数组，而 ash 的
#   `for x in a b c` 就够了，且与 chk58.sh 的风格一致。
# ★ 顺序：先两个「引擎」（坏了就完全不动），再两个「支撑件」。
#   报告里按这个顺序打，好读。
# ★★ 2026-09-28：为什么**只有这四个** —— 在 NAS 上对着 `compose.yaml` 逐条核过
#   （不是照文档抄的）。本栈共**六个** container_name，另两个**故意不盯**：
#     · `reseed-orchestrator` —— NAS 上是 `restart: "no"` 且**没有 `command:`**
#       ⇒ 它**本该是缺席的**（一次性 CLI，靠 `compose run --rm` 起）。
#       盯它的后果：每趟都报「不存在」⇒ 去 `up -d` ⇒ **假告警轰炸**，
#       而且它 `restart: "no"` ⇒ 拉起来也不会常驻，下一趟又报。
#       ⇒ ★ 这是本脚本最容易被"顺手补上"的一个错（四个 vs 六个看着像漏了）。
#       （机理解释已固化进 ENVIRONMENT.md `ERR-DOCKER-13` —— 别在这里复述。）
#     · `reseed-autoheal` —— 它**就是**处理 `unhealthy` 的那个侧车（见文件头「动作分档」）。
#       本脚本与它是**分工**，不是漏掉：它管卡死、本脚本管没起。
#   ⇒ 判据：**只盯 `restart: unless-stopped` 的常驻服务**（这四个正好是全部）。
CONTAINERS="reseed-cross-seed reseed-drive-loop reseed-prowlarr reseed-flaresolverr"
# ★★ 容器名 → **服务名** 的对照表（2026-09-29 真机实测撞出来的 bug，必读）。
#
#   踩的坑：本脚本原来把**容器名**（`reseed-flaresolverr`）直接喂给
#   `docker compose up -d`，而 compose 要的是 **服务名**（`flaresolverr`）⇒
#       no such service: reseed-flaresolverr
#   ⇒ **拉起永远失败**（rc=1），而且是**最不容易被自测抓到**的那种失败：
#     本机的假 docker 只把命令**记进日志**、**不校验服务名**，所以自测恒绿。
#     ★★ 这就是 §32.2 那条教训的**第二次**现场：**夹具比被测系统粗 ⇒ 看不见真 bug**。
#     这一次是**真机**替我抓出来的（`--dry-run` 看不出，真动作一跑就报）。
#
#   ★ 两个名字**不一样**，所以必须**成对**维护，别靠"去掉 reseed- 前缀"这种猜法
#     （那是隐式约定，将来改个名就静默失效）。
#   ★ 判据现场核对（NAS 2026-09-29）：
#       docker compose --profile drive-loop config --services
#       ⇒ autoheal / prowlarr / cross-seed / drive-loop / flaresolverr / reseed-orchestrator
#   ★ 格式：`容器名=服务名`，空格分隔。检测用**容器名**（`docker inspect` 报的就是它），
#     动作用**服务名**（compose 认的就是它）。
CONTAINER_SERVICE_MAP="reseed-cross-seed=cross-seed reseed-drive-loop=drive-loop reseed-prowlarr=prowlarr reseed-flaresolverr=flaresolverr"

# ★★ 第二组：项目 B（qbittorrent-reseed）。**容器名 = 服务名**（同名），
#   但仍**显式**写进对照表 —— 不做"去掉 reseed- 前缀"那类猜测（同上一条理由）。
#   NAS 现场核对（2026-10-01）：
#       docker inspect qbittorrent-reseed --format '{{.Config.Labels}}'
#       ⇒ com.docker.compose.service = qbittorrent-reseed   （= 容器名）
CONTAINERS_QB="qbittorrent-reseed"
CONTAINER_SERVICE_MAP_QB="qbittorrent-reseed=qbittorrent-reseed"

#: 容器名 → 服务名。查不到就原样返回（宁可按原名试，也不要静默发一个空命令）。
svc_of() {
    for pair in $CONTAINER_SERVICE_MAP; do
        case "$pair" in
            "$1"=*) printf '%s' "${pair#*=}"; return 0 ;;
        esac
    done
    printf '%s' "$1"
}

#: 项目 B 的容器名 → 服务名。与 `svc_of` 分开，因为**两张表必须各自维护**：
#   合并成一张会让"这个容器属于哪个项目"这件事从代码里消失（而它正是要编码的东西）。
svc_of_qb() {
    for pair in $CONTAINER_SERVICE_MAP_QB; do
        case "$pair" in
            "$1"=*) printf '%s' "${pair#*=}"; return 0 ;;
        esac
    done
    printf '%s' "$1"
}
# ★ 哪个容器需要 `--profile drive-loop` 才在 compose 里可见：
#   `reseed-drive-loop` 与 `reseed-autoheal` 带 `profiles: ["drive-loop"]`
#   ⇒ 不带 profile 的 `up -d` **看不到它们**（compose 会报 "no such service"）。
#   本脚本用一条**带 profile 的**命令同时照顾两类（profile 不影响不带 profile 的服务）。
COMPOSE_PROFILE="drive-loop"

# ★★ 报告头**故意放到 daemon 身份解析之后**（见下面"daemon 身份校验"那一段）——
#   否则这里只能打「（未验）」，而那一行**本来的用途**就是"翻日志时一眼看出
#   这次问的是哪台机器"。第一次写在前面，自测里就打出 `daemon=（未验）`，
#   把判据 `"未验" not in stdout` 弄红了 —— 也说明它**确实没写上真值**。

# ------------------------------------------------------------------ #
# 工具探测
# ------------------------------------------------------------------ #
DOCKER=""
if [ "$SELFTEST" = 1 ]; then
    DOCKER="$FAKE_DOCKER"
    if [ -z "$DOCKER" ] || [ ! -x "$DOCKER" ]; then
        echo "!! --selftest 需要 FAKE_DOCKER 指向一个可执行的假 docker" >&2
        exit 2
    fi
    say "[selftest] 用假 docker: $DOCKER"
elif command -v docker >/dev/null 2>&1; then
    DOCKER="docker"
elif command -v /usr/local/bin/docker >/dev/null 2>&1; then
    # 群晖的 Container Manager 有时只把它放在这里
    DOCKER="/usr/local/bin/docker"
else
    na "没有 docker 命令 ⇒ 本脚本**未验**（不是通过）"
    echo "──" >&2
    echo "★★ 没有 docker ⇒ 这是『查不了』，**不是**『容器都在跑』（B.10 第 14 条）。" >&2
    exit 2
fi

# ★★ daemon 身份校验（2026-09-29 加；这是 §32.8 那条「没修的边界」的修法）。
#
#   为什么需要：上面那道**路径形状**守卫**挡不住**「长得像 NAS 的绝对路径」——
#   在开发机上传 `/volume2/docker_ssd/...`（**这就是生产路径**）会过关，
#   然后 `docker inspect` 打到**本机 Docker Desktop** ⇒ 四个全报「不存在」
#   ⇒ 读数是「四个都没起」，真因是「问错了机器」。守卫靠路径**永远**判不出来。
#
#   ★ 判据改用 **daemon 自报的身份**（与路径完全独立的一维）。实测两端（2026-09-29）：
#       开发机：  docker info --format '{{.Name}}' ⇒ docker-desktop
#                 .OperatingSystem                  ⇒ Docker Desktop
#       NAS：     .Name                             ⇒ iSunker-DS423
#                 .OperatingSystem                  ⇒ Synology NAS
#
#   ★★ 判据的方向（关键）：**只拒「能确证是另一台机器」的**，不要求「必须是 NAS」。
#      理由：这个值我**只在 NAS 上实测过一次**，若哪天 DSM 改了名/返回空，
#      「要求 == iSunker-DS423」会把**生产也挡掉**（每 10 分钟计划任务全 rc=2 =
#      静默拒绝服务，比原来的洞更糟）。⇒ 反过来判，只拦**已知的开发机 daemon**。
#   ★ 读不出（命令失败/空）⇒ **放行 + 留痕**，不拒。读不出 ≠ 是开发机。
#   ★ 想收紧：设 `WATCH_EXPECT_DAEMON=iSunker-DS423` ⇒ 变成硬门（上 NAS 验过之后）。
#
#   ★ 顺带：`.Name`/`.OperatingSystem` 里的**子串匹配**很关键 —— `docker-desktop`
#     与 `Docker Desktop` 大小写/形状都不同，所以两条都要列。
KNOWN_FOREIGN_DAEMONS="docker-desktop|Docker Desktop"
DAEMON_ID=""
DAEMON_VERDICT="unknown"
# ★★ 别用 `$SELFTEST` 当这里的闸门 —— 那会让 **`--selftest-live` 也跳过身份校验**，
#   而那个模式的**用途**恰恰是"用假 docker 走真路径"。
#   第一次就是这么写的，结果判据**在自测里根本走不到**（测出来恒 rc=0）——
#   ★ 这与 §32.5 那个"最要紧的路径自测永远走不到"是**同一个病**，别再犯。
#   ⇒ 闸门用 **DRY**：`--selftest`（=纯 dry-run，不碰 daemon）才跳；
#     `--selftest-live`（DRY=0，假 docker 会答 `info`）**必须**走校验。
if [ "$DRY" = 0 ]; then
    DAEMON_ID=$("$DOCKER" info --format '{{.Name}}|{{.OperatingSystem}}' 2>/dev/null | tr -d '\r\n')
    if [ -z "$DAEMON_ID" ]; then
        # ★ 读不出 ⇒ **不拒**（否则会误挡生产），但要说出来。
        DAEMON_VERDICT="unreadable"
        na "daemon 身份**读不出**（docker info 失败）—— 不能据此断定在 NAS 上（留痕，不拦）"
    else
        OLDIFS=$IFS; IFS='|'
        for probe in $KNOWN_FOREIGN_DAEMONS; do
            case "$DAEMON_ID" in
                *"$probe"*)
                    echo "!! daemon 身份是 **$DAEMON_ID** —— 这是**开发机**的 Docker（$probe）。" >&2
                    echo "   ★★ 本脚本要在 **NAS 上**跑：现在这条命令会打到**本机 daemon**，" >&2
                    echo "      而容器全在 NAS 上 ⇒ 读数会是「四个都不存在」——**看着像真的**。" >&2
                    echo "      （真实生产路径也长得像 NAS 路径 ⇒ 路径守卫拦不住它，只能靠这一维。）" >&2
                    echo "   ⇒ 这是『未验』，不是通过。上 NAS 再跑（或改用 --selftest / --selftest-live）。" >&2
                    exit 2 ;;
            esac
        done
        IFS=$OLDIFS
        # ★ 可选硬门：设了 WATCH_EXPECT_DAEMON 才要求相等。
        if [ -n "$WATCH_EXPECT_DAEMON" ]; then
            case "$DAEMON_ID" in
                "$WATCH_EXPECT_DAEMON"*) DAEMON_VERDICT="expected" ;;
                *) echo "!! daemon 身份与 WATCH_EXPECT_DAEMON **不符**：" >&2
                   echo "      期望：$WATCH_EXPECT_DAEMON" >&2
                   echo "      实读：$DAEMON_ID" >&2
                   echo "   ⇒ 这是『未验』，不是通过（硬门是显式设的，收紧是你的选择）。" >&2
                   exit 2 ;;
            esac
        else
            DAEMON_VERDICT="not-foreign"
        fi
    fi
fi

# ★ 报告头放在这里（daemon 身份已解析）—— 见上面 COMPOSE_PROFILE 后那段注释。
echo "=== watch-containers.sh   $(date '+%Y-%m-%d %H:%M:%S')   dry_run=$DRY ==="
echo "    COMPOSE_DIR=$COMPOSE_DIR"
echo "    COMPOSE_DIR_QB=$COMPOSE_DIR_QB"
echo "    NOTIFY_ROOT=$NOTIFY_ROOT"
echo "    NOTIFY=$NOTIFY   (0=不投告警，只上屏；拉起动作不受影响)"
# ★ daemon 身份也打出来 —— 事后翻日志时能一眼看出"这次问的是哪台机器"。
#   （没它就是 §32.8 那个洞：读数是「四个都没起」，而真因是「问错了机器」。）
if [ -n "$DAEMON_ID" ]; then
    echo "    daemon=$DAEMON_ID"
else
    echo "    daemon=（未验：$DAEMON_VERDICT）"
fi
echo

# compose 命令：能 `docker compose`（插件）就用它，否则退回 `docker-compose`（老版独立二进制）。
# ★ 群晖 DSM 7 两种都可能出现，所以**两种都探**，别假设。
COMPOSE_CMD=""
if "$DOCKER" compose version >/dev/null 2>&1; then
    COMPOSE_CMD="compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
fi

# compose 文件在不在（拉起要用它；查状态不用）。
COMPOSE_FILE=""
for f in "$COMPOSE_DIR/compose.yaml" "$COMPOSE_DIR/docker-compose.yml"; do
    if [ -f "$f" ]; then COMPOSE_FILE="$f"; break; fi
done
# 项目 B 的 compose 文件（同样两种名字都探 —— 项目 B 用的是 docker-compose.yml）。
COMPOSE_FILE_QB=""
for f in "$COMPOSE_DIR_QB/compose.yaml" "$COMPOSE_DIR_QB/docker-compose.yml"; do
    if [ -f "$f" ]; then COMPOSE_FILE_QB="$f"; break; fi
done

# ------------------------------------------------------------------ #
# 判据：一次 inspect 拿全部容器（项目 A + 项目 B）
# ------------------------------------------------------------------ #
# ★★ 用 python 读 `docker inspect` 的 JSON，**不用 sed/grep 抠** ——
#   本仓踩过一次「sed 抠 JSON 静默抠错」（见 compose 里 healthcheck 那段注释）。
#   python 是 DSM 自带的；`PYTHONIOENCODING=utf-8` 已在顶部导出。
PY=""
if command -v python3 >/dev/null 2>&1; then PY="python3"
elif command -v python >/dev/null 2>&1; then PY="python"
fi
if [ -z "$PY" ]; then
    na "没有 python ⇒ 读不了 docker inspect 的 JSON ⇒ **未验**"
    echo "──" >&2
    echo "★★ 没有 python ⇒ 这是『查不了』，不是『没问题』。" >&2
    exit 2
fi

# ★ 输出格式（每容器一行，TAB 分隔）：
#     name <TAB> status <TAB> running <TAB> health <TAB> pid <TAB> exitcode
#   status ∈ running / exited / dead / created / paused / restarting / missing
#   health ∈ healthy / unhealthy / starting / none
#
# ★★ 三种结果必须在输出上**长得不一样**（本仓反复钉的「零命中既可能是没事、
#    也可能是没跑」，B.10 第 14 条）：
#     ① `docker inspect` **失败**（daemon 没起 / 没权限 / 没装）
#        ⇒ python 打一行哨兵 `!INSPECT-UNREADABLE` ⇒ 外壳判 **rc=2（未验）**。
#     ② inspect 成功但**返回空数组**（容器确实一个都没创建）
#        ⇒ 每容器一行 `missing` ⇒ 外壳判 **rc=1（确实不在跑，且该拉起）**。
#     ③ inspect 成功且有内容 ⇒ 逐容器判。
#   ★ 旧版把 ① 也当 `json.load` 失败 ⇒ `arr=[]` ⇒ 打印四行 `missing`
#     ⇒ 于是「**读不到 daemon**」被念成「**四个容器都没了**」，
#     还会去 `up -d` 一遍 —— 这正是"读数一样、含义相反"（ERR-AI-09）。
#   ★ `docker inspect` 的退出码**单独接**：管道会把左端 rc 丢掉，所以用 `{ ... ; }`
#     记下来（POSIX sh 没有 PIPESTATUS）。
INSPECT_OUT=$("$DOCKER" inspect $CONTAINERS $CONTAINERS_QB 2>/dev/null | "$PY" -c '
import json, sys
# ★★ 必须用 `sys.argv[1:]`（**列表**），不是 `sys.argv[1].split()`。
#   本机自测时实测栽过：`python3 -c "..." a b c d` 时 argv = ["-c", "a", "b", "c", "d"]，
#   于是 `argv[1].split()` 只得到 **["a"]** ⇒ 只查了**第一个**容器，
#   其余三个**看都没看**，且它们**根本不会出现在输出里**（不是报 missing）。
#   ⇒ 症状是"四个里只报了一个"，看着像"其它三个都正常" —— 正是本仓反复钉的
#     "看着绿、其实什么也没验"。⇒ 用 argv[1:] 整段取。
want = sys.argv[1:]
raw = sys.stdin.read()
try:
    arr = json.loads(raw)
except Exception:
    # ★ 读不动 ⇒ 打哨兵，**不是**打印 missing（见上面 ①②③）。
    print("!INSPECT-UNREADABLE"); raise SystemExit(0)
if not isinstance(arr, list):
    print("!INSPECT-UNREADABLE"); raise SystemExit(0)
by = {c.get("Name", "").lstrip("/"): c for c in arr if isinstance(c, dict)}
for name in want:
    c = by.get(name)
    if not c:
        print("%s\tmissing\tnone\tnone\t0\t0" % name); continue
    st = (c.get("State") or {})
    h = ((st.get("Health") or {}) or {}).get("Status") or "none"
    print("%s\t%s\t%s\t%s\t%s\t%s" % (
        name, st.get("Status") or "unknown",
        "1" if st.get("Running") else "0",
        h, st.get("Pid") or 0, st.get("ExitCode") if st.get("ExitCode") is not None else 0))
' $CONTAINERS $CONTAINERS_QB 2>/dev/null)

# ★★ 空集守卫 + 哨兵守卫：**读不到**与「全都在跑」必须**长得不一样**。
#   ★ 两组容器**共用**这一次 inspect + 这一个守卫 —— 若第二组单独 inspect，
#     它读空时会被静默当成"没有要处理的"，从而 rc=0 而 qb 其实不在（"看着绿"）。
if [ -z "$INSPECT_OUT" ] || [ "$INSPECT_OUT" = "!INSPECT-UNREADABLE" ] \
   || printf '%s\n' "$INSPECT_OUT" | grep -q '^!INSPECT-UNREADABLE$'; then
    na "docker inspect 读不出内容 ⇒ **未验**"
    echo "──" >&2
    echo "★★ 读不到读数 **不是**「四个都在跑」。可能是：docker 没权限（要 sudo）、" >&2
    echo "   daemon 没起来、或容器名对不上。手工核对： docker ps -a" >&2
    exit 2
fi

# ------------------------------------------------------------------ #
# 逐容器判 + 收集要拉起的
# ------------------------------------------------------------------ #
echo "--- 容器状态 ---"
NEED_UP=""
N_BAD=0
# ★★★ 2026-10-03 加：**daemon 本身不可用** 与 **容器停了** 必须分开
#   （`§32.17` 那个真 bug —— 生产实测踩到）。
#   现场：2026-10-03 00:13:54 `dockerd` 重启（ContainerManager 重载，
#   **NAS 没重启** —— uptime 1d20h 为证），全部容器一起 `exited`。而本脚本
#   那一刻 `docker info` 失败 ⇒ `DAEMON_ID` 空、`DAEMON_VERDICT=unreadable`，
#   且 `docker inspect` 对**每个**容器回 `missing` ⇒ 五个全判成「容器根本没创建」。
#   ⇒ 告警文案说「已尝试拉起、**复读仍不在跑**」—— **与事实不符**：
#     没有任何东西"试过又失败"，是 **daemon 不在**，`up -d` 根本没意义。
#   ⇒ 读到这条告警的人会去 `docker logs <容器>`（信里就这么写的），而真因在 daemon。
#   ★ 判据：`DAEMON_VERDICT=unreadable` 时**整趟改判为「未验」**，
#     只投**一条** daemon 级告警（key 与逐容器告警不同），**不**投那五条错的。
DAEMON_DOWN=0
if [ "$DRY" = 0 ] && [ "$DAEMON_VERDICT" = "unreadable" ]; then
    DAEMON_DOWN=1
fi

# ★★ 这个 `while read` 的输入走 **heredoc**，循环体里又调了 `"$PY"` ——
#   而那次调用的 stdin 是**继承来的**（= 这个 heredoc）。python 一读 stdin
#   就会把**剩下的行全吃掉** ⇒ 循环只跑第一遍，后面三个容器**静默消失**。
#   本机自测实测过这个形状（三个名字只处理了第一个）。
#   ⇒ 两条纪律，缺一不可：
#     ① 循环体里任何读 stdin 的命令都要显式接 `< /dev/null`（或用它**自己的**管道）；
#     ② 这里的 heredoc 是**最后一道**防线 —— 上面 `INSPECT_OUT` 已由**管道**喂给 python
#        （不是 stdin），所以这里安全。谁要是把它改成 `"$PY" - <<PY`，就会踩上面那条。
while IFS='	' read -r name status running health pid ecode; do
    [ -n "$name" ] || continue
    case "$status" in
        running)
            if [ "$health" = "unhealthy" ]; then
                # ★ 只报，不动手 —— autoheal 的地盘（见文件头「动作分档」）。
                na "$name  running 但 health=$health（pid=$pid）"
                say "     ↳ 这是**卡死**，归 autoheal 管；本脚本**不动手**（两边都动手会打架）。"
            elif [ "$health" = "starting" ]; then
                na "$name  running（health=starting —— 刚起来，给它时间）"
            else
                ok "$name  running（health=$health, pid=$pid）"
            fi
            ;;
        missing)
            if [ "$DAEMON_DOWN" = 1 ]; then
                # ★★★ daemon 不可用 ⇒ 「不存在」不是"容器被删了"，是**读不到 daemon**。
                #   绝不把它算进 N_BAD（那会触发"拉起"和那条错的告警）。
                na "$name  **读不到**（daemon 不可用 —— 不是容器被删）"
                say "     ↳ 整趟改判「未验」：见下面那条 daemon 级结论。"
            else
                down "$name  **不存在** —— 容器根本没创建"
                say "     ↳ 可能要整栈 up 一次（compose 文件：${COMPOSE_FILE:-找不到}）"
                NEED_UP="$NEED_UP $name"; N_BAD=$((N_BAD + 1))
            fi
            ;;
        exited|dead|created|paused)
            if [ "$DAEMON_DOWN" = 1 ]; then
                na "$name  status=$status（exit=$ecode）—— **daemon 不可用期间读到的**，不当成「停过」"
            else
                down "$name  status=$status（exit=$ecode, pid=$pid）—— **不在跑**"
                NEED_UP="$NEED_UP $name"; N_BAD=$((N_BAD + 1))
            fi
            ;;
        restarting)
            na "$name  restarting（正在自己重启，先观察一轮再说）"
            ;;
        *)
            down "$name  status=$status（未知状态 —— 当成不在跑处理）"
            NEED_UP="$NEED_UP $name"; N_BAD=$((N_BAD + 1))
            ;;
    esac
done <<EOF
$INSPECT_OUT
EOF
echo

# ★★★ 2026-10-03（`§32.17`）：daemon 不可用 ⇒ **另投一条 daemon 级告警**，
#   然后**跳过**整段"拉起"（`N_BAD` 是 0，所以本来也不会拉起，但要说清楚）。
#   ★ 为什么不复用逐容器那条告警：那条的正文写死「已尝试拉起 / 复读仍不在跑」，
#     而 daemon 不在时**什么都没尝试** ⇒ 复用就是**撒谎**（本仓最忌的"读数一样、含义相反"）。
#   ★ 判据：这条告警的 key 与逐容器那条**不同**（`watch-containers-daemon`），
#     标题也明说是 **daemon** 不在，不点名任何单个容器。
if [ "$DAEMON_DOWN" = 1 ]; then
    say "★★★ docker daemon **不可用**（\`docker info\` 失败 ⇒ 身份读不出）——"
    say "    这一刻**所有**容器都读不到，**不是**「容器被删」、也**不是**「拉起失败」。"
    say "    ⇒ 本趟**不拉起、不投逐容器告警**（那些结论在 daemon 不在时不成立）。"
    UNK=$((UNK + 1))
    SPOOL="$NOTIFY_ROOT/spool"; ARCHIVE="$NOTIFY_ROOT/archive"
    if [ "$NOTIFY" = 0 ]; then
        # ★ 通知已剥离/显式关闭：不投告警，但这一趟"未验"的**结论照样打屏**
        #   （退出码仍是 2）—— 关的是信，不是判断。
        say "  （NOTIFY=0：daemon 告警**不投递**，只上屏；结论仍为『未验』rc=2）"
    elif [ ! -d "$SPOOL" ]; then
        na "spool 目录不存在（$SPOOL）—— daemon 告警**投不出去**"
    else
        _day=$(date '+%Y-%m-%d')
        _tag="watch-containers-daemon-${_day}"
        _already=0
        if find "$SPOOL" "$ARCHIVE" -maxdepth 1 -name "*${_tag}*" 2>/dev/null | grep -q .; then
            _already=1
        fi
        if [ "$_already" = 1 ]; then
            say "  （daemon 今天已投过告警，不重复投）"
        else
            _ms=$(date '+%s')000
            _f="$SPOOL/${_ms}-$$-${_tag}.txt"
            _tmp="${_f}.tmp"
            {
                printf '# reseed-notify v1\n'
                printf 'kind: alert\n'
                printf 'level: alert\n'
                printf 'ts: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
                printf 'host: %s\n' "$(hostname 2>/dev/null || echo unknown)"
                printf 'key: watch-containers:daemon\n'
                printf 'title: docker daemon 不可用（不是容器拉不起来）\n'
                # ★ metrics 里**不许有空格**（键和值都不许）—— 见 notify.py 那段。
                printf 'metrics: scope=daemon reason=info-unreadable attempted=0\n'
                printf -- '---\n'
                printf '本趟 `docker info` 读不出 ⇒ **docker daemon 不可用**（或没权限）。\n'
                printf '★ 这一刻**所有**容器都读不到 —— 这**不是**「某个容器拉不起来」。\n'
                printf '★★ 常见的真因：**ContainerManager / dockerd 正在重启**（如 2026-10-03 00:13:54 那次），\n'
                printf '   NAS 本身**没**重启时也会这样。等 daemon 起来后，下一趟会自己恢复。\n'
                printf '★ 排查：\n'
                printf '  sudo docker info          （能不能连上 daemon）\n'
                printf '  ps -o lstart -C dockerd   （dockerd 是不是刚重启）\n'
                printf '  uptime                    （区分「daemon 重启」与「NAS 重启」）\n'
                printf '主机: %s\n' "$(hostname 2>/dev/null || echo unknown)"
            } > "$_tmp" 2>/dev/null
            if [ -s "$_tmp" ] && mv -f "$_tmp" "$_f" 2>/dev/null; then
                ok "已投 daemon 告警（$(basename "$_f")）"
            else
                warn "投 daemon 告警失败（写 $SPOOL 出错？）"
                rm -f "$_tmp" 2>/dev/null || true
            fi
        fi
    fi
    echo
fi

# ------------------------------------------------------------------ #
# 动作：拉起
# ------------------------------------------------------------------ #
if [ "$DAEMON_DOWN" = 1 ]; then
    :   # daemon 不在：跳过整段拉起（上面已解释，N_BAD 必为 0）
elif [ "$N_BAD" -eq 0 ]; then
    say "★ 全部容器都在跑（或正在自己重启）—— 本脚本没有要做的事。"
else
    say "★★ 有 $N_BAD 个容器不在跑："
    for c in $NEED_UP; do say "     · $c"; done
    # ★★ 给 `up -d` 用的**服务名**列表 —— 从容器名映射出来（见 `CONTAINER_SERVICE_MAP`）。
    #   ★ 别把这段和上面那个 for 合并：上面是**给人看的**（要容器名，运维认得），
    #     这里是**给 compose 吃的**（要服务名，否则 "no such service"）。
    #   ★★ **按项目分两张服务名表**：项目 A 的走 `svc_of`，项目 B（qb）的走 `svc_of_qb`，
    #      因为两条 up -d 要在**各自的目录**里跑（见下）。
    NEED_UP_SVC=""
    NEED_UP_SVC_QB=""
    for c in $NEED_UP; do
        case " $CONTAINERS_QB " in
            *" $c "*) NEED_UP_SVC_QB="$NEED_UP_SVC_QB $(svc_of_qb "$c")" ;;
            *)        NEED_UP_SVC="$NEED_UP_SVC $(svc_of "$c")" ;;
        esac
    done
    echo
    if [ "$DRY" = 1 ]; then
        say "[dry-run] 会执行（★ 注意这里列的是**服务名**，不是容器名；★ 顺序 = 依赖）："
        # ★★ 先项目 B（qb）再项目 A —— 顺序就是依赖：cross-seed 起来前 qb 必须可达。
        if [ -n "$NEED_UP_SVC_QB" ]; then
            say "  cd \"$COMPOSE_DIR_QB\" && $DOCKER $COMPOSE_CMD up -d$NEED_UP_SVC_QB"
        fi
        if [ -n "$NEED_UP_SVC" ]; then
            say "  cd \"$COMPOSE_DIR\" && $DOCKER $COMPOSE_CMD --profile $COMPOSE_PROFILE up -d$NEED_UP_SVC"
        fi
        say "[dry-run] 会投一条告警进 $NOTIFY_ROOT/spool/"
    elif [ -z "$COMPOSE_CMD" ]; then
        bad "找不到 compose 命令（docker compose / docker-compose 都没有）⇒ 拉不起来"
        say "  手工核对： $DOCKER ps -a   然后在 $COMPOSE_DIR 里 up -d"
    else
        # ---- 第一步：项目 B（qbittorrent-reseed）—— **必须先于**项目 A ----
        #   ★★ 这是跨项目依赖的**唯一**编码处：qb 起来前，cross-seed 会 exit 0 空转重启。
        #   ★ 不带 `--profile`（项目 B 无 profile，见 COMPOSE_PROFILE_QB）。
        if [ -n "$NEED_UP_SVC_QB" ]; then
            if [ -z "$COMPOSE_FILE_QB" ]; then
                bad "在 $COMPOSE_DIR_QB 里找不到 compose.yaml / docker-compose.yml ⇒ 拉不起来"
                say "  ★ 多半是 compose 目录没挂进容器，或路径写错了。"
            else
                say "→ 执行（项目 B / qb）： $DOCKER $COMPOSE_CMD up -d$NEED_UP_SVC_QB"
                # ★ 必须 cd 到 COMPOSE_DIR_QB：compose 的相对路径是相对 compose 文件所在目录解析的。
                #   传服务名（= 容器名），理由同项目 A。
                if ( cd "$COMPOSE_DIR_QB" && "$DOCKER" "$COMPOSE_CMD" up -d $NEED_UP_SVC_QB ); then
                    ok "up -d 已执行完（项目 B：$NEED_UP_SVC_QB）"
                else
                    bad "项目 B up -d **失败**（rc=$?）—— 见上面的输出"
                fi
            fi
        fi
        # ---- 第二步：项目 A（本栈）----
        if [ -n "$NEED_UP_SVC" ]; then
          if [ -z "$COMPOSE_FILE" ]; then
            bad "在 $COMPOSE_DIR 里找不到 compose.yaml / docker-compose.yml ⇒ 拉不起来"
          else
            say "→ 执行（项目 A）： $DOCKER $COMPOSE_CMD --profile $COMPOSE_PROFILE up -d$NEED_UP_SVC"
            # ★ 必须 cd 到 COMPOSE_DIR：compose 的相对路径（`./cross-seed:/config` 之类）
            #   是**相对 compose 文件所在目录**解析的，在别处跑会挂错目录或直接报错。
            # ★★ 最后一个参数是 **$NEED_UP_SVC（服务名）**，不是 $NEED_UP（容器名）——
            #    传容器名会报 `no such service`（真机实测踩过，见 CONTAINER_SERVICE_MAP 注释）。
            if ( cd "$COMPOSE_DIR" && "$DOCKER" "$COMPOSE_CMD" --profile "$COMPOSE_PROFILE" up -d $NEED_UP_SVC ); then
                ok "up -d 已执行完（项目 A：$NEED_UP_SVC）"
                # ★★ 双引号串里的**反引号会当命令替换跑掉** —— 本机自测实测：
                #   原样写 `up -d` 会让 sh 去执行 `up -d` 这个命令（报 "up: command not
                #   found"），同时把消息里那段话**吞掉**（打印出来少一块）。
                #   消息是给人看的，绝不能有副作用。⇒ 反引号一律转义。
                say "   ★ 注意：\`up -d\` 返回 0 **不等于**容器已经健康 —— 下面复读一次状态。"
            else
                bad "项目 A up -d **失败**（rc=$?）—— 见上面的输出"
            fi
          fi
        fi
    fi
fi
echo

# ------------------------------------------------------------------ #
# 复读：拉起之后**再查一次**（「说拉起了」不等于「真的在跑」）
# ------------------------------------------------------------------ #
if [ "$DRY" = 0 ] && [ "$N_BAD" -gt 0 ]; then
    echo "--- 复读（up -d 之后）---"
    AFTER=$("$DOCKER" inspect $NEED_UP 2>/dev/null | "$PY" -c '
import json, sys
# ★★ 必须用 `sys.argv[1:]`（**列表**），不是 `sys.argv[1].split()`。
#   本机自测时实测栽过：`python3 -c '...' a b c d` 时 argv = ['-c', 'a', 'b', 'c', 'd']，
#   于是 `argv[1].split()` 只得到 **['a']** ⇒ 只查了**第一个**容器，
#   其余三个**看都没看**，且它们**根本不会出现在输出里**（不是报 missing）。
#   ⇒ 症状是"四个里只报了一个"，看着像"其它三个都正常" —— 正是本仓反复钉的
#     "看着绿、其实什么也没验"。⇒ 用 argv[1:] 整段取。
want = sys.argv[1:]
raw = sys.stdin.read()
try:
    arr = json.loads(raw)
except Exception:
    # ★ 读不动 ⇒ 哨兵（同上面那一处：不能把"读不到"念成"都不在了"）
    print("!INSPECT-UNREADABLE"); raise SystemExit(0)
if not isinstance(arr, list):
    print("!INSPECT-UNREADABLE"); raise SystemExit(0)
by = {c.get("Name", "").lstrip("/"): c for c in arr if isinstance(c, dict)}
for name in want:
    c = by.get(name)
    if not c:
        print("%s\tmissing\t0\tnone" % name); continue
    st = (c.get("State") or {})
    h = ((st.get("Health") or {}) or {}).get("Status") or "none"
    print("%s\t%s\t%s\t%s" % (name, st.get("Status") or "unknown",
                              "1" if st.get("Running") else "0", h))
' $NEED_UP 2>/dev/null)
    STILL=""
    # ★ 复读也认哨兵：读不到就**报未验**，别念成"仍然不在跑"（含义相反）。
    if printf '%s\n' "$AFTER" | grep -q '^!INSPECT-UNREADABLE$'; then
        na "复读读不出内容 ⇒ **未验**（拉起到底成没成，这一轮判不了）"
        UNK=$((UNK + 1))
    fi
    # ★★ 这个循环体里**确实**调了 python（复读用的那个 `-c`）——
    #   而它若读 stdin 就会把 heredoc 剩下的行吃掉（本机自测实测过：
    #   三个名字只处理了第一个）。⇒ **必须**给那次 python 显式接 `< /dev/null`。
    while IFS='	' read -r name status running health; do
        [ -n "$name" ] || continue
        if [ "$status" = "running" ]; then
            ok "$name  running（health=$health）"
        else
            bad "$name  **仍然** status=$status —— 拉起没成功"
            STILL="$STILL $name"
        fi
    done <<EOF
$AFTER
EOF
    echo

    # ------------------------------------------------------------------ #
    # 投告警进 spool（复用现有发信链路 —— **不新造通道**）
    # ------------------------------------------------------------------ #
    # 事件文件格式必须与 `notify.py::Notifier._render` **逐字同构**，
    # 否则「排空 spool」那侧读不出来（它只读 `kind:` 与 `title:`，但整套头要齐）。
    #
    # ★★ 防轰炸：**同一天同一容器只投一次**。
    #   动机同 2026-09-13 那场邮件风暴 —— 本任务每 5 分钟跑一趟，若不冷却，
    #   一个拉不起来的容器会**一天 288 封**。冷却用**文件名**记（扫 spool + archive），
    #   不写新的状态文件 ⇒ 少一个要挂载的路径。
    SPOOL="$NOTIFY_ROOT/spool"
    ARCHIVE="$NOTIFY_ROOT/archive"
    if [ "$NOTIFY" = 0 ]; then
        # ★ 通知已剥离/显式关闭：逐容器告警**不投递**（拉起动作在上面已做完）。
        #   ★ 结论照样上屏（`$STILL` 已在上面 `bad` 打过），关的只是这封信。
        say "  （NOTIFY=0：逐容器告警**不投递**，只上屏；拉起动作不受影响）"
    elif [ ! -d "$SPOOL" ]; then
        na "spool 目录不存在（$SPOOL）—— 告警**投不出去**（但拉起已做完）"
    else
        _day=$(date '+%Y-%m-%d')
        # ★★ 只对 `$STILL`（**复读之后仍然不在跑**的）投告警 —— 不是 `$STILL $NEED_UP`。
        #   动机（本机自测实测）：`$NEED_UP` 是「**曾经**不在跑」的集合，**不是**
        #   「还没修好」的集合。写 `for c in $STILL $NEED_UP` 的后果：
        #     ① 三个**本来好好的**容器也各收一封「容器不在跑」—— 假警报，正是本仓
        #        反复钉的「读数一样、含义相反」；运维收到后就不再信这封信了；
        #     ② `drive-loop` 出现在两个变量里 ⇒ 循环**跑五遍**（四个容器），
        #        冷却判定在同一次循环里还没落盘，于是**同一次运行里重复投**。
        #   ⇒ `$STILL` 已经涵盖了「拉起失败」这个唯一该报的情形。
        for c in $STILL; do
            # 该容器当天是否已经投过？（冷却标记是**文件名里的一段**，不是文件内容）
            _tag="watch-containers-${c}-${_day}"
            _already=0
            # ★ 只看文件名，不 cat —— 在满盘/慢盘上读内容既慢又可能失败。
            #   `find` 加 `--` 不需要；路径都已引号包裹。
            if find "$SPOOL" "$ARCHIVE" -maxdepth 1 -name "*${_tag}*" 2>/dev/null | grep -q .; then
                _already=1
            fi
            if [ "$_already" = 1 ]; then
                say "  （$c 今天已投过告警，不重复投）"
                continue
            fi

            # ★ 变量名带 `_` 前缀避免与 POSIX 保留名冲突；文件名用毫秒时间戳前缀，
            #   与 notify.py 的 `%d-%d-%s.txt` 同形状 ⇒ 排空侧按名字排序仍是 FIFO。
            _ms=$(date '+%s')000
            _f="$SPOOL/${_ms}-$$-${_tag}.txt"
            _tmp="${_f}.tmp"
            {
                printf '# reseed-notify v1\n'
                printf 'kind: alert\n'
                printf 'level: alert\n'
                printf 'ts: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
                printf 'host: %s\n' "$(hostname 2>/dev/null || echo unknown)"
                printf 'key: watch-containers:%s\n' "$c"
                printf 'title: 容器拉不起来：%s\n' "$c"
                # ★ metrics 里**不许有空格**（键和值都不许）—— 见 notify.py 那段
                #   「空格会让下游按空格切出错键」的注释。这里的值天然无空格。
                printf 'metrics: container=%s action=restart attempted=1 failed=1\n' "$c"
                printf -- '---\n'
                # ★ 措辞须与「投告警时机」一致（见文件头）：这封信只在**拉起也没救回来**时发，
                #   不是「容器停过」就发。否则读信的人会以为「收到信 = 刚才停过」。
                printf '%s 不在跑，watch-containers.sh 已尝试拉起，**但复读仍不在跑**。\n' "$c"
                printf '★ 这封信只在「拉起没成功」时发 —— 手动去 NAS 上查：\n'
                printf '  docker ps -a        （看它的 status / ExitCode）\n'
                printf '  docker logs %s      （看它为什么起不来）\n' "$c"
                printf '主机: %s\n' "$(hostname 2>/dev/null || echo unknown)"
            } > "$_tmp" 2>/dev/null

            # ★★ 原子落盘：先写 .tmp 再 rename。排空任务**可能同时**在扫 spool，
            #   半个文件会让 `field_of` 读出空 title（与 notify.py 同一处置）。
            if [ -s "$_tmp" ] && mv -f "$_tmp" "$_f" 2>/dev/null; then
                ok "已投告警：$c（$(basename "$_f")）"
            else
                warn "投告警失败（写 $SPOOL 出错？）—— 拉起已做完，只是这封信没发"
                rm -f "$_tmp" 2>/dev/null || true
            fi
        done
    fi
    echo
fi

# ------------------------------------------------------------------ #
# 收尾
# ------------------------------------------------------------------ #
echo "============================================================"
if [ "$RC" -eq 0 ] && [ "$UNK" -eq 0 ]; then
    if [ "$N_BAD" -eq 0 ]; then
        echo "★ 全部容器都在跑 —— 没有要做的事。"
    elif [ "$DRY" = 1 ]; then
        # ★ dry-run 什么都**没做** —— 别说成「已拉起」（本机自测：这句曾把 dry-run
        #   念成已处理，与 473 行那句差之毫厘、含义相反）。
        echo "★ [dry-run] 有 $N_BAD 个容器不在跑，**本趟没动手**（未置 exit 1）。"
        echo "  ★ 去掉 --dry-run 再跑，才会真去 up -d。"
    else
        echo "★ 已尝试拉起 $N_BAD 个容器，并且复读都通过了。"
        echo "  ★ 但「拉起来过」本身是信号：去看 docker logs 找**为什么会停**。"
    fi
    exit 0
elif [ "$RC" -ne 0 ]; then
    echo "★★ 有不通过项（上面 ✗）—— 见上面的排查提示。" >&2
    exit 1
elif [ "$DAEMON_DOWN" = 1 ]; then
    echo "★★ docker daemon **不可用**（\`docker info\` 读不出）—— 这是『未验』，不是『没问题』。" >&2
    echo "   ★ 这一刻所有容器都读不到，**别**当成「容器都停了」。" >&2
    echo "   ★ 多半是 ContainerManager / dockerd 正在重启；起来后下一趟会自己恢复。" >&2
    exit 2
else
    echo "★★ 没有 ✗，但有 ${UNK} 段『查不出』—— 这是『未验』，不是『没问题』。" >&2
    exit 2
fi
