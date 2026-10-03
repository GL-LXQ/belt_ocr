"""以单进程 Uvicorn 启动本机后端并完成桌面安全握手。"""

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import sys
import threading

# 同时兼容源码模块入口和桌面打包入口。
SOURCE_DIRECTORY = Path(__file__).resolve().parents[1]
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

import uvicorn

from src.api.app import create_app


class DesktopServer(uvicorn.Server):
    """在关闭 HTTP 长连接前等待现场资源清理。"""

    async def shutdown(self, sockets=None) -> None:
        """先结束监测和 SSE，再执行 Uvicorn 的正常退出流程。

        Args:
            sockets: Uvicorn 使用的监听套接字列表。

        Returns:
            None  # 监测和 HTTP 服务均已停止
        """
        application = self.config.app
        if hasattr(application.state, "runtime_host"):
            await application.state.runtime_host.shutdown()
        await super().shutdown(sockets)


async def serve_backend(options: dict, desktop: bool, output) -> int:
    """绑定随机回环端口，发布握手并持续运行直到有序退出。

    Args:
        options: 已解析的临时启动参数。
        desktop: 是否启用桌面 stdin 生命周期监督。
        output: 仅用于握手的标准输出流。

    Returns:
        0  # 正常退出
        2  # 启动或设备清理失败
    """
    configuration_directory = Path(options["config_dir"]).expanduser().resolve()
    ocr_path = Path(options["ocr_config_path"]).expanduser().resolve() if options.get("ocr_config_path") else None
    if not (configuration_directory / "config.yaml").is_file():
        raise ValueError("配置目录必须包含 config.yaml。")
    if ocr_path is not None and not ocr_path.is_file():
        raise ValueError("OCR 配置文件不存在。")
    if options.get("host", "127.0.0.1") != "127.0.0.1":
        raise ValueError("后端只允许绑定 127.0.0.1。")
    port = options.get("port", 0)
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("端口必须是 0 到 65535 的整数。")
    token = options.get("token") or secrets.token_urlsafe(32)
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("桌面会话令牌至少需要 32 个字符。")

    # 先绑定端口，避免探测空闲端口和启动之间的抢占窗口。
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", port))
    listener.listen(128)
    listener.setblocking(False)
    application = create_app(configuration_directory, token, ocr_path)
    server = DesktopServer(uvicorn.Config(application, access_log=False, log_config=None, workers=1))
    server_task = asyncio.create_task(server.serve(sockets=[listener]))

    # 正式监听并完成 lifespan 初始化后才发送握手。
    try:
        while not server.started and not server_task.done():
            await asyncio.sleep(0.02)
        if not server.started:
            await server_task
            return 2
        handshake = {
            "protocol": 1,
            "host": "127.0.0.1",
            "port": listener.getsockname()[1],
            "pid": os.getpid(),
        }
        if not desktop:
            handshake["token"] = token
        output.write(json.dumps(handshake) + "\n")
        output.flush()
        shutdown_event = application.state.shutdown_requested
        loop = asyncio.get_running_loop()

        if desktop:
            def watch_parent_input():
                """在桌面父进程关闭管道时触发同一有序退出流程。

                Args:
                    无外部参数。

                Returns:
                    None  # stdin 关闭后已经向主事件循环提交退出通知
                """
                while os.read(sys.stdin.fileno(), 4096):
                    pass
                if not loop.is_closed():
                    loop.call_soon_threadsafe(shutdown_event.set)
            threading.Thread(target=watch_parent_input, name="desktop-parent-watch", daemon=True).start()

        # 退出命令与操作系统信号共用先清理、后关闭服务的顺序。
        shutdown_waiter = asyncio.create_task(shutdown_event.wait())
        try:
            completed, _ = await asyncio.wait({server_task, shutdown_waiter}, return_when=asyncio.FIRST_COMPLETED)
            if shutdown_waiter in completed:
                await application.state.runtime_host.shutdown()
                server.should_exit = True
            await server_task
        finally:
            shutdown_waiter.cancel()
            await asyncio.gather(shutdown_waiter, return_exceptions=True)
        return 2 if application.state.runtime_host.cleanup_failed else 0
    finally:
        listener.close()


def main() -> int:
    """读取桌面管道或开发参数，并运行唯一后端进程。

    Args:
        无外部参数，命令行参数由 argparse 读取。

    Returns:
        0  # 后端正常退出
        2  # 启动参数或资源释放失败
    """
    parser = argparse.ArgumentParser(description="BeltVision 本机 FastAPI 后端")
    parser.add_argument("--desktop", action="store_true", help="从 stdin 读取桌面临时启动参数")
    parser.add_argument("--config-dir", type=Path, default=SOURCE_DIRECTORY.parent / "config")
    parser.add_argument("--ocr-config", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    arguments = parser.parse_args()
    output = sys.stdout
    sys.stdout = sys.stderr
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    try:
        if arguments.desktop:
            line = sys.stdin.buffer.readline(65537)
            if len(line) > 65536:
                raise ValueError("桌面启动消息过长。")
            options = json.loads(line)
            if not isinstance(options, dict) or "config_dir" not in options or "token" not in options:
                raise ValueError("桌面启动消息缺少 config_dir 或 token。")
        else:
            options = {
                "config_dir": str(arguments.config_dir),
                "ocr_config_path": str(arguments.ocr_config) if arguments.ocr_config else None,
                "port": arguments.port,
            }
        return asyncio.run(serve_backend(options, arguments.desktop, output))
    except (OSError, ValueError, TypeError) as error:
        logging.getLogger(__name__).error("后端启动失败：%s", error)
        return 2
    finally:
        sys.stdout = output


if __name__ == "__main__":
    raise SystemExit(main())
