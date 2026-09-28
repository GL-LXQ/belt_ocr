"""使用本地测试图片运行一轮开发用测量。"""

import argparse
import asyncio
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import cv2

# 将项目后端目录加入脚本的模块搜索路径。
PROJECT_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIRECTORY))
sys.path.insert(0, str(PROJECT_DIRECTORY / "src"))

from camera.camera import Camera
from camera.hikrobot_sdk import (
    PIXEL_TYPE_MONO8,
    CameraFrame,
    convert_mono8_frame_to_array,
)
from config_util import MachineConfig, load_config
from database import Database
from enums import EventType, ProgressStage, ProgressStatus
from frequency_adapter import FrequencyAdapter
from machine import Machine
from models import FrequencyMeasurement, RuntimeEvent
from repo.machine_repo import MachineRepo
from repo.measurement_record_repo import MeasurementRecordRepo
from service.measurement_record_service import MeasurementRecordService
from text_recognition import TextRecognizer


def load_camera_frames(
    image_directory: Path, camera_serial: str
) -> tuple[CameraFrame, ...]:
    """将测试图片解码为连续编号的 Mono8 相机帧。

    Args:
        image_directory: 当前场景的 BMP 或 JPG 图片目录。
        camera_serial: 所选机器绑定的相机序列号。

    Returns:
        返回示例：
            (CameraFrame(...),)  # 按文件名排列的 Mono8 原始像素帧
    """
    # 按文件名读取当前场景的全部测试图片。
    image_paths = sorted((
        *image_directory.glob("*.jpg"),
        *image_directory.glob("*.bmp"),
    ))
    if not image_paths:
        raise ValueError(f"没有可用测试图片：{image_directory}")

    # 将每张图片转换成相机交付的灰度像素字节。
    frames = []
    for frame_number, image_path in enumerate(image_paths, start=1):
        image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ValueError(f"测试图片无法读取：{image_path}")
        height, width = image.shape
        frames.append(CameraFrame(
            camera_serial=camera_serial,
            frame_number=frame_number,
            device_timestamp=0,
            host_timestamp=0,
            received_monotonic=0.0,
            width=width,
            height=height,
            pixel_type=PIXEL_TYPE_MONO8,
            lost_packet_count=0,
            data=image.tobytes(),
        ))
    return tuple(frames)


class SimulatedCameraSdk:
    """只提供现有 Camera 所需的测试帧和 JPG 编码接口。"""

    def __init__(self, camera_serial: str, frames: tuple[CameraFrame, ...]) -> None:
        """登记待交付的原始帧和相机占用锁。

        Args:
            camera_serial: 本轮模拟相机的序列号。
            frames: 已解码的 Mono8 相机帧。

        Returns:
            返回示例：
                None  # 测试帧和相机状态已就绪
        """
        self.serial = camera_serial
        self.frames = frames
        self.next_frame_index = 0
        self.closed = False
        self.faulted = False
        self.capture_lock = threading.Lock()
        self.received_frame_count = 0

    def start_grabbing(self) -> None:
        """接收测试相机的开始取流调用。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 开始取流调用已完成
        """
        pass

    def read_frame(
        self, stop_requested: threading.Event, timeout_ms: int
    ) -> CameraFrame | None:
        """按顺序交付一张测试帧，交付完毕后等待采集窗口结束。

        Args:
            stop_requested: 当前采集任务的停止通知。
            timeout_ms: 本次无帧等待的超时毫秒数。

        Returns:
            返回示例：
                CameraFrame(...)  # 当前交付的 Mono8 帧
                None  # 当前没有更多测试帧
        """
        # 交付下一张图片并记录实际接收时间。
        if self.next_frame_index < len(self.frames):
            frame = replace(
                self.frames[self.next_frame_index],
                host_timestamp=time.time_ns(),
                received_monotonic=time.monotonic(),
            )
            self.next_frame_index += 1
            self.received_frame_count += 1
            return frame

        # 图片交付完毕后等待下一次采集检查。
        stop_requested.wait(timeout_ms / 1000)
        return None

    def stop_grabbing(self) -> None:
        """接收测试相机的停止取流调用。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 停止取流调用已完成
        """
        pass

    def encode_image(self, frame: CameraFrame) -> bytes:
        """将本轮 Mono8 原始帧编码为 JPG 证据图片。

        Args:
            frame: 待保存的 Mono8 相机帧。

        Returns:
            返回示例：
                b"\xff\xd8..."  # JPG 图片字节
        """
        image = convert_mono8_frame_to_array(frame)
        encoded, image_data = cv2.imencode(".jpg", image)
        if not encoded:
            raise RuntimeError("模拟证据图片编码失败")
        return image_data.tobytes()


async def run_measurement_simulation(scenario: str) -> dict:
    """按正式事件顺序识别测试图片并保存一轮测量结果。

    Args:
        scenario: normal 或 review 测试场景。

    Returns:
        返回示例：
            {
                "session_id": "abc123",  # 本轮唯一周期编号
                "machine_id": "1",  # 所选机器编号
                "ordered_lines": ("ABC",),  # 实际 OCR 终选文字
                "needs_review": False,  # 历史记录复核状态
                "final_frequency_hz": 50.0,  # 最终模拟频率
                "review_reason": None,  # 实际 OCR 复核原因
                "evidence_directory": "runtime/evidence/...",  # 本轮证据目录
                "evidence_count": 1,  # 已保存 JPG 数量
            }
    """
    # 读取正式存储路径，并仅在模拟进程内调整期限。
    config = load_config(PROJECT_DIRECTORY / "config")
    config = replace(
        config,
        capture_window_ms=500,
        ocr_result_timeout_ms=600000,
        max_cycle_open_ms=600000,
    )
    scenario_directory_name = (
        "imgs" if scenario == "normal" else "test_images_without_results"
    )
    image_directory = PROJECT_DIRECTORY / "statistics" / scenario_directory_name

    # 初始化现有业务库和运行库，并选择一台已启用机器。
    database = Database(config)
    machine = None
    worker_task = None
    try:
        database.initialize()
        machine_rows = MachineRepo(config.database_path).list_enabled()
        if not machine_rows:
            raise ValueError("没有启用的机器，请先在机器管理页添加并启用机器。")
        machine_row = machine_rows[0]
        machine_config = MachineConfig(
            machine_id=str(machine_row["id"]),
            camera_serial=machine_row["camera_serial"],
            frequency_meter_serial=machine_row["frequency_meter_serial"],
        )
        frames = load_camera_frames(image_directory, machine_config.camera_serial)

        # 待复核场景只交付首张已验证会产生 OCR 复核原因的 BMP。
        if scenario == "review":
            frames = frames[:1]

        # 建立共用队列的相机、频率适配器和真实测量处理器。
        async def publish_event(event: RuntimeEvent) -> None:
            """将模拟输入和后台结果送入同一个机器队列。

            Args:
                event: 待处理的真实业务事件。

            Returns:
                返回示例：
                    None  # 事件已进入当前机器队列
            """
            await machine.queue.put(event)

        async def publish_and_wait(event: RuntimeEvent) -> None:
            """送入一个模拟业务事件并等待机器确认处理完成。

            Args:
                event: 要交付的启动、频率或关闭事件。

            Returns:
                返回示例：
                    None  # 机器已完成该事件的处理
            """
            acknowledgement = asyncio.get_running_loop().create_future()
            await publish_event(replace(event, acknowledgement=acknowledgement))
            await acknowledgement

        ocr_completed = asyncio.Event()
        ocr_failed = asyncio.Event()
        fatal_errors = []

        def notify_progress(
            machine_id: str,
            session_id: str,
            stage: ProgressStage,
            status: ProgressStatus,
        ) -> None:
            """记录字符识别阶段的真实完成状态。

            Args:
                machine_id: 进度所属机器编号。
                session_id: 进度所属周期编号。
                stage: 当前测量阶段。
                status: 当前阶段状态。

            Returns:
                返回示例：
                    None  # OCR 完成或失败通知已登记
            """
            if stage == ProgressStage.CHARACTER_RECOGNITION:
                if status == ProgressStatus.SUCCESS:
                    ocr_completed.set()
                elif status == ProgressStatus.FAILED:
                    ocr_failed.set()
            elif (
                stage == ProgressStage.IMAGE_CAPTURE
                and status == ProgressStatus.FAILED
            ):
                ocr_failed.set()

        def report_fatal_error(error: Exception) -> None:
            """记录采集或识别后台任务的异常。

            Args:
                error: 后台任务产生的异常。

            Returns:
                返回示例：
                    None  # 异常已登记供主流程检查
            """
            fatal_errors.append(error)
            ocr_failed.set()

        camera = Camera(
            machine_config.machine_id,
            config.capture_window_ms,
            config.camera_timeout_ms,
            publish_event,
            report_fatal_error,
        )
        camera.sdk_camera = SimulatedCameraSdk(machine_config.camera_serial, frames)
        frequency_adapter = FrequencyAdapter(machine_config, config, publish_event)
        machine = Machine(
            machine_config,
            config,
            camera,
            frequency_adapter,
            TextRecognizer(),
            database,
            publish_event,
            notify_progress,
            report_fatal_error,
            asyncio.Event(),
        )
        machine.initialized = True
        worker_task = asyncio.create_task(machine.listen_events())

        # 正式受理 START，并等待相机交付和真实 OCR 完成。
        await publish_and_wait(RuntimeEvent(
            EventType.MACHINE_STARTED, machine_config.machine_id
        ))
        session = machine.current_session
        if session is None:
            raise RuntimeError("模拟 START 未创建测量周期")
        session_id = session.session_id
        ocr_ready_task = asyncio.create_task(ocr_completed.wait())
        ocr_failed_task = asyncio.create_task(ocr_failed.wait())
        try:
            completed_tasks, _ = await asyncio.wait(
                (ocr_ready_task, ocr_failed_task, worker_task),
                timeout=config.ocr_result_timeout_ms / 1000 + 5,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if worker_task in completed_tasks:
                worker_task.result()
                raise RuntimeError("机器事件 worker 提前结束")
            if ocr_failed_task in completed_tasks:
                if fatal_errors:
                    raise fatal_errors[0]
                raise RuntimeError("真实 OCR 处理失败")
            if ocr_ready_task not in completed_tasks:
                raise TimeoutError("等待真实 OCR 完成超时")
        finally:
            ocr_ready_task.cancel()
            ocr_failed_task.cancel()
            await asyncio.gather(
                ocr_ready_task, ocr_failed_task, return_exceptions=True
            )

        # 核对本轮真实 OCR 终选结果与目标场景一致。
        ocr_result = session.ocr_result
        if ocr_result is None:
            raise RuntimeError("真实 OCR 未产生本轮结果")
        if scenario == "normal" and ocr_result.review_reason is not None:
            raise RuntimeError(f"正常场景实际需要复核：{ocr_result.review_reason}")
        if scenario == "review" and ocr_result.review_reason is None:
            raise RuntimeError("待复核场景实际没有产生 OCR 复核原因")
        ocr_review_reason = ocr_result.review_reason

        # 先交付并确认有效频率，再发送本轮正常 CLOSE。
        measurement = FrequencyMeasurement(
            session_id, machine_config.frequency_meter_serial, 50.0
        )
        await publish_and_wait(RuntimeEvent(
            EventType.FREQUENCY_MEASURED,
            machine_config.machine_id,
            session_id,
            measurement,
        ))
        if session.measurement_frequencies[-1] != measurement:
            raise RuntimeError("模拟频率未进入本轮测量")
        await publish_and_wait(RuntimeEvent(
            EventType.MACHINE_CLOSED, machine_config.machine_id, session_id
        ))

        # 用现有测量记录服务核对正式记录和本轮 JPG 证据。
        measurement_record_service = MeasurementRecordService(
            MeasurementRecordRepo(config.database_path)
        )
        record = measurement_record_service.get_record(session_id)["record"]
        if record is None:
            raise RuntimeError("测量记录没有写入业务库")
        evidence_directory = Path(record["evidence_directory"])
        evidence_paths = sorted(evidence_directory.glob("*.jpg"))
        expected_review = scenario == "review"
        if record["needs_review"] != expected_review:
            raise RuntimeError("历史记录复核状态与真实 OCR 结果不一致")
        if record["final_frequency_hz"] != 50.0:
            raise RuntimeError("历史记录没有保存模拟有效频率")
        if not evidence_directory.is_dir() or not evidence_paths:
            raise RuntimeError("本轮证据 JPG 没有保存")
        if expected_review:
            if record["review_reason"] != ocr_review_reason:
                raise RuntimeError("历史复核原因与真实 OCR 结果不一致")
            if "没有找到最终频率" in record["review_reason"]:
                raise RuntimeError("历史复核原因错误地包含缺少频率")
        elif not record["ordered_lines"]:
            raise RuntimeError("正常记录缺少最终 OCR 文字")

        # 输出刚完成的周期结果供历史页面核对。
        return {
            "session_id": session_id,
            "machine_id": record["machine_id"],
            "ordered_lines": record["ordered_lines"],
            "needs_review": record["needs_review"],
            "final_frequency_hz": record["final_frequency_hz"],
            "review_reason": record["review_reason"],
            "evidence_directory": str(evidence_directory),
            "evidence_count": len(evidence_paths),
        }
    finally:
        try:
            # 停止本轮仍在运行的相机与识别任务。
            if machine is not None:
                await machine.release_resources("PROGRAM_FAILED")
        finally:
            # 取消事件 worker 并释放数据库进程锁。
            if worker_task is not None:
                worker_task.cancel()
                await asyncio.gather(worker_task, return_exceptions=True)
                machine.discard_pending_events()
            database.close()


def run_simulation_cli() -> None:
    """解析开发场景，运行一轮模拟并打印真实历史结果。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 本轮结果已打印，失败时抛出异常
    """
    argument_parser = argparse.ArgumentParser(description="使用真实 OCR 模拟一轮测量")
    argument_parser.add_argument("scenario", choices=("normal", "review"))
    arguments = argument_parser.parse_args()
    result = asyncio.run(run_measurement_simulation(arguments.scenario))
    print(f"Session ID：{result['session_id']}")
    print(f"机器编号：{result['machine_id']}")
    print(f"OCR 结果：{result['ordered_lines']}")
    print(f"状态：{'待复核' if result['needs_review'] else '正常'}")
    print(f"最终频率：{result['final_frequency_hz']:.1f} Hz")
    print(f"复核原因：{result['review_reason'] or '--'}")
    print(f"证据目录：{result['evidence_directory']}")
    print(f"JPG 数量：{result['evidence_count']}")


if __name__ == "__main__":
    run_simulation_cli()
