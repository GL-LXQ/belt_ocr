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

import numpy as np


PIXEL_TYPE_MONO8 = 0x01080001


class MvsError(RuntimeError):
    """记录 SDK 操作失败。"""


@dataclass(frozen=True)
class CameraFrame:
    """保存独立图像字节和 SDK 帧元数据。"""

    camera_serial: str  # 拍到本帧的相机序列号
    frame_number: int  # SDK 帧编号
    device_timestamp: int  # 设备原始时间戳
    host_timestamp: int  # SDK 主机时间戳
    received_monotonic: float  # 主机取到本帧的单调时间
    width: int  # 图像宽度
    height: int  # 图像高度
    pixel_type: int  # SDK 像素格式编号
    lost_packet_count: int  # SDK 报告的丢包数
    data: bytes  # 复制出的独立图像字节


def convert_mono8_frame_to_array(frame: CameraFrame) -> np.ndarray:
    """将 Mono8 相机帧转换为二维灰度图数组。

    Args:
        frame: 包含 Mono8 原始字节和图像尺寸的相机帧。

    Returns:
        返回示例：
            np.array(
                [
                    [1, 2],  # 第一行像素
                    [3, 4],  # 第二行像素
                ],
                dtype=np.uint8,  # 单通道八位像素
            )
    """
    # 确认相机帧使用 Mono8 像素格式。
    if frame.pixel_type != PIXEL_TYPE_MONO8:
        raise ValueError(f"仅支持 Mono8 相机帧，收到像素格式: {frame.pixel_type}")

    # 核对原始字节数与图像尺寸。
    expected_length = frame.width * frame.height
    if len(frame.data) != expected_length:
        raise ValueError(
            f"Mono8 图像字节数与尺寸不符: 收到 {len(frame.data)}，预期 {expected_length}"
        )

    # 将原始字节恢复为二维灰度图。
    return np.frombuffer(frame.data, dtype=np.uint8).reshape(frame.height, frame.width)


@dataclass
class MvsCamera:
    """管理单台已打开相机和取流资源。"""

    binding: SimpleNamespace  # 官方绑定集合
    handle: object  # SDK 相机连接句柄
    serial: str  # 相机真实序列号
    closed: bool = False  # 设备是否已关闭
    faulted: bool = False  # 设备是否已出现故障
    grabbing: bool = False  # 设备是否处于取流状态
    received_frame_count: int = 0  # SDK 已交付帧数
    capture_lock: object = field(default_factory=threading.Lock)  # 采集与关闭的独占锁
    encoding_lock: object = field(default_factory=threading.Lock)  # 同一设备编码的串行锁

    def start_grabbing(self) -> None:
        """启动本轮连续取流并清理历史缓存。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 相机已启动取流
        """
        # 设备已关闭或有故障时拒绝启动。
        if self.closed or self.faulted:
            raise MvsError(f"相机不可用：{self.serial}")

        # 启动连续取流，失败时标记设备故障。
        return_code = self.handle.MV_CC_StartGrabbing()
        if return_code != self.binding.errors.MV_OK:
            self.faulted = True
            raise MvsError(f"启动相机采集失败（StartGrabbing），错误码：0x{return_code:08X}")

        # 登记设备已进入取流状态。
        self.grabbing = True

        # 清空取流缓存，后续读取本轮新收到的图像。
        return_code = self.handle.MV_CC_ClearImageBuffer()
        if return_code != self.binding.errors.MV_OK:
            raise MvsError(f"清空相机图像缓存失败（ClearImageBuffer），错误码：0x{return_code:08X}")

    def read_frame(self, stop_requested: threading.Event, timeout_ms: int) -> CameraFrame | None:
        """取得一帧、复制图像及元数据，并在返回前释放 SDK Buffer。

        Args:
            stop_requested: 本轮停止信号，置位后不再发起取帧。
            timeout_ms: 单次 SDK 取帧等待上限，单位毫秒。

        Returns:
            返回示例：
                None  # 已收到停止通知或 SDK 无数据
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
        # 已收到停止通知时不再取帧。
        if stop_requested.is_set():
            return None

        # 使用官方结构体获取 SDK 内部图像缓存。
        frame_buffer = self.binding.parameters.MV_FRAME_OUT()
        return_code = self.handle.MV_CC_GetImageBuffer(frame_buffer, timeout_ms)

        # 无图像时返回空结果。
        if return_code == self.binding.errors.MV_E_NODATA:
            return None

        # 其他 SDK 错误登记为设备故障。
        if return_code != self.binding.errors.MV_OK:
            self.faulted = True
            raise MvsError(f"读取相机图像失败（GetImageBuffer），错误码：0x{return_code:08X}")

        # 累计 SDK 已成功交付的帧数。
        self.received_frame_count += 1

        # 复制图像与数值字段，再归还 SDK Buffer。
        try:
            # 取出帧信息并计算本帧字节数。
            information = frame_buffer.stFrameInfo
            frame_length = int(information.nFrameLenEx or information.nFrameLen)

            # 字节数或缓存地址无效时按故障处理。
            if frame_length <= 0 or not frame_buffer.pBufAddr:
                raise MvsError("SDK 返回空图像缓存")

            # 复制帧元数据，并通过 string_at 将图像复制为独立 bytes。
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
            # 无论复制成功或失败，都归还本次取得的 SDK Buffer。
            return_code = self.handle.MV_CC_FreeImageBuffer(frame_buffer)
            if return_code != self.binding.errors.MV_OK:
                self.faulted = True
                raise MvsError(f"释放相机图像缓存失败（FreeImageBuffer），错误码：0x{return_code:08X}")

        # 交付程序独立持有的图像。
        return frame

    def encode_image(self, frame: CameraFrame) -> bytes:
        """通过 MVS SDK 将独立原始帧编码成 JPG 图片。

        Args:
            frame: 已复制到程序内存的图像及像素格式信息。

        Returns:
            返回示例：
                b"\xff\xd8...\xff\xd9"  # 完整 JPG 文件字节，示例省略图片内容
        """
        # 为当前帧建立输入缓存和图片输出缓存。
        source_buffer = (ctypes.c_ubyte * len(frame.data)).from_buffer_copy(frame.data)
        output_capacity = frame.width * frame.height * 4 + 2048
        output_buffer = (ctypes.c_ubyte * output_capacity)()

        # 填入原始图像地址、字节数、像素格式和尺寸。
        parameters = self.binding.parameters.MV_SAVE_IMAGE_PARAM_EX3()
        parameters.pData = ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte))
        parameters.nDataLen = len(frame.data)
        parameters.enPixelType = frame.pixel_type
        parameters.nWidth = frame.width
        parameters.nHeight = frame.height

        # 设置 JPG 文件输出、压缩质量和 Bayer 插值参数。
        parameters.pImageBuffer = ctypes.cast(output_buffer, ctypes.POINTER(ctypes.c_ubyte))
        parameters.nBufferSize = output_capacity
        parameters.enImageType = self.binding.parameters.MV_Image_Jpeg
        parameters.nJpgQuality = 85
        parameters.iMethodValue = 1

        # 串行执行同一设备的图片编码。
        with self.encoding_lock:
            return_code = self.handle.MV_CC_SaveImageEx3(parameters)

        # 编码失败时抛出异常。
        if return_code != self.binding.errors.MV_OK:
            raise MvsError(f"图像编码为 JPG 失败（SaveImageEx3(JPEG)），错误码：0x{return_code:08X}")

        # 编码返回空内容时抛出异常。
        if parameters.nImageLen == 0:
            raise MvsError("SaveImageEx3(JPEG) 未返回图片内容")

        # 复制编码后的 JPG 文件字节并返回。
        return ctypes.string_at(output_buffer, parameters.nImageLen)

    def stop_grabbing(self) -> None:
        """停止相机取流并更新取流状态。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 相机已停止取流
        """
        # 未取流时直接返回。
        if not self.grabbing:
            return

        # 停止取流，失败时标记设备故障。
        return_code = self.handle.MV_CC_StopGrabbing()
        if return_code != self.binding.errors.MV_OK:
            self.faulted = True
            raise MvsError(f"停止相机采集失败（StopGrabbing），错误码：0x{return_code:08X}")

        # 登记设备已退出取流状态。
        self.grabbing = False

    def close(self) -> None:
        """等待采集线程退出，停止取流并关闭和销毁设备。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 设备资源已释放，失败时抛出 MvsError
        """
        # 独占相机采集锁后执行清理。
        with self.capture_lock:
            # 已关闭的设备直接返回。
            if self.closed:
                return

            # 收集清理过程中的错误。
            errors = []

            # 仍在取流时先停止取流。
            if self.grabbing:
                try:
                    self.stop_grabbing()
                except Exception as error:
                    errors.append(str(error))

            # 依次关闭设备与销毁句柄，收集失败原因。
            for operation in (self.handle.MV_CC_CloseDevice, self.handle.MV_CC_DestroyHandle):
                try:
                    return_code = operation()
                    if return_code != self.binding.errors.MV_OK:
                        errors.append(f"关闭相机或释放连接句柄失败（{operation.__name__}），错误码：0x{return_code:08X}")
                except Exception as error:
                    errors.append(str(error))

            # 登记设备已关闭。
            self.closed = True

            # 存在清理错误时汇总抛出。
            if errors:
                raise MvsError("; ".join(errors))


class MvsSdk:
    """管理官方绑定、SDK 初始化和已打开的相机。"""

    def __init__(self, binding: SimpleNamespace) -> None:
        """初始化 SDK 并建立相机资源集合。

        Args:
            binding: 包含 camera_class、parameters、errors 的官方绑定集合。

        Returns:
            返回示例：
                None  # SDK 实例初始化完成
        """
        # 登记官方绑定、相机集合与关闭状态。
        self.binding = binding
        self.cameras: dict[str, MvsCamera] = {}
        self.closed = False

        # 初始化相机驱动，失败时抛出异常。
        return_code = binding.camera_class.MV_CC_Initialize()
        if return_code != binding.errors.MV_OK:
            raise MvsError(f"初始化相机驱动失败（Initialize），错误码：0x{return_code:08X}")

    def enumerate_devices(self) -> tuple[object, list[dict]]:
        """枚举 GigE 和 USB 相机并返回设备列表及可读身份。

        Args:
            无外部参数。

        Returns:
            返回示例：
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
        # SDK 已关闭时拒绝枚举。
        if self.closed:
            raise MvsError("SDK 已关闭")

        # 按 GigE 与 USB 两类传输枚举设备。
        parameters = self.binding.parameters
        device_list = parameters.MV_CC_DEVICE_INFO_LIST()
        return_code = self.binding.camera_class.MV_CC_EnumDevices(
            parameters.MV_GIGE_DEVICE | parameters.MV_USB_DEVICE,
            device_list,
        )

        # 枚举失败时抛出异常。
        if return_code != self.binding.errors.MV_OK:
            raise MvsError(f"查找相机设备失败（EnumDevices），错误码：0x{return_code:08X}")

        # 从设备结构体提取序列号和传输类型。
        devices = []
        for device_index in range(device_list.nDeviceNum):
            # 取出本台设备的 SDK 信息结构。
            information = ctypes.cast(
                device_list.pDeviceInfo[device_index],
                ctypes.POINTER(parameters.MV_CC_DEVICE_INFO),
            ).contents

            # 按传输类型取对应分支的序列号字段。
            if information.nTLayerType == parameters.MV_GIGE_DEVICE:
                serial_buffer = information.SpecialInfo.stGigEInfo.chSerialNumber
            else:
                serial_buffer = information.SpecialInfo.stUsb3VInfo.chSerialNumber

            # 登记本台设备的可读身份。
            devices.append({
                "index": device_index,
                "serial": bytes(serial_buffer).split(b"\0", 1)[0].decode("utf-8"),
                "transport_type": int(information.nTLayerType),
            })

        # 返回 SDK 设备列表与可读身份。
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
        line_selector: str | None = None,
        line_mode: str | None = None,
        line_source: str | None = None,
        strobe_enabled: bool | None = None,
    ) -> MvsCamera:
        """按序列号打开相机，写入采集、图像和频闪参数。

        Args:
            serial: 相机真实序列号。
            pixel_format: 可选 SDK 像素格式名称，省略时保持设备设置。
            exposure_time_us: 可选手动曝光时间，单位微秒。
            gain: 可选手动增益，使用设备节点单位。
            line_selector: 可选输出线路名称。
            line_mode: 可选线路模式名称。
            line_source: 可选线路信号源名称。
            strobe_enabled: 可选频闪输出使能值。

        Returns:
            返回示例：
                camera  # 已打开且尚未取流的 MvsCamera 资源对象
        """
        # 同一序列号已打开时拒绝重复打开。
        if serial in self.cameras and not self.cameras[serial].closed:
            raise MvsError(f"相机已经打开：{serial}")

        # 枚举设备并按序列号定位目标相机。
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
            raise MvsError(f"创建相机连接句柄失败（CreateHandle），错误码：0x{return_code:08X}")
        camera = MvsCamera(self.binding, handle, serial)
        try:
            # 使用 Control 权限打开设备。
            return_code = handle.MV_CC_OpenDevice(3, 0)
            if return_code != self.binding.errors.MV_OK:
                raise MvsError(f"打开相机 {serial} 失败（OpenDevice({serial})），错误码：0x{return_code:08X}")

            # 汇总连续采集模式与关闭触发模式。
            enum_parameters = [
                ("AcquisitionMode", "Continuous"),
                ("TriggerMode", "Off"),
            ]

            # 按配置补充像素格式与手动曝光、增益开关。
            if pixel_format is not None:
                enum_parameters.append(("PixelFormat", pixel_format))
            if exposure_time_us is not None:
                enum_parameters.append(("ExposureAuto", "Off"))
            if gain is not None:
                enum_parameters.append(("GainAuto", "Off"))

            # 依次写入采集和图像枚举参数。
            for parameter_name, parameter_value in enum_parameters:
                return_code = handle.MV_CC_SetEnumValueByString(
                    parameter_name, parameter_value
                )
                if return_code != self.binding.errors.MV_OK:
                    raise MvsError(
                        f"设置相机参数 {parameter_name} 失败，"
                        f"错误码：0x{return_code:08X}"
                    )

            # 汇总需要写入的浮点参数。
            float_parameters = [("ExposureTime", exposure_time_us), ("Gain", gain)]

            # 逐项写入已提供的手动曝光和增益。
            for parameter_name, parameter_value in float_parameters:
                if parameter_value is not None:
                    return_code = handle.MV_CC_SetFloatValue(
                        parameter_name, parameter_value
                    )
                    if return_code != self.binding.errors.MV_OK:
                        raise MvsError(
                            f"设置相机参数 {parameter_name} 失败，"
                            f"错误码：0x{return_code:08X}"
                        )

            # 先选择输出线路，再写入该线路的模式和信号源。
            line_parameters = [
                ("LineSelector", line_selector),
                ("LineMode", line_mode),
                ("LineSource", line_source),
            ]
            for parameter_name, parameter_value in line_parameters:
                if parameter_value is not None:
                    return_code = handle.MV_CC_SetEnumValueByString(
                        parameter_name, parameter_value
                    )
                    if return_code != self.binding.errors.MV_OK:
                        raise MvsError(
                            f"设置相机参数 {parameter_name} 失败，"
                            f"错误码：0x{return_code:08X}"
                        )

            # 按配置写入频闪输出使能值。
            if strobe_enabled is not None:
                return_code = handle.MV_CC_SetBoolValue("StrobeEnable", strobe_enabled)
                if return_code != self.binding.errors.MV_OK:
                    raise MvsError(f"设置相机参数 StrobeEnable 失败，错误码：0x{return_code:08X}")

            # 为 GigE 相机设置 SDK 推荐的网络包大小。
            if device["transport_type"] == self.binding.parameters.MV_GIGE_DEVICE:
                packet_size = handle.MV_CC_GetOptimalPacketSize()
                if packet_size > 0:
                    return_code = handle.MV_CC_SetIntValue("GevSCPSPacketSize", packet_size)
                    if return_code != self.binding.errors.MV_OK:
                        raise MvsError(f"设置相机网络包大小失败（GevSCPSPacketSize），错误码：0x{return_code:08X}")
        except Exception:
            # 任一步失败时关闭已创建的资源，清理也失败则合并报告。
            try:
                camera.close()
            except Exception as cleanup_error:
                raise MvsError(f"相机打开失败，资源清理也失败：{cleanup_error}")
            raise

        # 登记已打开的相机并返回资源对象。
        self.cameras[serial] = camera
        return camera

    def close(self) -> None:
        """关闭所有相机并反初始化 SDK。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # SDK 资源已释放，失败时抛出 MvsError
        """
        # 已关闭时直接返回。
        if self.closed:
            return

        # 收集关闭过程中的错误。
        errors = []

        # 逐台关闭相机。
        for camera in self.cameras.values():
            try:
                camera.close()
            except Exception as error:
                errors.append(str(error))

        # 反初始化相机驱动并登记关闭状态。
        return_code = self.binding.camera_class.MV_CC_Finalize()
        self.closed = True
        if return_code != self.binding.errors.MV_OK:
            errors.append(f"释放相机驱动资源失败（Finalize），错误码：0x{return_code:08X}")

        # 存在关闭错误时汇总抛出。
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
        返回示例：
            sdk  # 已初始化的 MvsSdk 资源对象，使用结束后调用 close()
    """
    # 将官方 Python 绑定目录加入模块搜索路径。
    import_directory = development_directory / "Samples" / "Python" / "MvImport"
    if not (import_directory / "MvCameraControl_class.py").is_file():
        raise MvsError(f"未找到官方 Python 绑定：{import_directory}")
    if str(import_directory) not in sys.path:
        sys.path.insert(0, str(import_directory))

    # 未指定时按位数定位 Windows 公共 MVS Runtime 目录。
    if dll_directory is None:
        architecture = "Win64_x64" if ctypes.sizeof(ctypes.c_void_p) == 8 else "Win32_i86"
        common_directory = Path(os.environ.get("CommonProgramFiles(x86)", r"C:\Program Files (x86)\Common Files"))
        dll_directory = common_directory / "MVS" / "Runtime" / architecture

    # 登记 DLL 搜索目录并补充 PATH。
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
