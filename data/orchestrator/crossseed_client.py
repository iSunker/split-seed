"""cross-seed 守护进程 HTTP API 客户端（触发"按数据(data-based)搜索"）。

⚠️ 版本差异提醒：cross-seed 的 API 路径/参数/鉴权方式随版本有变化，部署时
请对照你实际安装版本的官方文档 https://www.cross-seed.org/ 核对以下几点：
  - 触发 data 搜索的端点：多为 POST /api/webhook，body 传 {"path": "<目录>"}；
    也有版本用表单字段 data-dirs / infoHash 等。
  - 鉴权：apiKey 可能走 HTTP 头 X-Api-Key，或查询串 ?apikey=xxx。
  - 传入的 path 通常必须落在 config.js 里 dataDirs 允许的范围内。
本客户端两种鉴权方式都带上（头 + 查询串），端点用 /api/webhook；若你的版本
不同，改这里一处即可。
"""
from __future__ import annotations

import logging
from urllib.parse import urlencode

from .http import HttpClient, HttpError, Response

log = logging.getLogger("reseed.crossseed")


class CrossSeedError(Exception):
    pass


class CrossSeedClient:
    def __init__(self, base_url: str, *, api_key: str = "", timeout: float = 60.0):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.http = HttpClient(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.api_key} if self.api_key else {}

    def _q(self, path: str) -> str:
        url = f"{self.base}{path}"
        if self.api_key:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urlencode({'apikey': self.api_key})}"
        return url

    # ★★ 2026-09-19：这仨状态码**语义完全不同，混成一个布尔就会误判**（本函数原先正是如此）。
    #   背景：`preflight` 长期把 cross-seed 报成"[??] 暂不可达"，而日志同刻显示
    #   `cross-seed 返回 HTTP 404 @ http://cross-seed:2468` —— 404 与"连不上"**不是一回事**。
    #   根因（读源码定论，cross-seed v6.13.7）：它的**HTTP 面只有一条路由 `/api/webhook`**，
    #   根路径 `/` **永远 404**，而**「路由存在但方法不对」回 405**。
    #   ⇒ 原实现按 `response.ok`（= 2xx）判活 ⇒ **恒判 False**，与 daemon 是否活着无关。
    #   ★ 判据的教训：**"能不能连上"与"这个端点存不存在"是两个问题**；
    #     拿"某端点返回 2xx"当"进程活着"的判据，会得到一个**恒假**的观测
    #     （同族：`B.10` 第 1 条"能解释" ≠ "已确认"；探针**指向**错对象而非没分辨力）。
    UP = 200
    NOT_FOUND = 404      # ★ 根路径的正常应答 —— 它恰恰**证明** daemon 在应答
    METHOD_NOT_ALLOWED = 405  # ★ 路由在、方法不对 ⇒ daemon 在、路由也在

    def ping(self, *, strict: bool = False) -> bool:
        """探活：daemon **是否在应答**。

        ★ 判据不是"根路径返回 2xx"（那在本 daemon 上**恒假** —— 根路径就该是 404），
        而是"**它是个 HTTP 应答**"：能拿到任何状态码 ⇒ 进程活着且 HTTP 层在工作。
        只有**拿不到应答**（连接被拒/超时/DNS 失败 ⇒ HttpError）才算不可达。

        ★ `strict=True` 恢复旧语义（要求 2xx），只为兼容/对照，**不要用于 preflight** ——
        它会在功能完全正常时报"不可达"。
        """
        try:
            response = self.http.get(self.base, headers=self._headers())
        except HttpError as e:
            log.warning("cross-seed 不可达 @ %s: %s", self.base, e)
            return False
        if strict:
            if not response.ok:
                log.warning("cross-seed 返回 HTTP %s @ %s（strict）", response.status, self.base)
                return False
            return True
        # 非 strict：拿到应答即算活着；把状态码分档写进日志，免得下次又误读
        if response.status in (self.NOT_FOUND, self.METHOD_NOT_ALLOWED):
            log.info("cross-seed 在应答 @ %s（HTTP %s —— 根路径无路由，符合预期）",
                     self.base, response.status)
        elif not response.ok:
            log.warning("cross-seed 应答异常 @ %s: HTTP %s", self.base, response.status)
        return True

    def search_data_path(self, path: str) -> Response:
        """对某个数据目录（一部单片）触发按内容搜索/匹配/注入。"""
        try:
            r = self.http.post_json(self._q("/api/webhook"), {"path": path}, headers=self._headers())
        except HttpError as e:
            raise CrossSeedError(f"触发 cross-seed 搜索失败（path={path}）: {e}") from e
        if not r.ok:
            raise CrossSeedError(
                f"cross-seed 搜索失败（HTTP {r.status}, path={path}, body={r.text[:200]!r}）"
            )
        log.info("已触发 cross-seed 搜索: %s", path)
        return r
