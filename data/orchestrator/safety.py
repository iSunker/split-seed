"""安全预检工具：硬链接的前提是"同一物理卷"。

本模块只做只读检查 + 目录枚举，不改动任何东西（真正建链接是 cross-seed
或可选的 hardlink.prestage 负责）。
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path


def nearest_existing(path: str | Path) -> Path:
    """返回该路径本身或其最近的已存在祖先目录（用于对尚未创建的目标做同卷判断）。"""
    p = Path(path)
    while not p.exists():
        if p.parent == p:  # 到根了
            return p
        p = p.parent
    return p


def same_volume(path_a: str | Path, path_b: str | Path) -> bool:
    """两个路径是否在同一文件系统/逻辑卷（硬链接的前提）。

    对不存在的目标目录，回退到最近的已存在祖先来判断（因为 st_dev 取自
    实际挂载点，父目录与将来创建的子目录同卷）。
    """
    dev_a = os.stat(nearest_existing(path_a)).st_dev
    dev_b = os.stat(nearest_existing(path_b)).st_dev
    return dev_a == dev_b


def hardlink_count(path: str | Path) -> int:
    """文件的硬链接计数（st_nlink）。>=2 表示还有别处引用同一 inode。"""
    return os.stat(path).st_nlink


def list_single_dirs(source_dir: str | Path,
                     include: list[str] | None = None,
                     exclude: list[str] | None = None) -> list[Path]:
    """列出大包源目录下的一级子目录（每个 = 一部单片，作为一个匹配单元）。

    include/exclude 是对"子目录名"的 glob（大小写不敏感）。默认 include=['*']。
    """
    src = Path(source_dir)
    if not src.is_dir():
        return []
    inc = include or ["*"]
    exc = exclude or []
    out: list[Path] = []
    for child in sorted(src.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        if not any(fnmatch.fnmatch(name.lower(), p.lower()) for p in inc):
            continue
        if any(fnmatch.fnmatch(name.lower(), p.lower()) for p in exc):
            continue
        out.append(child)
    return out


@dataclass
class JobPreflight:
    name: str
    source_dir: str
    source_exists: bool
    source_is_dir: bool
    same_volume_as_link: bool
    single_count: int
    singles: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def preflight_job(name: str, source_dir: str, link_dir: str,
                  include: list[str] | None = None,
                  exclude: list[str] | None = None) -> JobPreflight:
    """对单个大包任务做只读预检。"""
    src = Path(source_dir)
    problems: list[str] = []

    exists = src.exists()
    is_dir = src.is_dir()
    if not exists:
        problems.append(f"源目录不存在: {source_dir}")
    elif not is_dir:
        problems.append(f"源路径不是目录: {source_dir}")

    # 同卷校验（链接目标与源必须同卷）
    same_vol = False
    try:
        same_vol = same_volume(source_dir if exists else nearest_existing(source_dir), link_dir)
        if exists and not same_vol:
            problems.append(f"源目录与 linkDir 不在同一物理卷，无法建链接：{source_dir}  <->  {link_dir}")
    except OSError as e:
        problems.append(f"同卷校验失败: {e}")

    singles = list_single_dirs(source_dir, include, exclude) if is_dir else []
    if is_dir and not singles:
        problems.append("源目录下没有匹配到任何单片子目录（检查 include/exclude 或目录结构）")

    return JobPreflight(
        name=name,
        source_dir=source_dir,
        source_exists=exists,
        source_is_dir=is_dir,
        same_volume_as_link=same_vol,
        single_count=len(singles),
        singles=[p.name for p in singles],
        problems=problems,
    )
