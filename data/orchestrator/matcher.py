"""匹配器接口层（★可扩展接口的落脚点）。

Matcher 抽象类定义"给定一个大包任务 → 把其中的单片匹配成单种并注入 :3060 → 汇报"
的统一契约。本版本只实现 CrossSeedMatcher；IyuuMatcher / CustomMatcher 为
预留空壳，将来接别的大包/别的匹配方式时在此扩展，main.py 与 config 无需大改。
"""
from __future__ import annotations

import abc
import logging
import re
import time
from dataclasses import dataclass, field

from .config import AppConfig, JobConfig
from .crossseed_client import CrossSeedClient
from .qbit_client import QbitClient, classify

log = logging.getLogger("reseed.matcher")

# 匹配 torrent<->单片时忽略的通用词（分辨率/编码/来源等，避免误配）
_STOPWORDS = {
    "1080p", "2160p", "720p", "480p", "bluray", "blu", "ray", "webdl", "web",
    "webrip", "remux", "x264", "x265", "h264", "h265", "hevc", "avc", "hdr",
    "dts", "ddp", "aac", "flac", "atmos", "truehd", "the", "and", "frds",
}


def _tokens(name: str) -> set[str]:
    """从目录/种子名里抽出用于粗匹配的显著 token（小写、长度>=4、去停用词）。"""
    parts = re.split(r"[^0-9A-Za-z]+", name.lower())
    return {p for p in parts if len(p) >= 4 and p not in _STOPWORDS}


@dataclass
class SingleResult:
    single_name: str                 # 大包内子目录名（= 一部单片）
    search_triggered: bool = False
    matched_torrent: str | None = None   # 尽力对应到的 torrent 名（best-effort）
    state: str | None = None             # 该 torrent 的状态分类：seeding/checking/...
    progress: float | None = None


@dataclass
class MatchRun:
    job_name: str
    engine: str
    singles_total: int = 0
    searches_triggered: int = 0
    baseline_count: int = 0
    new_count: int = 0
    seeding: int = 0
    checking: int = 0
    errors: int = 0
    results: list[SingleResult] = field(default_factory=list)
    torrents: list[dict] = field(default_factory=list)
    note: str = ""


class Matcher(abc.ABC):
    """给未来的 IYUU / 自研匹配器预留的统一接口。"""

    engine: str = "abstract"

    @abc.abstractmethod
    def find_and_seed(self, job: JobConfig, *, dry_run: bool = False) -> MatchRun:
        ...


class CrossSeedMatcher(Matcher):
    engine = "cross-seed"

    def __init__(self, cfg: AppConfig, qbit: QbitClient, crossseed: CrossSeedClient):
        self.cfg = cfg
        self.qbit = qbit
        self.cs = crossseed

    def find_and_seed(self, job: JobConfig, *, dry_run: bool = False) -> MatchRun:
        from . import safety  # 延迟导入避免循环

        m = self.cfg.matcher
        category = self.cfg.category_for(job)
        singles = safety.list_single_dirs(job.source_dir, job.include, job.exclude)
        run = MatchRun(job_name=job.name, engine=self.engine, singles_total=len(singles))
        run.results = [SingleResult(single_name=p.name) for p in singles]

        # dry-run 必须保持只读：不创建分类，也不触发任何搜索或注入。
        if not dry_run:
            self.qbit.ensure_category(category, save_path=m.link_dir)
        baseline = {t.get("hash") for t in self.qbit.torrents(category=category)
                    if t.get("hash")}
        run.baseline_count = len(baseline)

        # 1) 逐个单片触发 cross-seed 按内容搜索
        for sr, path in zip(run.results, singles):
            if dry_run:
                continue
            resp = self.cs.search_data_path(str(path))
            sr.search_triggered = resp.ok
            if resp.ok:
                run.searches_triggered += 1
            time.sleep(1.0)  # 温和一点，别把守护进程/站点打爆

        if dry_run:
            run.note = "dry-run：只枚举并汇报，未触发搜索/注入"
            run.torrents = self.qbit.torrents(category=category)
            return run

        # 2) 轮询 :3060，等新注入的单种校验完成/开始做种
        deadline = time.time() + m.wait_timeout_sec
        torrents: list[dict] = []
        while True:
            torrents = self.qbit.torrents(category=category)
            new = [t for t in torrents if t.get("hash") and t["hash"] not in baseline]
            states = {classify(t.get("state", "")) for t in new}
            # 新种都已定型（做种/报错，无校验中/下载中）且确有新种 → 提前结束
            if new and not (states & {"checking", "downloading"}):
                break
            if time.time() >= deadline:
                log.warning("等待做种确认超时（%ss），按当前状态汇报", m.wait_timeout_sec)
                break
            time.sleep(m.poll_interval_sec)

        # 3) 汇总 + 尽力把新种对应回单片
        run.torrents = torrents
        new = [t for t in torrents if t["hash"] not in baseline]
        run.new_count = len(new)
        run.seeding = sum(1 for t in new if classify(t.get("state", "")) == "seeding")
        run.checking = sum(1 for t in new if classify(t.get("state", "")) == "checking")
        run.errors = sum(1 for t in new if classify(t.get("state", "")) == "error")
        self._map_back(run, new)
        return run

    @staticmethod
    def _map_back(run: MatchRun, new_torrents: list[dict]) -> None:
        """best-effort：靠共享 token 把新 torrent 对应回单片子目录名。

        注意：cross-seed 用 linkDir 时，种子名/内容路径是"站点发布名"，通常与大包
        子目录名不同，只能靠片名/年份等共享 token 粗对，仅供人看，不作准。
        """
        for sr in run.results:
            st = _tokens(sr.single_name)
            best = None
            for t in new_torrents:
                blob = f"{t.get('name', '')} {t.get('content_path', '')}"
                if st & _tokens(blob):
                    best = t
                    break
            if best is not None:
                sr.matched_torrent = best.get("name")
                sr.state = classify(best.get("state", ""))
                sr.progress = best.get("progress")


class IyuuMatcher(Matcher):  # ★预留：本版本不实现
    engine = "iyuu"

    def find_and_seed(self, job: JobConfig, *, dry_run: bool = False) -> MatchRun:
        raise NotImplementedError("IYUU 匹配器为预留项，本版本不实现（见项目计划 Phase 5）")


class CustomMatcher(Matcher):  # ★预留：将来自研站点匹配器
    engine = "custom"

    def find_and_seed(self, job: JobConfig, *, dry_run: bool = False) -> MatchRun:
        raise NotImplementedError("自研匹配器为预留项，本版本不实现")


def build_matcher(cfg: AppConfig) -> Matcher:
    """按 config.matcher.engine 造匹配器（含其依赖的 qB / cross-seed 客户端）。"""
    engine = cfg.matcher.engine
    if engine == "cross-seed":
        qbit = QbitClient(cfg.qbittorrent.url,
                          auth_mode=cfg.qbittorrent.auth_mode,
                          username=cfg.qbittorrent.username,
                          password=cfg.qbittorrent.password)
        cs = CrossSeedClient(cfg.matcher.crossseed_url, api_key=cfg.matcher.crossseed_api_key)
        return CrossSeedMatcher(cfg, qbit, cs)
    if engine == "iyuu":
        return IyuuMatcher()
    if engine == "custom":
        return CustomMatcher()
    raise NotImplementedError(f"未知匹配器: {engine}")
