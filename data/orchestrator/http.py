"""极简 HTTP 客户端（标准库 urllib）。

刻意不引入 requests/httpx，让容器只有 PyYAML 一个第三方依赖，
在 NAS（ARM64）上构建最省事、最稳。支持 GET / POST(form|json) 与
跨请求保持 Cookie（qBittorrent 登录后要带 SID）。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Any


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str]

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text) if self.body else None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class HttpError(Exception):
    pass


class HttpClient:
    def __init__(self, timeout: float = 30.0, default_headers: dict[str, str] | None = None):
        self.timeout = timeout
        self.default_headers = default_headers or {}
        self._cookies = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookies)
        )

    def _do(self, method: str, url: str, *,
            data: bytes | None = None,
            headers: dict[str, str] | None = None) -> Response:
        h = dict(self.default_headers)
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return Response(status=resp.status,
                                body=resp.read(),
                                headers={k.lower(): v for k, v in resp.headers.items()})
        except urllib.error.HTTPError as e:  # 4xx/5xx 也返回，交给调用方判断
            return Response(status=e.code,
                            body=e.read() if hasattr(e, "read") else b"",
                            headers={k.lower(): v for k, v in (e.headers or {}).items()})
        except urllib.error.URLError as e:
            raise HttpError(f"连接失败 {url}: {e.reason}") from e

    def get(self, url: str, params: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None) -> Response:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        return self._do("GET", url, headers=headers)

    def post_form(self, url: str, data: dict[str, Any] | None = None,
                  headers: dict[str, str] | None = None) -> Response:
        body = urllib.parse.urlencode(data or {}).encode("utf-8")
        h = {"Content-Type": "application/x-www-form-urlencoded"}
        if headers:
            h.update(headers)
        return self._do("POST", url, data=body, headers=h)

    def post_json(self, url: str, obj: Any,
                  headers: dict[str, str] | None = None) -> Response:
        body = json.dumps(obj).encode("utf-8")
        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)
        return self._do("POST", url, data=body, headers=h)
