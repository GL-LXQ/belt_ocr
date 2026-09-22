"""验证 SystemRuntime 构造只组装依赖，机器清单和相机在启动阶段准备。"""

import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest

from system_runtime import SystemRuntime
from repo.machine_repo import MachineRepo
from local_test_support import FakeMvsSdk, build_config, create_machine_database


def test_construction_does_not_touch_database(tmp_path: Path) -> None:
    """验证构造 SystemRuntime 只登记依赖，不建库也不读机器。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        None  # 业务库文件未创建，机器运行对象为空
    """
    config = build_config(tmp_path)

    # 构造应用后检查业务库目录和机器运行对象。
    system_runtime = SystemRuntime(config)
    assert system_runtime.machines == {}
    assert not config.database_path.exists()
    assert not config.database_path.parent.exists()


def test_initialize_machines_binds_enabled_machines(tmp_path: Path) -> None:
    """验证建立机器运行对象时只登记启用机器并绑定来源序列号。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        None  # 机器编号取自增编号，相机与频率仪序列号已绑定
    """
    config = build_config(tmp_path)
    first_id, second_id, _ = create_machine_database(config.database_path, [
        {
            "machine_name": "一号皮带机",  # 机器名称
            "camera_serial": "CAM-A",  # 相机序列号
            "frequency_meter_serial": "FREQ-A",  # 频率仪序列号
        },
        {
            "machine_name": "二号皮带机",  # 机器名称
            "camera_serial": "CAM-B",  # 相机序列号
            "frequency_meter_serial": "FREQ-B",  # 频率仪序列号
        },
        {
            "machine_name": "停用皮带机",  # 机器名称
            "camera_serial": "CAM-C",  # 相机序列号
            "frequency_meter_serial": "FREQ-C",  # 频率仪序列号
            "enabled": False,  # 停用机器不建立处理器
        },
    ])

    # 建立机器运行对象后检查登记范围与机器身份。
    system_runtime = SystemRuntime(config)
    system_runtime.initialize_machines()
    assert list(system_runtime.machines) == [str(first_id), str(second_id)]
    second_machine = system_runtime.machines[str(second_id)].machine_config
    assert second_machine.camera_serial == "CAM-B"
    assert second_machine.frequency_meter_serial == "FREQ-B"


def test_start_rejects_missing_enabled_machines(tmp_path: Path, monkeypatch) -> None:
    """验证没有启用机器时启动被拒绝，且不加载硬件资源。

    Args:
        tmp_path: 测试临时目录。
        monkeypatch: 测试依赖替换工具。

    Returns:
        None  # 启动抛出业务异常，机器表已建好且实例锁已释放
    """
    config = build_config(tmp_path)
    loader = Mock()
    monkeypatch.setattr("system_runtime.load_mvs_sdk", loader)

    # 启动应在读取机器后立即拒绝，不加载 SDK。
    system_runtime = SystemRuntime(config)
    with pytest.raises(ValueError, match="没有启用的机器"):
        asyncio.run(system_runtime.start())
    loader.assert_not_called()
    assert not system_runtime.worker_tasks
    assert not system_runtime.database.lock_acquired

    # 启动被拒绝时机器表已建好，可直接新增机器。
    assert MachineRepo(config.database_path).list_enabled() == []


def test_start_reads_machines_added_after_construction(tmp_path: Path, monkeypatch) -> None:
    """验证启动阶段读取机器清单，构造之后新增的启用机器同样生效。

    Args:
        tmp_path: 测试临时目录。
        monkeypatch: 测试依赖替换工具。

    Returns:
        None  # 新增机器已建立处理器并打开相机，退出后相机与实例锁已释放
    """
    config = build_config(tmp_path)
    sdk = FakeMvsSdk()
    monkeypatch.setattr("system_runtime.load_mvs_sdk", lambda *arguments: sdk)
    system_runtime = SystemRuntime(config)

    # 构造之后写入启用机器，启动时按数据库记录建立处理器。
    machine_id = create_machine_database(config.database_path, [{
        "machine_name": "一号皮带机",  # 机器名称
        "camera_serial": "CAM-A",  # 相机序列号
        "frequency_meter_serial": "FREQ-A",  # 频率仪序列号
    }])[0]

    async def run_start_and_stop() -> None:
        """启动应用后立即释放资源。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 启动与资源释放均已完成
        """
        await system_runtime.start()
        await system_runtime.stop()

    asyncio.run(run_start_and_stop())

    # 检查机器登记、相机绑定和退出后的资源状态。
    assert list(system_runtime.machines) == [str(machine_id)]
    assert system_runtime.machines[str(machine_id)].camera.sdk_camera.serial == "CAM-A"
    assert sdk.closed
    assert sdk.cameras["CAM-A"].closed
    assert not system_runtime.database.lock_acquired
