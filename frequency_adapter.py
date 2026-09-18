"""定义频率设备黑盒契约，组织窗口登记和有限时间收尾。"""

import asyncio
import logging
from dataclasses import dataclass

from configuration import MachineConfiguration, MeasurementConfiguration
from models import MeasurementEvent, PublishEvent


logger = logging.getLogger(__name__)


@dataclass
class FrequencyWindow:
    """保存同一主机单调时钟下的周期测量边界。"""

    session_id: str
    start_boundary: float
    close_boundary: float | None = None


class FrequencyAdapter:
    """管理周期窗口，将设备协议处理封装在监听和收尾接口中。"""

    def __init__(
        self,
        machine: MachineConfiguration,
        configuration: MeasurementConfiguration,
        publish_event: PublishEvent,
    ) -> None:
        """登记设备配置、结果事件入口和窗口集合。

        Args:
            machine: 当前机器和频率来源绑定。
            configuration: 频率有效范围和收尾等待期限。
            publish_event: 按机器和周期投递测量及终态事件的入口。

        Returns:
            None  # 适配器已初始化，尚未连接设备
        """
        # 保存设备绑定、配置和事件交付入口。
        self.machine = machine
        self.configuration = configuration
        self.publish_event = publish_event

        # 登记活动窗口、历史收尾窗口和收尾任务。
        self.active_window: FrequencyWindow | None = None
        self.windows: dict[str, FrequencyWindow] = {}
        self.tasks: set[asyncio.Task[None]] = set()

    def open_window(self, session_id: str, start_boundary: float) -> None:
        """登记 START 边界，开放本轮频率归属窗口。

        Args:
            session_id: 本轮测量编号。
            start_boundary: START 对应的主机单调时间，单位为秒。

        Returns:
            None  # 新窗口已登记，旧轮收尾窗口继续保留
        """
        # 创建并登记当前周期，供持续监听接口确定测量归属。
        window = FrequencyWindow(session_id, start_boundary)
        self.windows[session_id] = window
        self.active_window = window

    def seal_window(self, session_id: str, close_boundary: float) -> None:
        """固定 CLOSE 边界并安排本轮在途测量收尾。

        Args:
            session_id: 已打开、尚未请求封口的周期编号。
            close_boundary: CLOSE 对应的主机单调时间，单位为秒。

        Returns:
            None  # 截止时间已固定，收尾任务已启动
        """
        # 固定本轮边界并释放现场活动窗口。
        window = self.windows[session_id]
        window.close_boundary = close_boundary
        self.active_window = None

        # 安排收尾主流程，退出时由 App 取消并等待任务。
        task = asyncio.create_task(self.finish_frequency_window(window))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def run(self) -> None:
        """运行设备监听入口，并将设备异常交给所属机器处理。

        Args:
            无外部参数。

        Returns:
            None  # 监听结束；设备异常通过 DeviceFault 事件交付
        """
        # 执行设备监听，连接和读取异常报告为设备故障。
        try:
            await self.listen_measurements()
        except Exception:
            logger.exception("频率设备监听失败 machine_id=%s", self.machine.machine_id)
            await self.publish_event(MeasurementEvent(
                "DeviceFault", self.machine.machine_id,
                payload=self.machine.frequency_source_id,
            ))

    async def listen_measurements(self) -> None:
        """预留设备连接、持续读取、测量规范化和资源释放的黑盒入口。

        Args:
            无外部参数；通过 windows 读取周期边界。

        Returns:
            None  # 持续监听直至取消；通过 FrequencyMeasured 交付测量

        接口约定：
            每个事件包含 machine_id、session_id 和 FrequencyMeasurement。
            仅交付通过有效性、来源和时间归属检查的新测量，同一测量只交付一次。
            measured_monotonic 统一到窗口使用的主机单调时钟，measured_at 保留 UTC 时间。
            关闭前产生的晚到测量归原周期；无法确认归属时报告失败，不分配给新周期。
            本接口在退出或取消时停止接收并释放设备连接。
        """
        # 待实现：连接设备、识别新有效测量、映射时间、交付结果并释放连接。
        raise NotImplementedError("频率设备监听接口尚未实现。")

    async def drain_measurements(self, window: FrequencyWindow) -> None:
        """预留确认并交付指定窗口全部在途测量的黑盒入口。

        Args:
            window: 已固定 START/CLOSE 边界的周期窗口。

        Returns:
            None  # 本轮测量已全部交付，后续不再发布本轮测量

        接口约定：
            仅在本轮关闭前产生的测量已全部交付后返回，不以等待一段时间代替收齐确认。
            与持续监听共享测量身份和交付进度，避免重复交付。
            异常或取消时停止本轮交付并清理本轮资源，不修改其他周期。
        """
        # 待实现：确认设备输出进度，交付本轮在途数据并结束本轮接收。
        raise NotImplementedError("频率设备收尾接口尚未实现。")

    async def finish_frequency_window(self, window: FrequencyWindow) -> None:
        """有限等待本轮在途测量，交付成功封口或失败事件并释放窗口。

        Args:
            window: 已请求关闭的周期窗口。

        Returns:
            None  # 本轮封口或失败已交付，窗口已释放
        """
        # 在配置期限内等待黑盒完成数据交付。
        try:
            await asyncio.wait_for(
                self.drain_measurements(window),
                self.configuration.frequency_drain_timeout_ms / 1000,
            )
        except asyncio.TimeoutError:
            event_type = "FrequencyFailed"
            error_code = "FREQUENCY_DRAIN_TIMEOUT"
        except Exception as error:
            event_type = "FrequencyFailed"
            error_code = "FREQUENCY_DRAIN_FAILED"
            logger.error("频率收尾失败 session_id=%s error=%s", window.session_id, error)
        else:
            event_type = "FrequencyWindowSealed"
            error_code = None
        finally:
            # 仅释放本轮窗口，保留其他周期的监听和收尾状态。
            self.windows.pop(window.session_id)

        # 在全部已接收测量之后交付本轮终态，不把失败转换为成功封口。
        await self.publish_event(MeasurementEvent(
            event_type, self.machine.machine_id, window.session_id, error_code,
        ))
