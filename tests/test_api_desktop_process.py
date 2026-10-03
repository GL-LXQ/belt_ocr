"""验证真实子进程的桌面握手、SSE 和有序退出，不启动现场监测。"""

import asyncio
import json
import os
from pathlib import Path
import signal
import sys

import httpx
import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown_method", ["command", "eof", "signal"])
async def test_desktop_handshake_sse_and_shutdown(tmp_path, shutdown_method):
    """用临时配置启动真实后端并验证认证、完整事件和三种退出路径。

    Args:
        tmp_path: pytest 临时目录。
        shutdown_method: 通过 HTTP、stdin EOF 或系统信号结束进程。

    Returns:
        None  # 握手不包含令牌，后端清理后退出且不残留子进程
    """
    (tmp_path / "config.yaml").write_text(
        "application:\n  database_path: data.sqlite3\n  evidence_directory: evidence\n"
        "camera:\n  mvs_development_directory: sdk\nocr: {}\nfrequency: {}\nmachine: {}\n"
        "io:\n  modbus_serial_port: COM8\n  io_machine_channels: {}\n",
        encoding="utf-8",
    )
    token = "isolated-desktop-token-00000000000000000000"

    # Windows 直接启动实际解释器，并指定当前虚拟环境入口。
    executable_path = sys.executable
    process_environment = os.environ.copy()
    if os.name == "nt":
        executable_path = sys._base_executable
        process_environment["__PYVENV_LAUNCHER__"] = sys.executable

    # 使用管道启动后端并传入独立测试配置。
    process = await asyncio.create_subprocess_exec(
        executable_path, "-m", "src.api", "--desktop",
        cwd=Path(__file__).resolve().parents[1],
        env=process_environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        process.stdin.write(json.dumps({
            "token": token, "host": "127.0.0.1", "port": 0, "config_dir": str(tmp_path),
        }).encode() + b"\n")
        await process.stdin.drain()
        line = await asyncio.wait_for(process.stdout.readline(), timeout=10)
        assert line, (await process.stderr.read()).decode()
        handshake = json.loads(line)
        assert set(handshake) == {"protocol", "host", "port", "pid"}
        assert handshake["pid"] == process.pid
        base_url = f"http://127.0.0.1:{handshake['port']}"

        # HTTP 和 SSE 共用认证，断线重连始终获得完整快照。
        async with httpx.AsyncClient(base_url=base_url, timeout=5, trust_env=False) as client:
            assert (await client.get("/api/v1/health")).status_code == 401
            client.headers["Authorization"] = "Bearer " + token
            assert (await client.get("/api/v1/health")).json()["data"]["ready"]
            async with client.stream("GET", "/api/v1/events", headers={"Last-Event-ID": "999999"}) as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                lines = response.aiter_lines()
                assert (await anext(lines)).startswith("id: ")
                assert await anext(lines) == "event: snapshot"
                payload = json.loads((await anext(lines)).removeprefix("data: "))
                assert payload["data"]["status"] == "stopped"
                assert payload["data"]["machines"] == []
                assert payload["data"]["sessions"] == []

                # 保持 SSE 连接打开，退出命令仍能主动结束长连接。
                if shutdown_method == "command":
                    shutdown = await client.post("/api/v1/shutdown")
                    assert shutdown.json()["data"]["accepted"]
                elif shutdown_method == "eof":
                    process.stdin.close()
                else:
                    process.send_signal(signal.SIGTERM)
                await asyncio.wait_for(process.wait(), timeout=10)
        expected_exit = -signal.SIGTERM if shutdown_method == "signal" and os.name != "nt" else 0
        assert process.returncode == expected_exit, (await process.stderr.read()).decode()
        output = await process.stdout.read()
        assert output == b""
        assert token.encode() not in await process.stderr.read()
    finally:
        if process.returncode is None:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
