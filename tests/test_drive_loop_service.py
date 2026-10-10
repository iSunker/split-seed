# -*- coding: utf-8 -*-
"""`#58` drive-loop 迁容器 —— **本机离线**能钉住的那一半。

它钉的不是「迁容器做完了」。恰恰相反，本文件里**没有一条**断言能证明
「容器里真跑得起来」—— 那要 NAS、要 `docker`、要一次真批次（见文末「本机验不了的」）。
它钉的是**这次改动的三样东西，每一样都有一个"悄悄退化回原样"的失效模式**：

  ① `compose.yaml` 里的 `drive-loop` 服务（新增）
     ★★ 最要命的一条是 `profiles`：它是**安全锁**，不是分类标签。
        删掉它 ⇒ 该服务被 `docker compose up -d` **默认拉起来** ⇒
        在 DSM 任务那份 `run.sh` 还活着的时候把第二条 webhook 路点亮 ⇒
        一次 429 可能废掉几百条（README 坑 4）。
        **反向也要钉**：其余四个服务**都不许**带 profile —— 那会把它们一起锁住，
        是另一种事故（prowlarr 起不来 ⇒ cross-seed 全站搜不到）。
     ★ 另一条是「不写 `build:`」：本仓 build 上下文是**仓库根**、NAS 上是
        **compose 目录**，写了会在 NAS 上 build 失败。

  ② `scripts/drive-loop-resident.sh`（新增）
     钉住它**没有**把 `--once` 带上命令行 —— 常驻与 `--once` 就差这一个开关，
     而带错的后果不是报错、是「跑一批就退出 + 被 `restart: unless-stopped`
     反复拉起来」，看着像在跑。
     ★ 注释里**允许**出现 `--once`（要解释为什么不要它）⇒ 判据必须
       **只看命令行的参数区段**，不能用「grep 全文有没有 `--once`」那种写法。
     ★ 另钉 `--indexers` 名单与 `drive-loop-nas.sh` **逐字相同**：
       那个名单有**两个声明点**（本文件是第二个），而"加了站却漏改一处"
       正是 2026-09-12 卡住 224 部片子的形状。这条是本文件里**唯一**能抓住
       那种漂移的判据。

  ③ `scripts/drive-loop.py` 的**常驻分支真的写心跳**（15.a 的回归测试）
     ★★ 这是**唯一一条真能变红**的断言：把那段改回原样它就红。
        原缺陷的形状是「不报错、只是不动」—— `write_state()` 全在
        `once_round()` 里 ⇒ 常驻模式没有 `heartbeat_ts` ⇒
        `batch_alive()` 的容器分支**保守返回 True** ⇒ 「卡死被接管」永不发生。

★ 数法/行形：本文件只打印 `  ok  ` / ` FAIL `（`tests/README.md` 的形状 A）。
  **不许**自创行形 —— 新行形会让整份断言一条都不进账、而退出码仍是 0
  （`test_notify_drain.py` 实测过一次）。
★ 本文件**不碰任何状态文件**，所以不需要 `tempfile.mkdtemp()` 重定向；
  它只读仓库里的三个文件 + 做一次 AST 解析。
"""
from __future__ import annotations

import ast
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
# ★ 2026-10-05（B2）：容器代码收进 data/ ⇒ orchestrator / drive-loop 源码
#   都在 data/ 下。把 data/ 也加进 import 搜索路径（本文件上一行插的是仓库根）。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "data"))

try:
    sys.stdout.reconfigure(encoding="utf-8")   # GBK 控制台会炸（tests/README.md）
except Exception:
    pass

try:
    import yaml                                  # orchestrator 的依赖，本仓必有
except Exception:                                # noqa: BLE001
    yaml = None

_ok = 0
_bad = 0


def ck(name: str, cond: bool, extra: str = "") -> None:
    global _ok, _bad
    if cond:
        _ok += 1
        print(f"  ok   {name}")
    else:
        _bad += 1
        print(f"  FAIL {name}{('  —— ' + extra) if extra else ''}")


def ck_no(name: str, cond: bool, extra: str = "") -> None:
    """**期望为假**的断言 —— 阴性对照专用（`ck` 只能表达"期望为真"）。

    ★★ 为什么要它（2026-09-22 我自己踩的）：这个文件里的 `ck(name, cond, extra)`
      第二个参数是**条件本身**，不是"got"。所以想断言"判据说 no"时，
      写 `ck(name, (100 > 100))` 会**因为条件为假而报红** —— 报的是"断言失败"，
      而它**恰恰印证了判据说 no**。⇒ 语义反了，必须有个专门的入口。
    ★ 与 `test_once_gate.py` 的 `ck(name, got, want)` **同名不同义** ——
      两个文件的 `ck` 不是同一个东西，别跨文件照抄（我在这里连报两条假红）。
    """
    global _ok, _bad
    if not cond:
        _ok += 1
        print(f"  ok   {name}")
    else:
        _bad += 1
        print(f"  FAIL {name}（★ 期望为假却为真）{('  —— ' + extra) if extra else ''}")


COMPOSE = REPO / "compose.yaml"
RESIDENT = REPO / "data" / "drive-loop" / "run-resident.sh"
DOCKER_PY = REPO / "data" / "drive-loop" / "scripts" / "drive-loop.py"
NAS_SH = REPO / "data" / "drive-loop" / "run.sh"
# ★ 2026-10-05：`DEPLOY = REPO / "deploy.sh"` 已删（deploy.sh 整个删掉）——
#   受管集合改从哨兵内的常量 `DEPLOY_MANIFEST` 读（见 ⑥ 段里的 `_M`）。
MOUNT_SELFCHECK = REPO / "scripts" / "diag" / "drive-loop-mount-selfcheck.sh"
CHK58 = REPO / "scripts" / "diag" / "chk58.sh"

# ★★ 整份测试的**前提**：文件在不在。缺了就直接红，别让后面十几条
#    全部"因为读不到而跳过"——那是把「没验」伪装成「通过」（B.10 第 14 条）。
ck("前提：docker-compose.yml 存在", COMPOSE.is_file(), str(COMPOSE))
ck("前提：scripts/drive-loop-resident.sh 存在", RESIDENT.is_file(), str(RESIDENT))
ck("前提：scripts/drive-loop.py 存在", DOCKER_PY.is_file(), str(DOCKER_PY))

# ==========================================================================
print("\n=== ① compose：drive-loop 服务的形状 ===")
# ==========================================================================
YML = None
if yaml is not None and COMPOSE.is_file():
    YML = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
ck("前提：本机有 PyYAML（没有就该红，而不是静默跳过）", YML is not None,
   "缺 PyYAML ⇒ ① 整段无法判")

if YML:
    svcs = YML.get("services") or {}
    dl = svcs.get("drive-loop")
    ck("①a compose 里有 drive-loop 服务", isinstance(dl, dict), repr(sorted(svcs)))

if YML and isinstance(svcs.get("drive-loop"), dict):
    dl = svcs["drive-loop"]

    # ---- ①b profiles 锁（★ 这是本文件最重要的一条）------------------------
    ck("★★ ①b drive-loop 有 profiles（安全锁：删掉它就会被 up -d 默认拉起来）",
       dl.get("profiles") == ["drive-loop"], repr(dl.get("profiles")))
    #   ★ 反向：别的服务**不许**带 profile。少了这条，一个手滑把 profile
    #     加到 prowlarr 上就会把它锁住，而表现是"cross-seed 搜不到任何站"。
    #   ★★ 2026-09-17：`autoheal` **必须排除在外** —— 它**故意**带同一个 profile
    #     （与 drive-loop 同生同死，见 ⑦ 段）。不排除的话，这条一加服务就红，
    #     而它红得**有道理却指错方向**（读者会去删 autoheal 的 profile）。
    #   ★★ 2026-09-29：`watcher` **同样排除** —— 理由**完全同族**：它也是
    #     「与 drive-loop 同生同死」的**侧车**（容器巡检拉起件，见其服务块头注）。
    #     判据不变：**只有"服务 drive-loop 的侧车"才允许带这个 profile**。
    _LOCKED = {"drive-loop", "autoheal", "watcher"}   # ← 允许带 profile 的服务
    _others = {n: s.get("profiles") for n, s in svcs.items()
               if n not in _LOCKED and s.get("profiles")}
    ck("★★ ①c 其余服务**都不带** profiles（带 = 被一起锁住，另一种事故）",
       _others == {}, repr(_others))
    #   ★ 再钉一条：**允许带 profile 的恰好是那三个**，别多也别少。
    #   ★★ 为什么是"恰好"而不是"至少"：多一个 = 某个业务服务被**误锁**
    #     （表现是"cross-seed 搜不到任何站"那种远距离症状）；少一个 = 侧车会
    #     被裸的 `docker compose up -d` **单独**拉起来（握着 docker.sock 空转）。
    _withp = sorted(n for n, s in svcs.items() if s.get("profiles"))
    ck("★ ①c2 带 profile 的服务**恰好**是 autoheal + drive-loop + watcher（多一个就是误锁）",
       _withp == ["autoheal", "drive-loop", "watcher"], repr(_withp))

    # ---- ①d 不写 build:（NAS 上 build 上下文不同）-------------------------
    ck("★ ①d drive-loop 不写 build:（NAS 的构建上下文是 compose 目录，写了会炸）",
       "build" not in dl, repr(dl.get("build")))

    # ---- ①e image = Docker Hub 上的 reseed-toolkit-drive-loop ------------------
    # ★★ 2026-10-05（B5，push/pull）：镜像改从 **Docker Hub** 拉（`isunker/reseed-toolkit-drive-loop`，
    #   tag=git 短 sha）。判据不写死具体 sha（每次发版都变）⇒ 断言**前缀 + 仓库名**，
    #   并**反向**断言不再是旧的本地 tag（回滚到本地 build 那套会当场红）。
    _img = dl.get("image") or ""
    ck("①e image = Docker Hub isunker/reseed-toolkit-drive-loop:<sha>",
       _img.startswith("isunker/reseed-toolkit-drive-loop:")
       and _img != "isunker/reseed-toolkit-drive-loop:",
       repr(_img))
    ck("①e2 且不再是旧的本地 tag（pull 模型，NAS 不 build）",
       _img != "reseed-drive-loop:0.1.0", repr(_img))

    # ---- ①f logging 锚点没漏（漏了 = 无上限日志，/volume1 只剩约 20 GB）----
    _lg = dl.get("logging")
    ck("①f 有 logging（走 `*logging-limits` 锚点，且已被解析成 json-file+上限）",
       isinstance(_lg, dict) and _lg.get("driver") == "json-file"
       and _lg.get("options", {}).get("max-size") == "10m", repr(_lg))

    # ---- ①g 常驻语义：restart 要对，且入口不是"跑一批就退出"---------
    ck("①g restart = unless-stopped（常驻服务）",
       dl.get("restart") == "unless-stopped", repr(dl.get("restart")))
    #   ★★★ 2026-09-17 **实测炸过一次**：入口必须写在 `entrypoint:` 上，
    #     **不能写 `command:`** —— 镜像的 `ENTRYPOINT ["python", "…/drive-loop.py"]`
    #     **不会被 `command:` 覆盖**（`command:` 只替换 CMD）⇒ 实际执行的是
    #         python /app/scripts/drive-loop.py sh drive-loop/run-resident.sh
    #     ⇒ argparse 报 `unrecognized arguments` ⇒ 退出码 2 ⇒ 被 restart
    #     **每 ~10 秒重拉一次**，而 `attempts.log` 里 **`[resident]` 一行都不写**
    #     （根本没跑到那个脚本）⇒ 症状极像"路径不对"，真因是 ENTRYPOINT。
    #     ⇒ 这条断言就是那次事故的回归测试：**它必须能红**。
    _ep = dl.get("entrypoint")
    _ep_s = " ".join(_ep) if isinstance(_ep, list) else str(_ep or "")
    ck("★★★ ①h 入口写在 **entrypoint**（不是 command）里，且指向 run-resident.sh"
       "—— command 覆盖不了 ENTRYPOINT，写错了容器起来即退（码 2）",
       bool(_ep) and "run-resident.sh" in _ep_s, f"entrypoint={_ep!r}")
    #   ★ 反向：**别同时写 command** —— 镜像的 ENTRYPOINT 已被 entrypoint 替换，
    #     再给 command 只会变成"传给 run-resident.sh 的多余参数"（脚本用 "$@" 透传，
    #     最终喂给 argparse ⇒ 又是 unrecognized arguments）。
    #   ★★ 2026-09-22 修：原先用的是 `"command" not in dl`，而 `dl` 是**整个服务段**
    #     ⇒ 只要**任何**注释或值里出现 `command` 这个词就报红（极易假红），
    #     而**真正**要判的是「compose 的 `command:` **键**在不在」。
    #     ⇒ 改成读**键**。★ 顺带：这一条当初是真有分辨力的（写错会假绿），
    #       只是判据本身太脏 —— 属于"判据要能被证伪"那条的另一半：
    #       判据也不该**对无关输入敏感**。
    #   ★★ 而这一条**为什么值得单独提**：2026-09-22 我把 `DRIVE_PACKS=…` 误写进
    #     `volumes:` 时，**没有任何断言报红**（①k 当时只逐条比 1:1，两条挂载本就
    #     1:1 ⇒ 基线也是绿的）。补的 ①k2 就是给那种形状的。这里顺手记一笔。
    ck("★ ①h2 没有同时写 command（会变成喂给脚本的多余参数）",
       "command" not in dl, repr(dl.get("command")))
    #   ★ 绝对路径：镜像 WORKDIR 是 /app；★ 2026-10-05（B2/§8.3）：代码入镜像后
    #     entrypoint 指向**镜像内**的 `/app/run-resident.sh`（由 Dockerfile COPY 进去），
    #     不再指向挂载区。
    ck("★ ①h3 entrypoint = 镜像内 /app/run-resident.sh（绝对路径）",
       _ep_s.strip() == "sh /app/run-resident.sh", _ep_s)
    _cmd = dl.get("command") or dl.get("entrypoint") or []
    _cmd_s = " ".join(_cmd) if isinstance(_cmd, list) else str(_cmd)
    #   ★★ 这一条就是常驻与 `--once` 的分界。入口上带了它 ⇒ 跑一批就退出。
    ck("★★ ①i 入口**不带** --once（带了 = 跑一批就退出，被 restart 反复拉起）",
       "--once" not in _cmd_s, _cmd_s)

    # ---- ①j 两条网络都要（缺一条就有一半服务名解析不了）-------------------
    _nets = dl.get("networks") or []
    if isinstance(_nets, dict):                  # compose config 渲染后的形态
        _nets = list(_nets)
    ck("★ ①j networks 同时含 reseed-net 与 qbit-net（缺一条就连不上另一个服务）",
       "reseed-net" in _nets and "qbit-net" in _nets, repr(_nets))

    # ---- ①k 挂载清单（2026-10-05 按「代码入镜像」模型重写）-------------------
    #   ★★ 旧模型断言"**每条**挂载都 1:1 同名同路径"；新模型下 compose 根仍 1:1
    #     且 `:ro`，而**运行态**单独挂 `./data/drive-loop/state:/state`（**故意不 1:1**）。
    #   ⇒ 判据改成**逐条点名**：① compose 根 1:1 只读；② 状态目录挂到 /state；
    #      ③ 媒体卷 1:1；④ 时区。★ 逐条点名比"全部 1:1"更严 —— 它同时钉住"不许
    #      再出现上面那种"多挂/少挂一条却不报红"的形状。
    #   ★ 用**渲染后**的 dict 形态（`source`/`target`/`read_only`）断言 —— Docker 自己
    #     已经把 `:ro` 剥好了，不用我写字符串切分（`A.11`）。
    _vols = dl.get("volumes") or []
    _envs = dl.get("environment") or []
    _newroot = "/volume2/docker_ssd/reseed"

    def _mt(v):
        if isinstance(v, dict):
            return (v.get("source"), v.get("target"), bool(v.get("read_only")))
        parts = str(v).split(":")
        ro = len(parts) >= 3 and parts[-1] in ("ro", "rw", "z", "Z")
        if len(parts) >= 3 and parts[-1] in ("ro", "rw", "z", "Z"):
            parts = parts[:-1]
        return (parts[0] if parts else "", parts[1] if len(parts) >= 2 else "", ro)

    _mts = [_mt(v) for v in _vols]
    ck("★ ①k1 compose 根挂载 = 新根 1:1 且**只读**（代码不再取自这里）",
       (_newroot, _newroot, True) in _mts, repr(_mts))
    ck("★ ①k2 运行态卷挂在 /state（`./data/drive-loop/state:/state`，可写）",
       any(t == "/state" and not ro and s.endswith("data/drive-loop/state")
           for (s, t, ro) in _mts), repr(_mts))
    ck("★ ①k3 媒体卷 1:1 整卷（§9：硬链接不能跨卷）",
       ("/volume1/video", "/volume1/video", False) in _mts, repr(_mts))
    ck("★★ ①k4 且**没有把 environment 混进 volumes**（Docker 两种长列表形态相同，"
       "写成一样的缩进时 yaml 不会替你分开）",
       all("=" not in str(v) for v in _vols), repr([str(v) for v in _vols]))
    ck("★★ ①k5 `/etc/timezone` **不在**挂载里 —— 实测 NAS 上没这个文件，"
       "Docker 会建个空目录顶上（纯负担）",
       not any("timezone" in str(v) for v in _vols), repr([str(v) for v in _vols]))
    #   ★ 整卷/整目录：挂成子目录会静默退化（`first_existing()` 返回 None + 一行 warning）
    _vol_s = " ".join(str(v) for v in _vols)
    ck("★ ①l 挂的是整个 compose 根 + 整个媒体卷（不是挑子目录）",
       _newroot in _vol_s and "/volume1/video" in _vol_s, _vol_s)
    #   ★★ ①l3 三条路径环境变量必须都在 environment 里（代码入镜像的接线，缺一则静默退化）
    #   ★★ ①l2 DRIVE_LIMIT 在 environment 里（覆盖口，不是挂载）；
    #   ★★ 2026-10-05「去掉包的闸门」：`DRIVE_PACKS` **已随名单一起退休** ⇒ 断言反转成
    #      「**不再**出现 DRIVE_PACKS」（防止有人把它加回来当闸门）。
    _env_s = " ".join(str(v) for v in _envs)
    ck("★★ ①l2 DRIVE_LIMIT 在 environment 里（覆盖口，不是挂载）",
       "DRIVE_LIMIT" in _env_s, _env_s)
    ck("★★ ①l2b DRIVE_PACKS **不在** environment 里（名单已退休，别加回来）",
       "DRIVE_PACKS" not in _env_s, _env_s)
    ck("★★ ①l3 DRIVE_STATE_DIR / COMPOSE_DIR / RESEED_STATE_DB 三接线都在 environment 里",
       "DRIVE_STATE_DIR=/state" in _env_s and f"COMPOSE_DIR={_newroot}" in _env_s
       and "RESEED_STATE_DB=" in _env_s, _env_s)

    # ---- ①m 容器方言：PY / SUDO ----------------------------------------
    _env = dl.get("environment") or []
    if isinstance(_env, dict):
        _env = [f"{k}={v}" for k, v in _env.items()]
    _env_s = " ".join(str(e) for e in _env)
    ck("★ ①m environment 给了容器方言 PY / SUDO（并把 TZ 透进去）",
       "PY=python" in _env_s and "SUDO=" in _env_s and "TZ=" in _env_s, _env_s)

    # ---- ①n ★ 不加 user:（与另三个服务不同，这是刻意的）------------------
    #   宿主那份由 DSM 以 root 跑 ⇒ 状态文件属主是 root。容器若切成
    #   ${PUID}:${PGID}，同一个文件两个属主换来换去，必然有一边写不进去。
    ck("★ ①n drive-loop **不加** user:（与宿主那份同为 root，否则属主来回换）",
       "user" not in dl, repr(dl.get("user")))

    # ---- ①o/①p ★ healthcheck（2026-09-17 加；本仓第一个 healthcheck）---------
    #   判据是**心跳新鲜度**：15.a 把 `heartbeat_ts` 补上之后它才存在。
    _hc = dl.get("healthcheck")
    ck("★ ①o drive-loop 有 healthcheck（卡死接管的**判据**；没有它 autoheal 无从判）",
       isinstance(_hc, dict) and _hc.get("test"), repr(_hc))
    if isinstance(_hc, dict):
        _t = _hc.get("test")
        _t_s = " ".join(_t) if isinstance(_t, list) else str(_t)
        #   ★★★ 判据本体：读 `.drive-loop.state` 的 heartbeat_ts。
        #   ★★ 2026-09-18 改正：原先这里**写死 `"900" in _t_s`** —— 那是在钉一个
        #      **具体的数**，而真正该钉的是**性质**：「阈值必须 > 一轮睡眠」。
        #      写死那个数让这条断言在 900→1800 的修复里**红了**，而那次改动
        #      **恰恰是这条断言该支持的**（它是"阈值太短会误杀"的解药）。
        #      ⇒ 钉性质而不是钉字面量：读出来，再比下界。
        ck("★★ ①p healthcheck 判的是**心跳新鲜度**（读 .drive-loop.state 的 heartbeat_ts）",
           "heartbeat_ts" in _t_s and ".drive-loop.state" in _t_s,
           _t_s[:160])
        _hm = re.search(r"time\.time\(\)-hb<=(\d+)", _t_s)
        ck("★★ ①p′ 阈值存在且 **≥ 1800s**（> 一轮睡眠 45 分钟的那一档，留余量）"
           "—— 太短 ⇒ 健康的容器在睡眠期被判 unhealthy ⇒ 每轮一封「意外停止」邮件",
           bool(_hm) and int(_hm.group(1)) >= 1800,
           f"抠到 {_hm.group(1) if _hm else None!r}；正文={_t_s[:120]}")
        #   ★★ `start_period` 必须有 —— 没有它，容器刚起来（状态文件还没写）
        #      就会判 unhealthy ⇒ 被 autoheal **反复重启**。这是**必须**的一条。
        ck("★★ ①q healthcheck 有 start_period（没有它：刚起来就判 unhealthy ⇒ 反复重启）",
           bool(_hc.get("start_period")), repr(_hc.get("start_period")))
        #   ★ 用 python 读 JSON，不用 sed/grep 抠（本仓踩过 sed 抠 JSON 静默抠错）
        ck("★ ①r healthcheck 用 python 读 JSON（不用 sed/grep 抠 —— 那会静默抠错）",
           "python" in _t_s, _t_s[:120])

    # ---- ①s ★ label：autoheal 的**唯一作用域开关** ---------------------------
    _lbl = dl.get("labels")
    if isinstance(_lbl, dict):
        _lbl_s = " ".join(f"{k}={v}" for k, v in _lbl.items())
    elif isinstance(_lbl, list):
        _lbl_s = " ".join(str(x) for x in _lbl)
    else:
        _lbl_s = ""
    ck("★★ ①s drive-loop 带 label autoheal=true（没它 ⇒ 侧车**不会**管这个容器）",
       "autoheal=true" in _lbl_s, repr(_lbl))

# ==========================================================================
print("\n=== ② compose：profiles 的**真语义**（有 docker 就真跑）===")
# ==========================================================================
# ★ ①b 只断言「YAML 里有那个键」。它**证明不了** compose 真会把它排除在默认之外 ——
#   而"这条锁到底锁没锁上"恰恰是本改动最要紧的问题。
#   ⇒ 有 docker 时**真跑一遍** `config --services` 两个方向都看。
#   ★ 没有 docker 时**必须是"未验"，不是"通过"**（B.10 第 14 条：
#     空集/拿不到 都不许读成"没事"）—— 所以这里打印的是一条 `--` 行，
#     它**不计入断言数**，但把"这条没验"写在脸上。
_DOCKER = None
try:
    _r = subprocess.run(["docker", "compose", "version"],
                        capture_output=True, text=True, timeout=30)
    if _r.returncode == 0:
        _DOCKER = True
except Exception:                                # noqa: BLE001
    _DOCKER = False

if _DOCKER:
    def _svc_names(*extra):
        r = subprocess.run(["docker", "compose", "-f", str(COMPOSE), *extra,
                            "config", "--services"],
                           capture_output=True, text=True, timeout=120)
        return r.returncode, set((r.stdout or "").split())

    _rc0, _default = _svc_names()
    _rc1, _profiled = _svc_names("--profile", "drive-loop")
    ck("②a docker compose 解析得动这份 compose", _rc0 == 0 and _rc1 == 0,
       repr((_rc0, _rc1)))
    #   ★★ 这条是 profiles 锁的**真判据**：默认集里**必须没有** drive-loop。
    ck("★★ ②b 默认（不加 --profile）**不含** drive-loop —— 这就是那把锁",
       "drive-loop" not in _default, repr(sorted(_default)))
    ck("★★ ②c 加 --profile drive-loop 后**才**出现（显式启用才生效）",
       "drive-loop" in _profiled, repr(sorted(_profiled)))
    ck("★ ②d 默认集仍含其余四个（锁只锁住了 drive-loop/autoheal，没误伤）",
       {"prowlarr", "flaresolverr", "cross-seed", "reseed-orchestrator"}
       <= _default, repr(sorted(_default)))
    #   ★★ 2026-09-17：**两个**都必须不在默认集里（`autoheal` 与 `drive-loop` 同 profile）
    ck("★★ ②e 默认集里 **autoheal 也不在**（它俩同 profile ⇒ 同生同死）",
       "autoheal" not in _default, repr(sorted(_default)))
    ck("★★ ②f 加 --profile 后 **autoheal 也在**（与 drive-loop 一起被显式启用）",
       "autoheal" in _profiled, repr(sorted(_profiled)))
    #   ★★ 2026-09-29：`watcher`（容器巡检拉起件）同 profile ⇒ 同样两个方向都要钉。
    ck("★★ ②g 默认集里 **watcher 也不在**（它也是 drive-loop 的侧车）",
       "watcher" not in _default, repr(sorted(_default)))
    ck("★★ ②h 加 --profile 后 **watcher 也在**",
       "watcher" in _profiled, repr(sorted(_profiled)))
else:
    print("  --   本机没有可用的 docker ⇒ ② 段未验（**不是通过**）。")
    print("  --   要补验：在有 docker 的机器上跑 "
          "`docker compose config --services`，默认集里应当**没有** drive-loop。")

# ==========================================================================
print("\n=== ②w watcher（容器巡检拉起件）：安全性质 ===")
# ==========================================================================
# ★★ 这一段钉的是**本改动最大的新增风险**（第 2 个挂 docker.sock 的容器）。
#    每一条都对应一个"错了会静默出事"的形状，理由写在各自下面。
_w = svcs.get("watcher")
ck("②w-1 watcher 服务存在", _w is not None, repr(sorted(svcs)))
if _w:
    #   ★★★ `restart: always` —— 这是**关键的一条，且与业务容器相反**。
    #     业务容器用 `unless-stopped`（要能 `docker stop` 停住）；
    #     而 watcher 的职责是"`dockerd` 重启后把别人拉回来"⇒ 它**自己必须先回来**
    #     ⇒ 必须 `always`。★ 用 `unless-stopped` 的话，它**盖不住自己**：
    #     `dockerd` 重启后它不会自己起，而"它没起"这件事**没有任何读数会报**。
    ck("★★★ ②w-2 watcher 用 restart: always（**不是** unless-stopped）—— "
       "否则它自己盖不住 dockerd 重启，而那正是它存在的理由",
       _w.get("restart") == "always", repr(_w.get("restart")))
    #   ★★ socket 必须只读挂载（与 autoheal 同规格）。可写挂载是**多余**的权限。
    _vols = [str(v) for v in (_w.get("volumes") or [])]
    _sock = [v for v in _vols if "docker.sock" in v]
    ck("★★ ②w-3 watcher 挂 docker.sock", bool(_sock), repr(_vols))
    ck("★★ ②w-4 socket 挂成 **:ro**（给了写权限 = 白送 root 等价，没必要）",
       bool(_sock) and all(v.endswith(":ro") for v in _sock), repr(_sock))
    #   ★★ 用 glibc 基础镜像（python:3.12-slim），**不是 alpine**。
    #     实测：群晖的 docker/compose 是 glibc 动态链接，alpine(musl) 里跑会
    #     `no such file or directory`（缺 loader）。这条一错 = 容器起不来。
    ck("★★ ②w-5 用 glibc 基础镜像（python:3.12-slim）—— alpine 跑不了宿主的 docker",
       _w.get("image") == "python:3.12-slim", repr(_w.get("image")))
    #   ★★ compose 目录挂**同路径**：因为干活的是**宿主 daemon**，
    #     而 compose 文件里是宿主路径 ⇒ 换路径会让它解析出错误挂载。
    ck("★★ ②w-6 compose 目录挂载是**同路径**（左边 == 右边）",
       any(v.split(":")[0] == v.split(":")[1]
           for v in _vols if "/volume2/" in v), repr(_vols))
    #   ★ 宿主 docker + compose 插件都挂进来了（镜像里没有它们）。
    ck("★ ②w-7 宿主 docker 与 compose 插件都从宿主挂进来（镜像里没有）",
       sum(1 for v in _vols if "ContainerManager" in v) >= 2, repr(_vols))
    #   ★ 别写 build:（NAS 构建上下文不同，本仓踩过）
    ck("★ ②w-8 watcher 不写 build:", "build" not in _w, repr(_w.get("build")))
    #   ★ 入口是那个循环脚本
    _cmd = _w.get("command")
    _cmd_s = " ".join(_cmd) if isinstance(_cmd, list) else str(_cmd or "")
    ck("★ ②w-9 入口是 watch-containers-loop.sh（不是一次性 watch-containers.sh）",
       "watch-containers-loop.sh" in _cmd_s, repr(_cmd_s))

# ==========================================================================
print("\n=== ③ 常驻入口脚本：命令行区段 ===")
# ==========================================================================
# ★★ 判据的取法很要紧：`--once` **必须**在注释里出现（要解释为什么不要它），
#    所以「grep 全文」那种写法**恒红**、改一改就会退化成一个被删掉的断言。
#    ⇒ 只看**参数区段**：从 `RC=0` 那一行到 `exit "$RC"`。
_SH = RESIDENT.read_text(encoding="utf-8") if RESIDENT.is_file() else ""
#   ★★★ 取「参数区段」**故意不用正则** —— 我在这一处**连错三次**，
#      三次都是同一个原因：模式里要匹配的是 shell 的 `$RC`（一个**字面美元号**），
#      而它在正则里有第二种含义（行尾锚），得写 `\$` 才是字面量；
#      我写的却是裸 `$` ⇒ **恒不匹配** ⇒ 区段取不到 ⇒ ③ 段全假红。
#      ★ 而"红"的位置在 ③bcd（说 --url 不见了），与真因（正则写错）**隔着两层**。
#      ⇒ 教训与 `tests/README.md` 那条同族：**报红时先怀疑断言本身**。
#        而更实用的一步是：**别让判据需要转义** —— 按**行**取，不做模式匹配。
#      ⇒ 于是这里用最笨也最不会错的办法：找 `RC=0` 那一行的下标，
#        再找它之后第一个 `exit ` 开头的行。
_lines_sh = _SH.splitlines()
_i_start = next((i for i, l in enumerate(_lines_sh) if l.rstrip() == "RC=0"), None)
#   ★★ 边界取「**第一条空行**」——不是 `exit` 那一行。
#      我在这里又错了一次：区段取到 `exit "$RC"` 那行会**顺带收进**
#      `echo "... exit=$RC" >> "$ATTEMPTS"`（207 行）⇒ ③e 判"最后一行参数是不是
#      `"$@"`"时，读到的是那行 echo ⇒ **假红**，而脚本是对的。
#      ⇒ 参数块的结构是「`RC=0` / 若干续行 / 空行 / 收尾」。**空行就是边界。**
#      这条比"找 exit 行"稳：它不依赖后面有多少注释、注释里写了什么。
_i_blank = next((i for i, l in enumerate(_lines_sh)
                 if i > (_i_start or 0) and not l.strip()), None)
_ARGV = ("\n".join(_lines_sh[_i_start + 1:_i_blank])
         if _i_start is not None and _i_blank is not None else "")

ck("前提：找得到参数区段（`RC=0` 行 … 其后第一条空行）—— 找不到就该红",
   _i_start is not None and _i_blank is not None,
   f"start={_i_start} blank={_i_blank}（取不到 ⇒ ③ 段会变成空转）")
if _i_start is None or _i_blank is None:
    print("  FAIL 参数区段取不到 ⇒ ③ 段无法继续（下面几条**不是**代码的问题）")
    sys.exit(1)

ck("★★ ③a 参数区段里**没有** --once（带上 = 跑一批就退出，常驻变轮询）",
   "--once" not in _ARGV, _ARGV.strip())
ck("★★ ③b 参数区段里有 --url 与 --qbit-url（容器内必须服务名直连；"
   "群晖上『容器→宿主 LAN IP→DNAT』timeout）",
   "--url" in _ARGV and "--qbit-url" in _ARGV, _ARGV.strip())
ck("③c 两个 URL 是**服务名**，不是 ${NAS_IP} / 局域网 IP",
   # ★ 2026-10-06：保种实例改名/多实例，旧 qbittorrent-reseed:3060 → qbit-reseed1:3320。
   "http://cross-seed:2468" in _ARGV and "http://qbit-reseed1:3320" in _ARGV,
   _ARGV.strip())
ck("③d 带 --env \"$COMPOSE_DIR/.env\"（TORZNAB 那几个 apikey 的来源）",
   '--env "$COMPOSE_DIR/.env"' in _ARGV, _ARGV.strip())
#   ★ 同样别用正则抠 `"$@"`（`$` 在正则里有含义，写成 `\$` 又会变字面反斜杠）。
#     ✓ 和 ✗ 的差别就在**最后一行参数**上，用字符串尾判最直白也最难写错。
_ARGV_LAST = [l for l in _ARGV.splitlines() if l.strip() and not l.strip().startswith("#")]
ck('③e "$@" 放最后（用户给的参数覆盖脚本里定的）—— 判据是**最后一行参数**',
   bool(_ARGV_LAST) and _ARGV_LAST[-1].strip().startswith('"$@"'),
   repr(_ARGV_LAST[-1].strip() if _ARGV_LAST else None))

# ---- ③f ★★ 名单两个声明点必须逐字一致 -------------------------------------
#   本文件是 `--indexers` 的**第二个**声明点。加了站却漏改一处 ⇒ 新站永远搜不到，
#   而 Prowlarr / cross-seed 那边一切正常（2026-09-12 就是这么卡住 224 部的）。
#   ⇒ 这是本文件里**唯一**能抓住那种漂移的断言。
#   ★★ 取名单必须**按行**取脚本里**真正的那个参数行**，不能全文搜 ——
#      两个文件里都有**注释**在讲 `--indexers`（本文件的注释里还引用了这句话），
#      全文搜会**抓到注释里的字**，于是这条断言永远绿、什么也没钉住。
#      （第一次跑就是这么绿的：resident 侧抓到了注释里的「手工维护」。）
def _argline(path: pathlib.Path, flag: str):
    if not path.is_file():
        return None
    for ln in path.read_text(encoding="utf-8").splitlines():
        s = ln.strip()
        if s.startswith("#"):
            continue
        if s.startswith(flag + " "):
            return s[len(flag):].strip().rstrip(" \\").strip()
    return None


_rd_s = _argline(RESIDENT, "--indexers")
_nd_s = _argline(NAS_SH, "--indexers")
ck("前提：两份入口都找得到 --indexers 名单", _rd_s is not None and _nd_s is not None,
   repr((_rd_s, _nd_s)))
ck("★★ ③f --indexers 与 drive-loop-nas.sh（生产在跑的那份）**逐字相同**"
   "（两个声明点，漏改一处 = 新站永远搜不到）",
   _rd_s is not None and _rd_s == _nd_s,
   f"resident={_rd_s!r} nas={_nd_s!r}")

_rl_s = _argline(RESIDENT, "--limit")
_nl_s = _argline(NAS_SH, "--limit")
#   ★★★ 2026-09-22 **这条判据被推翻并改写**（用户拍「调大 --limit」）——
#     原文是「`--limit` 与 run.sh **逐字相同**，两个入口的批量口径不许分叉」。
#     ★ 它错在**把"同一个值"当成"同一个口径"**：
#       · `run-resident.sh` 带 `--pool` ⇒ `--limit` 是**全局**（一池 565 部）
#       · `drive-loop-nas.sh` **不带** `--pool` ⇒ `--limit` 是**每包**（一部 232 部）
#       ⇒ 两个数**本来就不该相等**（50 vs 50 只是"碰巧一样"，不是"一致性"）。
#     ★ 真正的不变量是**各自内部自洽** + **分叉是显式且被记录的**（同 ③l 的 `--pool`）。
#       所以这里改成三条：
#         ① resident 的值必须是"可被 env 覆盖的数字"（`${DRIVE_LIMIT:-N}` 形态）；
#         ② N（默认值）必须**大于** nas 那份 —— 合池下的全局额度若 ≤ 每包额度，
#            那等于**没开池还倒亏**（一批连一个包的量都吃不下）；
#         ③ 两份入口**都**必须是显式数字（不许出现"没写 --limit 靠默认值"）。
#     ★ 为什么不用"resident 必须 > 池子欠账 565"：那个数**会变**，写死等于把
#       一次读数焊成判据（`B.10`：理由有保质期）。这里只钉**方向**（严格大于）。
_m = re.search(r"\$\{DRIVE_LIMIT:-(\d+)\}", _rl_s or "")
ck("★★ ③g1 resident 的 --limit 是「可被 DRIVE_LIMIT 覆盖的数字」形态"
   "（同 --packs 的覆盖口，改额度不必改代码）",
   _m is not None, f"resident={_rl_s!r}")
ck("★★ ③g2 nas 那份 --limit 是显式数字（不许靠默认值 —— 那是「看着生效」的坑）",
   _nl_s is not None and _nl_s.isdigit(), f"nas={_nl_s!r}")
if _m and _nl_s and _nl_s.isdigit():
    _res_n, _nas_n = int(_m.group(1)), int(_nl_s)
    ck("★★★ ③g3 合池那份的额度**严格大于**每包那份 —— 否则合池等于白开"
       "（全局 50 ≤ 每包 50 ⇒ 一批连一个包的量都吃不下）",
       _res_n > _nas_n, f"resident={_res_n} nas={_nas_n}")
    #   ★★ 阴性对照 —— 用 `ck_no`（**期望为假**），不是 `ck`。
    #     被我验的那个表达式就是下面 `a > b`：喂"相等"和"更小"都必须说 no。
    #     ★ **教训**：把表达式喂进去时，"期望为假"要用对入口；
    #       写成 `ck(name, (100 > 100), False)` 是**把期望值塞进了 extra**，
    #       于是"判据工作正常"被读成"断言失败" —— 顺手把阴性对照废掉。
    ck_no("★ ③g4 阴性对照：同一比较喂「相等」的一组 → 判据说 no（不是恒真）",
          (100 > 100), "100 > 100")
    ck_no("★ ③g4b 阴性对照 ②：喂「resident 更小」→ 也说 no",
          (30 > 50), "30 > 50")
    #   ★ 阳性对照：把**判据本身**再喂一组真例，证明它**同时**能说 yes。
    #     只有这四条一起，"③g3 是活的判据"才算立住（否则可能是个恒假的式子）。
    ck("★ ③g4c 阳性对照：喂一组「合池更大」→ 说 yes",
       (500 > 50), "500 > 50")
else:
    ck("★★ ③g3/g4 前提不成立（上面两条已经报红）", False, True)

# ---- ③h 留痕与前置闸门 ----------------------------------------------------
ck("③h attempts.log 的行带 [resident] 标记（两条路共用这个文件，"
   "标记是唯一能分清谁写的办法）",
   "[resident]" in _SH, "没有 [resident]")
ck("★ ③i 有前置闸门：读 .drive-loop.state 并能在「另一条路在跑」时退出"
   "（§26.5 前提 ② 的兜底）",
   ".drive-loop.state" in _SH and re.search(r"exit\s+3\b", _SH) is not None,
   "没有读状态 / 没有非零退出")
ck("★ ③j 闸门**不读 pid**，读的是 mode（跨 pid 命名空间判 pid 是假信号）",
   '"mode"' in _SH or "'mode'" in _SH, "没引用 mode")

# ---- ③k ★★ `--pool` 已**退休**（2026-10-05「去掉包的闸门」，用户拍）----
#   ★★ 判据**反转**：resident **不许**带 `--pool`，也**不许**带 `--packs`。
#     池路径现在是**唯一路径**（drive-loop.py 永远扫 `pack` 表），`--pool` 参数已删；
#     两份入口都不该再有它。少了这条反向钉子，有人把 `--pool` 加回来
#     （对着已删的参数）会**静默失效**（argparse 直接报错或参数无效）——
#     正是"参数看着生效、其实没生效"的形状（`A.11`）。
ck("★★ ③k run-resident.sh **不带** `--pool`（参数已删，池是唯一路径）",
   _argline(RESIDENT, "--pool") is None,
   "resident 的参数行上还有 --pool —— 该参数已退休")
ck("★★ ③k2 run-resident.sh **不带** `--packs`（名单已退休）",
   _argline(RESIDENT, "--packs") is None,
   "resident 的参数行上出现了 --packs —— 名单已退休，别加回来")

ck("★★ ③l drive-loop-nas.sh（run.sh）也**不带** `--pool` / `--packs`",
   _argline(NAS_SH, "--pool") is None and _argline(NAS_SH, "--packs") is None,
   "run.sh 上出现了 --pool / --packs —— 两者都已退休")

#   ★ ③m：drive-loop.py 的 argparse **不再**定义 `--pool` / `--packs` / `--batch`
#     （③k 钉入口脚本，③m 钉源头 —— 两处都钉，改一处会被另一处抓住）。
#   ★ 用 DOCKER_PY（上面已定义成 pathlib 路径）读源码；两种引号写法都查
#     （`add_argument("--x")` 与 `add_argument('--x')`），避免漏掉单引号那种。
_dl_src = DOCKER_PY.read_text(encoding="utf-8") if DOCKER_PY.is_file() else ""
for _flag in ("--pool", "--packs", "--batch"):
    _dq = 'add_argument("%s"' % _flag
    _sq = "add_argument('%s'" % _flag
    _present = (_dq in _dl_src) or (_sq in _dl_src)
    ck("★ ③m drive-loop.py 的 argparse 不再有 " + _flag, not _present,
       "argparse 里还定义着 " + _flag + " —— 该参数已退休")

# ==========================================================================
print("\n=== ④ 宿主入口那份**没被改**（生产在跑，不该动）===")
# ==========================================================================
#   ★ 为什么值得单独钉：常驻入口是**照抄**它的形状。以后有人回头"顺手统一一下"
#     就会把 `--once` 从 run.sh 删掉 —— 而 DSM 任务调的就是它，
#     删了之后那条任务会变成"每次唤醒起一个永不退出的进程"。
_NSH = NAS_SH.read_text(encoding="utf-8") if NAS_SH.is_file() else ""
ck("★★ ④a drive-loop-nas.sh（生产那份）**仍然带 --once** —— 它是 DSM 一次性调用",
   re.search(r"^\s+--once\s*\\?$", _NSH, re.M) is not None,
   "run.sh 的 --once 不见了")
#   ★ 同样只看**参数行**：`run.sh` 的注释里**必须**能有 `--url`（它要解释
#     "刻意不写这两个参数"）⇒ 全文搜恒红。
ck("★ ④b drive-loop-nas.sh **仍然不传** --url / --qbit-url（宿主上默认值就对，"
   "重复写一份只会多一个漂移点）",
   _argline(NAS_SH, "--url") is None and _argline(NAS_SH, "--qbit-url") is None
   and "--url" in _NSH,
   "run.sh 的参数行上出现了 --url，或注释里那句解释被删掉了")

# ==========================================================================
print("\n=== ⑤ drive-loop.py：常驻分支真的写心跳（15.a 回归）===")
# ==========================================================================
#   ★★ 这是**唯一一条真能变红**的断言：把常驻分支改回去（把 `_resident_state`
#      的调用与 `with Heartbeat()` 删掉）当场红。
#   ★ 用 AST 而不是 grep：grep 会被注释里的字样骗到（本改动**就是**一大段注释）。
#     但 AST 也给不出"在循环里"这件事 ⇒ 两条都要：
#       ⑤a/⑤b：AST 断言 `main()` 里**存在**这些调用（真判据，能红）
#       ⑤c   ：AST 断言含 `while` 循环（否则整段搬出循环也没人发现）
_SRC = DOCKER_PY.read_text(encoding="utf-8") if DOCKER_PY.is_file() else ""
_TREE = None
try:
    _TREE = ast.parse(_SRC)
except SyntaxError as e:
    ck("前提：drive-loop.py 能被 AST 解析", False, str(e))

_main = None
if _TREE is not None:
    for _n in ast.walk(_TREE):
        if isinstance(_n, ast.FunctionDef) and _n.name == "main":
            _main = _n
            break
ck("前提：找得到 `def main()`", _main is not None, "没找到 main()")

if _main is not None:
    #   ★★ 第一版这里写错过：`main.body` 里嵌套的调用被 `ast.Expr` **包着**，
    #      只扫 `isinstance(node, ast.Call)` 会一条都扫不到 ⇒ **假红** ——
    #      它报"前导段没写状态"，而代码是对的。
    #      ⇒ 用 `ast.walk` 递归，别自己写遍历。
    _calls, _withs = [], []
    for _node in ast.walk(_main):
        if isinstance(_node, ast.Call):
            _f = _node.func
            _nm = getattr(_f, "id", None) or getattr(_f, "attr", None)
            if _nm:
                _calls.append(_nm)
        if isinstance(_node, ast.With):
            for _it in _node.items:
                _f = _it.context_expr
                #   ★★ 又一次同样的错，这次在 ⑤b：`with Heartbeat():` 的
                #      `context_expr` 是一个 **`ast.Call`**，名字在 **`Call.func`** 上，
                #      不在 Call 自己身上 ⇒ `getattr(call, "id")` 恒为 None
                #      ⇒ `_withs` 恒为空 ⇒ **假红**（报"没有 with Heartbeat()"，
                #      而代码第 2587 行就是它）。
                #      ⇒ 与前面两次同族：**判据写错的样子和代码写错长得一样**。
                #        统一走"先解 Call.func"这一条路。
                if isinstance(_f, ast.Call):
                    _f = _f.func
                _nm = getattr(_f, "id", None) or getattr(_f, "attr", None)
                if _nm:
                    _withs.append(_nm)

    ck("★★ ⑤a 常驻分支调了写状态的东西（`write_state` 或 `_resident_state`）"
       "—— 这是 15.a 的**回归判据**：原缺陷是这一整段不存在",
       "write_state" in _calls or "_resident_state" in _calls,
       repr(sorted(set(_calls))[:12]))
    ck("★★ ⑤b 常驻分支有 `with Heartbeat()` 包着跑批 —— 批次里有长时间不发请求的"
       "阶段（等日志静默/回灌），靠「每发一条 webhook 刷一次」会漏",
       "Heartbeat" in _withs, repr(_withs))
    #   ★ 只断言"存在 while"**不够**（两个 Helper 都可能被搬出循环），
    #     但它能挡住"整段搬走"。真正的"在循环里"靠 ⑤a 的调用点分布兜 ——
    #     那需要在嵌套层级上判，而那种断言一改缩进就红（噪声 > 价值）。
    ck("★ ⑤c 常驻分支仍有 while 循环（判据是「常驻」，不是「跑一遍」）",
       any(isinstance(_n, ast.While) for _n in ast.walk(_main)), "main() 里没有 while")
    #   ★★ 心跳的**收尾**也必须有：只写不清 ⇒ 残留心跳会让下一轮误判
    #      「上一批还在跑」。判据是"存在把 running_pid 置 None 的那次写"。
    _none_writes = 0
    for _n in ast.walk(_main):
        if isinstance(_n, ast.Call) and (
                getattr(_n.func, "id", None) or getattr(_n.func, "attr", None)
        ) == "write_state":
            for _a in _n.args:
                if isinstance(_a, ast.Dict):
                    for _k, _v in zip(_a.keys, _a.values):
                        if (isinstance(_k, ast.Constant) and _k.value == "running_pid"
                                and isinstance(_v, ast.Constant) and _v.value is None):
                            _none_writes += 1
    ck("★★ ⑤d 收尾把 running_pid 置 None（残留心跳会让下一轮误判「上一批还在跑」）",
       _none_writes >= 1, f"找到 {_none_writes} 处")

# --------------------------------------------------------------------------
#   ⑤e~⑤h ★★★ 2026-09-18：`phase` —— healthcheck 误杀那次的回归
#
#   背景（实测，不是推理）：`Heartbeat` 原本**只包住跑批**，睡眠期（日志
#   「等待 45.0 分钟后跑下一批」）心跳**冻结** ⇒ 45 分钟 ≫ healthcheck 阈值 900s
#   ⇒ **每一轮**判 unhealthy ⇒ autoheal 杀容器 ⇒ 群晖每 41 分钟一封
#   「drive-loop 意外停止」邮件。
#
#   ⇒ 修法：**睡眠期也刷心跳**（`Heartbeat(phase=PHASE_IDLE)` 包住 `time.sleep`），
#     同时给 `batch_alive()` 一个**新主判据 `phase`** —— 因为心跳一旦全时新鲜，
#     「心跳新鲜」就**不再能**区分「在跑」与「在睡」了：
#     `batch_alive()` 若继续只信心跳，会**永远返回 True** ⇒ 每轮跳过
#     ⇒ **静默永久停工**（正是 `Heartbeat` docstring 最怕的那个失败模式）。
#
#   ★★ 这一组**必须 import 真 `batch_alive()` 喂数据**，不能只看源码字符串 ——
#     因为要钉的是**四种输入下的行为**（含"旧格式仍走原逻辑"），
#     而 AST/字符串判据证不了行为。
# --------------------------------------------------------------------------
print("\n=== ⑤e `batch_alive()` 的 `phase` 四输入（2026-09-18 回归）===")
#   ★ 载真的那份：照 `tests/README.md` 的规矩，**先**登记 `sys.modules` 再 exec
#     （不登记的话 `@dataclass` 会炸一个看着毫不相干的 AttributeError）。
import importlib.util                             # noqa: E402
import time as _time                              # noqa: E402

_DL = None
try:
    _spec = importlib.util.spec_from_file_location("drive_loop_for_batch_alive", DOCKER_PY)
    _DL = importlib.util.module_from_spec(_spec)
    sys.modules["drive_loop_for_batch_alive"] = _DL      # ★ 必须在 exec_module 之前
    import contextlib as _ctx
    import io as _io
    with _ctx.redirect_stdout(_io.StringIO()), _ctx.redirect_stderr(_io.StringIO()):
        try:
            _spec.loader.exec_module(_DL)
        except SystemExit:
            pass
except Exception as e:                             # noqa: BLE001
    ck("前提：能载入 scripts/drive-loop.py 的 batch_alive", False, repr(e))

if _DL is not None:
    _ba = _DL.batch_alive
    _now = _time.time()

    def _st(**kw):
        """容器版状态的最小形状：**必须带 `running_pid_pidns="container"`**，
        否则会走宿主分支（那条路 `pid_alive` 一掺进来就测不准了）。"""
        base = {"running_pid": 1, "running_pid_pidns": "container",
                "heartbeat_ts": _now}
        base.update(kw)
        return base

    # ⑤e-1 在跑 + 心跳新鲜 ⇒ True（守卫：别把正常在跑的判成可接管）
    ck("★ ⑤e 相位 running + 心跳新鲜 ⇒ True（在跑，不许抢）",
       _ba(_st(phase="running")) is True, "被判成可接管了")

    # ⑤e-2 ★★ 空闲 + 心跳**新鲜** ⇒ False —— 这是**阴性对照**：
    #   若返回 True，说明 `phase` 压根没被读，心跳仍是唯一判据
    #   ⇒ 「睡眠期心跳全时刷」一上线就会**永远判在跑** ⇒ **静默永久停工**。
    ck("★★ ⑤f 相位 idle + 心跳**新鲜** ⇒ False（★ 阴性对照：证明 `phase` 真的被读了）",
       _ba(_st(phase="idle")) is False,
       "返回 True ⇒ `phase` 没接上，睡眠期刷心跳会让它**永远判在跑**")

    # ⑤e-3 ★ 在跑但**心跳陈旧** ⇒ False —— 「卡死」必须仍可检测
    #   （卡死正是当初加 autoheal 的理由；若这里返回 True，等于把自愈能力关掉了）
    ck("★ ⑤g 相位 running + 心跳**陈旧**(>600s) ⇒ False（卡死仍可检测）",
       _ba(_st(phase="running", heartbeat_ts=_now - 9999)) is False,
       "返回 True ⇒ 卡死检测被关掉了，autoheal 从此救不了卡死的容器")

    # ⑤e-4 ★★ 旧格式（**没有** `phase`）⇒ 原逻辑**逐字保留**：只信心跳
    #   升级窗口里状态文件是旧版本写的，那时「只信心跳」是对的
    ck("★★ ⑤h 无 `phase`（旧格式）+ 心跳新鲜 ⇒ True（升级窗口行为不变）",
       _ba(_st()) is True, "旧格式行为被改动 ⇒ 升级窗口会误接管")
    ck("★ ⑤h 无 `phase`（旧格式）+ 心跳陈旧 ⇒ False（原逻辑保留）",
       _ba(_st(heartbeat_ts=_now - 9999)) is False, "旧格式的残留判定丢了")

# ⑤i ★ 两个常量的**相对关系**必须对：healthcheck 阈值 > 一轮睡眠
#   （阈值写在 compose 的 python 一行式里，这里用字符串抠出来比）
_HE_TXT = COMPOSE.read_text(encoding="utf-8") if COMPOSE.is_file() else ""
_m = re.search(r"time\.time\(\)-hb<=(\d+)", _HE_TXT)
_he_thresh = int(_m.group(1)) if _m else 0
ck("★ ⑤i compose 的 healthcheck 阈值已从 900 抬到 1800（不再是 15 分钟）",
   _he_thresh == 1800, f"读到 {_he_thresh!r}")
ck("★★ ⑤i 阈值 > 睡眠上限（45 分钟 = 2700s 那一档要留余量 ⇒ 至少 1800）",
   _he_thresh >= 1800, f"阈值 {_he_thresh}s 太短，睡眠期仍会被误杀")
# ⑤j ★ 睡眠点**真的**被 `Heartbeat(phase=PHASE_IDLE)` 包住了
#   （否则"改了常量没改代码"——治标那半没做）
ck("★★ ⑤j 常驻主循环的睡眠点被 `Heartbeat(phase=PHASE_IDLE)` 包住（治本那半）",
   "with Heartbeat(phase=PHASE_IDLE):" in _SRC,
   "睡眠期没包心跳 ⇒ 阈值抬再高也只是治标")

# --------------------------------------------------------------------------
print("\n=== ⑤k `write_state()` 原子写（2026-09-18 加；#27 把它的暴露面放大了）===")
#   ★ 为什么单列一段：裸 `write_text` 的那个窗口**本来就在**，但 `#27` 把心跳改成
#     「睡眠期也刷」之后，写频率从「跑批期」扩到「**全时**」⇒ 暴露面变大。
#     读者是**容器外**的 healthcheck（`json.load(open(...))`）⇒ 读到半截 JSON
#     ⇒ 解析抛 ⇒ 判 unhealthy ⇒ autoheal 杀 ⇒ 又是一封「意外停止」邮件。
#   ★★ 判据落到**源码**上（不是行为上）：因为原子性是 "truncate 与写完之间" 的
#     性质，**离线测不出时序** —— 能测的是「写法是不是那条唯一原子的写法」。
if _DL is not None:
    import tempfile as _tf                            # noqa: E402
    import json as _json                              # noqa: E402

    # ⑤k ★ 源码里 `STATE_FILE.write_text` **一个字都不许剩**
    #   （留一处 = 那条路仍然裸写；而它正好是最常走的那条）
    ck("★★ ⑤k 源码里没有 `STATE_FILE.write_text`（裸写已彻底换掉）",
       "STATE_FILE.write_text" not in _SRC,
       "还有裸写落点 ⇒ 那条路仍能写出半截文件")

    # ⑤l ★★ 行为判据：**同目录** 且 临时名是 `<原名>.tmp`
    #   ★ 为什么"同目录"是硬要求：`os.replace` **跨文件系统**不是原子 rename，
    #     会退化成"拷贝+删" —— 那就把原子性丢了，而**代码看着一模一样**。
    _ws = getattr(_DL, "write_state", None)
    _suffix = getattr(_DL, "STATE_TMP_SUFFIX", None)
    ck("★ ⑤l 有 `STATE_TMP_SUFFIX` 且值为 `.tmp`",
       _suffix == ".tmp", f"读到 {_suffix!r}")

    if callable(_ws):
        with _tf.TemporaryDirectory() as _td:
            _sf = pathlib.Path(_td) / ".drive-loop.state"
            _orig = _DL.STATE_FILE
            _DL.STATE_FILE = _sf
            try:
                _ws({"round": 7, "phase": "idle"})
                # ⑤m ★ 内容真的落盘了、且是**完整 JSON**
                _got = _json.loads(_sf.read_text(encoding="utf-8")) if _sf.is_file() else {}
                ck("★ ⑤m `write_state` 落盘的内容可被 `json.load` 完整读回",
                   _got == {"round": 7, "phase": "idle"}, f"读到 {_got!r}")
                # ⑤n ★★ 目录里**没有**残留临时文件
                _left = sorted(p.name for p in pathlib.Path(_td).iterdir()
                               if p.name != ".drive-loop.state")
                ck("★★ ⑤n 写成功后目录里**没有**残留临时文件（否则哨兵会报未知文件）",
                   _left == [], f"残留 {_left!r}")
                # ⑤o ★★ 临时名是 `<原名>.tmp` —— 这是**两个守卫唯一同时满足**的写法
                #   （哨兵要求不匹配 `.state$`，chk58 的反向判据也要求不匹配）
                #   ★★ 判据**必须是"真去写一次、看目录里实际出现什么名字"** ——
                #     第一版只比对了字符串常量，于是把实现改成 `with_suffix(".tmp.state")`
                #     它**照样过**（实测：一条恒真的假断言）。教训同 ERR-AI 那一族：
                #     **断言要落在被测对象的行为上，不落在它自己声明的常量上。**
                _seen = []
                _sf2 = pathlib.Path(_td) / ".drive-loop.state"
                _orig2 = _DL.STATE_FILE
                _real_replace = _DL.os.replace

                def _spy(a, b):
                    _seen.append(pathlib.Path(a).name)
                    return _real_replace(a, b)

                _DL.STATE_FILE = _sf2
                _DL.os.replace = _spy
                try:
                    _ws({"round": 8})
                finally:
                    _DL.os.replace = _real_replace
                    _DL.STATE_FILE = _orig2
                ck("★★ ⑤o 真写一次：`os.replace` 的源名是 `.drive-loop.state.tmp`",
                   _seen == [".drive-loop.state.tmp"],
                   f"实际源名 {_seen!r}")
            finally:
                _DL.STATE_FILE = _orig

            # ⑤p ★ 失败路径：写临时文件就抛 ⇒ **不许**抛出去（契约：不拖垮批次）
            #     且不许留下垃圾
            _orig_ws = _DL.STATE_FILE
            _DL.STATE_FILE = pathlib.Path(_td) / "sub" / "nonexistent" / ".drive-loop.state"
            try:
                _raised = None
                try:
                    _ws({"round": 1})
                except Exception as e:                 # noqa: BLE001
                    _raised = e
                ck("★ ⑤p 写失败时**不抛**（通知/状态是附属功能，绝不能拖垮跑批）",
                   _raised is None, repr(_raised))
            finally:
                _DL.STATE_FILE = _orig_ws

# ⑤q ★★ 哨兵对临时名**显式登记**（不是靠通配糊过去）
_DRIFT = REPO / "scripts" / "diag" / "check-deploy-drift.py"
_DRIFT_SRC = _DRIFT.read_text(encoding="utf-8") if _DRIFT.is_file() else ""
ck("★★ ⑤q 哨兵的 KNOWN_NAS 显式登记了 `.state.tmp` 临时名",
   r"\.[^/]+\.state\.tmp$" in _DRIFT_SRC,
   "临时名没登记 ⇒ 哨兵若正好扫在 os.replace 之前会误报未知文件")


# ==========================================================================
print("\n=== ⑥ DEPLOY_MANIFEST：新脚本收进受管集合 ===")
# ==========================================================================
# ★★ 2026-10-05（B2/§8.3 + 删 deploy.sh）：模型改为「代码全在镜像里」⇒ drive-loop 的
#   **代码**（run-resident.sh / run.sh / drive-loop.py）**不再进受管集合**，
#   改由 `drive-loop.Dockerfile` 的 `COPY data/drive-loop/…` 打进镜像。
#   ★ 受管集合的来源同时从「解析 deploy.sh 的 FILES」改为**哨兵内的常量 `DEPLOY_MANIFEST`**
#     （`deploy.sh` 已整个删除）。这里载入哨兵本身，直接读那个常量。
#   ⇒ 断言随之翻转：**必须不在受管集合里**（进了就是"改代码走文件同步"的旧模型回潮），
#     且**必须在 Dockerfile 的 COPY 里**（否则容器里没有入口脚本）。
import importlib.util                             # noqa: E402
_DRIFT_MODNAME = "check_deploy_drift_for_service"
_spec_d = importlib.util.spec_from_file_location(
    _DRIFT_MODNAME, REPO / "scripts" / "diag" / "check-deploy-drift.py")
_M = importlib.util.module_from_spec(_spec_d)
sys.modules[_DRIFT_MODNAME] = _M                  # ★ 必须在 exec_module 之前（tests/README 的规矩）
_spec_d.loader.exec_module(_M)
# 受管集合的「条目行」形态：`本地::目的地`（与旧 `_DEP` 解析同形，便于逐条点名）。
_DEP_ENTRIES = [f"{s}::{d}" for s, d in _M.DEPLOY_MANIFEST]
ck("⑥a drive-loop 代码**不在**受管集合里（已改走镜像）",
   not any("drive-loop/run-resident.sh" in e or "drive-loop/scripts/drive-loop.py" in e
           for e in _DEP_ENTRIES),
   "drive-loop 代码又回到文件同步受管集合了 ⇒ 两个真源，会漂")
_DLDF = (REPO / "data" / "drive-loop" / "Dockerfile")
_DLDF_SRC = _DLDF.read_text(encoding="utf-8") if _DLDF.is_file() else ""
ck("⑥b drive-loop.Dockerfile 把常驻入口 COPY 进镜像（/app/run-resident.sh）",
   "COPY data/drive-loop/run-resident.sh /app/run-resident.sh" in _DLDF_SRC,
   "镜像里没有入口脚本 ⇒ compose 的 entrypoint /app/run-resident.sh 找不到文件")
#   ★ chk58.sh **故意不进**受管集合（它是 NAS 侧诊断件，不部署）——
#     这条钉的是"别顺手把它加进去"（加了就是**假一致**：生产上有个没人调的脚本）。
#   ★★ 取法**必须看「条目」不看「全文」**（2026-09-29 的教训，留痕）：
#     判据改成"**受管集合里的条目**"（只看条目，不看注释）——这才是它本来要钉的东西
#     （"别把 chk58.sh 收进受管集合"）。全文子串会把"提到它"当成"用了它"。
ck("★ ⑥c chk58.sh **不在**受管集合的条目里（NAS 侧诊断件，不部署；加进去=假一致）",
   not any("chk58.sh" in e for e in _DEP_ENTRIES),
   "chk58.sh 被加进受管集合了：" + repr([e for e in _DEP_ENTRIES if "chk58.sh" in e]))
#   ★ 反向对照：判据本身要能判红（防恒真）—— 造一条假的"已加入"条目
_FILES_FAKE = _DEP_ENTRIES + ["scripts/diag/chk58.sh::scripts/diag/chk58.sh"]
ck("★ ⑥c′ 对照：**真把 chk58.sh 加进条目**时，上面那条**判得红**（防恒真）",
   any("chk58.sh" in e for e in _FILES_FAKE),
   "判据恒真 ⇒ 它什么也没钉住")
#   ★★ 同时把**本轮新加的那两个**也钉住（否则 ⑥c 只钉了"没加什么"）
#   ★ 判据锚在**目的地侧**（`::…/watch-containers.sh`）—— 因为 `data/scripts/diag/...`
#     这个前缀随收拢变过，锚子串会因为前缀变而失配（⑥d 之前正是这么假红的候选）。
ck("★ ⑥d watch-containers.sh 与 -loop 都在受管集合里（容器读它们 ⇒ 必须由部署机制保证一致）",
   any(e.endswith("::data/scripts/diag/watch-containers.sh") for e in _DEP_ENTRIES)
   and any(e.endswith("::data/scripts/diag/watch-containers-loop.sh") for e in _DEP_ENTRIES),
   repr([e for e in _DEP_ENTRIES if "watch-containers" in e]))

# ==========================================================================
print("\n=== ⑦ autoheal 侧车：卡死接管 ===")
# ==========================================================================
#   ★ 这一段钉的是**接管机制**（2026-09-17 加）。为什么它是**新的一层**：
#     `restart: unless-stopped` 只对容器**退出**生效 —— **卡死**（进程活着、
#     心跳停了、但不退出）**不会**触发任何重启。要有人看着 `unhealthy` 并动手。
#   ★★ 最大的一条风险是**爆炸半径**：`/var/run/docker.sock` ≈ **root 等价**
#     （能创建/删除/exec 任何容器），而**本仓此前没有任何东西挂过它**。
#     所以下面几条**必须**逐条钉住：只读挂 + 作用域限定 + 与主容器同 profile。
if YML:
    _ah = (svcs.get("autoheal") if isinstance(svcs, dict) else None) or \
          (YML.get("services", {}) or {}).get("autoheal")
    ck("⑦a compose 里有 autoheal 服务（卡死接管侧车）", isinstance(_ah, dict),
       repr(sorted((YML.get("services") or {}).keys())))
    if isinstance(_ah, dict):
        #   ★★ 同 profile ⇒ 与 drive-loop **同生同死**。不带的话 `up -d` 会单独
        #      把它拉起来，而它要治的容器还没启用 ⇒ 空转的、握着 docker.sock 的容器。
        ck("★★ ⑦b autoheal 与 drive-loop **同一个 profile**（同生同死，不空转）",
           _ah.get("profiles") == ["drive-loop"], repr(_ah.get("profiles")))
        #   ★★ 只读挂 docker.sock —— 能少给一分权限就少给一分
        _av = _ah.get("volumes") or []
        _sock = [v for v in _av if "docker.sock" in str(v)]
        ck("★ ⑦c autoheal 挂了 /var/run/docker.sock", bool(_sock), repr(_av))
        _ro = all((v.get("read_only") is True) if isinstance(v, dict)
                  else str(v).endswith(":ro") for v in _sock) if _sock else False
        ck("★★ ⑦d docker.sock 是 **:ro 只读挂**（≈root 等价的东西，不给写）",
           _ro, repr(_sock))
        #   ★★ 作用域：只治带该 label 的容器（= 只有 drive-loop），不是"治好一切"
        _aenv = _ah.get("environment") or []
        if isinstance(_aenv, dict):
            _aenv = [f"{k}={v}" for k, v in _aenv.items()]
        _aenv_s = " ".join(str(e) for e in _aenv)
        ck("★★ ⑦e 有 AUTOHEAL_CONTAINER_LABEL（限定只治一个容器，不是治好一切）",
           "AUTOHEAL_CONTAINER_LABEL" in _aenv_s, _aenv_s)
        #   ★ 常驻 + 日志上限（本仓四个服务全有 logging，这个也不许漏）
        ck("⑦f autoheal restart = unless-stopped", _ah.get("restart") == "unless-stopped",
           repr(_ah.get("restart")))
        _alg = _ah.get("logging")
        ck("★ ⑦g autoheal 有 logging（走 *logging-limits；漏了=无上限日志）",
           isinstance(_alg, dict) and _alg.get("driver") == "json-file", repr(_alg))
    #   ★ 反向：autoheal **不许**吃 env_file（它不需要 .env 里的任何凭据 ——
    #     它要的只有 docker.sock。给它 .env 等于白送一份密钥面）。
    ck("★ ⑦h autoheal **不吃 env_file**（它不需要任何凭据，别白送密钥面）",
       isinstance(_ah, dict) and "env_file" not in _ah, repr(_ah.get("env_file") if isinstance(_ah, dict) else None))
else:
    print("  --   没有 PyYAML ⇒ ⑦ 段未验（**不是通过**）")

# ==========================================================================
# ⑧ shell 工具里的 **python heredoc 必须锁 UTF-8**（2026-09-18 实测的哑火）
# --------------------------------------------------------------------------
#   ★★ 形状：② 段那个 heredoc 里 `print("  ✓/✗ …")` 打的是 U+2713/U+2717，
#     而 Windows 的 python 默认 stdout 是 **GBK** ⇒ 编不出 ⇒ UnicodeEncodeError。
#   ★ 症状**不是"整个脚本崩掉"**（那样反而显眼），是 **② 段半途而废**：
#     前面的 `✓` 行都打完了，一到**第一条 `✗`** —— 也就是**真有问题、最该被看到**
#     的那一行 —— 就抛。⇒ 那一段的**全部裁决** + `⇒ 路径不可达数` **一行都出不来**，
#     只剩一个 traceback；而 `|| RC=1` 又兜住了退出码 ⇒ 脚本以 1 退出却**不给解释**。
#   ★ 也就是说：**判据恰好在最该报的时候哑掉**（本项目最怕的形状）。
#   ★ 处置是 `export PYTHONIOENCODING=utf-8`，本仓已有两处先例（`chk58.sh:107`、
#     `drive-loop-nas.sh` / `run-resident.sh` 同句），属"照抄"而非新设计。
#   ★★ 反向也要钉：shell 那侧的 `bad()`/`ok()`（`printf`）**不需要**这条 ——
#     printf 不过 python 的编码器，实测在 GBK 下照打（本项目已在 `chk58.sh` 里验过）。
#     别把"给 shell 也加一条"当补强 —— 那是把判据钉在一个**不存在**的失效模式上。
print()
_sh = {
    p.name: p.read_text(encoding="utf-8", errors="replace")
    for p in (MOUNT_SELFCHECK, CHK58)
}
for _n, _t in _sh.items():
    _has_py = ("python" in _t) and ("<<'PY'" in _t or "<<'PYEOF'" in _t
                                    or "\nPY\n" in _t or "\nPYEOF\n" in _t)
    _marks = ("✓" in _t) or ("✗" in _t)
    ck(f"⑧ {_n} 有 python heredoc 且打了 ✓/✗（所以这条断言对它有对象）",
       _has_py and _marks, f"has_py={_has_py} marks={_marks}")
    if _has_py and _marks:
        ck(f"⑧ ★★ {_n} 锁了 PYTHONIOENCODING=utf-8"
           "（没有它 ⇒ 第一条 ✗ 就把整段裁决吞掉）",
           "export PYTHONIOENCODING=utf-8" in _t,
           "缺 `export PYTHONIOENCODING=utf-8`；GBK 控制台上 python 打 ✗ 会抛")
#   ★ 反向判据：这条**只**该加在"python 打 unicode"的地方。
#     给纯 shell 的 printf 也加 = 钉一个不存在的失效模式（见上文）。
ck("★ ⑧ 反向：`chk58.sh` 的 ✓/✗ 同时有 shell printf 与 python 两条路 —— "
   "两处都必须被上面的断言覆盖到（别只改一处）",
   ("printf '  ✗ %s\\n'" in _sh.get("chk58.sh", ""))
   and ("export PYTHONIOENCODING=utf-8" in _sh.get("chk58.sh", "")),
   "chk58.sh 的 shell bad() 与 python 打印都在；锁只需管 python 那半")

# ==========================================================================
# ⑨ `phase` 的**默认值**与**三处收尾**（2026-09-19 加）
# --------------------------------------------------------------------------
#   ★★ 这一段的来历要说清楚，因为**我上一轮把结论搞反过**：
#     我在 NAS 上 `cat .drive-loop.state` 看到 `phase:"running"`，就断言
#     「收工后残留 running ⇒ 下一个容器永远判在跑 ⇒ 静默永久停工」。
#     **那是错的** —— 7 分钟后同一条命令读到 `phase:"idle"`。
#     ⇒ `running` 是**批次正在跑**时的正常瞬时值，睡眠期被
#       `Heartbeat(phase=PHASE_IDLE)` 覆盖成 `idle`。**现场从头到尾是对的。**
#     ★ 我错在**拿"读了一秒的那一帧"当稳定状态**，而这个字段**每 60 秒就翻一次**
#       （心跳 `period=60.0`）。⇒ 教训写进 SUMMARY §29.8：
#       **`cat` 一次 `.drive-loop.state` 不是有效的取证动作。**
#
#   ★ 所以这一段**不修任何东西**（没有缺陷可修），它钉的是**"别让将来的人
#     把这三处之一改成 `running`"** —— 那才会造出我上一轮**以为**存在的那个缺陷：
#       `_resident_state` 的 `phase` 形参有**一处默认值**，而它**四处调用里有三处
#       不传参**（启动 / dry-run / 无动作 / 正常收工）⇒ **默认值写错 = 三处同时写错**，
#       而症状是「永远判在跑 ⇒ 静默永久停工」（`Heartbeat` docstring 最怕的那个）。
#   ★★ 判据用 **AST 读 `defaults`**，不用字符串匹配：
#     字符串匹配分不出「默认值是 idle」和「注释里写了 idle」。
# ==========================================================================
print("\n=== ⑨ `phase` 默认值 + 三处收尾（2026-09-19 加；上轮结论搞反过）===")
if _TREE is not None:

    def _defn(name: str):
        for _x in ast.walk(_TREE):
            if isinstance(_x, ast.FunctionDef) and _x.name == name:
                return _x
        return None

    # ---- ⑨a `_resident_state` 的 `phase` 形参默认值必须是 PHASE_IDLE ----
    _rs = _defn("_resident_state")
    ck("前提：找得到 `_resident_state()`（常驻状态凡唯一写入点）", _rs is not None)
    if _rs is not None:
        _a = _rs.args
        #   ★ 形参名以 ast.arg.arg 报出；默认值在 args.defaults（**右侧对齐**）。
        _pos = [x.arg for x in _a.args]
        _dflt = _a.defaults
        _off = len(_pos) - len(_dflt)          # 默认值只覆盖尾部
        _map = {_pos[_off + i]: d for i, d in enumerate(_dflt)}
        _ph = _map.get("phase")
        _is_idle = (isinstance(_ph, ast.Name) and _ph.id == "PHASE_IDLE") or \
                   (isinstance(_ph, ast.Constant) and _ph.value == "idle")
        ck("★★ ⑨a `_resident_state` 的 `phase` 形参默认值是 `PHASE_IDLE`"
           "（★ 四处调用里三处不传参 ⇒ 默认值写错=三处同时写错）",
           _is_idle,
           f"读到默认值 {ast.dump(_ph) if _ph is not None else None!r}；"
           "若默认成 PHASE_RUNNING ⇒ 启动/dry-run/无动作/正常收工全写 running"
           " ⇒ 下一个容器永远判在跑 ⇒ 静默永久停工")
        #   ★ 反向：`phase` **必须带默认值**。不带的话四处调用里三处当场 TypeError
        #     （症状是"常驻模式起不来"，比写错值更显眼，但仍然该钉）。
        ck("★ ⑨a-2 `phase` **有**默认值（没有 ⇒ 三处不传参的调用当场 TypeError）",
           "phase" in _map, f"形参 {_pos} / 默认 {list(_map)}")

        # ---- ⑨a-3 ★★ `_resident_state(...)` 的**每个调用点**都要形状对得上 ----
        #   ★★ 为什么必须单独钉这个（2026-10-05 实测踩到）：去掉「包的闸门」时，
        #      `_resident_state` 的形参从 `(round_n, pack_idx, streak, note, phase=)`
        #      **删掉了 `pack_idx`** ⇒ 变成 `(round_n, streak, note, phase=)`。
        #      **四处调用里三处改对了，启动那处漏改** —— 它仍写
        #      `_resident_state(0, -1, consec, "启动")`：位置全串了一位，
        #      `streak` 收成 `-1`（int）⇒ 函数体 `int(streak[0])` 当场
        #      `TypeError: 'int' object is not subscriptable`。
        #   ★★ 危害形状：这条只在**常驻/`--dry-run` 路径**炸，而 `--once` 正常路径
        #      **不走它** ⇒ 常规跑批全绿；一上常驻（`restart: unless-stopped`）就
        #      **崩溃重启**，日志里是一串 traceback —— 属"改一处漏一处"的经典形状。
        #   ★★ 判据：把 `_resident_state` 的**形参名表**取出来，遍历 `main()` 里
        #      **每一个** `_resident_state(...)` 调用，断言：
        #        ① 位置参数个数 ≤ 形参个数（多了 ⇒ 又多传了一个已删的形参）；
        #        ② `note`（第 3 个位置实参，或 `note=`）是**字符串字面量**
        #           （`"启动"`/`"dry-run"`/… 都是常量）—— 上面那个 bug 里
        #           `note` 收到了**第 4 个位置实参 `"启动"` 之外的 `consec` 变量**，
        #           靠"note 必须是字面量"能抓住；
        #        ③ `phase`（第 4 个位置实参，或 `phase=`）**若给出**，必须是
        #           已知的 phase 串（`"idle"`/`"running"`）—— bug 里它收到 `"启动"`。
        #      ★ 用 AST 而不是字符串/正则：注释和 docstring 里满是 `_resident_state`。
        _POS_NAMES = [x.arg for x in _rs.args.args if x.arg != "self"]
        _n_pos = len(_POS_NAMES)

        def _phase_ok(node: ast.AST) -> bool:
            """第 4 个位置实参 / `phase=` 值如果是字面量，得是已知 phase。"""
            v = None
            if (isinstance(node, ast.Call) and len(node.args) >= 4
                    and not isinstance(node.args[3], ast.Starred)):
                v = node.args[3]
            for kw in (node.keywords if isinstance(node, ast.Call) else []):
                if kw.arg == "phase":
                    v = kw.value
            if v is None:
                return True                     # 没给 phase ⇒ 用默认值，合法
            #   只看字面量：给变量（如 `_phase`）时无从静态判定，放过。
            return (isinstance(v, ast.Constant)
                    and isinstance(v.value, str)
                    and v.value in ("idle", "running"))

        def _note_ok(node: ast.AST) -> bool:
            """第 3 个位置实参 / `note=` 值应是字符串字面量。"""
            v = None
            if (isinstance(node, ast.Call) and len(node.args) >= 3
                    and not isinstance(node.args[2], ast.Starred)):
                v = node.args[2]
            for kw in (node.keywords if isinstance(node, ast.Call) else []):
                if kw.arg == "note":
                    v = kw.value
            if v is None:
                return True
            return isinstance(v, ast.Constant) and isinstance(v.value, str)

        _rs_calls = [n for n in ast.walk(_TREE)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                     and n.func.id == "_resident_state"]
        ck("前提：找得到 `_resident_state` 的调用点（≥4 处：启动/dry-run/无动作/异常/收工）",
           len(_rs_calls) >= 4, f"找到 {len(_rs_calls)} 处")
        _bad_arity = [ast.unparse(n) for n in _rs_calls
                      if not (n.keywords and n.keywords)
                      and len(n.args) > _n_pos]
        ck("★★ ⑨a-3 每个 `_resident_state(...)` 调用的**位置参数个数 ≤ 形参个数**"
           "（多了 = 又传了一个已删的形参）",
           not _bad_arity,
           f"形参 {_POS_NAMES}（{_n_pos} 个）；越界调用：{_bad_arity}")
        _bad_note = [ast.unparse(n) for n in _rs_calls if not _note_ok(n)]
        ck("★★ ⑨a-3b 每个调用的第 3 位实参 / `note=` 是**字符串字面量**"
           "（位置串位时这里会收到变量 ⇒ 红）",
           not _bad_note, f"非字面量的调用：{_bad_note}")
        _bad_phase = [ast.unparse(n) for n in _rs_calls if not _phase_ok(n)]
        ck("★★ ⑨a-3c 每个调用若给 `phase`（第 4 位实参 / `phase=`）必须是"
           " `\"idle\"`/`\"running\"`（传了 `\"启动\"` 这种 = 位置串位 ⇒ 红）",
           not _bad_phase, f"非法 phase 的调用：{_bad_phase}")

    # ---- ⑨b 三处收尾都**显式**写 `"phase"`（整体覆盖，不写=删掉该键）----
    #   ★★ 为什么是"整体覆盖"这条性质在起作用：三处 `write_state({...})` 都是
    #     **字典字面量、不是 `{**st, ...}` 合并** ⇒ **不写 `phase` 就等于删掉它**
    #     ⇒ 下一个读者走「旧格式」分支（只信心跳）⇒ 「说在跑的那位早收工了」检测不到。
    #
    #   ★★★ 这一段的**第二版又栽了一次，同样被变异测试抓出来**（两次都栽在
    #     「怎么算'收尾那个'」上，值得逐字记）：
    #     第二版用 `ast.walk` 的**出现顺序取最后一个** —— 结果 `main()` 里取到的是
    #     **嵌套函数 `_resident_state` 的 `write_state`（L2957）**，而不是常驻分支
    #     真正的收尾（**L3073**）。⇒ 削掉 L3073 的 `phase` 之后断言**照样过**。
    #     ★ 根因：`ast.walk` 是 **BFS**（先父后子、同级按字段顺序），**不是源码顺序**；
    #       而 `_resident_state` 是 `main()` 的**嵌套函数**，它的调用点行号更小，
    #       却在 walk 顺序里排得更晚。⇒ **凡"第几个/最后一个"都必须按 `lineno` 排。**
    #     ★★ 第三版改成：**按 `lineno` 排序**，并且收尾那个用**结构特征**指名
    #       ——`once_round` 的收尾认 `"last_end_ts"`（开跑那次没有这个键）；
    #       `main` 的收尾也认 `"last_end_ts"`。**特征比位置稳**。
    #     ★ 一句话：**"取最后一个"这种写法，在嵌套函数 + BFS 遍历下是错的。**
    _want = [("once_round", "once_round() 的 finally 收尾", "tail", None),
             ("main", "main() 常驻分支的 --max-rounds 收尾", "tail", None),
             ("_resident_state", "_resident_state() 的每轮快照", "any", None)]

    def _ws_dict_keys(fn_name: str) -> list:
        """**按 `lineno` 排序**返回该函数里每个 `write_state(<字典字面量>)`。

        返回 `[(lineno, [键名…]), …]`。
        ★ 必须按 `lineno` 排，**不能靠 `ast.walk` 的出现顺序**（BFS：嵌套函数的调用
          点行号更小却排得更晚 ⇒ "取最后一个"会取错，实测栽过）。
        """
        _f = _defn(fn_name)
        if _f is None:
            return []
        out = []
        for _n in ast.walk(_f):
            if isinstance(_n, ast.Call) and isinstance(_n.func, ast.Name) \
                    and _n.func.id == "write_state":
                for _arg in _n.args:
                    if isinstance(_arg, ast.Dict):
                        out.append((_n.lineno,
                                    [_k.value for _k in _arg.keys
                                     if isinstance(_k, ast.Constant)]))
        return sorted(out)

    #: 收尾那次的**结构特征**：整体覆盖写、且带 `last_end_ts`（开跑那次没有）。
    #: ★ 用特征而不是位置 —— 位置法已经栽过两次（BFS + 嵌套函数）。
    _TAIL_MARK = "last_end_ts"

    _dbg = {}
    for _fn, _label, _which, _ in _want:
        _calls = _ws_dict_keys(_fn)
        _dbg[_fn] = _calls
        #   ★ 前提：找得到（找不到就该红，别让下面两条"因为空集而恒真"）
        ck(f"前提：⑨b `{_fn}` 里有 `write_state(<字典字面量>)` 可判",
           bool(_calls), f"读到 {_calls!r}")
        if not _calls:
            continue
        if _which == "tail":
            #   ★★ 指名**收尾那一次**：`last_end_ts` 是"这批结束了"的落盘标记，
            #      而开跑那次写的是 `running_pid_pidns`/`heartbeat_ts`。
            _tails = [k for _ln, k in _calls if _TAIL_MARK in k]
            ck(f"前提：⑨b `{_fn}` 里找得到**收尾那次**（带 `{_TAIL_MARK}`）",
               len(_tails) == 1, f"命中 {len(_tails)} 次：{_calls}")
            if len(_tails) != 1:
                continue
            _keys, _where = _tails[0], f"**收尾那次**（带 `{_TAIL_MARK}` 的整体覆盖写）"
        else:
            _keys = [k for _ln, _c in _calls for k in _c]
            _where = "任一"
        ck(f"★★ ⑨b {_label} 的 `write_state` 字面量里**含** `\"phase\"`"
           f"（{_where}；整体覆盖 ⇒ 不写就是删掉它，读者退回「只信心跳」）",
           "phase" in _keys,
           f"`{_fn}` 的{_where}键表 = {_keys}；"
           f"该函数全部 write_state（按行号）= {_calls}")

    def _tail_has_phase(fn_name: str) -> bool:
        return any("phase" in k for _ln, k in _ws_dict_keys(fn_name)
                   if _TAIL_MARK in k)

    ck("★★ ⑨b-2 三处**全部**有（漏一处 = 那条路的收尾退回旧格式）",
       _tail_has_phase("once_round") and _tail_has_phase("main")
       and any("phase" in k for _ln, k in _ws_dict_keys("_resident_state")),
       f"逐函数 write_state（行号, 键）={_dbg}")

    # ---- ⑨c ★★ 反向：`600s` 那条线**两个方向**都得成立 ----
    #   ★ 这是本段**唯一**钉行为的一条，而且它**正反都要**：
    #     · `running` + 心跳**新鲜**(60s)  ⇒ True  —— 别把**正常在跑**的判成可接管
    #       （判错这一边 = 两批并发发 webhook ⇒ 一次 429 废掉几百条，README 坑 4）
    #     · `running` + 心跳**陈旧**(900s) ⇒ False —— 卡死**必须**能被接管
    #       （判错这一边 = autoheal 那套机制**白装**，卡死的容器永远没人接管）
    #   ★★ 我上一轮提的「甲案」（把 600 抬到 1800）就是**牺牲后一边去修前一边**
    #     —— 而前一边**本来就没坏**（`cat` 到的那帧 `running` 7 分钟后自己变 `idle`）。
    #     这条断言把两个方向同时钉住，**将来谁再想抬这个阈值都会当场看到代价**。
    if _DL is not None:
        ck("★★ ⑨c-1 反向·正向：`running` + 心跳**新鲜**(60s) ⇒ **True**"
           "（正常在跑的绝不许判成可接管 —— 两批并发=一次 429 废几百条）",
           _ba(_st(phase="running", heartbeat_ts=_now - 60)) is True,
           "把正常在跑的判成可接管了；这是 README 坑 4 那个事故的形状")
        ck("★★ ⑨c-2 反向·负向：`running` + 心跳**陈旧**(900s) ⇒ **False**"
           "（卡死必须能被接管；判成 True ⇒ autoheal 那套白装）",
           _ba(_st(phase="running", heartbeat_ts=_now - 900)) is False,
           "900s 判成「还在跑」⇒ 卡死检测被关掉了")
        #   ★ 再钉一条**边界的方向**：阈值就在 600s 上下，
        #     599s 该 True、601s 该 False —— 免得将来有人把比较写成 `>=`。
        _TH = getattr(_DL, "HEARTBEAT_STALE_SEC", None)
        ck("★ ⑨c-3 `HEARTBEAT_STALE_SEC` 仍是 600s（这条线的位置本身就是判据）",
           _TH == 600, f"读到 {_TH!r}")
        if _TH == 600:
            #   ★★ 这条**证明的东西比第一版想的少，如实写清楚**（第一版措辞吹了）：
            #     · 它**能**抓住**比较方向写反**（`<=` → `>=`）——
            #       实测：这么改会让 ⑤e/⑤g/⑨c-1/⑨c-2/⑨c-4 **五条一起红**。
            #     · 它**抓不住** `<=` → `<` 这种**边界开闭**的改动：
            #       `time.time()` 不可能正好落在 `stale == 600.000000`，
            #       599/601 两侧两种写法**行为完全一样**（实测：97 过 / 0 失败）。
            #     ⇒ 想钉开闭只能**注入可控时钟**，本文件做不到（`batch_alive` 用的是
            #       模块级 `time.time`）⇒ 那种改动**本机未验**，不许当成已覆盖。
            ck("★ ⑨c-4 边界两侧取值方向没被判反：599s ⇒ True、601s ⇒ False"
               "（★ 只证方向，不证开闭 —— 见上方注释）",
               _ba(_st(phase="running", heartbeat_ts=_now - 599)) is True
               and _ba(_st(phase="running", heartbeat_ts=_now - 601)) is False,
               "边界两侧至少一侧判反了（注意：本机验不了 `<=` vs `<` 的开闭）")

#   ★★ ⑨d 写成一个**未验**（不是通过）：`phase` 的**翻转频率**（60s/拍）
#     决定了「`cat` 一次不算取证」这条方法学 —— 而它**离线判不了**
#     （要真跑一个常驻进程、在两个时刻各读一次）。⇒ 显式打 `--` 行，
#     **不计入断言数**：本项目铁律「拿不到 ≠ 通过」（B.10 第 14 条）。
print("  --   ⑨d `phase` 翻转频率（60s/拍）⇒「cat 一次不算取证」—— **本机未验**，"
      "要真常驻进程 + 两个时刻各读一次；**不是通过**")

# ==========================================================================
print("\n=== ⑩ CROSSSEED_DIRS / MANIFEST_DIR：`data/` 也要搜（B6 实测补）===")
# ==========================================================================
#   ★★ 为什么单列一段：2026-10-05 把 cross-seed 运行时数据收进 `data/` 之后，
#      `COMPOSE_DIR`（= 仓库根，含 `.env` / `build-farm.sh`）**不再是**它们所在的那层
#      （`cross-seed/cross-seed.db`、`logs/info.current.log`、`hlink/` 都在 `data/` 下）。
#      只搜根一层 ⇒ `first_existing()` 全部落空 ⇒ **静默退化**（日报 packpct=0，§26.15）。
#      ⇒ 这条钉住"根 **与** 根/data 两层都搜"（本机 AST，能红）。
if _TREE is not None:
    _cd_dirs = None
    _cd_root = None
    for _n in ast.walk(_TREE):
        if isinstance(_n, ast.Assign):
            for _t in _n.targets:
                if getattr(_t, "id", None) == "CROSSSEED_DIRS":
                    _cd_dirs = _n.value
                if getattr(_t, "id", None) == "_COMPOSE_ROOT":
                    _cd_root = _n.value
    ck("前提：drive-loop.py 里有模块级 `CROSSSEED_DIRS` 赋值", _cd_dirs is not None,
       "找不到 CROSSSEED_DIRS 赋值")
    #   判据用**源码文本**而不是 AST 结构：`CROSSSEED_DIRS` 现在是元组字面量，
    #   第二个元素 = `str(Path(_COMPOSE_ROOT) / "data")` ⇒ 源码里必须有 `"data"`。
    _cd_seg = ast.get_source_segment(_SRC, _cd_dirs) if _cd_dirs is not None else None
    ck("★★ ⑩a `CROSSSEED_DIRS` 里包含 `data` 子层（否则收进 data/ 的 db/日志全落空）",
       bool(_cd_seg) and '"data"' in _cd_seg,
       repr(_cd_seg)[:200] if _cd_seg else "取不到源码段")
    _bf = REPO / "data" / "drive-loop" / "scripts" / "build-farm.sh"
    _bf_src = _bf.read_text(encoding="utf-8") if _bf.is_file() else ""
    ck("★★ ⑩b `build-farm.sh` 的清单目录**只往可写的地方落**"
       "（容器把 compose 根挂成 :ro ⇒ 写 data/hlink 会 Read-only file system，退 2）",
       "FARM_MANIFEST_DIR" in _bf_src and "_pick_manifest_dir" in _bf_src,
       "build-farm.sh 没有可写性筛选 ⇒ 容器里农场巡检会挂")
    ck("★★ ⑩c `drive-loop.py::_run_verify` 传了 `FARM_MANIFEST_DIR`（指向 STATE_DIR/hlink）",
       "FARM_MANIFEST_DIR" in _SRC and 'str(STATE_DIR / "hlink")' in _SRC,
       "没传 ⇒ build-farm 回退到只读的 compose 根 ⇒ 退 2")

# ==========================================================================
print(f"\n{'=' * 60}")
print(f"断言 {_ok + _bad} 条：{_ok} 过 / {_bad} 失败")
if not _DOCKER:
    print("★ ② 段（profiles 的真语义）**本机未验** —— 上面打的是 `--` 行，"
          "不是通过。别把本文件的绿读成「那把锁真的锁上了」。")
sys.exit(0 if _bad == 0 else 1)
