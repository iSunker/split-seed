# CLAUDE.md —— 新会话入口（**唯一入口**）

> **这个文件是给 AI 的**。人接手请读 `README.md`（怎么用）→ `summary/26`（干到哪了）。
> ★★ **它是索引，不是事实源** —— 它只回答「**去哪找事实**」，自己**不含**事实。
> 这里写的任何数字，都以它指向的那份为准。

---

## 0. 开局三步（**做任何事之前**）

```bash
# ① 现在干到哪了？（跨会话待办的真源）
#    读 summary/26 的 §26.2 主表 + §26.2.3 三张总表 —— 别读别处的复述
# ② 这台机器长什么样？（运维事实的真源）
#    ENVIRONMENT.md 的 A 部分：至少看 A.1 可达性 / A.3 卷布局 / A.12 三处路径
# ③ 现在真的是几？（读数一律当场读，别信文档里印的数）
python tools/doc-audit/04_probes.py
```

**不要**先通读全部文档 —— 全仓 **1.9 万行 md**，通读既贵又会读到已作废的段落。
按下面的「真源表」**按需取**。

---

## 1. 真源表（★ 冲突时以这里为准）

| 想知道什么 | 真源 | 别去信 |
|---|---|---|
| **跨会话待办 / 干到哪了** | `summary/26-1-主题索引与旧会话清场.md` 的 **`§26.2` 主表** + **`§26.2.3`** | 会话 TaskList（换会话就没了）、`README` 的旧「下一步」 |
| **运维事实**（主机名/路径/调度/真实值） | `ENVIRONMENT.md` 的 **`A.13`–`A.19`** | 任何转述 |
| **环境前提与坑**（按症状查） | `ENVIRONMENT.md` 的 **A 部分** + `ERR-*` 条目（可 grep） | 别处的复述 |
| **为什么这么做 / 当时踩了什么** | `summary/` 各章（`§N`） | —— |
| **怎么做**（命令、步骤） | `README.md` | —— |
| **测试与断言条数** | `tests/COUNTS.json`（机器写） | 别处的数字 —— **人也不许手改** |
| **`DATA_DIRS` 该指到哪一层 / 哪些名字不许进** | `README.md`「★★ 往 `DATA_DIRS` 里加路径」+ `scripts/diag/datadirs_rules.py`（清单**唯一出处**）| 别处复述的清单 —— **就一份** |
| **当前读数**（磁盘/种子数/版本） | `python tools/doc-audit/04_probes.py` | **文档里印的任何数字**（那是"当时"，不是"现在"） |
| **文档结构本身**（有几份、谁引用谁） | `tools/doc-audit/INDEX-USAGE.md` | —— |

★ **优先级（2026-09-20 起，README 已瘦身成门面）**：
**运维事实以 `ENVIRONMENT.md` 为准** ＞ 过程与原理以 `summary/` 为准 ＞ `README.md` 只讲**怎么用**。

---

## 2. 几条**必须知道**的规矩

- **同一事实只在一处维护**（`A.11`）。跨文档用单向指路（「详见 `§N`」），**不重复登记**。
  ★ 这条**已破过多次**（`summary/26` 自己改过三次、每次都是"真源分家"）——
  所以：**引一个数之前，先 `grep` 它在几处出现**。
- **改已发布的文档不许直接改原句** —— 用「`~~原句~~` + `> ★ 更正（日期，来源）`」三段式（`§26.16` 附近有定义）。
  ★ 例外：**`ENVIRONMENT.md` 的 `A.13`–`A.19` 是运维事实，不是过程记录** ——
  那里**直接改成对的**，旧值进 git 历史，不留划掉段（否则读者要读完全文才知道哪句是真的）。
- **读数不当派生数据**。会动的数（水位/种子数）**不进正文**，用 `04_probes.py` 现读。
  只有**能重算出来**的数（如断言条数）才做成生成器（`tests/gen-counts.py`）。
- **判据要带"另一侧"**：任何计数/期望值要**能指回一条判据之外的真实记录**。
- **不静默跳过**：跳过要连理由一起列出来。
- **写指针之前，真的去看一眼它在不在**（引用前先 `grep`）。

---

## 3. 改完文档**必须跑**的闸门

```bash
for s in 01_headings 02_refs 03_terms 05_linerefs 06_sources; do python tools/doc-audit/$s.py; echo "$s rc=$?"; done
python tests/gen-counts.py --check      # 计数一致？
for t in tests/test_*.py; do python "$t" >/dev/null 2>&1 || echo "✗ $t"; done   # 全过？
```

| 什么时候 | 跑什么 |
|---|---|
| 改完**代码文件 / 挪了行** | `05_linerefs.py`（位置级引用） |
| 改了**任何标题 / 挪了章节** | `02_refs.py`（★ 符号级引用，必跑） |
| 改了**常量名 / 术语** | `03_terms.py`（同一事实散在多处？） |
| **改完一处「事实」** | `06_sources.py` —— ★ **但它只盖两种，见下；别把它当"事实表述的全面闸门"** |
| **加/删了测试断言** | `tests/gen-counts.py`（然后 `--check`） |
| **推之前** | `python scripts/diag/scan-secrets.py`（0 = 可以推） |

### ★★ `06_sources.py` **能查什么 / 查不到什么**（2026-09-20 实测，别越界承诺）

**它能查两种，且两种都要求那行**带「更正 / 勘误 / 订正」字样**：**

1. **同一文档内，同一事实被更正 ≥2 次**（改了两次，旧的没删）
2. **读者先撞上错的那句**（划掉段在文档前 1/3，而"对的"在后面）

**它查不到（★ 最重要的一条）：**

> **「几处各自陈述、都没有更正标记，但说法互相打架」。**

★ **实测依据**：2026-09-20，那句「文档分工 / 谁是真源」曾在 **5 份文件里各写一遍、两两打架**
（`SUMMARY.md` 甚至说反了），而 `06_sources` **命中 0 条**。
**根因**：它的 A 类入口是 `correction_lines()` —— 靠那三个字**筛候选行**；
那 5 处**只是平静地各说各话，没有"我改了"的自觉** ⇒ **第一步就被过滤掉了**。

⇒ **判据**：`06` 只数「**有人说自己改了**」，发现不了「**没人说，但几处对不上**」。
**那类只能靠人**（或 `grep` 一个已知句式）—— **别指望这个闸门**。
★ **为什么不做成自动判**：跨文档措辞差异太大，照 `03_terms` 那个形状做会**满仓假红**
（`terms.tsv` 里跨 ≥2 文件的词 25 个，多半是同义重复）——「永远红的闸门 = 没人看的闸门」。

★ 退出码 **1 = 有铁定失效的东西**（`05` 可直接当闸门）。
★ 两份 `out/*.tsv` 的 hash **每次跑都会变**（含行号 + 读数），**别拿 hash 相等当判据**。

---

## 4. 硬限制（别指望做到）

- **NAS 有 SSH shell（端口 `36688`，22 仍关闭）但无 SFTP** ⇒ 可 `ssh` 执行命令、可用 `ssh 'cat > <path>'` 推文件；
  但仍**不能 `scp`**。容器环境变量、`synoschedtask`、真发邮件**探针**仍只能走"等价物"
  （如容器 env → `cross-seed.db` 的 `indexer` 表）。
- **`:3060` 是与别人共用的 qB** ⇒ 凡读它**必须按 `category` 收窄**（我们的是 `reseed-singles`）。
- **文档审计只扫仓库根的 `*.md`** ⇒ `tools/`、`tests/` 下的 md（含 `INDEX-USAGE.md` 自己）**没人查**。
- **`Task#N` 不是文档锚点** —— 只在当次会话的棋盘里有意义，**会话结束就找不到**。

---

## 5. 结构规约（**整理仓库时按这套做**）

> ★ **本节是结构/目录/git/部署规则的唯一出处**（2026-10-10 起）。
> ★ **历史**：原独立文件 `.claude/rules` 因**缺 `.md`、又不是目录**，C​laude Code 的加载约定
> （`<repo>/.claude/rules/**/*.md`）**永远读不到它** ⇒ 规则对 agent 空转。现**并入本文件**
> （`CLAUDE.md` 是新会话必然自动加载的唯一入口）+ 删掉 `.claude/rules`。
> ★ 与 `A.11`「一事一处」一致：**结构规则只在这一处**，别处只留指针。

### 5.1 目录约定

- **容器相关的一切进 `data/`**（`data/cross-seed` / `data/hlink` / `data/orchestrator` /
  `data/drive-loop` / `data/prowlarr` / `data/scripts/diag`）。判据：**容器运行时读的** 或 **compose 引用的**。
- **`data/` 平级只允许一个 compose**，文件名 `compose.yaml`（本机与生产同名）。
- **本机专属**（`tests/`、`tools/`、根下 `scripts/`〔diag 工具集〕、`summary/`、各 `*.md`）
  **不上 NAS、不入镜像**。★ 注意与 `data/scripts/diag/` 区分：那个是 **watcher 容器读的**，要上 NAS；
  根下 `scripts/` 是本机资产（其中 3 个文件同时进部署集，见 `DEPLOY_MANIFEST`）。

### 5.2 「compose 目录」只有三条来源，必须一致

| # | 来源 | 位置 | 类型 |
|---|---|---|---|
| A | `$0` 反推 / `dirname` | `data/drive-loop/run.sh` | 动态 |
| B | `CROSSSEED_DIRS`（读 `COMPOSE_DIR` 环境变量） | `data/drive-loop/scripts/drive-loop.py` | **镜像里** |
| C | shell `${COMPOSE_DIR:-<默认>}` | `build-farm.sh`、`scripts/diag/*` | 静态默认 |

- ★★ **B 是唯一会"静默坏"的**：它经 `first_existing()` 解析 4 样东西
  （`build-farm.sh` / `cross-seed.db` / `info.current.log` / `.env`），路径错时**全部降级成 `LOG.warning`**，
  不退非零、日报数字错（`SUMMARY §26.15` 的 `packpct=0` 形状）。
- ★ 新根 = `/volume2/docker_ssd/reseed`。**任何 shell 默认值不许硬编码一个同样出现在镜像里的路径**。

### 5.3 代码定位规则（最重的一条）

- **容器读的代码要么在镜像里、要么挂载 1:1 同路径。** 本仓已选**代码全在镜像里**：
  `data/orchestrator/*`、`data/drive-loop/scripts/*`、`data/drive-loop/run-resident.sh` 全部由 Dockerfile `COPY` 进镜像（`/app/…`）。
- **改这些代码 ⇒ 重建镜像 + push + NAS pull**，受管文件集（`DEPLOY_MANIFEST`）**碰不到它们**。
  ★ 病根见 `SUMMARY §26.15`（一份代码三个副本、生产读到旧的那份、`packpct` 全 0，**不报错**）。
- 镜像内布局**固定**：`/app/scripts/` + `/app/orchestrator/`（`drive-loop.py` 靠 `sys.path` 找 orchestrator）。
- 运行态（`.state` / 日志 / `state.db`）走**挂载卷** `/state`（环境变量 `DRIVE_STATE_DIR`），**不在镜像层**。

### 5.4 git

- 远程 `origin` **已删除** —— **只在本机提交，绝不推远程**。
- `.env` 已 gitignore，**绝不进 git、也绝不进镜像**（`.dockerignore` 用 `.env*` 通配兜）。仓库**是公开的**。
- 密钥/口令**只从 `.env` 读**，绝不写进命令行、绝不回显（见 `scan-secrets.py`）。

### 5.5 部署模型（push/pull）

- **代码**走 Docker Hub 镜像：`isunker/reseed-toolkit-drive-loop:<短sha>` /
  `isunker/reseed-toolkit-orchestrator:<短sha>`（tag = git 短 sha）。本机 `docker build` → `push` → NAS `pull`。
  ★ NAS 上**不许 build**（那边没有源码树）。
  ★ **改 compose 里的 tag 前先确认镜像已 push**（否则 NAS 拉不到）——且**只改真要推的那个服务**。
- **compose / 配置**走 **SSH 直推**（NAS 无 SFTP ⇒ `ssh 'cat > <path>' < <localfile>`，逐文件原子替换 + 本地备份 + 可达性闸门）。
  受管文件集写在 `scripts/diag/check-deploy-drift.py` 的常量 `DEPLOY_MANIFEST`；`[A] 代码 ⇒ 走镜像`。
- 改代码的正确流程：`docker build` → `push` → NAS `docker compose pull <服务> && up -d --force-recreate <服务>`。

### 5.6 废旧件清理（**整理时用这条判据**）

**可删项三类**：① 空/孤儿目录（零引用、哨兵看不见的残留）；② 含凭据的备份（`.env.bak-*` 等）；
③ 已死机制的残留（如 `deploy.sh` 时代产物）。

**判据写在 `scripts/diag/check-deploy-drift.py` 的常量里**（`DEPLOY_MANIFEST` / `LOCAL_ONLY` / `KNOWN_NAS` / `IMAGE_ONLY`）：

```bash
python scripts/diag/check-deploy-drift.py     # 漂移哨兵：B 方向「未登记」应恒为 0；A 方向报 NAS 侧杂物
```

- 新增/删文件后**登记要跟上**，漏登记会被 `tests/test_drift_lists.py` 报出来。
- **NAS 侧清场只出计划、由人对真机确认**（哨兵契约：只读、绝不对 UNC 跑 rm）。

### 5.7 一次结构变更的"抓静默"清单（改名/收拢后必查）

1. `md5sum /app/scripts/drive-loop.py` 回读 == 本地（哨兵 `PHASE_IDLE` 只证"不是远古版"，**真判据是 md5**）。
2. drive-loop 容器挂载源 == 新根（`docker inspect reseed-drive-loop --format '{{json .Mounts}}'`）。
3. `grep first_existing drive-loop.log` 一批次内**无** warning（路径错**唯一**信号）。
4. 日报 metrics 非 0（`SUMMARY §26.15` 的 `packpct` 形状）。
5. `docker inspect` cross-seed / orchestrator / watcher 的 `./data/…` 挂载源都在。

### 5.8 判据写在**能被证伪**的地方

- 断言/登记**逐条点名**，不用"全部满足 X"这种会被无关输入影响的判据（见
  `tests/test_drive_loop_service.py` ①k 的重写：从"每条挂载都 1:1"改成"逐条点名四条挂载"）。
- 新增/删文件后登记要跟上：`scripts/diag/check-deploy-drift.py` 的常量
  `DEPLOY_MANIFEST` / `LOCAL_ONLY` · `KNOWN_NAS` · `IMAGE_ONLY`（走镜像的源码）。
