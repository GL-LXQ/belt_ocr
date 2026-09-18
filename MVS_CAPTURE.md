# 海康 MVS 固定窗口流式采集

底层通过同步逐帧回调交付图像，并已由 `camera.SessionCamera` 接入 App 和 Session。正式流程只使用 MVS 相机，文件夹模拟采集已删除；OCR 支持手动消费内存图片批次，真实模型尚未接入；频率仍为模拟实现。
实现参考 `mvs_tennis/packages/mvs/src/mvs/capture/grab.py`、`capture/pipeline.py` 和 `sdk/camera.py`，
沿用官方 Python 绑定、独立 Grabber、Buffer 复制和释放顺序，不包含同步组包或网球业务。

## 文件与入口

| 文件或接口 | 用途 |
|---|---|
| `mvs_sdk.py` | 加载官方 SDK、枚举和打开相机、配置参数、复制帧、释放设备 |
| `mvs_capture.py` | 独立任务队列、生产消费线程、采集计时、排空和统计 |
| `camera.py` | Session 适配、筛选占位、内存组批、FrameBatchSelected 和 CaptureSealed |
| `load_mvs_sdk(...)` | 加载并初始化 SDK，每个应用使用一个实例 |
| `sdk.enumerate_devices()` | 返回 SDK 设备列表及序列号、传输类型 |
| `sdk.open_camera(serial, ...)` | 按真实序列号打开相机并配置 Continuous / TriggerMode Off |
| `start_capture(camera, callback, session_id, ...)` | 响应采集信号，立即返回任务句柄 |
| `task.stop_requested.set()` | 请求本轮提前停止，仍处理已进入采集流程的帧 |
| `task.acquisition_finished` | 本轮生产已封口，不再有新帧入队，可尝试下一轮采集 |
| `task.wait(timeout_seconds)` | 等待采集和消费均结束，返回 `CaptureResult` |
| `sdk.close()` | 等待相机采集退出，关闭设备、销毁句柄并反初始化 SDK |

默认窗口 1 秒，单任务队列容量 32 帧，单次 SDK 取帧超时 50 毫秒。均可在 `start_capture` 中设置。
本版支持 Windows 官方绑定中的 GigE 和 USB 相机。

## 调用示例

在项目根目录执行以下 Python 代码，将 `serial` 替换为枚举得到的真实相机序列号。
示例逐帧计算图像摘要，回调可以替换为实际图像处理或保存逻辑。

```python
import hashlib
from pathlib import Path

from mvs_capture import CaptureFrame, start_capture
from mvs_sdk import load_mvs_sdk


def process_frame(frame: CaptureFrame) -> dict:
    """处理当前帧并返回轻量结果。

    Args:
        frame: 带周期和任务身份的独立图像帧。

    Returns:
        {
            "frame_number": 1,  # SDK 帧编号
            "image_digest": "...",  # 原始图像字节的 SHA256 摘要
        }
    """
    return {
        "frame_number": frame.image.frame_number,
        "image_digest": hashlib.sha256(frame.image.data).hexdigest(),
    }


def run_camera_capture(serial: str) -> None:
    """打开指定相机、执行一次流式采集并释放资源。

    Args:
        serial: 真实相机序列号。

    Returns:
        None  # 结果已输出
    """
    # 加载本机 SDK，并准备本轮任务句柄。
    sdk = load_mvs_sdk(Path(r"D:\app\HIK\MVS\Development"))
    task = None
    try:
        # 打开设备并响应一次采集信号。
        camera = sdk.open_camera(serial)
        task = start_capture(camera, process_frame, session_id="measurement-001")

        # 等待所有入队帧处理完成，读取统计和逐帧结果。
        result = task.wait(timeout_seconds=10)
        print(result)
    finally:
        # 请求停止并排空本轮任务，然后释放设备与 SDK。
        try:
            if task is not None:
                task.stop_requested.set()
                task.wait()
        finally:
            sdk.close()


run_camera_capture(serial="替换为真实序列号")
```

只枚举设备时，加载 SDK 后调用 `sdk.enumerate_devices()[1]`，并在 `finally` 中调用 `sdk.close()`。
`open_camera` 可选参数为 `pixel_format`、`exposure_time_us`、`gain`；未指定的参数保留设备当前设置，
指定曝光或增益时关闭对应自动模式。SDK 调用失败会报告操作名及十六进制错误码。

默认从 `Development/Samples/Python/MvImport` 加载绑定，从 Windows 公共目录的
`MVS/Runtime/Win64_x64` 或 `Win32_i86` 加载匹配 Python 位数的 DLL。
其他安装布局通过 `load_mvs_sdk(..., dll_directory=Path(...))` 指定。
无需复制整个 SDK，也不依赖 `mvs_tennis` 的运行环境。

## 生产、停止与消费

每轮具有独立 `capture_id`、调用方 `session_id`、有界队列和消费者。
Grabber 执行 `GetImageBuffer → ctypes.string_at 复制 → FreeImageBuffer → put_nowait`。
回调收到的 `frame.image.data` 为独立 `bytes`，不引用 SDK Buffer。
它保留原始像素格式；Bayer、Mono 或其他格式的解码、转换由后续处理负责。

队满时立即丢弃当前新帧并累计数量，Grabber 不等待消费者。
控制线程独立计时，到期或收到提前停止信号后禁止发起新的取帧，等待当前 Buffer 操作归还资源，
调用停止取流，等待 Grabber 的最后一次入队结束，再设置 `acquisition_finished`。
消费者在生产封口且队列排空后退出，控制线程等待消费者退出后才发布最终结果。

固定窗口从 `StartGrabbing` 成功后计时。停止请求可能等待正在执行的取帧和复制，实际取流时长可能略长于设定值；
单次 SDK 等帧使用 `timeout_ms`，实际 SDK 停止耗时也计入统计。这不是硬实时曝光截止。
停止时已进入的取帧操作若返回有效帧，该帧仍归本轮；不会继续主动提取 SDK 中尚未进入取帧流程的积压帧。
下一轮开始前清空 SDK 历史缓存，不复用上一轮的程序队列。
设备时间戳按原始值保留，第一版不把它映射成业务 START/CLOSE 边界。

同一相机采集中再次请求会立即抛出“相机正在采集”。A 轮生产封口后，B 轮可以开始，
此时 A 轮消费者可能仍在编码图片；A 轮的停止信号和结果不会修改 B 轮。
不同相机可以同时运行。消费者回调也可能跨任务并发，共享处理模型或输出资源由调用方管理。

回调必须同步完成当前帧处理后返回，不能仅启动异步任务就返回。
返回值应为轻量结果或已保存文件的路径；模块按本轮保留这些结果，不主动累计原始图像。
`wait` 超时只停止调用方等待，不取消任务。回调若一直不返回，本轮也不会被伪报为完成；
退出时应先通知各任务停止，再等待回调结束，最后关闭 SDK。

## 统计与异常

| `CaptureResult` 字段 | 含义 |
|---|---|
| `capture_duration_seconds` | 启动成功到停止取流调用结束的实际秒数，未启动时为 0 |
| `received_frame_count` | SDK 成功返回的帧数，包含后续复制或释放失败的帧 |
| `enqueued_frame_count` | 成功进入本轮队列的帧数 |
| `dropped_frame_count` | 仅统计程序队列满时丢弃的新帧，不等同设备或网络丢帧 |
| `processed_frame_count` | 回调正常返回的帧数 |
| `failed_frame_count` | 回调抛出异常的帧数 |
| `camera_stopped` | 是否确认相机已停止取流；停止失败为 False |
| `capture_errors` | 启动、取帧、复制、释放和停止阶段的错误 |
| `has_error` | 是否存在采集或单帧处理异常；队列满丢帧单独计数 |
| `frame_results` | 按消费顺序保存帧编号、回调结果和单帧错误 |

单帧回调失败会记录错误并继续消费，整轮标记异常。
取流失败会停止生产并排空已入队帧；取帧、复制、释放或停止失败后，相机禁止继续采集，需要关闭后重新打开。
停止失败时 `acquisition_finished` 仅表示本轮程序不会再入队，不表示硬件已确认停流，必须检查 `camera_stopped`。
正常取帧超时表示暂时没有数据；整轮可能收到 0 帧，调用方应按业务规则判断是否可用。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_mvs_capture.py -q
.\.venv\Scripts\python.exe -m pytest -q
```

测试使用可控假 SDK，覆盖 Buffer 释放后覆盖、采集消费并行、队满丢新帧、固定期限、在途复制、
旧轮消费与新轮采集重叠、回调失败和资源释放。真实 SDK 加载和枚举已在本机执行，但未枚举到相机，
尚未完成真机采集、帧率和现场长时间运行验证。

## App 接入

`App.start()` 按配置打开相机；未配置或未找到设备时单机进入 FAULT。START 使用业务创建的 capture_id 启动取流。CLOSE 只等待生产停止，不等待旧轮图片编码。适配器将图片与含统计的 CaptureSummary 顺序交付机器事件队列。图片批次到达 MachineManager 后立即送入 OCR 队列，封口不再提交 OCR；App 当前不自动启动识别任务。`App.stop()` 先停止并排空相机任务，再关闭 SDK。

## 内存 BMP 与延后保存

Session 消费线程调用 `MvsCamera.encode_image`，通过官方 `MV_CC_SaveImageEx3` 和 `MV_Image_Bmp` 将独立原始帧编码为完整 BMP 字节。此 API 在内存中编码，不写磁盘；同一设备的编码串行执行，使用独立编码锁，不占用取帧锁。

`camera.py` 保留业务时间边界和选帧上限检查，再调用目前统一返回 True 的 `is_frame_qualified`。合格帧携带 image_data 进入本轮批次，不创建证据文件或临时文件。满 8 帧交付 FrameBatchSelected，消费结束后先交付尾批再发布 CaptureSealed。默认每轮最多 5 帧，因此默认只交付尾批。编码失败记录处理错误并继续消费，最终封口包含 CAPTURE_FAILED。

MachineManager 将成功入队的图片保留在 Session.memory_frames 中；OCR 接收 BMP 字节，结果通过 frame_id 关联原图。最终文字与图片选择、保存预留在 select_final_text_and_img 中，算法尚未实现，当前无图片落盘。整轮失败、超时、中断和退出会清理内存，正常关闭允许原图随旧 Session 继续等待。真实 SDK 像素转换、取流和现场内存容量仍需真机验证。
