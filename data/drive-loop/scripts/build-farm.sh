#!/bin/sh
# =====================================================================
# 构建 / 增量更新「硬链接农场」（v3 方案 B，见 SUMMARY §10.5）
# ★ NAS 本机或 Windows 都能跑（Windows 走 SMB，硬链接是服务端真链，见 §10.5.7）
#
# 农场是什么
# ----------
# 一个**扁平**目录，里面每个子项 = 一个大包里的「单片」，用硬链接指向真实数据
# （不占数据块，只多 inode + 目录项）。之后 cross-seed 的 `dataDirs` 就
# **只指向农场这一条**，不再需要 49 条、也不需要调大 maxDataDepth。
#
# 为什么是「每个 dataDir 的直接子项」——这条规则保证等价
# ------------------------------------------------------
# cross-seed 枚举 searchee 的规则是：
#     dataDirs.flatMap(dd => readdir(dd).flatMap(c => findNestedRoots(c, maxDataDepth)))
# 农场的子项恰好 = 原来那 49 个 dataDir 的直接子项之并集，
# 于是「农场 + depth=2」与「49 个 dataDir + depth=2」**逐条产出完全相同的 searchee**。
# ★ 注意：这是**构造上**的等价，不依赖我们对 depth 规则的建模是否精确
#   —— 同一个函数作用在同一批首层条目上，结果必然一样。
#   镜像源目录结构（硬链接正好做到这点）是它成立的前提。
#
# 期望集从哪来：`FARM_SOURCES`（切换之后），不是 `DATA_DIRS`
# -----------------------------------------------------------
# ★ 2026-09-12 修，见 SUMMARY §16.2.1。这条不改的话 `--verify` 会**永远通过**。
#   切换之前 DATA_DIRS = 49 条源目录，读它就等于读期望集，正确。
#   而**切换这个动作本身**就是把 DATA_DIRS 改成农场那一条：
#       DATA_DIRS=/volume1/video/download/reseed/reseed_farm     ← 源 == 农场
#   于是校验变成**农场跟自己比**：0 漂移、永远 PASS、还每天报"一切正常"。
#   ★ 一般原则：**校验的"期望值"绝不能来自被校验对象本身。**
#     这种退化不报错、只静默地变成永远通过 —— 比不做校验更坏，因为它给你信心。
#
#   所以切换后由 .env 里的 **FARM_SOURCES** 承载那 49 条源目录（逗号分隔，同 DATA_DIRS 格式）。
#   它**只给本脚本读** —— compose 只透传 DATA_DIRS，cross-seed 压根看不到它，
#   所以加这一行**不影响容器行为、不需要重建容器**。
#   读取顺序：FARM_SOURCES → 退回 DATA_DIRS（未切换时就是对的）。
#   兜底还有一道自指闸：源里出现农场自己 → 直接停，不让它悄悄退化。
#
# 用法
# ----
#   sh build-farm.sh                  # 默认 dry-run：只统计和预演，一个文件都不建
#   sh build-farm.sh --apply          # 真建（增量：已存在的跳过）
#   sh build-farm.sh --apply --prune  # 顺带删掉"源已经没了"的农场条目
#   sh build-farm.sh --verify         # 只校验农场 vs 源，不建不删
#                                     # ★ 有漂移 → 退出码 1；无漂移 → 0（给计划任务判成败用）
#
# 环境变量
#   COMPOSE_DIR  含 .env 的目录（默认 /volume2/docker_ssd/reseed —— 改名后的新根）
#   FARM         农场路径（默认 /volume1/video/download/reseed/reseed_farm）
#                 ★ 2026-09-12 收进 reseed/ 父目录；改这里的同时必须改 .env 的
#                   DATA_DIRS / LINK_DIR 两行（SUMMARY §18）
#   SUDO         root 提权前缀（默认 sudo；已经是 root 就设 SUDO=）
#   （期望集本身不在这里配 —— 它在 .env 的 FARM_SOURCES / DATA_DIRS，见上一节）
#
# 在哪里跑：NAS 本机 **或** Windows —— 两边都行
# ---------------------------------------------
# 原先以为"SMB/Windows 建不了硬链接"，**实测是错的**（2026-09-11，详见 §10.5.7）：
# 原生 Windows 的 `os.link()` 对这个 NAS 成功，`nlink=2`、删掉原文件后另一个还在 ——
# 是服务端真实硬链接。所以可以从 Windows 直接建场，少一道"拷脚本上去 + SSH"的工序。
#
# 从 Windows 跑时，`.env` 里的 `/volume1/...` 在本机不存在 → 用 --map 做前缀翻译：
#   COMPOSE_DIR="//iSunker-DS423/docker_ssd/reseed" \
#   FARM="//iSunker-DS423/video/download/reseed/reseed_farm" \
#   sh build-farm.sh --map "/volume1=//iSunker-DS423" --apply
#   ★ COMPOSE_DIR / FARM 必须写成**命令前缀的环境变量**（如上行）。
#     本文档原先把它们排在脚本名**之后** —— 那样它们是**位置参数**，会被下面的
#     参数解析判成「未知参数: COMPOSE_DIR=...」并以退出码 2 结束。
#     （2026-09-12 实测。改用前缀写法后 `--verify` 正常返回 0。）
#
# ★ 不管理论如何，脚本每次都**自证**硬链接真的建成了（见下面的探针）——
#   所以"能不能建"不需要靠文档断言，跑一次就知道。
# ★ 探针会验证 inode 真的相同：有些实现会把 -l 悄悄降级成"复制"且退出码仍是 0，
#   真那样会白复制 5 TB 数据，必须拦住。
#
# 安全边界（本脚本**只增不删**，除 --prune）
#   * 绝不写源目录（只读源、只在 $FARM 下建）
#   * ★ 绝不 `chown -R`：硬链接文件与源文件是**同一个 inode**，
#     对链接文件 chown 会**改掉源文件的属主**。只对目录 chown（目录是新造的，不在 inode 上共享）
#   * 跨卷会失败 → 先 stat -c %d 比设备号，给出人话报错
#   * 幂等：重复跑不会重复建，也不会改已存在的东西
# =====================================================================
set -eu

COMPOSE_DIR="${COMPOSE_DIR:-/volume2/docker_ssd/reseed}"
FARM="${FARM:-/volume1/video/download/reseed/reseed_farm}"
SUDO="${SUDO-sudo}"

DRY=1
PRUNE=0
VERIFY_ONLY=0
MAP_FROM=""; MAP_TO=""
while [ $# -gt 0 ]; do
  case "$1" in
    --apply)       DRY=0 ;;
    --dry-run)     DRY=1 ;;
    --prune)       PRUNE=1 ;;
    --verify)      VERIFY_ONLY=1; DRY=1 ;;
    --map)
      shift
      [ $# -gt 0 ] || { echo "--map 需要 FROM=TO" >&2; exit 2; }
      MAP_FROM="${1%%=*}"; MAP_TO="${1#*=}"
      [ -n "$MAP_FROM" ] && [ "$MAP_FROM" != "$1" ] || { echo "--map 格式应为 FROM=TO" >&2; exit 2; }
      ;;
    -h|--help)     awk 'NR>1 && /^set -eu/{exit} NR>1{print}' "$0"; exit 0 ;;
    *) echo "未知参数: $1  (可用: --apply | --dry-run | --prune | --verify | --map FROM=TO | -h)" >&2; exit 2 ;;
  esac
  shift
done

say() { echo "$@"; }
die() { echo "[!!] $*" >&2; exit 1; }

# 路径前缀映射：把 .env 里的 **NAS 绝对路径**翻译成本机能访问的路径。
# ★ 为什么需要它：见下面「为什么 Windows 也能跑」。只用在前缀匹配上，不做任何猜测。
map_path() {
  [ -n "$MAP_FROM" ] || { printf '%s' "$1"; return; }
  case "$1" in
    "$MAP_FROM"|"$MAP_FROM"/*) printf '%s' "$MAP_TO${1#"$MAP_FROM"}" ;;
    *) printf '%s' "$1" ;;
  esac
}

cd "$COMPOSE_DIR" 2>/dev/null || die "目录不存在: $COMPOSE_DIR"
[ -f .env ] || die "$COMPOSE_DIR/.env 不存在"
[ -d "$FARM" ] || {
  if [ "$DRY" = 1 ]; then
    say "[dry-run] 农场还不存在，--apply 时会创建：$FARM"
  else
    mkdir -p "$FARM" || die "建不了农场目录: $FARM"
  fi
}

# ---------- 1) 期望集：优先 FARM_SOURCES，退回 DATA_DIRS ----------
# （为什么不能只用 DATA_DIRS 见文件头「期望集从哪来」）
# ★★ 2026-09-12：`| tr -d '\r'` 不是装饰，是**必须**的。
#   成因：`.env` 是 **CRLF 行尾**，而 `sed` 只认 `\n`、**不会剥掉行尾的 `\r`**，
#   于是 `$(...)` 取回来的值末尾挂着一个 CR。按逗号切成 49 条之后，这个 CR
#   落在**最后一条**的值末尾 —— 症状因此非常具有迷惑性：
#       只有最后一条源目录「不存在」，其余 48 条全对。
#   后果（实测踩到，2026-09-12 中午）：
#       · `--verify` 报「源目录缺失 1」→ **退出码 1**，看着像真有漂移；
#       · 那条目录的子项因此进不了期望集，农场里它们**反被当成孤儿** →
#         谁要是照提示跑 `--apply --prune`，会**删掉一个完全正常的农场条目**；
#       · 而这个坑要等挂上定期巡检才会天天报 —— 那正是最坏的一种：
#         巡检本身成了噪音源，人很快就不看它了。
#   ★ 教训：读逗号分隔的行时，**行尾符是这个值的一部分**。凡 `.env`（CRLF）
#     取值都必须显式剥 CR。Python 那边用 `splitlines()` 天然没这个问题，
#     出事的只会是 shell —— 所以 shell 里每条 `.env` 取值都该过一遍 tr。
DATA_DIRS=$(sed -n 's/^[[:space:]]*DATA_DIRS[[:space:]]*=[[:space:]]*//p' .env | head -1 | tr -d '\r')
FARM_SOURCES=$(sed -n 's/^[[:space:]]*FARM_SOURCES[[:space:]]*=[[:space:]]*//p' .env | head -1 | tr -d '\r')

SRC_LIST="$FARM_SOURCES"; SRC_FROM="FARM_SOURCES"
if [ -z "$FARM_SOURCES" ]; then
  SRC_LIST="$DATA_DIRS"; SRC_FROM="DATA_DIRS（退回）"
fi
[ -n "$SRC_LIST" ] || die "$COMPOSE_DIR/.env 里既读不到 FARM_SOURCES 也读不到 DATA_DIRS"
say "期望集来自   : $SRC_FROM"

# ★ 自指闸：源里出现农场自己（或农场内的路径）= 清单还没跟农场拆开。
#   这是**必须硬停**的两种情况，而且都不报错、只静默变正确：
#     verify → 农场跟自己比，永远 0 漂移；
#     apply --prune → 期望集若退化成空，会把**整个农场删光**。
#   宁可停下让人看一眼，也不要它"顺利跑完"。
set -f
OLDIFS0=$IFS; IFS=,
FARM_M=$(map_path "$FARM")
N_SELF=0
for dd in $SRC_LIST; do
  [ -n "$dd" ] || continue
  set +f
  case "$(map_path "$dd")" in
    "$FARM_M"|"$FARM_M"/*) N_SELF=$((N_SELF + 1)) ;;
  esac
  set -f
done
IFS=$OLDIFS0; set +f
if [ "$N_SELF" -gt 0 ]; then
  die "期望集里有 $N_SELF 条**就是农场自己**（$FARM_M）—— 拒绝继续。
      .env 的 DATA_DIRS 已经切成农场这一条，所以不能再拿它当源清单了。
      请在 .env 里另立一行 FARM_SOURCES=（逗号分隔的 49 条**源目录**，格式同 DATA_DIRS）：
          FARM_SOURCES=/volume1/video/download/movies/xxx,/volume1/video/download/TV/yyy,...
      它只给本脚本读：compose 只透传 DATA_DIRS，cross-seed 看不到它，
      所以加这行**不用重建容器**。"
fi

# 清单文件放在农场**外面**（农场内任何文件都会被 cross-seed 枚举到，
# 虽然 .tsv 扩展名会被它的 shouldIgnore 过滤掉，但没必要冒险）
#
# ★ 2026-09-12：落点从「农场旁边」= $(dirname "$FARM") 改到 **compose 目录的 hlink/**。
#   原因：原来它跟 movies/ TV/ reseed_farm/ 挤在同一个媒体目录里，人看一眼分不清
#   哪些是媒体、哪些是本项目的元数据（实测就是在这儿看见 .tmp 残留才发现清单放错地方）。
#   hlink/ 本来就是本项目放元数据的地方 —— config.yml 在那儿，compose 里
#   `./hlink:/config` 也是这个意思。**农场本身不动**，只是在硬盘上换了个记账本的位置。
# ★★ 2026-10-05（B6 实测修正）：**只往"可写"的目录里落清单**。
#   起因（NAS 实测，退出码 2）：drive-loop 容器把 compose 根挂成 **`:ro` 只读**
#   （`/volume2/docker_ssd/reseed:/volume2/docker_ssd/reseed:ro`），而清单曾被指到
#   `data/hlink/` ⇒ 写 `.names.tmp` 时报 `Read-only file system` ⇒ 农场巡检整个挂掉。
#   ★ 容器里**唯一可写**的挂载是 `/state`（= `data/drive-loop/state`）。
#   ⇒ 取值优先级：`FARM_MANIFEST_DIR`（显式覆盖，容器里由 `drive-loop.py::_run_verify`
#     设成 `/state/hlink`）> `$COMPOSE_DIR/hlink` > `$COMPOSE_DIR/data/hlink`
#     > `$DRIVE_STATE_DIR/hlink`（容器里 = /state/hlink）。
#   判据是 **"能不能写"**，不是"存不存在" —— 因为只读挂载下目录存在但写不进去。
_pick_manifest_dir() {
  for _c in "$FARM_MANIFEST_DIR" "$COMPOSE_DIR/hlink" "$COMPOSE_DIR/data/hlink" \
            "${DRIVE_STATE_DIR:+$DRIVE_STATE_DIR/hlink}"; do
    [ -n "$_c" ] || continue
    # 已存在则直接测可写；不存在则试着建（建得起来即可写）
    if [ -d "$_c" ]; then
      [ -w "$_c" ] && { echo "$_c"; return; }
    else
      mkdir -p "$_c" 2>/dev/null && { echo "$_c"; return; }
    fi
  done
  echo ""   # 一个都没有 ⇒ 交给下面 die 报错（别静默退化）
}
MANIFEST_DIR="$(_pick_manifest_dir)"
[ -n "$MANIFEST_DIR" ] || die "找不到可写的清单目录（试过 FARM_MANIFEST_DIR / \$COMPOSE_DIR/hlink / data/hlink / \$DRIVE_STATE_DIR/hlink）"
MANIFEST="$MANIFEST_DIR/.$(basename "$FARM").manifest.tsv"

# 旧落点的清单搬过来（只搬一次；新位置已有就**不覆盖** —— 免得把新的盖回旧的）
# 只 mv 不 rm：搬不动时留在原处只是碍眼，删掉就真没了。
OLD_MANIFEST="$(dirname "$FARM")/.$(basename "$FARM").manifest.tsv"
if [ ! -f "$MANIFEST" ] && [ -f "$OLD_MANIFEST" ]; then
  mv -f "$OLD_MANIFEST" "$MANIFEST" 2>/dev/null \
    && say "  [迁移] 清单已从旧落点搬来: $MANIFEST"
fi
for _ext in .names .missing.tmp .tmp; do
  [ -f "$OLD_MANIFEST$_ext" ] || continue
  [ -f "$MANIFEST$_ext" ] && continue
  mv -f "$OLD_MANIFEST$_ext" "$MANIFEST$_ext" 2>/dev/null || true
done

# 农场还不存在时（第一次跑），退到**最近的已存在祖先目录**取设备号 ——
# mkdir -p 造出来的新目录必然落在那个祖先所在的文件系统上。
DEV_PROBE="$FARM"
while [ ! -d "$DEV_PROBE" ] && [ "$DEV_PROBE" != "/" ] && [ -n "$(dirname "$DEV_PROBE")" ]; do
  DEV_PROBE=$(dirname "$DEV_PROBE")
done
DEV_FARM=$(stat -c %d "$DEV_PROBE" 2>/dev/null || echo "")
[ -n "$DEV_FARM" ] || die "stat 不可用，无法校验同卷（本脚本依赖 coreutils）"

# 能力自检：cp -al 是整套方案的关键（-a 保结构、-l 建硬链接而非复制）
# ★ 探针要落在**农场所在的文件系统**上（硬链接是 per-filesystem 的），
#   而且绝不能拿 .env 当试验品 —— 失败路径会留下一个含密钥的副本。
#   trap 保证任何退出方式都收干净。
PROBE_DIR=$(dirname "$FARM")
[ -d "$PROBE_DIR" ] || PROBE_DIR="$COMPOSE_DIR"
PROBE_SRC="$PROBE_DIR/.hlink_probe.$$"
PROBE_DST="$PROBE_SRC.link"
trap 'rm -f "$PROBE_SRC" "$PROBE_DST"' EXIT INT TERM
: > "$PROBE_SRC" 2>/dev/null || die "农场所在目录不可写: $PROBE_DIR"
if ! cp -al "$PROBE_SRC" "$PROBE_DST" 2>/dev/null; then
  die "本机的 cp 不支持 -al（硬链接复制）。农场必须与源同卷，且文件系统要支持硬链接。"
fi
# ★ 光看 cp 退出码不够：有些实现会把 -l 悄悄降级成"复制"，退出码仍是 0。
#   必须验 inode 真的相同 —— 否则会白复制 5 TB 数据（而不是建链接）。
I_SRC=$(stat -c %i "$PROBE_SRC"); I_DST=$(stat -c %i "$PROBE_DST")
if [ "$I_SRC" != "$I_DST" ]; then
  die "cp -al 没产生硬链接（inode $I_SRC vs $I_DST）—— 是复制而不是链接，绝不能跑。"
fi
rm -f "$PROBE_SRC" "$PROBE_DST"

say "===================== 硬链接农场构建 ====================="
say "  .env       : $COMPOSE_DIR/.env"
say "  农场       : $FARM"
say "  清单       : $MANIFEST"
say "  模式       : $( [ "$VERIFY_ONLY" = 1 ] && echo '仅校验' || { [ "$DRY" = 1 ] && echo 'DRY-RUN（不建）' || echo 'APPLY'; } )$([ "$PRUNE" = 1 ] && echo ' + prune')"
say "========================================================="

# ---------- 2) 逐 dataDir、逐直接子项处理 ----------
# set -f：关掉通配符展开。DC 的路径里有全角括号和点，虽然不含 * 或 ?，
# 但把 DATA_DIRS 按逗号拆开时让 shell 别去 glob 更稳妥。
set -f
OLDIFS=$IFS
IFS=,
N_NEW=0; N_HAVE=0; N_MISS=0; N_SRC=0; N_SKIP_DEV=0
# 本次的「期望集」：名字列表（给 prune 做精确比对）+ 名字→源（给人看）
NAMES_TMP="$MANIFEST.names.tmp"
# 仅 --verify 用：期望集里有、农场里没有的（名字→源），供明细行打印
MISSING_TMP="$MANIFEST.missing.tmp"
: > "$NAMES_TMP"
: > "$MISSING_TMP"
# ★ 只有真要落盘（--apply）才准备清单本体。verify/dry-run 写它纯属浪费 ——
#   115 KB 要经 SMB 来回跑一趟，最后还是删掉。
if [ "$DRY" = 0 ]; then : > "$MANIFEST.tmp"; fi
# ★★ 2026-09-12：三个中间文件原先只在一个分支里删，**--verify 会漏掉 $MANIFEST.tmp**
#    实测：跑完 --verify，/volume1/video/download/ 下留了一个 115632 字节的
#    .reseed_farm.manifest.tsv.tmp（大小和真清单一模一样）。
#    改成 trap 统一兜底：无论从哪条路出去（正常 exit / die / set -e 报错 /
#    Ctrl-C）都会收干净。apply 成功后这两个文件已被 mv 走，rm -f 是空操作。
trap 'rm -f "$PROBE_SRC" "$PROBE_DST" "$NAMES_TMP" "$MISSING_TMP" "$MANIFEST.tmp"' \
  EXIT INT TERM

for dd in $SRC_LIST; do
  [ -n "$dd" ] || continue
  set +f
  dd=$(map_path "$dd")
  if [ ! -d "$dd" ]; then
    say "  [!!] 源目录不存在，跳过: $dd"
    N_MISS=$((N_MISS + 1))
    continue
  fi
  DEV_SRC=$(stat -c %d "$dd")
  if [ "$DEV_SRC" != "$DEV_FARM" ]; then
    say "  [!!] 与农场不在同一物理卷，跳过: $dd"
    say "       （硬链接只能同卷；农场的 FARM 要落在同一卷上）"
    N_SKIP_DEV=$((N_SKIP_DEV + 1))
    continue
  fi
  OWNER=$(stat -c '%u:%g' "$dd")

  # 直接子项 = 农场条目。这就是全部"规则"，没有深度计算。
  for src in "$dd"/*; do
    [ -e "$src" ] || continue
    name=$(basename "$src")
    dst="$FARM/$name"
    N_SRC=$((N_SRC + 1))

    # ★ 无论新建还是已存在，都要记进**期望集** —— prune 靠它判断"哪些该留"。
    #   （早期版本只记新建的，结果 prune 把已存在的一律当成过期删光。）
    printf '%s\n' "$name" >> "$NAMES_TMP"
    if [ "$DRY" = 0 ]; then
      printf '%s\t%s\n' "$name" "$src" >> "$MANIFEST.tmp"
    fi

    if [ -e "$dst" ]; then
      N_HAVE=$((N_HAVE + 1))
      set -f; continue
    fi

    if [ "$DRY" = 1 ]; then
      N_NEW=$((N_NEW + 1))
      # verify 时顺手把"缺的那条"记下来，下面打印明细用
      [ "$VERIFY_ONLY" = 1 ] && printf '%s\t%s\n' "$name" "$src" >> "$MISSING_TMP"
      set -f; continue
    fi

    # ★ cp -al：建目录结构 + 对每个文件建**硬链接**（不复制数据）
    if ! cp -al "$src" "$dst" 2>/dev/null; then
      # 退一步：低版本 cp 不认 -al 的组合时，走 find + ln
      mkdir -p "$dst" && find "$src" -type d -exec mkdir -p "$dst"/{} \; 2>/dev/null || true
      if ! ( cd "$src" && find . -type f -exec sh -c '
             d="$2"; mkdir -p "$(dirname "$d")"; ln "$1" "$d"' _ {} "$dst"/{} \; ) 2>/dev/null; then
        say "  [!!] 建链接失败: $name"
        set -f; continue
      fi
    fi
    # 只给**目录**改属主：目录是新造的；文件是硬链接 = 同一个 inode，
    # 对其 chown 会连带改掉源文件属主（灾难）。所以这里绝不能加 -R。
    # ★ $SUDO 必须**不加引号**——要让它按词拆分成多个参数交给 find -exec。
    #   从 Windows/SMB 跑时 sudo 不存在 → 失败被 || true 吞掉，
    #   结果是"沿用服务端按登录用户给的属主"，不影响正确性。
    find "$dst" -type d -exec $SUDO chown "$OWNER" {} + 2>/dev/null || true

    N_NEW=$((N_NEW + 1))
    set -f
  done
  set -f
done
IFS=$OLDIFS
set +f

N_EXPECT=$(wc -l < "$NAMES_TMP" 2>/dev/null | tr -d ' \t' || echo 0)
N_FARM=0
for dst in "$FARM"/*; do [ -e "$dst" ] && N_FARM=$((N_FARM + 1)); done

# ---------- 3) prune：农场里有、期望集里没有 → 源已经没了（或改名了）----------
N_PRUNE=0
if [ "$PRUNE" = 1 ] && [ "$N_FARM" -gt 0 ]; then
  # ★ 安全闸：期望集为空却要 prune = 几乎一定是 DATA_DIRS 读错/全都没了，
  #   真删下去会把**整个农场删光**。宁可停下让人看一眼。
  if [ "$N_EXPECT" -eq 0 ]; then
    say ""
    say "  [!!] 期望集为 0，但农场里有 $N_FARM 条 —— 拒绝 prune。"
    say "       多半是 $COMPOSE_DIR/.env 的 DATA_DIRS 读不到，或所有源目录都不存在。"
    say "       先跑一次不带 --prune 的 --apply 看清楚，再决定。"
  else
    for dst in "$FARM"/*; do
      [ -e "$dst" ] || continue
      name=$(basename "$dst")
      if ! grep -qxF "$name" "$NAMES_TMP" 2>/dev/null; then
        if [ "$DRY" = 1 ]; then
          say "  [prune·预演] 将删除: $name"
        else
          say "  [prune] 源已不存在，删除农场条目: $name"
          rm -rf "$dst"
        fi
        N_PRUNE=$((N_PRUNE + 1))
      fi
    done
  fi
fi

# ---------- 3b) 期望集落盘（apply 时；dry-run 不留任何东西）----------
# ★ 注意这个魔改清单**只在 apply 时更新**：verify 不能刷新它，否则期望集又变成
#   "农场自己写的"，等于把 §16.2.1 那个退化换个地方重演一遍。
if [ "$DRY" = 0 ]; then
  mv -f "$NAMES_TMP" "$MANIFEST.names" 2>/dev/null || cp -f "$NAMES_TMP" "$MANIFEST.names"
  mv -f "$MANIFEST.tmp" "$MANIFEST" 2>/dev/null || cp -f "$MANIFEST.tmp" "$MANIFEST"
  rm -f "$NAMES_TMP" "$MANIFEST.tmp" "$MISSING_TMP" 2>/dev/null || true
fi

# ---------- 4) 汇总 ----------
say ""
say "源直接子项合计 : $N_SRC   （= 本次期望集 $N_EXPECT 条）"
if [ "$VERIFY_ONLY" = 1 ]; then
  # 三条漂移各自是**不同的事**，不能合成一个数：
  #   源有农场无 → 新内容没进农场（cross-seed 白跑，搜不到这部）
  #   农场有源无 → **源删了/改名了，农场那条还留着**（cross-seed 照样拿去搜 = 白烧额度）
  #   源目录本身缺失 / 跨卷 → 期望集不完整，**上面的数都不可信**（比有漂移更严重）
  N_MISSING=$((N_EXPECT - N_HAVE))
  N_ORPHAN=$((N_FARM - N_HAVE))
  say "农场现有条目   : $N_FARM"
  say "源有但农场没有 : $N_MISSING"
  say "农场有但源没有 : $N_ORPHAN"
  [ "$N_MISS" -gt 0 ]     && say "源目录缺失     : $N_MISS"
  [ "$N_SKIP_DEV" -gt 0 ] && say "跨卷跳过       : $N_SKIP_DEV"

  if [ "$N_MISSING" -gt 0 ]; then
    say ""
    say "  [漂移] 源里有、农场里没有（跑 --apply 会补上）："
    SHOWN=0
    while IFS='	' read -r nm sp; do
      [ -n "$nm" ] || continue
      if [ "$SHOWN" -lt 20 ]; then
        say "      $nm"
        say "          ← $sp"
      fi
      SHOWN=$((SHOWN + 1))
    done < "$MISSING_TMP"
    [ "$SHOWN" -gt 20 ] && say "      …还有 $((SHOWN - 20)) 条"
  fi

  if [ "$N_ORPHAN" -gt 0 ]; then
    say ""
    say "  [漂移] 农场里有、源里没有（源被删或改名了）："
    SHOWN=0
    for dst in "$FARM"/*; do
      [ -e "$dst" ] || continue
      nm=$(basename "$dst")
      grep -qxF "$nm" "$NAMES_TMP" 2>/dev/null && continue
      if [ "$SHOWN" -lt 20 ]; then say "      $nm"; fi
      SHOWN=$((SHOWN + 1))
    done
    [ "$SHOWN" -gt 20 ] && say "      …还有 $((SHOWN - 20)) 条"
    say "      ★ 删不删由人定：确认源真的没了，再跑 --apply --prune。"
    say "        本脚本**只报告、绝不自动删** —— 判「源没了」的依据就是上面这份清单，"
    say "        拿它自动删等于把一次误判放大成不可逆的数据丢失。"
  fi

  if [ "$N_MISS" -gt 0 ] || [ "$N_SKIP_DEV" -gt 0 ]; then
    say ""
    say "  [!!] 有源目录不存在 / 跨卷被跳过 —— 期望集本身是**不完整的**，"
    say "       它只是「能被读到的那些源」的子集，上面的漂移计数因此不可信。"
    say "       先把这些修好（路径对不对？卷挂上了吗？）再谈漂移。"
  fi

  # 中间文件的清理交给上面那个 trap（原先这里只删两个、漏了 $MANIFEST.tmp，
  # 实测在 --verify 后留下过 115 KB 的 .reseed_farm.manifest.tsv.tmp）。
  say ""
  # ★ 退出码：计划任务靠它判成败。§16.2.2 第 1 条 —— 以前只打印计数、
  #   恒返回 0，挂上去的巡检**永远不会报警**。
  if [ "$N_MISSING" -gt 0 ] || [ "$N_ORPHAN" -gt 0 ] || \
     [ "$N_MISS" -gt 0 ] || [ "$N_SKIP_DEV" -gt 0 ]; then
    say "★ 结论：有漂移（退出码 1）"
    exit 1
  fi
  say "★ 结论：无漂移（退出码 0）"
  exit 0
elif [ "$DRY" = 1 ]; then
  say "★ 将要新建     : $N_NEW   （dry-run，什么都没建）"
  say "  已存在(跳过) : $N_HAVE"
  say "  源目录缺失   : $N_MISS"
  say "  跨卷跳过     : $N_SKIP_DEV"
  [ "$PRUNE" = 1 ] && say "  将要删除     : $N_PRUNE   （--prune 预演）"
  say ""
  say "看清楚了就加 --apply 真建。"
else
  say "★ 新建         : $N_NEW"
  say "  已存在(跳过) : $N_HAVE"
  say "  源目录缺失   : $N_MISS"
  say "  跨卷跳过     : $N_SKIP_DEV"
  [ "$PRUNE" = 1 ] && say "  已删除       : $N_PRUNE"
  say ""
  # ★ 这一段只在「还没切换」时才有意义。切换过之后 DATA_DIRS 已经是农场了，
  #   再往下走会把 dataDirs 指回 49 条 —— 那是**回退**，不是下一步。
  if [ "$SRC_FROM" = "FARM_SOURCES" ]; then
    say "下一步：本批新建的 $N_NEW 条已经在农场里了，cross-seed 下一轮就会看到它 —— 不用做别的。"
    say "  校验：sh build-farm.sh --verify   （有漂移会返回退出码 1）"
    say "  ★ 别再动 DATA_DIRS —— 它现在 = 农场这一条，是 cross-seed 的输入，不是源清单。"
  else
    say "下一步（人工确认无误后）——把 cross-seed 的 dataDirs 从 49 条切成农场这一条："
    say "  1) 本地 .env 里把 DATA_DIRS 改成：$FARM"
    say "  2) ★ 同一次改动里把原来那份 49 条清单另存成 FARM_SOURCES=（否则 --verify 从此永远通过）"
    say "  3) python scripts/diag/gen-nas-env-update.py   （生成新的 nas-update-env.sh）"
    say "  4) 在 NAS 上跑 sh nas-update-env.sh       （改 .env + --force-recreate 容器）"
    say "  5) 验证：drive-loop 日志里「索引器自检」与 searchee 数应与切换前一致"
  fi
fi
