#!/bin/sh
# =====================================================================
# `#58` D2 —— 容器版 drive-loop 的**挂载自证**（只读，容器起来之前跑）
#
# 为什么必须有这一条（2026-09-17 本机实测的形态）
# ----------------------------------------------
# `drive-loop.py` 里有**写死的绝对路径**（`CROSSSEED_DIRS[0]` =
# compose 根，现为 /volume2/docker_ssd/reseed，可用 `COMPOSE_DIR` 覆盖），而 `state.db` 里
# 存的也全是 `/volume1/...` 绝对路径。
# ⇒ 挂载**少挂一条**、或**挂到别的路径**时，它**不报错**：只打一行
#   ⚠ 找不到 cross-seed 的 info 日志（试过 …）—— 回灌将推不出「是否在做种」，
#     原本 SEEDING 的片子会被 **误降级成 MATCHED**。
#   ★★ 后果是**静默改错状态**，不是崩溃 ⇒ 必须有一条**会红**的检查挡在前面。
#
# ★★ 2026-10-05 模型变更（§8.3：代码全在镜像里）后，挂载清单变了：
#   · compose 根 **只读挂进来**（`<新根>:<新根>:ro`）—— 给 `CROSSSEED_DIRS`
#     读 cross-seed.db / info.current.log / build-farm.sh / .env 用；
#   · 运行态目录单独挂（`./state:/state`）—— 6 个 `.state` + attempts.log + state.db 都在这儿；
#   · `drive-loop/run.sh`（宿主 `--once` 那份）**不再进容器挂载**，本脚本不再查它。
#   ⇒ 判据随之收窄：查 compose 根能读、查**挂载进来**的 state.db 能读。

# 本脚本就查三件事（全部只读）：
#   ① 硬编码的 compose 目录在容器内**能 stat 到**，且里面那三样关键文件在；
#   ② **挂载进来**的 `state.db` 里 `pack` 表的绝对路径（`root`/`farm_root`）在容器内**能 stat 到**；
#   ③ 媒体卷的挂载点与 `.env` 里的路径前缀**一致**（§9：硬链接不能跨卷）。
#
# 用法（在容器里跑；把 COMPOSE_DIR / VOL1 / DB 按挂载点传）
#   sh mount-selfcheck.sh
# 退出码：0 = 全过；1 = 有 ✗（**别往下跑**）；2 = 查不了（缺工具/缺文件）
# =====================================================================
set -eu

# ★★ 让 python 的 stdout/stderr 强制 UTF-8 —— **否则本脚本会在 GBK 控制台上自己打挂自己**
# --------------------------------------------------------------------------------------
#   ② 段那个 heredoc 里 `print("  ✓/✗ …")` 打的是 U+2713/U+2717，而 Windows 的 python
#   默认 stdout 编码是 **GBK**，编不出这两个字符 ⇒ python 抛 UnicodeEncodeError。
#   ★ 症状**不是崩溃**，是**② 段半途而废**：前面的 `✓` 行打完了，一到**第一条 `✗`**
#     （也就是真的有问题、最该被看到的那一行）就抛 ⇒ ② 的结论与那行 `⇒ 路径不可达数`
#     **全都打不出来**，而退出码由 heredoc 那支 `|| RC=1` 兜住 ⇒ 整脚本仍以 1 退出，
#     却**不给任何解释**。⇒ 恰好是"判据在最该报的时候哑掉"。
#   ★ 处置与 `chk58.sh:107` / `drive-loop-nas.sh` / `run-resident.sh` **同一句**（照抄，不新设计）。
#   ★ 为什么 shell 那侧的 `bad()`/`ok()`（printf）**不需要**这条：printf 走的是 shell 自己的
#     字节输出，不过 python 的编码器 —— 所以只有 **python heredoc** 会中招，别一并改。
export PYTHONIOENCODING=utf-8

COMPOSE_DIR="${COMPOSE_DIR:-/volume2/docker_ssd/reseed}"
VOL1="${VOL1:-/volume1/video}"
# ★ state.db 现挂在**运行态卷**下（`./data/drive-loop/state:/state`），不再在 compose 根里。
DB="${DB:-/state/hlink/state.db}"

RC=0
say() { printf '  %s\n' "$*"; }
bad() { printf '  ✗ %s\n' "$*"; RC=1; }
ok()  { printf '  ✓ %s\n' "$*"; }

echo "[挂载自证] COMPOSE_DIR=$COMPOSE_DIR  VOL1=$VOL1"
echo

# ---------- ① 硬编码目录 + 三样关键文件 ----------
echo "① 硬编码目录（代码里写死的绝对路径）"
if [ -d "$COMPOSE_DIR" ]; then
  ok "目录在：$COMPOSE_DIR"
else
  bad "目录不在：$COMPOSE_DIR  ⇒ ★ 挂载清单漏了/挂错路径了（这是**静默**的，不会报错）"
fi
for rel in "cross-seed/cross-seed.db" "cross-seed/logs/info.current.log" \
           "build-farm.sh"; do
  p="$COMPOSE_DIR/$rel"
  if [ -e "$p" ]; then ok "$rel"; else bad "$rel 不在（回灌/巡检会静默退化）"; fi
done
echo

# ---------- ② state.db 里的绝对路径 ----------
echo "② state.db 里的**绝对路径**（这才是 1:1 挂载的实质）"
if [ ! -f "$DB" ]; then
  bad "读不到 state.db：$DB  ⇒ 闸门/对账的状态全在里面，缺它=从零开始"
else
  # ★ 用 python 读，不引第三方；只打路径，不打别的内容
  python - "$DB" <<'PY' || RC=1
import os, sqlite3, sys
db = sys.argv[1]
con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
con.row_factory = sqlite3.Row
miss = 0
try:
    rows = list(con.execute("SELECT name, root, farm_root FROM pack"))
except sqlite3.Error as e:
    print("  ✗ 读 pack 表失败：%s" % e); raise SystemExit(1)
for r in rows:
    for k in ("root", "farm_root"):
        v = r[k]
        if not v:
            continue
        e_ = os.path.isdir(v)
        print("  %s pack=%-18s %-9s %s" % ("✓" if e_ else "✗", r["name"], k, v))
        if not e_:
            miss += 1
try:
    m = con.execute("SELECT path FROM movie WHERE path IS NOT NULL LIMIT 1").fetchone()
    if m:
        e_ = os.path.isdir(m["path"])
        print("  %s movie.path        %s" % ("✓" if e_ else "✗", m["path"]))
        if not e_:
            miss += 1
except sqlite3.Error:
    pass
print("  ⇒ 路径不可达数：%d" % miss)
raise SystemExit(0 if miss == 0 else 1)
PY
fi
echo

# ---------- ③ 卷前缀一致（§9 硬链接不能跨卷） ----------
echo "③ .env 里的路径前缀 vs 挂载点"
if [ ! -f "$COMPOSE_DIR/.env" ]; then
  bad "读不到 $COMPOSE_DIR/.env"
else
  BAD=$(grep -hE '^(DATA_DIRS|LINK_DIR|FARM_SOURCES)=' "$COMPOSE_DIR/.env" 2>/dev/null \
        | sed 's/^[A-Z_]*=//' | tr ',' '\n' | grep -v '^$' \
        | grep -v "^$VOL1/" || true)
  if [ -n "$BAD" ]; then
    bad "这些路径不在 $VOL1 下（挂载清单要跟着改；§9：硬链接不能跨卷）："
    printf '%s\n' "$BAD" | sed 's/^/       /'
  else
    ok "DATA_DIRS / LINK_DIR / FARM_SOURCES 全在 $VOL1 下"
  fi
fi
echo

if [ "$RC" -eq 0 ]; then
  echo "[挂载自证] ★ 全过 —— 可以往下跑"
else
  echo "[挂载自证] ★★ 有不通过项 ⇒ **别往下跑**（跑下去会静默改错状态）" >&2
fi
exit "$RC"
