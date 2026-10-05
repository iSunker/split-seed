# 大包拆包 · 单种保种工具链（v1）

把一个正在做种的**多合一大包**（如 `DouBan_IMDB.TOP250.Movies.Mixed.Collection.20240501.FRDS`），
**零磁盘开销**地让其中每一部单片，也在隔离的第三实例 `qbittorrent-reseed`(:3060) 上以**单种**做种。

```
大包(主QB做种)                   ┌──────────── Prowlarr(把你的PT站→Torznab) ──[可选]FlareSolverr
 /volume1/video/...              │  ▲ 搜索
        │  dataDirs               │  │
        ▼                         │  │
   cross-seed ───按内容匹配单种(名称→搜索→文件名+大小校验)───┘
        │  命中：在 linkDir 里按"单种发布名"建硬链接(零磁盘)
        ▼  注入 + 触发校验(recheck)
   qbittorrent-reseed :3060  ──校验通过→ 单种做种 ✅
        │
        ▼ (本次不做，已预留)
   IYUU 读 :3060 的 InfoHash → 扩散到更多站点
```

> **为什么不用 IYUU 起步**：IYUU 按 InfoHash 匹配，而单片"藏在大包这个整种里"，大包 InfoHash ≠ 任何单种，**IYUU 无法从大包引导出单种**。必须先用**按内容匹配**的 cross-seed 拿到"第一个单种"，之后才轮到 IYUU 扩散。

本次范围 = 做到"**单种在 :3060 成功做种**"。IYUU 扩散、别的大包，均**已预留接口**、本次不实现。

> ★ **2026-10-05：`deploy.sh` 已删除。** 部署模型改为「**代码走 Docker Hub 镜像**
> （本机 build + push，NAS `docker compose pull`）+ **compose/配置经 SSH 直推**（NAS 无 SFTP）」。
> 受管文件登记在 `scripts/diag/check-deploy-drift.py` 的常量 `DEPLOY_MANIFEST` 里。

---

## ⚠ 免责声明与使用边界

> **先读这一节再往下。** 这套工具链会**自动建立链接、向 qB 注入种子**，
> 因此**可能改写你磁盘上的文件**（见「源文件被写穿」），也会**以你的身份向 PT 站发搜索**。
> 用之前请确认你清楚下面每一条。

### 本工具链**只**做什么

| ✅ 会做 | ❌ 不做 |
|---|---|
| 按内容匹配，**建立链接**（硬链接 / reflink）指向你**已有的**文件 | **不下载、不生成、不修改、不伪造**任何媒体数据 |
| 向 qB 注入 `.torrent` 并**触发 recheck** | **不参与**「这份数据到底对不对」的判断 —— 那是 qB 的 piece 校验 |
| 按**你给的站点和节奏**发 Torznab 搜索 | **不绕过**站点规则，**不伪造**进度 / 上传量 / 分享率 |

★ **一句话**：本项目是**搬运工**，不是**生产者**。它把"你手上已经有的"数据**换个目录再做一份种**
（判据是**字节一致**，见「原理 C」）—— **它没有任何一条代码路径能造出、或掩盖一份不存在的数据**。

### 「不完整做种」不是「假种」—— 判据是「**有没有骗人**」

| 维度 | ✅ 真种（完整做种） | ⚠ 不完整做种（缺种） | ❌ **假种** |
|---|---|---|---|
| qB 显示进度 | **100%** | <100%（常见 99.9%） | ★ **也可能显示 100%** |
| piece 校验 | **全部通过** | 有 piece **失败** | 数据与种子描述**根本不符** |
| 数据 | 与种子**逐字节一致** | **大部分正确**，缺的是少数 piece | 用**完全不同的**数据冒充 |
| 有没有骗人 | —— | **没有**（给出的有效数据是真的） | ★ **有** —— 故意提供与描述不符的数据 |
| 性质 | 正常做种 | **合法但不完整** | **欺诈** |

★ **两者的判据完全不同，别混着用**：
- 判「是不是**完整**做种」→ 看 qB 的**进度列**（任何 <100% 就是）；
- 判「是不是**假种**」→ ★ **进度不是判据**（假种可以显示 100%）—— 要看**数据来源是否可信**。

> ★★ **本项目可能产生「不完整做种」，但永远不会产生「假种」** —— 这两个词在本项目里
> **必须分开用**。「不完整」是**上游 partial 匹配的已知副作用**（见下）；
> 「假种」是**人的行为**，本项目既不提供、也不容忍。

### 本项目的产物**确实**是「不完整做种」—— 这是已知且有实测的

cross-seed 的 `partial` 匹配会把「**名称+大小匹配、但 piece 哈希不一致**」的单种也注入 qB；
qB recheck **不通过时不报错**，而是**就地重下**那几个 piece。上游文档把这个现象写成正常行为：

> Nearly all partial matches recheck to **99.9%** rather than 100% *(expected due to piece hashing)*

★ **它的危害不是"假"，是"占位不产出"**：这类种子**不产生上传量**，
长期挂着会踩站点的 **HnR（做种时长 / 分享率不达标）** —— 那是**你**的账号风险。
**本项目已把闸门关上**（`matchMode` ⇄ `linkType` 结构性互锁：只有 reflink 才许 partial），
**但改动前已建的存量仍在**，见「源文件被写穿」第 2 步。

### 你**自负**的后果

| 事项 | 说明 |
|---|---|
| **站点规则** | 多站重复做种是否允许、HnR 怎么算、分享率怎么保 —— **各站规则不同，本项目不替你判断** |
| **搜索压力** | 每站 **14 天一轮**的机器人流量（见「原理 B」）—— 这是**设计时就知道的代价**，请用对方站点的规则去衡量它 |
| **磁盘占用** | reflink 重下分离出的块**要占真实空间**；本项目这块 `/volume1` **已用满 100%** |
| **数据不可逆** | 被写穿的文件**无备份可恢复**（本项目已实测：`net view` 无任何备份共享、4 处 `#snapshot` 全不存在）⇒ 只能**回站点重新下** |
| **凭据** | PT 站凭据**只进 Prowlarr**，不进本仓库；泄了是你自己的运维问题 |

> ★★ **若你打算用它做「假种」、刷流量、或拿别人的数据冒充 —— 请立刻停手。**
> 本项目**没有**、也**不会**提供这类能力。任何以此为目的的使用**与作者无关**，
> 后果**由行为人自负**；也请勿据此向作者追责。

---

## 我要做什么 → 看哪节

| 我想… | 看哪节 |
|---|---|
| **我要拿去用了 —— 先看责任边界** | **⚠ 免责声明与使用边界**（只搬运不生产 · 真种/不完整/假种的**分野** · 你自负的后果）|
| 搞清这东西干嘛的 | 上面这段 + **目录结构** |
| **搞清「包」是怎么被认出来的 / 搜索压力从哪来** | **`ENVIRONMENT.md` `A.15`**（包是**声明**的不是识别的 · 压力来自**分母** · 风险清单）→ SUMMARY §19 |
| 从零部署一台 | **部署步骤**（Phase 0→3，每步可独立验证）+ **验证清单** |
| **动手前：这件事现在允许做吗 / 改动检查过了吗** | **开工前的两道门** ← 动作→允许条件表 + 9 格改动前自检 |
| 跑起来 / 继续跑 | **`ENVIRONMENT.md` `A.19`** ← 最常用，先看这个 |
| **新会话开局 / 不了解这台机器** | ★ **先读 `CLAUDE.md`（唯一入口：开局三步 + 真源表）** → 再由它引到 **`ENVIRONMENT.md`**（环境前提 + 按症状 grep 的 `ERR-*`，含全部 20 项技术弯路）与 **`summary/26`**（干到哪了）|
| 我卡住了（报错 / 搜不到 / 不动了） | **常见问题** + **`ENVIRONMENT.md` `A.19.1.9`**（交接必读的坑）→ 该文件的 `ERR-*`（按症状检索）|
| **想从电脑上手动跑点什么** | **`ENVIRONMENT.md` `A.19.1.8`** ← 电脑只剩诊断用途（**都在 `scripts/diag/`**）：`check-deploy-drift.py`（查漂移）、`scan-secrets.py`（推前扫凭据）、`audit-found-*.py`（对账 `Found` 行） |
| **想知道 NAS 上有没有我不知道的文件** | **`ENVIRONMENT.md` `A.17.1`**（漂移哨兵）← `python scripts/diag/check-deploy-drift.py` |
| **想推代码，但怕把密钥一起推上去** | **`ENVIRONMENT.md` `A.17.2`**（推前凭据扫描）← `python scripts/diag/scan-secrets.py`（绿了再 `git push`） |
| 加站 / 换站 | **多站点** → SUMMARY §13.3（完整流程，可复用） |
| 让它出事了主动通知我 | ★ **本仓已不含通知写侧**（2026-10-05 砍掉）—— 见**独立通知项目**；链路见该项目的 `ARCHIVE-A18-reseed通知链路.md` |
| **下一步该做什么** | **`summary/26-1-主题索引与旧会话清场.md`** 的 `§26.2`（主表）+ `§26.2.3`（三张总表） |
| 下一步还能自动化什么 | **SUMMARY §16**（四条预案 + **两条前提被推翻**；原「还没做」第 7 条，2026-09-19 改指） |
| **判「能不能建硬链接」/ 某共享在哪个卷 / 某共享 SMB 里「看不见」** | **SUMMARY §23**（`docker_ssd ∈ /volume2` 的双来源复核 · ★★ **`stat -c %d` 只在 NAS 侧成立**，Windows/SMB 侧零分辨力 ⇒ 对照量用 `df` · **枚举 ≠ 全量**）· 判据本体在 `ENVIRONMENT.md` 的 `A.3` |
| 状态机说没做种、qB 里明明在做种 | **常见问题** → **SUMMARY §17.5.1**（已修） |
| 接手这个项目 | **`ENVIRONMENT.md`** + **SUMMARY §13**（全过程 + 坑单）→ **§13.11**（最新进度）；待办见 **§26.2** |

> **文档分工与优先级：见 `CLAUDE.md` 的真源表**（唯一完整版 —— 这里不复述，免得两处打架）。
> 本文自己这一侧：**本文件 = GitHub 门面** —— 只留「**怎么用**」（免责声明 · 目录结构 · 部署步骤 · 验证清单 · 常见问题），
> 通篇是 `YOUR-NAS` / `NAS_IP` / `SiteA` 占位符，**可以安全公开**。
> ⇒ **运维事实**（真实路径 · 调度 · 日报读数基线 · 状态机语义 · 通知链路 · 农场 · 漂移哨兵 ·
> 凭据扫描 · 当前状态）**在 `ENVIRONMENT.md` 的 `A.13`–`A.19`**（那里通篇是真实值，**别公开贴出**）。
> ★ **新会话一律从 `CLAUDE.md` 进门**（那里有真源表，别在这里找）。
> ★ `SUMMARY` / `summary/` 的正文 2026-09-16 起**已按章拆到 `summary/`** —— 入口 `SUMMARY.md` 只剩导航
> +「§N → 文件」表，所以下面这些 `§N` 引用**照旧有效**；要读哪章就打开对应小文件，不必整份吞。
> ★ 原 `走过的弯路.md`（20 项技术弯路）已于 2026-09-13 **全部并入 `ENVIRONMENT.md` 并删除**，
> 文件名不再存在；要按历史编号找，用 `grep -n '弯路 #' ENVIRONMENT.md`。

---

> **占位符约定**：为便于公开分享，文档与脚本里的真实主机名 / 内网 IP / 私有站名
> 已替换为 `YOUR-NAS`、`NAS_IP`、`SiteA`、`SiteB`。照着做时请换成你自己的。
> 脚本没有硬编码这些值 —— `scripts/diag/run-batch.sh` 读环境变量，
> 或读 `scripts/.nasrc`（已 gitignore）。完整对照表见 SUMMARY.md §0。

---

## 目录结构

```
reseed/                    # NAS 部署目录（2026-10-05 由 prowlarr_cross-seed_autohardlink 改名）
├─ compose.yaml           # ★唯一的 compose（prowlarr + flaresolverr + cross-seed + reseed-orchestrator
│                         #   + drive-loop + autoheal + watcher）；容器相关目录都收进 data/
├─ .env                   # 由 .env.example 复制而来；IP/密钥/路径/开关（单点维护）
├─ .env.example
├─ data/                  # ★容器相关的**全部**内容（2026-10-05 收拢）
│  ├─ cross-seed/
│  │  ├─ config.js        # cross-seed 配置（从 .env 取值；含版本免责说明）→ 挂进 /config
│  │  └─ wait-for-qb.sh   # 启动闸门：先等 qB 就绪**且能登录**，再 exec daemon → /config
│  ├─ prowlarr/           # Prowlarr 配置目录（首次启动自动写入）→ 挂进 /config
│  ├─ hlink/
│  │  └─ config.yml       # ★大包任务 + 全局设置（你主要编辑这个）→ 挂进 /config
│  ├─ orchestrator/       # 编排器(Python, 仅依赖 PyYAML) 源码 + Dockerfile：预检/驱动/汇报
│  │  └─ state.py         # ★单片状态机（sidecar 库，只读 cross-seed/qB，见「扩展」）
│  ├─ drive-loop/         # drive-loop 容器（代码**全在镜像里**，见 docker build 一节）
│  │  ├─ Dockerfile       # 镜像定义（构建上下文 = 仓库根）
│  │  ├─ run.sh           # 宿主 `--once` 入口（DSM 任务计划调，手工放 NAS）
│  │  ├─ run-resident.sh  # 容器内常驻入口（build 进镜像 = /app/run-resident.sh）
│  │  ├─ scripts/         # drive-loop.py / reseed-state.py / build-farm.sh（build 进镜像）
│  │  └─ state/           # ★运行态挂载卷：6 个 .state + attempts.log + drive-loop.log
│  │                      #   + hlink/state.db（容器内 = /state）—— 不进 git、不进镜像
│  └─ scripts/diag/       # watch-containers*.sh（watcher 容器读）
├─ patches/reseed-qbit.conf.md   # Phase 0：:3060 需改的配置项（参考文档）
├─ ENVIRONMENT.md         # ★环境前提与坑手册（检索层：A 部分前提 + B 部分 `ERR-*` 条目）
└─ README.md
```

> ★ 2026-10-05 结构变更（`重构方案.md` §8）：容器相关文件全部收进 `data/`，`data/` 平级
>   只留**一个** `compose.yaml`；**代码只进镜像**（本机 build + push，NAS pull），
>   compose/配置走 **SSH 直推**。规则见 `.claude/rules`。


> 开发仓库里另有 `scripts/`（`run-batch.sh` 抽样脚本、`reseed-state.py` 状态机 CLI、
> `drive-loop.py` 自动续跑、`add-indexers.py` 加站、`gen-datadirs.py` 生成嵌套包的 `DATA_DIRS` 片段、
> `build-farm.sh` 构建硬链接农场〔NAS 本机**或** Windows 经 SMB 都能跑，见「扩展 → 硬链接农场」〕、
> `migrate-reseed-dirs.py` 目录搬迁（qB `setLocation`）+ `wait-for-checks.py` 等它校完
> 〔2026-09-12 新增，见 SUMMARY §18〕、`add-torznab-indexer.py` 往生产 `.env` 加索引器
> 或摘索引器（`--remove`，换站用〔apikey 从同文件现有条目**原样抄**，不经过人眼〕）、
> `check-indexer-timestamps.py` **只读**查「哪个站真的搜出去过 + 到底有没有在被限流」
> 〔加站流程第 ③ 步的闸门，见 SUMMARY §18.11〕、
> `prowlarr-indexerstatus.py` **只读**查「Prowlarr 是不是**在本地禁用**某个站」
> 〔`ERR-SVC-17` 里三种限流机制的分辨器，与上一条是**一对**：一条看 Prowlarr 侧、
> 一条看 cross-seed 侧；key 从生产 `.env` 读、**绝不打印**，站名只取
> `cross-seed.db` 的 `id`/`name` 两列〕、
> `check-deploy-drift.py` **只读**的**漂移哨兵**（NAS 上有没有没登记的文件 / 仓库里有没有
> 该部署却没进受管集合的已跟踪文件，见「漂移哨兵」一节，SUMMARY §18.14）、
> `scan-secrets.py` **只读**的**推前凭据扫描**（按值的形状找漏进仓库的 cookie / apikey /
> passkey，见「推前凭据扫描」一节，SUMMARY §18.15）、
> `audit-found-lines.py` / `audit-found-resolve.py` **只读**的两道**对账**
> （`Found` 行：a−b 验「抓到的形状 = 正则认的形状」，b−c 验「抓到的行真的落到了某个单片」；
> 直读 NAS 的 `info.current.log` 与 `state.db`〔后者 `query_only` 硬闸〕，见 SUMMARY §18.18）、
> `sa-volume-usage.py` **只读**群晖 Storage Analyzer 的报告
> 〔内存里解 zip、不落盘、无凭据；它留着的是**一句否定结论的证据** ——
> 报告里**没有可用空间**，见 SUMMARY §22 与「🔴 下一步」第 14 条〕、
> `check-datadirs-names.py` **只读**的**「名字挡板」**（查 `DATA_DIRS` 里有没有
> **分类目录名** —— `儿童` / `欧美剧` 这种**不是发布名**的目录，见下节
> 「★★ 往 `DATA_DIRS` 里加路径」；清单在 `scripts/diag/datadirs_rules.py`）。
> 其余工具要么跑在 NAS 上，要么是**手动**用的（不必进容器）。
> ★ 2026-09-12 起电脑端只留 **`check-deploy-drift.py`**（查漂移）、
> **`scan-secrets.py`**（推前扫凭据）、**`audit-found-*.py`**（对账）、
> **`prowlarr-indexerstatus.py`** / **`check-indexer-timestamps.py`**（查限流是哪一种）几个用途，
> 见「⛔ 电脑端已不参与」。
> `tests/` 是**离线自测** —— ★ **脚本数与断言数只维护在 `tests/README.md`，别在这里抄第二份**
> （抄过一次就漂了）。当前数字看那份的生成区。
> 原先散在 `D:/tmp` 里**没有版本管理**，2026-09-12 搬进仓库。不联网、不碰生产、不碰真库，
> `python tests/<名字>.py` **任一 cwd** 都能跑（路径按 `__file__` 解析），全过退出码 0。
> 见 `tests/README.md`。
> 由状态机导出的 `unmatched.tsv` /
> `todo-paths.txt` 属运行时产物，已 gitignore。

三大服务的配置目录一一对应：`prowlarr/`→Prowlarr、`cross-seed/`→cross-seed、`hlink/`→编排器。
`.env` 是**唯一密钥/IP/路径来源**：`hlink/config.yml` 用 `${VAR}` 引用它，`cross-seed/config.js` 从环境变量读它。三处保持一致，只需改 `.env`。

> 注意区分：`hlink/` 只放编排器的**配置/日志**；真正的硬链接（做种数据）在 `.env` 的 `LINK_DIR`（须在 `/volume1/video` 下，与大包**同卷**，不是 `docker_ssd`）。

---

## 开工前的两道门（动手前必读）

> 这一节是**闸**，不是资料 —— 它只回答两个问题：**这件事现在允许做吗**、**这个改动检查过了吗**。
> 每条的判据本体在 `ENVIRONMENT.md`（动作边界 `A.2`、判据约定 `A.11`，与对应的 `ERR-*`；按症状 grep）—— 这里只是把它收成一张动手前扫一眼的表。

### ① 动作 → 允许条件

**`批次间隙`** = `.drive-loop.state` 的 `running_pid` 为 `null`。

| 动作 | 允许条件 |
|---|---|
| 只读 SMB / 只读 HTTP 端口 | 任何时刻 |
| `deploy.sh --apply`（已删，历史动作）| ~~批次间隙~~ —— 该动作已随 `deploy.sh` 删除；改配置现走 SSH 直推 |
| `docker compose up -d --force-recreate` | 批次间隙 **+ 该重建确有必要** |
| 改 `.env` 的 `DATA_DIRS` / `LINK_DIR` | 用 `gen-nas-env-update.py` 生成脚本，**不手敲** |
| `build-farm.sh --apply` | 批次间隙（要写 NAS）|
| 手动 `rm` NAS 上的东西 | 只走 `rm-staging.sh`（有闸），**绝不 `rm` UNC 路径** |
| qB 的 `setLocation` | **一站一批**（按 `save_path` 分组、一批一次调用），且种子不在在途状态 |

**在途状态**（`migrate-reseed-dirs.py` 的 `IN_FLIGHT_STATES`）：`checkingDL` / `checkingUP` / `moving` / `allocating`。

> ★ **`running_pid` 是快照，不是窗口** —— 它 `null` 只说明**这一刻**没在跑，**不说明能安全多久**。
> 循环每 15 分钟醒一次（`last_sleep_sec` 常见 `10800`）⇒ **窗口长度要看它，不是看这一刻**。
> ★ 状态文件在 **NAS** 上（`<compose>/drive-loop/scripts/.drive-loop.state`），**不在本机仓库里** ——
> 本机看不到它，就别假装读过。

### ② 改动前自检（9 格，逐格填，不许"全绿了就算过"）

```
□ 这个改动落在哪一层？（构建上下文 / NAS 宿主机 / 容器内）
□ 需不需要 deploy / --build / --force-recreate？
□ 期望值的另一侧是谁？能指回哪条真实记录？
□ 阴性对照做了吗？打断代码测试会红吗？
□ 部署窗口对吗？批次在跑吗？
□ 回读证据准备好了吗？（md5 / 符号 grep / docker inspect）
□ 有没有把"没查到"写成"查到了没有"？
□ 有没有构造出"看着合理其实无意义"的数？（`.get() or 0`）
□ 这份输出会不会在别处被照抄？（措辞一旦成断言就成假事实）
```

★ 最后一格有**凭据版**：脱敏要按**值形状**兜底 —— `apikey=` 会出现在 URL 的**值**里，
按**键名**兜不住；`cut -c1-N` **不是**脱敏。

---

## ★★ 往 `DATA_DIRS` 里加路径（**先读这节**）

> `cross-seed` 把 `dataDirs` 的**直接子目录**当作 searchee，并**拿子目录名**去站点搜索。
> ⇒ 你指到哪一层，**那一层的目录名就会被当成发布名拿去搜**。

★★ **有一条实测过的静默失败**（`§26.58` 三 / `§10.2`）：`TV/` 里混着 `儿童` /
`欧美剧` 两个**分类目录**（不是发布名）。一旦它们进了 `DATA_DIRS`：

- **搜不到任何东西** —— 白耗站点查询额度；
- ★★ **日志一切正常** —— 那个"包"**永远不被搜**，你不会收到任何报错（`§19.1`）。

### 规矩：**分类目录名不许进 `DATA_DIRS`**

现存的**排除清单**（`scripts/diag/datadirs_rules.py`，想加一条先读那个文件）：

| 不许用的名字 | 为什么 |
|---|---|
| `儿童` | 分类习惯，不是发布名（实测真空，`§26.58` 三）|
| `欧美剧` | 同上 |

★ 判据是**整名比对**（`基名 == 清单项`），**不是子串包含** —— 所以
**`儿童医院.S01.2026.1080p.WEB-DL.H.264` 这种真发布名会被放过**，别怕误杀。

### 怎么加（三道挡板，**别绕**）

```bash
# ① 先看某一层有哪些目录（★ 对着盘上的真东西看，别凭记忆）
python scripts/diag/gen-datadirs.py "//<NAS>/video/download/TV" --level 2 --list

# ② 生成片段 —— ★ 若这一层含分类目录名，会**直接拒绝**（rc=3）并告诉你换哪层
python scripts/diag/gen-datadirs.py "//<NAS>/video/download/TV" --level 2 \
    --nas-prefix /volume1/video

# ③ 事后兜底校验（改完 .env 随时可跑；只读）
MSYS_NO_PATHCONV=1 python scripts/diag/check-datadirs-names.py
```

★ 退出码：`gen-datadirs.py` 拒生成 = **`3`**；`check-datadirs-names.py` 命中 = **`1`**
（逐条判据被测试钉住：`tests/COUNTS.json::notes[test_datadirs_names.py]`）。
★ 本机（Git Bash）跑校验器**必须带 `MSYS_NO_PATHCONV=1`** —— 否则以 `/` 开头的参数
会被改写成 `C:/Program Files/Git/…`，**打印出来的路径不是你的输入**
（同上锚）。

> ★ **为什么这条"生成侧也挡"很重要**：原先挡板只在**事后**的校验器里
> ⇒ 正确顺序「先 `--list` 看一眼、再粘进 `.env`」中，那个**看一眼的工具本身不挡**，
> 而校验器**没人保证会跑** ⇒ 等于**一半的护栏**（`§26.63` A 格①）。
> 现在生成侧在**吐出来之前**就拒绝。

---

## 部署步骤（分阶段，每步可独立验证）

> 前置：把本目录内容整个放到 NAS 部署目录 `/volume2/docker_ssd/reseed`，`cd` 进去执行下述命令。
> `cp .env.example .env`，然后编辑 `.env`：至少填 `NAS_IP`、`CROSSSEED_API_KEY`、确认 `DATA_DIRS`/`LINK_DIR` 路径。
> 确保 `LINK_DIR` 与 `DATA_DIRS` **在同一物理卷**（都在 `/volume1/video` 下即可）。

### Phase 0 — 修 `:3060`
按 [`patches/reseed-qbit.conf.md`](patches/reseed-qbit.conf.md)：停容器→备份 conf→改 BT 端口 56883、开局域网免密白名单、关 DHT/PeX/LSD、关队列、注入不暂停→启容器。
验证：`curl http://<NAS_IP>:3060/api/v2/app/version` 免密返回版本号。

### Phase 1 — Prowlarr 加站
```bash
docker compose up -d prowlarr flaresolverr   # 不需要过CF可省略 flaresolverr
```
1. 打开 `http://<NAS_IP>:9696`，把目标 PT 站加进 Indexers（填各站 cookie/passkey/API；中文站若卡 Cloudflare，在 Settings→Indexers 里配 FlareSolverr = `http://flaresolverr:8191`）。
2. 每个站页面找 **Torznab Feed / API** 的地址与 apikey，拼进 `.env` 的 `TORZNAB_URLS`（逗号分隔），如
   `http://prowlarr:9696/1/api?apikey=xxxx,http://prowlarr:9696/2/api?apikey=yyyy`。
3. 在 Prowlarr 里对**一部电影名**手动搜一下，确认至少一个站能出结果（这步验证站点可搜性）。

### Phase 2 — cross-seed 试点（关键闸门）
```bash
docker compose up -d cross-seed
docker compose logs -f cross-seed        # 看有无 "unknown option" 等版本键名报错
```
先对**大包里 1~2 部电影**手动触发匹配（把 `<某电影目录>` 换成 `DATA_DIRS` 下真实子目录）：
```bash
curl -XPOST http://<NAS_IP>:2468/api/webhook \
  -H "X-Api-Key: <你的 CROSSSEED_API_KEY>" \
  --data-urlencode 'path=/volume1/video/download/movies/DouBan_IMDB.TOP250.Movies.Mixed.Collection.20240501.FRDS/<某电影目录>'
```
> webhook 路径/参数名随 cross-seed 版本略有差异；若 404/400，见 `cross-seed/config.js` 顶部说明与官方文档，并对照 `orchestrator/crossseed_client.py` 的注释调整。

验证（[验证清单](#验证清单)）：`linkDir` 下出现该电影的硬链接、且 `:3060` 里该单种在校验后做种。
**若中文站匹配率过低**，问题会在这里暴露 → 换站 / 加 FlareSolverr / 调 `MATCH_MODE`，再决定是否全量。

### Phase 3 — 编排器（我们的程序）
```bash
docker compose build reseed-orchestrator
docker compose run --rm reseed-orchestrator preflight                       # 只读预检
docker compose run --rm reseed-orchestrator run --job frds-top250-2024 --dry-run  # 只枚举不触发
docker compose run --rm reseed-orchestrator run --job frds-top250-2024      # 正式：逐单片触发→注入→等待
docker compose run --rm reseed-orchestrator status                          # 按分类汇总做种状态
```
编排器做：安全预检（源存在/同卷/单片枚举）→ 逐单片驱动 cross-seed → 轮询 `:3060` 汇报"哪些单片已做种 / 未匹配到"。

---

## 验证清单

1. **Phase 0**：`curl .../api/v2/app/version` 免密返回版本；`nc -vz <NAS_IP> 56883` 通。
2. **Phase 2/3**：硬链接生效（删单种不伤大包）——
   ```bash
   ls -l "<LINK_DIR>/<某电影>"/*.mkv   # 链接计数(第2列)应 ≥ 2
   ```
   且 `:3060` 该单种进度 100%、状态为做种（`status` 子命令里计入 `seeding`）。
3. **全量后**：编排器报告的做种数与 `:3060` 分类 `reseed-singles` 实际数量一致。
4. **日报「各包进度」带不带 ② 口径完成度**（Part B 的验收；`SUMMARY §26.15`）——
   ★ 这条**必须是可执行命令**，不许写成「看一眼日报」：后一种写法**没有判据形态**，
   而它正是 Part B 挂在「未验收」状态过了九次收口的原因。
   ```bash
   # ★ 2026-10-05：通知写侧已砍 ⇒ `notify/log/*.tsv` **不再产生**，这条命令已失效。
   #   现走容器日志（日报打进 drive-loop.log；容器里它在 /app/state/ 下）：
   docker compose --profile drive-loop logs drive-loop | grep -c packpct
   ```
   * `>0` ⇒ **生效**：日报正文里该出现 `（95%，74/78）` 这样的「百分比 + 未约简分数」，
     分母为 0 的包写 **`（n/a）`**（**不是** `0%`），末尾还有一行 `总计：…  ← 各包之和，非平均值`。
   * `=0` ⇒ **没生效**。★ 先别怀疑代码 —— 按 `§26.15` 查**容器到底读的哪份 `drive-loop.py`**：
     `drive-loop.py` **在镜像里与挂载里各有一份**，只有镜像那份会随 `docker build/push` 更新
     （旧文档里「随 `deploy.sh` 更新」的说法已随 `deploy.sh` 删除而作废）。
     ```bash
     # 判据只比 drive-loop.py —— ★ 2026-10-05：`notify.py` 已迁走（不在镜像里），
     #   旧判据 `md5sum /app/scripts/notify.py` 会因文件缺失而恒空 ⇒ 已换成本文件自比。
     docker compose --profile drive-loop exec drive-loop sh -c 'md5sum /app/scripts/drive-loop.py drive-loop/scripts/drive-loop.py'
     ```
     两行 **md5 相同** ⇒ 镜像不旧；**不同** ⇒ 镜像旧，**必须重建镜像**（`docker build` + `push` + NAS `pull`，SSH 直推配置治不了）。
     ★★ **`drive-loop.py` 不比 md5** —— 它的真源是**镜像里那份**（`run-resident.sh` 里
     `SCRIPT="$APP_DIR/scripts/drive-loop.py"`，`APP_DIR` 指向镜像 `/app`；compose `entrypoint` =
     `sh /app/run-resident.sh`，**不再从挂载区读**）。镜像里那份是 **CRLF**，若挂载区另放一份会被剥成 LF
     ⇒ **两份 md5 永远不等**。拿它当判据 = **恒假的「狼来了」**
     （平时假红、真出事也红，没人分得清）。详见 `SUMMARY §26.56` 四。
     `run-resident.sh` 已在启动时自检：读到不含 `PHASE_IDLE` 的旧代码会**退 3** 并在 `attempts.log` 留一行。

5. **「装不出来的单种」那一段有没有真进日报**（`SUMMARY §27`）——
   ★ 同样是**可执行命令**，不是「看一眼」。
   ```bash
   # ★ 2026-10-05：通知写侧已砍 ⇒ `notify/log/*.tsv` 不再产生，改看容器日志：
   docker compose --profile drive-loop logs drive-loop | grep -c 未完成且停滞   # 应 > 0
   docker compose --profile drive-loop logs drive-loop | grep -o 'fz_total=[0-9]*'  # 应出现
   ```
   * 两条**都**成立 ⇒ 生效。★ **只看第一条会把「还没发」误判成「没生效」**
     （日报按**日历日**去重，`--force-recreate` 不影响它 —— 详见 `§26.15`）。
   * 要**当场**看是哪几条（只读，不碰 qB）：
     ```bash
     python scripts/diag/reseed-freeze-report.py            # 默认只打聚合
     python scripts/diag/reseed-freeze-report.py --show-hashes  # 12 位短 hash + 站点目录名
     ```
   * ⚠ 这三条种子**不要手动隔离**（不必限速 —— 它们本来就没有 peer，速率恒为 0）。
     ★★ **别去动它们的数据文件**：qB 只重下缺的 piece，所以源**目前还没被写穿**；
     一动才会真写穿源（`§27.2` / `§27.6`）。

---

## 常见问题

- **中文站搜不到 / 结果为空**：Prowlarr 里该站是否需要 FlareSolverr？passkey/cookie 是否过期？先在 Prowlarr 手动搜确认；再考虑放宽 `MATCH_MODE`。
- **命中率低、有些片子搜不到**：正常。**单站（sitea）已搜部分实测命中率约 50%**
  （87 部里 44 部；详见 SUMMARY §5.3）。想提高就**加索引器**，见下方「多站点」。
  当前匹配/未匹配的完整清单见 `scripts/unmatched.tsv`。
- **跑完一轮但感觉"漏了很多"**：先看进度条是不是 `(N/总数)` **一次性从某个数跳到总数** ——
  那不是跑完了，是**索引器被退避、剩下的条目被秒跳**了（cross-seed 跳过不排队）。
  去 Prowlarr 日志确认该站真实状态码：`500/502/520/522` = 站点侧故障，等它好了**重打一次根目录 webhook** 续跑；
  只有真 `429 + Retry-After` 才需要把 `delay` 往上调。详见 SUMMARY §5.3 / §6.5。
  **别靠肉眼判断漏了哪些** —— 用下面的状态机，它会精确列出"该搜但没搜"的清单。
- **某部片子确认没匹配上，怎么处理**：多半是**站点上还没有这个单种**（或只有别的分辨率/压制组），不是配置问题。
  **不要反复重打 webhook** —— cross-seed 有搜索缓存，短期内重复打基本是白打，还可能撞上 Prowlarr 的失败退避。
  正确做法：让它留在 `scripts/unmatched.tsv` 里，用 cross-seed 的 `searchCadence` + `excludeRecentSearch`
  做**低频自动重扫**（例如每周扫一轮、同一部 3 天内不重复搜）。等哪天有人发了对应单种，下一轮自然就命中了。
- **注入后一直"校验中"或校验失败**：单种发布名/文件大小与你的数据不完全一致属正常（换一个匹配结果）；确认 `LINK_DIR` 与源**同卷**、`/volume1/video` 已 1:1 挂进 cross-seed 与 `:3060` 两个容器。
- **cross-seed 启动报 `unknown option`/键名错误**：版本键名差异，按 `cross-seed/config.js` 顶部说明与官方文档改键名。
- **外网连不上 / 上传为 0**：路由器 56883 的 **TCP 和 UDP** 都要转发；确认 conf 三处端口都是 56883。
- **`drive-loop` 报「有 N 个 active 索引器拉不到名字（prowlarr#3）」**：
  这是**索引器拉的 caps 失败**（名字来自站点 caps，取不回来就没名字），
  `#N` 就是 `TORZNAB_URLS` / `/N/api` 那个号，**不是 Prowlarr 界面序号**。
  先去 Prowlarr 看这站还在不在、是否被禁用 —— 多半又是「`.env` 删了站但容器没重建」
  （见上面「交接必读的坑」），重建即可。SUMMARY §13.11 记了这个标签曾经写错的 bug。
- **`:3060` API 403**：白名单网段没覆盖来源 IP（改 `WebUI\AuthSubnetWhitelist`），或应改用 `QBIT_AUTH_MODE=password`。
- **`drive-loop` 回灌打出 `新增做种 -26`（负数）/ qB 里明明在做种，状态机却写 `MATCHED` 或 `UNMATCHED`**：
  状态机的 `SEEDING` 是**推导**出来的 —— `seeding_count = |matched_hashes ∩ qB 的 hash|`
  （`orchestrator/state.py:963`）。**`matched_hashes` 一空，stage 就必然降级**，跟 qB 通不通无关。
  **先别怀疑 qB**，直接两边数一下对照：
  ```bash
  curl -s "http://NAS_IP:3060/api/v2/torrents/info?category=reseed-singles" \
    | python -c "import sys,json;print('qB 在做种:',len(json.load(sys.stdin)))"
  python -c "
  import sqlite3;c=sqlite3.connect(r'//YOUR-NAS/.../drive-loop/hlink/state.db')
  print('状态机认的:', c.execute('SELECT COUNT(*) FROM movie WHERE seeding_count>0').fetchone()[0])"
  ```
  ★ **补救（幂等、不发任何站点请求）**：补跑一次带 `--qbit-url` 的 `sync`。
  ✅ **2026-09-12 上午已定位并修复**：真根因是 **v3 农场切换后 cross-seed 报的是农场路径**，
  而 `pack.roots` 还是原路径 → searchee 全被判成「别的包」**静默跳过** → `matched_hashes` 空。
  修法（`pack.farm_root` 列 + `reseed-state.py farm` 子命令）与验证（**`SEEDING` 21 → 201**）见 **SUMMARY §17.5.1**。
  ⚠ 光补跑 `sync` **治不了这个** —— 实测只从 13 回到 21。当时的误判过程见 §17.3。
- **`backoff_hits` 一直是 0，但索引器状态明明是 `RATE_LIMITED`**：
  ✅ 已修：加 `--backoff-check-secs`（默认 60）**按秒**触发检查，并按 `retry_after` 变化认定「新发生了一次退避」。
  相关取值：检查间隔 `--check-every` × `--interval` = 10 × 30s = **300 秒**，`--max-wait` **1800**。
  失效机制（窗口整段落在两次检查之间 ⇒ 既不等待也不计数）见
  `tests/COUNTS.json::notes[test_backoff.py]` / `notes[test_next_sleep.py]`；见 **SUMMARY §17.5.2~§17.5.4**。
- **`attempts.log` 里出现 `exit=127 (no python: /usr/bin/python3)`，但同一批的 python 明明跑完了**：
   **那是假象** —— 旧的 `deploy.sh`（**现已删除**）曾在脚本**运行中**覆盖它（`sh` 边读边执行，会从旧偏移读到新内容）。
   `deploy.sh` 当时已改成**原子替换**（写 `.new` 再 `mv`，换 inode），并在检测到批次在跑时给出告警；
   现在配置改走 SSH 直推，同样要求「**批次间隙**」才动会被 `sh` 读的文件。
  判据：**同一次 `start` 没有配对的 `exit`** 时，先怀疑"脚本被换过"，别急着信那个退出码。详见 **SUMMARY §17.2**。

---

## 扩展

### 多站点 —— 提高命中率的唯一手段

单站（sitea）已搜部分实测命中率约 **50%**，加站能把剩下那 50% 里的一部分也捞回来：

1. Prowlarr（`http://<NAS_IP>:9696`）→ Indexers → Add Indexer，把其它 PT 站加进来。
   > cookie / passkey / API **只在 Prowlarr 网页里填**，绝不写进任何文件、聊天或命令行。
2. 记下每个站的 **indexerId**（索引器详情页 URL 里的数字，如 `/indexer/3` → id=3）。
   > ⚠ **这个数字会变** —— 删站再重加，ID 往往就换了。**每次增删索引器后都要重新核对**：
   > ```bash
   > python scripts/diag/prowlarr-indexers.py --torznab
   > ```
   > ★★ **不要用 `curl ... /api/v1/indexer`**（判据与原因被测试钉住：
   > `tests/COUNTS.json::notes[test_prowlarr_indexers.py]`）。详见 `ENVIRONMENT.md` `ERR-SVC-02`。
   > 详见 SUMMARY §6.6。
3. 把它的 Torznab 地址追加进 `.env` 的 `TORZNAB_URLS`，**逗号分隔、单行**（`scripts/diag/add-indexers.py` 已自动化这步，会自动补 `/api`、只改一行、自动备份）：
   ```
   TORZNAB_URLS=http://prowlarr:9696/2/api?apikey=<Prowlarr API key>,http://prowlarr:9696/3/api?apikey=<同一个 key>
   ```
   > 用的是 **Prowlarr 自己的 API key**（所有条目共用同一个），不是各站的 passkey。
4. 重建 cross-seed 让新 env 生效 —— **必须 `--force-recreate`，`restart` 不够**（见下）。
5. 在 Prowlarr 里对一部电影手动搜一下，确认新站能出结果。
6. **加完站要重跑一轮全量**：对 `DATA_DIRS` 的**根目录**打一次 webhook（见 SUMMARY §6.4），
   cross-seed 会把整包重新遍历一遍。已经命中的会被跳过，只有没命中的才有机会被新站捞到。

> ⚠ **`docker compose restart` 不重新注入环境变量** —— 改了 `TORZNAB_URLS` / `DATA_DIRS` 后
> 必须 `sudo docker compose up -d --no-deps --force-recreate cross-seed`
> （`--no-deps` 防连带重建 Prowlarr）。完整加站流程见 **SUMMARY §13.3**。
>
> ⚠ **只在 Prowlarr 里禁用索引器 ≠ cross-seed 不搜它** —— cross-seed 只认自己的
> `TORZNAB_URLS`，会继续请求已禁用的站，每搜一次吃一个 `HTTP 410` 并 snooze。
> **彻底停用某站必须把它从 `TORZNAB_URLS` 移除**再重建（SUMMARY §13.6 坑 5）。

**对站点友好**：`delay` 保持 30~45；Prowlarr 每个站的 **Query Limit 只当保险丝**（设成明显高于实际用量的值，如 1000/天），
不要拿它当节流阀。加站**不会**增加单个站的查询量 —— 一次搜索由 Prowlarr 分发到各站各一次。

> ★ 「定期人工看额度」这件事**能自动化到什么程度、哪里是天花板**（站点真实余额
> Prowlarr 根本不知道），见 **SUMMARY §16.1**。

> **怎么判断某个站要不要 FlareSolverr**：在 Prowlarr 里手动搜一下。
> 看到 `403` + 挑战页 HTML / 日志里出现 `Cloudflare` → 需要；
> 看到 `500/502/520/522/timeout`（站点后端挂了）或 `429`（限流）或能正常返回结果 → **不需要**。

> ⚠ **Cloudflare**：中文 NexusPHP 站常需要 FlareSolverr。真要给某个站启用，做法是
> **在 Prowlarr 里给那个站挂一个名为 `flaresolverr` 的 tag**（Prowlarr 按**索引器挂 tag**
> 启用，**没有全局开关** —— 「Settings → Indexers 里填个 URL」这个说法是错的）。
>
> ★ **「从 GHCR 拉镜像失败」那条已经过时**：SUMMARY §7 记的是**早期**的情况；2026-09-12
> 那次 `up -d` 已经把 `ghcr.io/flaresolverr/flaresolverr:latest` 成功拉了 12 层，
> 现在容器跑的就是它。`compose.yaml` 里的镜像名**不用**换成 Docker Hub 那个。
>
> ★ **现状（2026-09-12 17:15 实测）：FlareSolverr 装了、容器在跑（v3.5.2、健康），
> 但一次都没被用上** —— Prowlarr 的 tag 列表是**空的**，四个索引器**一个 tag 都没挂**。
> 而且现在**四个站都不需要它**。判定见 SUMMARY **§18.12**。

### 单片状态机（已实现）—— 记录每部片子走到哪一步了

（本节连同 `### 多包支持（已接入 3 个包）` / `### IYUU 扩散（本次不实现）` / `### 别的匹配器（本次不实现）` 一并搬走 —— **标题保留是为了让全仓既有引用照旧命中**。）

> **本节已搬（2026-09-20，README 瘦身成 GitHub 门面）** —— **单片状态机的阶段语义、`--depth` 的硬约束、三个真实包的结构** —— 已搬。
> ★ 真相源：`ENVIRONMENT.md` 的 **`A.13` 运行语义（真实值）**。
> 
#### 多包支持（已接入 3 个包）—— 只留锚（原文见 ENVIRONMENT.md `A.13.2`）

> 原文可从 git 历史取回：`git show 94a3d0f:README.md`。
> ★ **要再加一个大包**：`ENVIRONMENT.md` 的 **`A.13.2`** 有「**怎么再加一个大包**」三步 + 三个必踩的坑；
> 先跑 `python scripts/diag/find-packs.py`（**只读**列出"像大包但没登记"的目录）。

#### 生产 `.env` 怎么更新（本地改完要同步到 NAS）

`deploy.sh`（**现已删除**）的白名单**故意不含 `.env`**（怕覆盖生产上的真实密钥），所以 `DATA_DIRS` 得单独上 NAS 改；
现在 `.env` 同样**不在受管集合（`DEPLOY_MANIFEST`）里**，仍要单独处理。这条值长约 3.5 KB、含中文与全角括号、
还有带空格的路径 —— **别手敲**。用生成器：

```bash
python scripts/diag/gen-nas-env-update.py          # 由本地 .env 生成 scripts/nas-update-env.sh
```

把生成的 `scripts/nas-update-env.sh` 传到 NAS（和 `compose.yaml` 同目录），然后：
```bash
sh nas-update-env.sh --dry-run       # 先看：备份 + 新旧条数 + 逐条校验 49 个路径是否存在
sh nas-update-env.sh                 # 改 .env + 重启 cross-seed + 回读容器内条数做闭环验证
sh nas-update-env.sh --no-restart    # 只改不重启
```

脚本会：① 备份 `.env` → `.env.bak.<时间戳>`；② 用 `awk` 就地替换 `DATA_DIRS=` / `LINK_DIR=`
两行（不走 `sed`，避开分隔符与 `&` 转义坑）；③ 逐条 `[ -d ]` 校验（缺失只告警）；
④ 重启后 `docker inspect` 回读容器内的 `DATA_DIRS` 条数并比对。回滚：`cp -p .env.bak.<时间戳> .env`。

**它只动这两行，其余键逐字节保留** —— 因为本地 `.env` 的 `TORZNAB_URLS` 是脱敏占位符
（`apikey=xxxx…`）、生产上是真实密钥，串了就是全线 401（写盘前有 `cmp -s` 硬闸把关）。
原理与实测见 SUMMARY §11.12。

> ⚠ **怎么确认它真的生效了**：`.env.new` 存在 ≠ `.env` 已更新。判断一律看这两处 ——
> ① `docker inspect reseed-cross-seed` 的 `DATA_DIRS` 条数 ② `cross-seed.db` 的 `data` 表
> 里有没有新包的路径。别看 `ls`。详见 SUMMARY §11.12。

#### 其它要点

1. **`hlink/config.yml` 的 `jobs`** 是数组 —— 复制一段、改 `name` / `source_dir`；
   注意 job 的 `source_dir` 语义是「**其子目录 = 各单片**」。
2. **`LINK_DIR` 必须与源大包同一个物理卷**（硬链接不能跨卷）。新大包若在**另一个卷**上，
   就要为新卷再配一个 linkDir —— cross-seed v6 的 `linkDirs` 是数组，它会按 searchee 所在设备挑同卷的那个。
   **不要**把 linkDir 指到 SSD 上图省事：跨卷建不了硬链接，会直接失败。
3. **分类与汇报**：`QBIT_CATEGORY` 目前是单一分类 `reseed-singles`。多包时靠
   `LINK_DIR/<包名>/<Tracker>/...` 的目录结构区分，或给每个包单独起一个 qB 分类。
4. **磁盘**：新大包本身要占真实空间 —— NAS 的 `/volume1` **已 0 可用 / 100%**。
   ★ 先看 **SUMMARY §9 的「卷归属表」**：`video` / `Download` / `docker` 在**满**的那块
   （56 T），`docker_ssd` / `qb_temp` / `技术文档` / `000 临时文件夹nas（内容可删除）`
   在**另一块 448 G 的盘上（304 G 可用）**。要临时落脚就放后者，**别往 `/volume1` 写真实文件**。
   但**硬链接侧依旧零开销** —— 这正是本项目能在满卷上跑起来的原因。
5. **站点压力**：接入 DC 后 searchee 总数从 ~405 涨到 **~1000+**，一轮全量搜索的耗时和 API 次数
   都会成倍增长。**按包分时段跑、务必带 `--limit`**，别同时开多个全量任务。
6. **日常重搜别打大包根的 webhook** —— 它不会排除已做种的片子。走
   `reseed-state.py drive`（已排除 `SEEDING`/`MATCHED`），详见 SUMMARY §11.11。

### 硬链接农场（v3）

> **本节已搬（2026-09-20，README 瘦身成 GitHub 门面）** —— **农场怎么建、怎么验、怎么切换**（`FARM_SOURCES` 自指闸 / `--map` / 安全边界）—— 已搬。
> ★ 真相源：`ENVIRONMENT.md` 的 **`A.14` 存储与农场**（与 `A.3` 卷水位是**一对**）。
> 
#### 残留风险 / 边界 —— 只留锚（原文见 ENVIRONMENT.md `A.16.1.2`）

> 原文可从 git 历史取回：`git show 94a3d0f:README.md`。

### IYUU 扩散（本次不实现）

### 别的匹配器（本次不实现）

> **本节已搬（2026-09-20，README 瘦身成 GitHub 门面）** —— 这两个**预留位**已搬。
> ★ 真相源：`ENVIRONMENT.md` 的 **`A.13.3`**。
> 
> 原文可从 git 历史取回：`git show 94a3d0f:README.md`。

## 原理技术和风险须知

### 原理 C —— 只留锚（原文见 `ENVIRONMENT.md` `A.15.3`）

> **本节已搬（2026-09-20，README 瘦身成 GitHub 门面）** —— **「包是声明的不是识别的」· 搜索压力来自分母 · BT 只校验 piece · 风险清单** —— 已搬。
> ★ 真相源：`ENVIRONMENT.md` 的 **`A.15` 原理与风险（真实值）**；
> ★ 完整论证与四条对策仍只在 **`SUMMARY §19`**。
> 
> 原文可从 git 历史取回：`git show 94a3d0f:README.md`。

## 源文件被写穿 —— partial 匹配 × 硬链接农场（2026-09-13 发现）

> **本节已搬（2026-09-20，README 瘦身成 GitHub 门面）** —— **那次事故的完整现场记录**（关闸门 · 链接守护 · 硬链接 vs reflink 怎么判）—— 已搬。
> ★ 真相源：`ENVIRONMENT.md` 的 **`A.16` 写穿事件与链接类型**。
> 
### 链接守护 —— 只留锚（原文见 `ENVIRONMENT.md` `A.16.1.2`）

> 原文可从 git 历史取回：`git show 94a3d0f:README.md`。

## 漂移哨兵 —— NAS 和仓库到底一不一致

## 推前凭据扫描 —— 别把密钥推上去

> **本节已搬（2026-09-20，README 瘦身成 GitHub 门面）** —— **`check-deploy-drift.py`（NAS 与仓库一不一致）· `scan-secrets.py`（推前按值形状扫凭据）** —— 已搬。
> ★ 真相源：`ENVIRONMENT.md` 的 **`A.17` 部署、漂移与凭据**。
> 
> 原文可从 git 历史取回：`git show 94a3d0f:README.md`。

## 通知 / 告警（NAS 侧发信）

> ★★ **2026-10-05：本仓的通知写侧已整体砍掉**（`notify.py` / spool / `--notify*` 全删）。
>   要接邮件告警请用**独立通知项目**（`reseed-toolkit` 那份代码 + 归档的链路说明作参考），
>   **不要在本仓复活**。下面这节是**历史搬迁记录**，其描述的 spool 链路在本仓已不存在。

> **本节已搬（2026-09-20，README 瘦身成 GitHub 门面）** —— **spool 分工 · 两条通道 · 每日摘要=心跳 · DSM 任务配置（含 `21:20` 勘误）· 通知开关** —— 已搬。
> ★ 真相源：**独立通知项目** `nas里pt相关程序邮件通知告警项目\ARCHIVE-A18-reseed通知链路.md`
>   （原 `ENVIRONMENT.md A.18`，2026-10-05 整体迁出）。
> 
### 两条通道 —— 只留锚（原文见归档 `ARCHIVE-A18-reseed通知链路.md` §A.18.1.2）

#### NAS 侧一次性配置 —— 只留锚（原文见归档 `ARCHIVE-A18-reseed通知链路.md` §A.18.1.4）

> 原文可从 git 历史取回：`git show 94a3d0f:README.md`。

## 安全

- `.env`、`cross-seed/`（含 cross-seed 自建的 db）、`prowlarr/`（Prowlarr 配置）**不要提交/外传**——含 cookie/passkey/apikey。
- PT 站凭据只进 **Prowlarr**；`:3060` 走局域网免密白名单（或 `.env` 里的密码），二选一。
- ★ **本项目从不生成、修改或伪造任何媒体数据** —— 只建立链接、注入种子、触发 recheck。
  是否"完整做种"由 **qB 的 piece 校验**决定，本项目不做保证、也不承担判断责任。
  完整边界见 **⚠ 免责声明与使用边界**。

---

## 当前状态与下一步（「下一步」已撤 —— 见 `§26.2`）（2026-09-14 更新）

（本节连同 `## 日常看板` / `### 怎么继续跑（两种方式）` / `### ⛔ 电脑端已不参与（2026-09-12 退役）` / `### ⚠ 交接必读的坑` / `### 还没做` 一并搬走 —— **标题保留是为了让全仓既有引用照旧命中**。）

> **本节已搬（2026-09-20，README 瘦身成 GitHub 门面）** —— **状态快照、日报 `metrics` 八格基线、「三种读不到」的区分、调度与台账** —— 已搬。
> ★ 真相源：`ENVIRONMENT.md` 的 **`A.19` 观测口径与状态快照**。
> 

### 还没做（**已撤 —— 见 `§26.2`**）—— 只留锚（原文见 ENVIRONMENT.md `A.19.1.10`）

### 系统现状（一句话）—— 只留锚（原文见 ENVIRONMENT.md `A.19.1.1`）

### 🔴 下一步（按优先级）—— 只留锚（原文见 ENVIRONMENT.md `A.19.1.2`）

### 日常看板 —— 只留锚（原文见 ENVIRONMENT.md `A.19.1.4`）

### 怎么继续跑 —— 只留锚（原文见 ENVIRONMENT.md `A.19.1.6`）

### 把调度挂到 NAS 上（✅ 已完成 —— 2026-09-12 凌晨）—— 只留锚（原文见 ENVIRONMENT.md `A.19.1.6`；该小节已并入「怎么继续跑」）

### ⚠ 交接必读的坑 —— 只留锚（原文见 ENVIRONMENT.md `A.19.1.9`）

#### 收尾命令（一次重建同时办完两件事）—— 只留锚（原文见 ENVIRONMENT.md `A.19.1.10.1`）

> 原文可从 git 历史取回：`git show 94a3d0f:README.md`。
