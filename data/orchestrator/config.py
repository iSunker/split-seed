"""配置模型与加载/校验。

仅依赖标准库 + PyYAML，尽量减少 NAS 上的构建风险。
未知键会被忽略（并发出警告），已知的"预留字段"是一等公民，
即使本版本尚未使用，也保留在模型里，方便将来扩展别的大包/能力。
"""
from __future__ import annotations

import dataclasses
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("reseed.config")


class ConfigError(Exception):
    """配置非法时抛出，附带可读信息。"""


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass
class QbitConfig:
    url: str = "http://127.0.0.1:3060"
    # subnet_whitelist = 依赖 qB 的局域网免密白名单（推荐，对齐你另一个 qB 实例）
    # password         = 用用户名/密码登录
    auth_mode: str = "subnet_whitelist"
    username: str = "admin"
    password: str = ""
    # 注入单种统一使用的分类，便于隔离/汇报/清理
    category: str = "reseed-singles"
    # NAS 路径 <-> 容器路径映射；本环境 /volume1/video 是 1:1，通常留默认即可
    path_mapping: dict[str, str] = field(default_factory=lambda: {"/volume1/video": "/volume1/video"})

    def validate(self) -> None:
        if self.auth_mode not in ("subnet_whitelist", "password"):
            raise ConfigError(f"qbittorrent.auth_mode 只能是 subnet_whitelist 或 password，收到: {self.auth_mode!r}")
        if self.auth_mode == "password" and not self.password:
            raise ConfigError("auth_mode=password 时必须提供 qbittorrent.password（建议放 .env 再注入）")
        if not self.url.startswith(("http://", "https://")):
            raise ConfigError(f"qbittorrent.url 必须以 http(s):// 开头，收到: {self.url!r}")


@dataclass
class MatcherConfig:
    # ★预留：cross-seed（本版实现） | iyuu | custom
    engine: str = "cross-seed"
    crossseed_url: str = "http://cross-seed:2468"
    crossseed_api_key: str = ""
    # cross-seed 匹配宽松度。★ 2026-09-13：这个键**不是决定项** —— 真正的闸门在
    #   cross-seed/config.js，那里把 matchMode 与 linkType 绑在一起（硬链接下强制
    #   strict）。这里读它只是为了「配置里能看到全貌」，改它不会改变 cross-seed 行为。
    match_mode: str = "partial"
    # 内容匹配（名称+大小）→ 建议 false，即注入后做一次校验(recheck)确认数据一致
    skip_recheck: bool = False
    # ★ 2026-09-13 起这是**活键**：orchestrator/hardlink.py::prestage() 会读它
    #   （此前只被定义和校验，无人引用）。
    #   hardlink：同 inode ⇒ qB 就地重下会写穿源文件（事故根源，除非 matchMode=strict）
    #   symlink ：同样会写穿源；只在明确接受风险时用
    #   reflink ：COW，写入按块分离 ⇒ 源文件不受影响。BTRFS/XFS 可用。
    #   必须与 cross-seed/config.js 的 linkType 保持同一个值，否则两条建链接的路径
    #   行为不一致（一半硬链接、一半 reflink）。
    link_type: str = "hardlink"  # hardlink | symlink | reflink
    # cross-seed 建链接（做种数据）的位置，必须与各大包源目录在同一物理卷
    # ★ 2026-09-12 目录搬迁：旧根 /volume1/video/download/reseed_singles 已废弃，
    #   数据全在 reseed/ 父目录下（SUMMARY §18）。
    #   ★ 这个默认值**实际是死代码** —— hlink/config.yml 里 `link_dir: "${LINK_DIR}"`
    #     总会命中，而 load_config() 用 os.path.expandvars 从容器环境展开它。
    #     但它是把**新路径**写错时的静默陷阱：一旦 config.yml 少写这一行，
    #     这里就会悄悄退回一个**看起来合理、但已经不存在**的路径，
    #     而不是报错。所以跟着一起改，别留旧的。
    link_dir: str = "/volume1/video/download/reseed/reseed_singles"
    # 单次 run 里，触发匹配后最多等待做种确认的秒数
    wait_timeout_sec: int = 900
    poll_interval_sec: int = 15

    SUPPORTED_ENGINES = ("cross-seed", "iyuu", "custom")

    def validate(self) -> None:
        if self.engine not in self.SUPPORTED_ENGINES:
            raise ConfigError(f"matcher.engine 只能是 {self.SUPPORTED_ENGINES}，收到: {self.engine!r}")
        if self.engine in ("iyuu", "custom"):
            raise ConfigError(f"matcher.engine={self.engine!r} 为预留项，本版本尚未实现（仅 cross-seed 可用）")
        if self.link_type not in ("hardlink", "symlink", "reflink"):
            raise ConfigError(f"matcher.link_type 非法: {self.link_type!r}")


@dataclass
class JobConfig:
    name: str
    source_dir: str
    enabled: bool = True
    include: list[str] = field(default_factory=lambda: ["*"])
    exclude: list[str] = field(default_factory=list)
    # ---- ★预留：未来大包/未来能力（本版本除 category 外均不生效）----
    category: str | None = None          # 覆盖全局分类
    indexers: list[str] = field(default_factory=list)   # 限定只用某些 Prowlarr 索引器
    matcher_engine: str | None = None    # 单任务覆盖匹配器
    iyuu_handoff: bool = False           # 做种成功后触发 IYUU 扩散（预留，不实现）
    tags: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if not self.name:
            raise ConfigError("job.name 不能为空")
        if not self.source_dir:
            raise ConfigError(f"job[{self.name}].source_dir 不能为空")


@dataclass
class AppConfig:
    qbittorrent: QbitConfig
    matcher: MatcherConfig
    jobs: list[JobConfig]

    def validate(self) -> None:
        self.qbittorrent.validate()
        self.matcher.validate()
        if not self.jobs:
            raise ConfigError("config.yml 至少要有一个 job")
        seen: set[str] = set()
        for j in self.jobs:
            j.validate()
            if j.name in seen:
                raise ConfigError(f"job.name 重复: {j.name!r}")
            seen.add(j.name)
            eng = j.matcher_engine or self.matcher.engine
            if eng != "cross-seed":
                raise ConfigError(f"job[{j.name}] 使用的匹配器 {eng!r} 本版本未实现")

    def enabled_jobs(self) -> list[JobConfig]:
        return [j for j in self.jobs if j.enabled]

    def job(self, name: str) -> JobConfig:
        for j in self.jobs:
            if j.name == name:
                return j
        raise ConfigError(f"找不到 job: {name!r}（已知: {[j.name for j in self.jobs]}）")

    def category_for(self, job: JobConfig) -> str:
        return job.category or self.qbittorrent.category


# --------------------------------------------------------------------------- #
# 加载
# --------------------------------------------------------------------------- #
def _build(cls, data: dict[str, Any], *, context: str):
    """按 dataclass 字段名从 dict 取值构造，未知键仅告警忽略。"""
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - known
    if unknown:
        log.warning("%s 含未知字段（已忽略）: %s", context, sorted(unknown))
    kwargs = {k: v for k, v in data.items() if k in known}
    return cls(**kwargs)


def load_config(path: str | Path) -> AppConfig:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"配置文件不存在: {p}")
    try:
        # 先展开 ${VAR}（来自 .env / compose 环境），让 config.yml 与 .env 单点维护
        # IP、路径、密码等。未定义的 ${VAR} 原样保留（不报错）。
        text = os.path.expandvars(p.read_text(encoding="utf-8"))
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"config.yml 解析失败: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError("config.yml 顶层必须是映射（键值对）")

    qbit = _build(QbitConfig, raw.get("qbittorrent", {}) or {}, context="qbittorrent")
    matcher = _build(MatcherConfig, raw.get("matcher", {}) or {}, context="matcher")

    jobs_raw = raw.get("jobs", []) or []
    if not isinstance(jobs_raw, list):
        raise ConfigError("config.yml 的 jobs 必须是列表")
    jobs = [_build(JobConfig, j or {}, context=f"jobs[{i}]") for i, j in enumerate(jobs_raw)]

    cfg = AppConfig(qbittorrent=qbit, matcher=matcher, jobs=jobs)
    cfg.validate()
    return cfg
