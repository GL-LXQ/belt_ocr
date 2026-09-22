"""验证退出流程由 Machine 自行清理本机资源，并保持既有中断与故障语义。"""

import asyncio
from pathlib import Path
from unittest.mock import patch

from enums import SessionState
from local_test_support import FakeMvsSdk, build_config, create_machine_database
from system_runtime import SystemRuntime


def test_stop_clears_machine_internals_after_interrupting_cycle(tmp_path: Path) -> None:
    """验证退出先中断活动周期，随后本机周期、任务与采集状态全部清空。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        返回示例：
            None  # 周期按 CYCLE_INTERRUPTED 结算，机器内部状态与后台任务均已清空
    """
    config = build_config(tmp_path, capture_window_ms=200, camera_timeout_ms=20, shutdown_timeout_ms=300)
    create_machine_database(config.database_path, [{
        "machine_name": "一号皮带机",  # 机器名称
        "camera_serial": "CAM-A",  # 相机序列号
        "frequency_meter_serial": "FREQ-A",  # 频率仪序列号
    }])
    sdk = FakeMvsSdk()

    async def run_start_and_stop() -> None:
        """受理启动后退出应用，并核对退出后的本机状态。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 退出后的本机状态检查已全部通过
        """
        with patch("system_runtime.load_mvs_sdk", lambda *arguments: sdk):
            system_runtime = SystemRuntime(config)
            await system_runtime.start()

            # 受理启动信号，留下一个活动周期。
            await system_runtime.handle_start("1")
            machine = system_runtime.machines["1"]
            session = machine.current_session

            # 退出后核对周期结算与本机残留状态。
            await system_runtime.stop()
            pending_tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

        assert "CYCLE_INTERRUPTED" in session.errors
        assert session.state == SessionState.FAILED
        assert machine.current_session is None
        assert machine.frequency_adapter.active_session_id is None
        assert machine.deadline_tasks == {}
        assert machine.recognition_task is None
        assert machine.camera.current_capture is None
        assert machine.camera.delivery_task is None
        assert not machine.camera.is_capturing
        assert not pending_tasks
        assert not system_runtime.worker_tasks
        assert sdk.closed

    asyncio.run(run_start_and_stop())


def test_stop_records_program_failed_when_system_already_failed(tmp_path: Path) -> None:
    """验证系统已有故障时退出，未完成周期按 PROGRAM_FAILED 结算。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        返回示例：
            None  # 周期已按程序故障结算，本机内部状态已清空
    """
    config = build_config(tmp_path, capture_window_ms=200, camera_timeout_ms=20)
    create_machine_database(config.database_path, [{
        "machine_name": "一号皮带机",  # 机器名称
        "camera_serial": "CAM-A",  # 相机序列号
        "frequency_meter_serial": "FREQ-A",  # 频率仪序列号
    }])
    sdk = FakeMvsSdk()

    async def run_failed_stop() -> None:
        """受理启动后登记故障并退出，核对周期故障原因。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 故障原因与本机状态检查已全部通过
        """
        with patch("system_runtime.load_mvs_sdk", lambda *arguments: sdk):
            system_runtime = SystemRuntime(config)
            await system_runtime.start()
            await system_runtime.handle_start("1")

            # 登记故障后退出，跳过正常收尾直接释放资源。
            machine = system_runtime.machines["1"]
            session = machine.current_session
            system_runtime.failure = RuntimeError("模拟程序故障")
            await system_runtime.stop()

        assert session.errors == ["PROGRAM_FAILED"]
        assert session.state == SessionState.FAILED
        assert machine.current_session is None
        assert machine.frequency_adapter.active_session_id is None
        assert machine.recognition_task is None

    asyncio.run(run_failed_stop())


def test_release_resources_clears_unclosed_cycle_with_given_shutdown_reason(tmp_path: Path) -> None:
    """验证 Machine 自行释放未关闭周期时按传入的退出原因结算。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        返回示例：
            None  # 周期按 SHUTDOWN_TIMEOUT 失败结算，本机任务与采集已清空
    """
    config = build_config(tmp_path, capture_window_ms=200, camera_timeout_ms=20)
    create_machine_database(config.database_path, [{
        "machine_name": "一号皮带机",  # 机器名称
        "camera_serial": "CAM-A",  # 相机序列号
        "frequency_meter_serial": "FREQ-A",  # 频率仪序列号
    }])
    sdk = FakeMvsSdk()

    async def run_machine_release() -> None:
        """受理启动后直接调用本机资源释放，核对退出原因与残留状态。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 本机资源释放结果检查已全部通过
        """
        with patch("system_runtime.load_mvs_sdk", lambda *arguments: sdk):
            system_runtime = SystemRuntime(config)
            await system_runtime.start()
            await system_runtime.handle_start("1")
            machine = system_runtime.machines["1"]
            session = machine.current_session

            # 事件入口已停止交付，直接执行本机资源释放。
            system_runtime.releasing_resources = True
            await machine.release_resources("SHUTDOWN_TIMEOUT")

            # 取消系统级任务，避免测试结束时残留待取消任务。
            for task in system_runtime.worker_tasks:
                task.cancel()
            await asyncio.gather(*system_runtime.worker_tasks, return_exceptions=True)

        assert session.errors == ["SHUTDOWN_TIMEOUT"]
        assert session.state == SessionState.FAILED
        assert machine.current_session is None
        assert machine.frequency_adapter.active_session_id is None
        assert machine.deadline_tasks == {}
        assert machine.recognition_task is None
        assert machine.camera.delivery_task is None

    asyncio.run(run_machine_release())
