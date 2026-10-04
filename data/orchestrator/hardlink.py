"""可选的「预拆」模块——参考 hardlink_collection.py 的加固版。

主链路里建链接由 cross-seed 负责（只对匹配到的单片按发布名建链接）。
本模块保留「把大包所有单片先整体链接到某目录」的能力，供以下场景：
  - 你想手动往 :3060 加种（数据已就位）；
  - 将来接非 cross-seed 的匹配器；
  - 只是想在同卷内摆出一份可浏览的单集副本。

相比参考脚本，增加了：同卷校验、统计返回、更清晰的日志、dry-run。

★ 2026-09-13：链接类型不再写死硬链接，改由 matcher.link_type 决定
  （hardlink | symlink | reflink）。原因是「源文件被写穿」事故：硬链接下
  qB 校验失败就地重下 = 直接改写源文件；reflink 则写入时按块分离（COW），
  源文件不受影响。详见 README「源文件被写穿」一节与 cross-seed/config.js。
  注意这个键以前是**死的**（config.py 定义 + 校验，无人读），本模块是
  唯一的使用方 —— 不接上它，config.yml 里写 reflink 也只是好看。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from . import safety

log = logging.getLogger("reseed.hardlink")

# Linux 的 FICLONE：把 src 的全部 extent 克隆给 dst（BTRFS/XFS 上的 reflink）。
# 常量值 = _IOW(0x94, 9, int)，内核 ABI 稳定，直接写字面量。
_FICLONE = 0x40049409

LINK_TYPES = ("hardlink", "symlink", "reflink")


@dataclass
class LinkStats:
    linked: int = 0
    skipped: int = 0
    failed: int = 0
    dirs_created: int = 0


def make_link(src: Path, dst: Path, link_type: str) -> None:
    """按 link_type 在 dst 处为 src 建一个链接。失败时抛 OSError。

    - hardlink：os.link。同 inode ⇒ qB 就地重下会**写穿 src**（事故根源）。
    - symlink ：os.symlink，指向 src 的**绝对路径**。qB 会顺着写进 src，
                同样不安全；只在「想要一份能看的副本、且接受风险」时用。
    - reflink ：FICLONE。BTRFS/XFS 上瞬间完成、不占新空间，但写入时按块
                分离（COW）⇒ 重下只改 dst，**源文件不受影响**。这是我们
                要的那个。
    """
    if link_type == "hardlink":
        os.link(src, dst)
        return
    if link_type == "symlink":
        os.symlink(os.path.abspath(src), dst)
        return
    if link_type == "reflink":
        _reflink(src, dst)
        return
    raise OSError(f"未知的 link_type: {link_type!r}（可选 {LINK_TYPES}）")


def _reflink(src: Path, dst: Path) -> None:
    """用 FICLONE ioctl 做 COW 克隆。必须同文件系统（prestage 已先行校验）。"""
    try:
        import fcntl  # 仅 Linux/Unix 有
    except ImportError as e:                                     # pragma: no cover
        raise OSError(
            f"reflink 需要 Linux 的 fcntl.ioctl(FICLONE)，当前平台不支持: {src}"
        ) from e

    sfd = os.open(src, os.O_RDONLY)
    dfd = None
    try:
        # O_EXCL：dst 已存在就报错，绝不覆盖别人的数据
        dfd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            fcntl.ioctl(dfd, _FICLONE, sfd)
        except Exception:
            # ★ 关键：ioctl 失败时内核可能已经建出 dst（空文件/半成品），
            #   不清掉的话下次会命中「已存在则跳过」，留下一个 0 字节的假链接。
            os.close(dfd)
            dfd = None
            try:
                os.unlink(dst)
            except OSError:
                pass
            raise
    finally:
        os.close(sfd)
        if dfd is not None:
            os.close(dfd)


def prestage(source_dir: str, target_dir: str, *,
             link_type: str = "hardlink",
             dry_run: bool = False) -> LinkStats:
    """把 source_dir 的目录树链接到 target_dir（保持相对结构），已存在则跳过。

    link_type 见 make_link()；默认 hardlink 只为向后兼容，**新调用请显式传**。
    """
    src = Path(source_dir)
    dst = Path(target_dir)
    if not src.is_dir():
        raise FileNotFoundError(f"源目录不存在或不是目录: {src}")
    if link_type not in LINK_TYPES:
        raise OSError(f"未知的 link_type: {link_type!r}（可选 {LINK_TYPES}）")

    # 同卷校验（对尚未创建的 dst 用最近已存在祖先判断）
    # hardlink 与 reflink 都要求同一文件系统；symlink 不要求，但同卷也无害。
    if not safety.same_volume(src, dst):
        raise OSError(f"源与目标不在同一物理卷，无法建 {link_type} 链接：{src} <-> {dst}")

    stats = LinkStats()
    if not dry_run:
        dst.mkdir(parents=True, exist_ok=True)

    for root, _dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target_root = dst / rel if rel != "." else dst
        if not target_root.exists():
            if dry_run:
                log.info("[dry-run] 将创建目录: %s", target_root)
            else:
                target_root.mkdir(parents=True, exist_ok=True)
            stats.dirs_created += 1

        for fn in files:
            s = Path(root) / fn
            d = target_root / fn
            if d.exists() or d.is_symlink():   # 断链的 symlink exists() 为假，要单独看
                stats.skipped += 1
                continue
            if dry_run:
                log.info("[dry-run] 将建 %s 链接: %s -> %s", link_type, s, d)
                stats.linked += 1
                continue
            try:
                make_link(s, d, link_type)
                stats.linked += 1
            except OSError as e:
                log.error("建 %s 链接失败 %s: %s", link_type, fn, e)
                stats.failed += 1

    log.info("预拆完成 [%s -> %s] link_type=%s：链接 %d / 跳过 %d / 失败 %d / 新建目录 %d",
             src, dst, link_type, stats.linked, stats.skipped, stats.failed, stats.dirs_created)
    return stats
