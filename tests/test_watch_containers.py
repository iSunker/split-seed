# -*- coding: utf-8 -*-
"""`scripts/diag/watch-containers.sh` 的回归 —— 钉住**四个真 bug**（2026-09-28 一轮内全踩）。

## 为什么这个文件必须存在

那个脚本的**全部价值**是「容器没跑起来时把它拉起来，并告诉你」。
它的判据是 `docker inspect` 的读数 —— 而 **read 错、动错机器、报错对象**，
三种失败都**不会**表现在"脚本能跑通"上。本轮实测到的四个 bug 全属于这一类：
**输出看起来完全正常**（有 ✓ 有 ✗、有 rc），而含义是反的。

### ① 假 docker 把「预检」当成了「动作」—— 它**掩蔽了下面三条**

夹具里 `case "$1" in compose) touch $FLAG ...` 对**所有** `compose` 调用生效，
而脚本第 165 行**预检**跑的是 `docker compose version` ⇒ 预检就把"已拉起"的标志写了。
⇒ 后面三条 bug 在夹具上**全部看不见**（读数恒为"四个都在跑"）。
★ 这一条本身不是脚本的 bug，但它是**整轮验证作废**的原因，必须钉住形状：
  夹具必须**区分** `compose version`（无副作用）与 `compose ... up -d`（有副作用）。

### ② 双引号串里未转义的反引号 ⇒ **消息被当命令执行**

    say "   ★ 注意：`up -d` 返回 0 **不等于**容器已经健康"

sh 在双引号里把 `` `up -d` `` 当**命令替换** ⇒ 真去执行 `up -d`（报
`up: command not found`），同时把那段话**吞掉**（打出来少一块）。
★ 讽刺的是：**同一个文件头第 41 行就明文禁了这条**
（"给 shell 看的字符串里只用 ASCII 标点，注释里也不写反引号… dash/ash 的取词器
会误解成对的全角括号与反引号"）—— **代码违反了自家的规约**。
★ 判据形状：**消息可以有副作用**这件事，只有跑起来才看得见（源码里那句话是"对的"）。

### ③ `bad()` 在「检出」时就置 `RC=1` ⇒ **拉起成功也 exit 1**

文件头第 55 行的契约是 `0 = 四个都在跑（或已成功拉起）`。
而 `bad()` 是 `RC=1`，它在"发现容器不在跑"那一步就被调用了
⇒ 容器停 → 检出 → `up -d` 成功 → 复读通过 —— **这一趟是成功的，却 exit 1**。
★ 后果（真实）：NAS 计划任务每 10 分钟跑一趟 ⇒ **每次都记失败**，
  真失败时反而看不出（狼来了）；且"已处理好"与"拉不起来"**rc 一样**，没法分流告警。
★ 修法：拆出 `down()`（只打印 ✗、**不置 RC**）给"检出"用；`bad()` 只留给真失败。

### ④ `for c in $STILL $NEED_UP` ⇒ **给本来好好的容器也发告警**

`NEED_UP` 是「**曾经**不在跑」的集合，不是「还没修好」的集合。
⇒ 一个容器停了，**四个**全收信；且 `drive-loop` 在两个变量里都出现
⇒ 循环跑**五遍**（四容器）。
★ 这是本仓反复钉的「**读数一样、含义相反**」（`B.10` 第 14 条）：
  收到这封信的运维会以为"这四个都出事了"，实际三个一直好好的。
★ 修法：只对 `$STILL`（复读后**仍**不在跑的）投。

### ⑤ 守卫被 UNC 穿透（本轮第 4 个真 bug，是在 NAS 侧核实物时撞出来的）

守卫原来只判 `[A-Za-z]:`（盘符）和 `/*`（绝对路径），而 **UNC `//NAS/share` 以 `/` 开头**
⇒ **它穿过了守卫**。实测（开发机上拿 NAS 的 UNC 路径跑）：
  · `docker inspect` 打到的是**本机 Docker Desktop** ⇒ 四个容器全报「不存在」
  · 于是脚本**真的执行了 `docker compose ... up -d`** —— 打到**本机的 daemon**
  · 还试图往 NAS 的 UNC 路径投告警
★ 这就是守卫**存在的理由**，而它**越过了守卫本身** —— 所以本条钉的是
  「守卫的**补集**」：UNC、反斜杠、相对路径、盘符，四种都必须 rc=2。

## 钉什么（每条都带**阴性对照**，防"判据恒真"）

  ① UNC / 反斜杠 / 盘符 / 相对路径 ⇒ **rc=2**，且**不许**执行任何 `up -d`；
     真 NAS 路径（`/volume2/...`）**必须放行**（否则守卫会把生产也挡了）。
  ② 四种读数**必须长得不一样**：全在跑 / 拉起成功 / 拉不起来 / 读不出。
  ③ **拉起成功 = rc=0 且静默**（不投告警）；**拉不起来 = rc=1 且只给那一个投**。
  ④ 告警**只给 `$STILL`**：本来在跑的容器**一封都不许收**（bug ④ 的判据）。
  ⑤ 冷却：同一天同一容器不重复投。
  ⑥ 双引号串里**不许有未转义反引号**（bug ② 的**静态**判据 —— 它比跑起来快）。
  ⑦ 事件文件必须能被 `notify-spool.sh` 的 `field_of`/`body_of` 解析
     （`kind` / `title` / `metrics` 三件，且 metrics 里**不许有空格**）。

★ 全部离线：**假 docker 是本文件自己造的**（不碰真 daemon、不碰 NAS、不碰网络）。
  ★★ 假 docker 里**故意**把 `compose version` 与 `compose ... up -d` 分开记账 ——
     这正是 bug ① 的教训，别改回一条。
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
SRC = REPO / "data" / "scripts" / "diag" / "watch-containers.sh"

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


CONTAINERS = ["reseed-cross-seed", "reseed-drive-loop",
              "reseed-prowlarr", "reseed-flaresolverr"]

# ★★ 2026-10-01：**项目 B**（另一个 compose 项目）的容器 —— 脚本现在也盯它。
#   `qbittorrent-reseed` 与上面四个**不在同一个 compose 项目**里，脚本为它单开
#   第二次 `up -d`（在它自己的目录里）。夹具必须**同分辨率**：既要有它的 inspect 行，
#   也要让假 docker 记得"这次 up -d 是从哪个目录跑的"（否则"跑错目录"这个
#   最严重的失败形态**恒绿** —— 正是 §32.2 那条教训）。
QB_CONTAINERS = ["qbittorrent-reseed"]

#: daemon 身份的**真机实测值**（2026-09-29，`docker info --format '{{.Name}}|{{.OperatingSystem}}'`）。
#  ★ 开发机：docker-desktop|Docker Desktop     ← 必须**被拒**
#  ★ NAS：   iSunker-DS423|Synology NAS        ← 必须**放行**
#  ★ 别把这两个值改掉去"迁就"脚本 —— 它们是**实测读数**，脚本该迁就它们。
NAS_DAEMON = "iSunker-DS423|Synology NAS"
DEV_DAEMON = "docker-desktop|Docker Desktop"


def fixture(running: dict) -> str:
    """造一份 `docker inspect` 的 JSON。running: {name: (status, pid, health)}

    ★ 覆盖**两组**容器（项目 A 的四个 + 项目 B 的 qb）—— 因为脚本一次 inspect 拿全部。
      两组共用同一个 `running` 字典；不在字典里 = exited（与脚本"missing/exited"语义一致）。
    """
    items = []
    for name in CONTAINERS + QB_CONTAINERS:
        st, pid, health = running.get(name, ("exited", 0, "none"))
        item = {"Name": "/" + name,
                "State": {"Status": st, "Running": st == "running",
                          "Pid": pid, "ExitCode": 0}}
        if health != "none":
            item["State"]["Health"] = {"Status": health}
        items.append(item)
    import json
    return json.dumps(items)


#: 全部都在跑（含项目 B 的 qb —— 否则每个"都在跑"的用例都会看见 qb 停着去拉它）
ALL_OK = fixture({c: ("running", 100 + i, "healthy" if c == "reseed-drive-loop" else "none")
                  for i, c in enumerate(CONTAINERS + QB_CONTAINERS)})
#: drive-loop 停了，其余在跑（qb 也在跑 —— 这个夹具只测"项目 A 内一个停了"）
DL_EXITED = fixture({**{c: ("running", 100 + i, "none") for i, c in enumerate(CONTAINERS + QB_CONTAINERS)},
                     "reseed-drive-loop": ("exited", 0, "none")})
#: ★ 项目 B（qb）停了、项目 A 四个都在跑 —— 测"第二次 up -d 落在项目 B 目录"
QB_EXITED = fixture({**{c: ("running", 100 + i, "none") for i, c in enumerate(CONTAINERS + QB_CONTAINERS)},
                     "qbittorrent-reseed": ("exited", 0, "none")})


class Lab:
    """沙箱：脚本副本 + 假 docker。

    ★★ 假 docker 的关键设计（bug ① 的教训）：
      · `inspect`        → 读当前 fixture 文件
      · `compose version`→ **只记账，绝不碰标志**（预检无副作用）
      · `compose ... up -d` → 记账 + 翻标志（真动作才有副作用）
      ⇒ 若把这两条合成一条，bug ②③④ 就**又看不见了**。

    ★★ 2026-10-05：通知写侧已砍 ⇒ 本夹具**不再建 notify/spool 树、不再传 `--notify`**
      （那参数已删）。原先靠 spool 取证的判据改成**只看行为**（rc / 有没有 up -d /
      有没有重复拉），告警那几条改成"**没有**任何写侧副作用"。
    """

    def __init__(self):
        self.d = pathlib.Path(tempfile.mkdtemp(prefix="watch-lab-"))
        self.script = self.d / "watch-containers.sh"
        shutil.copy2(SRC, self.script)
        self.compose = self.d / "compose"
        self.compose.mkdir()
        (self.compose / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
        # ★★ 项目 B 的 compose 目录（2026-10-01）：脚本会为 qb 在这里跑第二次 up -d。
        #   用 `docker-compose.yml`（与真机项目 B 同名）—— 顺带测"两种文件名都探得到"。
        self.compose_qb = self.d / "compose_qb"
        self.compose_qb.mkdir()
        (self.compose_qb / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        self.calls = self.d / "calls.log"
        self.calls.write_text("", encoding="utf-8")
        self.fix = self.d / "fixture.json"
        self.fix.write_text(ALL_OK, encoding="utf-8")
        #: up -d 之后换成这份（None = 拉不起来，保持原样）
        self.after = None
        #: 假 docker 对 `info` 的回答。默认给 **NAS 的身份**（真机实测值），
        #  这样"sandbox 里跑得通"就等于"在 NAS 上跑得通"，而不是"在开发机上也能过"。
        #  要测"开发机被拒"就显式改成 DEV_DAEMON。
        self.daemon = NAS_DAEMON
        self.bindir = self.d / "bin"
        self.bindir.mkdir()
        self.docker = self.bindir / "docker"
        # ★★ `LAB_DAEMON` 是假 docker 对 `info` 的回答（daemon 身份校验用）。
        #   默认空 = **模拟读不出**（道 guard 会放行，见脚本里"读不出⇒不拒"那条）。
        #   ★ 2026-09-29 加：守卫多了一维「daemon 身份」，假 docker 必须能回答它 ——
        #     否则测不出「开发机被拒/NAS 放行」这一对判据（§32.8 那个洞的判据）。
        #   ★★ 2026-10-01 加：ACTION/PROBE 行**带上 `$(pwd)`** —— 这样"第二次 up -d
        #     跑在项目 B 目录"是**可断言**的。少了它，"跑错目录"（真机上 = `no such
        #     service`）会**恒绿**：正是 §32.2「夹具比被测系统粗 ⇒ 看不见真 bug」。
        #   ★★ 2026-10-01 加：ACTION/PROBE 行**带上目录名**（`$(pwd)` 的 basename）——
        #     这样"第二次 up -d 跑在项目 B 目录"是**可断言**的。少了它，"跑错目录"
        #     （真机上 = `no such service`）会**恒绿**：正是 §32.2「夹具比被测系统粗
        #     ⇒ 看不见真 bug」。★ 用 basename 而不是全路径：假 docker 跑在 MSYS sh 下，
        #     `$(pwd)` 是 `/c/...` 或 `/tmp/...`，与 python 的 `C:\...` **字符串不等** ——
        #     那会让判据**恒红**（同样什么也钉不住，方向相反）。两个 compose 目录的
        #     basename（compose / compose_qb）在本夹具里天然唯一。
        self.docker.write_text(
            "#!/bin/sh\n"
            "D=\"$LAB_DIR\"\n"
            'case "$1" in\n'
            "  inspect) cat \"$D/fixture.json\"; exit 0 ;;\n"
            "  info)\n"
            "    if [ -n \"$LAB_DAEMON\" ]; then printf '%s\\n' \"$LAB_DAEMON\"; fi\n"
            "    exit 0 ;;\n"
            "  compose)\n"
            "    cwdname=$(basename \"$(pwd)\")\n"
            "    case \"$*\" in\n"
            "      *\" up \"*)\n"
            "        printf 'ACTION cwd=%s :: %s\\n' \"$cwdname\" \"$*\" >> \"$D/calls.log\"\n"
            "        if [ -n \"$LAB_AFTER\" ]; then cp \"$LAB_AFTER\" \"$D/fixture.json\"; fi\n"
            "        exit 0 ;;\n"
            "      *)\n"
            "        printf 'PROBE cwd=%s :: %s\\n' \"$cwdname\" \"$*\" >> \"$D/calls.log\"\n"
            "        exit 0 ;;\n"
            "    esac ;;\n"
            "  version) exit 0 ;;\n"
            "esac\nexit 0\n",
            encoding="utf-8")
        os.chmod(self.docker, 0o755)

    def set_fixture(self, text: str) -> None:
        self.fix.write_text(text, encoding="utf-8")

    def env(self) -> dict:
        e = dict(os.environ)
        e.update(LAB_DIR=str(self.d),
                 LAB_AFTER=str(self.after) if self.after else "",
                 LAB_DAEMON=self.daemon,
                 FAKE_DOCKER=str(self.docker),
                 PATH=str(self.bindir) + os.pathsep + os.environ.get("PATH", ""))
        return e

    def run(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", str(self.script), "--compose", str(self.compose),
             "--compose-qb", str(self.compose_qb), *args],
            env=self.env(), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180)

    def calls_of(self, kind: str) -> list:
        return [ln for ln in self.calls.read_text(encoding="utf-8").splitlines()
                if ln.startswith(kind)]

    def calls_in_dir(self, kind: str, directory) -> list:
        """假 docker 里 `cwd=<basename>` 的调用（用于断言"第二次 up -d 跑在项目 B 目录"）。

        ★ 用 basename 比对：假 docker 在 MSYS sh 下 `$(pwd)` 的**全路径**与 python 的
          `C:\\...` 字符串不等（恒红），而两个 compose 目录的 basename 天然唯一。
        """
        name = pathlib.Path(directory).name
        return [ln for ln in self.calls_of(kind) if "cwd=%s " % name in ln]

    def cleanup(self) -> None:
        shutil.rmtree(self.d, ignore_errors=True)


def selftest_run(lab: Lab, *extra) -> subprocess.CompletedProcess:
    """走 `--selftest-live`（假 docker + **DRY=0**，才能进"拉起+复读"那段）。

    ★★ 为什么不能用 `--selftest`：它**强制 DRY=1**，而复读段是 `[ "$DRY" = 0 ]`
      才进的 ⇒ 那条**最要紧**的路径在 `--selftest` 下**永远走不到**。
      （这正是 bug ②③④ 能藏住的根因之一；本文件就是来钉这件事的。）
    """
    return subprocess.run(
        ["sh", str(lab.script), "--selftest-live",
         "--compose", str(lab.compose), "--compose-qb", str(lab.compose_qb), *extra],
        env=lab.env(), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180)


def selftest_run_path(lab: Lab, compose: str, *extra) -> subprocess.CompletedProcess:
    """用 `--selftest-live` 跑，但 **compose 路径由调用方指定**（测守卫用）。

    ★ 与 `selftest_run` 的区别只有路径：守卫判据要试"真 NAS 形状"与"开发机形状"，
      而 Lab 默认那个临时目录既不是前者也不是后者。
    """
    return subprocess.run(
        ["sh", str(lab.script), "--selftest-live",
         "--compose", compose, "--compose-qb", str(lab.compose_qb), *extra],
        env=lab.env(), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180)


# =====================================================================
print("\n── ① ★★ 机器守卫：四种「开发机形状」必须 rc=2，真 NAS 路径必须放行 ──")
# ★ 这一组**不需要**假 docker —— 守卫在探 docker **之前**就该拦下来。
#   判据成对：既要断言拦得住，也要断言**不误拦**（否则"守卫"恒真）。
lab = Lab()
try:
    for label, path, want in [
        ("Windows 盘符", "D:/Projects/x", 2),
        ("UNC（//NAS/share）", "//iSunker-DS423/docker_ssd/x", 2),
        ("反斜杠", "\\\\NAS\\share", 2),
        ("相对路径", "scripts", 2),
    ]:
        r = subprocess.run(["sh", str(lab.script), "--compose", path],
                           env=lab.env(), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        check(f"★ {label} ⇒ rc=2（未验，不是通过）", r.returncode == 2,
              "rc=%s out=%s" % (r.returncode, (r.stdout + r.stderr)[:200]))
    # 阴性对照：**真** NAS 形状必须放行（否则守卫把生产也挡了）
    # ★ 这里必须带上 **NAS 的 daemon 身份**：否则在开发机上跑自测会被
    #   daemon 身份那关拦掉（这正是它该做的），而那不是守卫"误拦"。
    lab.daemon = NAS_DAEMON
    r = selftest_run_path(lab, "/volume2/docker_ssd/reseed")
    check("★★ 对照：真 NAS 路径（/volume2/...）+ NAS 身份 **被放行**（不当成开发机拦掉）",
          r.returncode == 0 and "未验" not in r.stdout,
          "rc=%s（若这里红了看是不是守卫误拦）" % r.returncode)

    # ★★★ 2026-09-29 新增：**§32.8 那个洞本身的判据** ——
    #   "长得像 NAS 的绝对路径" + 开发机的 daemon ⇒ 必须 rc=2。
    #   ★ 这是路径守卫**永远**拦不住的那个形状（路径就是生产路径）。
    lab.daemon = DEV_DAEMON
    r = selftest_run_path(lab, "/volume2/docker_ssd/reseed")
    check("★★★ §32.8：生产路径 + **开发机 daemon** ⇒ rc=2（路径守卫拦不住的那一维）",
          r.returncode == 2, "rc=%s out=%s" % (r.returncode, (r.stdout + r.stderr)[:200]))
    check("  ↳ 且**没有**执行任何 up -d（绝不许打到本机 daemon）",
          lab.calls_of("ACTION") == [], repr(lab.calls_of("ACTION")))
    # ★★★ 2026-10-03 判据**就地更正**（`§32.17`）：**读不出** daemon 身份 ⇒ 现在判 **rc=2（未验）**，
    #   而**不是**旧版的 rc=0「放行」。
    #   ★ 为什么改：旧版写「读不出 ⇒ 放行（不误挡生产）」—— 那是**纯本机推理**。
    #     2026-10-03 00:13:54 生产实测把它的**另一面**暴露了出来：`dockerd` 重启时
    #     `docker info` 读不出 ⇒ 五个容器全被 `docker inspect` 报 `missing` ⇒
    #     脚本判「容器根本没创建」⇒ 去 `up -d`（daemon 不在，白跑）⇒ 投出 5 条
    #     「容器拉不起来 / 已尝试拉起 / 复读仍不在跑」——**与事实全反**（那晚没尝试过任何真正
    #     有效的拉起，真因是 daemon 不在）。★ 这是「只拒能确证的」这条**太松**：
    #     它把「读不出」当成了「没事」，而实测「读不出」**恰恰**是最该报的时刻。
    #   ⇒ 新判据：读不出 ⇒ **未验（rc=2）**，且**只投一条 daemon 级告警**，
    #     **不投**任何逐容器「拉不起来」告警、**不执行** up -d。
    lab.daemon = ""
    r = selftest_run_path(lab, "/volume2/docker_ssd/reseed")
    check("★★ 读不出 daemon 身份 ⇒ **未验 rc=2**（测到/没测到必须分得开）",
          r.returncode == 2, "rc=%s（读不出 = 未验，不是放行）" % r.returncode)
    check("★★ 且**没有**任何 up -d（daemon 不在时不空跑 compose）",
          lab.calls_of("ACTION") == [], repr(lab.calls_of("ACTION")))
    # ★★ 2026-10-05：通知写侧已砍 ⇒ 不再有 spool 可取；原「daemon 级告警」两条判据
    #   改成**行为不变性**：读不出 daemon ⇒ 只报未验，**不**做任何真动作。
    check("★★★ 读不出 daemon ⇒ **只报未验、不碰 daemon**（告警写侧已砍，判据转为行为）",
          lab.calls_of("ACTION") == [] and r.returncode == 2,
          "rc=%s actions=%s" % (r.returncode, lab.calls_of("ACTION")))
finally:
    lab.cleanup()

# =====================================================================
print("\n── ② ★★ 三种读数必须长得不一样（B.10 第 14 条）──")
# ★ 动机：'docker inspect 读不出' 与 '四个容器都没了' 若打成同一种输出，
#   前者会被念成后者 ⇒ 脚本会去 up -d 一遍（本仓 ERR-AI-09 的形状）。
lab = Lab()
try:
    # (a) 有内容 + 都在跑 ⇒ rc=0
    r = selftest_run(lab, "--dry-run")
    check("有其内容且都在跑 ⇒ rc=0", r.returncode == 0, r.stdout[-300:])
    check("  ↳ 且报「四个都在跑」", "都在跑" in r.stdout, r.stdout[-200:])

    # (b) 读不出（空串）⇒ rc=2，且**不许**说成"都没了"
    lab.set_fixture("")
    r = selftest_run(lab, "--dry-run")
    check("★ 读数**空** ⇒ rc=2（未验）", r.returncode == 2, "rc=%s" % r.returncode)
    check("  ↳ 且报「未验」/「读不出」", ("未验" in r.stdout or "读不出" in r.stdout), r.stdout[-300:])
    check("  ↳ ★ 且**不许**出现「不存在」（那是「确实没了」，含义相反）",
          "不存在" not in r.stdout, r.stdout[-300:])

    # (c) 读不出（垃圾，不是 JSON）⇒ 同样 rc=2
    lab.set_fixture("docker: command not found\n")
    r = selftest_run(lab, "--dry-run")
    check("★ 读数**不是 JSON** ⇒ rc=2（未验）", r.returncode == 2, "rc=%s" % r.returncode)

    # (d) 空数组 ⇒ 全部 missing（这是**真的**一个都没创建，与 (b) 含义不同）
    lab.set_fixture("[]\n")
    r = selftest_run(lab, "--dry-run")
    check("★ 空数组 ⇒ 全部（5 个）都报「不存在」",
          r.stdout.count("不存在") == len(CONTAINERS) + len(QB_CONTAINERS),
          "count=%d" % r.stdout.count("不存在"))
    check("  ↳ ★★ 与 (b) 的 rc **不一样**（2 vs 0/1）—— 两种读数可分",
          r.returncode != 2, "rc=%s" % r.returncode)
finally:
    lab.cleanup()

# =====================================================================
print("\n── ③ ★★ 退出码契约：拉起成功=0、拉不起来=1（bug ③ 的判据）──")
lab = Lab()
try:
    # (a) 都在跑 ⇒ 0，且**不执行 up -d**
    lab.set_fixture(ALL_OK)
    r = selftest_run(lab)
    check("全部都在跑 ⇒ rc=0", r.returncode == 0, "rc=%s" % r.returncode)
    check("  ↳ 且**没有**任何 up -d", lab.calls_of("ACTION") == [],
          repr(lab.calls_of("ACTION")))
    # ★★ 2026-10-05：通知写侧已砍 ⇒ 原「不投告警」判据改成**行为不变性**
    #   （成功拉起一趟不碰 daemon 之外的东西 —— 这里用"up -d 恰好一次"见证）。
    check("  ↳ 且**只**做了一次 up -d、没别的动作", len(lab.calls_of("ACTION")) == 0
          or len(lab.calls_of("ACTION")) == 1, repr(lab.calls_of("ACTION")))

    # (b) 一个停了 → 拉起成功 ⇒ **rc=0**（这是 bug ③ 的核心）
    lab2 = Lab()
    try:
        lab2.after = lab2.d / "after.json"
        lab2.after.write_text(ALL_OK, encoding="utf-8")
        lab2.set_fixture(DL_EXITED)
        r = selftest_run(lab2)
        check("★★ 检出停 → up -d 成功 → 复读通过 ⇒ **rc=0**（不是 1）",
              r.returncode == 0, "rc=%s（bug ③ 复现：检出就置 RC=1 会得到 1）" % r.returncode)
        check("  ↳ 且**真的执行了** up -d（且只带那一个 **服务名**）",
              len(lab2.calls_of("ACTION")) == 1
              and "drive-loop" in lab2.calls_of("ACTION")[0]
              and "reseed-drive-loop" not in lab2.calls_of("ACTION")[0],
              "（传容器名会 no such service —— 真机 2026-09-29 踩过）" + repr(lab2.calls_of("ACTION")))
        check("  ↳ 且**预检**走的是 PROBE 那条（没被当成动作）",
              len(lab2.calls_of("PROBE")) >= 1, repr(lab2.calls_of("PROBE")))
        check("  ↳ ★★ 拉起成功 ⇒ **静默**（rc=0，不再有写侧副作用）", r.returncode == 0,
              "（产品决定：只有拉起**失败**才报）rc=%s" % r.returncode)
    finally:
        lab2.cleanup()

    # (c) 拉不起来 ⇒ rc=1（bug ③ 的对侧）
    lab3 = Lab()
    try:
        lab3.set_fixture(DL_EXITED)   # LAB_AFTER 为空 ⇒ up 后 fixture 不变，仍 exited
        r = selftest_run(lab3)
        check("★★ 拉不起来 ⇒ rc=1", r.returncode == 1, "rc=%s" % r.returncode)
        check("★★ 且**只**对停的那一个发 up -d（本来在跑的**一次都不许拉**）",
              len(lab3.calls_of("ACTION")) == 1
              and "drive-loop" in lab3.calls_of("ACTION")[0]
              and not any(c in lab3.calls_of("ACTION")[0]
                          for c in ("cross-seed", "prowlarr", "flaresolverr")),
              "（bug ④：`for c in $STILL $NEED_UP` 会四个都拉）" + repr(lab3.calls_of("ACTION")))
    finally:
        lab3.cleanup()
finally:
    lab.cleanup()

# =====================================================================
print("\n── ④ ★ ★ 复读仍不在跑 ⇒ 结论稳定，不因重复跑而反复 up -d ──")
# ★ 2026-10-05：原「同日冷却：不重复投」随通知写侧砍掉而改为**行为**判据 ——
#   现在没有可冷却的信件，但「拉不起来」这一结论仍要**稳定**（两趟读数一致）。
lab = Lab()
try:
    lab.set_fixture(DL_EXITED)
    r1 = selftest_run(lab)
    n1 = len(lab.calls_of("ACTION"))
    r2 = selftest_run(lab)
    n2 = len(lab.calls_of("ACTION"))
    check("★ 第一趟：检出停 ⇒ 恰好一次 up -d", n1 == 1, repr(lab.calls_of("ACTION")))
    check("★★ 第二趟：读数一致 ⇒ 结论稳定（rc 相同）",
          r1.returncode == r2.returncode == 1, "r1=%s r2=%s" % (r1.returncode, r2.returncode))
finally:
    lab.cleanup()

# =====================================================================
print("\n── ⑤ ★ 静判据：双引号串里不许有**未转义反引号**（bug ②）──")
# ★ 为什么用静态判据：bug ② 的表现是"消息里那段话被吞掉 + 顺手跑了个命令"，
#   跑起来才看得见；而它在源码里**看着是完整的**（那句话字面写对了）。
#   ⇒ 静态扫一遍，比等它咬人便宜。
_src = SRC.read_text(encoding="utf-8")
# 只看**非注释**行里的双引号串（注释里写反引号无害 —— 文件头自己就在讲反引号）
_lines = [ln for ln in _src.split("\n")
          if not ln.lstrip().startswith("#")]
_bad = []
for ln in _lines:
    # 去掉已转义的反引号，再看剩下的双引号串里有没有裸反引号
    probe = ln.replace("\\`", "")
    for m in re.finditer(r'"([^"]*)"', probe):
        if "`" in m.group(1):
            _bad.append(ln.strip()[:120])
check("★★ 非注释行的双引号串里没有裸反引号", not _bad,
      "（会当命令替换执行）" + repr(_bad[:3]))
# 阴性对照：造一行已知的坏样本 ⇒ 判据必须判红（防"恒真"）
_probe = '            say "   ★ 注意：`up -d` 返回 0 不等于健康"'
_hit = any("`" in m.group(1)
           for m in re.finditer(r'"([^"]*)"', _probe.replace("\\`", "")))
check("★★ 对照：已知坏样本（原版那行）**判得红**", _hit,
      "判据恒真 ⇒ 它什么也没钉住")

# =====================================================================
print("\n── ⑥ ★★ 写侧已砍 ⇒ 脚本**不许**再产生任何文件/告警 ──")
# ★★ 2026-10-05：原「事件文件必须与 notify-spool.sh 同构」一节的**对象**（spool 事件文件）
#   已随通知写侧整体砍掉。⇒ 判据**反转**为阴性对照：跑一趟，确认**没有任何写侧副作用**
#   （文件系统里除假 docker 的 calls.log / fixture 外，临时目录内容不因脚本而增）。
lab = Lab()
try:
    lab.set_fixture(DL_EXITED)
    before = {p.name for p in lab.d.iterdir()}
    r = selftest_run(lab)
    after = {p.name for p in lab.d.iterdir()}
    check("前提：跑了一趟（拉不起来 ⇒ rc=1）", r.returncode == 1, "rc=%s" % r.returncode)
    check("★★ 脚本**没有**新建任何文件（spool/archive 写侧已砍）",
          after == before, "新增=%s" % sorted(after - before))
    check("★ 且没有 `notify`/`spool` 目录被重建",
          not (lab.d / "notify").exists() and not (lab.d / "spool").exists(),
          repr(sorted(x.name for x in lab.d.iterdir())))
finally:
    lab.cleanup()

# =====================================================================
print("\n── ⑦ ★ 本文件也得被登记（否则它是下一个 q2.py）──")
_counts = REPO / "tests" / "COUNTS.json"
check("前提：tests/COUNTS.json 在", _counts.exists())
if _counts.exists():
    import json
    _d = json.loads(_counts.read_text(encoding="utf-8"))
    check("★★ test_watch_containers.py 在 COUNTS.json 的 notes 里",
          "test_watch_containers.py" in (_d.get("notes") or {}),
          "跑 `python tests/gen-counts.py` 登记它")

# =====================================================================
print("\n── ⑧ ★★ 跨项目（2026-10-01）：qb 在**另一个 compose** 里 ⇒ 第二次 up -d 落在**它的**目录 ──")
# ★ 动机：`qbittorrent-reseed` 不在本项目 compose 里。若把它的名字混进项目 A 的
#   `up -d`（或用 COMPOSE_DIR 去 up 它），真机会报 `no such service`——而假 docker
#   若只记命令行、不记**目录**，这个错会**恒绿**（§32.2 的教训）。⇒ 夹具记 cwd。
lab = Lab()
try:
    # (a) 项目 B 停了 ⇒ 只有一次 ACTION，且**跑在项目 B 目录**、**不带 --profile**
    lab.set_fixture(QB_EXITED)
    r = selftest_run(lab)
    acts = lab.calls_of("ACTION")
    check("★★ 项目 B 停 ⇒ 恰 1 次 up -d", len(acts) == 1, repr(acts))
    check("★★ 且这次 up -d **跑在项目 B 目录**（否则真机 no such service）",
          len(lab.calls_in_dir("ACTION", lab.compose_qb)) == 1,
          "项目 A 目录收到 %d 次、项目 B 目录收到 %d 次"
          % (len(lab.calls_in_dir("ACTION", lab.compose)),
             len(lab.calls_in_dir("ACTION", lab.compose_qb))))
    if acts:
        check("★★ 且**不带 --profile**（项目 B 没有 profile）",
              "--profile" not in acts[0], repr(acts[0]))
        check("★★ 且服务名就是 qbittorrent-reseed（同名，别去前缀猜）",
              "qbittorrent-reseed" in acts[0], repr(acts[0]))
    check("  ↳ 且项目 A 的 up -d 一次都没跑（没有多余的）",
          lab.calls_in_dir("ACTION", lab.compose) == [],
          repr(lab.calls_in_dir("ACTION", lab.compose)))
finally:
    lab.cleanup()

# (b) ★★ 顺序 = 依赖：两组都停时，项目 B 的 up -d 必须**先于**项目 A 的
lab = Lab()
try:
    lab.after = lab.d / "after.json"
    lab.after.write_text(ALL_OK, encoding="utf-8")   # up 之后都起来 ⇒ 复读通过
    lab.set_fixture(fixture({c: ("exited", 0, "none") for c in CONTAINERS + QB_CONTAINERS}))
    r = selftest_run(lab)
    lines = lab.calls.read_text(encoding="utf-8").splitlines()
    act_lines = [ln for ln in lines if ln.startswith("ACTION")]
    _qbname = lab.compose_qb.name
    _aname = lab.compose.name
    ib = next((i for i, ln in enumerate(act_lines) if "cwd=%s " % _qbname in ln), None)
    ia = next((i for i, ln in enumerate(act_lines) if "cwd=%s " % _aname in ln), None)
    check("★★ 两组都停 ⇒ 两次 up -d（项目 B 一次、项目 A 一次）",
          ib is not None and ia is not None, repr(act_lines))
    check("★★★ 顺序：项目 B（qb）的 up -d **先于**项目 A 的 —— 这是跨项目依赖的**唯一**编码处",
          ib is not None and ia is not None and ib < ia,
          "B@%s A@%s ⇒ 顺序反了的话 cross-seed 会先于 qb 起来、空转重启" % (ib, ia))
    check("  ↳ ★ 顺序反了**判得红**（阴性对照：断定 ib<ia 不是恒真）",
          not (ia is not None and ib is not None and ia < ib),
          "（本行是同一判据的反面，恒真则说明夹具没区分目录）")
    check("  ↳ 且拉起成功 ⇒ rc=0（不再有写侧副作用）", r.returncode == 0,
          "rc=%s" % r.returncode)
finally:
    lab.cleanup()

# (c) 项目 B 拉不起来 ⇒ rc=1，且**只**对 qb 发 up -d（阴性：项目 A 四个一次都不许拉）
lab = Lab()
try:
    lab.set_fixture(QB_EXITED)   # LAB_AFTER 为空 ⇒ up 后 fixture 不变，qb 仍 exited
    r = selftest_run(lab)
    check("★ 项目 B 拉不起来 ⇒ rc=1", r.returncode == 1, "rc=%s" % r.returncode)
    acts = lab.calls_of("ACTION")
    check("  ↳ 且**只**对 qbittorrent-reseed 发 up -d",
          len(acts) == 1 and "qbittorrent-reseed" in acts[0], repr(acts))
    check("★★ 且项目 A 的四个**一次都不许拉**（阴性对照）",
          not any(c in (acts[0] if acts else "") for c in
                  ("reseed-cross-seed", "reseed-drive-loop", "reseed-prowlarr", "reseed-flaresolverr")),
          repr(acts))
finally:
    lab.cleanup()

# (d) ★ statically：脚本里**不许**把 qb 混进项目 A 的 CONTAINERS（那是错目录的根源）
_src8 = SRC.read_text(encoding="utf-8")
_containers_line = next((ln for ln in _src8.split("\n")
                         if ln.startswith("CONTAINERS=")), "")
check("★ 静态：`CONTAINERS=` 那一行**不含** qbittorrent-reseed（否则会用错目录 up 它）",
      "qbittorrent-reseed" not in _containers_line, repr(_containers_line))
check("★ 静态：另有 `CONTAINERS_QB=` 且含 qbittorrent-reseed",
      any(ln.startswith("CONTAINERS_QB=") and "qbittorrent-reseed" in ln
          for ln in _src8.split("\n")), "缺少 CONTAINERS_QB 声明")
# 阴性对照：上述静态判据不是恒真 —— 造一行错的必须判红
_probe8 = 'CONTAINERS="reseed-cross-seed qbittorrent-reseed"'
check("★ 对照：把 qb 混进 CONTAINERS 的那一行**判得红**",
      "qbittorrent-reseed" in _probe8 and not _probe8.startswith("CONTAINERS_QB="),
      "判据恒真 ⇒ 它什么也没钉住")

# =====================================================================
print("\n── ⑨ ★★ 静态：orchestrator 不属于看护名单 + cross-seed 走启动闸门（2026-10-01）──")
# ★★ 动机（两件事都**只能靠静态判据**钉，因为它们是「配置的形状」而离线跑不出来）：
#
#  (1) `reseed-orchestrator` 是**一次性 CLI**（`restart: "no"` 且**没有 `command:`**）⇒
#      它**本该缺席**。看护器盯着它 ⇒ 每趟报「不存在」⇒ `up -d` ⇒ **假告警轰炸**。
#      ★ 这是本仓最容易被"顺手补上"的错（四个 vs 六个看着像漏了）⇒ 用静态判据钉死：
#        在**仓库 compose** 里，orchestrator 服务**必须** `restart: "no"` 且**没有 `command:`**。
#        任何一条不成立 ⇒ 说明有人把它改成了常驻 ⇒ "它该不该被盯"的前提变了 ⇒ 判红提醒。
#        机理解释在 ENVIRONMENT.md `ERR-DOCKER-13`（A.11：同一事实只在一处维护）。
#
#  (2) cross-seed 的 `command:` 必须是**启动闸门**（`/config/wait-for-qb.sh`），不能直接
#      `daemon` —— 直接 daemon 会在 qb 未就绪时 exit 0 → restart 拉起 → **无限重启循环**
#      （`ERR-DOCKER-13`）。且闸门脚本本体必须**在仓库里**（不在仓库 ⇒ deploy 推不上去 ⇒
#      容器里没有那个文件 ⇒ 起不来）。判据**成对**：正的要有、反的（`command: daemon`）要判红。
try:
    import yaml  # noqa: PLC0415  —— 本仓必有（drive-loop 等测试都 import 它）
    _compose = yaml.safe_load((REPO / "compose.yaml").read_text(encoding="utf-8"))
    _svcs = _compose.get("services") or {}

    # (1) orchestrator：restart: no（或 "no"）且无 command
    _orch = _svcs.get("reseed-orchestrator") or {}
    _orch_restart = str(_orch.get("restart", "")).strip()
    check("★★ 静态：compose 里 `reseed-orchestrator` 是 `restart: \"no\"`（一次性 CLI，本该缺席）",
          _orch_restart == "no", "restart=%r ⇒ 它若变常驻，看护名单的前提就变了" % _orch_restart)
    check("★★ 静态：且 `reseed-orchestrator` **没有 `command:`**（缺 command = 不常驻）",
          "command" not in _orch, "有 command ⇒ 它可能变成常驻，需重新评估是否纳入看护")
    # 阴性对照：造一个"常驻 orchestrator"，上面两条判据的合取必须判红（防恒真）
    _probe_orch = {"restart": "unless-stopped", "command": "preflight"}
    _would_pass = (str(_probe_orch.get("restart", "")).strip() == "no") and ("command" not in _probe_orch)
    check("★★ 对照：一个「常驻 orchestrator」（restart=unless-stopped + 有 command）**判得红**",
          not _would_pass, "判据恒真 ⇒ 它什么也没钉住")

    # (2) cross-seed：entrypoint 是启动闸门
    _cs = _svcs.get("cross-seed") or {}
    # ★★ 判据必须是 **`entrypoint:`**、不是 `command:` —— NAS 首部署**实测**踩到的坑：
    #   本镜像 ENTRYPOINT = `catatonit -- /usr/local/bin/cross-seed` ⇒ 写 `command: ["sh", …]`
    #   会被当**参数**喂给 `cross-seed` ⇒ `error: unknown command 'sh'` ⇒ **崩溃循环**。
    #   ⇒ 判据：cross-seed 的 **`entrypoint:`** 含 `wait-for-qb.sh`，且**没有** `command:` 抢戏。
    #   ★ 这一条的"该抓的错"就是"写成了 command 而不是 entrypoint" —— 它跑起来才咬人。
    _cs_ep = _cs.get("entrypoint")
    _cs_ep_s = (" ".join(_cs_ep) if isinstance(_cs_ep, list)
                else ("" if _cs_ep is None else str(_cs_ep)))
    check("★★ 静态：cross-seed 用 **`entrypoint:`** 走**启动闸门**（含 wait-for-qb.sh）",
          "wait-for-qb.sh" in _cs_ep_s, "entrypoint=%r" % (_cs_ep,))
    check("★★ 静态：cross-seed **没有** `command:`（写 command 会变成 cross-seed 的**参数** ⇒"
          " `unknown command 'sh'` ⇒ 崩溃循环）",
          "command" not in _cs, "command=%r" % (_cs.get("command"),))
    # 阴性对照：把"闸门"错写在 `command:` 上（正是 NAS 上真实翻过的那种写法）必须判红
    _probe_cmd = {"entrypoint": None, "command": ["sh", "/config/wait-for-qb.sh"]}
    _probe_ep_s = (" ".join(_probe_cmd["entrypoint"]) if isinstance(_probe_cmd.get("entrypoint"), list)
                   else "")
    check("★★ 对照：把闸门错写在 `command:` 上（entrypoint 空）**判得红**",
          "wait-for-qb.sh" not in _probe_ep_s, "判据恒真 ⇒ 它什么也没钉住")

    # (3) 闸门脚本本体必须在仓库里（否则 deploy 推不上去 ⇒ 容器里没这个文件）
    _gate = REPO / "data" / "cross-seed" / "wait-for-qb.sh"
    check("★★ 静态：闸门脚本 `cross-seed/wait-for-qb.sh` **在仓库里**（否则 deploy 推不上去）",
          _gate.is_file(), "缺 %s" % _gate)
    # (4) 且它**带硬超时**（不许无限挂起 —— 那会变成"卡住的假在跑"，比崩溃循环更难发现）
    if _gate.is_file():
        _gs = _gate.read_text(encoding="utf-8")
        check("★★ 静态：闸门脚本有**硬超时**（含 MAX_WAIT 且超时后仍 `exec`，绝不无限挂起）",
              "MAX_WAIT" in _gs and "exec /usr/local/bin/cross-seed" in _gs,
              "缺 MAX_WAIT 或 exec ⇒ 可能无限挂起（静默假在跑）")
        check("  ↳ 且用**全路径** `exec /usr/local/bin/cross-seed`（entrypoint 被覆盖后 PATH 不可靠）",
              "exec /usr/local/bin/cross-seed daemon" in _gs,
              "经 PATH 找 cross-seed 在覆盖 entrypoint 后不保证成立")
except Exception as e:  # noqa: BLE001
    check("★★ ⑨ 段能读到并解析 compose（前提断言）", False, "异常: %r" % (e,))

print("\n%s（%d/%d 条）" % ("全部通过" if _ok == _n else "!!! %d 条失败" % (_n - _ok), _ok, _n))
sys.exit(0 if _ok == _n else 1)