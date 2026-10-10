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
#   · 当天日志里**一批都没跑**（无新批次行）
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
#   sudo sh watch-containers.sh              # 真查；发现不在跑就拉起
#   sudo sh watch-containers.sh --dry-run    # 只查只报，**不拉起**
#   sh watch-containers.sh --selftest        # 本机自测（用假 docker，**只走 dry-run**）
#   sh watch-containers.sh --selftest-live   # 本机自测**真动作路径**（假 docker + DRY=0）
#   sudo sh watch-containers.sh --compose <dir>
#
# ★ 退出码语义：0 = 全都在跑（或已成功拉起）；1 = 有拉不起来的；2 = 查不了
#
# ★★ 盯哪些容器（**数量以 `CONTAINERS` + `CONTAINERS_QB` 两处声明为准，别在正文里抄**）
#   · **项目 A**（本项目 compose）：`restart: unless-stopped` 的 4 个常驻服务；
#   · **项目 B**（另一个 compose，`qbit-reseed1..4`）：4 个，2026-10-01 加 / 2026-10-06 扩。
#   ⇒ 共 8 个。为什么不盯 `reseed-orchestrator` / `reseed-autoheal` 见 CONTAINERS 处。
#
# ★★ 验证状态（2026-09-28，**别把本机验过当成已上线**）
#   ✅ 本机验过（假 docker，`--selftest-live`）：四种退出码 0/0/1/2 各有正例；
#      「读不出 / 空数组 / 有内容」三种读数长得不一样。
#      （★ 早期还验过"事件文件被 NAS 上那份真的 `notify-spool.sh` 逐字段解析通过"
#       —— 通知写侧已于 2026-10-05 砍掉，那条验证随之作废。）
#   ✅ 对着 **NAS 的 `compose.yaml`** 核过：本栈六个 `container_name` 里只盯
#      `restart: unless-stopped` 的**四个**常驻服务（另两个为何不盯见下面 CONTAINERS 处）；
#      ★ 2026-10-01 又加了**项目 B**的 `qbit-reseed1..4`（见 CONTAINERS_QB 处）。
#   ⛔ **还没上过 NAS** ⇒ 以下**未验**（都是真机才能证的）：
#      ① 真 `docker inspect` 的字段形状（`State.Status/Pid/ExitCode/Health.Status`）——
#         夹具是我按文档造的，可能与真 daemon 有出入；
#      ② `--profile drive-loop` 能否真把 `reseed-drive-loop` 拉起来、拉起来后是否真
#         变 healthy（夹具只是「换个文件」）；
#      ③ 计划任务下的 `$PATH` / `sudo` / 宿主时间（DSM 计划任务的 env 与交互 shell 不同）。
#   ⇒ 纪律：先在 NAS 上 `--dry-run` 看读数对不对，再手跑一次真的，**最后**才挂计划任务。
#
# ★★ 结论口径（**产品决定，已拍板**）：**只有「拉起之后仍然不在跑」才算失败**（rc=1）。
#   成功拉起 ⇒ **完全静默**（rc=0）。
#   · 理由：一次偶发停机被自动救回来后不必惊动人。
#   · 代价（**认下来**）：容器「停过又起来了」这件事**事后无痕** —— 只能从
#     rc / 计划任务日志看出来。
#   · ★ 曾写错过：判据写成「曾经不在跑」会给**本来好好跑着**的容器也记一笔，
#     且 `drive-loop` 重复出现 ⇒ 同趟数五遍。判据就是本仓那条「读数一样、含义相反」。
#     ⇒ 正确判据是 `$STILL`（**复读之后仍然不在跑**的），不是 `$STILL $NEED_UP`。
#
# ★★ 本脚本的**唯一动作 = 拉起容器**（通知写侧已于 2026-10-05 整体砍掉）：
#     ① `docker compose ... up -d`（拉起容器）—— 唯一写的"地方"。
#   ★ 除此之外**不写任何文件**（早先那段"投告警进 spool"已删除，见本仓 commit）。
#   ★ 退出码与上屏结论**照旧**：拉不起来的容器仍会 `bad`/rc=1 —— 只是不再发信。
#     要发信请去独立通知项目（`D:\Projects\nas里pt相关程序邮件通知告警项目`）。

set -u

export PYTHONIOENCODING=utf-8

COMPOSE_DIR="${COMPOSE_DIR:-/volume2/docker_ssd/reseed}"
# ★ 通知（NOTIFY / NOTIFY_ROOT）已于 2026-10-05 整体砍掉：本脚本不再写 spool，
#   `NOTIFY`（投不投告警）这个变量随之消失 —— 它只管"投不投"，与"拉不拉"无关，
#   现在既没有投信也没有可关的开关。`--notify` / `--no-notify` 两个参数也已删。
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

# ★★ 第二组容器：`qbit-reseed1..4` —— **不在**本项目（COMPOSE_DIR）的 compose 里，
#   它在**另一个 compose 项目**（自己的目录）。两个致命理由，所以**不能**并进上面
#   `CONTAINERS`：
#     ① 动作用 `cd "$COMPOSE_DIR" && docker compose up -d <服务名>`。qb 的服务不在
#        本项目里 ⇒ 会报 `no such service: qbit-reseed1`（就是 §32.3 ⑤
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
COMPOSE_DIR_QB="${COMPOSE_DIR_QB:-/volume2/docker_ssd/qbit-reseed}"
# ★ 项目 B 无 profile ⇒ 显式空串（不继承 COMPOSE_PROFILE）。别删这一行：
#   它让"为什么第二条 up 不带 --profile"在代码里可见，而不是靠读注释回忆。
COMPOSE_PROFILE_QB=""

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)  DRY=1 ;;
        --selftest) SELFTEST=1; DRY=1 ;;
        # ★★ `--selftest-live`：用假 docker，但**允许 DRY=0** —— 否则「拉起 + 复读」
        #   这条**最要紧**的路径根本无法自测（`--selftest` 强制 DRY=1，
        #   而复读那段是 `[ "$DRY" = 0 ]` 才进的 ⇒ 永远走不到）。
        #   本机自测实测踩过：改了 `$STILL` 的范围（`$STILL` 而非 `$STILL $NEED_UP`），
        #   却因为跑不出这条路径而**看不出对错** —— 正是本仓反复钉的「没验 = 不算过」。
        #   ★ 生产上**绝不能用**（它会真去 up -d）；只给本机自测用。
        --selftest-live) SELFTEST=1 ;;
        --compose)  shift; COMPOSE_DIR="${1:-}" ;;
        --compose-qb) shift; COMPOSE_DIR_QB="${1:-}" ;;
        # ★ 半角括号 + ASCII 标点，理由见文件头（dash 取词器会在全角括号上截断）。
        *) echo "未知参数: $1   (可用: --dry-run | --selftest | --selftest-live | --compose <dir> | --compose-qb <dir>)" >&2; exit 2 ;;
    esac
    shift
done

COMPOSE_DIR=$(printf '%s' "$COMPOSE_DIR" | tr -d '\r\n' | sed 's/[[:space:]]*$//')
COMPOSE_DIR_QB=$(printf '%s' "$COMPOSE_DIR_QB" | tr -d '\r\n' | sed 's/[[:space:]]*$//')

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

# ★★ 第二组：项目 B（qbit-reseed1..4）。**容器名 = 服务名**（同名），
#   但仍**显式**写进对照表 —— 不做"去掉 reseed- 前缀"那类猜测（同上一条理由）。
#   NAS 现场核对（2026-10-01 / 2026-10-06 扩容）：
#       docker inspect qbit-reseed1 --format '{{.Config.Labels}}'
#       ⇒ com.docker.compose.service = qbit-reseed1   （= 容器名）
CONTAINERS_QB="qbit-reseed1 qbit-reseed2 qbit-reseed3 qbit-reseed4"
CONTAINER_SERVICE_MAP_QB="qbit-reseed1=qbit-reseed1 qbit-reseed2=qbit-reseed2 qbit-reseed3=qbit-reseed3 qbit-reseed4=qbit-reseed4"

# ★★★ R3（2026-10-06）：**qb 探活**（补 §5.5.2 坐实的盲点 —— "容器在跑、qb 死在容器里"）
#   ─────────────────────────────────────────────────────────────────────
#   现状：本脚本只看 **容器** 在不在跑。而 qb 的 s6 会在**容器内**重启 qb 进程 ⇒
#         容器**不退出** ⇒ 探活永远说 "running（pid=旧值）" ⇒ qb 真死了也报绿。
#         证据链《NAS-qB三实例OOM证据链》§5.5.2：2026-10-06 开机 8.3h 内 OOM-kill 15 次，
#         每次 s6 容器内重启 qb，容器状态始终 running。
#   ⇒ 本段用一个 **python3 stdlib** 探针直接问 qb 的 WebUI API（`/api/v2/app/version`），
#     把"容器 running"升级成"容器 running **且 qb 应答**"。
#
#   ★★ 三条硬约束（F5/F6/F7，是本地基，别改）：
#     ① **无 curl**：NAS 上不一定有 curl ⇒ 用 `$PY`（stdlib urllib）打 HTTP。
#     ② **容器内 127.0.0.1 ≠ 宿主**：本脚本跑在 watcher 容器里，而三个 qb 是
#        **宿主**上的另一个栈 ⇒ 探 127.0.0.1:<port> 探的是**watcher 自己**。
#        必须走**宿主网关** `172.17.0.1`（docker 默认 bridge 网关）→ 宿主 →
#        宿主上监听的 3020/3060/8085。
#     ③ **8085 也要探**：套件版 qb（Synology 原生包）不在 docker 里，但同样会 OOM
#        ⇒ 三个实例（3020 opencd / 3060 reseed / 8085 套件版）一个都不能漏。
#
#   ★★ 为什么**不**引入 `QB_STATE_DIR`（本文件第 90 行的纪律）：
#     "冷却状态用**文件名**记…不写新的状态文件 —— 少一个要挂载的路径"。
#     ⇒ 探活只做"进程/服务/数据"三层，**全部无状态**（每次现探现判），不新开挂载点。
#     （原 diff 里的 `QB_STATE_DIR=$NOTIFY_ROOT/state` 行**已删** —— 见评审定案 D2。）
QB_PROBE_GATEWAY="${QB_PROBE_GATEWAY:-172.17.0.1}"
# ★★ 实测（2026-10-06，从 watcher 容器内探宿主 qb，只读）：
#     · watcher 自己的网关是 `172.30.0.1`（`reseed_reseed-net`）——
#       它能到 3020/3060，**但到不了 8085**（套件版 qb 只绑在 docker0 网关那侧）。
#     · `172.17.0.1`（docker0 默认桥）**三个都到**（3020/3060/8085 全 200）。
#     · `192.168.0.7`（NAS LAN IP）三个都**到不了**（宿主防火墙拦）。
#   ⇒ 默认用 `172.17.0.1`；并把**候选列表**都试一遍（哪个连通用哪个），
#     免得某天某个 qb 换了绑定接口就"看着绿/看着死"。
QB_PROBE_GATEWAYS="${QB_PROBE_GATEWAYS:-172.24.0.1 $QB_PROBE_GATEWAY 172.30.0.1}"
QB_PROBE_TIMEOUT="${QB_PROBE_TIMEOUT:-5}"
# ★ 探哪些实例：`name:port:confpath`（**空格分隔**，POSIX sh 无数组）。
#   ★★ 凭据来源（2026-10-06 定）：**从每个 qb 自己的 `qBittorrent.conf` 读 `WebUI\APIKey`**，
#      而不是往 `.env` 里再抄一份 —— 凭据**留在它原本的家**、本脚本**只读不落盘**，
#      且**绝不打印**（凭据纪律：不打印、不进 env 快照、不写第二份）。
#      这三份 conf 由 compose **只读**挂进 watcher（见 compose 的 watcher.volumes ④⑤⑥）。
#   ★★ 默认值用 `${VAR-…}`（**只在未设时**取默认），**不是** `${VAR:-…}`（未设**或空**）。
#      区别很重要：**显式传空串 `QB_PROBE_INSTANCES=` ⇒ 探活整段跳过**（零实例）。
#      离线自测就靠这一手（否则探针会真去连网关、整轮测试挂住）；生产上不传即用默认。
QB_PROBE_INSTANCES="${QB_PROBE_INSTANCES-reseed1:3320:/qbconf/reseed1/qBittorrent.conf reseed2:3340:/qbconf/reseed2/qBittorrent.conf reseed3:3360:/qbconf/reseed3/qBittorrent.conf reseed4:3380:/qbconf/reseed4/qBittorrent.conf}"

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
# ★ 通知（NOTIFY / NOTIFY_ROOT）已于 2026-10-05 砍掉 ⇒ 这里不再打印它们。
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

# ★★★ 2026-10-03（`§32.17`）：daemon 不可用 ⇒ 本趟**不拉起任何容器**（`N_BAD` 是 0，
#   本来也不会拉起，但要说清楚），并且把这条结论**打屏 + 置 rc=2（未验）**。
#   ★ 通知写侧已砍掉（2026-10-05）：原先这里还会投一条 daemon 级告警进 spool —— 已删。
if [ "$DAEMON_DOWN" = 1 ]; then
    say "★★★ docker daemon **不可用**（\`docker info\` 失败 ⇒ 身份读不出）——"
    say "    这一刻**所有**容器都读不到，**不是**「容器被删」、也**不是**「拉起失败」。"
    say "    ⇒ 本趟**不拉起**（结论在 daemon 不在时不成立），退出码 2（未验）。"
    UNK=$((UNK + 1))
    echo
fi

# ------------------------------------------------------------------ #
# qb 探活（容器在跑 ≠ qb 活着）—— 见 CONTAINERS_QB 后那段长注
# ------------------------------------------------------------------ #
# ★ 判据三层，缺一不可（读数一样、含义相反是本仓最忌的）：
#     ① 容器 running（上面已判）
#     ② qb WebUI **应答** `/api/v2/app/version`（本段）
#     ③ 版本号能解析出来（本段：打印 `OK vX.Y.Z n=<种子数>`）
#   ⇒ 任一层红 ⇒ `down` 上屏 + `N_QB_BAD++`（收尾并入退出码，见 D1）。
# ★★ 本段**无状态**：每次现探现判，不写 `state/`（见上面纪律）。
# ★★ 探活也**照跑 dry-run**：dry-run 是"不改**容器**"，不是"不**查**"——
#    探活纯只读，正应当照跑（原 diff 里那句 `if [ "$DRY" = 0 ]; then :; fi` 是空操作，已删）。
echo "--- qb 探活（容器在跑 ≠ qb 活着）---"
N_QB_BAD=0
if [ -z "$PY" ]; then
    # 上面 `[ -z "$PY" ]` 分支已 exit 2；这里只是防御性兜底。
    na "没有 python ⇒ 探不了 qb（**未验**）"
    N_QB_BAD=$((N_QB_BAD + 1))
else
    for _inst in $QB_PROBE_INSTANCES; do
        _name=${_inst%%:*}
        _rest=${_inst#*:}
        _port=${_rest%%:*}
        _conf=${_rest#*:}
        # ★★ 修法 A（2026-10-10 T-08b）：不再读 APIKey、不再带 Authorization。
        #   原因：qB 5.2.4 的 WebUI\AuthSubnetWhitelist 已放行 172.24.0.0/16 / 172.17.0.0/16
        #   ⇒ 白名单网关来的请求免认证即可（实测无 key 探测 5 实例全 200 v5.2.4）。
        #   反之硬传 Bearer <conf 里的 key> 会被判 403（key 与运行时不一致）。
        _conf=""
        _probe=""
        # ★ 依次试候选网关，第一个答 `OK` 的即采用（见 QB_PROBE_GATEWAYS 的长注）。
        for _gw in $QB_PROBE_GATEWAYS; do
            _probe=$("$PY" - "$_gw" "$_port" "$QB_PROBE_TIMEOUT" <<'PYEOF' 2>/dev/null
import sys, json, urllib.request
gw, port, tmo = sys.argv[1], sys.argv[2], float(sys.argv[3])
base = "http://%s:%s/api/v2" % (gw, port)
def _get(path):
    # 修法 A：不带 Authorization（依赖 AuthSubnetWhitelist 放行白名单网关）
    with urllib.request.urlopen(base + path, timeout=tmo) as r:
        return r.read().decode("utf-8", "replace")
try:
    ver = _get("/app/version").strip()          # ② WebUI 应答
except Exception as e:                           # noqa: BLE001
    print("DEAD\t%s" % type(e).__name__); raise SystemExit(0)
# ③ 顺带取种子数。★ 实测（qB 5.2.4）：`/torrents/count` 回**裸整数**（如 `2980`），
#    不是 `{"count":N}` ⇒ 兼容两种（先试 int，再试 JSON 的 count 字段）。
try:
    raw = _get("/torrents/count").strip()
    try:
        cnt = int(raw)
    except ValueError:
        cnt = json.loads(raw).get("count", "?")
except Exception:                                # noqa: BLE001
    cnt = "?"                                    # 版本拿到了；条数拿不到不算死
print("OK\t%s n=%s" % (ver, cnt))
PYEOF
)
            case "$_probe" in OK*) _probe="OK	${_probe#OK	} (via $_gw)"; break ;; esac
        done
        case "$_probe" in
            OK*)   ok "qb:$_name  ${_probe#OK	}" ;;
            DEAD*)
                _why=${_probe#DEAD	}
                # ★ 这里用 `down`（✗ 醒目）**且**抬 `N_QB_BAD` —— 见评审定案 D1：
                #   qb 级红**独立成计数**，收尾并入退出码，不与容器级 `RC` 混。
                down "qb:$_name  **API 不可达**（$_why）—— 容器在跑，但 qb 没应答"
                say "     ↳ 多半是 qb 被 OOM-kill、s6 正在容器内重启（见 OOM 证据链 §5.5）。"
                N_QB_BAD=$((N_QB_BAD + 1)) ;;
            *)
                na "qb:$_name  探针无输出 ⇒ **未验**"
                N_QB_BAD=$((N_QB_BAD + 1)) ;;
        esac
    done
fi
echo

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
    elif [ -z "$COMPOSE_CMD" ]; then
        bad "找不到 compose 命令（docker compose / docker-compose 都没有）⇒ 拉不起来"
        say "  手工核对： $DOCKER ps -a   然后在 $COMPOSE_DIR 里 up -d"
    else
        # ---- 第一步：项目 B（qbit-reseed1..4）—— **必须先于**项目 A ----
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
    # ★ 通知写侧已砍掉（2026-10-05）：原先这里把「拉起也没救回来」的容器**投告警进
    #   `$NOTIFY_ROOT/spool/`**。整段（含事件文件渲染、同日冷却、原子落盘）已删除。
    #   结论**照旧上屏**（上面 `$STILL` 已 `bad` 打过）+ rc=1 —— 只是不再发信。
    #   要发信请去独立通知项目。
    if [ -n "$STILL" ]; then
        say "  ★ 以下容器**拉起也没救回来**（本脚本不再发信，请手动查）：$STILL"
    fi
    echo
fi

# ------------------------------------------------------------------ #
# 收尾
# ------------------------------------------------------------------ #
echo "============================================================"
# ★★★ R3（2026-10-06）：第一支多一个条件 `&& [ "$N_QB_BAD" -eq 0 ]`（评审定案 D1）。
#   动机：`N_QB_BAD>0` 而**容器级全绿**（正是本改法要抓的场景 —— qb 死在容器里）
#   时，原判据 `RC=0 && UNK=0` 会**第一支就命中并 exit 0**，永远走不到那个
#   `elif [ "$RC" -ne 0 ]` ⇒ qb 级红**被吞掉**、退出码照旧全绿。
#   ⇒ 把 qb 级红并进**第一支**的判据（语义清晰：qb 级红独立于容器级 RC）。
#   ★ 下方 `elif [ "$N_QB_BAD" -gt 0 ]` 是新增的**专门**分支 —— 让 qb 级红有自己的
#     退出码文案（不是笼统的"有不通过项"）。
if [ "$RC" -eq 0 ] && [ "$UNK" -eq 0 ] && [ "$N_QB_BAD" -eq 0 ]; then
    if [ "$N_BAD" -eq 0 ]; then
        echo "★ 全部容器都在跑、三个 qb 都应答 —— 没有要做的事。"
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
elif [ "$N_QB_BAD" -gt 0 ] && [ "$RC" -eq 0 ] && [ "$UNK" -eq 0 ]; then
    # ★★★ 专门给 qb 级红（容器全绿、qb 没应答）—— 退出码 1，文案点明是 qb 不是容器。
    echo "★★ 容器都在跑，但**有 $N_QB_BAD 个 qb 实例没应答**（上面 ✗ qb:<name>）——" >&2
    echo "   ★ 这是「容器在、qb 死」（s6 在容器内重启 qb 的典型症状；见 OOM 证据链 §5.5）。" >&2
    echo "   ★ 查： docker logs <容器>; 宿主 dmesg | grep -i 'qbittorrent\\|oom'" >&2
    exit 1
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
