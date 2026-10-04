"""qBittorrent WebUI API v2 客户端（只用到本项目需要的接口）。

三种鉴权：
  - subnet_whitelist：qB 开了局域网/本机免密白名单，直接调，无需登录。
  - password：POST /api/v2/auth/login 拿 SID Cookie 后再调。
  - apikey：★ 2026-10-05 新增 —— 只用 `Authorization: Bearer <QB_API_KEY>` 头，
    **不登录、不改 qB 的 conf、不动白名单**。与 `state.py::_qb_auth_header` 同一套钥匙
    （`.env` 的 `QB_API_KEY`，别名 `QBIT_API_KEY`）。qB v5.2.4 后白名单为空会让
    172.x 网段的无鉴权请求 403 —— 这个模式就是那条 403 循环的"我们侧"修法。

所有请求都带 Referer/Origin，避免 qB 的 CSRF/HostHeader 校验挡下（reseed 实例
建议照你现有实例的做法关掉这些校验，见 patches/reseed-qbit.conf.md）。
"""
from __future__ import annotations

import logging
import os

from .http import HttpClient, HttpError

log = logging.getLogger("reseed.qbit")


def _qbit_api_key() -> str:
    """qB 只读 API Key（`QB_API_KEY` 优先，`QBIT_API_KEY` 兼容别名）。

    ★ 与 `config.py::_qbit_api_key` / `state.py::_qb_auth_header` **同一把钥匙、同一套
      变量名** —— 三处必须一致。这里**不缓存**，便于测试注入。
    """
    return (os.environ.get("QB_API_KEY") or os.environ.get("QBIT_API_KEY") or "").strip()


# torrent 状态分类（qB 4.6.x）
SEEDING_STATES = {"uploading", "stalledUP", "queuedUP", "forcedUP"}
CHECKING_STATES = {"checkingUP", "checkingDL", "checkingResumeData", "moving"}
ERROR_STATES = {"error", "missingFiles"}
DOWNLOADING_STATES = {"downloading", "stalledDL", "metaDL", "forcedDL", "queuedDL", "allocating"}
PAUSED_STATES = {"pausedUP", "pausedDL"}


def classify(state: str) -> str:
    if state in SEEDING_STATES:
        return "seeding"
    if state in CHECKING_STATES:
        return "checking"
    if state in ERROR_STATES:
        return "error"
    if state in DOWNLOADING_STATES:
        return "downloading"
    if state in PAUSED_STATES:
        return "paused"
    return state or "unknown"


class QbitError(Exception):
    pass


class QbitClient:
    def __init__(self, base_url: str, *, auth_mode: str = "subnet_whitelist",
                 username: str = "admin", password: str = "", timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.auth_mode = auth_mode
        self.username = username
        self.password = password
        # qB 要求带 Referer；关掉 HostHeaderValidation 后也无妨。
        headers = {"Referer": self.base, "Origin": self.base}
        # ★ 2026-10-05：apikey 模式 —— 每个请求都带 `Authorization: Bearer <key>`，
        #   不登录。与 `state.py::_qb_auth_header` 完全同形（同变量、同头）。
        if auth_mode == "apikey":
            k = _qbit_api_key()
            if k:
                headers["Authorization"] = f"Bearer {k}"
        self.http = HttpClient(timeout=timeout, default_headers=headers)

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    # -- 鉴权 / 连接 ------------------------------------------------------- #
    def connect(self) -> str:
        """建立连接（必要时登录），返回 qB 版本号，失败抛 QbitError。

        ★ `apikey` 与 `subnet_whitelist` 都是"不登录、直接调"——差别只是前者多带一个
          `Authorization` 头（在 `__init__` 里已注入）。所以这里两者走同一条路。
        """
        if self.auth_mode == "password":
            self.login()
        try:
            ver = self.app_version()
        except HttpError as e:
            raise QbitError(f"无法连接 qBittorrent（{self.base}）: {e}") from e
        log.info("已连接 qBittorrent %s @ %s (%s)", ver, self.base, self.auth_mode)
        return ver

    def login(self) -> None:
        try:
            r = self.http.post_form(self._url("/api/v2/auth/login"),
                                    {"username": self.username, "password": self.password})
        except HttpError as e:
            raise QbitError(f"登录请求失败: {e}") from e
        if not r.ok or r.text.strip() != "Ok.":
            raise QbitError(f"登录失败（HTTP {r.status}, body={r.text!r}）。"
                            f"检查用户名/密码，或改用 auth_mode=subnet_whitelist。")

    # -- 只读查询 --------------------------------------------------------- #
    def app_version(self) -> str:
        r = self.http.get(self._url("/api/v2/app/version"))
        if not r.ok or not r.text.strip():
            raise QbitError(f"获取 qBittorrent 版本失败（HTTP {r.status}）")
        return r.text.strip()

    def categories(self) -> dict:
        r = self.http.get(self._url("/api/v2/torrents/categories"))
        if not r.ok:
            raise QbitError(f"获取 qBittorrent 分类失败（HTTP {r.status}）")
        try:
            return r.json() or {}
        except ValueError as e:
            raise QbitError("qBittorrent 分类接口返回了无效 JSON") from e

    def torrents(self, *, category: str | None = None, tag: str | None = None) -> list[dict]:
        params: dict[str, str] = {}
        if category is not None:
            params["category"] = category
        if tag is not None:
            params["tag"] = tag
        r = self.http.get(self._url("/api/v2/torrents/info"), params=params or None)
        if not r.ok:
            raise QbitError(f"获取 torrents 列表失败（HTTP {r.status}）")
        return r.json() or []

    # -- 写操作（分类管理；注入交给 cross-seed，这里只保证分类存在）-------- #
    def ensure_category(self, name: str, save_path: str = "") -> None:
        if name in self.categories():
            return
        r = self.http.post_form(self._url("/api/v2/torrents/createCategory"),
                                {"category": name, "savePath": save_path})
        if not r.ok:
            raise QbitError(f"创建分类 {name!r} 失败（HTTP {r.status}, body={r.text!r}）")
        log.info("已创建分类: %s", name)
