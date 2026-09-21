"""仅供测试使用的 MVS 相机，不读取文件夹图片。"""

import ctypes
import struct
import threading
import time
from types import SimpleNamespace

from mvs_sdk import MvsCamera


class FakeCameraHandle:
    """按固定间隔在复用的 SDK 内存中产生测试帧。"""

    def __init__(self):
        """初始化测试相机状态。

        Args:
            无外部参数。

        Returns:
            None  # 测试相机已初始化
        """
        self.buffer = ctypes.create_string_buffer(b"\x10\x20\x30\x40")
        self.frame_number = 0
        self.grabbing = False
        self.failure = False
        self.return_no_data = False
        self.encoding_error = 0
        self.started = threading.Event()

    def MV_CC_ClearImageBuffer(self):
        """验证清空缓存时测试相机已启动取流。

        Args:
            无外部参数。

        Returns:
            0  # 操作成功
        """
        assert self.grabbing
        return 0

    def MV_CC_StartGrabbing(self):
        """开启测试相机取流。

        Args:
            无外部参数。

        Returns:
            0  # 操作成功
        """
        self.grabbing = True
        self.started.set()
        return 0

    def MV_CC_GetImageBuffer(self, frame, timeout_ms):
        """生成独立编号的原始灰度帧。

        Args:
            frame: 接收图像地址和元数据的 SDK 结构。
            timeout_ms: 等帧超时毫秒数。

        Returns:
            0  # 成功；无数据返回 7，失败返回 123
        """
        time.sleep(min(0.02, timeout_ms / 1000))
        if self.failure:
            return 123
        if self.return_no_data:
            return 7

        # 更新复用内存及 SDK 帧元数据。
        self.frame_number += 1
        ctypes.memmove(self.buffer, b"\x10\x20\x30\x40", 4)
        frame.pBufAddr = ctypes.addressof(self.buffer)
        frame.stFrameInfo = SimpleNamespace(
            nFrameLenEx=4, nFrameLen=4, nFrameNum=self.frame_number,
            nDevTimeStampHigh=0, nDevTimeStampLow=self.frame_number, nHostTimeStamp=0,
            nExtendWidth=2, nWidth=2, nExtendHeight=2, nHeight=2,
            enPixelType=17301505, nLostPacket=0,
        )
        return 0

    def MV_CC_FreeImageBuffer(self, frame):
        """释放时覆盖 SDK 内存。

        Args:
            frame: 待释放的 SDK 帧。

        Returns:
            0  # 操作成功
        """
        ctypes.memset(frame.pBufAddr, 255, 4)
        return 0

    def MV_CC_StopGrabbing(self):
        """停止测试相机取流。

        Args:
            无外部参数。

        Returns:
            0  # 操作成功
        """
        self.grabbing = False
        return 0

    def MV_CC_SaveImageEx3(self, parameters):
        """模拟 SDK 将 Mono8 转成有效的 24 位 BMP。

        Args:
            parameters: 正式封装传入的 SDK 图片编码参数。

        Returns:
            0  # 编码成功；注入失败时返回 encoding_error
        """
        if self.encoding_error:
            return self.encoding_error
        assert parameters.enImageType == 1
        assert parameters.enPixelType == 17301505
        source = ctypes.string_at(parameters.pData, parameters.nDataLen)

        # 将灰度行转成自下而上的 BGR 行，并补齐行末对齐字节。
        rows = []
        row_padding = b"\0" * ((-parameters.nWidth * 3) % 4)
        for row_number in reversed(range(parameters.nHeight)):
            start = row_number * parameters.nWidth
            row = source[start:start + parameters.nWidth]
            rows.append(b"".join(bytes((value, value, value)) for value in row) + row_padding)
        pixel_data = b"".join(rows)

        # 写入 BMP 文件头和图像内容到调用方提供的缓存。
        file_header = struct.pack("<2sIHHI", b"BM", 54 + len(pixel_data), 0, 0, 54)
        image_header = struct.pack(
            "<IiiHHIIiiII", 40, parameters.nWidth, parameters.nHeight, 1, 24, 0, len(pixel_data), 0, 0, 0, 0,
        )
        image_data = file_header + image_header + pixel_data
        assert len(image_data) <= parameters.nBufferSize
        ctypes.memmove(parameters.pImageBuffer, image_data, len(image_data))
        parameters.nImageLen = len(image_data)
        return 0

    def MV_CC_CloseDevice(self):
        """关闭测试相机。

        Args:
            无外部参数。

        Returns:
            0  # 操作成功
        """
        return 0

    def MV_CC_DestroyHandle(self):
        """销毁测试相机句柄。

        Args:
            无外部参数。

        Returns:
            0  # 操作成功
        """
        return 0


class FakeMvsSdk:
    """提供测试用相机枚举、打开和关闭。"""

    def __init__(self, *arguments):
        """建立测试 SDK。

        Args:
            arguments: 接收应用传入的 SDK 路径参数。

        Returns:
            None  # 测试 SDK 已初始化
        """
        self.cameras = {}
        self.closed = False

    def open_camera(self, serial, **parameters):
        """创建使用真实 MvsCamera 封装的测试相机。

        Args:
            serial: 测试相机序列号。
            parameters: 应用传入的相机配置。

        Returns:
            camera  # 由真实封装管理的测试相机
        """
        binding = SimpleNamespace(
            parameters=SimpleNamespace(
                MV_FRAME_OUT=SimpleNamespace,
                MV_SAVE_IMAGE_PARAM_EX3=SimpleNamespace,
                MV_Image_Bmp=1,
            ),
            errors=SimpleNamespace(MV_OK=0, MV_E_NODATA=7),
        )
        camera = MvsCamera(binding, FakeCameraHandle(), serial)
        self.cameras[serial] = camera
        return camera

    def close(self):
        """关闭测试 SDK 的全部相机。

        Args:
            无外部参数。

        Returns:
            None  # 测试资源已释放
        """
        for camera in self.cameras.values():
            camera.close()
        self.closed = True
