# -*- coding: utf-8 -*-
"""`scripts/diag/watch-containers-loop.sh` 的回归 —— 钉住**循环语义**（2026-09-29）。

## 为什么这个文件必须存在

那个循环的**全部价值**是「**一直在**」。它的失效方式全都**不表现在"
脚本能跑通"**上，而是表现在**它什么时候退出**上：

### ① 单轮 `rc≠0` 就退出 ⇒ 把一次故障放大成**重启风暴**

循环里跑 `watch-containers.sh`，而它对"拉不起来"回 **rc=1**、对"查不了"回 **rc=2**。
本进程跑在 `restart: always` 的容器里 —— **一旦退出，docker 就重启它**。
⇒ 若按 rc 退出，则「一个容器拉不起来」会变成「**看护容器反复重启**」：
故障**被放大**，而且现场（它自己的日志）被重启冲掉。

★ 判据：**`rc=1` 和 `rc=2` 都必须继续下一轮**，不许退出。

### ② 循环"卡死"没有任何读数会报（★ 这条是选项 (C) 的洞）

`restart: always` 只在**进程退出**时拉 —— 而"进程活着但循环卡住"（比如
`sleep` 之后那段挂了、或脚本自己 deadlock）它**不管**，而且**没有任何东西会报**。
⇒ 本文件的处置：**每轮打一行带时间戳的心跳**（轮次号 + 时刻）。
判据钉两件：**轮次号在涨**、**时间戳存在** —— 这是唯一能把
「在跑」与「卡住的假在跑」分开的东西。

### ③ ★★ `--compose` 出现两次 ⇒ 本脚本的默认值**被静默覆盖**

**本机实测抓到**（写这个文件时）：循环每轮自己拼一个
`--compose "$DEFAULT_COMPOSE_DIR"`，而调用方若又透传一个 `--compose X`，
则命令行上有**两个** —— 而 `watch-containers.sh` 是**最后一个赢**。
⇒ 报告头打印的是**被覆盖后**的值，**看起来一切正常**，而本脚本的默认值
（那个实测过的 NAS 路径）**根本没生效**。
★ 同族：`B.10` 第 14 条「读数一样、含义相反」。
★ 判据：**只有一个 `--compose`**，且**调用方给的那个赢**。

## 钉什么

  ① `--once` + `rc=1` ⇒ **退出码是 1**（`--once` 才允许带出 rc）。
  ② `rc=1` **不带** `--once` ⇒ **不退**，继续跑下一轮（本文件的核心）。
  ③ `rc=2`（查不了）⇒ **同样不退**。
  ④ 心跳：**轮次号在涨** + **每轮都有时间戳行**（② 的洞）。
  ⑤ `--compose` **只出现一次**，且**调用方给的赢**（③ 的 bug 的判据）。
  ⑥ `--interval` 非数字 / 0 ⇒ **退回默认**且**说出来**（不静默）。

★ 全部离线：假 docker + 假 fixture，**不碰真 daemon、不碰 NAS、不碰网络**。
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
LOOP = REPO / "data" / "scripts" / "diag" / "watch-containers-loop.sh"
WATCH = REPO / "data" / "scripts" / "diag" / "watch-containers.sh"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

_ok = 0
_n = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _ok, _n
    _n += 1
    _ok += 1 if cond else 0
    print(("  ok  " if cond else " FAIL ") + "%s" % name)
    if not cond and detail:
        print("        ← " + detail)


#: 四个都在跑 / 一个停了（driveloop）
ALL_OK = ('[{"Name":"/reseed-cross-seed","State":{"Status":"running","Running":true,"Pid":11,"ExitCode":0}},'
          '{"Name":"/reseed-drive-loop","State":{"Status":"running","Running":true,"Pid":12,"ExitCode":0}},'
          '{"Name":"/reseed-prowlarr","State":{"Status":"running","Running":true,"Pid":13,"ExitCode":0}},'
          '{"Name":"/reseed-flaresolverr","State":{"Status":"running","Running":true,"Pid":14,"ExitCode":0}},'
          # ★ 2026-10-01：项目 B 的 qb 也必须"在跑" —— 否则"全在跑"用例会看见它 missing 去拉它。
          '{"Name":"/qbittorrent-reseed","State":{"Status":"running","Running":true,"Pid":15,"ExitCode":0}}]')

#: 一个停了，且**拉不起来**（compose 文件缺失 ⇒ rc=1）
DL_EXITED = ('[{"Name":"/reseed-cross-seed","State":{"Status":"running","Running":true,"Pid":11,"ExitCode":0}},'
             '{"Name":"/reseed-drive-loop","State":{"Status":"exited","Running":false,"Pid":0,"ExitCode":0}},'
             '{"Name":"/reseed-prowlarr","State":{"Status":"running","Running":true,"Pid":13,"ExitCode":0}},'
             '{"Name":"/reseed-flaresolverr","State":{"Status":"running","Running":true,"Pid":14,"ExitCode":0}}]')


class Lab:
    """沙箱：循环脚本 + 真 watch 脚本副本 + 假 docker + notify 目录。"""

    def __init__(self, fixture: str):
        self.d = pathlib.Path(tempfile.mkdtemp(prefix="watch-loop-lab-"))
        # ★ 循环脚本用 `$(dirname $0)/watch-containers.sh` 找兄弟脚本
        #   ⇒ 必须把**真** watch 脚本也拷进同一个目录（否则它会去找 NAS 路径）。
        self.bin = self.d / "scripts" / "diag"
        self.bin.mkdir(parents=True)
        self.loop = self.bin / "watch-containers-loop.sh"
        shutil.copy2(LOOP, self.loop)
        shutil.copy2(WATCH, self.bin / "watch-containers.sh")
        self.compose = self.d / "compose"
        self.compose.mkdir()
        # ★ 2026-10-01：项目 B 的 compose 目录（循环现在会透传 --compose-qb）。建出来即可
        #   —— 循环测试不关心它里面有没有 compose 文件（qb 在夹具里都是 running）。
        self.compose_qb = self.d / "compose_qb"
        self.compose_qb.mkdir()
        # ★ 不写 compose.yaml ⇒ 拉起必失败 ⇒ rc=1（② 段要的就是这个）
        self.notify = self.d / "notify"
        for s in ("spool", "archive"):
            (self.notify / s).mkdir(parents=True)
        self.fix = self.d / "fixture.json"
        self.fix.write_text(fixture, encoding="utf-8")
        self.calls = self.d / "calls.log"
        self.calls.write_text("", encoding="utf-8")
        self.docker = self.d / "docker"
        self.docker.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            '  inspect) cat "$LAB_FIX"; exit 0 ;;\n'
            '  info) printf \'%s\\n\' "iSunker-DS423|Synology NAS"; exit 0 ;;\n'
            '  compose) case "$*" in *" up "*) printf \'ACTION %s\\n\' "$*" >> "$LAB_CALLS"; exit 0 ;; *) exit 0 ;; esac ;;\n'
            "esac\nexit 0\n",
            encoding="utf-8")
        os.chmod(self.docker, 0o755)

    def env(self) -> dict:
        e = dict(os.environ)
        e.update(LAB_FIX=str(self.fix), LAB_CALLS=str(self.calls),
                 FAKE_DOCKER=str(self.docker),
                 PATH=str(self.d) + os.pathsep + os.environ.get("PATH", ""))
        return e

    def run(self, *args, timeout=120) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", str(self.loop), "--compose", str(self.compose),
             "--compose-qb", str(self.compose_qb),
             "--notify", str(self.notify), *args],
            env=self.env(), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout)

    def run_selftest(self, *args, loop_env: dict | None = None, timeout=180) -> subprocess.CompletedProcess:
        """跑循环，**透传 `--selftest-live`**（★ 这是唯一能在开发机上跑通的模式）。

        ★★ 为什么必须带它（本文件写时实测踩到，记下来）：
          `watch-containers.sh` 有两道**只对非自测生效**的守卫 ——
            ① 机器守卫：`C:\...` 这种 Windows 盘符 ⇒ rc=2；
            ② **daemon 身份校验**：读到 `docker-desktop` ⇒ rc=2。
          ⇒ 在 Windows 上直接跑（不带 `--selftest*`），**两道都会开火**，
            而它们**都是对的**（那正是它们存在的理由）。
          ⇒ 自测要测的是**循环逻辑**，不是守卫 ⇒ 必须走 `--selftest-live`
            （它豁免守卫①，且用假 docker ⇒ 守卫②读到的也是假的 NAS 身份）。
            ★ 而**守卫本身**由 `test_watch_containers.py` ①段负责钉 —— 各测各的。
        """
        e = {**self.env(), "WATCH_LOOP_SELFTEST": "1", "WATCH_INTERVAL": "1"}
        if loop_env:
            e.update(loop_env)
        return subprocess.run(
            ["sh", str(self.loop), "--compose", str(self.compose),
             "--compose-qb", str(self.compose_qb),
             "--notify", str(self.notify), "--selftest-live", *args],
            env=e, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout)

    def both(self, r) -> str:
        """stdout + stderr —— ★ 守卫的解释走 **stderr**（本文件写时踩到）。"""
        return (r.stdout or "") + (r.stderr or "")

    def cleanup(self) -> None:
        shutil.rmtree(self.d, ignore_errors=True)


# =====================================================================
print("\n── ① --once：退出码原样带出（唯一允许退出的模式）──")
lab = Lab(ALL_OK)
try:
    r = lab.run_selftest("--once")
    check("★ 四个都在跑 + --once ⇒ rc=0", r.returncode == 0,
          "rc=%s out=%s" % (r.returncode, (r.stdout or "")[-300:]))
finally:
    lab.cleanup()

lab = Lab(DL_EXITED)
try:
    r = lab.run_selftest("--once")
    check("★★ 拉不起来 + --once ⇒ rc=1（**rc 必须带出来**，否则 --once 没用）",
          r.returncode == 1, "rc=%s out=%s" % (r.returncode, (r.stdout or "")[-300:]))
finally:
    lab.cleanup()

# =====================================================================
print("\n── ② ★★ 核心：单轮 rc≠0 **不许**终止循环（否则 = 重启风暴）──")
lab = Lab(DL_EXITED)
try:
    # 跑 3 轮（WATCH_LOOP_SELFTEST=1 ⇒ 3 轮后自动退，免得挂住）
    r = lab.run_selftest()
    rounds = len(re.findall(r"---- 第 (\d+) 轮   ", r.stdout))
    check("★★ 单轮 rc=1 **没让它退出**：跑满了 3 轮（不是第 1 轮就死）",
          rounds >= 3, "轮数=%d\n%s" % (rounds, r.stdout[-500:]))
    check("  ↳ 且整个过程 rc=0（自测模式正常收尾）", r.returncode == 0,
          "rc=%s" % r.returncode)
    check("  ↳ 且每轮都记下了 rc（能看到 rc=1）", r.stdout.count("rc=1") >= 3,
          "rc=1 出现 %d 次" % r.stdout.count("rc=1"))
finally:
    lab.cleanup()

print("\n── ②b 查不了（rc=2）同样不许退出 ──")
lab = Lab(ALL_OK)
try:
    # 把 docker 挖掉 ⇒ watch 脚本 rc=2（查不了）
    lab.docker.unlink()
    r = subprocess.run(
        ["sh", str(lab.loop), "--compose", str(lab.compose), "--notify", str(lab.notify)],
        env={**lab.env(), "WATCH_LOOP_SELFTEST": "1", "WATCH_INTERVAL": "1"},
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    rounds = len(re.findall(r"---- 第 (\d+) 轮   ", r.stdout))
    check("★★ rc=2（没 docker / 读不出）⇒ **照样跑满 3 轮**",
          rounds >= 3, "轮数=%d\n%s" % (rounds, r.stdout[-400:]))
    check("  ↳ 且**没有**把 rc=1 当成 rc=2（语义没混）",
          "rc=2" in r.stdout, r.stdout[-400:])
finally:
    lab.cleanup()

# =====================================================================
print("\n── ③ ★★ 心跳：轮次号在涨 + 每轮有时间戳（选项 (C) 的洞）──")
lab = Lab(ALL_OK)
try:
    r = lab.run_selftest()
    nums = [int(x) for x in re.findall(r"---- 第 (\d+) 轮   ", r.stdout)]
    check("★★ 轮次号**递增且不重复**（能区分『在跑』与『卡住的假在跑』）",
          nums == sorted(set(nums)) and len(nums) >= 3, repr(nums))
    ts = re.findall(r"---- 第 \d+ 轮   (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ----", r.stdout)
    check("★★ 每轮都带**时间戳**（没有它就分不出『卡住』）",
          len(ts) >= 3, repr(ts))
    check("  ↳ 且**每轮**都有收尾行（不是只有开头有）",
          r.stdout.count("轮结束") >= 3, "轮结束 出现 %d 次" % r.stdout.count("轮结束"))
finally:
    lab.cleanup()

# =====================================================================
print("\n── ④ ★★ --compose 只出现一次，且**调用方给的赢**（实测抓到的 bug）──")
lab = Lab(ALL_OK)
try:
    r = lab.run_selftest()
    # 报告头里的 COMPOSE_DIR 必须是**调用方传的那个**（lab.compose），不是默认的 NAS 路径
    m = re.search(r"COMPOSE_DIR=(.+)", r.stdout)
    got = (m.group(1).strip() if m else "")
    check("★★ COMPOSE_DIR = 调用方传的（不是被覆盖成默认 NAS 路径）",
          str(lab.compose) in got and "/volume2/" not in got,
          "COMPOSE_DIR=%r（若含 /volume2/ 说明默认值偷偷赢了）" % got)
    check("  ↳ 且循环自己的头里也报的是它",
          str(lab.compose) in r.stdout, (r.stdout or "")[:300])
finally:
    lab.cleanup()

# =====================================================================
print("\n── ④b ★★ --compose-qb 同族：调用方给的赢（2026-10-01 加）──")
# ★ 与 ④ 完全同族：循环自己也拼 `--compose-qb`，调用方若再透传一个，
#   watch-containers.sh 的"最后一个赢"会让**默认值静默覆盖调用方的** —— 同一个坑。
#   ★★ 判据写成**成对**的（防"恒真"）：
#     ① 正例：调用方传的**非默认**路径 ⇒ 报告头必须是它；
#     ② 对照：把那句判据喂给**默认值**，必须判红（否则这条判据什么也没钉住）。
lab = Lab(ALL_OK)
try:
    r = lab.run_selftest()
    m = re.search(r"COMPOSE_DIR_QB=(.+)", r.stdout)
    got = (m.group(1).strip() if m else "")
    check("★★ COMPOSE_DIR_QB = 调用方传的（不是被覆盖成默认 NAS 路径）",
          str(lab.compose_qb) in got and "/volume2/" not in got,
          "COMPOSE_DIR_QB=%r（若含 /volume2/ 说明默认值偷偷赢了）" % got)
    check("  ↳ 且循环自己的头里也报了 compose-qb",
          "compose-qb" in r.stdout, (r.stdout or "")[:400])
    # 阴性对照：判据的反面（默认 NAS 路径）必须判红 —— 防"恒真"
    _default = "/volume2/docker_ssd/qbittorrent-reseed"
    _would_pass = (_default in _default) and ("/volume2/" not in _default)
    check("★★ 对照：默认 NAS 路径喂进同一判据 ⇒ **判得红**（判据非恒真）",
          not _would_pass, "判据恒真 ⇒ 它什么也没钉住")
finally:
    lab.cleanup()

# =====================================================================
print("\n── ⑤ ★ --interval 非法值 ⇒ 退回默认并**说出来**（不静默）──")
lab = Lab(ALL_OK)
try:
    r = subprocess.run(
        ["sh", str(lab.loop), "--compose", str(lab.compose), "--notify", str(lab.notify),
         "--selftest-live", "--interval", "abc", "--once"],
        env=lab.env(), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60)
    check("★ 非数字 interval ⇒ 提示退回默认", "退回默认" in (r.stdout + r.stderr),
          (r.stdout + r.stderr)[:300])
    check("  ↳ 且仍能正常跑完（不因为参数错就崩）", r.returncode in (0, 1),
          "rc=%s" % r.returncode)
    r2 = subprocess.run(
        ["sh", str(lab.loop), "--compose", str(lab.compose), "--notify", str(lab.notify),
         "--selftest-live", "--interval", "0", "--once"],
        env=lab.env(), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60)
    check("★ interval=0 ⇒ 也退回（0 会变成忙等烧 CPU）",
          "退回默认" in (r2.stdout + r2.stderr), (r2.stdout + r2.stderr)[:300])
finally:
    lab.cleanup()

# =====================================================================
print("\n── ⑥ ★ 静态判据：**没有**「同一行里既判 rc 又 exit」的行 ──")
# ★ 为什么用静态判据：这条 bug 的表现是"**某个** rc 下退出了"，而离线很难穷举所有
#   rc 组合（还要造出每种 rc 的现场）⇒ 直接看源码形状更省。
# ★★ 判据写的**是那个形状本身**：「同一行里既有 rc 判断、又有 exit」——
#   那正是"按 rc 退出"（= 会变成重启风暴）的写法。
_src = (REPO / "data" / "scripts" / "diag" / "watch-containers-loop.sh").read_text(encoding="utf-8")


def _has_rc_guarded_exit(src: str) -> bool:
    """有没有『**按 rc 决定要不要 exit**』的行 —— 那正是会变成重启风暴的形状。

    ★★ 判据必须**区分两种 `exit "$RC"`**（本文件第一版在这里栽了，留痕）：
      · **坏**：`if [ "$RC" -ne 0 ]; then exit "$RC"; fi`  ⇒ 按 rc 退出
      · **好**：`if [ "$ONCE" = 1 ]; then exit "$RC"; fi`   ⇒ 只在 --once 退（rc 只是被带出）
    两者**都含 `exit` + `$RC`** ⇒ 靠"行内共现"分不开（第一版就是这么恒真的）。
    ⇒ 真正的判据是：**条件里有没有拿 `RC` 做比较**（`-ne` / `-eq` / `!=` / `=` 对 RC）。
    """
    for ln in src.split("\n"):
        t = ln.strip()
        if t.startswith("#") or t.startswith("echo"):
            continue
        if "exit" not in t:
            continue
        # 条件里是否拿 RC 做比较（那才是"按 rc 退出"）
        if re.search(r'\$RC"?\s*(-ne|-eq|=|!=)|\[\s*"\$RC"', t):
            return True
    return False


check("★★ 真脚本里**没有**「同一行既判 rc 又 exit」的行（有 ⇒ 循环会提前退出）",
      not _has_rc_guarded_exit(_src),
      "真脚本里出现了 rc 守卫的 exit —— 那会把『一次故障』放大成『重启风暴』")

# ★ 阴性对照：判据本身要能判红（防恒真）。
# ★★ 第一版在这里**写错了**并被抓出来（留痕，因为这是本仓的老毛病）：
#    当时把"坏样本"当成**整行以 `exit ` 开头**来找，而 `if ...; then exit ...` 这行
#    **不以 `exit ` 开头** ⇒ 判据为假 ⇒ **恒真**（等于什么也没钉住）。
#    ⇒ 现在的判据找的是**行内共现**（rc + exit），坏样本就能被判红。
check("★★ 对照：已知坏样本（`if [ \"$RC\" -ne 0 ]; then exit \"$RC\"; fi`）**判得红**",
      _has_rc_guarded_exit('  if [ "$RC" -ne 0 ]; then exit "$RC"; fi'),
      "判据恒真 ⇒ 它什么也没钉住")
# ★ 第二层对照：**合法**的 `--once` 退出**不该**被判红（否则守卫恒红）
check("  ↳ 反向对照：`--once` 那种**无条件** exit **不**被判红",
      not _has_rc_guarded_exit('if [ "$ONCE" = 1 ]; then exit "$RC"; fi'),
      "把合法的 --once 退出也判红了 ⇒ 守卫恒红")

print("\n%s（%d/%d 条）" % ("全部通过" if _ok == _n else "!!! %d 条失败" % (_n - _ok), _ok, _n))
sys.exit(0 if _ok == _n else 1)
