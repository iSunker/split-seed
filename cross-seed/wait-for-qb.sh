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
# ② **就绪判断只看 HTTP 通不通**（`version` 端点返回 2xx）—— 不猜别的语义。
#    这里**不做**登录（不碰 qB 写操作、不需要凭据），只问"端口后面有没有人"。
#
# ★ 本脚本跑在 **cross-seed 容器内部**，用**服务名** `qbittorrent-reseed` 直连
#   （`:3060`）；**不要**改成 `${NAS_IP}` —— 群晖上「容器→宿主 LAN IP→DNAT 回
#   另一容器」的回环会 timeout（见 docker-compose.yml 顶部 2026-09-11 实测）。
#
# ★ 依赖容器里已有的 `wget`（cross-seed 基础镜像自带；2026-10-01 实测确认）。
#   用一个**短** `--timeout=5` —— 点不通要**快**返回，好进下一轮，别把一轮卡死。
#
# ★ 为什么用 `sh` 而不是 bash：基础镜像的 `/bin/sh` 足够，且 compose 里就是
#   `command: ["sh", "/config/wait-for-qb.sh"]`。别引入 bash 依赖。
# =====================================================================

# ---- 可调参数（都可用环境变量覆盖，便于测试；默认值针对生产）----
# qB 的就绪地址（服务名 + 端口；见上面 ★）。
QB_URL="${WAIT_QB_URL:-http://qbittorrent-reseed:3060}"
# 单次探测超时（秒）。点不通要快返回。
PROBE_TIMEOUT="${WAIT_PROBE_TIMEOUT:-5}"
# 总等待上限（秒）。**这是硬顶** —— 到点一律照常启动（约束 ①）。
MAX_WAIT="${WAIT_MAX_WAIT:-300}"
# 两轮之间的间隔（秒）。
INTERVAL="${WAIT_INTERVAL:-5}"

now() { date +%s; }

# ★ `$(now)` 不写 `$(now())` —— 空括号会让某些 `sh`（dash）在解析 `$(` 时把它
#   当成函数定义，报 `syntax error near unexpected token )`（本机实测抓到）。
START=$(now)
# 已等秒数（用整数算术，POSIX sh 的 `$(( ))` 就够）。
waited=0

echo "[wait-for-qb] 等 $QB_URL/api/v2/app/version 就绪（最多 ${MAX_WAIT}s，每 ${INTERVAL}s 探一次）…"

while :; do
    # `-q` 静默、`-O-` 输出到 stdout（丢弃即可，只关心退出码）、`-T` 短超时。
    # ★ 把输出丢进 /dev/null：我们**只要"通不通"**，不要内容（约束 ②）。
    if wget -q -O /dev/null -T "$PROBE_TIMEOUT" "$QB_URL/api/v2/app/version" 2>/dev/null; then
        echo "[wait-for-qb] qB 已就绪（等了 ${waited}s）。启动 cross-seed daemon。"
        break
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
