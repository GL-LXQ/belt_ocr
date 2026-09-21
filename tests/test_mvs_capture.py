"""验证相机统一入口、独立帧内存和采集资源释放。"""

import asyncio
import ctypes
import time
import queue
import threading
from types import SimpleNamespace
from dataclasses import replace

import pytest

import mvs_sdk
from camera import SessionCamera
from mvs_sdk import MvsCamera, MvsError, MvsSdk


@pytest.fixture
def start_capture():
    """通过相机适配器启动测试采集，并在测试结束后排空结果交付。

    Args:
        无外部参数。

    Returns:
        begin_capture  # 返回 CaptureTask 的测试启动函数
    """
    event_loop = asyncio.new_event_loop()
    adapters = []
    delivery_tasks = {}
    session_ids = {}
    results = {}

    async def publish_event(event):
        """接收测试采集完成事件。

        Args:
            event: 本轮采集结果事件。

        Returns:
            None  # 事件已接收
        """
        results[event.session_id] = event.payload

    def begin_capture(camera, duration_seconds=1.0, timeout_ms=50):
        """在测试事件循环中调用正式采集入口。

        Args:
            camera: 假 SDK 相机。
            duration_seconds: 采集窗口秒数。
            timeout_ms: 单次取帧超时毫秒数。

        Returns:
            CaptureTask  # 包含停止信号和采集完成信号的任务
        """
        adapter = SessionCamera(
            SimpleNamespace(machine_id="machine-1", camera_serial=camera.serial),
            SimpleNamespace(capture_window_ms=duration_seconds * 1000, camera_timeout_ms=timeout_ms),
            publish_event,
            lambda error: None,
        )
        adapter.device = camera
        adapters.append(adapter)

        async def begin():
            """启动采集并返回本轮任务。

            Args:
                无外部参数。

            Returns:
                CaptureTask  # 当前采集任务
            """
            # 为本轮分配周期身份并登记任务与周期的对应关系。
            session_id = f"session-{len(adapters)}"
            adapter.start_capture(session_id, time.monotonic())
            delivery_tasks[adapter.current_capture] = adapter.delivery_task
            capture_task = adapter.current_capture
            session_ids[capture_task] = session_id
            # 让异步主流程提交后台采集，再将控制权交给同步测试。
            for step in range(3):
                await asyncio.sleep(0)
            return capture_task

        return event_loop.run_until_complete(begin())

    def wait_for_delivery(task, timeout_seconds=2):
        """运行事件循环直到正式结果交付任务结束。

        Args:
            task: 本轮采集任务。
            timeout_seconds: 测试等待上限秒数。

        Returns:
            CaptureResult  # 正式交付完成后的本轮采集结果
        """
        async def receive_result():
            """等待结果交付并取得采集结果。

            Args:
                无外部参数。

            Returns:
                CaptureResult  # 包含帧集合和采集统计的结果
            """
            await asyncio.wait_for(asyncio.shield(delivery_tasks[task]), timeout_seconds)
            return results[session_ids[task]]

        return event_loop.run_until_complete(receive_result())

    begin_capture.wait_for_delivery = wait_for_delivery

    # 向测试提供启动入口，结束时停止采集并完成事件交付。
    yield begin_capture
    try:
        for adapter in adapters:
            event_loop.run_until_complete(adapter.stop())
    finally:
        event_loop.close()


@pytest.fixture
def wait_capture(start_capture):
    """提供等待正式采集结果交付的测试入口。

    Args:
        start_capture: 本测试的采集启动入口。

    Returns:
        wait_for_delivery  # 等待异步交付并返回 CaptureResult 的函数
    """
    return start_capture.wait_for_delivery


class FakeHandle:
    """提供可控的 SDK 图像缓存及返回码。"""

    def __init__(self):
        """创建可控帧源和资源调用记录。

        Args:
            无外部参数。

        Returns:
            None  # 假相机初始化完成
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
        """启动假相机取流。

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






def test_empty_capture_waits_until_deadline(camera, start_capture, wait_capture):
    """验证空队列不提前完成且无图像时仍按期限停止。

    Args:
        wait_capture: 等待正式异步结果交付的测试入口。
        start_capture: 使用正式相机入口的测试启动函数。
        camera: 假 SDK 相机。

    Returns:
        None  # 断言完成
    """
    task = start_capture(camera, duration_seconds=0.2, timeout_ms=10)
    with pytest.raises(asyncio.TimeoutError):
        wait_capture(task, 0.05)
    result = wait_capture(task, 2)
    assert result.statistics["received_frame_count"] == 0
    assert 0.18 <= result.statistics["capture_duration_seconds"] < 1
    assert not camera.grabbing




def test_inflight_copy_finishes_before_stop_and_sealing(camera, monkeypatch, start_capture, wait_capture):
    """验证截止时在途复制被完整释放和入队后才封口。

    Args:
        wait_capture: 等待正式异步结果交付的测试入口。
        start_capture: 使用正式相机入口的测试启动函数。
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
        result = wait_capture(task, 2)
    assert result.frames[0].data == b"last"
    operations = [operation for operation, arguments in camera.handle.calls]
    assert operations.index("free") < operations.index("stop")






@pytest.mark.parametrize("operation", ["start", "get", "free", "stop"])
def test_sdk_errors_are_recorded_and_resources_released(camera, operation, start_capture, wait_capture):
    """验证 SDK 失败可追溯且任务能退出。

    Args:
        wait_capture: 等待正式异步结果交付的测试入口。
        start_capture: 使用正式相机入口的测试启动函数。
        camera: 假 SDK 相机。
        operation: 注入失败的 SDK 操作。

    Returns:
        None  # 断言完成
    """
    camera.handle.failure_operation = operation
    camera.handle.frames.put(b"frame")
    task = start_capture(camera, duration_seconds=0.05)
    with pytest.raises(MvsError):
        wait_capture(task, 2)
    assert camera.grabbing == (operation == "stop")
    assert not camera.handle.buffer_outstanding
    assert task.capture_finished.is_set()
    assert camera.received_frame_count == (0 if operation in {"start", "get"} else 1)
    with pytest.raises(MvsError, match="不可用"):
        start_capture(camera)

    # 清除注入错误并验证相机关闭和句柄销毁。
    camera.handle.failure_operation = ""
    camera.close()
    operations = [name for name, arguments in camera.handle.calls]
    assert operations[-2:] == ["MV_CC_CloseDevice", "MV_CC_DestroyHandle"]


def test_capture_and_stop_errors_preserve_both_failures(camera, monkeypatch, start_capture, wait_capture, caplog):
    """验证取帧和停流同时失败时保留两个异常及各自日志。

    Args:
        camera: 假 SDK 相机。
        monkeypatch: 相机方法替换工具。
        start_capture: 正式采集启动入口。
        wait_capture: 正式结果等待入口。
        caplog: 日志捕获器。

    Returns:
        None  # 两个错误均已记录，异常链完整且采集锁已释放
    """
    from unittest.mock import Mock

    # 注入读取和停流两个独立异常。
    reading_failure = MvsError("测试取帧失败")
    stopping_failure = MvsError("测试停流失败")
    monkeypatch.setattr(camera, "read_frame", Mock(side_effect=reading_failure))
    monkeypatch.setattr(camera, "stop_grabbing", Mock(side_effect=stopping_failure))

    # 采集失败后仍执行停流，并将后续异常连同原始异常传出。
    capture_task = start_capture(camera)
    with pytest.raises(MvsError) as captured_failure:
        wait_capture(capture_task)
    assert captured_failure.value is stopping_failure
    assert stopping_failure.__context__ is reading_failure
    assert capture_task.capture_finished.is_set()
    assert not camera.capture_lock.locked()

    # 每个独立异常只在对应操作失败时记录一次。
    failure_logs = [record for record in caplog.records if record.exc_info]
    assert [record.exc_info[1] for record in failure_logs] == [reading_failure, stopping_failure]


def test_task_creation_failure_releases_camera(camera, monkeypatch, start_capture, wait_capture):
    """验证异步任务创建失败后无锁和未关闭协程遗留。

    Args:
        camera: 假 SDK 相机。
        monkeypatch: 异步任务创建入口替换工具。
        start_capture: 正式采集启动入口。
        wait_capture: 正式结果等待入口。

    Returns:
        None  # 未启动协程已关闭，相机锁已释放且可再次采集
    """
    failed_workflows = []

    def fail_task_creation(workflow):
        """记录待启动协程并模拟任务创建异常。

        Args:
            workflow: 尚未启动的采集协程。

        Returns:
            无返回值  # 抛出 RuntimeError
        """
        failed_workflows.append(workflow)
        raise RuntimeError("测试任务创建失败")

    # 创建失败必须同时清理协程和相机占用。
    with monkeypatch.context() as patch:
        patch.setattr(asyncio, "create_task", fail_task_creation)
        with pytest.raises(RuntimeError, match="测试任务创建失败"):
            start_capture(camera)
    assert failed_workflows[0].cr_frame is None
    assert not camera.capture_lock.locked()

    # 恢复任务入口后，同一相机可以完成新一轮采集。
    result = wait_capture(start_capture(camera, duration_seconds=0.01))
    assert result.frames == ()
    assert not camera.grabbing


def test_copy_exception_still_frees_sdk_buffer(camera, monkeypatch, start_capture, wait_capture):
    """验证复制抛出异常时 SDK Buffer 仍被释放。

    Args:
        wait_capture: 等待正式异步结果交付的测试入口。
        start_capture: 使用正式相机入口的测试启动函数。
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
    with pytest.raises(MemoryError, match="无法复制图像"):
        wait_capture(start_capture(camera))
    assert camera.received_frame_count == 1
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
            """返回可记录操作的相机句柄。

            Args:
                无外部参数。

            Returns:
                handle  # 假相机句柄
            """
            return handle

        MV_CC_Initialize = staticmethod(lambda: handle.call("initialize"))
        MV_CC_Finalize = staticmethod(lambda: handle.call("finalize"))

        @staticmethod
        def MV_CC_EnumDevices(transport_types, device_list):
            """填入一台可打开的相机。

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
    """验证关闭相机失败时仍销毁句柄并报告错误。

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




def test_stop_exception_blocks_next_capture(camera, monkeypatch, start_capture, wait_capture):
    """验证停流接口抛出异常后禁止新一轮采集。

    Args:
        wait_capture: 等待正式异步结果交付的测试入口。
        start_capture: 使用正式相机入口的测试启动函数。
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
    with pytest.raises(RuntimeError, match="停流接口异常"):
        wait_capture(start_capture(camera, duration_seconds=0.05))
    assert camera.grabbing
    with pytest.raises(MvsError, match="不可用"):
        start_capture(camera)


def test_old_sdk_buffer_is_cleared_before_new_capture(camera, monkeypatch, start_capture, wait_capture):
    """验证启动取流后、读取本轮图像前清除 SDK 残留缓存。

    Args:
        wait_capture: 等待正式异步结果交付的测试入口。
        start_capture: 使用正式相机入口的测试启动函数。
        camera: 假 SDK 相机。
        monkeypatch: pytest 替换工具。

    Returns:
        None  # 断言完成
    """
    buffer_cleared = threading.Event()

    def clear_buffer():
        """清空假 SDK 中上轮未取出的图像。

        Args:
            无外部参数。

        Returns:
            0  # 缓存已清空
        """
        assert camera.grabbing
        assert camera.handle.started.is_set()
        while True:
            try:
                camera.handle.frames.get_nowait()
            except queue.Empty:
                buffer_cleared.set()
                return 0

    # 预置 SDK 残留帧，启动新轮后再送入本轮帧。
    monkeypatch.setattr(camera.handle, "MV_CC_ClearImageBuffer", clear_buffer)
    camera.handle.frames.put(b"stale")
    task = start_capture(camera, duration_seconds=0.1)
    assert buffer_cleared.wait(1)
    camera.handle.frames.put(b"current")
    result = wait_capture(task, 2)
    assert [frame.data for frame in result.frames] == [b"current"]


def test_clear_buffer_failure_stops_capture(camera, start_capture, wait_capture):
    """验证启动后清缓存失败仍会停止取流并释放相机占用。

    Args:
        camera: 假 SDK 相机。
        start_capture: 使用正式相机入口的测试启动函数。
        wait_capture: 等待正式异步结果交付的测试入口。

    Returns:
        None  # 断言完成
    """
    # 注入清缓存错误并运行正式采集流程。
    camera.handle.failure_operation = "MV_CC_ClearImageBuffer"
    with pytest.raises(MvsError, match="ClearImageBuffer"):
        wait_capture(start_capture(camera))

    # 核对失败后的停流状态和相机占用释放。
    assert camera.faulted
    assert not camera.grabbing
    assert camera.handle.stopped.is_set()
    assert not camera.capture_lock.locked()


def test_two_cameras_capture_independently(camera, start_capture, wait_capture):
    """验证一台相机正在采集时另一台可以独立完成。

    Args:
        wait_capture: 等待正式异步结果交付的测试入口。
        start_capture: 使用正式相机入口的测试启动函数。
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
        second_result = wait_capture(second_task, 2)
        assert second_result.frames[0].camera_serial == "CAM002"
        assert not first_task.capture_finished.is_set()
    finally:
        first_task.stop_requested.set()
        wait_capture(first_task, 2)


def test_collects_all_owned_frames_without_encoding(camera, start_capture, wait_capture):
    """验证全部帧独立于 SDK 缓存且不限制前五帧。

    Args:
        wait_capture: 等待正式异步结果交付的测试入口。
        start_capture: 使用正式相机入口的测试启动函数。
        camera: 可控 SDK 相机。

    Returns:
        None  # 帧数量、原始数据和完成通知已验证
    """
    for number in range(12):
        camera.handle.frames.put(f"frame-{number}".encode())
    result = wait_capture(start_capture(camera, duration_seconds=0.1, timeout_ms=5))
    assert len(result.frames) == 12
    assert [frame.data for frame in result.frames] == [f"frame-{number}".encode() for number in range(12)]
    assert result.statistics["received_frame_count"] == 12
    assert not camera.capture_lock.locked()


def test_close_keeps_inflight_tail_frame(camera, monkeypatch, start_capture, wait_capture):
    """验证关闭时的在途尾帧保留在本轮集合。

    Args:
        wait_capture: 等待正式异步结果交付的测试入口。
        start_capture: 使用正式相机入口的测试启动函数。
        camera: 可控相机。
        monkeypatch: 接口替换工具。

    Returns:
        None  # 尾帧保留和资源释放已验证
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
        return replace(original_read(threading.Event(), timeout_ms), received_monotonic=time.monotonic())

    monkeypatch.setattr(camera, "read_frame", read_delayed)
    camera.handle.frames.put(b"late")
    task = start_capture(camera, duration_seconds=2)
    try:
        assert reading.wait(1)
        task.stop_requested.set()
    finally:
        release.set()
    result = wait_capture(task, 2)
    assert [frame.data for frame in result.frames] == [b"late"]
    assert not camera.grabbing



def test_thread_start_failure_releases_camera(camera, start_capture, monkeypatch, wait_capture):
    """验证线程启动失败后释放相机锁，允许重新启动采集。

    Args:
        wait_capture: 等待正式异步结果交付的测试入口。
        camera: 假 SDK 相机。
        start_capture: 使用正式相机入口的测试启动函数。
        monkeypatch: pytest 替换工具。

    Returns:
        None  # 启动异常、锁释放和再次采集已验证
    """
    async def fail_start(operation, *arguments, **keyword_arguments):
        """模拟线程启动失败。

        Args:
            operation: 待执行的采集函数。
            arguments: 位置参数。
            keyword_arguments: 关键字参数。

        Returns:
            无返回值，抛出 RuntimeError。
        """
        raise RuntimeError("线程启动失败")

    # 注入启动错误，确认相机占用已释放。
    with monkeypatch.context() as patch:
        patch.setattr(asyncio, "to_thread", fail_start)
        task = start_capture(camera)
        with pytest.raises(RuntimeError, match="线程启动失败"):
            wait_capture(task)
    assert not camera.capture_lock.locked()

    # 恢复线程入口，确认同一相机可以正常采集。
    result = wait_capture(start_capture(camera, duration_seconds=0.05))
    assert not camera.grabbing


@pytest.mark.parametrize("cancel_delivery", [False, True])
def test_capture_shutdown_waits_for_worker_only(camera, monkeypatch, cancel_delivery):
    """验证取消等待真实采集结束，CLOSE 不等待被阻塞的结果发布。

    Args:
        camera: 可控 SDK 相机。
        monkeypatch: 接口替换工具。
        cancel_delivery: 是否取消异步采集主流程。

    Returns:
        None  # 线程结束、相机占用释放和交付等待已验证
    """
    entered = threading.Event()
    release_read = threading.Event()

    def read_blocked(stop_requested, timeout_ms):
        """保持一次读取直到测试允许结束。

        Args:
            stop_requested: 停止信号。
            timeout_ms: 读取超时。

        Returns:
            None  # 本次未取得帧
        """
        entered.set()
        assert release_read.wait(3)
        return None

    async def scenario():
        """执行取消或交付阻塞场景并释放资源。

        Args:
            无外部参数。

        Returns:
            None  # 场景执行完成
        """
        release_delivery = asyncio.Event()
        published = []

        async def publish(event):
            """等待允许后记录交付结果。

            Args:
                event: 采集结果事件。

            Returns:
                None  # 事件已记录
            """
            await release_delivery.wait()
            published.append(event)

        adapter = SessionCamera(
            SimpleNamespace(machine_id="machine", camera_serial="camera"),
            SimpleNamespace(capture_window_ms=2000, camera_timeout_ms=50),
            publish,
            lambda error: None,
        )
        adapter.device = camera
        adapter.start_capture("session", time.monotonic())
        delivery_task = adapter.delivery_task
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            if cancel_delivery:
                delivery_task.cancel()
                await asyncio.sleep(0)
                assert not delivery_task.done()
                assert camera.capture_lock.locked()

            # 发出停止信号并允许在途读取结束，只等待硬件停止。
            release_read.set()
            await asyncio.wait_for(adapter.inform_capture_workflow_stop(), 1)
            assert not camera.capture_lock.locked()
            if cancel_delivery:
                with pytest.raises(asyncio.CancelledError):
                    await delivery_task
                assert not published
            else:
                assert not delivery_task.done()
                release_delivery.set()
                await delivery_task
                assert len(published) == 1
        finally:
            release_read.set()
            release_delivery.set()
            await asyncio.gather(delivery_task, return_exceptions=True)

    monkeypatch.setattr(camera, "read_frame", read_blocked)
    asyncio.run(scenario())
