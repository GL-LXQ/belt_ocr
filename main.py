"""运行三台机器的模拟测量演示。"""

import argparse
import asyncio
import logging
from pathlib import Path

from configuration import load_configuration
from measurement_executor import MeasurementExecutor


async def run_measurement_demo(configuration_path: Path) -> None:
    """初始化系统、模拟两个周期、等待保存并释放资源。"""
    # 读取配置并启动持续工作的测量执行器。
    configuration = load_configuration(configuration_path)
    executor = MeasurementExecutor(configuration)
    await executor.start()
    try:
        # 同时启动各机器的第一轮测量。
        machine_ids = [machine.machine_id for machine in configuration.machines]
        await asyncio.gather(*(
            executor.handle_start(machine_id) for machine_id in machine_ids
        ))
        await asyncio.sleep(configuration.capture_window_ms / 1000 + 0.1)
        await asyncio.gather(*(
            executor.handle_close(machine_id) for machine_id in machine_ids
        ))

        # 在旧轮后台处理中启动第一台机器的下一轮。
        await executor.handle_start(machine_ids[0])
        await asyncio.sleep(configuration.capture_window_ms / 1000 + 0.1)
        await executor.handle_close(machine_ids[0])

        # 等待全部结果落库并输出数据库位置。
        await executor.wait_until_idle()
        logging.info("演示完成，记录已保存到 %s", configuration.database_path)
    finally:
        await executor.stop()


def main() -> None:
    """读取启动参数并运行模拟测量主流程。"""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    argument_parser = argparse.ArgumentParser(description="多皮带机模拟测量")
    argument_parser.add_argument(
        "--config", type=Path, default=Path(__file__).with_name("config.example.json"),
    )
    arguments = argument_parser.parse_args()
    asyncio.run(run_measurement_demo(arguments.config))


if __name__ == "__main__":
    main()
