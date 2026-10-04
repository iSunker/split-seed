#!/usr/bin/env bash
# =====================================================================
# 本地 → NAS 生产 的白名单同步（**只搬 compose + 配置**；代码走镜像）
#   本地: 本仓库根目录（默认 = 脚本所在目录，可用 SRC 覆盖）
#   生产: NAS 上的 compose 目录（必须用 DST 指定）
#
# ★★ 2026-10-05（B4，push/pull 改造）：本脚本**只管 compose 与配置**。
#   容器读的**代码**（`orchestrator/*`、`drive-loop/scripts/*.py`、`run-resident.sh`）
#   已**全部从 FILES 表移除**，改由 **Docker Hub 镜像** 携带（`isunker/reseed-toolkit-*`，
#   tag=git 短 sha）⇒ 改代码 = 本机 `docker build` + `docker push` + NAS `docker compose pull`。
#   理由与规则见 `.claude/rules`（§「代码定位」）与 `重构方案.md §8.3`。
#
# 原则：
#   1. 白名单 —— 只覆盖 FILES 里列出的文件，其余一律不碰。
#   2. 绝不覆盖生产独有/含密钥的内容：
#      .env(真实 apikey) / prowlarr/(站点 cookie+db) / cross-seed 的 db 与
#      cross-seeds 输出 / hlink 日志。它们不在白名单里，动不到。
#   3. 覆盖前备份到本地 .deploy-backup/<时间戳>/（放本地，避免污染 NAS 上的
#      docker build 上下文），可一键回滚。
#   4. 默认 dry-run 只打 diff；--apply 才写入。
#
# 用法:
#   DST=//my-nas/docker_ssd/reseed bash deploy.sh             预览差异
#   DST=//my-nas/docker_ssd/reseed bash deploy.sh --apply     写入（先自动备份）
#   DST=//my-nas/docker_ssd/reseed bash deploy.sh --rollback  回滚到最近一次备份
#
#   DST 也可写进 scripts/.nasrc（已 gitignore），避免每次输入。
# =====================================================================
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ★ 命令行/环境变量必须**优先于** .nasrc —— 2026-09-12 实测踩过：
#   `DST=/tmp/sandbox bash deploy.sh --apply` 原本会被 .nasrc 里的 DST **覆盖掉**，
#   于是"在沙箱里试一下"实际打到了**生产 NAS**（`DST=` 赋值在 source 之后）。
#   想拿临时目录做演练却误伤生产，这个口子必须堵上：先存下外部给的值，source 完再恢复。
#   （`.nasrc` 的定位是**默认值**，不是**强制值**。）
_DST_FROM_ENV="${DST:-}"
# shellcheck source=/dev/null
[[ -f "$SELF_DIR/scripts/.nasrc" ]] && source "$SELF_DIR/scripts/.nasrc"

SRC="${SRC:-$SELF_DIR}"
DST="${_DST_FROM_ENV:-${DST:-}}"
DST="${DST:?请设置 DST（NAS 上的 compose 目录），例如 DST=//my-nas/docker_ssd/reseed}"
BACKUP_ROOT="$SRC/.deploy-backup"

# <本地相对路径>::<生产相对路径>
# ★ 2026-10-05（B2）：本地 compose 已改名 `docker-compose.yml` → `compose.yaml`，
#   与生产**同名** ⇒ 映射变成 `compose.yaml::compose.yaml`（不再是跨名映射）。
FILES=(
  "compose.yaml::compose.yaml"
  "data/cross-seed/config.js::cross-seed/config.js"
  # ★ 2026-10-01：cross-seed 的**启动闸门**（`command: ["sh", "/config/wait-for-qb.sh"]`）。
  #   它在容器内是 `/config/wait-for-qb.sh`（./data/cross-seed 挂到 /config），故必须随本白名单
  #   一起上 NAS —— 否则 compose 里引用的那个文件**不存在** ⇒ 容器起不来（`no such file`）。
  #   与上面 config.js 同一种"挂载目录里的文件"（同一条：动它之后 `--force-recreate`）。
  #   ★ 2026-10-05（B2）：本地源路径加 `data/` 前缀（生产相对路径不变）。
  "data/cross-seed/wait-for-qb.sh::cross-seed/wait-for-qb.sh"
  "data/hlink/config.yml::hlink/config.yml"
  ".dockerignore::.dockerignore"
  # 农场清单维护脚本 build-farm.sh。
  #   ★ 2026-10-05（B2）：它**同时**进镜像（`/app/scripts/build-farm.sh`，供代码调用）
  #     **和** compose 根目录 —— 因为 `drive-loop.py::_run_verify()` 用
  #     `first_existing("build-farm.sh")` 在 **CROSSSEED_DIRS（= 挂载的 compose 根）**
  #     里找它，找不到就**静默跳过农场巡检**（一条 warning，不退非零）。
  #     ⇒ 两处都要有，且 must 一致。源路径随 data/ 收拢改成 `data/drive-loop/scripts/`。
  #   ★ 原先它靠"手工拷上去"，代价实测过（09-11 那次改完 --verify 的期望集，生产副本
  #     是另外手工放的，两边一度不一致、且没有备份）。进白名单后改完 `--apply` 就同步、
  #     自动进 `.deploy-backup/` 可回滚。
  "data/drive-loop/scripts/build-farm.sh::build-farm.sh"
  # ★ 2026-09-12 晚补：这两个原先**靠手工拷**，是同一族问题的最后两个。
  #   查"git 提交是不是一半 NAS 一半本地"时量出来的 —— 白名单之外的文件
  #   **没有任何机制保证两边一致**（build-farm.sh 就是前车之鉴，见上）。
  #   实测收进来时两边已一致，所以这次是**零风险**地把"靠记性"换成"靠 cmp"。
  #
  #   ★ 2026-10-05（B2）：下面三个仍是**本机 `scripts/`（根）** 的源码/生成物（不在 data/）。
  #   fix-statedb-farm-root.py —— 仓库里是 scripts/ 下的**已跟踪源码**，
  #     生产副本却放在 compose **根目录**（和数据修复时手边方便有关）。
  #   nas-update-env.sh —— 是**生成物**（scripts/gen-nas-env-update.py 产出，
  #     故在仓库里被 gitignore）。★ 之所以能同步：它只装载 DATA_DIRS + LINK_DIR
  #     两个**路径**键，不含任何凭据；且实测本地 .env 与生产 .env 的 DATA_DIRS
  #     **逐字节相同**（指纹 420afc99339a）。若哪天它开始携带别的键，先回看这里。
  #     本地没生成过这个文件时（如全新 clone），deploy.sh 会打
  #     「⚠ 本地缺失，跳过」并继续 —— 不会因为少了它而中断。
  "scripts/fix-statedb-farm-root.py::fix-statedb-farm-root.py"
  "scripts/nas-update-env.sh::nas-update-env.sh"
  # ★ 2026-09-13 补：删「暂存区」目录 —— 哨兵 `--cleanup` 攒出来的
  #   `_cleanup-YYYYMMDD/`，以及早先误删进回收站的 `#recycle/env-bak-*`。
  #   为什么落成文件、不往 DSM 任务框里粘：
  #     ① 这类目录**会反复出现**（每清一次杂物就攒一个），粘一次只能用一次；
  #     ② 它必须**绕开 File Station** —— File Station 的删除只是挪进 `#recycle`，
  #        文件仍在盘上；而暂存区里最要紧的恰恰是 `.env` 的**明文副本**（带全部凭据）。
  #        2026-09-13 实测 `docker_ssd/#recycle/env-bak-20260912/`
  #        里就躺着 8 个，全是之前几次「File Station 删除」留下的。
  #        ★ 这 8 个里只有 7 个是 `.env.bak.<时间戳>`，第 8 个是裸名 `.env.bak` ——
  #          脚本原先的删除口径 `.env.bak.*` 就漏了它，现已放宽成 `.env*`
  #          （详见 README 该节「窄口已修」）。
  #   放在 compose **根目录**（不是 scripts/），理由同 build-farm.sh —— 和它要
  #   清理的目标在同一层，命令行里路径短、不容易打错。
  #   ★ 它**不进任何计划任务**：只在 DSM 任务计划里手动运行一次，
  #     手动跑 → 用户选 root → 脚本框一句 `sh <路径>/rm-staging.sh <目标> --apply`。
  "scripts/rm-staging.sh::rm-staging.sh"
  # ★★ 2026-09-29：**容器巡检拉起件**的两个文件。
  #
  #   ★★ 为什么**改变**了原决定（原决定写在本文件的漂移哨兵 KNOWN_NAS 里：
  #      「宿主机运行、故意不部署」）：
  #      当时的设计是"挂在 DSM 计划任务上、由宿主机跑" ⇒ 部署它 = 假一致
  #      （deploy.sh 推上去的那份，与"到底谁在跑"无关）。
  #      **2026-09-29 用户改方案：改成常驻容器**（`reseed-watcher`，见 compose 里
  #      那段头注）⇒ 这两个文件现在是**容器读的**，而容器读的是 compose 目录里的
  #      那份 ⇒ **必须由 deploy.sh 保证两边一致**，否则"改了仓库、容器还跑旧的"
  #      且**没有任何读数会报**（这正是本仓最怕的形状）。
  #
  #   ★ 映射到 `scripts/diag/`（**不是** compose 根）：因为 compose 里
  #     `command: ["sh", "data/scripts/diag/watch-containers-loop.sh"]`
  #     与 `working_dir: /volume2/…/reseed` ⇒ 相对路径就是 `data/scripts/diag/`
  #     （★ 2026-10-05 收进 data/ 后，脚本的生产相对路径也加了 `data/` 前缀）。
  #     （`chk58.sh` 等"人手动跑"的件仍在 `KNOWN_NAS`、**不在**这里 ——
  #       判据是「**容器/自动机制会不会读它**」，不是"它是不是诊断件"。）
  "data/scripts/diag/watch-containers.sh::data/scripts/diag/watch-containers.sh"
  "data/scripts/diag/watch-containers-loop.sh::data/scripts/diag/watch-containers-loop.sh"
)

MODE="${1:---dry-run}"
case "$MODE" in
  --apply|--rollback|--dry-run) ;;
  *) echo "未知参数: $MODE  (可用: --apply | --rollback | --dry-run)" >&2; exit 2 ;;
esac

[[ -d "$SRC" ]] || { echo "✗ 本地目录不存在: $SRC" >&2; exit 1; }
# ★★ 闸门必须"**真的去读**"，不能只 `stat()`（2026-09-21 实测，一次真实事故）：
#   原写法是 `[[ -d "$DST" ]]` —— 它在 **SMB 凭据错**时**返回真**，于是闸门放行，
#   一路走到 `mkdir` 才炸。实测症状（用户亲历）：
#       mkdir: cannot create directory '//192.168.0.7/docker_ssd': Permission denied
#   而**在此之前**它已经打了 `有差异的文件 (29)`、**29 条全是 `[新]`** ——
#   因为 `[ -f "$DST/$d" ]` 在凭据错时也一律为假 ⇒ 每个文件都被判成"目标不存在"。
#   ⇒ 极易被读成"推成功了"，**这正是本项目最忌的「判据看着过了、其实没测到东西」**。
#
#   ★ 根因：`-d` 走 `stat()`，MSYS 对 UNC 路径**不真正展开目录内容**；
#     而 `ls`/`-f` 会**真的去读**，凭据错就被拒。当场实测三种判据（断开共享后）：
#       A) [[ -d "$DST" ]]                      → **true**（放过 ← 缺陷）
#       B) ls "$DST" >/dev/null 2>&1            → **false**（拦住 ✅）
#       C) [[ -f "$DST/orchestrator/main.py" ]] → **false**（拦住 ✅）
#     ⇒ 取 **B**：它不挑文件（白名单增删都不影响它），且比 C 少一个"某个具体文件必须在"的隐含前提。
#   ★★ 还有一条实测要记住：**断开后头几秒三种判据全都放过** —— Windows 会**缓存** SMB 会话
#     （连接是懒惰建立的）。所以这一闸**不是瞬时的**：它挡的是"**缓存过期后**"的坏状态。
#     换言之，**推之前先跑一次 `--dry-run` 看差异数**（`[新]` 大片出现 = 假的）仍然必要。
if ! ls "$DST" >/dev/null 2>&1; then
  echo "✗ 生产目录不可达（或凭据被拒）: $DST" >&2
  echo "  ★ 不要只看\"目录在不在\" —— 本闸门是**真的去列一次**。" >&2
  echo "    凭据错/未挂载时，-d 会返回真而 ls 会失败（2026-09-21 实测）。" >&2
  echo "  ⇒ 先修连接再推，例如（Windows cmd，一次即可）：" >&2
  echo "    net use \\\\<NAS_IP>\\<share> /user:<用户名> \"<密码>\" /persistent:yes" >&2
  echo "  ⇒ 然后确认能列出来（Git Bash）：  ls \"$DST\" | head" >&2
  exit 1
fi

# ---------- 回滚 ----------
if [[ "$MODE" == "--rollback" ]]; then
  LATEST="$(ls -1 "$BACKUP_ROOT" 2>/dev/null | sort | tail -1 || true)"
  [[ -n "${LATEST:-}" ]] || { echo "✗ 没有任何备份: $BACKUP_ROOT" >&2; exit 1; }
  echo "→ 从备份回滚: $LATEST"
  ( cd "$BACKUP_ROOT/$LATEST"
    find . -type f | while read -r f; do
      f="${f#./}"
      echo "  恢复 $f"
      mkdir -p "$DST/$(dirname "$f")"
      # ★ 原子替换，同下面的说明 —— 回滚同样可能落在批次运行中。
      cp -p "$f" "$DST/$f.new.$$"
      mv -f "$DST/$f.new.$$" "$DST/$f"
    done )
  echo "✓ 回滚完成。NAS 上记得: sudo docker compose up -d --force-recreate cross-seed"
  exit 0
fi

# ★ 不写字面量 —— 写字面量会被编辑器当成真 CR，把这个文件自己弄污了。
CR_CHAR=$(printf '\r')

# ---------- 算差异 ----------
CHANGED=()
for p in "${FILES[@]}"; do
  s="${p%%::*}"; d="${p##*::}"
  if [[ ! -f "$SRC/$s" ]]; then
    echo "⚠ 本地缺失，跳过: $s" >&2
    continue
  fi
  if [[ ! -f "$DST/$d" ]] || ! cmp -s "$SRC/$s" "$DST/$d"; then
    CHANGED+=("$p")
  fi
done

if [[ ${#CHANGED[@]} -eq 0 ]]; then
  echo "✓ 生产与本地一致，无需同步。"
  exit 0
fi

echo "有差异的文件 (${#CHANGED[@]}):"
# ★★ `[新]` 有两种完全不同的含义，**必须分开**（2026-09-21 实测踩过）：
#     真·新文件   —— 生产端确实没有它（首次部署，正常）
#     读不到      —— 生产端**有**它，但**读不了**（凭据错/掉线）
#   原写法把两者都印成 `[新]` ⇒ 一次凭据错的事故里**29 个文件全印 `[新]`**，
#   读起来像"首次部署一次到位"，**根本看不出是坏的**。
#   ⇒ 判据：拿**第一个**差异文件去问一次"这目录到底能不能读"；读不了就**显式报警**。
#   ★ 这里只**报警并让人自己判断**，不自动退出 —— 因为"真·首次部署"时
#     生产端目录可能真的是空的，那种情况下 `[新]` 是对的。
N_NEW=0
for p in "${CHANGED[@]}"; do
  s="${p%%::*}"; d="${p##*::}"
  if [[ -f "$DST/$d" ]]; then echo "  [改] $s → $d"; else echo "  [新] $s → $d"; N_NEW=$((N_NEW+1)); fi
done

# ★ 全 `[新]` 是"读不到"的典型指纹（真·首次部署极罕见一次 29 个全空）。
#   注意：上面那条 `ls` 闸门**可能**因 SMB 会话缓存而刚放过（实测：断开后头几秒仍为真）
#   ⇒ 这里再问一次"目标目录**里有没有东西**"，用**真读一次**的方式（不是 `stat`）。
if [[ "$N_NEW" -eq "${#CHANGED[@]}" && "${#CHANGED[@]}" -gt 3 ]]; then
  if ! ls "$DST"/* >/dev/null 2>&1; then
    echo
    echo "⚠⚠ 全部 ${#CHANGED[@]} 条都被判成 [新]，但**生产目录读不出任何内容**。" >&2
    echo "    ⇒ 这几乎肯定是**连接/凭据问题**，不是\"首次部署\"。" >&2
    echo "    ⇒ 千万别直接 --apply（会写不进去，或在更糟的情形下写错地方）。" >&2
    exit 1
  fi
fi

# ---------- dry-run ----------
if [[ "$MODE" != "--apply" ]]; then
  echo
  echo "──────── diff（生产 → 本地）────────"
  for p in "${CHANGED[@]}"; do
    s="${p%%::*}"; d="${p##*::}"
    echo "=== $d ==="
    diff -u "$DST/$d" "$SRC/$s" || true
  done
  echo
  echo "以上仅为预览。确认无误后执行： bash deploy.sh --apply"
  exit 0
fi

# ---------- 批次运行中告警（见 SUMMARY §17.2）----------
# 覆盖一个**正在被 sh 读取**的脚本，会让它从旧偏移读到新文件的字节、执行到错位的代码。
# 实测症状：attempts.log 里冒出一条**假的** exit=127（"no python"，其实 python 好得很），
# 而那批真正的 exit=0 永远不写。改成下面的原子替换后不再有这个问题，
# 但**正在跑的那一批会继续用旧脚本**，所以还是提示一句更稳。
# 判据用**心跳新鲜度**（NAS 侧批次运行期间每 60s 刷一次 .drive-loop.state）。
# 读不到就闭嘴 —— 宁可不提示，也不要误报。
STATE="$DST/drive-loop/scripts/.drive-loop.state"
if [[ -f "$STATE" ]]; then
  HB="$(sed -n 's/.*"heartbeat_ts"[[:space:]]*:[[:space:]]*\([0-9.]*\).*/\1/p' "$STATE" 2>/dev/null | head -1 || true)"
  now_ts="$(date +%s)"
  if [[ -n "${HB:-}" ]] && [[ "$HB" =~ ^[0-9.]+$ ]]; then
    AGE="$(awk -v a="$now_ts" -v b="$HB" 'BEGIN{printf "%d", a-b}')"
    if [[ "${AGE:-99999}" -ge 0 && "${AGE:-99999}" -lt 600 ]]; then
      echo "⚠ NAS 上**有批次正在跑**（心跳 ${AGE}s 前刚刷过）。"
      echo "  本次同步不影响它（原子替换，它继续读旧脚本），但它跑完前你看到的是旧行为。"
      echo "  急着看新行为的话，等心跳停止刷新（~10 分钟）再重新触发一次。"
      echo
    fi
  fi
fi

# ---------- 备份 + 写入 ----------
STAMP="$(date +%Y%m%d-%H%M%S)"
BDIR="$BACKUP_ROOT/$STAMP"
mkdir -p "$BDIR"
echo "→ 备份到 $BDIR"
for p in "${CHANGED[@]}"; do
  d="${p##*::}"
  if [[ -f "$DST/$d" ]]; then
    mkdir -p "$BDIR/$(dirname "$d")"
    cp -p "$DST/$d" "$BDIR/$d"
  fi
done

# ★ 必须是「写临时文件 + mv」，**不能**直接 cp 覆盖 —— 见 SUMMARY §17.2。
#   sh 是边读边执行的：直接 cp 会**原地截断重写同一个 inode**，正在运行的那份脚本
#   （例：跑批中的 drive-loop/run.sh）会拿着旧偏移去读新文件 → 执行到错位的代码。
#   实测症状就是 attempts.log 里那条假的 `exit=127 (no python: /usr/bin/python3)`。
#   mv 是同文件系统的 rename()，**换 inode** —— 老进程继续读它自己那份旧的，
#   新进程看到新内容。（python 不受影响：CPython 启动时一次性读完源码。）
#   cp -p 先保住权限位，再 mv 带过去，所以 755 这类可执行位不会丢。
trap 'rm -f "$DST"/*.new.$$ 2>/dev/null || true' EXIT
echo "→ 同步（原子替换）"
for p in "${CHANGED[@]}"; do
  s="${p%%::*}"; d="${p##*::}"
  mkdir -p "$DST/$(dirname "$d")"
  cp -p "$SRC/$s" "$DST/$d.new.$$"
  # ★★ 部署前剥掉 CR（2026-09-18 实测）：工作树在 Windows 上可能带 CRLF
  #   （Edit/Write 工具与 python 文本模式写入都会把 LF 变成 CRLF），而 NAS 是 Linux
  #   ⇒ 原样传过去会让 .sh 的行尾变成 CRLF，撞 ERR-SH-02 / ERR-SH-03。
  #   ★ 排除 .cmd/.bat：那两个必须是 CRLF（见 .gitattributes，删了会静默失败）。
  #   ★ 必须 tr 到另一个临时文件再 mv —— 不能在 SMB 上就地改写（同 §17.2 的理由）。
  case "$s" in
    *.cmd|*.bat) : ;;
    *) LC_ALL=C tr -d "$CR_CHAR" < "$DST/$d.new.$$" > "$DST/$d.new.$$.lf"
       if ! cmp -s "$DST/$d.new.$$" "$DST/$d.new.$$.lf"; then
         mv -f "$DST/$d.new.$$.lf" "$DST/$d.new.$$"
       else
         rm -f "$DST/$d.new.$$.lf"
       fi ;;
  esac
  mv -f "$DST/$d.new.$$" "$DST/$d"
  echo "  ✓ $d"
done
trap - EXIT

# ★★★ 2026-09-19：这里原先打的是**一条写死的**"下一步"（`up -d --force-recreate cross-seed`）。
#   实测害人一次：那次推的是 `orchestrator/crossseed_client.py`，它**归 `reseed-orchestrator`**
#   （**一次性 CLI**，代码是 `COPY orchestrator/` 进镜像的 ⇒ 要 `build`，`--force-recreate` 不够），
#   而 `reseed-cross-seed` 是**第三方 Node 镜像，根本不碰它** ⇒ 用户白重启了一次容器。
#   ★ 根因：**推的是一组文件，而"哪一步让它生效"是逐文件的**（有的进镜像、有的挂载即生效、
#     有的一次性 CLI 压根没有服务可重启）⇒ 一条**静态文本**冒充**本次的动作**，
#     与 SUMMARY §28 发现三同形：**两个说法都对，只是量的不是同一样东西**。
#   ⇒ 改成**按本次实际推到的文件分类**，各报各的生效方式；拿不准就明说"未归类"，别猜。
# ★★★ 2026-10-05（B4，push/pull 改造）：**代码不再走 deploy.sh** ——
#   `orchestrator/*` / `scripts/drive-loop*.py|sh` 已**从 FILES 表移除**，改由 Docker Hub
#   镜像携带（`docker compose pull … && up -d --force-recreate`，见 `.claude/rules`）。
#   ⇒ 下面 `[A]` 分类的判据随之改为"**走镜像**"（如果哪天有文件又回到"进镜像"这条路），
#     提示语从旧的 `build` 改成 `pull`。分类逻辑保持**按本次实际推到的文件**（不写死）。
_need_image=(); _need_recreate=(); _need_nothing=(); _unknown=()
for p in "${CHANGED[@]}"; do
  d="${p##*::}"
  case "$d" in
    orchestrator/*|drive-loop/*)          _need_image+=("$d") ;;
    cross-seed/*)                         _need_nothing+=("$d") ;;
    *.env|*.env.example|compose.yml|compose.yaml) _need_recreate+=("$d") ;;
    *.sh|hlink/*)                         _need_nothing+=("$d") ;;
    *)                                    _unknown+=("$d") ;;
  esac
done

echo
echo "✓ 同步完成。备份: .deploy-backup/$STAMP  （回滚: bash deploy.sh --rollback）"
echo
echo "下一步 —— ★ 按**本次实际推到的文件**分类，别照抄（每一类生效方式不同）："
echo
if (( ${#_need_image[@]} )); then
  echo "  [A] 代码 ⇒ **走镜像**（本项目代码全在镜像里；改代码 = 重建 + push + pull）："
  for d in "${_need_image[@]}"; do echo "        $d"; done
  echo "        本机： docker build … && docker push isunker/reseed-toolkit-<x>:<sha>"
  echo "        NAS ： sudo docker compose pull <服务> && sudo docker compose up -d --force-recreate <服务>"
  echo
fi
if (( ${#_need_recreate[@]} )); then
  echo "  [B] compose / .env ⇒ 需重建容器："
  for d in "${_need_recreate[@]}"; do echo "        $d"; done
  echo "        sudo docker compose config --quiet && echo 'YAML OK'"
  echo "        sudo docker compose up -d --force-recreate <受影响的服务>"
  echo
fi
if (( ${#_need_nothing[@]} )); then
  echo "  [C] 挂载 / 脚本 / 配置 ⇒ **推完即生效**，无需重启："
  for d in "${_need_nothing[@]}"; do echo "        $d"; done
  echo
fi
if (( ${#_unknown[@]} )); then
  echo "  [D] ⚠ **未归类**（本脚本不知道它归哪个服务生效）—— 请自行确认再动手："
  for d in "${_unknown[@]}"; do echo "        $d"; done
  echo "        ★ 判据：先 grep 它的 import 方 / 谁读它，再决定。**别直接照抄 A/B。**"
  echo
fi
echo "  ★ 一般性复核（只读、随时可跑）："
echo "        cd /volume2/docker_ssd/reseed"
echo "        sudo docker compose ps"
