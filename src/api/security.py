"""限制回环连接、可信来源和单次启动的临时访问令牌。"""

import ipaddress
import secrets
from urllib.parse import urlsplit

from starlette.responses import JSONResponse, Response


DEFAULT_ORIGINS = frozenset({
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
})


class LocalSessionMiddleware:
    """在所有 API 和证据请求前校验本机来源与 Bearer 令牌。"""

    def __init__(self, app, token: str, allowed_origins: frozenset[str] = DEFAULT_ORIGINS) -> None:
        """保存当前进程的临时访问策略。

        Args:
            app: 内层 ASGI 应用。
            token: 仅存在于本次进程内存的高熵令牌。
            allowed_origins: 允许发起跨来源访问的准确来源集合。

        Returns:
            None  # 中间件已配置，未持久化任何令牌
        """
        self.app = app
        self.token = token
        self.allowed_origins = allowed_origins

    async def __call__(self, scope, receive, send) -> None:
        """校验连接和认证，安全响应预检并添加禁止缓存头。

        Args:
            scope: ASGI 请求元信息。
            receive: ASGI 请求消息接收函数。
            send: ASGI 响应消息发送函数。

        Returns:
            None  # 请求已拒绝或交给业务应用处理
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope["headers"]}
        origin = headers.get("origin")
        try:
            host = urlsplit("//" + headers.get("host", "")).hostname
        except ValueError:
            host = None
        client = scope.get("client")
        try:
            local_client = client is not None and ipaddress.ip_address(client[0]).is_loopback
        except ValueError:
            local_client = False

        # 拒绝远程连接、重绑定主机名和未授权网页来源。
        error = None
        if not local_client or host not in {"127.0.0.1", "localhost", "::1"}:
            error = (403, "仅允许本机回环连接。")
        elif origin is not None and origin not in self.allowed_origins:
            error = (403, "请求来源不受信任。")
        elif scope["method"] != "OPTIONS":
            supplied = headers.get("authorization", "")
            if not secrets.compare_digest(supplied.encode("latin-1"), ("Bearer " + self.token).encode("utf-8")):
                error = (401, "会话已失效，请重新连接桌面后端。")
        if error is not None:
            error_headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
            if origin in self.allowed_origins:
                error_headers.update({"Access-Control-Allow-Origin": origin, "Vary": "Origin"})
            response = JSONResponse(
                {"success": False, "data": None, "message": error[1]},
                status_code=error[0],
                headers=error_headers,
            )
            await response(scope, receive, send)
            return

        # 预检仅返回精确来源和实际使用的请求头。
        cors_headers = {
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type, Last-Event-ID",
            "Access-Control-Max-Age": "600",
        }
        if origin is not None:
            cors_headers["Access-Control-Allow-Origin"] = origin
            cors_headers["Vary"] = "Origin"
        if scope["method"] == "OPTIONS":
            await Response(status_code=204, headers=cors_headers)(scope, receive, send)
            return

        async def send_private(message):
            """为业务响应附加本机会话的缓存和来源限制。

            Args:
                message: ASGI 响应消息。

            Returns:
                None  # 消息已附加安全头并发送
            """
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_headers.extend((key.encode(), value.encode()) for key, value in cors_headers.items())
                response_headers.extend([
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                ])
                message = dict(message, headers=response_headers)
            await send(message)
        await self.app(scope, receive, send_private)
