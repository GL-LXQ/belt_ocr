"""复用 mvs_tennis 的官方绑定加载、设备生命周期和 Buffer 复制方式。"""

import ctypes
import importlib
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace


class MvsError(RuntimeError):
    """记录 SDK 操作失败。"""


@dataclass(frozen=True)
class CameraFrame:
    """保存独立图像字节和 SDK 帧元数据。"""

    camera_serial: str
    frame_number: int
    device_timestamp: int
    host_timestamp: int
    received_monotonic: float
    width: int
    height: int
    pixel_type: int
    lost_packet_count: int
    data: bytes


@dataclass
class MvsCamera:
    """管理单台已打开相机和取流资源。"""

    binding: SimpleNamespace
    handle: object
    serial: str
    closed: bool = False
    faulted: bool = False
    grabbing: bool = False
    received_frame_count: int = 0
    capture_lock: object = field(default_factory=threading.Lock)
    buffer_lock: object = field(default_factory=threading.Lock)

    def start_grabbing(self) -> None:
        """清理历史缓存并启动本轮连续取流。

        Args:
            无外部参数。

        Returns:
            None  # 相机已启动取流
        """
        with self.buffer_lock:
            if self.closed or self.faulted:
                raise MvsError(f"相机不可用：{self.serial}")
            # 清空旧缓存，然后开启本轮取流。
            return_code = self.handle.MV_CC_ClearImageBuffer()
            if return_code != self.binding.errors.MV_OK:
                raise MvsError(f"ClearImageBuffer: 0x{return_code:08X}")
            return_code = self.handle.MV_CC_StartGrabbing()
            if return_code != self.binding.errors.MV_OK:
                self.faulted = True
                raise MvsError(f"StartGrabbing: 0x{return_code:08X}")
            self.grabbing = True

    def read_frame(self, stop_requested: threading.Event, timeout_ms: int) -> CameraFrame | None:
        """取得一帧、复制图像及元数据，并在返回前释放 SDK Buffer。

        Args:
            stop_requested: 本轮停止信号，置位后不再发起取帧。
            timeout_ms: 单次 SDK 取帧等待上限，单位毫秒。

        Returns:
            无数据或已停止时返回 None；成功返回以下 CameraFrame 示例：
                CameraFrame(
                    camera_serial="CAM001",  # 相机序列号
                    frame_number=1,  # SDK 帧编号
                    device_timestamp=100,  # 原始设备时间戳
                    host_timestamp=200,  # SDK 主机时间戳
                    received_monotonic=1.5,  # 主机取到帧的单调时间
                    width=2,  # 图像宽度
                    height=1,  # 图像高度
                    pixel_type=17301505,  # SDK 像素格式编号
                    lost_packet_count=0,  # SDK 报告的丢包数
                    data=b"\x01\x02",  # 独立图像字节
                )
        """
        with self.buffer_lock:
            if stop_requested.is_set():
                return None
            # 使用官方结构体获取 SDK 内部图像缓存。
            frame_buffer = self.binding.parameters.MV_FRAME_OUT()
            return_code = self.handle.MV_CC_GetImageBuffer(frame_buffer, timeout_ms)
            if return_code == self.binding.errors.MV_E_NODATA:
                return None
            if return_code != self.binding.errors.MV_OK:
                self.faulted = True
                raise MvsError(f"GetImageBuffer: 0x{return_code:08X}")
            self.received_frame_count += 1

            # 复制图像与数值字段，再归还 SDK Buffer。
            try:
                information = frame_buffer.stFrameInfo
                frame_length = int(information.nFrameLenEx or information.nFrameLen)
                if frame_length <= 0 or not frame_buffer.pBufAddr:
                    raise MvsError("SDK 返回空图像缓存")
                frame = CameraFrame(
                    camera_serial=self.serial,
                    frame_number=int(information.nFrameNum),
                    device_timestamp=(int(information.nDevTimeStampHigh) << 32) | int(information.nDevTimeStampLow),
                    host_timestamp=int(information.nHostTimeStamp),
                    received_monotonic=time.monotonic(),
                    width=int(information.nExtendWidth or information.nWidth),
                    height=int(information.nExtendHeight or information.nHeight),
                    pixel_type=int(information.enPixelType),
                    lost_packet_count=int(information.nLostPacket),
                    data=ctypes.string_at(frame_buffer.pBufAddr, frame_length),
                )
            finally:
                return_code = self.handle.MV_CC_FreeImageBuffer(frame_buffer)
                if return_code != self.binding.errors.MV_OK:
                    self.faulted = True
                    raise MvsError(f"FreeImageBuffer: 0x{return_code:08X}")
            return frame

    def stop_grabbing(self) -> None:
        """等待当前 Buffer 操作完成并停止相机取流。

        Args:
            无外部参数。

        Returns:
            None  # 相机已停止取流
        """
        with self.buffer_lock:
            if not self.grabbing:
                return
            # 停止取流，失败时禁止继续使用本相机。
            return_code = self.handle.MV_CC_StopGrabbing()
            if return_code != self.binding.errors.MV_OK:
                self.faulted = True
                raise MvsError(f"StopGrabbing: 0x{return_code:08X}")
            self.grabbing = False

    def close(self) -> None:
        """等待采集线程退出，停止取流并关闭和销毁设备。

        Args:
            无外部参数。

        Returns:
            None  # 设备资源已释放，失败时抛出 MvsError
        """
        with self.capture_lock:
            if self.closed:
                return
            errors = []
            # 依次尝试释放所有设备资源，收集清理错误。
            if self.grabbing:
                try:
                    self.stop_grabbing()
                except Exception as error:
                    errors.append(str(error))
            for operation in (self.handle.MV_CC_CloseDevice, self.handle.MV_CC_DestroyHandle):
                try:
                    return_code = operation()
                    if return_code != self.binding.errors.MV_OK:
                        errors.append(f"{operation.__name__}: 0x{return_code:08X}")
                except Exception as error:
                    errors.append(str(error))
            self.closed = True
            if errors:
                raise MvsError("; ".join(errors))


class MvsSdk:
    """管理官方绑定、SDK 初始化和已打开的相机。"""

    def __init__(self, binding: SimpleNamespace) -> None:
        """初始化 SDK 并建立相机资源集合。

        Args:
            binding: 包含 camera_class、parameters、errors 的官方绑定集合。

        Returns:
            None  # SDK 实例初始化完成
        """
        self.binding = binding
        self.cameras: dict[str, MvsCamera] = {}
        self.closed = False
        return_code = binding.camera_class.MV_CC_Initialize()
        if return_code != binding.errors.MV_OK:
            raise MvsError(f"Initialize: 0x{return_code:08X}")

    def enumerate_devices(self) -> tuple[object, list[dict]]:
        """枚举 GigE 和 USB 相机并返回设备列表及可读身份。

        Args:
            无外部参数。

        Returns:
            (
                device_list,  # SDK 设备列表，打开设备时使用
                [  # 可读设备信息
                    {
                        "index": 0,  # SDK 枚举下标
                        "serial": "CAM001",  # 相机序列号
                        "transport_type": 1,  # SDK 传输类型
                    },
                ],
            )
        """
        if self.closed:
            raise MvsError("SDK 已关闭")
        # 枚举 GigE 和 USB 设备。
        parameters = self.binding.parameters
        device_list = parameters.MV_CC_DEVICE_INFO_LIST()
        return_code = self.binding.camera_class.MV_CC_EnumDevices(
            parameters.MV_GIGE_DEVICE | parameters.MV_USB_DEVICE,
            device_list,
        )
        if return_code != self.binding.errors.MV_OK:
            raise MvsError(f"EnumDevices: 0x{return_code:08X}")

        # 从设备结构体提取序列号和传输类型。
        devices = []
        for device_index in range(device_list.nDeviceNum):
            information = ctypes.cast(
                device_list.pDeviceInfo[device_index],
                ctypes.POINTER(parameters.MV_CC_DEVICE_INFO),
            ).contents
            if information.nTLayerType == parameters.MV_GIGE_DEVICE:
                serial_buffer = information.SpecialInfo.stGigEInfo.chSerialNumber
            else:
                serial_buffer = information.SpecialInfo.stUsb3VInfo.chSerialNumber
            devices.append({
                "index": device_index,
                "serial": bytes(serial_buffer).split(b"\0", 1)[0].decode("utf-8"),
                "transport_type": int(information.nTLayerType),
            })
        return (
            device_list,
            devices,
        )

    def open_camera(
        self,
        serial: str,
        pixel_format: str | None = None,
        exposure_time_us: float | None = None,
        gain: float | None = None,
    ) -> MvsCamera:
        """按序列号打开相机，配置连续模式及指定的曝光和像素参数。

        Args:
            serial: 相机真实序列号。
            pixel_format: 可选 SDK 像素格式名称，省略时保持设备设置。
            exposure_time_us: 可选手动曝光时间，单位微秒。
            gain: 可选手动增益，使用设备节点单位。

        Returns:
            camera  # 已打开且尚未取流的 MvsCamera 资源对象
        """
        if serial in self.cameras and not self.cameras[serial].closed:
            raise MvsError(f"相机已经打开：{serial}")
        device_list, devices = self.enumerate_devices()
        device = next((device for device in devices if device["serial"] == serial), None)
        if device is None:
            raise MvsError(f"未找到相机：{serial}")

        # 创建相机句柄并保留独立资源对象。
        information = ctypes.cast(
            device_list.pDeviceInfo[device["index"]],
            ctypes.POINTER(self.binding.parameters.MV_CC_DEVICE_INFO),
        ).contents
        handle = self.binding.camera_class()
        return_code = handle.MV_CC_CreateHandle(information)
        if return_code != self.binding.errors.MV_OK:
            raise MvsError(f"CreateHandle: 0x{return_code:08X}")
        camera = MvsCamera(self.binding, handle, serial)
        try:
            # 使用 Control 权限打开设备。
            return_code = handle.MV_CC_OpenDevice(3, 0)
            if return_code != self.binding.errors.MV_OK:
                raise MvsError(f"OpenDevice({serial}): 0x{return_code:08X}")

            # 设置连续采集，并关闭逐帧触发。
            enum_parameters = {
                "AcquisitionMode": "Continuous",
                "TriggerMode": "Off",
            }
            if pixel_format is not None:
                enum_parameters["PixelFormat"] = pixel_format
            if exposure_time_us is not None:
                enum_parameters["ExposureAuto"] = "Off"
            if gain is not None:
                enum_parameters["GainAuto"] = "Off"
            for name, value in enum_parameters.items():
                return_code = handle.MV_CC_SetEnumValueByString(name, value)
                if return_code != self.binding.errors.MV_OK:
                    raise MvsError(f"{name}: 0x{return_code:08X}")

            # 应用手动曝光和增益参数。
            float_parameters = {
                "ExposureTime": exposure_time_us,
                "Gain": gain,
            }
            for name, value in float_parameters.items():
                if value is not None:
                    return_code = handle.MV_CC_SetFloatValue(name, value)
                    if return_code != self.binding.errors.MV_OK:
                        raise MvsError(f"{name}: 0x{return_code:08X}")

            # 为 GigE 相机设置 SDK 推荐的网络包大小。
            if device["transport_type"] == self.binding.parameters.MV_GIGE_DEVICE:
                packet_size = handle.MV_CC_GetOptimalPacketSize()
                if packet_size > 0:
                    return_code = handle.MV_CC_SetIntValue("GevSCPSPacketSize", packet_size)
                    if return_code != self.binding.errors.MV_OK:
                        raise MvsError(f"GevSCPSPacketSize: 0x{return_code:08X}")
        except Exception:
            try:
                camera.close()
            except Exception as cleanup_error:
                raise MvsError(f"相机打开失败，资源清理也失败：{cleanup_error}")
            raise
        self.cameras[serial] = camera
        return camera

    def close(self) -> None:
        """关闭所有相机并反初始化 SDK。

        Args:
            无外部参数。

        Returns:
            None  # SDK 资源已释放，失败时抛出 MvsError
        """
        if self.closed:
            return
        errors = []
        # 逐台关闭相机，最后反初始化 SDK。
        for camera in self.cameras.values():
            try:
                camera.close()
            except Exception as error:
                errors.append(str(error))
        return_code = self.binding.camera_class.MV_CC_Finalize()
        self.closed = True
        if return_code != self.binding.errors.MV_OK:
            errors.append(f"Finalize: 0x{return_code:08X}")
        if errors:
            raise MvsError("; ".join(errors))


# 保留 DLL 搜索目录句柄。
_dll_directory_handles = []


def load_mvs_sdk(development_directory: Path, dll_directory: Path | None = None) -> MvsSdk:
    """从本机 MVS 安装目录加载官方 Python 绑定并初始化 SDK。

    Args:
        development_directory: MVS Development 目录。
        dll_directory: 可选 DLL 目录，省略时查找 Windows 公共 MVS Runtime。

    Returns:
        sdk  # 已初始化的 MvsSdk 资源对象，使用结束后调用 close()
    """
    # 将官方 Python 绑定目录加入模块搜索路径。
    import_directory = development_directory / "Samples" / "Python" / "MvImport"
    if not (import_directory / "MvCameraControl_class.py").is_file():
        raise MvsError(f"未找到官方 Python 绑定：{import_directory}")
    if str(import_directory) not in sys.path:
        sys.path.insert(0, str(import_directory))

    # 配置对应 Python 位数的 Windows DLL 搜索路径。
    if dll_directory is None:
        architecture = "Win64_x64" if ctypes.sizeof(ctypes.c_void_p) == 8 else "Win32_i86"
        common_directory = Path(os.environ.get("CommonProgramFiles(x86)", r"C:\Program Files (x86)\Common Files"))
        dll_directory = common_directory / "MVS" / "Runtime" / architecture
    _dll_directory_handles.append(os.add_dll_directory(str(dll_directory)))
    os.environ["PATH"] = str(dll_directory) + os.pathsep + os.environ.get("PATH", "")

    # 延迟导入官方绑定并统一交给 SDK 封装。
    camera_module = importlib.import_module("MvCameraControl_class")
    binding = SimpleNamespace(
        camera_class=camera_module.MvCamera,
        parameters=importlib.import_module("CameraParams_header"),
        errors=importlib.import_module("MvErrorDefine_const"),
    )
    return MvsSdk(binding)
