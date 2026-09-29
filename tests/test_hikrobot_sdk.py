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
        getattr(handle, method_name).return_value = 0
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
