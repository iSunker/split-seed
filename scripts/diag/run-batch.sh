#!/usr/bin/env bash
# =====================================================================
# 小批量试跑 + 度量   —— 在 Windows 的 Git Bash 里直接运行
#
#   9696 / 2468 / 3320 三个端口都已发布到宿主机，所以不需要 SSH 上 NAS。
#   ★ 2026-10-06 起保种实例改名/多实例：QB 宿主端口 3060 → 3320（qbit-reseed1）。
#
# 测三件事：
#   1. 实际命中率    —— 抽样 N 部里有几部真的注入成种
#   2. 单部耗时      —— webhook 到第一个种子出现的秒数
#   3. 有没有被限流  —— 跑前跑后 Prowlarr 的 indexerstatus 都必须是 []
#
# 用法:
#   bash scripts/run-batch.sh              # 默认抽 10 部
#   COUNT=25 bash scripts/run-batch.sh     # 抽 25 部
#   DRY=1 bash scripts/run-batch.sh        # 只打印抽中哪几部，不发 webhook
#   SKIP_QUIET_CHECK=1 bash scripts/run-batch.sh   # 跳过 60s 静置体检（不建议）
#
# 注意：本轮**不要**先去调 cross-seed 的 delay。这一轮就是要测当前 delay
#       会不会触发站点的限流，提前改就测不出来了。
#
# ★ 跑之前必须确认 cross-seed 没有别的任务在后台注入（脚本会静置 60s 自动体检）。
#   否则"新增种子"会把后台任务的产物算到当前这部头上，制造假 HIT。
#   见 SUMMARY.md §6.4。
# =====================================================================
set -uo pipefail

# ---------------------------------------------------------------- NAS 地址
# 改成你自己的 NAS，或用环境变量覆盖。也可写进 scripts/.nasrc（已 gitignore）：
#     NAS_HOST=192.168.1.10
#     NAS_NAME=my-nas
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
[[ -f "$SCRIPT_DIR/.nasrc" ]] && source "$SCRIPT_DIR/.nasrc"
NAS_HOST="${NAS_HOST:?请设置 NAS_HOST（NAS 局域网 IP），或写进 scripts/.nasrc}"
NAS_NAME="${NAS_NAME:?请设置 NAS_NAME（NAS 主机名），或写进 scripts/.nasrc}"

CS_URL="http://$NAS_HOST:2468"
QB_URL="http://$NAS_HOST:3320"
PW_URL="http://$NAS_HOST:9696"

# 列目录走 SMB 视图；发给 cross-seed 的必须是 NAS 内路径
PKG_REL="download/movies/DouBan_IMDB.TOP250.Movies.Mixed.Collection.20240501.FRDS"
SMB_PKG="//$NAS_NAME/video/$PKG_REL"
NAS_PKG="/volume1/video/$PKG_REL"
PROD_ENV="//$NAS_NAME/docker_ssd/reseed/.env"

CATEGORY="reseed-singles"
COUNT="${COUNT:-10}"
PER_MOVIE_TIMEOUT="${PER_MOVIE_TIMEOUT:-180}"   # 单部最长等待(秒)
QUIET_BREAK="${QUIET_BREAK:-30}"                # 已有命中后，静默多少秒就进下一部
POLL=5
DRY="${DRY:-0}"

OUT_DIR="$SCRIPT_DIR"
REPORT="$OUT_DIR/batch-report.tsv"

# ---------------------------------------------------------------- 密钥
# 从生产 .env 读，全程不回显。也可用环境变量覆盖。
CSKEY="${CSKEY:-}"
PWKEY="${PWKEY:-}"
if [[ -z "$CSKEY" || -z "$PWKEY" ]]; then
  if [[ ! -f "$PROD_ENV" ]]; then
    echo "✗ 读不到生产 .env: $PROD_ENV" >&2
    echo "  检查 SMB 连接，或手工: CSKEY=... PWKEY=... bash $0" >&2
    exit 1
  fi
  [[ -z "$CSKEY" ]] && CSKEY="$(grep -m1 '^CROSSSEED_API_KEY=' "$PROD_ENV" | cut -d= -f2- | tr -d '\r\n')"
  # Prowlarr 的 key 就嵌在 TORZNAB_URLS 的 apikey= 里
  [[ -z "$PWKEY" ]] && PWKEY="$(grep -m1 '^TORZNAB_URLS=' "$PROD_ENV" | sed -E 's/.*apikey=([^,&[:space:]]+).*/\1/' | tr -d '\r\n')"
fi
[[ -n "$CSKEY" ]] || { echo "✗ 没拿到 CROSSSEED_API_KEY" >&2; exit 1; }
[[ -n "$PWKEY" ]] || { echo "✗ 没拿到 Prowlarr API key" >&2; exit 1; }
echo "✓ 密钥已加载（cross-seed ${#CSKEY} 字符 / prowlarr ${#PWKEY} 字符，不回显内容）"

# ---------------------------------------------------------------- 工具
indexer_status() {
  curl -s --max-time 20 "$PW_URL/api/v1/indexerstatus?apikey=$PWKEY"
}

qb_hashes() {
  curl -s --max-time 25 "$QB_URL/api/v2/torrents/info?category=$CATEGORY" \
    | grep -o '"hash":"[0-9a-f]\{40\}"' | cut -d'"' -f4 | sort -u
}

# 已经注入过的种子名。抽样时要排除掉 —— 重复打 webhook 只会得到
# ALREADY_EXISTS（新增 0 个种子），会被误记成 MISS，把命中率压低。
qb_names() {
  curl -s --max-time 25 "$QB_URL/api/v2/torrents/info?category=$CATEGORY" \
    | grep -o '"name":"[^"]*"' | cut -d'"' -f4 | sort -u
}

# cross-seed v6 两种鉴权写法都兼容：先试 header，401/403 再退到 query 参数
#
# ★★ 路径必须走 stdin 交给 curl（--data-urlencode "path@-"），
#    绝不能写成 `--data-urlencode "path=$p"`。
#
#    原因：Git Bash(MINGW) 在把参数交给原生 curl.exe 时会做「路径转换」——
#    `path=/volume1/…` 这种 KEY=VALUE 里的值只要长得像 POSIX 路径就会被改写成
#    `C:/Program Files/Git/volume1/…`，中文还会按 ANSI 代码页(GBK)重编码。
#    cross-seed 收到后 stat() 必然失败，一律回 HTTP 400，
#    匹配逻辑一次都没执行，脚本却把它记成 MISS —— 测出来的命中率全是假的。
#    （2026-09-11 实测：10 部全部 400，见 SUMMARY.md §6.3）
#
#    走 stdin 后路径不再出现在命令行上，转换无从发生。
#    ★ 不要改用 `export MSYS_NO_PATHCONV=1`：路径是干净了，但同一条 curl 命令里的
#      `-o /dev/null` 会因此失效（curl 退出码 23）。实测见 SUMMARY.md §6.3。
WEBHOOK_STYLE=""
cs_webhook() {
  local p="$1" code
  if [[ "$WEBHOOK_STYLE" != "query" ]]; then
    code=$(printf '%s' "$p" | curl -s -o /dev/null -w '%{http_code}' --max-time 30 -X POST \
             -H "X-Api-Key: $CSKEY" --data-urlencode "path@-" "$CS_URL/api/webhook")
    if [[ "$code" != "401" && "$code" != "403" ]]; then
      WEBHOOK_STYLE="header"; echo "$code"; return
    fi
  fi
  code=$(printf '%s' "$p" | curl -s -o /dev/null -w '%{http_code}' --max-time 30 -X POST \
           --data-urlencode "path@-" "$CS_URL/api/webhook?apikey=$CSKEY")
  WEBHOOK_STYLE="query"; echo "$code"
}

# ---------------------------------------------------------------- 跑前体检
echo
echo "── 跑前体检 ──"

ST_BEFORE="$(indexer_status)"
if [[ -z "$ST_BEFORE" ]]; then
  echo "✗ Prowlarr 没响应 ($PW_URL)" >&2; exit 1
fi
if [[ "$(echo "$ST_BEFORE" | tr -d '[:space:]')" != "[]" ]]; then
  echo "✗ Prowlarr 索引器正处在失败退避窗口里，现在跑测出来的命中率是假的：" >&2
  echo "  $ST_BEFORE" >&2
  echo "  等 disabledTill 过去再跑。" >&2
  exit 1
fi
echo "✓ indexerstatus = []（没有索引器在退避）"

BASE_HASHES="$(qb_hashes)"
BASE_N=$(echo "$BASE_HASHES" | grep -c . || true)
echo "✓ qB 分类 $CATEGORY 现有种子 $BASE_N 个（作为基线）"
if (( BASE_N == 0 )); then
  echo "✗ 客户端里一个种子都没有 —— cross-seed 在这种情况下注入必定失败" >&2
  echo "  （它在 /torrents/add 之前先调 /torrents/info，空列表会命中失败分支）" >&2
  echo "  见 SUMMARY.md §3.5，先手工塞一个种子进去。" >&2
  exit 1
fi

# 静置观察：确认没有别的 cross-seed 任务正在后台注入。
# 否则抽样期间出现的"新增种子"会把**后台任务的产物**算到当前这部头上，
# 制造假 HIT（2026-09-11 实测：一个整包 webhook 在后台跑时，10 部全被判成 HIT，
# 而真正自己匹配上的只有 3~4 部）。
if [[ "${SKIP_QUIET_CHECK:-0}" != "1" ]]; then
  echo -n "  · 静置 60s，观察是否有后台注入"
  sleep 60
  QUIET_HASHES="$(qb_hashes)"
  if [[ "$QUIET_HASHES" != "$BASE_HASHES" ]]; then
    echo " → ✗"
    echo "✗ 静置期间 qB 里仍冒出新种子 —— cross-seed 正在跑别的任务。" >&2
    echo "  最常见的原因：有人对『大包根目录』打过 webhook，它在逐目录全量搜。" >&2
    echo "  这时测出来的命中率全是假的（后台任务的注入会被算成本部命中）。" >&2
    echo "  → 等它跑完，或重启 cross-seed 容器后再跑本脚本。" >&2
    echo "  → 确实要强行测：SKIP_QUIET_CHECK=1 bash $0" >&2
    exit 1
  fi
  echo " → ✓ 无后台注入，基线干净"
fi

[[ -d "$SMB_PKG" ]] || { echo "✗ 读不到大包目录: $SMB_PKG" >&2; exit 1; }

# ---------------------------------------------------------------- 抽样
TMP_ALL="$(mktemp)"; TMP_DONE="$(mktemp)"
trap 'rm -f "$TMP_ALL" "$TMP_DONE"' EXIT

qb_names > "$TMP_DONE"

find "$SMB_PKG" -mindepth 1 -maxdepth 1 -type d \
  | sed 's|^.*/||' \
  | grep -vE '^(0观影清单|@eaDir)' \
  | grep -viE 'sample' \
  | grep -vxF -f "$TMP_DONE" \
  | sort > "$TMP_ALL"

TOTAL=$(grep -c . "$TMP_ALL" || true)
DONE_N=$(grep -c . "$TMP_DONE" || true)
(( TOTAL > 0 )) || { echo "✗ 大包里没找到可用子目录" >&2; exit 1; }
(( COUNT > TOTAL )) && COUNT=$TOTAL
echo "✓ 大包待办子目录 $TOTAL 个（已排除 $DONE_N 个注入过的），等间隔抽 $COUNT 部"

# 等间隔抽样：比取前 N 部更能代表整包的命中率
PICKS=()
for ((i=0; i<COUNT; i++)); do
  idx=$(( i * TOTAL / COUNT + 1 ))
  PICKS+=( "$(sed -n "${idx}p" "$TMP_ALL")" )
done

echo
echo "── 抽中的 $COUNT 部 ──"
for d in "${PICKS[@]}"; do echo "  · $d"; done

if [[ "$DRY" == "1" ]]; then
  echo
  echo "DRY=1，到此为止，没有发任何 webhook。"
  exit 0
fi

# ---------------------------------------------------------------- 主循环
printf 'idx\tstatus\thttp\tnew_torrents\tsecs_to_first_hit\tsecs_total\tname\n' > "$REPORT"

RUN_START=$(date +%s)
HITS=0
ERRS=0
NEW_TOTAL=0
HIT_SECS=()

echo
echo "── 开始试跑（单部最长 ${PER_MOVIE_TIMEOUT}s，命中后静默 ${QUIET_BREAK}s 即进下一部）──"

for ((i=0; i<${#PICKS[@]}; i++)); do
  name="${PICKS[$i]}"
  n=$((i+1))
  printf '\n[%2d/%d] %s\n' "$n" "${#PICKS[@]}" "$name"

  before="$(qb_hashes)"
  t0=$(date +%s)
  code="$(cs_webhook "$NAS_PKG/$name")"
  echo "       webhook → HTTP $code"
  if [[ "$code" == "400" || "$code" == "000" ]]; then
    echo "       ✗ HTTP $code = 请求被 cross-seed 拒绝，匹配逻辑根本没执行！" >&2
    echo "         这不是「没匹配到」。先修路径转换（见脚本顶部 ★★ 注释）再跑。" >&2
  fi

  new=0; first_hit=""; quiet=0
  while true; do
    now=$(date +%s); elapsed=$(( now - t0 ))
    (( elapsed >= PER_MOVIE_TIMEOUT )) && break
    sleep $POLL
    after="$(qb_hashes)"
    cur=$(comm -13 <(echo "$before") <(echo "$after") | grep -c . || true)
    if (( cur > new )); then
      new=$cur; quiet=0
      [[ -z "$first_hit" ]] && { first_hit=$(( $(date +%s) - t0 )); echo "       ✓ 命中，${first_hit}s 出现第 1 个种子"; }
    elif (( new > 0 )); then
      quiet=$(( quiet + POLL ))
      (( quiet >= QUIET_BREAK )) && break
    fi
  done
  total=$(( $(date +%s) - t0 ))

  if (( new > 0 )); then
    status=HIT; HITS=$((HITS+1)); NEW_TOTAL=$((NEW_TOTAL+new)); HIT_SECS+=("$first_hit")
    echo "       → HIT  新增 $new 个种子，用时 ${total}s"
  elif [[ "$code" == "400" || "$code" == "000" ]]; then
    # 请求压根没被受理，不能记成 MISS —— 否则命中率会被系统性压低
    status=ERR; ERRS=$((ERRS+1)); first_hit=-1
    echo "       → ERR  请求被拒（HTTP $code），未进入匹配（${total}s）"
  else
    status=MISS; first_hit=-1
    echo "       → MISS 未匹配到（${total}s）"
  fi
  printf '%d\t%s\t%s\t%d\t%s\t%d\t%s\n' "$n" "$status" "$code" "$new" "$first_hit" "$total" "$name" >> "$REPORT"
done

RUN_TOTAL=$(( $(date +%s) - RUN_START ))

# ---------------------------------------------------------------- 跑后体检 + 汇总
ST_AFTER="$(indexer_status)"
ST_AFTER_CLEAN="$(echo "$ST_AFTER" | tr -d '[:space:]')"

avg_hit="-"
if (( ${#HIT_SECS[@]} > 0 )); then
  s=0; for v in "${HIT_SECS[@]}"; do s=$((s+v)); done
  avg_hit=$(( s / ${#HIT_SECS[@]} ))
fi
avg_per_movie=$(( RUN_TOTAL / ${#PICKS[@]} ))
est_full=$(( avg_per_movie * TOTAL / 3600 ))

VALID=$(( ${#PICKS[@]} - ERRS ))
echo
echo "════════════════ 汇总 ════════════════"
echo "① 实际命中率 : $HITS/$VALID   （共注入 $NEW_TOTAL 个单种）"
if (( ERRS > 0 )); then
  echo "   ⚠ 另有 $ERRS 部返回 HTTP 400/000 —— 请求被拒、没进入匹配，"
  echo "     已从分母剔除；这一轮数据不完整，先修路径转换再重跑。"
fi
echo "② 单部耗时   : 命中平均 ${avg_hit}s 出第 1 个种 / 整体平均 ${avg_per_movie}s 一部"
echo "             全量 $TOTAL 部按此节奏估约 ${est_full} 小时"
if [[ "$ST_AFTER_CLEAN" == "[]" ]]; then
  echo "③ 限流       : 干净，indexerstatus 仍为 []"
else
  echo "③ 限流       : ⚠ 被退避了！$ST_AFTER"
  echo "               → delay 要往上提（cross-seed/config.js 第 54 行）"
fi
echo "══════════════════════════════════════"
echo "明细: $REPORT"
echo
echo "限流还有一个佐证，在 NAS 上看容器日志（本脚本查不到）："
echo "  sudo docker logs --tail=400 reseed-cross-seed 2>&1 | grep -iE '429|retry-after|rate.?limit'"
echo "无输出 = 干净。"
