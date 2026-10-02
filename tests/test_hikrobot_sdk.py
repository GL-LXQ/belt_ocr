"""验证 MVS 相机启动参数写入和失败清理。"""

import ctypes
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from camera.hikrobot_sdk import MvsError, MvsSdk


class DeviceInformation(ctypes.Structure):
    """提供测试用相机设备结构体。"""

    _fields_ = [("device_number", ctypes.c_int)]


def create_camera_sdk() -> tuple[MvsSdk, Mock]:
    """建立可记录参数写入与资源释放的测试相机 SDK。

    Args:
        无外部参数。

    Returns:
        返回示例：
            (
                sdk,  # 测试用 SDK 对象
                handle,  # 记录相机节点调用的测试句柄
            )
    """
    # 创建全部相机生命周期节点的成功响应。
    handle = Mock()
    for method_name in (
        "MV_CC_CreateHandle", "MV_CC_OpenDevice", "MV_CC_CloseDevice",
        "MV_CC_DestroyHandle", "MV_CC_SetEnumValueByString",
        "MV_CC_SetFloatValue", "MV_CC_SetBoolValue", "MV_CC_SetIntValue",
    ):
        method = getattr(handle, method_name)
        method.return_value = 0
        method.__name__ = method_name
    handle.MV_CC_GetOptimalPacketSize.return_value = 1500

    # 建立包含一台 GigE 相机的 SDK 绑定和枚举结果。
    camera_class = Mock(return_value=handle)
    camera_class.MV_CC_Initialize.return_value = 0
    binding = SimpleNamespace(
        camera_class=camera_class,
        parameters=SimpleNamespace(
            MV_CC_DEVICE_INFO=DeviceInformation, MV_GIGE_DEVICE=1
        ),
        errors=SimpleNamespace(MV_OK=0),
    )
    sdk = MvsSdk(binding)
    device_information = DeviceInformation(1)
    device_list = SimpleNamespace(pDeviceInfo=[ctypes.pointer(device_information)])
    sdk.enumerate_devices = Mock(return_value=(device_list, [
        {"index": 0, "serial": "camera-1", "transport_type": 1},
    ]))
    return sdk, handle


def test_open_camera_writes_parameters_in_order() -> None:
    """确认相机在取流前依次写入公共图像和频闪参数。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 所有节点按要求写入，且增益零值没有跳过
    """
    # 打开测试相机并应用完整运行参数。
    sdk, handle = create_camera_sdk()
    camera = sdk.open_camera(
        "camera-1",
        pixel_format="Mono8",
        exposure_time_us=80.0,
        gain=0.0,
        line_selector="Line2",
        line_mode="Strobe",
        line_source="ExposureStartActive",
        strobe_enabled=True,
    )

    # 核对参数节点的写入内容与完整顺序。
    parameter_calls = [
        handle_call for handle_call in handle.mock_calls
        if handle_call[0].startswith("MV_CC_Set")
    ]
    assert parameter_calls == [
        call.MV_CC_SetEnumValueByString("AcquisitionMode", "Continuous"),
        call.MV_CC_SetEnumValueByString("TriggerMode", "Off"),
        call.MV_CC_SetEnumValueByString("PixelFormat", "Mono8"),
        call.MV_CC_SetEnumValueByString("ExposureAuto", "Off"),
        call.MV_CC_SetEnumValueByString("GainAuto", "Off"),
        call.MV_CC_SetFloatValue("ExposureTime", 80.0),
        call.MV_CC_SetFloatValue("Gain", 0.0),
        call.MV_CC_SetEnumValueByString("LineSelector", "Line2"),
        call.MV_CC_SetEnumValueByString("LineMode", "Strobe"),
        call.MV_CC_SetEnumValueByString("LineSource", "ExposureStartActive"),
        call.MV_CC_SetBoolValue("StrobeEnable", True),
        call.MV_CC_SetIntValue("GevSCPSPacketSize", 1500),
    ]
    assert sdk.cameras["camera-1"] is camera
    handle.MV_CC_StartGrabbing.assert_not_called()


@pytest.mark.parametrize("opening_method", ["MV_CC_OpenDevice", "MV_CC_SetEnumValueByString"])
@pytest.mark.parametrize("cleanup_method", ["MV_CC_CloseDevice", "MV_CC_DestroyHandle"])
@pytest.mark.parametrize("cleanup_raises", [False, True])
def test_open_camera_preserves_unknown_error_after_cleanup_failure(
    opening_method: str,
    cleanup_method: str,
    cleanup_raises: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """确认打开相机的未知异常不会被清理故障转换为设备异常。

    Args:
        opening_method: 抛出未知异常的 SDK 打开或配置方法。
        cleanup_method: 清理过程中失败的 SDK 方法。
        cleanup_raises: 为 True 时清理抛错，否则返回失败码。
        caplog: pytest 提供的日志收集工具。

    Returns:
        返回示例：
            None  # 原始异常保持不变，清理故障已记录且句柄销毁已尝试
    """
    # 同时注入打开异常和资源清理故障。
    sdk, handle = create_camera_sdk()
    opening_error = RuntimeError("SDK 绑定发生未知异常")
    getattr(handle, opening_method).side_effect = opening_error
    cleanup_operation = getattr(handle, cleanup_method)
    if cleanup_raises:
        cleanup_operation.side_effect = RuntimeError("清理连接失败")
    else:
        cleanup_operation.return_value = 1

    # 清理故障不能覆盖最先发生的异常或改变故障分类。
    with pytest.raises(RuntimeError) as raised:
        sdk.open_camera("camera-1")
    assert raised.value is opening_error
    assert "相机打开失败后的资源清理失败" in caplog.text
    assert "camera_serial=camera-1" in caplog.text

    # 打开失败时仍依次尝试关闭设备和销毁句柄。
    handle.MV_CC_CloseDevice.assert_called_once_with()
    handle.MV_CC_DestroyHandle.assert_called_once_with()
    assert "camera-1" not in sdk.cameras


@pytest.mark.parametrize(
    ("opening_method", "expected_message"),
    [
        ("MV_CC_OpenDevice", "OpenDevice(camera-1)"),
        ("MV_CC_SetEnumValueByString", "AcquisitionMode"),
    ],
)
def test_open_camera_preserves_sdk_error_after_cleanup_failure(
    opening_method: str,
    expected_message: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """确认相机清理失败后仍报告最先发生的 SDK 错误码和操作。

    Args:
        opening_method: 返回失败码的相机打开或配置方法。
        expected_message: 原始错误应包含的 SDK 操作或参数名称。
        caplog: pytest 提供的日志收集工具。

    Returns:
        返回示例：
            None  # 原始设备故障可见，清理故障独立记录
    """
    # 使用不同错误码区分最初故障和后续清理故障。
    sdk, handle = create_camera_sdk()
    getattr(handle, opening_method).return_value = 2
    handle.MV_CC_CloseDevice.return_value = 3

    # 原始设备故障保持原有分类和诊断信息。
    with pytest.raises(MvsError) as raised:
        sdk.open_camera("camera-1")
    assert expected_message in str(raised.value)
    assert "0x00000002" in str(raised.value)
    assert "0x00000003" in caplog.text

    # 关闭失败不阻止继续销毁句柄。
    handle.MV_CC_CloseDevice.assert_called_once_with()
    handle.MV_CC_DestroyHandle.assert_called_once_with()
    assert "camera-1" not in sdk.cameras


@pytest.mark.parametrize(
    ("method_name", "parameter_name"),
    [
        ("MV_CC_SetEnumValueByString", "LineSource"),
        ("MV_CC_SetFloatValue", "Gain"),
        ("MV_CC_SetBoolValue", "StrobeEnable"),
    ],
)
def test_open_camera_cleans_handle_after_parameter_failure(
    method_name: str,
    parameter_name: str,
) -> None:
    """确认关键节点写入失败时关闭设备并销毁句柄。

    Args:
        method_name: 返回错误码的 SDK 设置方法。
        parameter_name: 返回错误码的相机参数名。

    Returns:
        返回示例：
            None  # 失败相机未登记，设备和句柄均已关闭
    """
    # 指定一个参数节点写入失败。
    sdk, handle = create_camera_sdk()

    def fail_parameter(current_name: str, parameter_value: object) -> int:
        """使指定相机参数写入返回错误码。

        Args:
            current_name: 本次写入的相机参数名。
            parameter_value: 本次写入的相机参数值。

        Returns:
            返回示例：
                1  # 指定参数写入失败
        """
        return 1 if current_name == parameter_name else 0

    getattr(handle, method_name).side_effect = fail_parameter

    # 打开相机并核对失败后的资源清理。
    with pytest.raises(MvsError, match=f"设置相机参数 {parameter_name} 失败"):
        sdk.open_camera(
            "camera-1",
            pixel_format="Mono8",
            exposure_time_us=80.0,
            gain=0.0,
            line_selector="Line2",
            line_mode="Strobe",
            line_source="ExposureStartActive",
            strobe_enabled=True,
        )
    handle.MV_CC_CloseDevice.assert_called_once_with()
    handle.MV_CC_DestroyHandle.assert_called_once_with()
    assert "camera-1" not in sdk.cameras
    handle.MV_CC_StartGrabbing.assert_not_called()
