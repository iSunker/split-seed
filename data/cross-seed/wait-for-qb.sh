#!/bin/sh
# =====================================================================
# cross-seed 启动闸门：先等 qbittorrent-reseed 就绪，再交棒给 `cross-seed daemon`
# =====================================================================
#
# ★★★ 为什么需要这个文件（本仓 ERR-DOCKER-13，2026-10-01 实测）
# ---------------------------------------------------------------------
# cross-seed 的 `daemon` 子命令在**启动时**会**登录 qB 以校验配置**。
# 而 qbittorrent-reseed 在**另一个 compose 项目**里（见文件头「两个项目」）——
# compose 的 `depends_on` **无法跨项目表达** ⇒ 没有任何东西保证 cross-seed
# 起来时 qb 已经就绪。
#
# 一旦 qb 还没起来（冷启动竞态 / 或 qb 中途死亡）：
#   cross-seed 重试登录 6 次 × 60s → `Your configuration is invalid` → **exit 0**
#   → `restart: unless-stopped` 立刻把它拉起来 → 又失败 → **无限重启循环**。
#   实测症状：日志里 154 条启动 banner、间隔精确 5 分钟、`RestartCount=44`。
#
# 这个脚本把那个竞态**在 cross-seed 进程之外**吸收掉：
#   轮询 qB 的 `/api/v2/app/version`，就绪了再 `exec` 真正的 daemon。
#
# ★★★ 两条**硬约束**（写错任何一条都比不加这个脚本更糟）
# ---------------------------------------------------------------------
# ① **绝不无限挂起** —— 那会变成"卡住的假在跑"：容器 `running`、无 restart、
#    无告警，比"崩溃循环"更难发现（崩溃至少会重启、会被 `ERR-DOCKER-12` 的
#    `RestartCount` 抓到）。⇒ **硬超时**：等满 MAX_WAIT 秒，**不管 qb 好没好，
#    一律照常 `exec cross-seed daemon`**，把上游自己的重试当兜底。
# ② 就绪判断**不能只看端口通不通** —— 必须**真的登录一次**。
#    ★★ 2026-10-04 修正（这正是 403 循环的根因被漏在这里的地方）：
#       旧版只 GET `/api/v2/app/version` 看 2xx。但 qB **未授权也返回 200**（它
#       把 API 请求当浏览器、回一页 HTML/JSON 而**不是** 403）⇒ 闸门**放行** ⇒
#       cross-seed daemon 接着去**真登录** ⇒ 白名单为空 ⇒ `Your configuration is
#       invalid` ⇒ exit 0 ⇒ `restart` 拉起 ⇒ **又是无限重启循环**。
#       ⇒ 闸门必须与 daemon 做**同一件事**：POST `/api/v2/auth/login`，
#         且必须拿到 `Ok.`（qB 登录成功的字面量），否则**继续等**。
#   参见：docker-compose.yml 里那段 ERR-DOCKER-13 注释 / 重构方案 §12.3（A2）。
#
# ★ 本脚本跑在 **cross-seed 容器内部**，用**服务名** `qbittorrent-reseed` 直连
#   （`:3060`）；**不要**改成 `${NAS_IP}` —— 群晖上「容器→宿主 LAN IP→DNAT 回
#   另一容器」的回环会 timeout（见 docker-compose.yml 顶部 2026-09-11 实测）。
#
# ★ 依赖容器里已有的 **`curl`**（cross-seed 基础镜像自带；2026-10-04 实测确认：
#   `/usr/bin/curl` 在；同时 `/usr/bin/wget` 是 **BusyBox** 版，`--version` 都不认，
#   且 `--post-data` 行为不标准）。★ 改用 `curl` + **只看 HTTP 状态码**，别解析正文：
#   实测 qB v5.2.4 登录成功回 **`204 No Content` 且正文为空**（不是老版的 `200 Ok.`），
#   而失败回 `401` / 未授权接口回 `403`。⇒ 判据 = 状态码 ∈ {200,204}，其余一律继续等。
#   一个**短** `--max-time=5` —— 点不通要**快**返回，好进下一轮，别把一轮卡死。
#
# ★ 为什么用 `sh` 而不是 bash：基础镜像的 `/bin/sh` 足够，且 compose 里就是
#   `command: ["sh", "/config/wait-for-qb.sh"]`。别引入 bash 依赖。
# =====================================================================

# ---- 可调参数（都可用环境变量覆盖，便于测试；默认值针对生产）----
# qB 的就绪地址（服务名 + 端口；见上面 ★）。
QB_URL="${WAIT_QB_URL:-http://qbit-reseed1:3320}"
# 单次探测超时（秒）。点不通要快返回。
PROBE_TIMEOUT="${WAIT_PROBE_TIMEOUT:-5}"
# 总等待上限（秒）。**这是硬顶** —— 到点一律照常启动（约束 ①）。
MAX_WAIT="${WAIT_MAX_WAIT:-300}"
# 两轮之间的间隔（秒）。
INTERVAL="${WAIT_INTERVAL:-5}"

# ★★ 登录凭据（约束 ② 要真登录）：从 .env 的 QBIT_URL_CRED 里解出来。
#   形如 http://user:pass@host:port —— 用参数展开剥掉协议头，再按 `@` 取前段，
#   最后按 `:` 取 user / pass。任一缺失 ⇒ 退回旧行为（只探端口），并**明确告警**，
#   免得"以为在验凭据、其实没验"（静默降级比不验更坏）。
CRED="${QBIT_URL_CRED:-}"
# 去掉 `scheme://`
CRED_NOPROTO="${CRED#*://}"
# `@` 之前 = user:pass；没有 `@` 说明没带凭据。
AUTH="${CRED_NOPROTO%%@*}"
if [ "$AUTH" = "$CRED_NOPROTO" ]; then
    QB_USER=""; QB_PASS=""
else
    QB_USER="${AUTH%%:*}"
    QB_PASS="${AUTH#*:}"
fi

now() { date +%s; }

# ★ `$(now)` 不写 `$(now())` —— 空括号会让某些 `sh`（dash）在解析 `$(` 时把它
#   当成函数定义，报 `syntax error near unexpected token )`（本机实测抓到）。
START=$(now)
# 已等秒数（用整数算术，POSIX sh 的 `$(( ))` 就够）。
waited=0

if [ -n "$QB_USER" ]; then
    echo "[wait-for-qb] 等 $QB_URL 就绪**且能登录**（用户 $QB_USER，最多 ${MAX_WAIT}s，每 ${INTERVAL}s 探一次）…"
else
    echo "[wait-for-qb] ⚠ 未从 QBIT_URL_CRED 解出凭据 ⇒ 只探端口（不验授权）。" \
         "若 qB 白名单为空，daemon 仍会登录失败并循环 —— 请设 QBIT_URL_CRED。"
fi

while :; do
    # "通了且能登录"才放行；否则一律继续等（约束 ②）。
    if [ -n "$QB_USER" ]; then
        # ★ POST 真登录，**只取状态码**（`-o /dev/null -w '%{http_code}'`）。
        #   200/204 = 成功放行；401（密码错/白名单外）/403（未授权）＝ 继续等。
        _code=$(curl -s -o /dev/null -w '%{http_code}' --max-time "$PROBE_TIMEOUT" \
                    --data-urlencode "username=$QB_USER" \
                    --data-urlencode "password=$QB_PASS" \
                    "$QB_URL/api/v2/auth/login" 2>/dev/null)
        case "$_code" in
            200|204)
                echo "[wait-for-qb] qB 已就绪**且登录成功**（HTTP $_code，等了 ${waited}s）。启动 cross-seed daemon。"
                break
                ;;
            "")
                : ;;   # 连不上（端口还没起）—— 静默继续等，别刷屏
            *)
                echo "[wait-for-qb] 登录未通过（HTTP $_code）——继续等。"
                ;;
        esac
    else
        # 无凭据：退回旧行为，只探端口（2xx 即通）。**明确标注这是弱判据**。
        # ★ 注意：qB 未授权时 `/app/version` 回 **403**（不是 200）——本判据确实很弱，
        #   仅当白名单已配好（无需登录）时才够用。
        _code=$(curl -s -o /dev/null -w '%{http_code}' --max-time "$PROBE_TIMEOUT" \
                    "$QB_URL/api/v2/app/version" 2>/dev/null)
        case "$_code" in
            200|204)
                echo "[wait-for-qb] qB 端口已通（HTTP $_code，等了 ${waited}s，未验授权）。启动 cross-seed daemon。"
                break
                ;;
        esac
    fi

    if [ "$waited" -ge "$MAX_WAIT" ]; then
        # ★★ 硬超时：**照常启动**（约束 ①）。宁可让 cross-seed 自己重试，
        #    也绝不让这个脚本无限挂起（那会变成静默的假在跑）。
        echo "[wait-for-qb] 等满 ${MAX_WAIT}s 仍未就绪 —— 照常启动 cross-seed daemon（由它自己重试兜底）。"
        break
    fi

    sleep "$INTERVAL"
    waited=$(( $(now) - START ))
done

# ★ `exec`：用 cross-seed 进程**替换**本脚本（不留一个多余的 sh 父进程），
#   这样信号（docker stop 的 SIGTERM）能**直接**送给 cross-seed，
#   日志/退出码也都是 cross-seed 的。
# ★★ 用**全路径** `/usr/local/bin/cross-seed` —— 本脚本是经 `entrypoint:` 覆盖直接
#   用 `/bin/sh` 起的（**绕过了镜像原来的 entrypoint** `catatonit -- /usr/local/bin/cross-seed`），
#   此时**不保证 PATH 里有** `cross-seed`（实测：这镜像里是 symlink，靠 PATH 不如靠全路径稳）。
#   ★ 这条是**NAS 首部署实测**抓到的：`command: ["sh", ...]` 会被当**参数**喂给镜像
#   ENTRYPOINT 里的 `cross-seed` ⇒ `error: unknown command 'sh'` ⇒ 崩溃循环。
#   ⇒ 正确做法是覆盖 **entrypoint**（见 docker-compose.yml），不是 command。
exec /usr/local/bin/cross-seed daemon
