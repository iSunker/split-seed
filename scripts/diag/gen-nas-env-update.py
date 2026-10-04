#!/usr/bin/env python3
"""从本地 .env 的 DATA_DIRS 生成「NAS 一键更新 .env」的 shell 脚本。

**为什么需要它**：受管集合（`DEPLOY_MANIFEST`）**故意不含 `.env`**（怕覆盖生产上的真实
密钥），所以生产 `.env` 的 `DATA_DIRS` 一直是老值。改 `.env` 只能手动上 NAS 改，
而这条 `DATA_DIRS=` 长达 ~3.5 KB、含 CJK 与全角括号、还有带空格的路径 ——
手敲必错。所以：本地 `.env` 是唯一事实源，本脚本把它「编译」成一段可粘贴的
POSIX sh，粘到 NAS 上跑一次即可（自带备份 + 校验 + 重启）。

用法::

    python scripts/gen-nas-env-update.py                    # 写到 scripts/nas-update-env.sh
    python scripts/gen-nas-env-update.py --stdout           # 直接打到终端（方便复制粘贴）
    python scripts/gen-nas-env-update.py -o /tmp/x.sh       # 指定输出
    python scripts/gen-nas-env-update.py --compose-dir /volume2/xxx   # 换 NAS 上的 compose 目录

生成的脚本支持：

    sh nas-update-env.sh --dry-run      # 只看会改什么（不写、不重启）
    sh nas-update-env.sh                # 改 + 重启
    sh nas-update-env.sh --no-restart   # 只改不重启
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

# 只搬这些键：DATA_DIRS 是本次的主角，LINK_DIR 一起带上以防生产是空的/写错。
KEYS = ("DATA_DIRS", "LINK_DIR")

DEFAULT_COMPOSE_DIR = "/volume2/docker_ssd/reseed"

TEMPLATE = r'''#!/bin/sh
# =====================================================================
# NAS 一键更新 .env —— 由本地 scripts/gen-nas-env-update.py 生成，勿手改！
#   生成时间: {generated_at}
#   事实源  : 本地 reseed-toolkit/.env
#   本次更新: DATA_DIRS {n_old_hint}
#
# 用法（NAS 上，SSH 或 Container Manager「计划任务」均可）:
#   sh nas-update-env.sh --dry-run      只看会改什么（不写盘、不重启）
#   sh nas-update-env.sh                改 + 重启 cross-seed
#   sh nas-update-env.sh --no-restart   只改不重启
#
# 脚本做的事:
#   1) 生成新的 .env（先不落地）→ 逐条 [ -d ] 校验 → 打印将要发生的变化
#   2) --dry-run 到此为止，一个文件都不留
#   3) 否则：备份 .env → .env.bak.<时间戳>，再落地
#   4) 重启 cross-seed，并回读容器内的 DATA_DIRS 条数做闭环验证
#
# 幂等、可重复跑。中间文件叫 .env.new.src / .env.new / .env.new.list（都放在
# COMPOSE_DIR 里），跑前先清、trap 兜底再清；成功落地后一个都不会留下。
# =====================================================================
set -eu

COMPOSE_DIR="${{COMPOSE_DIR:-{compose_dir}}}"
DRY=0
RESTART=1
for a in "$@"; do
  case "$a" in
    --dry-run)     DRY=1 ;;
    --no-restart)  RESTART=0 ;;
    -h|--help)     sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "未知参数: $a  (可用: --dry-run | --no-restart | -h)" >&2; exit 2 ;;
  esac
done

cd "$COMPOSE_DIR" 2>/dev/null || {{ echo "[!!] 目录不存在: $COMPOSE_DIR" >&2; exit 1; }}
[ -f .env ] || {{ echo "[!!] $COMPOSE_DIR/.env 不存在" >&2; exit 1; }}

# ---------- 0) 清掉上次中断可能残留的中间文件（这几个名字都是本脚本专用的）----------
rm -f .env.new.src .env.new .env.new.list .env.other.old .env.other.new

# ---------- 1) 新值：heredoc 用单引号定界 → 里面一个字符都不会被展开/转义 ----------
# 中间文件都放 COMPOSE_DIR 里（相对路径），不用 mktemp：
#   ① 系统临时目录在某些环境下不可写/被清理；
#   ② 相对路径能保证 trap 的 rm 一定成功，不会在 /tmp 里留垃圾。
NEWFILE=".env.new.src"
# 无论正常结束 / 报错 / Ctrl-C，都把自己产生的中间文件收干净
trap 'rm -f "$NEWFILE" .env.new .env.new.list .env.other.old .env.other.new' EXIT INT TERM
cat > "$NEWFILE" <<'DATA_DIRS_EOF'
{new_lines}
DATA_DIRS_EOF

# ---------- 2) 生成新 .env 到 .env.new（先不落地，dry-run 也能看到结果）----------
# 用 awk 而不是 sed：路径里全是 '/'，sed 的 s/// 得换分隔符，且替换串里的 '&' 会被
# 特殊解释。awk 逐行 print 完全绕开这些坑。
# 注意：新文件有 2 行（DATA_DIRS + LINK_DIR），必须**分别**捕获，
# 不能写成一个 new=$0 —— 否则 new 会停在最后一行，把 DATA_DIRS 覆盖成 LINK_DIR。
awk 'NR==FNR {{
       if      ($0 ~ /^DATA_DIRS=/) dd = $0;
       else if ($0 ~ /^LINK_DIR=/)  ld = $0;
       next
     }}
     /^[[:space:]]*DATA_DIRS[[:space:]]*=/ {{ if (dd != "") {{ print dd; next }} }}
     /^[[:space:]]*LINK_DIR[[:space:]]*=/  {{ if (ld != "") {{ print ld; next }} }}
     {{ print }}' "$NEWFILE" .env > .env.new

if ! grep -q '^DATA_DIRS=' .env.new; then
  echo "[!!] 替换失败：.env 里没有 DATA_DIRS= 行" >&2
  exit 1
fi

# ---- 安全闸 ----
# 闸1：DATA_DIRS 必须恰好 1 行（0 行 = 没替换上，2 行 = 替换逻辑炸了）
[ "$(grep -c '^DATA_DIRS=' .env.new)" = "1" ] || {{
  echo "[!!] .env.new 里 DATA_DIRS 不是恰好 1 行，拒绝写入" >&2; exit 1; }}

# 闸2：除 DATA_DIRS / LINK_DIR 之外的行必须**逐字节不变**。
# 为什么不能只比行数：行数一样但内容被换掉完全可能。而生产的 .env 里是
# 真实的 TORZNAB 密钥，本仓库里的 .env 是脱敏占位符（apikey=xxxx…）——
# 一旦串了，cross-seed 会带着假密钥重启，全线 401。
OTHER_RE='^[[:space:]]*(DATA_DIRS|LINK_DIR)[[:space:]]*='
grep -vE "$OTHER_RE" .env     > .env.other.old || true
grep -vE "$OTHER_RE" .env.new > .env.other.new || true
if ! cmp -s .env.other.old .env.other.new; then
  echo "[!!] 除 DATA_DIRS/LINK_DIR 外的行发生了变化，拒绝写入。差异（前 20 行）：" >&2
  diff .env.other.old .env.other.new 2>/dev/null | head -20 >&2 || true
  exit 1
fi
echo "      [ok] 安全闸：DATA_DIRS 1 行；其它键逐字节未变"

N_OLD=$(awk '/^[[:space:]]*DATA_DIRS[[:space:]]*=/{{n=split($0,a,","); print n; exit}}' .env)
N_NEW=$(awk '/^DATA_DIRS=/{{n=split($0,a,","); print n}}' .env.new)

# 逐条校验（路径可能含空格 → 必须 IFS= read，不能 for 循环分词）
# 只取 DATA_DIRS 那一行拆开，别把 LINK_DIR 也算进来。
grep '^DATA_DIRS=' "$NEWFILE" | sed 's/^DATA_DIRS=//' | tr ',' '\n' > .env.new.list
N_MISS=0
while IFS= read -r p; do
  [ -n "$p" ] || continue
  if [ ! -d "$p" ]; then
    echo "      [warn] 路径不存在: $p"
    N_MISS=$((N_MISS + 1))
  fi
done < .env.new.list

echo "──────────────────────────────────────────────"
echo "目标 : $COMPOSE_DIR/.env"
echo "变化 : DATA_DIRS $N_OLD 条 → $N_NEW 条${{N_MISS:+   ⚠ $N_MISS 条路径不存在}}"
echo "  首2: $(sed -n '1,2p' .env.new.list | tr '\n' ' ')"
echo "  末1: $(tail -1 .env.new.list)"
echo "──────────────────────────────────────────────"

if [ "$DRY" = 1 ]; then
  echo "[dry-run] 未写入任何文件、未重启。（中间文件由 trap 自动清理）"
  exit 0
fi

# ---------- 3) 备份 + 落地 ----------
TS="$(date +%Y%m%d-%H%M%S)"
BAK=".env.bak.$TS"
cp -p .env "$BAK"
echo "[1/4] 备份完成 → $BAK"

mv .env.new .env
echo "[2/4] 已写入 .env（回滚: cd $COMPOSE_DIR && cp -p $BAK .env）"

# ---------- 4) 重启 + 闭环验证 ----------
if [ "$RESTART" = 0 ]; then
  echo "[3/4] --no-restart：跳过重启。记得手动执行："
  echo "        cd $COMPOSE_DIR && sudo docker compose up -d --force-recreate cross-seed"
  exit 0
fi

if docker ps >/dev/null 2>&1; then SUDO=""; else SUDO="sudo"; fi
if $SUDO docker compose version >/dev/null 2>&1; then DC="$SUDO docker compose"
elif command -v docker-compose >/dev/null 2>&1; then DC="$SUDO docker-compose"
else echo "[!!] 找不到 docker compose / docker-compose" >&2; exit 1; fi

echo "[3/4] 重启 cross-seed: $DC up -d --force-recreate cross-seed"
$DC up -d --force-recreate cross-seed

sleep 5
GOT=$($SUDO docker inspect reseed-cross-seed \
        --format '{{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}' 2>/dev/null \
      | grep '^DATA_DIRS=' | tr ',' '\n' | grep -c . || true)
echo
echo "──────────────────────────────────────────────"
if [ "$GOT" = "$N_NEW" ]; then
  echo "[4/4] [ok] 容器内 DATA_DIRS = $GOT 条，与预期一致。"
else
  echo "[4/4] [!!] 容器内 DATA_DIRS = $GOT 条，预期 $N_NEW 条 —— 请检查！"
fi
echo "──────────────────────────────────────────────"
echo "看日志: $SUDO docker logs --tail=60 reseed-cross-seed"
'''


def parse_env(path: pathlib.Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def split_csv(value: str) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从本地 .env 生成 NAS 一键更新 .env 的脚本")
    ap.add_argument("--env", default=None, help="本地 .env 路径（默认 <repo>/.env）")
    ap.add_argument("-o", "--out", default=None,
                    help="输出路径（默认 <repo>/scripts/nas-update-env.sh）")
    ap.add_argument("--stdout", action="store_true", help="打到 stdout（不写文件）")
    ap.add_argument("--compose-dir", default=DEFAULT_COMPOSE_DIR,
                    help=f"NAS 上的 compose 目录，默认 {DEFAULT_COMPOSE_DIR}")
    args = ap.parse_args(argv)

    repo = pathlib.Path(__file__).resolve().parent.parent
    env_path = pathlib.Path(args.env) if args.env else repo / ".env"
    if not env_path.is_file():
        print(f"[!!] 找不到 {env_path}", file=sys.stderr)
        return 2

    env = parse_env(env_path)
    missing = [k for k in KEYS if k not in env or not env[k].strip()]
    if missing:
        print(f"[!!] {env_path} 缺少: {', '.join(missing)}", file=sys.stderr)
        return 2

    # DATA_DIRS 一行可能很长，保持原样（不重排、不加换行），确保与本地完全一致。
    new_lines = "".join(f"{k}={env[k]}\n" for k in KEYS)

    dd = split_csv(env["DATA_DIRS"])
    gen_at = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = TEMPLATE.format(
        generated_at=gen_at,
        compose_dir=args.compose_dir,
        new_lines=new_lines.rstrip("\n"),
        n_old_hint=f"→ {len(dd)} 条",
    )

    if args.stdout:
        sys.stdout.write(text)
        print(f"\n# （DATA_DIRS 共 {len(dd)} 条；LINK_DIR={env['LINK_DIR']}）", file=sys.stderr)
        return 0

    out = pathlib.Path(args.out) if args.out else repo / "scripts" / "nas-update-env.sh"
    out.write_text(text, encoding="utf-8", newline="\n")
    print(f"已写出 {out}（DATA_DIRS {len(dd)} 条，{len(text)} 字节）")
    print(f"NAS 上执行：  sh {out.name} --dry-run     # 先看")
    print(f"              sh {out.name}              # 再改+重启")
    return 0


if __name__ == "__main__":
    sys.exit(main())
