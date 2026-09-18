"""验证按 Session 结算批次并触发一次文字后处理。"""

import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest

from app import App
from configuration import load_configuration
from models import BeltSession, CapturedFrame, CaptureSummary, MeasurementEvent


@pytest.fixture
def recognition_context():
    """准备不同机器和周期的业务对象及后处理替身。

    Args:
        无外部参数。

    Returns:
        (
            app,  # 未启动设备的应用对象
            sessions,  # 三个独立 Session，前两个属于 M01，最后一个属于 M02
            frames,  # 各 Session 对应的测试图片
        )
    """
    # 加载配置并建立业务对象，不启动设备和后台任务。
    configuration = load_configuration(Path(__file__).resolve().parents[1] / "config.example.json")
    app = App(configuration)
    app.text_recognizer.select_final_text_and_img = Mock(
        wraps=app.text_recognizer.select_final_text_and_img
    )
    sessions = []
    frames = []

    # 为同机跨轮和跨机器场景分别建立独立 Session。
    for session_number, machine_id in enumerate(("M01", "M01", "M02")):
        session = BeltSession(
            session_id=f"session-{session_number}",
            machine_id=machine_id,
            camera_id=f"camera-{machine_id}",
            frequency_source_id=f"frequency-{machine_id}",
            capture_id=f"capture-{session_number}",
            start_time="2026-09-18T00:00:00+00:00",
            capture_start_time=0,
        )
        app.machine_managers[machine_id].sessions[session.session_id] = session
        sessions.append(session)

        # 准备携带原周期身份的图片对象。
        frames.append(CapturedFrame(
            session_id=session.session_id,
            capture_id=session.capture_id,
            camera_id=session.camera_id,
            frame_id=f"frame-{session_number}",
            captured_at=session.start_time,
            captured_monotonic=1,
            image_data=b"BM-test-image",
        ))

    return (
        app,
        sessions,
        frames,
    )


@pytest.mark.parametrize("seal_first", [True, False])
def test_postprocessing_waits_for_seal_and_results(recognition_context, seal_first):
    """验证封口与结果两种先后顺序均只触发一次后处理。

    Args:
        recognition_context: 应用、周期和图片测试对象。
        seal_first: 是否先交付封口事件。

    Returns:
        None  # 完成触发时机和原始结果传递断言
    """
    app, sessions, frames = recognition_context
    session = sessions[0]
    manager = app.machine_managers[session.machine_id]
    results = [{
        "frame_id": frames[0].frame_id,
        "blocks": [{"lines": [{"text": "003"}]}],
    }]

    async def deliver_events():
        """按指定顺序交付入队、封口和结果事件。

        Args:
            无外部参数。

        Returns:
            None  # 完成事件交付和触发检查
        """
        # 提交本轮批次并检查独立待处理数量。
        await manager.apply_event(
            MeasurementEvent("FrameBatchSelected", session.machine_id, session.session_id, (frames[0],))
        )
        assert session.pending_recognition_batches == 1
        events = [
            MeasurementEvent(
                "CaptureSealed",
                session.machine_id,
                session.session_id,
                CaptureSummary(session.capture_id),
            ),
            MeasurementEvent("RecognitionBatchCompleted", session.machine_id, session.session_id, results),
        ]
        if not seal_first:
            events.reverse()

        # 第一个事件尚不足以触发，第二个事件才满足全部条件。
        await manager.apply_event(events[0])
        app.text_recognizer.select_final_text_and_img.assert_not_called()
        await manager.apply_event(events[1])
        app.text_recognizer.select_final_text_and_img.assert_called_once_with(results, session.images_for_final_selection)

        # 再次检查同一周期不会重复触发。
        await manager.apply_event(MeasurementEvent(
            "CaptureSealed", session.machine_id, session.session_id, CaptureSummary(session.capture_id),
        ))
        assert session.pending_recognition_batches == 0
        assert session.text_postprocessing_started
        app.text_recognizer.select_final_text_and_img.assert_called_once()

    asyncio.run(deliver_events())


def test_failed_and_rejected_batches_preserve_successful_results(recognition_context):
    """验证失败和拒收批次跳过，其他 Session 不影响本轮后处理。

    Args:
        recognition_context: 应用、周期和图片测试对象。

    Returns:
        None  # 完成隔离、失败结算和部分成功后处理断言
    """
    app, sessions, frames = recognition_context
    session = sessions[0]
    manager = app.machine_managers[session.machine_id]

    async def deliver_events():
        """交付跨机器、跨周期和部分失败事件。

        Args:
            无外部参数。

        Returns:
            None  # 只对已结算的目标周期触发后处理
        """
        # 为三个周期各提交一批，再给目标周期提交第二批。
        for current_session, frame in zip(sessions, frames):
            current_manager = app.machine_managers[current_session.machine_id]
            await current_manager.apply_event(MeasurementEvent(
                "FrameBatchSelected", current_session.machine_id, current_session.session_id, (frame,),
            ))
        await manager.apply_event(
            MeasurementEvent("FrameBatchSelected", session.machine_id, session.session_id, (frames[0],))
        )

        # 拒收目标周期的额外批次，计数保持两批且不会冻结结果。
        app.text_recognizer.accepting_batches = False
        await manager.apply_event(
            MeasurementEvent("FrameBatchSelected", session.machine_id, session.session_id, (frames[0],))
        )
        assert session.pending_recognition_batches == 2
        assert session.frozen_payload is None

        # 目标周期封口，一批失败后仍等待另一批结果。
        await manager.apply_event(MeasurementEvent(
            "CaptureSealed", session.machine_id, session.session_id, CaptureSummary(session.capture_id),
        ))
        await manager.apply_event(
            MeasurementEvent("RecognitionBatchFailed", session.machine_id, session.session_id, "模型失败")
        )
        app.text_recognizer.select_final_text_and_img.assert_not_called()

        # 最后一批成功结果返回后，只处理目标周期的有效文字。
        results = [{
            "frame_id": frames[0].frame_id,
            "blocks": [{"lines": [{"text": "003"}]}],
        }]
        await manager.apply_event(
            MeasurementEvent("RecognitionBatchCompleted", session.machine_id, session.session_id, results)
        )
        app.text_recognizer.select_final_text_and_img.assert_called_once_with(results, session.images_for_final_selection)
        assert [current.pending_recognition_batches for current in sessions] == [0, 1, 1]
        assert session.errors == ["OCR_BATCH_REJECTED", "模型失败"]

    asyncio.run(deliver_events())


@pytest.mark.parametrize("completion", ["no_frames", "empty_blocks", "failed"])
def test_no_usable_text_skips_postprocessing(recognition_context, completion):
    """验证无合格图片、无文字或全部失败时跳过后处理。

    Args:
        recognition_context: 应用、周期和图片测试对象。
        completion: 无图片、空文字或识别失败场景。

    Returns:
        None  # 完成无文字跳过和一次性收尾断言
    """
    app, sessions, frames = recognition_context
    session = sessions[0]
    manager = app.machine_managers[session.machine_id]

    async def deliver_events():
        """交付无可用文字的周期事件并检查后处理入口。

        Args:
            无外部参数。

        Returns:
            None  # 本轮只进入一次筛选入口，空文字在入口内部跳过
        """
        # 有图片时先提交并结算识别任务。
        if completion != "no_frames":
            await manager.apply_event(MeasurementEvent(
                "FrameBatchSelected", session.machine_id, session.session_id, (frames[0],),
            ))
            if completion == "failed":
                event_type, payload = "RecognitionBatchFailed", "模型失败"
            else:
                event_type, payload = "RecognitionBatchCompleted", [{"blocks": []}]
            await manager.apply_event(MeasurementEvent(event_type, session.machine_id, session.session_id, payload))

        # 封口并重复交付封口事件，确认筛选入口只调用一次。
        await manager.apply_event(MeasurementEvent(
            "CaptureSealed", session.machine_id, session.session_id, CaptureSummary(session.capture_id),
        ))
        await manager.apply_event(MeasurementEvent(
            "CaptureSealed", session.machine_id, session.session_id, CaptureSummary(session.capture_id),
        ))
        assert session.text_postprocessing_started
        assert session.pending_recognition_batches == 0
        app.text_recognizer.select_final_text_and_img.assert_called_once_with(
            session.recognition_results, session.images_for_final_selection
        )

    asyncio.run(deliver_events())


@pytest.mark.parametrize("sealed,pending,finished", [(False, 0, False), (True, 1, False), (True, 0, True)])
def test_ocr_finished_check_only_returns_state(recognition_context, sealed, pending, finished):
    """验证结束判断不受筛选状态影响且不修改 Session。

    Args:
        recognition_context: 应用、周期和图片测试对象。
        sealed: 本轮是否已封口。
        pending: 本轮待处理批次数。
        finished: 预期的识别结束判断结果。

    Returns:
        None  # 完成纯状态判断和无副作用断言
    """
    # 准备本轮采集状态和已触发筛选标记。
    app, sessions, frames = recognition_context
    session = sessions[0]
    session.is_capture_finished = sealed
    session.pending_recognition_batches = pending
    session.text_postprocessing_started = True

    # 检查判断结果，确认不改状态也不调用筛选。
    manager = app.machine_managers[session.machine_id]
    assert manager.is_session_ocr_finished(session) is finished
    assert session.is_capture_finished is sealed
    assert session.pending_recognition_batches == pending
    assert session.text_postprocessing_started
    app.text_recognizer.select_final_text_and_img.assert_not_called()


@pytest.mark.parametrize("failure_event", ["OCRTimeout", "OCRFailed"])
def test_terminal_failure_releases_only_target_images(recognition_context, failure_event):
    """验证整轮失败和超时释放本轮图片，保留其他周期排队图片。

    Args:
        recognition_context: 应用、周期和图片测试对象。
        failure_event: 整轮失败或超时事件名称。

    Returns:
        None  # 完成内存释放、队列记账及跨周期隔离断言
    """
    app, sessions, frames = recognition_context
    session = sessions[0]
    manager = app.machine_managers[session.machine_id]

    async def deliver_failure():
        """交付图片和失败事件并检查迟到图片被丢弃。

        Args:
            无外部参数。

        Returns:
            None  # 已验证异常清理及剩余批次可正常结算
        """
        # 提交两个周期图片，保留相互独立的内存引用。
        for current_session, frame in zip(sessions[:2], frames[:2]):
            await manager.apply_event(MeasurementEvent(
                "FrameBatchSelected", current_session.machine_id,
                current_session.session_id, (frame,),
            ))
        assert session.images_for_final_selection[frames[0].frame_id] is frames[0]

        # 本轮异常后释放图片并移除排队批次，不执行终选。
        await manager.apply_event(MeasurementEvent(
            failure_event, session.machine_id, session.session_id,
        ))
        assert session.images_for_final_selection == {}
        assert session.pending_recognition_batches == 0
        assert sessions[1].images_for_final_selection[frames[1].frame_id] is frames[1]
        app.text_recognizer.select_final_text_and_img.assert_not_called()

        # 迟到批次不再占用内存，其他周期批次仍可消费并完成队列记账。
        await manager.apply_event(MeasurementEvent(
            "FrameBatchSelected", session.machine_id, session.session_id,
            (frames[0],),
        ))
        remaining_batch = app.text_recognizer.batch_queue.get_nowait()
        assert remaining_batch.session_id == sessions[1].session_id
        app.text_recognizer.batch_queue.task_done()
        await asyncio.wait_for(app.text_recognizer.batch_queue.join(), 1)
        assert session.images_for_final_selection == {}

    asyncio.run(deliver_failure())
