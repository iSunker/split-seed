## §14 驱动层从 Windows 迁到 NAS（2026-09-11 深夜）

> **一句话**：`drive-loop` 的调度从 Windows 计划任务搬到了 **NAS 的 DSM 任务计划**。
> 起因是 Windows 侧批次**静默消失**；查下去发现是**两个独立的原因**，
> 其中一个是本会话自己改出来的。代码与状态库已就位，
> **只剩「在 DSM 上建任务」这一件人工操作**（步骤见 `ENVIRONMENT.md` `A.18`（原 README「把调度挂到 NAS 上」））。

### 14.1 症状：批次"跑了，但什么都没发生"

`drive-loop.log` 里存在**有启动、没收尾**的批次。最干净的一例：

```text
2026-09-11 19:41:11,932 [INFO] [--once] 跑包 dc-collection（第 1/2 个）
        ← 此后该进程再无任何输出
2026-09-11 19:50:44,606 [INFO] === drive-loop 启动 … ===      ← 下一轮
2026-09-11 19:50:44        （没有报"上一批仍在运行"）
```

判读要点：**如果 19:41 那个进程还活着，19:50 那一轮应当报"上一批（pid …）仍在运行，跳过本轮"**
（20:25 / 20:40 / 22:25 / 22:40 几轮都报了）。它没报 → 19:41 的进程**已经没了**。
而且它没有留下任何收尾：没有 traceback、没有 `异常`、没有 `finally` 写的状态收尾。

> **这是判据，不是猜测**：Python 里异常/崩溃**一定会**留下痕迹。
> 能造成"整棵进程树被抹掉、连日志缓冲都来不及刷"的，只有 `TerminateProcess`。

### 14.2 原因 A：`<StopOnIdleEnd>true</StopOnIdleEnd>`

实时查任务定义（不是回忆，是当场读出来的）：

```powershell
schtasks /Query /TN "reseed-drive-loop" /XML
```

```xml
<StopOnIdleEnd>true</StopOnIdleEnd>
<DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>
<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
<Command>D:\Projects\reseed\scripts\drive-loop-once.cmd</Command>
```

`StopOnIdleEnd` 的含义是：**机器一旦"不再空闲"（你碰鼠标/键盘），Windows 直接
`TerminateProcess` 这个任务实例。** 这精确地解释了 14.1 的形态 —— 静默、无痕、
连 `finally` 都不执行、状态文件里 `running_pid` 永远挂着，于是之后每一轮都
"上一批仍在运行" 空转下去。

这是一个**设计上就与"无人值守长跑"互斥**的设置，而这个任务恰恰是 24 分钟一批的长跑。
换句话说：只要这台机器上有人，这个任务就永远跑不完一批。

> ★ 诚实标注：`StopOnIdleEnd=true` 是**读出来的事实**；把它与 14.1 那些
> 未收尾批次**因果关联**是**推断**（证据是时间点与"无痕"这一形态吻合）。
> 推论不影响结论 —— 下面 14.3 的迁移方案把这个问题整个绕开了。

### 14.3 原因 B：`.cmd` 包装器是 LF 行尾（★ 本会话自己弄坏的）

这才是 **22:55 之后任务彻底跑不起来**的直接原因，而且**是本会话 22:43 那次
"给包装器加留痕"的修改引入的**。必须原样记下来。

**观测到的证据**（`scripts/drive-loop.attempts.log` 全文只有两行）：

```text
[2026/09/11 周五 22:55:02.14] exit=9009
[2026/09/11 周五 23:10:02.11] exit=9009
```

`scripts/drive-loop.task.err` 尾部：

```text
'""' 不是内部或外部命令，也不是可运行的程序
或批处理文件。
'""' 不是内部或外部命令，也不是可运行的程序
或批处理文件。
```

**三个信号拼在一起，指向同一个解释：**

1. `9009` = cmd.exe 的**"命令找不到"**退出码；
2. `'""'` = 它试图执行的命令名**字面上是两个引号** —— 即 `%PY%` 和 `%SCRIPT%`
   **都展开成了空**，整行退化成 `"" "" --once …`；
3. `.cmd` 第 55 行 `echo … start >> attempts.log` 是**无条件执行**的，
   可 `attempts.log` 里**只有 `exit=` 没有 `start=`**。

**根因：批处理文件是 LF-only 行尾，cmd.exe 解析错位。**

cmd.exe 不是逐行读批处理文件，而是**按字节块读取并回退重定位**。
LF-only 会让它的字节偏移记账错位，结果是**从某一行中间开始执行** ——
于是 `REM` 注释里的普通英文单词被当成命令跑，而它前面那些 `set` 也就全丢了。

`scripts/drive-loop-once.cmd` 换行统计（`git HEAD` 版同样是 LF-only，
说明这个隐患**早就在**，只是那时文件短、偏移恰好没踩中）：

```text
修复前  工作区 CRLF=0   LF=59        ← 会坏
        git HEAD CRLF=0 LF=37        ← 这个长度下侥幸能跑
修复后  工作区 CRLF=59  LF=0         ← 正常
```

**受控 A/B（同一份内容，只差行尾）**：

| | attempts.log | stdout |
|---|---|---|
| CRLF | `start` + `exit=0` | 0 B |
| LF | **只有 `exit=0`，没有 `start`** | 511 B 乱码 |

LF 版的乱码正是注释文字被当命令执行：

```text
is:       cannot open `is' (No such file or directory)
expanded: cannot open `expanded' (No such file or directory)
by:       cannot open `by' (No such file or directory)
cmd.exe:  cannot open `cmd.exe' (No such file or directory)
at:       cannot open `at' (No such file or directory)
RUN:      cannot open `RUN' (No such file or directory)
time,:    cannot open `time,' (No such file or directory)
so:       cannot open `so' (No such file or directory)
the:      cannot open `the' (No such file or directory)
```

↑ 这九个词**按顺序**就是包装器注释里那句
"`%USERPROFILE%` **is expanded by cmd.exe at RUN time, so the** task definition itself stays pure ASCII."
—— 铁证。

**修复**：`scripts/drive-loop-once.cmd` 转成 CRLF。转换后桩测通过
（`attempts.log` 得到 `start` + `exit=0` 两行，被替换的那行也执行到了）。

> ★ **同一类问题一次清干净**：顺手体检了全仓库的脚本行尾，另外两个也要修 ——
> `deploy.sh`（CRLF=168）和 `scripts/run-batch.sh`（CRLF=298）都是 **CRLF 的 bash 脚本**。
> 在 Git Bash 里跑没事，但一旦拿到 WSL / NAS 上就是 `\r: command not found`。
> 两者都已归正成 LF。
>
> **教训**：早前用 `grep -c $'\r'` 查行尾，得到"CRLF=0，没有行尾问题"的结论 ——
> **方向反了**。CRLF=0 只说明"是 LF"，而 `.cmd` 要的恰恰是 CRLF。
> **按扩展名判定期望行尾**（`.cmd`/`.bat` → CRLF，`.sh`/`.py` → LF），
> 或用字节级 `b.count(b"\r\n")`，别用一句通用判断。

### 14.4 做了什么：迁移到 NAS

结论是**别再修 Windows 侧了** —— 这套东西的每一条依赖本来就都在 NAS 上
（cross-seed、qB、Prowlarr、媒体库），Windows 只提供一个调度器，
却为此引入 SMB 往返 + `<StopOnIdleEnd>` + 非 ASCII 用户名 + 行尾四个坑。

**代码侧（已完成）**

| 改动 | 文件 |
|---|---|
| 未来注解导入（NAS 系统 python 是 3.8.15，`str \| None` 语法否则在**定义期**就 `TypeError`，且那时 `logging` 还没配置 → **零日志输出**） | `scripts/drive-loop.py` |
| 路径探测 `CROSSSEED_DIRS`：**先 NAS 原生、再 UNC**（一份代码两边都能跑，不切参数） | `scripts/drive-loop.py` |
| spool 同样的候选探测 `SPOOL_CANDIDATES` | `scripts/notify.py` |
| **NAS 侧入口 `run.sh`**（DSM 任务调它）：钉死 `TZ`/`PYTHONIOENCODING`，写 `attempts.log` | `scripts/drive-loop-nas.sh` → `drive-loop/run.sh` |
| 白名单新增 6 条 | `deploy.sh` |

**为什么入口那层还要单独记一份 `attempts.log`**：`drive-loop.log` 只收 `logging` 的输出。
如果 python **自己起不来**（版本不对 → `SyntaxError`/注解 `TypeError`），`logging` 根本没配置，
`drive-loop.log` 一个字节都不会写 —— 表现就是"任务跑了但什么都没发生、也没有任何报错"。
**这正是 14.1 那个坑的形状**，所以 shell 这层必须自己留痕。三个信号分得很清楚：

| `attempts.log` | 含义 |
|---|---|
| 没有 `start` 行 | 任务压根没被触发（计划建错 / 没启用） |
| 有 `start` 无 `exit` | 进程树被强杀（`TerminateProcess`） |
| 有 `exit` 且 ≠ 0 | 它自己出错了（看 `drive-loop.log` 和退出码） |

**状态侧（已完成）**：`hlink/state.db` 的 605 部已迁到 `<compose>/drive-loop/hlink/state.db`，
`local_roots` 已从 UNC 归一成 NAS 原生路径（用 SQLite 快照 API 读，避免在 Windows 任务
可能正在跑批时读到撕裂状态）。回读校验 pack=3 / movie=605 / attempt=1350。

**为什么是 `--once` + 任务计划，而不是常驻容器**：`--once` 的跨进程节流带
**心跳超时接管**（`HEARTBEAT_STALE_SEC=600`）—— 上一批卡死 10 分钟后，
下一轮会自动接管。常驻进程卡死就没人接管了。见 §13.5。

### 14.5 排查表（NAS 侧）

| 症状 | 先看 | 多半是 |
|---|---|---|
| 完全没有 `start` 行 | DSM 任务是否启用、计划是否"每 15 分钟"、脚本路径 | 任务没被触发 |
| 有 `start` 无 `exit` | 是否有别的进程在抢 | 进程被强杀（NAS 上少见，多见于 Windows） |
| `exit=127` | `/usr/bin/python3` 在不在 | `run.sh` 找不到解释器或脚本 |
| `exit` ≠ 0 且有 traceback | `drive-loop/scripts/drive-loop.log` | python 层错误，**首先怀疑解释器版本** |
| `exit=9009`（Windows） | 包装器行尾 | `.cmd` 不是 CRLF —— 见 §14.3 |
| 每轮都"跳过" | `drive-loop.log` | 距上批不足 30 分钟 / 上一批的心跳还没过期 |
| 邮件里全是 `410` | Prowlarr 的 indexerId | **改了 `.env` 没 `--force-recreate`**（§13.10 ⑧） |

### 14.6 下一步（人工）

只剩三件必须你亲手做的事，详见 `ENVIRONMENT.md` `A.18`（原 README「把调度挂到 NAS 上」）：

1. **先停用 Windows 计划任务** `reseed-drive-loop`
   （`schtasks /Change /TN "reseed-drive-loop" /DISABLE`）——
   顺序很重要，两边同时驱动 cross-seed 会撞 429，一次能废掉几百条；
2. DSM 建任务，**第一次带 `--dry-run`**；
3. 手动跑一次，确认 `attempts.log` 有 `start` + `exit=0`，然后去掉 `--dry-run`。

> ✅ **已在 NAS 上实测通过（2026-09-11 23:27）**：`sh drive-loop/run.sh --dry-run`
> 在真机上跑通，`attempts.log` 得到

```
[2026-09-11 23:27:42] start  py=/usr/bin/python3
[2026-09-11 23:27:43] exit=0
```

> —— **`start` 行在了**（Windows 侧一直缺的正是这行），退出码 0，时间是本地时间
> （`TZ=Asia/Shanghai` 生效），`drive-loop.log` 落在 `drive-loop/scripts/` 下
> （`ROOT = HERE.parent` 的假设成立）。旁边生成了 `__pycache__/*.cpython-38.pyc`
> —— **解释器就是 CPython 3.8**，这条原先只是从文档抄来的假设，现在坐实了。
> `from __future__ import annotations` 确实是必需的（去掉会在 import 阶段静默崩）。
>
> 万一将来对不上（比如 DS 大版本升级换了 python），症状同样会是「静默」，
> 按 `ENVIRONMENT.md` `A.18.1.4`「NAS 侧一次性配置」里列的后备方案换解释器即可。

### 14.7 ★ 电脑端退役（2026-09-12 下午）—— 从"停用"走到底

**为什么当初只 `DISABLE` 而没有删**：迁移当天（09-11 深夜）的目标是"先别两边同时跑"，
`DISABLE` 就够了；留着是**回退余地** —— 万一 NAS 侧跑不起来，还能一分钟内切回去。
所以 §14.6 第 1 条写的是"先停用"，措辞刻意留了余地。

**什么时候余地不再需要**：2026-09-12 上午，NAS 侧连续几批都正常
（`attempts.log` 有 `start` + `exit=0`、跨进程节流真的在拦、
「新增做种 36 部 🎉」这类业务输出也对）。**能回退的价值 < 半退役状态的代价**，
于是走到底。

**代价具体是什么**（这是决定删的理由，不是"清理癖"）：代码里到处是"Windows 也能跑"
的痕迹，留着会让下一个人以为那条路还活着 —— 然后照着去配、去调、去踩已经踩过的坑。
典型的就是 `CROSSSEED_DIRS`：它**看起来**只是个路径列表，
但它承载的语义是"同一份代码两边都能跑"，这个语义在只有 NAS 一个环境时**已经不存在了**。
路径列表本身没错，错的是它暗示的世界。

**① 删（真删）**

| 文件 | 为什么能删 |
|---|---|
| `scripts/drive-loop-once.cmd` | 计划任务的入口，任务本身已停用；代码在 git 历史里，回退拿得回来 |
| `scripts/drive-loop.{log,attempts.log,task.err}` | PC 侧运行时产物，跑批侧已经全搬到 NAS |
| `scripts/.drive-loop.state`、`.notify.state` | PC 侧的节流/冷却状态；NAS 侧各有一份自己的 |

★ 删之前**逐个看过内容**（都是 09-11 的、含着 `\\iSunker-DS423\…` 和 GBK 乱码的 PC 侧记录），
不是只看文件名就删 —— `attempts.log` 里那份 `exit=9009` 正是 §14.3 那个坑的物证。

**② 注释（不删代码）**

`drive-loop.py` 的 `CROSSSEED_DIRS` UNC 兜底、`pid_alive()` 的 `tasklist` 分支
（连 `import subprocess`）、`notify.py` 的 `SPOOL_CANDIDATES` UNC 兜底。
统一用可 grep 的标记 `[电脑端已退役 2026-09-12]`。

★ 注释掉的代码值钱的不是代码，是它旁边那句话 —— 两条都**反直觉**：
* `os.kill(pid, 0)` 在 POSIX 上是"发 0 号信号探测存活"（标准做法），
  搬到 Windows 上**会真的把进程杀掉**。所以 `pid_alive()` 必须留 `tasklist` 的那支。
* UNC 兜底在 NAS 上**不报错**：它会绕一圈 SMB 连回自己 —— 只是慢，而且难查。
  这正是"多一条候选路径 = 多一个静默降级点"的实例。

**③ 没删（明确保留）**

* **`deploy.sh`** —— 唯一还从电脑发起的操作（把代码推到 NAS）。
* **`scripts/run-batch.sh`** —— 手动命中率试跑工具，不是调度的一部分。
* ⚠ `scripts/nas-update-env.sh` 仍是**手工放在** `<compose>/nas-update-env.sh` 的，
  **不在 `deploy.sh` 白名单里**（和 `build-farm.sh` 退役前的情况一样）。
  这条**没修** —— 它是给一次性 `.env` 迁移用的，用完就不该再跑，进白名单反而危险。

**④ 验证**：`ast.parse` 三个改动文件全过；四个测试套件
（`test_quota_trend` / `test_next_sleep` / `test_backoff` / `test_once_gate`）全过；
`drive-loop.py --help` 能跑；`deploy.sh --apply` 同步后在 NAS 副本上复跑正常。

**⑤ ✅ 已结案（2026-09-12 中午）：计划任务已删除。** 记录一下过程里的坑。

我这边（普通权限）试删，返回「拒绝访问」：

```
schtasks /Delete /TN "reseed-drive-loop" /F     # → 错误: 拒绝访问。
```

**这个报错方向很容易看反**，值得写下来：

| 报错 | 含义 |
|---|---|
| `拒绝访问` | **没提权** —— 换管理员窗口重跑就行，任务还在 |
| `找不到指定的文件` / `ObjectNotFound` | **已经不存在了** —— 这是**成功信号** |

用户随后敲 `Unregister-ScheduledTask` 得到 `ObjectNotFound`，一度看着像"删不掉"，
实际是**之前那条 `schtasks /Delete` 已经生效了**。两把工具独立复核确认：

```
schtasks /Query /TN "reseed-drive-loop"        → 错误: 系统找不到指定的文件。
Get-ScheduledTask | ? TaskName -like '*reseed*' → （空）
```

对照：同日 **11:45** 同一条 `schtasks /Query` 还能查到（`模式: 已禁用`、
`下次运行时间: N/A`），全量 CSV 扫描确认**本机 reseed 相关任务只有这一条**。

★ 教训：**先 `Query` 再 `Delete`**。少了这一步，就得靠猜"这条报错到底是在说权限还是说存在"。

**⑥ `.probe_done_<pid>` 之谜（已结案）**：`//iSunker-DS423/docker_ssd` 根和
`//iSunker-DS423/video` 根各有 5 个 0 字节的 `.probe_done_<pid>`
（2026-09-11 21:20~21:30，两处**同名且 mtime 精确到纳秒一致**）。

**查了一整轮全是错的**：仓库搜不到、git 历史搜不到、`docker_ssd` 下所有脚本搜不到、
本地 `D:\Projects` + `D:\tmp` 全量搜（含被 gitignore 的）也搜不到；从 PID 反推
`21293` 是奇数（Windows 的 PID 恒为 4 的倍数）⇒ 判定「NAS 上的、非本项目的进程」。
**这个判定是错的** —— 错在只搜了**当前**的代码，而它来自一段**只存在于聊天记录里、
从未进过仓库**的一次性探针脚本。

真凶是我们自己：09-11 晚为查「notify 的 spool 到底能写哪个目录」，我给用户写了一段
DSM「用户定义的脚本」探针，用户挂在**任务计划**上跑（21:20 / 21:25 / 21:30 正是 5 分钟一档，
21:27、21:28 两次是手工点）：

```sh
for d in /volume1/docker /volume1/video /volume2/docker_ssd; do
  if touch "$d/.probe_$$" 2>/dev/null; then
    mv -f "$d/.probe_$$" "$d/.probe_done_$$"
    mv -f "$d/.probe_done_$$" /dev/null 2>/dev/null   # ★ 就是这行
    rm -f "$d/.probe_$$"
  fi
done
```

★★ **`mv -f 文件 /dev/null` 不是删除。** `mv` 是拿这个文件去**替换** `/dev/null`
这个字符设备节点 —— 非 root 必然失败，而 `2>/dev/null` 把错误吞得干干净净，
文件原地留下。紧跟着那句真正该干的 `rm -f` 删的却是**已经被 `mv` 走的名字**
（`.probe_$$`），是**空操作**。一行 bug 制造了 10 个垃圾文件，且**完全不报错**。

**教训（这条比文件本身值钱）**：① 「把东西丢进 `/dev/null`」只有**重定向**（`> /dev/null`）
才是丢弃，`mv ... /dev/null` 是误解；② 清理动作写错时**必须让错误可见** ——
这里 `2>/dev/null` 把一个注定失败的操作伪装成了成功，是整件事唯一的隐蔽点；
③ 排查「这东西谁写的」时，**只在仓库和历史里搜是不够的**，一次性脚本不在版本库里，
先查当事人最近手动做过什么。

**处置**：10 个文件（两处各 5）都移进
`<compose>/notify/probe-artifacts-20260911/{from-video-root,from-docker_ssd-root}/`，
同名分目录，附 `README.txt` 说明来历。**全程只 `mv` 不 `rm`** ——
项目约束明写「别对 NAS 的 UNC 路径跑 `rm`」。video 根与 `#recycle` 那处现均已干净。

**⬜ 顺带两件要你确认**：① DSM「任务计划」里那条探针任务**删掉了吗**
（最后一个文件停在 09-11 21:30，之后没再冒出来，看着是删了，但请核一眼）；
② 探针也扫过 `/volume1/docker`，那里**很可能同样躺着 5 个**，它不是共享文件夹、
SMB 看不到，SSH 上去 `ls -la /volume1/docker/.probe_done_*` 确认一下。

> ★★ **② 已结案（2026-09-17）**：`//iSunker-DS423/docker/` **只有 `iSunker-DS423_1.hbk`
> 一项、没有 `.probe_done_*`** ⇒ **不用 SSH，SMB 就够**。
> ★ **本段原文有两处已被推翻，别再照抄**：① 该目录**是**共享文件夹、`UNC 可达`
> —— 「不是共享文件夹、SMB 看不到」**是错的**，实测详情与判据见 **§23.3 / §23.4**；
> ② 「SSH 上去确认」**不需要**（本机没有 SSH，`ERR-DSM-09`）。
> ★ 至于"为什么没有那 5 个文件"仍是**解释**（`touch` 权限不足、`if` 分支没进），**未坐实**。

> **① 的核查记录（2026-09-12 11:3x）**：想从旧会话记录里翻一条**现成的删除命令**
> 给用户，结论是**没有** —— 三条 reseed 任务当年都是在 **DSM GUI 里建**的，
> `synoschedtask` 全程**只被用来查看**（就是上面 14.7 ② 那条 `--get`）。
> 记录里出现过 `--set` / `--del` 字样，但都是**当时的猜测，从未真的执行过**，
> 所以**不给用户任何猜出来的参数**。
>
> 另一个**差点成立的误判**：09-11 23:51 那份 `--get` 输出里只有
> `reseed-drive-loop` / `reseed-notify-drain` / `reseed-notify-digest` +
> 备份任务，**没有**探针任务 —— 看着能证明「早删了」。但那条命令原文是
> `synoschedtask --get | grep -n -B2 -A20 'reseed'`，**输出是按 `reseed` 过滤过的**，
> 名字里不含 `reseed` 的任务根本不会出现。**过滤后的"没看见"不等于不存在。**
>
> 可靠的两步（`--get` 的可用性与输出格式均已实测确认）：
> ```sh
> sudo /usr/syno/bin/synoschedtask --get | grep 'Name:'   # 先看它还在不在
> sudo /usr/syno/bin/synoschedtask --help                 # 要删再查删除参数
> ```
> 输出形如 `Name: [reseed-drive-loop]` / `ID: [10]` / `State: [enabled]` / `User: [root]`。
> 不想折腾 CLI 就直接走 GUI：控制面板 → 任务计划 → 「计划的任务」→ 选中 → 删除。
>
> **✅ 结案（2026-09-12 11:40，不过滤的全量清单）**：用户跑的是
> `sudo /usr/syno/bin/synoschedtask --get | grep 'Name:'` —— **没有 `reseed` 过滤，
> 是全量枚举**，共 12 条，reseed 相关只剩两条：
>
> ```
> Name: [reseed-notify-drain]     AppName: [#common:command_line#]
> Name: [reseed-drive-loop]       AppName: [#common:command_line#]
> ```
>
> **没有探针任务** ⇒ 已删除，待办 ① 关上。
>
> **顺带发现**：`reseed-notify-digest` 也**不在**列表里。这**不是缺陷** ——
> 每日台账现在由 drive-loop 自己在批次末尾发（2026-09-12 11:12:22 实测
> `已投递通知(batch) → 每日台账` + `已投递每日台账（额度 + 趋势）`），
> 独立 digest 任务属**多余**。防重复投递由 `.daily-report.state` 负责。
>
> 另外`Name:` 块里**没有** `Run time / until` 字段（在下面十几行），
> 但 §14.7 ② 那个「小时位存错」的坑**不必再复核**：该任务今天
> 10:00 / 10:15 / 10:30 / 11:15 / 11:30 **全天都在正常触发**，
> 若 `until` 还是 `[2]:[45]` 根本跳不了 —— 说明早已修正。

---

