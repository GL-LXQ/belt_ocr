"""验证 MVS 独立内存、流式消费、队列溢出和跨轮收尾。"""

import ctypes
import queue
import threading
from types import SimpleNamespace
from concurrent.futures import TimeoutError
from dataclasses import replace

import pytest

import mvs_sdk
from mvs_capture import start_capture
from mvs_sdk import MvsCamera, MvsError, MvsSdk


class FakeHandle:
    """提供可控的 SDK 图像缓存及返回码。"""

    def __init__(self):
        """创建可控帧源和资源调用记录。

        Args:
            无外部参数。

        Returns:
            None  # 假设备初始化完成
        """
        self.frames = queue.Queue()
        self.released_frames = queue.Queue()
        self.buffer = ctypes.create_string_buffer(256)
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.calls = []
        self.frame_number = 0
        self.failure_operation = ""
        self.buffer_outstanding = False

    def call(self, operation, *arguments):
        """记录 SDK 操作并返回指定的失败码。

        Args:
            operation: 操作名称。
            arguments: 操作参数。

        Returns:
            0  # 正常返回；指定操作失败时返回 123
        """
        self.calls.append((operation, arguments))
        return 123 if operation == self.failure_operation else 0

    def __getattr__(self, name):
        """为简单 SDK 配置及释放接口提供调用记录。

        Args:
            name: SDK 接口名称。

        Returns:
            operation  # 返回整数状态码的可调用对象
        """
        def operation(*arguments):
            """转发 SDK 测试调用。

            Args:
                arguments: SDK 操作参数。

            Returns:
                0  # 正常返回；指定失败时返回 123
            """
            return self.call(name, *arguments)

        operation.__name__ = name
        return operation

    def MV_CC_StartGrabbing(self):
        """启动假设备取流。

        Args:
            无外部参数。

        Returns:
            0  # 成功，注入失败时返回 123
        """
        self.stopped.clear()
        self.started.set()
        return self.call("start")

    def MV_CC_StopGrabbing(self):
        """验证 Buffer 已归还并停止取流。

        Args:
            无外部参数。

        Returns:
            0  # 成功，注入失败时返回 123
        """
        assert not self.buffer_outstanding
        self.started.clear()
        self.stopped.set()
        return self.call("stop")

    def MV_CC_GetImageBuffer(self, frame_buffer, timeout_ms):
        """将可控帧写入同一块 SDK 内存。

        Args:
            frame_buffer: 接收图像指针和元数据的对象。
            timeout_ms: 无数据时的最大等待毫秒数。

        Returns:
            0  # 成功；无数据返回 7；失败返回 123
        """
        self.calls.append(("get", ()))
        if self.failure_operation == "get":
            return 123
        try:
            data = self.frames.get(timeout=timeout_ms / 1000)
        except queue.Empty:
            return 7

        # 将新帧写入可复用缓存，并填写 SDK 帧信息。
        self.buffer_outstanding = True
        self.frame_number += 1
        ctypes.memmove(self.buffer, data, len(data))
        frame_buffer.pBufAddr = ctypes.addressof(self.buffer)
        frame_buffer.stFrameInfo = SimpleNamespace(
            nFrameLenEx=len(data), nFrameLen=0, nFrameNum=self.frame_number,
            nDevTimeStampHigh=1, nDevTimeStampLow=2, nHostTimeStamp=3,
            nExtendWidth=len(data), nWidth=0, nExtendHeight=1, nHeight=0,
            enPixelType=17301505, nLostPacket=0,
        )
        return 0

    def MV_CC_FreeImageBuffer(self, frame_buffer):
        """归还并覆盖 SDK 缓存，模拟驱动内存复用。

        Args:
            frame_buffer: 当前 SDK 帧结构。

        Returns:
            0  # 成功，注入失败时返回 123
        """
        self.buffer_outstanding = False
        ctypes.memset(frame_buffer.pBufAddr, 88, 256)
        self.released_frames.put(self.frame_number)
        return self.call("free")


@pytest.fixture
def camera():
    """创建无需安装 SDK 的相机封装。

    Args:
        无外部参数。

    Returns:
        camera  # 使用假 SDK 的 MvsCamera 对象
    """
    binding = SimpleNamespace(
        parameters=SimpleNamespace(MV_FRAME_OUT=SimpleNamespace),
        errors=SimpleNamespace(MV_OK=0, MV_E_NODATA=7),
    )
    return MvsCamera(binding, FakeHandle(), "CAM001")






def test_empty_capture_waits_until_deadline(camera):
    """验证空队列不提前完成且无图像时仍按期限停止。

    Args:
        camera: 假 SDK 相机。

    Returns:
        None  # 断言完成
    """
    task = start_capture(camera, duration_seconds=0.2, timeout_ms=10)
    with pytest.raises(TimeoutError):
        task.wait(0.05)
    result = task.wait(2)
    assert result.received_frame_count == 0
    assert 0.18 <= result.capture_duration_seconds < 1
    assert result.camera_stopped




def test_inflight_copy_finishes_before_stop_and_sealing(camera, monkeypatch):
    """验证截止时在途复制被完整释放和入队后才封口。

    Args:
        camera: 假 SDK 相机。
        monkeypatch: pytest 替换工具。

    Returns:
        None  # 断言完成
    """
    copying = threading.Event()
    release_copy = threading.Event()
    original_copy = ctypes.string_at

    def copy_buffer(address, size):
        """在复制期间保持 SDK Buffer 被占用。

        Args:
            address: SDK 内存地址。
            size: 字节长度。

        Returns:
            b"last"  # 独立图像字节
        """
        copying.set()
        assert release_copy.wait(3)
        return original_copy(address, size)

    monkeypatch.setattr(mvs_sdk.ctypes, "string_at", copy_buffer)
    task = start_capture(camera, duration_seconds=2)
    try:
        camera.handle.frames.put(b"last")
        assert copying.wait(1)
        task.stop_requested.set()
        assert not camera.handle.stopped.wait(0.05)
        assert not camera.handle.stopped.is_set()
    finally:
        release_copy.set()
        task.stop_requested.set()
        result = task.wait(2)
    assert result.frames[0].data == b"last"
    operations = [operation for operation, arguments in camera.handle.calls]
    assert operations.index("free") < operations.index("stop")






@pytest.mark.parametrize("operation", ["start", "get", "free", "stop"])
def test_sdk_errors_are_recorded_and_resources_released(camera, operation):
    """验证 SDK 失败可追溯且任务能退出。

    Args:
        camera: 假 SDK 相机。
        operation: 注入失败的 SDK 操作。

    Returns:
        None  # 断言完成
    """
    camera.handle.failure_operation = operation
    camera.handle.frames.put(b"frame")
    task = start_capture(camera, duration_seconds=0.05)
    result = task.wait(2)
    assert bool(result.capture_errors)
    assert result.capture_errors
    assert result.camera_stopped == (operation != "stop")
    assert not camera.handle.buffer_outstanding
    assert task.completion_future.done()
    assert result.received_frame_count == (0 if operation in {"start", "get"} else 1)
    with pytest.raises(MvsError, match="不可用"):
        start_capture(camera)

    # 清除注入错误并验证设备关闭和句柄销毁。
    camera.handle.failure_operation = ""
    camera.close()
    operations = [name for name, arguments in camera.handle.calls]
    assert operations[-2:] == ["MV_CC_CloseDevice", "MV_CC_DestroyHandle"]


def test_copy_exception_still_frees_sdk_buffer(camera, monkeypatch):
    """验证复制抛出异常时 SDK Buffer 仍被释放。

    Args:
        camera: 假 SDK 相机。
        monkeypatch: pytest 替换工具。

    Returns:
        None  # 断言完成
    """
    def fail_copy(address, size):
        """模拟复制失败。

        Args:
            address: SDK 图像地址。
            size: 图像字节数。

        Returns:
            无返回值，抛出 MemoryError。
        """
        raise MemoryError("无法复制图像")

    monkeypatch.setattr(mvs_sdk.ctypes, "string_at", fail_copy)
    camera.handle.frames.put(b"frame")
    result = start_capture(camera).wait(2)
    assert result.received_frame_count == 1
    assert len(result.frames) == 0
    assert bool(result.capture_errors)
    assert camera.handle.released_frames.get_nowait() == 1
    assert not camera.handle.buffer_outstanding


def test_sdk_enumeration_configuration_and_cleanup():
    """验证真实封装的枚举、连续模式参数和生命周期调用顺序。

    Args:
        无外部参数。

    Returns:
        None  # 断言完成
    """
    class SerialInformation(ctypes.Structure):
        _fields_ = [("chSerialNumber", ctypes.c_char * 32)]

    class SpecialInformation(ctypes.Union):
        _fields_ = [("stGigEInfo", SerialInformation), ("stUsb3VInfo", SerialInformation)]

    class DeviceInformation(ctypes.Structure):
        _fields_ = [("nTLayerType", ctypes.c_uint), ("SpecialInfo", SpecialInformation)]

    information = DeviceInformation()
    information.nTLayerType = 1
    information.SpecialInfo.stGigEInfo.chSerialNumber = b"CAM001"
    handle = FakeHandle()

    class CameraDriver:
        """模拟官方相机类和全局 SDK 方法。"""

        def __new__(cls):
            """返回可记录操作的设备句柄。

            Args:
                无外部参数。

            Returns:
                handle  # 假设备句柄
            """
            return handle

        MV_CC_Initialize = staticmethod(lambda: handle.call("initialize"))
        MV_CC_Finalize = staticmethod(lambda: handle.call("finalize"))

        @staticmethod
        def MV_CC_EnumDevices(transport_types, device_list):
            """填入一台可打开的设备。

            Args:
                transport_types: 传输类型位掩码。
                device_list: 接收枚举结果的对象。

            Returns:
                0  # 枚举成功
            """
            device_list.nDeviceNum = 1
            device_list.pDeviceInfo = [ctypes.pointer(information)]
            return handle.call("enumerate", transport_types)

    binding = SimpleNamespace(
        camera_class=CameraDriver,
        parameters=SimpleNamespace(
            MV_CC_DEVICE_INFO_LIST=SimpleNamespace, MV_CC_DEVICE_INFO=DeviceInformation,
            MV_GIGE_DEVICE=1, MV_USB_DEVICE=4,
        ),
        errors=SimpleNamespace(MV_OK=0),
    )
    sdk = MvsSdk(binding)
    camera = sdk.open_camera("CAM001", pixel_format="Mono8", exposure_time_us=1000, gain=2)
    assert camera.serial == "CAM001"
    assert not camera.grabbing
    assert ("MV_CC_SetEnumValueByString", ("AcquisitionMode", "Continuous")) in handle.calls
    assert ("MV_CC_SetEnumValueByString", ("TriggerMode", "Off")) in handle.calls
    assert ("MV_CC_SetFloatValue", ("ExposureTime", 1000)) in handle.calls
    assert ("MV_CC_SetFloatValue", ("Gain", 2)) in handle.calls
    sdk.close()
    operations = [name for name, arguments in handle.calls]
    assert operations[-3:] == ["MV_CC_CloseDevice", "MV_CC_DestroyHandle", "finalize"]


def test_close_failure_still_destroys_handle(camera):
    """验证关闭设备失败时仍销毁句柄并报告错误。

    Args:
        camera: 假 SDK 相机。

    Returns:
        None  # 断言完成
    """
    camera.handle.failure_operation = "MV_CC_CloseDevice"
    with pytest.raises(MvsError, match="CloseDevice"):
        camera.close()
    operations = [name for name, arguments in camera.handle.calls]
    assert operations[-1] == "MV_CC_DestroyHandle"




def test_stop_exception_blocks_next_capture(camera, monkeypatch):
    """验证停流接口抛出异常后禁止新一轮采集。

    Args:
        camera: 假 SDK 相机。
        monkeypatch: pytest 替换工具。

    Returns:
        None  # 断言完成
    """
    def fail_stop():
        """模拟停流接口抛出异常。

        Args:
            无外部参数。

        Returns:
            无返回值，抛出 RuntimeError。
        """
        raise RuntimeError("停流接口异常")

    monkeypatch.setattr(camera.handle, "MV_CC_StopGrabbing", fail_stop)
    result = start_capture(camera, duration_seconds=0.05).wait(2)
    assert not result.camera_stopped
    assert bool(result.capture_errors)
    with pytest.raises(MvsError, match="不可用"):
        start_capture(camera)


def test_old_sdk_buffer_is_cleared_before_new_capture(camera, monkeypatch):
    """验证开始新一轮前清除 SDK 残留缓存。

    Args:
        camera: 假 SDK 相机。
        monkeypatch: pytest 替换工具。

    Returns:
        None  # 断言完成
    """
    def clear_buffer():
        """清空假 SDK 中上轮未取出的图像。

        Args:
            无外部参数。

        Returns:
            0  # 缓存已清空
        """
        while True:
            try:
                camera.handle.frames.get_nowait()
            except queue.Empty:
                return 0

    # 预置 SDK 残留帧，启动新轮后再送入本轮帧。
    monkeypatch.setattr(camera.handle, "MV_CC_ClearImageBuffer", clear_buffer)
    camera.handle.frames.put(b"stale")
    task = start_capture(camera, duration_seconds=0.1)
    assert camera.handle.started.wait(1)
    camera.handle.frames.put(b"current")
    result = task.wait(2)
    assert [frame.data for frame in result.frames] == [b"current"]


def test_two_cameras_capture_independently(camera):
    """验证一台相机正在采集时另一台可以独立完成。

    Args:
        camera: 第一台假 SDK 相机。

    Returns:
        None  # 断言完成
    """
    other_camera = MvsCamera(camera.binding, FakeHandle(), "CAM002")
    first_task = start_capture(camera, duration_seconds=2)
    try:
        # 第二台相机使用自己的取流线程和队列。
        second_task = start_capture(
            other_camera,
            duration_seconds=0.1,
        )
        other_camera.handle.frames.put(b"second")
        second_result = second_task.wait(2)
        assert second_result.frames[0].camera_serial == "CAM002"
        assert not first_task.completion_future.done()
    finally:
        first_task.stop_requested.set()
        first_task.wait(2)


def test_collects_all_owned_frames_without_encoding(camera):
    """验证全部帧独立于 SDK 缓存且不限制前五帧。

    Args:
        camera: 可控 SDK 相机。

    Returns:
        None  # 帧数量、原始数据和完成通知已验证
    """
    for number in range(12):
        camera.handle.frames.put(f"frame-{number}".encode())
    result = start_capture(camera, duration_seconds=0.1, timeout_ms=5).wait(2)
    assert len(result.frames) == 12
    assert [frame.data for frame in result.frames] == [f"frame-{number}".encode() for number in range(12)]
    assert result.received_frame_count == 12
    assert not camera.capture_lock.locked()


def test_close_excludes_inflight_frame_after_boundary(camera, monkeypatch):
    """验证关闭后才取得的在途帧不进入本轮集合。

    Args:
        camera: 可控相机。
        monkeypatch: 接口替换工具。

    Returns:
        None  # 截止边界、丢弃统计和资源释放已验证
    """
    import time

    reading = threading.Event()
    release = threading.Event()
    original_read = camera.read_frame

    def read_delayed(stop_requested, timeout_ms):
        """在读取前等待关闭信号到达。

        Args:
            stop_requested: 采集停止标记。
            timeout_ms: 读取超时。

        Returns:
            CameraFrame  # 截止时间之后获得的帧
        """
        reading.set()
        assert release.wait(2)
        return replace(original_read(threading.Event(), timeout_ms), received_monotonic=task.capture_stop_time + 0.01)

    monkeypatch.setattr(camera, "read_frame", read_delayed)
    camera.handle.frames.put(b"late")
    task = start_capture(camera, duration_seconds=2)
    try:
        assert reading.wait(1)
        task.capture_stop_time = time.monotonic()
        task.stop_requested.set()
    finally:
        release.set()
    result = task.wait(2)
    assert not result.frames
    assert result.skipped_frame_count == 1
    assert result.camera_stopped
