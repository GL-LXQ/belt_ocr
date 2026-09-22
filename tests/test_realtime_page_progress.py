"""验证后台本轮阶段状态落到实时监测页的进度节点和卡片文案上。"""

import asyncio
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

# 在导入 Qt 之前指定离屏渲染，测试期间不弹出真实窗口。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 把项目根目录加入搜索路径，ui 包内的模块按 from src.xxx 导入后端模块。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

from app import App
from enums import ProgressStage, ProgressStatus
from local_test_support import FakeMvsSdk, build_config, create_machine_database
from repo.machine_repo import MachineRepo
from service.machine_service import MachineService
from ui.pages.realtime_page import RealtimePage, StepProgress


# 进度节点使用的显示颜色，与页面内的调色板一致。
DOT_PENDING_COLOR = "#c5cfda"
DOT_RUNNING_COLOR = "#2f7cf6"
DOT_SUCCESS_COLOR = "#18ae59"
DOT_FAILED_COLOR = "#ef4444"
CONNECTOR_PENDING_COLOR = "#d9e1ea"
CONNECTOR_SUCCESS_COLOR = "#18ae59"

# 正常一周期内后台上报的九个阶段状态，顺序与 Machine 的处理流程一致。
CYCLE_PROGRESS_EVENTS = (
    (ProgressStage.SESSION_START, ProgressStatus.SUCCESS),
    (ProgressStage.IMAGE_CAPTURE, ProgressStatus.RUNNING),
    (ProgressStage.FREQUENCY_COLLECTION, ProgressStatus.RUNNING),
    (ProgressStage.IMAGE_CAPTURE, ProgressStatus.SUCCESS),
    (ProgressStage.CHARACTER_RECOGNITION, ProgressStatus.RUNNING),
    (ProgressStage.CHARACTER_RECOGNITION, ProgressStatus.SUCCESS),
    (ProgressStage.FREQUENCY_COLLECTION, ProgressStatus.SUCCESS),
    (ProgressStage.EVIDENCE_STORAGE, ProgressStatus.RUNNING),
    (ProgressStage.EVIDENCE_STORAGE, ProgressStatus.SUCCESS),
)


@pytest.fixture(scope="session", autouse=True)
def qt_application() -> QApplication:
    """为整个测试会话提供离屏 Qt 应用实例。

    Args:
        无。

    Returns:
        返回示例：
            QApplication()  # 已创建或复用的离屏应用实例
    """
    return QApplication.instance() or QApplication([])


@pytest.fixture
def realtime_page(tmp_path: Path) -> RealtimePage:
    """按业务库的一台启用机器建立实时监测页。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        返回示例：
            RealtimePage()  # 已建好一张机器卡片，卡片区含该机器编号
    """
    # 建好含一台启用机器的业务库，并据此创建页面。
    database_path = tmp_path / "measurements.sqlite3"
    create_machine_database(database_path, [
        {
            "machine_name": "一号皮带机",
            "camera_serial": "CAM-A",
            "frequency_meter_serial": "FREQ-A",
        },
    ])
    page = RealtimePage(MachineService(MachineRepo(database_path)))
    page.resize(1180, 760)
    page.show()
    QApplication.processEvents()
    return page


def page_machine_id(page: RealtimePage) -> str:
    """取页面卡片区里第一台机器的编号。

    Args:
        page: 已建好卡片的实时监测页。

    Returns:
        返回示例：
            "1"  # 数据库自增编号的字符串形式
    """
    return next(iter(page.cards_by_machine_id))


def read_node_colors(step: StepProgress) -> tuple[list[str], list[str]]:
    """按显示顺序读出五个进度圆点和四条连接线的实际颜色。

    Args:
        step: 待读取的步骤进度组件。

    Returns:
        返回示例：
            (
                ["#18ae59", "#2f7cf6", "#c5cfda", "#c5cfda", "#c5cfda"],  # 五个圆点的显示颜色
                ["#18ae59", "#d9e1ea", "#d9e1ea", "#d9e1ea"],  # 四条连接线的显示颜色
            )
    """
    # 固定尺寸并完成一次布局，让连接线取到相邻圆点之间的几何位置。
    step.resize(420, 60)
    step.show()
    QApplication.processEvents()
    image = step.grab().toImage()

    # 圆点取圆环内、图标上方的像素，避开圆点中心的图标。
    dot_colors = []
    for dot in step.dots:
        rect = dot.geometry()
        dot_colors.append(image.pixelColor(rect.center().x(), rect.center().y() - 9).name())

    # 连接线取中段像素，未着色时读到的是组件背景色。
    connector_colors = []
    for connector in step.connectors:
        rect = connector.geometry()
        connector_colors.append(image.pixelColor(rect.x() + rect.width() // 2, rect.y() + 1).name())
    return dot_colors, connector_colors


def test_page_shows_pending_progress_before_first_stage(realtime_page: RealtimePage) -> None:
    """验证未收到阶段信号时五个圆点都停在待完成颜色。

    Args:
        realtime_page: 已建好一张机器卡片的实时监测页。

    Returns:
        None  # 圆点保持待完成色，卡片显示未启动
    """
    # 未收到任何阶段信号时，五个圆点都是待完成颜色。
    card = realtime_page.cards_by_machine_id[page_machine_id(realtime_page)]
    dot_colors, _ = read_node_colors(card.steps)
    assert dot_colors == [DOT_PENDING_COLOR] * 5

    # 卡片建立后随即按初始连接状态刷新，文案停在未启动。
    assert card.badge.text() == "未启动"
    assert card.state_label.text() == "未启动"


def test_progress_follows_backend_stage_sequence(realtime_page: RealtimePage) -> None:
    """验证一周期内逐条上报的阶段状态按顺序落到圆点和卡片上。

    Args:
        realtime_page: 已建好一张机器卡片的实时监测页。

    Returns:
        None  # 五个圆点全部成功，卡片停在证据入库已完成
    """
    # 按后台真实顺序逐条推入阶段状态。
    machine_id = page_machine_id(realtime_page)
    card = realtime_page.cards_by_machine_id[machine_id]
    for stage, status in CYCLE_PROGRESS_EVENTS:
        realtime_page.update_measurement_progress(machine_id, "session-1", stage.value, status.value)

        # 图像采集中途核对：第一步已完成、第二步运行中、其余待完成。
        if (stage, status) == (ProgressStage.IMAGE_CAPTURE, ProgressStatus.RUNNING):
            dot_colors, _ = read_node_colors(card.steps)
            assert dot_colors[0] == DOT_SUCCESS_COLOR
            assert dot_colors[1] == DOT_RUNNING_COLOR
            assert dot_colors[2:] == [DOT_PENDING_COLOR] * 3
            assert card.state_label.text() == "图像采集进行中"

    # 一周期结束后五个圆点全部成功，卡片停在证据入库已完成。
    dot_colors, _ = read_node_colors(card.steps)
    assert dot_colors == [DOT_SUCCESS_COLOR] * 5
    assert card.badge.text() == "测量中"
    assert card.state_label.text() == "证据入库已完成"
    assert card.progress_statuses == {
        "session_start": "success",
        "image_capture": "success",
        "frequency_collection": "success",
        "character_recognition": "success",
        "evidence_storage": "success",
    }


def test_new_session_clears_previous_progress(realtime_page: RealtimePage) -> None:
    """验证下一轮周期到达时清空上一轮留下的阶段状态。

    Args:
        realtime_page: 已建好一张机器卡片的实时监测页。

    Returns:
        None  # 只保留新周期已上报的阶段，其余圆点回到待完成色
    """
    # 先让第一轮全部成功。
    machine_id = page_machine_id(realtime_page)
    card = realtime_page.cards_by_machine_id[machine_id]
    for stage, status in CYCLE_PROGRESS_EVENTS:
        realtime_page.update_measurement_progress(machine_id, "session-1", stage.value, status.value)

    # 第二轮的第一条上报到达时，上一轮五个阶段被清空。
    realtime_page.update_measurement_progress(
        machine_id, "session-2", ProgressStage.SESSION_START.value, ProgressStatus.SUCCESS.value,
    )
    assert card.progress_session_id == "session-2"
    assert card.progress_statuses == {"session_start": "success"}
    dot_colors, _ = read_node_colors(card.steps)
    assert dot_colors == [DOT_SUCCESS_COLOR] + [DOT_PENDING_COLOR] * 4


def test_failed_stage_marks_card_without_touching_other_stages(realtime_page: RealtimePage) -> None:
    """验证频率采集失败时卡片改为测量失败，其余阶段状态保留。

    Args:
        realtime_page: 已建好一张机器卡片的实时监测页。

    Returns:
        None  # 失败节点为失败色，卡片显示测量失败
    """
    # 推入两个成功阶段和一次频率结算失败。
    machine_id = page_machine_id(realtime_page)
    card = realtime_page.cards_by_machine_id[machine_id]
    for stage, status in (
        (ProgressStage.SESSION_START, ProgressStatus.SUCCESS),
        (ProgressStage.IMAGE_CAPTURE, ProgressStatus.SUCCESS),
        (ProgressStage.FREQUENCY_COLLECTION, ProgressStatus.FAILED),
    ):
        realtime_page.update_measurement_progress(machine_id, "session-1", stage.value, status.value)

    # 已成功的阶段保持成功，失败阶段显示失败色。
    assert card.progress_statuses == {
        "session_start": "success",
        "image_capture": "success",
        "frequency_collection": "failed",
    }
    dot_colors, _ = read_node_colors(card.steps)
    assert dot_colors == [
        DOT_SUCCESS_COLOR,
        DOT_SUCCESS_COLOR,
        DOT_FAILED_COLOR,
        DOT_PENDING_COLOR,
        DOT_PENDING_COLOR,
    ]

    # 卡片按失败状态显示。
    assert card.badge.text() == "测量失败"
    assert card.state_label.text() == "频率采集失败"
    assert card.property("tone") == "waiting"


def test_connectors_follow_adjacent_stage_state(realtime_page: RealtimePage) -> None:
    """验证连接线只在相邻两个阶段都成功时显示完成颜色。

    Args:
        realtime_page: 已建好一张机器卡片的实时监测页。

    Returns:
        None  # 相邻都成功的连接线为完成色，其余为待完成色
    """
    # 推入两个相邻的成功阶段和一次失败阶段。
    machine_id = page_machine_id(realtime_page)
    card = realtime_page.cards_by_machine_id[machine_id]
    for stage, status in (
        (ProgressStage.SESSION_START, ProgressStatus.SUCCESS),
        (ProgressStage.IMAGE_CAPTURE, ProgressStatus.SUCCESS),
        (ProgressStage.FREQUENCY_COLLECTION, ProgressStatus.FAILED),
    ):
        realtime_page.update_measurement_progress(machine_id, "session-1", stage.value, status.value)

    # 只有前两个相邻阶段都成功的那条连接线是完成色。
    _, connector_colors = read_node_colors(card.steps)
    assert connector_colors == [
        CONNECTOR_SUCCESS_COLOR,
        CONNECTOR_PENDING_COLOR,
        CONNECTOR_PENDING_COLOR,
        CONNECTOR_PENDING_COLOR,
    ], "连接线未按相邻阶段状态着色，读到的颜色是组件背景色"

    # 整轮全部成功后四条连接线都是完成色。
    for stage, status in CYCLE_PROGRESS_EVENTS:
        realtime_page.update_measurement_progress(machine_id, "session-2", stage.value, status.value)
    _, connector_colors = read_node_colors(card.steps)
    assert connector_colors == [CONNECTOR_SUCCESS_COLOR] * 4, "全部成功后连接线未显示完成色"


def test_progress_for_missing_card_is_ignored(realtime_page: RealtimePage) -> None:
    """验证信号对应的机器已不在卡片区时跳过本次更新。

    Args:
        realtime_page: 已建好一张机器卡片的实时监测页。

    Returns:
        None  # 缺失卡片不抛异常，其余卡片不受影响
    """
    # 运行期间机器被移除并刷新后，卡片区不再有该机器编号。
    existing_machine_id = page_machine_id(realtime_page)
    assert "99" not in realtime_page.cards_by_machine_id

    # 与 update_connection_state 一样跳过缺失卡片。
    realtime_page.update_measurement_progress(
        "99", "session-1", ProgressStage.SESSION_START.value, ProgressStatus.SUCCESS.value,
    )
    card = realtime_page.cards_by_machine_id[existing_machine_id]
    assert card.progress_statuses == {}


def test_failed_cycle_reports_to_page_without_evidence(tmp_path: Path) -> None:
    """验证识别失败周期只上报识别与频率失败，回放后卡片显示测量失败。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        None  # 失败轮不上报证据入库且不写测量记录，页面显示测量失败
    """
    # 建好一台启用机器和测试配置，并用相机替身启动应用。
    config = build_config(
        tmp_path,
        capture_window_ms=200,
        camera_timeout_ms=20,
        frequency_interval_ms=20,
    )
    create_machine_database(config.database_path, [
        {
            "machine_name": "一号皮带机",  # 机器名称
            "camera_serial": "CAM-A",  # 相机序列号
            "frequency_meter_serial": "FREQ-A",  # 频率仪序列号
        },
    ])
    sdk = FakeMvsSdk()
    progress_events = []
    application = App(config)

    def fail_recognition(images: list[bytes]) -> list[dict]:
        """用模型故障构造整轮识别失败。

        Args:
            images: 按顺序排列的合格图片字节。

        Returns:
            无正常返回值  # 直接抛出识别故障，由整轮任务转为识别失败事件
        """
        raise RuntimeError("OCR_MODEL_FAILED")

    application.text_recognizer.recognize_images = fail_recognition

    async def run_failed_cycle() -> None:
        """启动一轮采集，等识别失败后关闭并释放资源。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 失败轮已结算，资源已释放
        """
        # 启动应用并开始本轮采集。
        await application.start(
            notify_measurement_progress=lambda machine_id, session_id, stage, status: progress_events.append(
                (machine_id, session_id, stage, status)
            )
        )
        machine_id = next(iter(application.machines))
        await application.handle_start(machine_id)

        # 等识别失败上报后再关闭，避免关闭先结算导致失败事件被状态守卫丢弃。
        deadline = asyncio.get_running_loop().time() + 5
        while not any(
            stage == ProgressStage.CHARACTER_RECOGNITION and status == ProgressStatus.FAILED
            for _, _, stage, status in progress_events
        ):
            assert asyncio.get_running_loop().time() < deadline, "识别失败未在期限内上报"
            await asyncio.sleep(0.02)

        # 发送正常关闭，等本轮失败清理完成并释放资源。
        await application.handle_close(machine_id)
        await application.wait_until_idle(10)
        await application.stop()

    with patch("app.load_mvs_sdk", lambda *arguments: sdk):
        asyncio.run(run_failed_cycle())

    # 失败轮只上报识别失败与频率结算失败，不上报证据入库。
    reported_events = [(stage.value, status.value) for _, _, stage, status in progress_events]
    assert ("character_recognition", "failed") in reported_events
    assert ("character_recognition", "success") not in reported_events
    assert ("frequency_collection", "failed") in reported_events
    assert not [event for event in reported_events if event[0] == "evidence_storage"]

    # 失败轮不写入测量记录。
    with closing(sqlite3.connect(config.database_path)) as connection:
        record_count = connection.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
    assert record_count == 0

    # 把这次失败轮的上报回放到页面，卡片显示测量失败。
    page = RealtimePage(MachineService(MachineRepo(config.database_path)))
    page.resize(1180, 760)
    page.show()
    QApplication.processEvents()
    machine_id = page_machine_id(page)
    for reported_machine_id, session_id, stage, status in progress_events:
        if reported_machine_id == machine_id:
            page.update_measurement_progress(reported_machine_id, session_id, stage.value, status.value)
    card = page.cards_by_machine_id[machine_id]
    assert card.badge.text() == "测量失败"
    assert card.state_label.text() == "频率采集失败"
    dot_colors, _ = read_node_colors(card.steps)
    assert dot_colors[3] == DOT_FAILED_COLOR
