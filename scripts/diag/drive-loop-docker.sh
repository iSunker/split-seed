#!/bin/sh
# =====================================================================
# drive-loop 的 **容器版入口** —— 由 DSM 任务计划唤醒，跑「一次性容器」
#
# ★★ 状态：**草案。未部署、也未进受管集合 `DEPLOY_MANIFEST`（有意）。**
#    理由：受管集合的语义是「这个文件两边必须一致」，而这份还没在 NAS 上验过。
#    现在就收进去，下次谁直推一次就会把它推到生产
#    —— 推上去本身无害（没人调用它），但会制造一个假一致：
#    生产上有这个文件、看起来「这就是在用的那套」，而实际上 DSM 任务调的还是
#    `drive-loop/run.sh`。**先验，后进受管集合**，顺序别倒。
#    ★ 2026-09-13 冒烟实测：它依赖的 `reseed-orchestrator:0.1.0` **本机不存在**，
#      走之前必须先 build（详见下方「镜像」一节）。即**当前连冒烟都还跑不起来**。
#
# 为什么要有这一层（它和 run.sh 的分工）
# --------------------------------------
#   `drive-loop/run.sh`（仓库里叫 `scripts/drive-loop-nas.sh`）：**容器内**跑的那份。
#     它已经写好了环境（TZ / PYTHONIOENCODING）、参数（--once / --env / --indexers
#     / --limit）、以及 attempts.log 的四个信号。**本包装刻意复用它、不重抄一遍** ——
#     重抄 = `--indexers` 这类名单出现第二个声明点，而「加了站却忘了改一处」
#     正是 2026-09-12 卡住 224 部片子的那个形状（见 run.sh 头部）。
#   本包装只管**容器外边**的事：选镜像、拼挂载、接网络、把两个容器方言的参数
#     透传进去。⇒ 于是「同一批逻辑，宿主跑还是容器跑」只差这一层。
#
# 用法
# ----
#   sh drive-loop-docker.sh                      # 跑一批就退出
#   sh drive-loop-docker.sh --dry-run             # 只看计划（透传给 drive-loop.py）
#   sh drive-loop-docker.sh --limit 5             # 调试用；后给的参数覆盖 run.sh 里的
#   COMPOSE_DIR=... IMAGE=... sh drive-loop-docker.sh
#
#   DSM → 控制面板 → 任务计划 → 新增 → **用户定义的脚本**，用户选 `root`，
#   计划「每 15 分钟」，脚本框一句：
#     sh /volume2/docker_ssd/reseed/drive-loop/drive-loop-docker.sh
#   ★★ 切过来之前**必须先把 id=9 那个跑 `run.sh` 的任务停掉** —— 否则两条路会同时
#      跑：它们**共用同一份** `.drive-loop.state` 与 `attempts.log`。`--once` 的闸门
#      （pid + 心跳）确实能挡住并发，但 attempts.log 会交错成看不懂的样子，
#      而排查时间线时它是唯一的证据文件。
#
# ★★ 什么时候能跑它 —— **测的时候不必等批次间隙**（2026-09-13 读代码确认）：
#    `drive-loop.py` 里那个闸门只有一条进入路径
#        if args.once and not args.dry_run: return once_round(...)
#    ⇒ 带上 `--dry-run` 时**根本不进这里**：`batch_alive()`（pid + 心跳）与
#      批次间隔闸门**一个都不走**，直接落到常驻分支里的那一轮。
#      也就是说 `--dry-run` 是「随时可跑、且不落状态」的那条路。
#    ★ 反过来说：`--dry-run` **只挡写状态，不挡真动作** —— 判断依据是
#      `dry_run` 在 `drive-loop.py` 里只出现两次（2047 的闸门、2058 的透传），
#      别把它当成"什么都不做"的开关到处用。
#    ★ 这一条与「① 部署/② 冒烟要不要等批次间隙」是同一个问题的答案：
#      **冒烟随时可做**；要占批次的是**真正 run 一轮**（不带 --dry-run）。
#
# 挂载清单（★ 这就是「1:1 对上」的全部内容）
# -----------------------------------------
#   两条，缺一不可。**都不是"挑几个子目录"**，因为代码里的路径**全是绝对路径**：
#
#   ① `$COMPOSE_DIR:$COMPOSE_DIR`   （= /volume2/docker_ssd/reseed）
#      必须**同名同路径**。它一次覆盖下面这些，全都写死在代码里：
#        · `CROSSSEED_DIRS[0]` —— `drive-loop.py` 里的**硬编码绝对路径**，
#          `first_existing()` 拿它找 `build-farm.sh`、`cross-seed/cross-seed.db`、
#          `cross-seed/logs/info.current.log`（回灌要用）。找不到只打一行 warning
#          然后**静默退化**。
#        · `build-farm.sh` 的 `COMPOSE_DIR` 默认值、以及它 `mkdir -p` 的清单目录
#          `$COMPOSE_DIR/hlink`（`--verify` 会往那儿写 `*.missing.tmp`）⇒ **必须可写**。
#        · `--env "$COMPOSE_DIR/.env"` —— TORZNAB 那几个 apikey 的来源。
#        · 闸门与台账的全部状态，都在 `$COMPOSE_DIR/drive-loop/scripts/`：
#          `.drive-loop.state` / `.farm-check.state` /
#          `.reconcile.state` / `.daily-report.state` / `drive-loop.log` / `attempts.log`。
#          ★★ 这几个**最容易被漏**（它们不在 `drive-loop/hlink/` 里），而漏了的后果是
#             **静默**的：对账/无人认领的基线会被当成「首次读数」重新记一遍。
#          （★ 原先清单里还有 `.notify.state` 与"告警出栈口 notify/spool/"
#           ——通知写侧已于 2026-10-05 整体砍掉，二者都不再存在。）
#        · sidecar 状态库 `$COMPOSE_DIR/drive-loop/hlink/state.db`（生产独有）。
#          ★ 本包装刻意**不设** `RESEED_STATE_DB` —— 让它走代码里
#            `DEFAULT_DB = ROOT/"hlink"/"state.db"`（`ROOT = HERE.parent`）那条推导。
#            它推导对了，本身就是「挂载 1:1 对上了」的自证；另设一个环境变量
#            等于把这条自证换成「我看过了」。
#        ★ 代价要写明：挂整个 compose 目录 = 把 `.env`、`prowlarr/`（cookie + db）、
#          `cross-seed/`（含 API key）一并交给这个容器。这是**新扩大的爆炸半径**，
#          B/C 两条路都躲不掉（要 1:1 就必然如此）。
#          （★ 原清单里还有 `notify/notify.conf`＝邮箱口令 —— 通知写侧已砍，
#           那份文件不再存在。）
#
#   ② `$VOL1:$VOL1`                （= /volume1/video）
#      媒体卷，同理必须同名同路径：`.env` 里 `DATA_DIRS` / `LINK_DIR` / `FARM_SOURCES`
#      全是 `/volume1/video/...` 开头的绝对路径。而更硬的一条是 **§9：硬链接不能跨卷**
#      —— 农场与源数据必须在**同一条设备**上，所以这个挂载**只能是整卷**，
#      拆成子目录挂就等着建出一堆跨卷副本。脚本启动时会就这一条**自证**（见下）。
#
# 三处与「宿主跑」不同、必须一起改的
# ----------------------------------
#   (a) `--url` / `--qbit-url` —— **本包装显式传服务名**。
#       `drive-loop/run.sh` 的注释写着「刻意不写这两个参数，默认值就是 NAS 的局域网
#       IP」。那在宿主上对；**在容器里会 timeout** —— 群晖上「容器 → 宿主机 LAN IP
#       → DNAT 回另一容器」这条回环实测不通（记录在 `compose.yaml` 头部）。
#       ⇒ 传 `http://cross-seed:2468` / `http://qbittorrent-reseed:3060`。
#       ★ TORZNAB 那侧**不用改**：`TORZNAB_URLS` 实测已经是
#         `http://prowlarr:9696/N/api?apikey=...`（服务名），照旧可用。
#   (b) 网络要**两个**：cross-seed 与 prowlarr 在 `reseed-net`，qbittorrent-reseed
#       在 `qbit-net`（=`qbittorrent-reseed_default`，external）。本包装的做法是
#       「**和 `reseed-cross-seed` 容器同网**」—— 它本来就同时接了两条，
#       于是不需要猜任何网络名（下方有一段专门解释为什么不能猜）。
#   (c) `PY=python` —— run.sh 默认 `/usr/bin/python3`（DSM 自带），容器里没有，
#       镜像里的是 PATH 上的 `python`。`SUDO=` 同理由：镜像里没有 sudo，
#       而 `build-farm.sh` 用的是 `${SUDO-sudo}`，给空串它就跳过（那步本来也 `|| true`）。
#
# ★★ 镜像：**不新写 Dockerfile，但要先把它 build 出来** —— 前提已被实测证伪。
#   原设想是「复用现成的 `reseed-orchestrator:0.1.0`」。2026-09-13 在 NAS 上冒烟，
#   原样返回：
#       Unable to find image 'reseed-orchestrator:0.1.0' locally
#       docker: Error response from daemon: pull access denied for reseed-orchestrator,
#       repository does not exist or may require 'docker login'
#   ⇒ 这个 tag **本机不存在**（也从没 push 到任何 registry）。`compose.yaml` 里写着
#     `image: reseed-orchestrator:0.1.0` 只表示**build 出来的产物该叫什么**，
#     不等于它已经 build 过 —— 「声明了 image 名」和「镜像在本地」是两件事，
#     这个坑和下面那条「看起来像名字的字符串」是同一个形状。
#   ⇒ 走 B 之前必须先 `sudo docker compose build reseed-orchestrator`
#     （或改 `IMAGE=` 指向任何一个自带 python3 的镜像）。**没 build 就跑，
#     报错是 pull 失败 —— 一条"看着像网络/权限问题"的路。**
#
#   build 出来之后，这份包装确实**不需要自己的 Dockerfile**：
#     它自带 python3 与 `/bin/sh`；代码是**挂进去**的（见挂载 ①），不靠 `COPY`。
#     唯一的坑是它的 `ENTRYPOINT ["python","-m","orchestrator.main"]` —— 所以这里
#     用 `--entrypoint sh` 盖掉，否则参数会被接到 `orchestrator.main` 后面去。
#   ★ 这条 `--entrypoint sh` 成立的前提是镜像里有 `/bin/sh`：`python:3.12-slim`
#     （Debian 底）有；**换任何 alpine/distroless 系镜像都要重新确认**，
#     而没有 sh 的表现是 create 阶段直接失败，倒还好 —— 不会静默。
#
# ★★ PyYAML 那条**不用再查了**（2026-09-13 查过并结案）：
#   镜像里**有** PyYAML（Dockerfile 的 `pip install -r requirements.txt` 是 build 期
#   保证），但**这个结论其实用不上** —— 更硬的理由是
#   **`drive-loop.py` 的整个 import 闭包是纯标准库**（`orchestrator/__init__.py`
#   无 import；`orchestrator/state.py` 也只 import 标准库）。
#   于是它**换任何自带 python3 的镜像都成立**，不绑定 `reseed-orchestrator`。
#   PyYAML 只被 `orchestrator/config.py` 用，那条路径本包装不走。
#   ⇒ 下次别再问「镜像里有没有 PyYAML」；问「import 闭包里非标准库的有没有」，
#     而这个答案是**空集**，且不随镜像变。
#
# ★ 为什么不用 `docker run --network A --network B` 一次写完
#   `docker run` 的 `--network` 在旧版 Engine 上**只认最后一个**，而且**不报错**
#   （正是本项目最怕的静默降级）。重复 `--network` 是较新版本才有的能力。
#   本机碰不到 NAS 的 docker（SSH 关着），**版本这一条我没有实测**，
#   所以这里用 `create → network connect×2 → start` —— 它在所有版本上都成立，
#   代价只是多两行。★ 若哪天确认了 Engine ≥ 25，可以化简，但**没必要**。
#
# ★★ 若最终决定采用，「首选写法」其实是做成 `compose.yaml` 的一个服务
#   （网络与卷由 compose 统一声明，不用 create/connect 绕，也不用在这边复述挂载清单），
#   届时本包装退化成一句 `docker compose run --rm drive-loop`。
#   现在**不直接改 `compose.yaml`** 的理由只有一个：它是受管集合里的生产文件，
#   改它 = 改生产。草案先在受管集合外待着。
#
# ★★ 还没验的三件事（别把这份草案读成"已经跑通过了"）
#   ① 容器里**实际跑一遍**没做过 —— 以上全是静态对账（读 NAS 上的 `run.sh` /
#      `compose.yaml` + 本地代码）。本机不能在 NAS 上执行命令。
#   ② `--once` 闸门**跨容器**是否可靠：代码上是成立的（批次收尾时
#      `write_state({"running_pid": None, ...})` 把 pid 置空，下一容器 `pid_alive(None)`
#      直接 False），但**没在真容器里连跑两轮验过**。被强杀（无 finally）时残留 pid
#      在新容器的 pid 命名空间里可能"恰好存在" —— 兜底是心跳过期 10 分钟后接管，
#      代价是最多多等 10 分钟。
#   ③ 挂载整个 compose 目录后的**属主/权限**：宿主那份 `run.sh` 由 DSM 以 root 跑，
#      所以容器这边也**不要**加 `--user`（混用会让 `.state` 文件出现 root 属主、
#      另一边写不进去）。现在两边都是 root，一致。
#   ④ **镜像能力未验** —— build 出来的镜像里这些外部命令在不在，没实测过，
#      而它们**全部在关键路径上**，缺一个就静默退化：
#        · `python`     —— 已设 `PY=python`（默认值 `/usr/bin/python3` 是 DSM 自带，
#                          镜像里没有）。★ 这条**必须验**，缺了就是「跑了但什么都没发生」。
#        · `sh`         —— 见上方 `--entrypoint sh`。缺了在 create 阶段就失败（响）。
#        · `find` `stat` `sed` `tr` `basename` `dirname` `head` `mkdir` `mv` `cp` `rm`
#                       —— `drive-loop/run.sh` 与 `build-farm.sh` 都在用。
#                          `python:3.12-slim`（Debian）这些都有，但**是"应该"不是"验过"**。
#      ⇒ 一条命令同时验完（只读，不起容器，不需要批次空档）：
#          sudo docker compose -f /volume2/docker_ssd/reseed/compose.yaml \
#               run --rm --entrypoint sh reseed-orchestrator -c \
#               'command -v python sh find stat sed tr basename dirname head mkdir mv cp rm'
#        ★ 输出应当**恰好 12 行**；少一行就是少一个命令 —— 而少的那一行，
#          正是"为什么这批跑完什么都没干"的答案。
# =====================================================================
set -eu

COMPOSE_DIR="${COMPOSE_DIR:-/volume2/docker_ssd/reseed}"
VOL1="${VOL1:-/volume1/video}"
IMAGE="${IMAGE:-reseed-orchestrator:0.1.0}"
#: 参照容器：**照它的网络接**。选它的理由是它本来就同时接在 reseed-net 与 qbit-net 上。
#: ★ 不要改成自己拼网络名 —— compose 会给没有显式 `name:` 的网络加 `<项目名>_` 前缀，
#:   而那个前缀取决于 compose 项目名（默认取目录名）。
#:   实测记录：`qbit-net` 在 compose.yaml 里写了显式 `name:`，所以它**是**字面量；
#:   而 `reseed-net` **没有** ⇒ 它的真实名字不是 `reseed-net`。
#:   「看起来像名字的字符串」和「名字」不是一回事，猜错的后果是连不上网、
#:   而连不上网的表现是 drive-loop 打不开 cross-seed ⇒ 又是一条"看着像别的问题"的路。
PEER="${PEER:-reseed-cross-seed}"
ATTEMPTS="$COMPOSE_DIR/drive-loop/attempts.log"

# ---- 前置：docker 在不在、参照容器起没起 ----
command -v docker >/dev/null 2>&1 || {
  echo "[!!] 找不到 docker。本脚本要在 **NAS 上以 root** 跑（DSM 任务计划）。" >&2
  exit 127
}
[ -f "$COMPOSE_DIR/drive-loop/run.sh" ] || {
  echo "[!!] 找不到 $COMPOSE_DIR/drive-loop/run.sh —— 受管集合同步过了吗？" >&2
  exit 127
}

# 照 $PEER 的网络接。拿不到就**当场退出**，别带着一个空网络列表往下跑。
NETS=$(docker inspect -f \
  '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' \
  "$PEER" 2>/dev/null || true)
if [ -z "$NETS" ]; then
  echo "[!!] 拿不到参照容器 $PEER 的网络（它没在跑？名字不对？）" >&2
  echo "     先看一眼： sudo docker ps --format '{{.Names}}'" >&2
  exit 1
fi

# ---- ★ 自证：.env 里的路径必须都落在 $VOL1 下 ----
# 这一条是**「1:1 挂载」这个说法成不成立**的判据，而且必须在**容器起来之前**判：
# 挂错了不是报错，是**建出一堆跨卷副本**（§9），等发现时数据已经多了一份。
# 只读 .env 里那三个**路径**键，失败时也只打路径 —— 不碰其余行（它们含凭据）。
# ★ 用「一条 grep 取完再判」而不是 `grep | while read; do ... exit 1; done`：
#   后者那个 while **跑在子 shell 里**，`exit` 只结束子 shell，主脚本继续往下跑
#   —— 一条"看着像校验、其实什么都没拦住"的路。
BAD=$(grep -hE '^(DATA_DIRS|LINK_DIR|FARM_SOURCES)=' "$COMPOSE_DIR/.env" 2>/dev/null \
      | sed 's/^[A-Z_]*=//' | tr ',' '\n' | grep -v "^$VOL1/" || true)
if [ -n "$BAD" ]; then
  echo "[!!] .env 里有路径不在 $VOL1 下 —— 挂载清单要跟着改（§9：硬链接不能跨卷）：" >&2
  printf '%s\n' "$BAD" | sed 's/^/       /' >&2
  exit 1
fi

# ---- 起容器：create → connect×N → start ----
# ★ 名字带 $$：留着上一次被强杀的同名容器会一直挡住新的（而"挡住"表现为
#   drive-loop 静默不跑，不是报错）。
CID=$(docker create \
  --name "drive-loop-once-$$" \
  --entrypoint sh \
  -e PY=python \
  -e SUDO= \
  -v "$COMPOSE_DIR:$COMPOSE_DIR" \
  -v "$VOL1:$VOL1" \
  "$IMAGE" \
  "$COMPOSE_DIR/drive-loop/run.sh" \
    --url "http://cross-seed:2468" \
    --qbit-url "http://qbittorrent-reseed:3060" \
    "$@")

#: 无论成败都要收掉容器。★ `--rm` 在 create/start 这条路上不一定生效，
#:   所以自己兜一道 —— 万一这次失败，残留的容器会带着**同一个名字**一直占着。
cleanup() { docker rm -f "$CID" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM

for n in $NETS; do
  docker network connect "$n" "$CID"
done

# ★ 跟 run.sh 一样在 **shell 这一层**留痕：python 自己起不来时（镜像里没有 python、
#   注解求值报错）logging 什么都没配，`drive-loop.log` 一个字节都不会写 ——
#   表现是「跑了，但什么都没发生，也没有任何报错」。四个信号的读法见 run.sh 头部。
#   打 `[docker]` 标记，是因为切过来之后这份文件与宿主那份**同名同路径**。
echo "[$(date '+%F %T')] start [docker] py=python img=$IMAGE" >> "$ATTEMPTS"

RC=0
docker start -a "$CID" || RC=$?

echo "[$(date '+%F %T')] exit=$RC [docker]" >> "$ATTEMPTS"
exit "$RC"
