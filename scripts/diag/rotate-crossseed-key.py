#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""#109 第 3 步的工具：把 `cross-seed.db` 的 `indexer.apikey` 列换成新 key。

★ 为什么这一步**不能省**（`#109` 卡里原来漏了它）：
  `indexer` 表里 `apikey` 是**独立的一列**，不是 URL 的一部分。
  实测 4 行**同一种取值**、且与 `.env` 的 `PROWLARR_API_KEY` **逐字节相同**。
  ⇒ 只改 `.env`、不改这里 ⇒ **仍然 401**。

安全闸：
  · **默认 dry-run**，`--apply` 才写；
  · 写前**先备份**（`cross-seed.db.bak-<时间戳>`）；
  · ★ **先做完整性检查**（`PRAGMA integrity_check`），坏的库不碰；
  · ★ 写入用**事务**，且**写后立刻回读**核对（4 行都变成新 key）；
  · key **从文件读**，不走命令行；
  · ★ **提醒**：cross-seed 容器**正在跑**时直接改它的 db 有风险
    ⇒ 脚本**默认拒绝在容器未停时 --apply**（`--force-running` 可跳过，不推荐）。

用法：
    python scripts/rotate-crossseed-key.py --db <cross-seed.db> --new-key-file <文件>
    python scripts/rotate-crossseed-key.py --db <...> --new-key-file <...> --apply
"""
from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import sqlite3
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

DEFAULT_DB = "//iSunker-DS423/docker_ssd/reseed/cross-seed/cross-seed.db"

#: 要检查的容器名（compose.yaml 里的 container_name）
CONTAINER = "reseed-cross-seed"


def _sh(cmd: str) -> tuple[int, str]:
    """跑一条命令，返回 (rc, 输出)。★ rc 与输出**分开**返回 ——
    不要把失败折进输出字符串（那正是 `|| echo` 的毛病：**把"工具不存在"伪装成"没问题"**）。"""
    import subprocess
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=20)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:                                # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


def container_state(db_path: str = "") -> tuple[str, list[str]]:
    """判 cross-seed 容器**是不是真的没在跑**。

    返回 `(verdict, detail_lines)`，verdict ∈ {"stopped", "running", "unknown"}。

    ★★ 为什么不用 `docker ps`：2026-09-17 实测它**会说谎** ——
       容器已 `State=exited`、`ExitCode=137`、`Pid=0`，而 `docker ps` 仍显示
       `reseed-cross-seed Up 4 minutes`（守护进程侧没刷新）。
     ★ 判据取 **`docker inspect` 的 `State.Status`**（权威字段），用 **`Pid`** 交叉验证，
       再用 **`-wal`/`-shm` 在不在** 做第三道独立检查。
     ★ 三条**任一**说"在跑"就是 running；**查不出**则 unknown ——
       **unknown 不放行**（宁要人确认，也不在"不知道"时写生产库）。
    """
    import os
    det: list[str] = []
    # ① docker 在不在（★ docker 不可用 ⇒ "不知道"，**不是**"没在跑"）
    rc, out = _sh("docker --version")
    if rc != 0:
        det.append(f"① docker 不可用（rc={rc}）：{out[:80]}")
        return "unknown", det
    det.append(f"① docker 可用：{out.splitlines()[0][:60]}")

    # ② 权威判据：inspect 的 State.Status / Pid / ExitCode
    rc, out = _sh(f"docker inspect {CONTAINER} "
                  "--format '{{.State.Status}}|{{.State.Pid}}|{{.State.ExitCode}}'")
    if rc != 0:
        low = out.lower()
        if "no such" in low or "not found" in low:
            det.append(f"② 容器不存在（rc={rc}）⇒ 视为**没在跑**")
            return "stopped", det
        det.append(f"② inspect 失败（rc={rc}）：{out[:100]}")
        return "unknown", det
    parts = out.strip().strip("'").split("|")
    if len(parts) != 3:
        det.append(f"② inspect 输出形状不对：{out[:100]!r}")
        return "unknown", det
    status, pid, code = (p.strip() for p in parts)
    det.append(f"② docker inspect: State.Status={status}  Pid={pid}  ExitCode={code}")
    if status == "running":
        det.append("   ⇒ ★ **正在运行**")
        return "running", det
    if pid not in ("", "0"):
        det.append(f"   ★★ 自相矛盾：Status={status} 但 Pid={pid}（非 0）⇒ 判 unknown")
        return "unknown", det
    det.append(f"   ⇒ **没在跑**（Status={status}，Pid=0）")

    # ③ 交叉验证：SQLite 的 -wal / -shm 在不在
    #    ★ 干净关闭时 SQLite 会 checkpoint 并**删掉**这两个文件 ⇒ 不在 = 大概率没人持有。
    #    ★ 比 `fuser` 可靠：DSM 上**没有 fuser**，而 `... || echo "没人占用"`
    #      会把"工具不存在"读成"没人占用"（2026-09-17 实测踩到过）。
    #    ★★ **但这道的分辨力有限，如实记下**（2026-09-17 实测）：
    #       ① 只是"读"（或**小事务**）时，WAL 可能**根本没建**或是 0 字节
    #          ⇒ 进程明明在持有，这道也看不出 ⇒ **会误报 stopped**；
    #       ② 只有当**写出了大量未 checkpoint 的数据**时，`-wal` 才明显存在
    #          ⇒ 那时它确实能判出 unknown（已实测）。
    #       ⇒ **所以这道只能"加严"，不能"独立证明没人持有"** ——
    #         真正靠得住的是 ② 的 `docker inspect`（State/Pid），本道只是**补充**。
    #       ⇒ 若哪天要给"持有中"一个**可靠**判据，该走 `lsof`/`fuser` 那种**按 fd 查**的路
    #         （而 DSM 上两者都可能缺）—— 那件事**没做**，别以为这里已经解决了。
    if db_path:
        present = [s for s in ("-wal", "-shm") if os.path.exists(db_path + s)]
        if present:
            det.append(f"③ ★ `{','.join(present)}` **仍在** ⇒ 可能有进程持有 ⇒ 判 unknown")
            return "unknown", det
        det.append("③ `-wal` / `-shm` 都不在（★ 只能说明**没有大量未落盘数据**，"
                   "不构成「没人持有」的证明）")
    else:
        det.append("③ （未给 db 路径，跳过 -wal 检查）")
    return "stopped", det


def main() -> int:
    ap = argparse.ArgumentParser(description="换 cross-seed.db 里 indexer.apikey")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--new-key-file", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force-running", action="store_true",
                    help="★ 不推荐：容器在跑时也写")
    args = ap.parse_args()

    db_p = pathlib.Path(args.db)
    key_p = pathlib.Path(args.new_key_file)
    if not db_p.is_file():
        print(f"[!!] 读不到 db：{db_p}"); return 2
    if not key_p.is_file():
        print(f"[!!] 读不到新 key 文件：{key_p}"); return 2

    new = key_p.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{16,64}", new):
        print(f"[!!] 新 key 形状不对（长度 {len(new)}）"); return 2

    con = sqlite3.connect(str(db_p))
    con.row_factory = sqlite3.Row

    # ① 完整性
    ok = con.execute("PRAGMA integrity_check").fetchone()[0]
    print(f"integrity_check: {ok}")
    if ok != "ok":
        print("[!!] 库不完整，停"); return 2

    # ② 现状（**只报长度与"是否与新 key 相同"**，不打印值）
    rows = list(con.execute("SELECT id, name, apikey FROM indexer"))
    print(f"indexer 行数: {len(rows)}")
    olds = set()
    for r in rows:
        k = r["apikey"] or ""
        olds.add(k)
        same = "（已是新 key）" if k == new else ""
        print(f"  id={r['id']:<3} {r['name'][:22]:<24} apikey 长度={len(k):<3} {same}")
    print(f"  ★ 不同取值数: {len(olds)}（卡里记的是 1 ⇒ 期望 1）")
    if len(olds) != 1:
        print("  ★★ 取值不止一种 —— 请先人工看一眼再决定，脚本停。"); return 2

    n_todo = sum(1 for r in rows if (r["apikey"] or "") != new)
    print(f"  ⇒ 需要更新的行: {n_todo} / {len(rows)}")

    if not args.apply:
        print("\n( dry-run —— 未写任何东西 )")
        return 0

    # ── 真闸：三条，每条都**真去查**；查不出来就报"未知"而不是放行 ──────────
    # ★ 2026-09-17 修：原来这里**根本不查**，只是无条件要求 `--force-running`，
    #   却打出一句「需要先停掉容器」—— 那句提示**与事实无关**（用户真的停了它，仍被拒），
    #   而且**把用户引向 `--force-running`**，而那正是唯一的保护。⇒ 假闸，已删。
    print("\n[闸] 检查容器是否真的没在跑 …")
    verdict, detail = container_state(str(db_p))
    for line in detail:
        print("     " + line)

    if verdict == "running":
        print("\n[!!] cross-seed **正在运行** ⇒ 不要写它持有的 db。")
        print("     停：  sudo docker compose stop cross-seed")
        print("     ★ 若 `stop` 报 `did not receive an exit event`，别硬来 —— 先看：")
        print("         sudo docker inspect reseed-cross-seed --format 'State={{.State.Status}} Pid={{.State.Pid}}'")
        print("      若显示 `exited` / `Pid=0` ⇒ 它其实已经死了（docker ps 会误报 Up）。")
        print("     ★ 确认它真的没跑之后，再加 --force-running。")
        return 2
    if verdict == "unknown":
        print("\n[!!] **查不出**容器状态（上面已说明原因）。")
        print("     闸不会在'不知道'的时候放行 —— 请人工确认后加 --force-running。")
        return 2
    if not args.force_running:
        # ★ 已确认没在跑；但**停容器是用户的决定**，所以还要一句显式确认。
        print("\n[!!] 容器已确认没在跑（见上）。但仍需你**显式确认**才写：")
        print("     加 --force-running 表示「我知道要写的是生产的 cross-seed.db」。")
        print("     ★ 若你确实想写、且上面的检查都显示 stopped ⇒ 加它即可。")
        return 2
    print("     ⇒ ✓ 已确认没在跑 ⇒ 继续写")

    bak = db_p.with_name(db_p.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(db_p, bak)
    try:
        with con:                                   # 事务
            con.execute("UPDATE indexer SET apikey = ?", (new,))
        # 回读核对
        after = list(con.execute("SELECT apikey FROM indexer"))
        bad = [i for i, r in enumerate(after) if (r["apikey"] or "") != new]
        print(f"★ 已写。备份: {bak}")
        print(f"★ 回读：{len(after)} 行，**不匹配 {len(bad)} 行** ⇒ {'✓ 全部更新' if not bad else '★★ 有问题！'}")
    except Exception as e:                          # noqa: BLE001
        print(f"[!!] 写失败：{type(e).__name__}: {e}")
        print(f"     备份在 {bak} —— 可直接拷回去")
        return 1
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
