#!/bin/sh
# =====================================================================
# `--pool` 空池探针 —— ★★ 在 **NAS 宿主机** 上跑（不在容器里），单文件自包含
#
# 为什么是"宿主机 + docker exec 内嵌 python"这个形状
# ------------------------------------------------
#   · `scripts/diag/probe-pool.py` 得先进容器 ⇒ 而 NAS 上**还没有 scripts/ 目录**
#     （这些本机诊断件一直没往 NAS 推）⇒ `docker cp` 起手就 `lstat … no such file`
#     ⇒ 而 `docker cp` 读的是**发送端**文件系统，容器挂载盖不到它，那条路不通。
#   · ⇒ 这份把"要跑什么"整个装进**一个 heredoc**，`docker exec -i` 喂给容器里的 python。
#     ★ 宿主上**只需要一个文件**：`cat > p.sh` 一次粘完。
#
# ★★ 为什么这次 heredoc 不会像上次那样炸（上一版就是这么炸的）
#   · 用 `<<'PY'` —— **带引号** ⇒ 宿主 shell **一个字符都不展开**（`$`、反引号、
#     单引号在 python 里都原样保留，不用转义一次）。
#   · 而 python 代码**顶格写** —— 上次的 `IndentationError: unexpected indent`
#     是 `python -c "…"` 里每一行都被缩进了两格，python 把第一行当成缩进块。
#     ⇒ heredoc 的内容**不做任何缩进**（本文件里它就是顶格的）。
#   · `set -eu` **不加** —— 探针要能在某一步失败后继续报后面的段（见下）。
#
# 安全性（逐条，不是"应该没问题"）
# --------------------------------
#   · 只读：`mode=ro` + `PRAGMA query_only=1`，**不跑 schema 迁移**（那会 ALTER TABLE）
#   · 零网络请求；不读 `TORZNAB_URLS`；不 SELECT `url`/`apikey` 列；输出里没有 URL
#   · 不写任何文件（输出全走 stdout）
#
# 用法
# ----
#   sh p.sh                                  # 默认：frds 一个包、--limit 500、四个站
#   PROBE_PACKS=a,b PROBE_LIMIT=50 sh p.sh   # 覆盖
# =====================================================================

CT="${PROBE_CONTAINER:-reseed-drive-loop}"
DB="${PROBE_DB:-/volume2/docker_ssd/reseed/drive-loop/hlink/state.db}"
ORCH="${PROBE_ORCH:-/volume2/docker_ssd/reseed/drive-loop/orchestrator}"

echo "== 宿主侧前置核对（三步，任一步失败就没有读数，先看这里）=="
if ! command -v docker >/dev/null 2>&1; then
  echo "[!!] 宿主上没有 docker ⇒ 这份脚本要在 **NAS 宿主** 上跑，不要在容器/别的机器上跑" >&2
  exit 2
fi
if ! docker inspect -f '{{.State.Running}}' "$CT" >/dev/null 2>&1; then
  echo "[!!] 找不到容器 $CT（或 docker 没权限）—— 先确认它叫什么： docker ps" >&2
  exit 2
fi
echo "   ✓ 容器存在：$CT（running=$(docker inspect -f '{{.State.Running}}' "$CT")）"
if ! docker exec "$CT" test -d "$ORCH"; then
  echo "[!!] 容器里没有 $ORCH" >&2
  echo "     ⇒ 那份挂载可能还没生效（本仓有「compose 必须 --force-recreate 才读新配置」的先例）。" >&2
  echo "     先看它到底挂了什么： docker inspect -f '{{range .Mounts}}{{.Source}}{{\"\\n\"}}{{end}}' $CT" >&2
  exit 2
fi
echo "   ✓ 容器内 orchestrator 目录在：$ORCH"
echo "   ✓ 容器内 state.db：$(docker exec "$CT" sh -c "ls -l '$DB' 2>&1" | head -1)"
echo

# ★★ 时区自证：★ 这三行就是 §26.46 那次错判的**自动判据**
#    （当时我把"本地时间"当 UTC 去比，算出了根本不存在的"差 8 小时"）
echo "== 时区自证（★ 上次那个"差 8 小时"就是没量这一步）=="
echo "   宿主: date=$(date '+%F %T %z') utc=$(date -u '+%F %T')"
echo "   容器: $(docker exec "$CT" date '+%F %T %z')  utc=$(docker exec "$CT" date -u '+%F %T')"
echo

# ★ `docker exec -i` 把 heredoc 从**标准输入**喂给 python（`-` = 从 stdin 读程序）
#   ⇒ 不用 `-c`，不用转义，也不用 `docker cp`。
docker exec -i \
  -e "PROBE_DB=$DB" \
  -e "PROBE_PACKS=${PROBE_PACKS:-frds-top250-2024}" \
  -e "PROBE_LIMIT=${PROBE_LIMIT:-500}" \
  -e "PROBE_INDEXERS=${PROBE_INDEXERS:-HDtime,HDFans,NanyangPT,BTSCHOOL}" \
  "$CT" python - <<'PY'
# -*- coding: utf-8 -*-
"""容器内只读探针：`--pool` 报「没有待搜索项」是怎么算出来的。四层逐层数。"""
import datetime
import os
import pathlib
import sqlite3
import sys

# ★★ 强制 UTF-8 —— 必须在**任何 print 之前**。片名与校验符是多字节，
#   而容器 locale 常是 POSIX(C) ⇒ 打印到一半 UnicodeEncodeError **把探针打挂**，
#   **挂在哪一行取决于数据** ⇒ 最坏是"读到了却没打出来"（ERR-AI-10 那个形状）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DB = os.environ.get("PROBE_DB", "")
PACKS = [s.strip() for s in os.environ.get("PROBE_PACKS", "").split(",") if s.strip()]
LIMIT = int(os.environ.get("PROBE_LIMIT", "500"))
IX = [s.strip() for s in os.environ.get("PROBE_INDEXERS", "").split(",") if s.strip()]

# ★ 路径候选：容器内一套 + 本机一套（★ 探针必须能在写它的机器上验，
#   否则它只是个"裸奔上线的读数来源"）。PROBE_ORCH 由外面传进来。
_C = []
if os.environ.get("PROBE_ORCH"):
    _C.append(pathlib.Path(os.environ["PROBE_ORCH"]))
_C.append(pathlib.Path("/volume2/docker_ssd/reseed"
                       "/drive-loop/orchestrator"))
_C.append(pathlib.Path(__file__).resolve().parent) if "__file__" in dir() else None
for _c in _C:
    if _c.is_dir() and str(_c) not in sys.path:
        sys.path.insert(0, str(_c))
print("sys.path[0] =", sys.path[0] if sys.path else "-")

try:
    import state as S
except Exception as e:
    print("!! import state 失败：%r" % (e,))
    print("   sys.path =", sys.path)
    raise SystemExit(3)

print("=" * 68)
print("now(local)   =", datetime.datetime.now())
print("now(utc)     =", datetime.datetime.now(datetime.timezone.utc))
print("local offset =", datetime.datetime.now().astimezone().utcoffset(),
      " ★ 非 0 且与宿主一致 = 时区正确")
print("TZ env       =", os.environ.get("TZ"))
print("PACKS        =", PACKS)
print("LIMIT        =", LIMIT)
print("INDEXERS     =", IX)
print("db           =", DB)
_p = pathlib.Path(DB)
print("db exists    =", _p.exists(), (_p.stat().st_size if _p.exists() else "-"))
print("=" * 68)
if not _p.exists():
    print("!! state.db 不存在 ⇒ 后面全无意义。先确认上面的 db 路径。")
    raise SystemExit(3)

# ★ 只读打开：不跑 schema 迁移（那是写操作）
con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
con.row_factory = sqlite3.Row
con.execute("PRAGMA query_only=1")

print("\n-- ① pack 表登记了谁（对照 PACKS ⇒ 谁『登记了却没被驱动』）--")
try:
    for r in con.execute("SELECT name FROM pack ORDER BY name").fetchall():
        print("   pack: %-24s%s" % (r["name"], "  ★在 PACKS 里" if r["name"] in PACKS
                                   else "  （登记了但没被驱动）"))
except Exception as e:
    print("   !! pack 表读不出来：%r" % (e,))

print("\n-- ② 每包 movie 行数 / 按 stage 分布 --")
ALL = {}
for pk in PACKS:
    try:
        rows = con.execute("SELECT * FROM movie WHERE pack=?", (pk,)).fetchall()
    except Exception as e:
        print("   movie(%s) 失败：%r" % (pk, e))
        continue
    c = {}
    for r in rows:
        c[r["stage"]] = c.get(r["stage"], 0) + 1
        ALL[r["stage"]] = ALL.get(r["stage"], 0) + 1
    print("   movie(%-20s) = %d   %s" % (pk, len(rows),
          " ".join("%s×%d" % (k, v) for k, v in sorted(c.items()))))
print("   合计 stage：", ALL)

print("\n-- ③ indexer_seen 里实际出现过的**站名全集**（★ 对照 --indexers）--")
try:
    seen = {}
    for r in con.execute("SELECT indexer_seen FROM movie WHERE indexer_seen IS NOT NULL"
                         " AND indexer_seen NOT IN ('', '{}')").fetchall():
        for k in S._d(r["indexer_seen"]):
            seen[k] = seen.get(k, 0) + 1
    for k, v in sorted(seen.items(), key=lambda t: -t[1]):
        print("   %-28s %5d 部%s" % (k, v,
              "  ✓在 --indexers 里" if k in IX else "  ★★ 不在 --indexers 里"))
    miss = [k for k in IX if k not in seen]
    if miss:
        print("   ★★ --indexers 里有、但库里从没搜过的：", miss)
    print("   ⇒ ★ 名字对不上（如 NanyangPT (南洋) vs NanyangPT）⇒ due_indexers 判它"
          "『从没搜过』⇒ 每部片子每轮都多一个到期站 ⇒ 挤占 --limit")
except Exception as e:
    print("   读不出来：%r" % (e,))

print("\n-- ④ ★ 核心：逐部算 due_indexers（池子就是这一层算出来的）--")
now_dt = datetime.datetime.now()
tot_ok = tot_due = 0
for pk in PACKS:
    rows = con.execute("SELECT * FROM movie WHERE pack=?", (pk,)).fetchall()
    n_done = n_nodue = 0
    per_ix = {}
    per_stage = {}
    for r in rows:
        if r["stage"] in S.DONE_STAGES:
            n_done += 1
            continue
        due = S.due_indexers(S._d(r["indexer_seen"]), IX,
                            cadence_days=S.DEFAULT_CADENCE_DAYS, now=now_dt)
        if due:
            tot_due += 1
            per_stage[r["stage"]] = per_stage.get(r["stage"], 0) + 1
            for ix in due:
                per_ix[ix] = per_ix.get(ix, 0) + 1
        else:
            n_nodue += 1
    tot_ok += len(rows) - n_done
    print("   %-20s 共%5d｜DONE%5d｜★该搜%5d｜该搜但无到期站%5d   %s" % (
        pk, len(rows), n_done, len(rows) - n_done - n_nodue, n_nodue,
        " ".join("%s×%d" % (k, v) for k, v in sorted(per_stage.items()))))
    if per_ix:
        print("        ↳ 到期分布：%s" % dict(sorted(per_ix.items(), key=lambda t: -t[1])))

print("   ⇒ 全池该搜合计 = %d（--limit %d ⇒ 本批取 %d）" % (
    tot_ok, LIMIT, min(tot_ok, LIMIT)))
if tot_ok > LIMIT:
    print("   ★★ tot_ok(%d) > LIMIT(%d) ⇒ **尾部 %d 部永远取不到**（『每次从头取前 N』）"
          % (tot_ok, LIMIT, tot_ok - LIMIT))
else:
    print("   ✓ tot_ok ≤ LIMIT ⇒ 池子**一轮能清空**（不会饿死尾部）")

# 真调一次 todo_pooled（绕过 __init__，它会跑迁移 = 写）
try:
    st = S.StateStore.__new__(S.StateStore)
    st.con = con
    pooled = st.todo_pooled(PACKS, indexers_now=IX,
                            cadence_days=S.DEFAULT_CADENCE_DAYS,
                            limit=LIMIT, now=now_dt)
    print("\n   todo_pooled(...) 返回 = %d" % len(pooled))
    for pk, r, due in pooled[:5]:
        print("     · %-20s %-28s stage=%-9s due=%s" % (
            pk, r["dir_name"][:28], r["stage"], due))
except Exception as e:
    import traceback
    print("\n   ★★ todo_pooled 抛了：")
    traceback.print_exc()

print("\n-- ⑤ dc/mbf 的行还在不在（『登记了却没被驱动』那两个）--")
for pk in ("dc-collection", "mbf"):
    try:
        n = con.execute("SELECT COUNT(*) c FROM movie WHERE pack=?",
                        (pk,)).fetchone()["c"]
        print("   %-16s movie 行 = %d  ★ PACKS 里%s" % (
            pk, n, "有" if pk in PACKS else "没有"))
    except Exception as e:
        print("   %s: %r" % (pk, e))

con.close()
print("\n完成（全程只读、零请求）。")
PY
RC=$?
echo
echo "== python 退出码 = $RC（0 = 拿全了；≠0 = 上面有段没跑完，先看哪一段）=="
exit $RC
