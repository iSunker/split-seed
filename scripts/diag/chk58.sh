#!/bin/sh
# chk58.sh —— `#58`「drive-loop 迁容器」：**启用容器前**的 NAS 侧只读验证
#
# ★★ 这个文件是**重建**的，原版已经丢了
# -------------------------------------
# 原来那份叫 `<compose>/_tmp58/_chk58.sh`（一次性件，放临时目录、按 `A.2.1` 用完删）。
# **但它删掉的时候，判据一起没了** —— 仓库里 `git log --all -p -S chk58` 搜不到任何代码，
# 只有两处提到它的**名字**（`ENVIRONMENT.md` A.2.1 的命名示例、`SUMMARY §26.5` 的状态行）。
# 「五段只读验证」要验哪五段，**当时没有任何地方记下来**。
# ⇒ 教训（已落到 `ENVIRONMENT.md` A.2.1）：
#   **"用完就删"对临时件是对的，但"判据"必须留在文档或仓库里** ——
#   删掉的是脚本，不该是"该验什么"这件事本身。
# ⇒ 所以本次**改变交付方式**：入库为 `scripts/chk58.sh` + 进 `check-deploy-drift.py` 的
#   `LOCAL_ONLY`（与 `drive-loop-mount-selfcheck.sh`、`chk-volume1-free.sh` 同一处置 ——
#   在 NAS 上跑、不进容器、不部署）。
#
# 它在回答什么问题
# ----------------
# `#58` 的代码侧已经就绪（15.a 心跳修复 / compose `drive-loop` 服务 + `profiles` 锁 /
# 常驻入口 `run-resident.sh`）。**启用容器之前**必须为真的那些前提，本脚本逐条去读现状。
# ★ 它**不**证明"容器里跑得起来"——那要真起容器、真跑一批（本脚本做不到，也**不假装**做到）。
#
# ★★ 六段，每段都**只读**，且**要么给数、要么明说查不出**
# ------------------------------------------------------
#   [1] 镜像在不在、标对不对（`§26.5` 那句"已 load"是 09-16 的读数，**要现读**）
#   [2] 挂载所需的宿主路径与关键文件都在（含本次刚进白名单的 `run-resident.sh`）
#   [3] ★★ `drive-loop/scripts/` 下**六个** `.state` 文件都在且可读
#   [4] `.env` 路径前缀与挂载点一致 + `state.db` 里的绝对路径可 stat
#   [5] ★★ **两条路没同时在跑**（`.drive-loop.state` 的 mode / pid / 心跳现状）
#   [6] DSM 任务与 `attempts.log` 里的 `[resident]` 计数（人工核对项）
#
# ★ 为什么 [3] 是重点：漏挂一个 `.state` 文件**不报错**，只是把对账/无人认领的**基线
#   当成"首次读数"重新记一遍**（`first_existing()` 返回 None + 一行 warning）——
#   **静默**改状态。`.linkguard.state` 是其中最大的（约 827 KB，那是基线本身）。
#   ★ 顺带更正一处**陈旧描述**：`docker-compose.yml` 与 `README` 15.e 原文写"**五个**"
#     `.state` 文件，2026-09-17 实测是**六个**（见 [3] 的清单）。
#
# 用法（**在 NAS 上跑**：DSM → 控制面板 → 终端机和 SNMP，或 Container Manager「终端」）
# --------------------------------------------------------------------------------
#   sudo sh chk58.sh              # 全跑
#   sh chk58.sh --section 3       # 只跑一段（调试用；只读，不需要 sudo）
#
# ★ 退出码（照 `drive-loop-mount-selfcheck.sh` 的语义）：
#     0 = 全过（可以往下走「停 DSM 任务 → up -d」）
#     1 = 有 ✗ ⇒ **别往下走**（先解决它）
#     2 = 查不了（缺 docker / 缺文件 —— 这是"未验"，**不是**"没问题"）
#
# ★ 本脚本**只读**：没有 rm / mv / cp / chmod / chown / truncate，
#   也**不写任何文件**（全部打 stdout —— `/volume1` 是满卷，别往那儿重定向，`ERR-FS-03`）。
#
# ★★ 本机自测（2026-09-17，假树；照 `drive-loop-mount-selfcheck.sh` 的规矩：
#    **正例全绿 + 每个反例必须红**）—— 顺带在本机**抓出四个自己的毛病**，都是
#    「看着绿、其实什么也没验」那一族：
#
#   | 用例 | 期望 | 实测 |
#   |---|---|---|
#   | 正例：假树六件齐、pack 一行可达、idle | rc=0 全绿 | ✅ |
#   | 反例 A：删 `.linkguard.state` | rc=1 且**点名**它 | ✅ |
#   | 反例 B：多出一个未登记 `.state` | 提示"清单过时"（不置红） | ✅ |
#   | 反例 C：`--compose` 传**相对路径** | rc=2 未验 | ✅ **本机抓到的毛病 ①** |
#   | 反例 D：`--compose` 传 Windows 盘符 | rc=2 未验 | ✅ **毛病 ②** |
#   | 反例 E：`state.db` 的 `pack` 表**空** | rc=1「什么也没验」 | ✅ **毛病 ③（空集假绿）** |
#   | 反例 F：`pack.root` 指向不存在的路径 | rc=1 且点名 | ✅ |
#   | 反例 G：`.drive-loop.state` 是**坏 JSON** | rc=1「不能确认」 | ✅ |
#
#   ★★ 毛病 ① / ② —— **本机有 Docker Desktop**，传相对路径时 [1] 会拿**本机**的
#      docker 去 `image inspect`，并**真报出一个 ✓**（本机镜像列表里恰好有
#      `reseed-drive-loop:0.1.0`）。它与 NAS 上有没有**毫无关系**。
#      ⇒ 于是加了"必须绝对路径 + 不许盘符"两道闸，认不出 NAS 就 **rc=2（未验）**。
#   ★★ 毛病 ③ —— `pack` 表空 ⇒ 遍历一圈都没进 ⇒ 印「不可达数：0」，
#      **与"全可达"逐字相同，但它一条都没验**（B.10 第 14 条）。⇒ 改成显式报
#      「读到 pack N 行」，且 N==0 时**置红**。
#   ★★ 毛病 ④ —— python heredoc 里打了 U+21D2，GBK 控制台编不出 ⇒ python 抛
#      UnicodeEncodeError ⇒ 被 2>/dev/null 吞掉 ⇒ 判读退化成读不出来 ⇒ **假红**。
#      修法与 drive-loop-nas.sh / run-resident.sh 同：顶部 export PYTHONIOENCODING=utf-8。
#   ★ 另一个坑（**注释里也要守**）：shell 的取词器会**在注释里**数引号与反引号。
#     本机实测：一句注释里同时出现反引号与不配对的双引号（形如 反引号 + 2>/dev/null
#     || echo 问号 + 反引号），dash 从那里开始"吞"到后面的代码，报错跑到第 102 行。
#     ⇒ **注释里只用成对的 ASCII 引号，别写反引号包代码片段。**
#   ★ 另有一条**文案**坑：双引号里写反引号会被 shell 当**命令替换**执行，
#     `--once` 直接消失（实测把「宿主 `--once` 那条路」印成「宿主  那条路」）。
#     ⇒ 那几行一律改**单引号**。★ 与 README「别让判据需要转义」同族：**别让文案需要转义**。
#
# ★★★ 最花时间的一条（记在这里，免得下一个人重走）：**行尾必须是 LF，不能是 CRLF。**
#   第一版被写成了 **CRLF**（本机是 Windows）。`dash` / BusyBox `ash` 在 `do`、`then`
#   这类**保留字**后面**不认** 那个 `\r` —— 它把 `do\r` 当成没结束 ⇒ `while … do`
#   结构没闭合 ⇒ 报错落在**下一段的 `case … in`**：
#       scripts/chk58.sh: 102: Syntax error: word unexpected (expecting "in")
#   ★★ 为什么这条极难查（我在这里绕了很久）：
#     · `bash -n` **不报**（bash 容得下 CRLF）⇒「本机验过了」是**假绿**；
#     · `sed -n 'Np'`、python 的 `splitlines()` **看不见它**（把 `\r\n` 当行尾吃掉）
#       ⇒ 逐行打印"看着完全正常"；
#     · 我据此先后错怪了**全角括号、注释里的反引号、heredoc 形状** —— 全不是。
#     ⇒ 只有 `cat -A`（行尾显 `^M$`）或数 `\r` 才看得见。**先量行尾，再怀疑语法。**
#   ⇒ 本仓其余 `.sh` **都是 LF**（实测 `drive-loop-resident.sh` / `chk-volume1-free.sh`
#     的 `\r` 计数都是 0）⇒ 新脚本也要 LF。**验证：`grep -c $'\r' scripts/chk58.sh` 得 0。**
#   ★ 一句话：**「bash 说没问题」≠「NAS 上的 ash 说没问题」。**

set -u

# ★★ 让 python 的 stdout/stderr 强制 UTF-8 —— **否则本脚本会在 GBK 控制台上自己打挂自己**。
#   我写它时在本机自测里实测栽过一次：两个 heredoc 里都打了 `⇒`（U+21D2），
#   而 GBK 编不出它 ⇒ python 抛 UnicodeEncodeError **整个脚本段崩掉** ⇒
#   外面的 `2>/dev/null || echo "?"` 把异常吞了 ⇒ 判读退化成"读不出来" ⇒ **假红**。
#   ★ 与 `tests/README.md` 那条「`⑪` 在 GBK 里编不出去」**同一族**，也与
#     `drive-loop-nas.sh` / `run-resident.sh` 里那句 `PYTHONIOENCODING=utf-8` 同一处置。
export PYTHONIOENCODING=utf-8
# ★ 同时把 LC_ALL 钉成 C 之外的值没有意义（群晖上是 C）；真正的保障是上面那行。

COMPOSE_DIR="${COMPOSE_DIR:-/volume2/docker_ssd/reseed}"
VOL1="${VOL1:-/volume1/video}"
STATE="$COMPOSE_DIR/drive-loop/scripts/.drive-loop.state"
DB="$COMPOSE_DIR/drive-loop/hlink/state.db"

ONLY=""
while [ $# -gt 0 ]; do
    case "$1" in
        --section) shift; ONLY="${1:-}" ;;
        --compose) shift; COMPOSE_DIR="${1:-}" ;;
        # ★ 这一行**故意用半角括号** —— nose：
        #   dash / BusyBox ash 的取词器对**成对的全角括号 + 反引号**会误解，
        #   于是 case/for 结构被截断，报错出现在**下一段**
        #   （本机实测：报在第 102 行 case 的 in，真因在这一行）。
        #   ⇒ **给 shell 看的字符串里只用 ASCII 标点。**
        #   ★ 与"判据别需要转义"同一族：**别让语法需要解释器猜。**
        *) echo "未知参数: $1   (可用: --section N | --compose <dir>)" >&2; exit 2 ;;
    esac
    shift
done
# ★★ 先把 COMPOSE_DIR 归一 —— 它可能从 `$(pwd -W)` 之类的地方带进**换行**，
#    而带换行的路径会让后面每一条 `[ -e ]` 都变成"文件不存在"的**假红**
#    （我写这个脚本时在本机自测里就栽了一次：`--compose` 传进来一串两行，
#     于是 [2]/[3]/[4]/[6] 全报"不在"，而假树其实好好的）。
#    ⇒ 去掉所有空白与回车，只留一行。
COMPOSE_DIR=$(printf '%s' "$COMPOSE_DIR" | tr -d '\r\n' | sed 's/[[:space:]]*$//')
STATE="$COMPOSE_DIR/drive-loop/scripts/.drive-loop.state"
DB="$COMPOSE_DIR/drive-loop/hlink/state.db"

# ★★ 机器守卫：本脚本是**在 NAS 上跑**的（它读 NAS 原生路径与 NAS 上的 docker）。
#    在开发机（Windows + Docker Desktop）上跑会得到**完全错误的通过** ——
#    `docker image inspect` 查的是**本机**的 docker，而 `/volume1` 在本机不存在。
#    ⇒ 那正是本项目一直在抓的形状：**看着绿、其实什么也没验**。
#    （本机自测时**实测撞上**：用相对路径 `_chk58t/compose` 传进来 ⇒ [1] 当场
#      用本机 Docker Desktop 报了个 `✓ 镜像在`。本机的 docker 里**确实**有这个镜像，
#      但那是另一台 daemon —— 与 NAS 上有没有**毫无关系**。）
#    ⇒ 判据两条：① 不许是 Windows 盘符；② **必须是绝对路径**（群晖上就是 /volumeX/...）。
#      任一条不满足 ⇒ 退出码 2（未验），而不是打一串 ✗ 或 ✓。
case "$COMPOSE_DIR" in
    [A-Za-z]:[/\\]*)
        echo "!! COMPOSE_DIR 看起来是 **Windows 路径**（$COMPOSE_DIR）——" >&2
        echo "   本脚本要在 **NAS 上**跑（它读 /volume1 与 NAS 的 docker）。" >&2
        echo "   ⇒ 这是『未验』，不是通过。上 NAS 再跑（DSM → 终端机 / Container Manager）。" >&2
        exit 2 ;;
    /*) : ;;                     # 绝对路径 ⇒ 放行
    *)
        echo "!! COMPOSE_DIR 不是绝对路径（$COMPOSE_DIR）——" >&2
        echo "   ★★ 这条不是吹毛求疵：[1] 会拿**本机**的 docker 去查镜像，" >&2
        echo "      在开发机上会得到一个与 NAS 无关的 ✓（实测撞过）。" >&2
        echo "   ⇒ 改传绝对路径（NAS 上是 /volume2/docker_ssd/reseed）。" >&2
        exit 2 ;;
esac

RC=0
bad() { printf '  ✗ %s\n' "$*"; RC=1; }
ok()  { printf '  ✓ %s\n' "$*"; }
na()  { printf '  ─ %s\n' "$*"; }          # 不置红：中性信息（但它**必须**有内容，别留空）
UNK=0                                       # "查不出"计数 ⇒ 决定退出码 2

want() { [ -z "$ONLY" ] || [ "$ONLY" = "$1" ]; }   # 这一段要不要跑

echo "=== chk58.sh（#58 启用容器前的只读验证）  $(date '+%Y-%m-%d %H:%M:%S')  ==="
echo "    COMPOSE_DIR=$COMPOSE_DIR"
echo "    VOL1=$VOL1"
echo

# =====================================================================
if want 1; then
echo "--- [1] 镜像：reseed-drive-loop:0.1.0 在不在 ---"
# ★ docker 这一段的**前提**是"这是 NAS 上那个 docker daemon"。本脚本已在开头拦了
#   Windows 盘符路径；这条再补一层：**没看见 NAS 的 compose 目录就不信本机 docker**
#   （否则在开发机上 `docker image inspect` 会查到**本机**的镜像，报一个假 ✓）。
if [ ! -d "$COMPOSE_DIR" ]; then
    na "看不到 $COMPOSE_DIR ⇒ [1] **未验**（本机若也有 docker，那是**另一个** daemon，查它无意义）"
    UNK=$((UNK+1))
elif command -v docker >/dev/null 2>&1; then
    if docker image inspect reseed-drive-loop:0.1.0 >/dev/null 2>&1; then
        ok "镜像在：reseed-drive-loop:0.1.0"
        # 顺带把它的 ID / 大小打出来——"在"不够，还要能对上"是哪一份"
        docker image inspect --format '      id={{.Id}}  size={{.Size}}B  created={{.Created}}' \
            reseed-drive-loop:0.1.0 2>/dev/null || na "（inspect 取不到 ID/大小）"
    else
        bad "没有 reseed-drive-loop:0.1.0 ⇒ 容器起不来（§26.5 那句『已 load』是 09-16 的读数，要现读）"
        na  "  受控核对： docker images | grep drive-loop"
    fi
    # 顺带看一下 autoheal 那个侧车镜像在不在（启用时会拉；没有就是"还没拉过"）
    if docker image inspect willfarrell/autoheal:latest >/dev/null 2>&1; then
        ok "镜像在：willfarrell/autoheal:latest（接管侧车）"
    else
        na "willfarrell/autoheal:latest 本机还没有 —— 启用时 compose 会去拉（NAS 能连仓库，见 README）"
    fi
else
    na "本机没有 docker 命令 ⇒ [1] **未验**（不是通过）"
    UNK=$((UNK+1))
fi
echo
fi

# =====================================================================
if want 2; then
echo "--- [2] 挂载所需的宿主路径与关键文件 ---"
if [ -d "$COMPOSE_DIR" ]; then ok "compose 目录在：$COMPOSE_DIR"; else bad "compose 目录不在 ⇒ 1:1 挂载必然挂空"; fi
if [ -d "$VOL1" ]; then ok "媒体卷在：$VOL1"; else bad "媒体卷不在：$VOL1"; fi
# ★ 这几样是代码里的**绝对路径**依赖（挑挂会静默退化，见 README 15.e）
for rel in \
    "drive-loop/scripts/drive-loop.py" \
    "drive-loop/run.sh" \
    "drive-loop/run-resident.sh" \
    "cross-seed/cross-seed.db" \
    "cross-seed/logs/info.current.log" \
    "drive-loop/hlink/state.db"
do
    if [ -e "$COMPOSE_DIR/$rel" ]; then
        ok "$rel"
    else
        bad "$rel 不在 ⇒ 对应功能会**静默退化**，不是报错"
    fi
done
# ★ run-resident.sh 单独点名：它已随代码入镜像（compose 的 entrypoint 指向 `/app/run-resident.sh`）
if [ ! -e "$COMPOSE_DIR/drive-loop/run-resident.sh" ]; then
    na "  ↑ run-resident.sh 缺 = 挂载区没有这份（★ 2026-10-05 起入口在镜像里，此处应视为历史挂载件）"
fi
echo
fi

# =====================================================================
if want 3; then
echo "--- [3] ★★ drive-loop/scripts/ 下的 .state 文件（漏一个 = 基线被静默重记）---"
SD="$COMPOSE_DIR/drive-loop/scripts"
if [ ! -d "$SD" ]; then
    bad "$SD 不在 ⇒ [3] 无法判（这个目录不在 hlink/ 里，最容易漏挂）"
else
    # 逐个点名，**不带通配兜底** —— 通配会让"一个都没有"也打印成功（空集假绿）
    # ★ 2026-10-05：`.notify.state` 从**必需**清单里删除 —— 通知写侧已砍，不再产生它。
    MISS=0
    for f in .daily-report.state .drive-loop.state .farm-check.state \
             .linkguard.state .reconcile.state
    do
        p="$SD/$f"
        if [ -f "$p" ]; then
            SZ=$(wc -c < "$p" 2>/dev/null | tr -d ' ')
            ok "$f  (${SZ:-?} B)"
        else
            bad "$f **不在** ⇒ 那个巡检/对账会把现状当成『首次读数』重新起锚（静默）"
            MISS=$((MISS+1))
        fi
    done
    # ★ 反向：目录里**还有没有别的** .state（说明我这份清单本身过时了）
    #   ★ 2026-10-05：白名单里**保留 `notify`** —— 通知写侧虽已砍，NAS 上可能还遗留
    #     一个旧的 `.notify.state`，留着它免得把"遗留"误报成"清单过时"。
    EXTRA=$(ls -a "$SD" 2>/dev/null | grep -E '^\..*\.state$' \
            | grep -vE '^\.(daily-report|drive-loop|farm-check|linkguard|notify|reconcile)\.state$' || true)
    if [ -n "$EXTRA" ]; then
        na "★ 清单之外还有 .state 文件（本脚本的清单该更新了）："
        printf '%s\n' "$EXTRA" | sed 's/^/        /'
    else
        ok "没有清单之外的 .state 文件（5 件必需 + notify 遗留的白名单是完整的）"
    fi
    # ★ .linkguard.state 是最大的那个（基线）—— 特别提醒它的量级
    if [ -f "$SD/.linkguard.state" ]; then
        SZ=$(wc -c < "$SD/.linkguard.state" 2>/dev/null | tr -d ' ')
        na "  参考量级：.linkguard.state ≈ 827 KB（2026-09-17 实测）；现读 ${SZ:-?} B —— 差太多要问为什么"
    fi
fi
echo
fi

# =====================================================================
if want 4; then
echo "--- [4] .env 路径前缀 vs 挂载点；state.db 里的绝对路径 ---"
if [ ! -f "$COMPOSE_DIR/.env" ]; then
    bad "读不到 $COMPOSE_DIR/.env"
else
    # ★ 只读三个路径键，只打**路径**（打值会带出密钥；本项目有脱敏硬规矩）
    BAD=$(grep -hE '^(DATA_DIRS|LINK_DIR|FARM_SOURCES)=' "$COMPOSE_DIR/.env" 2>/dev/null \
          | sed 's/^[A-Z_]*=//' | tr ',' '\n' | grep -v '^$' \
          | grep -v "^$VOL1/" || true)
    if [ -n "$BAD" ]; then
        bad "这些路径不在 $VOL1 下（挂载清单要跟着改；§9：硬链接不能跨卷）："
        printf '%s\n' "$BAD" | sed 's/^/        /'
    else
        ok "DATA_DIRS / LINK_DIR / FARM_SOURCES 全在 $VOL1 下"
    fi
fi
if [ ! -f "$DB" ]; then
    bad "读不到 state.db：$DB ⇒ 闸门/对账的状态全在里面，缺它 = 从零开始"
else
    # ★ 用 python 读（镜像/DSM 都有），只打路径；不引第三方
    if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi
    if command -v "$PY" >/dev/null 2>&1; then
        "$PY" - "$DB" <<'PYEOF' || RC=1
import os, sqlite3, sys
db = sys.argv[1]
try:
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)   # ★ 只读打开
except sqlite3.Error as e:
    print("  ✗ 打不开 state.db（只读）：%s" % e); raise SystemExit(1)
con.row_factory = sqlite3.Row
miss = 0
try:
    rows = list(con.execute("SELECT name, root, farm_root FROM pack"))
except sqlite3.Error as e:
    print("  ✗ 读 pack 表失败：%s" % e); raise SystemExit(1)
# ★★ 空集守卫（B.10 第 14 条）：pack 表**一行都没有**时，下面那圈一条都不跑，
#    最后会印「不可达数：0」—— 那**和"全可达"长得一模一样，但它什么也没验**。
#    （本机自测实测撞上：假树里 state.db 空表 ⇒ 印 0 ⇒ 看着像过。）
#    ⇒ 显式说出"一条都没读到"，并**置红**：NAS 上的 pack 表绝不该是空的
#      （空的意味着 state.db 没迁过来 / 表被清 / 读错了库）。
if not rows:
    print("  ✗ pack 表**一条记录都没有** ⇒ 这一节**什么也没验**"
          "（不是『路径全可达』）——确认 state.db 是不是迁对了")
    raise SystemExit(1)
for r in rows:
    for k in ("root", "farm_root"):
        v = r[k]
        if not v:
            continue
        good = os.path.isdir(v)
        print("  %s pack=%-18s %-9s %s" % ("✓" if good else "✗", r["name"], k, v))
        if not good:
            miss += 1
print("  ⇒ 读到 pack %d 行，路径不可达数：%d" % (len(rows), miss))
raise SystemExit(0 if miss == 0 else 1)
PYEOF
    else
        na "没有 python ⇒ [4] 的 state.db 那半 **未验**（不是通过）"
        UNK=$((UNK+1))
    fi
fi
echo
fi

# =====================================================================
if want 5; then
echo "--- [5] ★★ 两条路**没有同时在跑**（.drive-loop.state 现状）---"
if [ ! -f "$STATE" ]; then
    na "没有 $STATE ⇒ 从没跑过（或已被删）—— 这种情况**两条路都没在跑**，可以起容器"
else
    na "文件：$STATE"
    na "内容（原样，只含状态不含凭据）："
    sed 's/^/        /' "$STATE" 2>/dev/null || na "        （读不出来）"
    # ★ 用 python 判读：与 drive-loop.py 的 batch_alive() 同口径
    #   （mode=resident ⇒ 容器版；无 mode ⇒ 宿主 --once 版；心跳 > 600s ⇒ 陈旧）
    if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi
    if command -v "$PY" >/dev/null 2>&1; then
        VERDICT=$("$PY" - "$STATE" <<'PYEOF' 2>/dev/null || echo "?|读不出来"
import json, sys, time
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        st = json.load(fh)
except Exception:
    print("?|状态文件**读不出来**（半个 JSON / 空文件？）⇒ 当作『不能确认』，别起容器")
    raise SystemExit(0)

pid  = st.get("running_pid")
mode = st.get("mode")
hb   = st.get("heartbeat_ts")
STALE = 600                     # 与 drive-loop.py 的 HEARTBEAT_STALE_SEC 同口径

if pid is None:
    print("idle|running_pid 为 null ⇒ **这一刻没有批在跑**（★ 是快照，不是窗口："
          "循环每 15 分钟醒一次，窗口长度看 last_sleep_sec）")
    if mode == "resident":
        print("     ★ 但 mode=resident ⇒ 上一次是**容器常驻**（这文件是它留下的）")
    raise SystemExit(0)

age = (time.time() - float(hb)) if hb else None
fresh = (age is not None and age <= STALE)
if mode == "resident":
    print("resident|mode=resident ⇒ ★★ **容器常驻那条路在跑**"
          "（心跳 %s）" % ("新鲜 %.0fs" % age if fresh else "陈旧/缺失"))
else:
    print("once|**没有 mode 键** ⇒ 是宿主 `--once` 那份写的（DSM 任务 `run.sh`）"
          "（心跳 %s，pid=%s）" % ("新鲜 %.0fs" % age if fresh else "陈旧/缺失", pid))
PYEOF
)
        V="${VERDICT%%|*}"; WHY="${VERDICT#*|}"
        case "$V" in
            idle)
                ok "判定：**空闲** —— 可以起容器"
                printf '        %s\n' "$WHY" ;;
            resident)
                na "判定：上一次是**容器常驻**（残留，容器已停）⇒ 起新容器会**接管**"
                printf '        %s\n' "$WHY" ;;
            once)
                # ★ 这里**不许在外层双引号里写反引号** —— `` `--once` `` 会被 shell 当成
                #   **命令替换**去执行（本机自测实测：文案里的 `--once` 直接消失，
                #   变成「宿主  那条路还在」）。⇒ 用单引号 printf，反引号才是字面量。
                #   ★ 与 `README` 那条「别让判据需要转义」同族：**别让文案需要转义**。
                bad '★★ 判定：**宿主 `--once` 那条路还在**（DSM 任务活着）⇒ **先停它再起容器**'
                printf '        %s\n' "$WHY"
                na '  ★★ 两条路**没有任何跨进程互斥**：同时发 webhook ⇒ 一次 429 可能废掉几百条（坑 4）'
                na '  ⇒ DSM → 控制面板 → 任务计划 → 停掉「驱动跑批」（`sh <路径>/drive-loop/run.sh`）' ;;
            *)
                bad '★ 判定：**不能确认**（状态文件读不出来）—— 「读不出来」不是「没问题」'
                printf '        %s\n' "$WHY"
                na '  ⇒ 先只读看一眼它是什么；确认真没人跑（`pgrep -f drive-loop.py`）再动' ;;
        esac
    else
        na "没有 python ⇒ [5] 的判读 **未验**（不是通过）"
        UNK=$((UNK+1))
    fi
fi
echo
fi

# =====================================================================
if want 6; then
echo "--- [6] DSM 任务与 attempts.log（人工核对项）---"
AT="$COMPOSE_DIR/drive-loop/attempts.log"
if [ -f "$AT" ]; then
    NALL=$(grep -c '' "$AT" 2>/dev/null | tr -d ' ')
    NRES=$(grep -c '\[resident\]' "$AT" 2>/dev/null | tr -d ' ')
    na "attempts.log：共 ${NALL:-?} 行，其中 **[resident] ${NRES:-?} 行**"
    if [ "${NRES:-0}" = "0" ]; then
        na "  ★ [resident] = 0 ⇒ **容器版从未跑过**（与大前提一致）"
    else
        na "  ★ [resident] > 0 ⇒ 容器版**跑过**；注意看最后一条的 exit 码"
    fi
    na "  最后 3 行："
    tail -3 "$AT" 2>/dev/null | sed 's/^/        /'
else
    bad '读不到 $AT（`drive-loop/` 挂载或路径不对）'
fi
echo
echo "  ★★ 人工核对（本脚本查不到 DSM 面板里的任务，只能读文件）——"
echo "     DSM → 控制面板 → 任务计划：**驱动跑批**这一条（用户 root）。"
echo "     ★ 2026-10-05：通知写侧已砍 ⇒ 原先的「排空 spool / 每日摘要」两条任务**应已删掉**；"
echo "       若还留着，它们只会空转（spool 不再产生），可以删。"
echo "     **启用容器前必须先停掉「驱动跑批」那一条**，顺序不能倒："
echo "       ① DSM 停「驱动跑批」  →  ② docker compose --profile drive-loop up -d drive-loop autoheal"
echo '     ★ 启用命令是**显式**的：`docker compose up -d` **不会**起它们（带 profiles，这是防呆锁）。'
echo "     ★ 反向核对：驱动跑批没停就会撞 429。"
echo
fi

# =====================================================================
echo "============================================================"
if [ "$RC" -eq 0 ] && [ "$UNK" -eq 0 ]; then
    echo "★ 全过 —— 但**本脚本只验了只读前提**，它**没有**证明容器里跑得起来。"
    echo "  下一步（人工，批次间隙）：① 停 DSM「驱动跑批」 ② up -d ③ 看 docker ps 的 STATUS"
    echo "  与 drive-loop/scripts/.drive-loop.state 是否出现 mode=resident + 新鲜心跳。"
    exit 0
elif [ "$RC" -ne 0 ]; then
    echo "★★ 有不通过项（上面 ✗）⇒ **别往下走**，先解决它们。" >&2
    echo "   （另有 ${UNK} 段『查不出』—— 那是未验，不是通过。）" >&2
    exit 1
else
    echo "★★ 没有 ✗，但有 ${UNK} 段**查不出** ⇒ 这是『未验』，不是『没问题』（B.10 第 14 条）。" >&2
    echo "   补齐前置（docker / python）后重跑，别把这份输出当成『验证过了』。" >&2
    exit 2
fi
