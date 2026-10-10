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
#   （期望集本身不在这里配 —— 它在 .env 的 FARM_SOURCES / FARM_DEEP_SOURCES，
#     以及**独立文件** `$COMPOSE_DIR/.farm-extra`（FARM_EXTRA，见下））
#
# ★★ 2026-10-11：`FARM_EXTRA` —— 手工 `ln` 条目的显式期望集（**独立文件**）
# ------------------------------------------------------------
# 起因：农场里有**名字无法由任何源镜像产生**的条目（手工 `ln` 的季包：Friends /
#   GBR / 是,大臣 / Money.Heist）。它们天然不在「期望集前缀链」（COVER）里 ⇒
#   一跑 `--apply --prune` 就被当孤儿删。
# 方案：把 `FARM_EXTRA` 列出的**农场相对路径**（文件 `$COMPOSE_DIR/.farm-extra`，
#   **一行一条**）**直接进期望集**（于是进 COVER ⇒ 被 prune 保护），但本脚本**不建它们**
#   （条目由人手工 `ln`）。
#   · ★★ 为什么是**独立文件**而不是 .env 里的一行（2026-10-11，见下第 3 段）：
#     compose 用 `env_file: [.env]` 读 .env，而 docker compose 的 env_file 解析器
#     **不支持多行值**。FARM_EXTRA 多行 ⇒ compose 一读就炸、`docker compose …` 整条不可用。
#     而 compose **根本不用** FARM_EXTRA ⇒ 挪出 .env 即解。
#   · ★ 分隔符 = **换行**（不是逗号！）—— 发布名里可能含 ASCII 逗号（`是,大臣` / `Yes,Minister`），
#     逗号分隔会把一条名字切成几段（2026-10-11 实测：6 条切成 18 段）。换行对任何文件名都安全。
#     ★ 也**不认 `#` 注释** —— 同源理由（任何 `#` 开头的名字会被当注释吃掉 ⇒ 静默丢声明）。
#   · 文件**不存在 / 为空** = 无声明 ⇒ 与旧行为**逐字节一致**。
#   · 声明了却**不在农场** ⇒ 告警 + 计入 `--verify` 漂移（退出码 1）—— 声明即承诺，
#     漏 `ln` / 名字打错不该静默通过（对称于「源有农场无」）。
#   · 每条都过格式闸：拒绝空串、绝对路径（/*）、含 `..` 段、以及"就是农场自身"。
#   · 语义：值 = 农场相对路径，与 NAMES_TMP 同粒度 ⇒ 深条目也可（a/b/c 会被 COVER 展开）。
#   · ★ 与 FARM_SOURCES / FARM_DEEP_SOURCES 的根本区别：那两者是**源**（枚举 + cp -al 建），
#     本键是**声明**（只认、不建）。别把源路径填进来。
#   · ★★ **硬切**（2026-10-11 定）：本脚本**只认**这个文件，**不再读** .env 里的 FARM_EXTRA。
#     于是迁移必须**守序**：先建 `.farm-extra` → 再推本脚本 → 最后删 .env 里那条 FARM_EXTRA。
#     否则「脚本已上、文件未建」的窗口内声明条目会短暂失去 prune 保护。
#
# ★★ 2026-10-05：镜像深度跟随源（嵌套单片）
# ------------------------------------------------------------
# 起因：大包里"嵌套的单片"（如 DC/01.绿箭侠…/Arrow.S01-S08…/Arrow.S01.Bluray…）
#   只能靠**深镜像**进农场；但旧脚本只镜像每个源的**一层直接子项**，于是那些
#   嵌套单片要么进不了农场、要么被下次 `--apply --prune` 当孤儿删掉。
# 方案：`FARM_SOURCES` 里的源仍按**一层**镜像（= 今天的 `"$dd"/*`，行为零变化），
#   另立 **`FARM_DEEP_SOURCES`** 列出"需要递归镜像"的源，递归到 **`FARM_DEPTH`** 层。
#   两键都在 .env 里（逗号分隔，NAS 路径视角，格式同 FARM_SOURCES / DATA_DIRS）：
#       FARM_DEEP_SOURCES=/volume1/video/download/DC/01.绿箭侠,/volume1/.../TV/某合集
#       FARM_DEPTH=2
#   · `FARM_DEEP_SOURCES` 留空 = 无深源 ⇒ 与旧行为**逐字节一致**（代码先上、行为惰性）。
#   · 深度语义：从深源的**直接子项**往下算，对应 cross-seed 的 maxDataDepth，
#     须与 `reseed-state.py init --depth N` / `pack.max_depth` **对齐**（否则农场有
#     条目、库解析不到）。
#   · 判定键由「basename」升级为「农场**相对路径**」：深度 1 时二者**逐字相同**
#     ⇒ 扁平源零变化、旧清单天然兼容（NAMES_TMP 每轮重算，从不读旧文件）。
#   · 同一个源同时出现在两键 ⇒ 取更深值并**告警**（不静默）。
#   · `FARM_DEEP_SOURCES` 也走自指闸（含 $FARM 即 die）。
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
# ★ 2026-10-05：深镜像源（见文件头「镜像深度跟随源」）。同样必须 tr -d '\r'（.env 是 CRLF）。
FARM_DEEP_SOURCES=$(sed -n 's/^[[:space:]]*FARM_DEEP_SOURCES[[:space:]]*=[[:space:]]*//p' .env | head -1 | tr -d '\r')
# ★★ 2026-10-11：**显式期望集**（手工 `ln` 的条目）。见文件头「期望集从哪来」。
#   背景：手工 `ln` 进农场的季包（Friends/GBR/是,大臣/…）名字**无法由任何源镜像产生**
#   ⇒ 天然不在 COVER 里 ⇒ 一跑 `--apply --prune` 就被当孤儿删。FARM_EXTRA 让这些
#   条目**能被声明进期望集**，从而被 prune 保护。
#   ★★ 分隔符 = **换行**（每行一条，不是逗号！）—— 2026-10-11 实测教训：
#      发布名里**可能含 ASCII 逗号**（`是,大臣` / `Yes,Minister`），逗号分隔会把一条
#      名字切成几段（实测 6 条切成 18 段）。换行则任何名字都安全（文件名不含换行）。
#   ★★★ 2026-10-11（#57）**外置到独立文件** `$COMPOSE_DIR/.farm-extra`：
#      **为什么不能再放 .env** —— compose 用 `env_file: [.env]` 读它，而 docker compose
#      的 env_file/.env 解析器**不支持多行值**。FARM_EXTRA 正是多行 ⇒ compose 一读就炸：
#          failed to read …/.env: line 69: unexpected character "," in variable name
#      ⇒ 生产上 `docker compose up/…` 整条命令**不可用**（2026-10-11 实测踩到）。
#      而 compose **根本不用** FARM_EXTRA（它只透传 DATA_DIRS；FARM_EXTRA 是 build-farm.sh 私事）
#      ⇒ 把它挪出 .env、单独一个文件，.env 恢复单行可解析，compose 就活了。
#   ★ 新文件格式：**纯文本，一行一条「农场相对路径」**（相对 $FARM），UTF-8，建议 **LF**：
#          Friends.S01.1994.…-HDH
#          Friends.S02.1995.…-HDH
#          是,大臣S01.Yes,Minister.1980.…@FRDS
#      读取规则：整文件逐行都是条目；**空行跳过**（剥 CR）；**不认 `#` 注释**（见下）。
#   ★ 与 FARM_SOURCES/FARM_DEEP_SOURCES 的**根本区别**：后两者是「源」，本脚本会去
#     枚举并 `cp -al` 建条目；FARM_EXTRA 是「**声明**」—— 本脚本**只认它、不建它**，
#     条目由人在农场侧手工 `ln`（AI 不写 NAS §A.10）。声明了却不存在 ⇒ 告警 + 计入 verify 漂移。
#   ★ 文件**不存在 / 为空** ⇒ 无声明 ⇒ 与旧行为**逐字节一致**（代码先上、行为惰性）。
#   ★★ 读取策略 = **硬切**（2026-10-11 定）：只认这个文件；**不再读** .env 里的 FARM_EXTRA。
#      语义单一、杜绝"两处声明各自漂开"（本仓反复吃过这个亏）。代价：迁移必须守序
#      （先建文件、再推本脚本、最后改 .env —— 见 summary/33 与 _tmp-tv1 设计稿 §9）。
#   ★★ **不认 `#` 注释**：本键外置的根因就是"名字里的逗号被朴素解析器切碎"；
#      `#` 注释会引入**同一类**静默风险（任何 `#` 开头的名字被当注释吃掉 ⇒ 丢声明 ⇒
#      被当孤儿删）。故一行就是一条，verbatim（只跳过"整行全空白"）。
EXTRA_FILE="$COMPOSE_DIR/.farm-extra"
if [ -f "$EXTRA_FILE" ]; then
  # 逐行 `s/\r$//`（容忍 CRLF；.env 曾因没剥 CR 踩坑，见下）,整行全空白删掉，其余原样。
  FARM_EXTRA=$(sed -e 's/\r$//' -e '/^[[:space:]]*$/d' "$EXTRA_FILE")
else
  FARM_EXTRA=""
fi
FARM_DEPTH=$(sed -n 's/^[[:space:]]*FARM_DEPTH[[:space:]]*=[[:space:]]*//p' .env | head -1 | tr -d '\r')
# 默认 2：与 reseed-state.py init --depth 的默认值对齐（见文件头）。
FARM_DEPTH="${FARM_DEPTH:-2}"
# 非正整数 ⇒ 回退 2（.env 里手滑写空/写错不能让整个巡检挂掉）。
case "$FARM_DEPTH" in
  ''|*[!0-9]*) FARM_DEPTH=2 ;;
  0)           FARM_DEPTH=2 ;;
esac

SRC_LIST="$FARM_SOURCES"; SRC_FROM="FARM_SOURCES"
if [ -z "$FARM_SOURCES" ]; then
  SRC_LIST="$DATA_DIRS"; SRC_FROM="DATA_DIRS（退回）"
fi
[ -n "$SRC_LIST" ] || die "$COMPOSE_DIR/.env 里既读不到 FARM_SOURCES 也读不到 DATA_DIRS"
say "期望集来自   : $SRC_FROM"
if [ -n "$FARM_DEEP_SOURCES" ]; then
  say "深镜像源     : $FARM_DEEP_SOURCES   （深度 $FARM_DEPTH）"
fi

# ★ 同一个源同时出现在 FARM_SOURCES 与 FARM_DEEP_SOURCES ⇒ 取更深值 + 告警（不静默）。
#   比对用**未 map 的原值**（两键都是 NAS 视角写的，map 只影响本机访问）。
if [ -n "$FARM_DEEP_SOURCES" ] && [ -n "$FARM_SOURCES" ]; then
  set -f; _oIFS=$IFS; IFS=,
  for _d in $FARM_DEEP_SOURCES; do
    [ -n "$_d" ] || continue
    case ",$FARM_SOURCES," in
      *",$_d,"*) say "  [!!] 告警：$_d 同时在 FARM_SOURCES 与 FARM_DEEP_SOURCES —— 按深源($FARM_DEPTH)处理" ;;
    esac
  done
  IFS=$_oIFS; set +f
fi

# ★ 自指闸：源里出现农场自己（或农场内的路径）= 清单还没跟农场拆开。
#   这是**必须硬停**的两种情况，而且都不报错、只静默变正确：
#     verify → 农场跟自己比，永远 0 漂移；
#     apply --prune → 期望集若退化成空，会把**整个农场删光**。
#   宁可停下让人看一眼，也不要它"顺利跑完"。
set -f
OLDIFS0=$IFS; IFS=,
FARM_M=$(map_path "$FARM")
N_SELF=0
# ★ 2026-10-05：深源也要走自指闸 —— 两键各遍历一趟（**不要**把它们拼成一个词，
#   `a,b,c` 这种拼接在 POSIX sh 里对 IFS 拆分的时机很反直觉、易漏判）。
for _gate in "$SRC_LIST" "$FARM_DEEP_SOURCES"; do
  for dd in $_gate; do
    [ -n "$dd" ] || continue
    set +f
    case "$(map_path "$dd")" in
      "$FARM_M"|"$FARM_M"/*) N_SELF=$((N_SELF + 1)) ;;
    esac
    set -f
  done
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
  for _c in "${FARM_MANIFEST_DIR:-}" "$COMPOSE_DIR/hlink" "$COMPOSE_DIR/data/hlink" \
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
say "  .farm-extra: $EXTRA_FILE （$([ -f "$EXTRA_FILE" ] && echo 存在 || echo '缺失 = 无 FARM_EXTRA 声明')）"
say "  农场       : $FARM"
say "  清单       : $MANIFEST"
say "  模式       : $( [ "$VERIFY_ONLY" = 1 ] && echo '仅校验' || { [ "$DRY" = 1 ] && echo 'DRY-RUN（不建）' || echo 'APPLY'; } )$([ "$PRUNE" = 1 ] && echo ' + prune')"
say "========================================================="

# ---------- 2) 逐 dataDir、逐子项处理（枚举登记 / 创建 两相） ----------
# set -f：关掉通配符展开。DC 的路径里有全角括号和点，虽然不含 * 或 ?，
# 但把 DATA_DIRS 按逗号拆开时让 shell 别去 glob 更稳妥。
set -f
OLDIFS=$IFS
IFS=,
N_NEW=0; N_HAVE=0; N_MISS=0; N_SRC=0; N_SKIP_DEV=0
# 本次的「期望集」：**农场相对路径**列表（给 prune 做精确比对）+ 相对路径→源（给人看）
# ★ 2026-10-05：键从 basename 升级为「农场相对路径」。深度 1 时二者逐字相同。
NAMES_TMP="$MANIFEST.names.tmp"
# 仅 --verify 用：期望集里有、农场里没有的（相对路径→源），供明细行打印
MISSING_TMP="$MANIFEST.missing.tmp"
# 每个源枚举出的相对路径落这里，再 `while read < 文件` 消费。
# ★ 绝不 pipe 进 while —— 那是子 shell，N_SRC/N_NEW 等计数器会丢。
ENUM_I="$MANIFEST.enum.i.tmp"
PRUNE_I="$MANIFEST.prune.i.tmp"
SEEN_I="$MANIFEST.seen.i.tmp"
COVER_TMP="$MANIFEST.cover.tmp"
# ★ 2026-10-11：FARM_EXTRA 里「声明了但农场里没有」的条目（verify 据此计漂移）。
EXTRA_MISSING="$MANIFEST.extra_missing.tmp"
: > "$NAMES_TMP"
: > "$MISSING_TMP"
: > "$SEEN_I"
: > "$COVER_TMP"
: > "$EXTRA_MISSING"
# ★ 只有真要落盘（--apply）才准备清单本体。verify/dry-run 写它纯属浪费 ——
#   115 KB 要经 SMB 来回跑一趟，最后还是删掉。
if [ "$DRY" = 0 ]; then : > "$MANIFEST.tmp"; fi
# ★★ 2026-09-12：三个中间文件原先只在一个分支里删，**--verify 会漏掉 $MANIFEST.tmp**
#    实测：跑完 --verify，/volume1/video/download/ 下留了一个 115632 字节的
#    .reseed_farm.manifest.tsv.tmp（大小和真清单一模一样）。
#    改成 trap 统一兜底：无论从哪条路出去（正常 exit / die / set -e 报错 /
#    Ctrl-C）都会收干净。apply 成功后这两个文件已被 mv 走，rm -f 是空操作。
trap 'rm -f "$PROBE_SRC" "$PROBE_DST" "$NAMES_TMP" "$MISSING_TMP" "$ENUM_I" "$PRUNE_I" "$SEEN_I" "$COVER_TMP" "$EXTRA_MISSING" "$MANIFEST.tmp"' \
  EXIT INT TERM

# _enum_rel <源目录> <深度>：把源枚举成「相对 $FARM 的路径」，最多 depth 层。
# ★ depth=1 ≡ 今天的 `"$dd"/*` 之 basename（逐字相同）⇒ 扁平源零变化。
# ★ 用 find -maxdepth（与下面 cp -al 的 find 同源依赖）；不用 shell 数组，
#   POSIX sh 没有。输出按 LC_ALL=C 排序，保证 NAMES_TMP 稳定、二次跑零 churn。
_enum_rel() {
  _dd="$1"; _depth="$2"
  find "$_dd" -mindepth 1 -maxdepth "$_depth" 2>/dev/null \
    | sed -e "s|^$_dd/||" | LC_ALL=C sort
}

# ★★ 2026-10-05：主循环遍历**两键的并集** —— 深源也必须被枚举/创建。
#   `$SRC_LIST` 里的源按 depth=1；`$FARM_DEEP_SOURCES` 里的源按 depth=$FARM_DEPTH。
#   同一源同时出现两处时**只跑一次**（取更深值，前面已告警）—— 用 SEEN_I 去重。
#   ★ 遍历两趟（先扁平后深）、而非把两键拼起来 —— 拼接会丢"哪个源该用哪个深度"。
for _pass in flat deep; do
  case "$_pass" in
    flat) _iter="$SRC_LIST" ;;
    deep) _iter="$FARM_DEEP_SOURCES" ;;
  esac
  for dd in $_iter; do
  [ -n "$dd" ] || continue
  set +f
  dd_orig="$dd"                 # ★ 未 map 的原值：拿去和 FARM_DEEP_SOURCES 比对
  # 去重 + 取深：一个源若**同时**在两键里，只在深源趟跑（深度取更深的那个）。
  #   ⇒ 扁平趟遇到"也在深源列表里的源"就跳过（前面已告警）。
  #   用一个 SEEN_I 记录**已整体处理过**的源，避免两趟重复枚举/重复记期望集。
  if grep -qxF "$dd_orig" "$SEEN_I" 2>/dev/null; then continue; fi
  if [ "$_pass" = flat ] && [ -n "$FARM_DEEP_SOURCES" ]; then
    case ",$FARM_DEEP_SOURCES," in
      *",$dd_orig,"*) continue ;;   # 交给深源趟处理
    esac
  fi
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

  # 深度：扁平趟 = 1（= 今天的直接子项）；深源趟 = FARM_DEPTH。
  depth=1
  [ "$_pass" = deep ] && depth="$FARM_DEPTH"
  printf '%s\n' "$dd_orig" >> "$SEEN_I"

  _enum_rel "$dd" "$depth" > "$ENUM_I"
  # ★ 只枚举、不创建嵌套：顶层节点一次 `cp -al` 会把整棵子树带出来；
  #   中间的嵌套节点（含 '/' 的 rel）**不单独建**，否则白跑、还可能踩到已建的树。
  while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    dst="$FARM/$rel"
    N_SRC=$((N_SRC + 1))

    # ★ 无论新建还是已存在，都要记进**期望集** —— prune 靠它判断"哪些该留"。
    #   （早期版本只记新建的，结果 prune 把已存在的一律当成过期删光。）
    printf '%s\n' "$rel" >> "$NAMES_TMP"
    if [ "$DRY" = 0 ]; then
      printf '%s\t%s\n' "$rel" "$dd/$rel" >> "$MANIFEST.tmp"
    fi

    if [ -e "$dst" ]; then
      N_HAVE=$((N_HAVE + 1))
      continue
    fi

    # ★★ 2026-10-05：嵌套节点（rel 含 '/'）**缺了**时怎么办？
    #   分两种情况，靠"顶层祖先在不在农场"判：
    #     (a) 顶层祖先**不存在** ⇒ 马上要对它做 `cp -al`，整棵子树（含本节点）都会
    #         被带出来 ⇒ 这里**跳过**，避免重复建（也避免踩到正在重建的树）。
    #     (b) 顶层祖先**已存在**（如上次建过的树里某个子目录被删了）⇒ 光跳过就
    #         **永远修不好**（父节点的 cp -al 因"已存在"被跳过）⇒ 必须**单独建本节点**。
    #   ★ 这正是"只增不删"巡检应有的自愈能力：增量修补，而非只认整树。
    if [ -n "$rel" ] && [ "$rel" != "${rel#*/}" ]; then
      _top="${rel%%/*}"
      [ -e "$FARM/$_top" ] && _need_nested=1 || _need_nested=0
      if [ "$_need_nested" = 0 ]; then
        continue   # 交给下面（或前面）顶层那次的 cp -al 带出
      fi
    fi

    if [ "$DRY" = 1 ]; then
      N_NEW=$((N_NEW + 1))
      # verify 时顺手把"缺的那条"记下来，下面打印明细用
      [ "$VERIFY_ONLY" = 1 ] && printf '%s\t%s\n' "$rel" "$dd/$rel" >> "$MISSING_TMP"
      continue
    fi

    # ★ cp -al：建目录结构 + 对每个文件建**硬链接**（不复制数据）
    src="$dd/$rel"
    if ! cp -al "$src" "$dst" 2>/dev/null; then
      # 退一步：低版本 cp 不认 -al 的组合时，走 find + ln
      mkdir -p "$dst" && find "$src" -type d -exec mkdir -p "$dst"/{} \; 2>/dev/null || true
      if ! ( cd "$src" && find . -type f -exec sh -c '
             d="$2"; mkdir -p "$(dirname "$d")"; ln "$1" "$d"' _ {} "$dst"/{} \; ) 2>/dev/null; then
        say "  [!!] 建链接失败: $rel"
        continue
      fi
    fi
    # 只给**目录**改属主：目录是新造的；文件是硬链接 = 同一个 inode，
    # 对其 chown 会连带改掉源文件属主（灾难）。所以这里绝不能加 -R。
    # ★ $SUDO 必须**不加引号**——要让它按词拆分成多个参数交给 find -exec。
    #   从 Windows/SMB 跑时 sudo 不存在 → 失败被 || true 吞掉，
    #   结果是"沿用服务端按登录用户给的属主"，不影响正确性。
    find "$dst" -type d -exec $SUDO chown "$OWNER" {} + 2>/dev/null || true

    N_NEW=$((N_NEW + 1))
  done < "$ENUM_I"
  done
done
IFS=$OLDIFS
set +f

# ★★ 2026-10-11：把 FARM_EXTRA 的**声明条目**并入期望集（见文件头「FARM_EXTRA」）。
#   位置很关键：必须在 N_EXPECT 计算**之前**、COVER 构建之前 —— 于是这些条目
#   ① 计入 N_EXPECT（print 总数）；② 进 COVER（⇒ is_covered 认它们 ⇒ prune 不删）；
#   ③ 但不影响枚举计数 N_SRC/N_HAVE（它们不是被枚举出来的）。
#   ★ 存在性：声明了却不在农场 ⇒ 告警 + 记进 EXTRA_MISSING（下面 verify 段据此计漂移）。
#   ★★ 分隔符 = **换行**（每行一条）—— 发布名可能含 ASCII 逗号（是,大臣 / Yes,Minister），
#      逗号分隔会被切碎。故此处按行读（IFS= read -r），不再按逗号切。
#   格式闸：空行跳过 / 绝对路径（/*）/ 含 `..` 段 → 直接 die（配置错要早停，别静默）。
N_EXTRA=0; N_EXTRA_MISS=0
if [ -n "$FARM_EXTRA" ]; then
  while IFS= read -r _ex; do
    [ -n "$_ex" ] || continue
    case "$_ex" in
      /*|..|../*|*/../*|*/..) die "FARM_EXTRA 条目必须是**农场相对路径**（非绝对、不含 ..）: '$_ex'" ;;
    esac
    printf '%s\n' "$_ex" >> "$NAMES_TMP"
    N_EXTRA=$((N_EXTRA + 1))
    if [ ! -e "$FARM/$_ex" ]; then
      say "  [!!] FARM_EXTRA 声明了但农场里不存在（漏 ln / 名字错？）: $_ex"
      printf '%s\n' "$_ex" >> "$EXTRA_MISSING"
      N_EXTRA_MISS=$((N_EXTRA_MISS + 1))
    fi
  done <<EOF
$FARM_EXTRA
EOF
  [ "$N_EXTRA" -gt 0 ] && say "FARM_EXTRA    : 声明 $N_EXTRA 条（手工 ln 条目，本脚本只认不建）$([ "$N_EXTRA_MISS" -gt 0 ] && echo "  ★ 其中 $N_EXTRA_MISS 条农场里不存在")"
  # ★ 去重 + 稳定排序：FARM_EXTRA 可能与镜像条目重名（那是**冗余声明**，不报错），
  #   也可能与其它 FARM_EXTRA 条目重复 —— 二者都靠 sort -u 收敛，保证 NAMES_TMP 稳定。
  LC_ALL=C sort -u "$NAMES_TMP" -o "$NAMES_TMP"
fi

N_EXPECT=$(wc -l < "$NAMES_TMP" 2>/dev/null | tr -d ' \t' || echo 0)

# ★★ 2026-10-05：「覆盖集」—— 判孤儿的唯一正确口径。
#   不变量：农场由「对每个**顶层**期望条目一次 `cp -al`」建成 ⇒ 农场里**任何**路径
#   要么是某个期望条目本身，要么是它的子孙。于是：
#       农场路径 rel 合法  ⟺  rel 是期望条目的前缀之一（= rel ∈ COVER）
#       COVER = { 每个期望 rel 的所有前缀（含自身，不含空串） }
#   例：期望 {PackA, Arrow.S01-S08, Arrow.S01-S08/Arrow.S01.Bluray}
#       ⇒ COVER = {PackA, Arrow.S01-S08, Arrow.S01-S08/Arrow.S01.Bluray}
#       农场里的 PackA/single/s.mkv 的祖先 PackA ∈ COVER ⇒ **合法**（cp -al 带出的）。
#   ★ 这条同时解释了为什么"扁平源的子树"不会被误报孤儿（旧代码只数一层、看不见它，
#     新代码看见了、但用 COVER 判它合法）。
#   ★ 也解释了为什么不能用"全局 DEPTH_ANY"——扁平源按深度 1 枚举，却带出整棵子树，
#     拿统一深度去数必然把 `PackA/single` 当孤儿（本计划最易错处）。
# （COVER_TMP 已在上面临时文件区声明并清空；这里只需重建内容。）
if [ "$N_EXPECT" -gt 0 ]; then
  while IFS= read -r _rel; do
    [ -n "$_rel" ] || continue
    # 逐个前缀：a/b/c → a、a/b、a/b/c
    printf '%s\n' "$_rel" | awk -F/ '{p=""; for(i=1;i<=NF;i++){p=(i==1)?$i:p"/"$i; print p}}'
  done < "$NAMES_TMP" >> "$COVER_TMP"
fi
# ★ 排序去重；COVER 必须用 LC_ALL=C sort -u（与 find 的名称排序一致）。
LC_ALL=C sort -u "$COVER_TMP" -o "$COVER_TMP"

# is_covered <rel>：rel 本身或其**任一祖先** ∈ COVER？
#   ★ 判据是"整条链上任一前缀命中"，不是只看直接父 —— 因为 `cp -al` 会带出
#     **多层**子树（扁平源的 PackA/single/s.mkv：父 PackA/single 不在 COVER，
#     但祖父 PackA 在）。只看直接父会把深层文件全误报孤儿。
is_covered() {
  grep -qxF "$1" "$COVER_TMP" 2>/dev/null && return 0
  _c="$1"
  while :; do
    case "$_c" in
      */*) _c="${_c%/*}" ;;
      *)   break ;;
    esac
    grep -qxF "$_c" "$COVER_TMP" 2>/dev/null && return 0
  done
  return 1
}

# farm_rel_all：把农场里**所有**条目（任意深度）以「农场相对路径」列出。
# ★ 孤儿判据用全深度（不是 DEPTH_ANY）—— 才能看见深树里被删源的残留。
farm_rel_all() {
  find "$FARM" -mindepth 1 2>/dev/null | sed -e "s|^$FARM/||" | LC_ALL=C sort
}

# N_FARM = 农场里**合法**（∈ COVER）的条目数 —— 与 N_HAVE 同口径（都是"期望条目存在"），
#   区别只是 N_HAVE 在枚举时数、N_FARM 从盘上数。二者应相等（都 = |期望 ∩ 盘|）。
#   无深源时 COVER = 期望的一层名字 ⇒ N_FARM ≡ 旧 `"$FARM"/*` 计数（逐字等价）。
# ★ N_FARM_RAW = 农场里**所有**条目数（不论合法与否）—— **prune 安全闸必须用它**：
#   期望集为空时 N_FARM(=0) 会骗过"农场里有东西吗"的判断，让 prune 静默跳过而不是报警。
N_FARM=0
farm_rel_all > "$PRUNE_I"
N_FARM_RAW=$(wc -l < "$PRUNE_I" 2>/dev/null | tr -d ' \t'); [ -n "$N_FARM_RAW" ] || N_FARM_RAW=0
if [ "$N_EXPECT" -gt 0 ]; then
  while IFS= read -r _r; do
    [ -n "$_r" ] || continue
    is_covered "$_r" && N_FARM=$((N_FARM + 1))
  done < "$PRUNE_I"
fi

# ---------- 3) prune：农场里有、期望集里没有 → 源已经没了（或改名了）----------
N_PRUNE=0
if [ "$PRUNE" = 1 ] && [ "$N_FARM_RAW" -gt 0 ]; then
  # ★ 安全闸：期望集为空却要 prune = 几乎一定是 DATA_DIRS 读错/全都没了，
  #   真删下去会把**整个农场删光**。宁可停下让人看一眼。
  if [ "$N_EXPECT" -eq 0 ]; then
    say ""
    say "  [!!] 期望集为 0，但农场里有 $N_FARM_RAW 条 —— 拒绝 prune。"
    say "       多半是 $COMPOSE_DIR/.env 的 DATA_DIRS 读不到，或所有源目录都不存在。"
    say "       先跑一次不带 --prune 的 --apply 看清楚，再决定。"
  else
    # ★ 2026-10-05：孤儿 = 农场里**不在覆盖集**里的条目。用相对路径键（深度 1 时 ≡ basename）。
    #   只报/删**孤儿树的顶层**（父已被覆盖、或本身就是顶层）—— 否则一棵被删的深树会
    #   每层各报一次、`--prune` 还会逐层 rm -rf（虽幂等但噪音大且易误读）。
    #   子孙保护天然成立：某孤儿若有**被覆盖的子孙**（不该发生，除非源被部分删），
    #   COVER 里只有前缀、不会覆盖"孤儿之下的活条目" —— 故此处额外用 grep 兜一道。
    #   ★ 落临时文件再 read，绝不 `find | while` —— 那是子 shell，N_PRUNE 会丢。
    farm_rel_all > "$PRUNE_I"
    while IFS= read -r rel; do
      [ -n "$rel" ] || continue
      is_covered "$rel" && continue
      # 只处理"孤儿树的**顶层**"：其父要么是农场根（视为已覆盖）、要么已被覆盖。
      # 一棵整树消失时，只有顶层那行的父是"根"，其余内层的父都未覆盖 ⇒ 被跳过。
      _parent="${rel%/*}"
      [ "$_parent" = "$rel" ] && _parent=""        # 顶层：无父（= 根）
      if [ -n "$_parent" ] && ! is_covered "$_parent"; then
        continue
      fi
      if [ "$DRY" = 1 ]; then
        say "  [prune·预演] 将删除: $rel"
      else
        say "  [prune] 源已不存在，删除农场条目: $rel"
        rm -rf "$FARM/$rel"
      fi
      N_PRUNE=$((N_PRUNE + 1))
    done < "$PRUNE_I"
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
# ★ 2026-10-05：有深源时 N_SRC 含**嵌套层**（不只直接子项）—— 而 N_NEW 是**如实计数**
#   （每个顶层直接子项算 1，一个深源 = 父容器 + N 个子层，不是一次 cp -al 计 1）。
# ★ 2026-10-11：有 FARM_EXTRA 时 N_EXPECT ≠ N_SRC（extras 不在枚举计数里，且可能与镜像去重）
#   ⇒ 这里把 extras 单列，避免"$N_SRC = $N_EXPECT"看起来自相矛盾。
say "期望集条目合计 : $N_EXPECT   （镜像枚举 $N_SRC 条$([ "$N_EXTRA" -gt 0 ] && echo " + FARM_EXTRA 声明 $N_EXTRA 条，去重后").$([ -n "$FARM_DEEP_SOURCES" ] && echo '  ★ 含嵌套层（深源按 FARM_DEPTH 展开）')）"
if [ "$VERIFY_ONLY" = 1 ]; then
  # 三条漂移各自是**不同的事**，不能合成一个数：
  #   源有农场无 → 新内容没进农场（cross-seed 白跑，搜不到这部）
  #   农场有源无 → **源删了/改名了，农场那条还留着**（cross-seed 照样拿去搜 = 白烧额度）
  #   源目录本身缺失 / 跨卷 → 期望集不完整，**上面的数都不可信**（比有漂移更严重）
  # ★ 2026-10-11：**不能再**用 `N_EXPECT - N_HAVE` 算"源有农场无"。
  #   成因：N_EXPECT 现在含 FARM_EXTRA 声明条目；而声明条目里若有**与镜像条目重名**的
  #   （冗余声明，sort -u 去重后从 NAMES_TMP 里消失），N_EXTRA 就**多算**了 ⇒ 减出来可能
  #   为负（实测 -1）。★ 教训：拿"两个集合的势之差"当漂移数，前提是两集合同构 ——
  #   一旦混进第三类来源（这里是 extras），这个前提就没了。
  #   正解：**直接数 MISSING_TMP** —— 它在枚举时对**每个"镜像有、农场没有"的条目**恰好
  #   落一行（仅 --verify 落），与 extras 天然无关，既不重复也不会负。
  N_MISSING=$(wc -l < "$MISSING_TMP" 2>/dev/null | tr -d ' \t'); [ -n "$N_MISSING" ] || N_MISSING=0
  # ★ 2026-10-05：孤儿 = 农场里**不在覆盖集**的**顶层**条目（内层交给它那次的整树处理）。
  #   不再用 `N_FARM - N_HAVE` —— 那个恒等式只在"农场与期望同粒度"时成立，
  #   混合扁平+深源时 N_FARM（合法条目）含深层、N_HAVE（枚举计数）粒度不同，会算错。
  N_ORPHAN=0
  farm_rel_all > "$PRUNE_I"
  while IFS= read -r _r; do
    [ -n "$_r" ] || continue
    is_covered "$_r" && continue
    _p="${_r%/*}"
    [ "$_p" = "$_r" ] && _p=""
    if [ -n "$_p" ] && ! is_covered "$_p"; then continue; fi
    N_ORPHAN=$((N_ORPHAN + 1))
  done < "$PRUNE_I"
  say "农场现有条目   : $N_FARM   （合法：∈ 期望覆盖范围）"
  say "源有但农场没有 : $N_MISSING"
  say "农场有但源没有 : $N_ORPHAN"
  [ "$N_EXTRA" -gt 0 ] && say "FARM_EXTRA     : 声明 $N_EXTRA 条（手工 ln），其中缺 $N_EXTRA_MISS 条"
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
    # ★ 2026-10-05：用 COVER 判孤儿（与上面 N_ORPHAN 同口径、同粒度为"顶层"）。
    farm_rel_all > "$PRUNE_I"
    while IFS= read -r nm; do
      [ -n "$nm" ] || continue
      is_covered "$nm" && continue
      _p="${nm%/*}"
      [ "$_p" = "$nm" ] && _p=""
      if [ -n "$_p" ] && ! is_covered "$_p"; then continue; fi
      if [ "$SHOWN" -lt 20 ]; then say "      $nm"; fi
      SHOWN=$((SHOWN + 1))
    done < "$PRUNE_I"
    [ "$SHOWN" -gt 20 ] && say "      …还有 $((SHOWN - 20)) 条"
    say "      ★ 删不删由人定：确认源真的没了，再跑 --apply --prune。"
    say "        本脚本**只报告、绝不自动删** —— 判「源没了」的依据就是上面这份清单，"
    say "        拿它自动删等于把一次误判放大成不可逆的数据丢失。"
  fi

  if [ "$N_EXTRA_MISS" -gt 0 ]; then
    say ""
    say "  [漂移] FARM_EXTRA 声明了、农场里却没有（漏 ln 或名字打错）："
    SHOWN=0
    while IFS= read -r nm; do
      [ -n "$nm" ] || continue
      if [ "$SHOWN" -lt 20 ]; then say "      $nm"; fi
      SHOWN=$((SHOWN + 1))
    done < "$EXTRA_MISSING"
    [ "$SHOWN" -gt 20 ] && say "      …还有 $((SHOWN - 20)) 条"
    say "      ★ 这些条目不会被 --apply 自动创建（本脚本只认不建）—— 请手工 ln 后再 verify。"
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
  if [ "$N_MISSING" -gt 0 ] || [ "$N_ORPHAN" -gt 0 ] || [ "$N_EXTRA_MISS" -gt 0 ] || \
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
  [ "$N_EXTRA" -gt 0 ] && say "  FARM_EXTRA   : 声明 $N_EXTRA 条（手工 ln，本脚本不建），其中缺 $N_EXTRA_MISS 条"
  [ -n "$FARM_DEEP_SOURCES" ] && say "  ★ 含嵌套层：一个深源 = 父容器 + N 个子层（按 FARM_DEPTH=$FARM_DEPTH 展开）"
  say ""
  say "看清楚了就加 --apply 真建。"
else
  say "★ 新建         : $N_NEW"
  say "  已存在(跳过) : $N_HAVE"
  say "  源目录缺失   : $N_MISS"
  say "  跨卷跳过     : $N_SKIP_DEV"
  [ "$PRUNE" = 1 ] && say "  已删除       : $N_PRUNE"
  [ "$N_EXTRA" -gt 0 ] && say "  FARM_EXTRA   : 声明 $N_EXTRA 条（手工 ln，本脚本不建），其中缺 $N_EXTRA_MISS 条"
  [ -n "$FARM_DEEP_SOURCES" ] && say "  ★ 含嵌套层：一个深源 = 父容器 + N 个子层（按 FARM_DEPTH=$FARM_DEPTH 展开）"
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
