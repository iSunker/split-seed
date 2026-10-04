# drive-loop 镜像（`#58` 容器化的第二步）
#
# ★★ 与 `orchestrator/Dockerfile` 的关键差别：**没有 pip install**。
#   理由（2026-09-17 本机实测，不是推断）：
#     `scripts/drive-loop.py` 的 import 闭包 = `orchestrator.state`（+ 可选 `notify`），
#     而这几处的顶层 import **全是标准库**
#     （`orchestrator/__init__.py` 只有 docstring、`state.py` 纯 stdlib）。
#     `yaml` 只在 `orchestrator/config.py`，drive-loop **不走那条路径**。
#   ⇒ 立 `python:3.12-slim` 跑得起来（实证：在没装任何 pip 包的镜像里
#     `from orchestrator import state` 均 OK）。
#   ★ 2026-10-04：`notify` 模块已**整体剥离**到独立项目（NAS 侧发信脚本仍留 NAS），
#     ⇒ 本镜像**不再 COPY notify.py`，`drive-loop.py` 的 `import notify` 走 try 兜底
#     （`_notify = None`），通知出口静默降级为 no-op。见 `重构方案.md` §11。
#   ★ 若哪天 drive-loop 开始用第三方库，**必须在这里补 pip**，
#     而判据是「import 闭包里非标准库的有没有」——`D:\tmp\check_imports.py` 就是查这个的。
#
# ★★ 迁容器的**顺带收益**：摆脱 DSM 的 python 3.8.15（见 `ERR-PY-01`：
#   那是整条链路的单点依赖，而 `from __future__ import annotations` 是为它加的）。
#   这里直接 3.12 ⇒ 那几个兼容性顾虑在容器里**消失**。
FROM python:3.12-slim

LABEL org.opencontainers.image.title="reseed-drive-loop" \
      org.opencontainers.image.description="大包拆包·单种保种 · drive-loop（一次性容器）"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    TZ=Asia/Shanghai

WORKDIR /app

# ★★ 这三份代码在镜像里的**角色**（2026-09-18 更正 —— 原文写着
#   「代码是**挂进去**的，这里 COPY 只是为了让镜像**脱离挂载也能冒烟**」，
#   而实测证明**生产跑的就是这里 COPY 的这份**，那句话与事实不符）：
#
#     · **生产实际读的是挂载那份**：`run-resident.sh` 里
#         SCRIPT="$SELF_DIR/scripts/drive-loop.py"
#       而 `entrypoint` 给的是 `<compose>/drive-loop/run-resident.sh`（挂载区内）
#       ⇒ `SELF_DIR` = `<compose>/drive-loop` ⇒ 读到的是
#         `<compose>/drive-loop/scripts/drive-loop.py`。
#
#     · **但镜像里这份仍是"第二真源"** —— 只要挂载没盖住 /app，容器读到的就是它。
#       ★ 2026-09-18 实测踩到：Part B 的新渲染（② 口径）**从未生效**，
#         全 7 天日报的 metrics 里 `packpct` 都是 0，而三个位置的 `drive-loop.py`
#         是**三个不同的文件**（镜像那份 160023 字节，连 `PHASE_IDLE` 都没有）。
#         详见 SUMMARY §26.15。
#
#   ⇒ ★★ **纪律（两条，缺一不可）**
#     ① **改了 `scripts/drive-loop.py` / `reseed-state.py` / `build-farm.sh` 之后，
#        必须重建本镜像并 `docker load` 到 NAS** —— 光 `deploy.sh --apply`
#        （推挂载那份）**不够**，上面那次实测就是这么坏掉的。
#     ② **改完必须回读**：容器里 `/app/scripts/drive-loop.py` 与挂载那份
#        **md5 必须相同**。判据（NAS，单行）：
#          docker compose --profile drive-loop exec drive-loop \
#            md5sum /app/scripts/drive-loop.py drive-loop/scripts/drive-loop.py
#        ★ `run-resident.sh` 已加启动自检：读到不含 `PHASE_IDLE` 的旧代码会
#          **退非零并在 attempts.log 留一行** —— 别让"跑了旧代码"再变成静默。
#
#   本仓的形状（2026-10-05 收进 data/ 后）：`data/drive-loop/scripts/` 与
#   `data/orchestrator/` 是**兄弟**关系 —— 镜像里要复刻这个形状，
#   因为 `drive-loop.py` 靠 `sys.path.insert(ROOT)`（ROOT = 自己所在目录的**上级**）
#   找 `orchestrator`。
#   ⇒ 镜像里必须**保持同一相对形状**：/app/scripts/ + /app/orchestrator/
#     （★ 源路径随 data/ 收拢而变，**镜像内目标路径不变** —— 上面两条依赖它）。
#   ★ 构建上下文是**仓库根**（`docker build -f data/drive-loop/Dockerfile .`）；
#     NAS 上**不许** build（那边没有源码树）—— 只 pull。
COPY data/orchestrator/ /app/orchestrator/
COPY data/drive-loop/scripts/drive-loop.py   /app/scripts/drive-loop.py
# ★ 2026-10-04：`notify.py` **不再 COPY** —— 通知模块已整体剥离（见文件头与本仓
#   `重构方案.md` §11）。drive-loop.py 的 `import notify` 用 try 兜住 ⇒ 缺它不报错。
COPY data/drive-loop/scripts/reseed-state.py /app/scripts/reseed-state.py
# ★ `_run_verify()` 会起子进程跑它 ⇒ **必须在镜像里**（2026-09-17 读代码发现，`#58` 记过）
COPY data/drive-loop/scripts/build-farm.sh   /app/scripts/build-farm.sh
# ★★ 2026-10-05（§8.3，代码入镜像）：**常驻入口 `run-resident.sh` 也进镜像** ——
#   compose 的 `entrypoint` 从 `<挂载区>/drive-loop/run-resident.sh` 改为 `/app/run-resident.sh`。
#   ⇒ "挂载那份盖住镜像"这条病根（§26.15 packpct=0）从结构上消失。
COPY data/drive-loop/run-resident.sh /app/run-resident.sh

# ★★ 2026-10-05（B6 上线实测踩到）：**剥掉 CRLF**。
#   镜像的构建上下文是**本机工作树**，而本仓工作树是 **CRLF**（git 里存 LF，
#   `.gitattributes` 是 `text=auto eol=lf`）⇒ `COPY` 进来的 shell 脚本带 `\r`。
#   症状（NAS 实测）：容器**无限重启**，`docker logs` 只有一行
#       `/app/run-resident.sh: 87: set: Illegal option -`
#   —— `sh`(dash) 把 `set -eu\r` 的 `\r` 当成了一个**非法选项字符**。
#   ★ 为什么以前没暴露：代码此前是**挂载**进去的（SMB 同步会归一化行尾），
#     改成"代码入镜像"后**第一次**由 `COPY` 直接把工作树字节搬进镜像 ⇒ 踩中。
#   ★ 判据（能被证伪）：`docker run --rm --entrypoint sh <img> -c 'cat -A /app/run-resident.sh | grep -c "\^M"'` == 0。
#   只对 shell 脚本做（python 不在乎行尾，但一并剥掉更一致、也少一个口径）。
RUN for f in /app/run-resident.sh /app/scripts/*.sh /app/scripts/*.py; do \
        [ -f "$f" ] && sed -i 's/\r$//' "$f"; \
    done; \
    if grep -rlU $'\r' /app/run-resident.sh /app/scripts/ 2>/dev/null | grep -q .; then \
        echo "CRLF 未清干净"; exit 1; \
    fi

# 默认只打印用法，不真跑（真跑用 `docker compose run --rm drive-loop …`）
ENTRYPOINT ["python", "/app/scripts/drive-loop.py"]
CMD ["--help"]
