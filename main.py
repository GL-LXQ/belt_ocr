"""执行相机启停信号演示，频率设备由黑盒适配接口提供。"""

import argparse
import asyncio
import logging
from pathlib import Path

from configuration import load_configuration
from app import App


async def run_measurement_demo(configuration_path: Path) -> None:
    """初始化系统、模拟两个周期、等待保存并释放资源。"""
    # 读取配置并启动持续工作的测量应用实例。
    configuration = load_configuration(configuration_path)
    app = App(configuration)
    await app.start()
    try:
        # 选择相机可用的机器，没有设备时仅报告状态。
        machine_ids = [
            identifier for identifier, manager in app.machine_managers.items()
            if manager.acceptance_state == "READY"
        ]
        if not machine_ids:
            logging.warning("没有可测量的机器，请配置真实相机序列号并连接设备。")
            return

        # 同时启动已连接机器的第一轮测量。
        await asyncio.gather(*(
            app.handle_start(machine_id) for machine_id in machine_ids
        ))
        await asyncio.sleep(configuration.capture_window_ms / 1000 + 0.1)
        await asyncio.gather(*(
            app.handle_close(machine_id) for machine_id in machine_ids
        ))

        # 在旧轮后台处理中启动第一台机器的下一轮。
        await app.handle_start(machine_ids[0])
        await asyncio.sleep(configuration.capture_window_ms / 1000 + 0.1)
        await app.handle_close(machine_ids[0])

        # 等待全部结果落库并输出数据库位置。
        await app.wait_until_idle()
        logging.info("演示完成，记录已保存到 %s", configuration.database_path)
    finally:
        await app.stop()


def main() -> None:
    """读取启动参数并运行模拟测量主流程。"""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    argument_parser = argparse.ArgumentParser(description="MVS 相机测量信号演示，频率设备接口待接入")
    argument_parser.add_argument(
        "--config", type=Path, default=Path(__file__).with_name("config.example.json"),
    )
    arguments = argument_parser.parse_args()
    asyncio.run(run_measurement_demo(arguments.config))


if __name__ == "__main__":
    main()
