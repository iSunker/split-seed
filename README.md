# split-seed —— 大包拆包 · 单种保种工具链（v1）

把做种的**多合一大包**，**零磁盘开销**地让每部单片也在隔离实例 `qbittorrent-reseed`(:3060) 上以**单种**做种。

```
大包(主QB) ─dataDirs→ cross-seed ─按内容匹配→ 命中:建硬链接+注入qB → :3060 校验通过→单种做种 ✅
                          ▲搜索  Prowlarr(PT站→Torznab) [可选]FlareSolverr
```

> 不用 IYUU 起步：它按 InfoHash 匹配，而单片"藏在大包里"、大包 InfoHash ≠ 任何单种 ⇒ 必须先用 cross-seed（**按内容匹配**）拿到"第一个单种"。
> 范围 = "单种在 :3060 做种"；IYUU 扩散已留接口、不实现。
> ★ 2026-10-05 `deploy.sh` 已删：部署 = **代码走 Docker Hub 镜像**（本机 build+push，NAS pull）+ **compose/配置 SSH 直推**（登记见 `check-deploy-drift.py::DEPLOY_MANIFEST`）。

---

## ⚠ 免责声明与使用边界

> **先读这节。** 本工具链**自动建链接、向 qB 注入种子**，可能**改写磁盘文件**（见「源文件被写穿」），也会**以你的身份向 PT 站发搜索**。

★ 本项目是**搬运工不是生产者** —— 只把"你已有的"数据**换个目录再做一份种**（判据 = **字节一致**）；**没有代码路径能造出、或掩盖一份不存在的数据**。

★ **`不完整做种 ≠ 假种`**：进度 <100% = 不完整；判假种**不看进度**（假种也能显 100%），看**来源可信否**。本项目**可能有「不完整做种」**（`partial` 副作用），**但永不会有「假种」**。

★★ **拿它做「假种」/刷流量/冒充别人 —— 立刻停手**；后果**自负**。

**你自负的后果**：站点规则（HnR/分享率）· 搜索压力（每站 14 天一轮）· reflink 占盘 · 写穿无备份 · 凭据只进 Prowlarr。

---

## 我要做什么 → 看哪节

| 我想… | 去哪 |
|---|---|
| 搞清它干嘛的 | 上面 + `ENVIRONMENT.md` `A.15` → `SUMMARY §19` |
| 从零部署一台 | 下「部署步骤（分阶段，每步可独立验证）」 |
| 跑起来 / 卡住了 | `ENVIRONMENT.md` `A.19` + 下「常见问题」 |
| 新会话开局 | ★ 先读 `CLAUDE.md`（唯一入口：开局三步 + 真源表） |
| 下一步做什么 | `summary/26-1` 的 `§26.2` / `§26.2.3` |
| 本机手动跑点东西 | `scripts/diag/`：`check-deploy-drift.py` · `scan-secrets.py` · `audit-found-*.py` |

> **本文件 = GitHub 门面**（只留"怎么用"，通篇占位符，可安全公开）。
> **运维事实**（真路径/调度/基线/凭据）在 `ENVIRONMENT.md` `A.13`–`A.19`（**别公开**）。
> **为什么这么做/踩过什么坑** → `SUMMARY` / `summary/`（按 `§N` 寻址）。

---

## 目录结构

```
reseed/          # NAS 部署目录（2026-10-05 由 prowlarr_cross-seed_autohardlink 改名）
├─ compose.yaml  # 唯一 compose（prowlarr+flaresolverr+cross-seed+orchestrator+drive-loop+autoheal+watcher）
├─ .env(.example)  # 唯一密钥/IP/路径来源
├─ data/         # ★容器相关的全部内容（2026-10-05 收拢）
├─ patches/reseed-qbit.conf.md   # Phase 0：:3060 需改配置项
└─ ENVIRONMENT.md · README.md · CLAUDE.md · summary/
```

★ 代码只进镜像；compose/配置走 SSH 直推。`.env` 是唯一来源（`config.yml` 用 `${VAR}`、`config.js` 读环境变量），硬链接在 `LINK_DIR`（须与大包**同卷**）。

---

## 开工前的两道门（动手前必读）

> 判据本体在 `ENVIRONMENT.md`（动作边界 `A.2`、判据约定 `A.11` + `ERR-*`）。

**① 动作 → 允许条件**（`批次间隙` = `.drive-loop.state` 的 `running_pid` 为 `null`）：只读 SMB/HTTP=随时；`--force-recreate`、`build-farm.sh --apply`=批次间隙；改 `DATA_DIRS`/`LINK_DIR`=用 `gen-nas-env-update.py`**不手敲**；`rm`=只走 `rm-staging.sh`、**绝不 `rm` UNC**。

**② 改动前自检 9 格**：改哪层？要 `--build`/`--force-recreate`？另一侧是谁？阴性对照？回读证据（md5/grep/inspect）？没查到 ≠ 查到没有？输出会被照抄吗？

★ `running_pid` 是**快照非窗口**。

---

## ★★ 往 `DATA_DIRS` 里加路径（**先读这节**）

> cross-seed 拿 `dataDirs` 的**直接子目录名**当发布名去搜。★ 实测静默失败：`TV/` 里混两个**分类目录**，一旦进 `DATA_DIRS` ⇒ **搜不到东西但日志一切正常**（`§26.58`/`§19.1`）。**规矩**：分类目录名**不许**进 `DATA_DIRS`（清单 `scripts/diag/datadirs_rules.py`，判据是**整名比对**）。

```bash
python scripts/diag/gen-datadirs.py "//<NAS>/video/download/TV" --level 2 --list   # ① 对着盘上真东西看
# ② 生成片段（含分类目录名会 rc=3 拒绝）：加 --nas-prefix /volume1/video
MSYS_NO_PATHCONV=1 python scripts/diag/check-datadirs-names.py                     # ③ 兜底（命中=1）
```

---

## 部署步骤（分阶段，每步可独立验证）

> 前置：整目录放到 NAS `/volume2/docker_ssd/reseed`，`cp .env.example .env` 填 `NAS_IP`/`CROSSSEED_API_KEY`/`DATA_DIRS`/`LINK_DIR`（后两者**同一物理卷**）。

- **Phase 0 修 `:3060`**：按 `patches/reseed-qbit.conf.md`（端口 56883、局域网免密白名单、关 DHT/PeX/LSD/队列、注入不暂停）。验证 `curl .../api/v2/app/version` 免密返回。
- **Phase 1 Prowlarr 加站**：`docker compose up -d prowlarr flaresolverr`；在 `:9696` 加站，各站 Torznab URL+apikey 拼进 `.env` 的 `TORZNAB_URLS`。
- **Phase 2 cross-seed 试点**：`up -d cross-seed`；对 1~2 部电影打 webhook，验证 `linkDir` 出硬链接且 `:3060` 该校验后做种。
- **Phase 3 编排器**：`build reseed-orchestrator` → `run --rm reseed-orchestrator preflight` / `run --job <名>` / `status`。

---

## 验证清单

1. Phase 0：`curl .../api/v2/app/version` 免密返回；`nc -vz <NAS_IP> 56883` 通。
2. Phase 2/3：`ls -l "<LINK_DIR>/<某电影>"/*.mkv` **链接计数 ≥ 2**（删单种不伤大包）；`:3060` 进度 100% 且 `seeding`。
3. 全量后：编排器做种数 = `:3060` 分类 `reseed-singles` 实际数。
4. 日报「各包进度」：`docker compose --profile drive-loop logs drive-loop | grep -c packpct` > 0（★ 判据必须可执行，不许写"看一眼日报"）。
5. 「装不出来的单种」进日报：`... | grep -c 未完成且停滞` > 0（`SUMMARY §27`）。

---

## 常见问题

- **中文站搜不到/为空**：该站要 FlareSolverr？passkey/cookie 过期？先在 Prowlarr 手动搜确认，再考虑放宽 `MATCH_MODE`。
- **命中率低**：正常，单站实测 ~50%（`§5.3`）。想提高就**加索引器**（下「多站点」）；清单 `scripts/unmatched.tsv`。
- **跑完像"漏很多"**：多半是**索引器被退避、剩余秒跳**（去 Prowlarr 看状态码：`5xx`=站点故障、等好重打根目录 webhook；真 `429+Retry-After` 才调 `delay`）。别肉眼判断，用状态机。
- **注入后一直"校验中"/失败**：发布名/大小与数据不完全一致属正常；确认 `LINK_DIR` 与源**同卷**、已 1:1 挂进两个容器。
- **`cross-seed` 报 `unknown option`**：版本键名差异，按 `config.js` 顶部说明改。
- **外网连不上/上传为 0**：路由器 56883 **TCP 和 UDP** 都要转发。
- **`:3060` API 403**：白名单没覆盖来源 IP，或改用 `QBIT_AUTH_MODE=password`。
- **回灌打出负数 / qB 做种状态机却写 `MATCHED`**：先两边对数；真根因 `§17.5.1`。
- **`attempts.log` 有 `exit=127` 但 python 明明跑完**：**假象**（旧 `deploy.sh` 曾在运行中覆盖脚本）；判据 = 一次 `start` 没配对 `exit` ⇒ 先怀疑脚本被换过（`§17.2`）。

---

## 扩展

### 多站点 —— 提高命中率的唯一手段

Prowlarr 加 Indexer（凭据**只在网页填**）→ 记 **indexerId**（⚠ 删站重加会换）→ 追加进 `.env` `TORZNAB_URLS` → **必须 `--force-recreate`** 重建 cross-seed → 对 `DATA_DIRS` 根重打 webhook（`§13.3`）。★ **只在 Prowlarr 禁用 ≠ 不搜**，要停用必须从 `TORZNAB_URLS` 移除（`§13.6`）。

### 单片状态机

记录每部片子走到哪一步。阶段语义/`--depth` 约束/包结构见 `ENVIRONMENT.md` `A.13`。

#### 多包支持

只留锚 —— 见 `A.13.2`（先跑 `find-packs.py`）。

#### 生产 `.env` 怎么更新

`.env` 不在 `DEPLOY_MANIFEST` 里、要单独处理（含中文/全角/空格路径，**别手敲**）：`gen-nas-env-update.py` 生成 → 传 NAS → `sh nas-update-env.sh [--dry-run]`。★ 确认生效看 `docker inspect`/`cross-seed.db`，**别看 `ls`**（`§11.12`）。

### 硬链接农场（v3）

怎么建/验/切换（`FARM_SOURCES` 自指闸/`--map`/安全边界）见 `ENVIRONMENT.md` `A.14`。

#### 残留风险 / 边界

只留锚 —— 见 `A.16.1.2`。

### IYUU 扩散 / 别的匹配器（本次不实现）

只留锚 —— 见 `A.13.3`。

## 原理技术和风险须知

### 原理 C

「包是**声明**的不是识别的 · 搜索压力来自**分母**不是频率 · BT 只校验 piece · 风险清单」见 `ENVIRONMENT.md` `A.15`；完整论证 `SUMMARY §19`。

## 源文件被写穿 —— partial 匹配 × 硬链接农场

事故完整现场（关闸门/链接守护/硬链接 vs reflink）见 `ENVIRONMENT.md` `A.16`。

### 链接守护

只留锚 —— 见 `A.16.1.2`。

## 漂移哨兵 —— NAS 和仓库到底一不一致

`check-deploy-drift.py`（NAS 有没登记外的文件 / 仓库有没有该部署却没进受管集合的已跟踪文件）—— `ENVIRONMENT.md` `A.17`。

## 推前凭据扫描 —— 别把密钥推上去

`scan-secrets.py`（按**值的形状**找漏进仓库的 cookie/apikey/passkey）—— **绿了再 `git push`**。`ENVIRONMENT.md` `A.17.2`。

## 通知 / 告警（NAS 侧发信）

> ★★ **2026-10-05 本仓通知写侧已整体砍掉**（`notify.py`/spool/`--notify*` 全删）—— 要接邮件告警用**独立通知项目**，**别在本仓复活**。spool 分工/两条通道/DSM 配置/开关等详见该项目 `ARCHIVE-A18-reseed通知链路.md`（原 `ENVIRONMENT.md A.18`）。

### 两条通道

只留锚 —— 见归档 `ARCHIVE-A18…通知链路.md` `§A.18.1.2`。

#### NAS 侧一次性配置

只留锚 —— 见归档 `ARCHIVE-A18…通知链路.md` `§A.18.1.4`。

## 安全

- `.env` / `cross-seed/`（含自建 db）/ `prowlarr/`（含 cookie/passkey/apikey）**不要提交/外传**。
- PT 站凭据只进 **Prowlarr**；`:3060` 走局域网免密白名单或 `.env` 密码。
- ★ 本项目**从不生成、修改或伪造任何媒体数据** —— 完整边界见「⚠ 免责声明与使用边界」。

---

## 当前状态与下一步（「下一步」已撤 —— 见 `§26.2`）

本章（连同子节）已搬 —— 真相源 `ENVIRONMENT.md` `A.19`。**标题保留是为了让全仓既有引用照旧命中。**

### 还没做（**已撤 —— 见 `§26.2`**）

只留锚 —— 见 `A.19.1.10`。

### 系统现状

只留锚 —— 见 `A.19.1.1`。

### 🔴 下一步（按优先级）

只留锚 —— 见 `A.19.1.2`。

### 日常看板

只留锚 —— 见 `A.19.1.4`。

### 怎么继续跑

只留锚 —— 见 `A.19.1.6`。

### 把调度挂到 NAS 上

只留锚 —— 见 `A.19.1.6`。

### ⚠ 交接必读的坑

只留锚 —— 见 `A.19.1.9`。

#### 收尾命令（一次重建同时办完两件事）

只留锚 —— 见 `A.19.1.10.1`。
