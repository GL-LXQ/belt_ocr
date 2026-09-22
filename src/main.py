"""执行相机启停信号演示，频率仪由黑盒适配接口提供。"""

import argparse
import asyncio
import logging
from pathlib import Path

from config_util import load_config
from system_runtime import SystemRuntime


async def run_measurement_demo(configuration_directory: Path) -> None:
    """启动测量演示，接收后台故障并统一释放资源。

    Args:
        configuration_directory: 测量 YAML 配置目录。

    Returns:
        None  # 演示正常结束；相机或后台故障时抛出异常
    """
    # 读取配置并准备应用和本次主流程任务。
    try:
        config = load_config(configuration_directory)
        system_runtime = SystemRuntime(config)
    except Exception:
        logging.exception("测量配置初始化失败")
        raise
    tasks = []
    try:
        # 启动应用，并同时等待测量完成或相机故障。
        await system_runtime.start()
        measurement_task = asyncio.create_task(run_measurement_cycles(system_runtime))
        failure_task = asyncio.create_task(system_runtime.wait_for_failure())
        tasks = [measurement_task, failure_task]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        # 优先传播相机故障，否则读取测量主流程的执行结果。
        if system_runtime.failure is not None:
            raise system_runtime.failure
        await measurement_task
    except Exception:
        # 记录尚未由相机或后台任务处理的演示流程异常。
        if system_runtime.failure is None:
            logging.exception("测量流程失败")
        raise
    finally:
        # 取消未完成的演示和故障等待，统一释放所有应用资源。
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await system_runtime.stop()

    # 退出时的相机释放故障同样交给命令行报告失败。
    if system_runtime.failure is not None:
        raise system_runtime.failure


async def run_measurement_cycles(system_runtime: SystemRuntime) -> None:
    """执行两轮演示启停并等待测量结算。

    Args:
        system_runtime: 已启动的测量运行时。

    Returns:
        None  # 演示周期已结束，测量已完成或失败清理
    """
    # 选择现场状态已就绪的机器。
    machine_ids = [
        identifier for identifier, machine in system_runtime.machines.items()
        if machine.acceptance_state == "READY"
    ]
    if not machine_ids:
        logging.warning("没有已就绪的机器，请确认现场已关闭。")
        return

    # 同时启动全部机器的第一轮测量，再发送正常关闭。
    await asyncio.gather(*(system_runtime.handle_start(machine_id) for machine_id in machine_ids))
    await asyncio.sleep(system_runtime.config.capture_window_ms / 1000 + 0.1)
    await asyncio.gather(*(system_runtime.handle_close(machine_id) for machine_id in machine_ids))

    # 等待第一轮全部保存或清理完成，再启动下一轮。
    await system_runtime.wait_until_idle()
    await system_runtime.handle_start(machine_ids[0])
    await asyncio.sleep(system_runtime.config.capture_window_ms / 1000 + 0.1)
    await system_runtime.handle_close(machine_ids[0])

    # 等待全部测量结算，输出正常结果的数据库位置。
    await system_runtime.wait_until_idle()
    logging.info("演示结束，正常结果数据库：%s；失败原因见日志。", system_runtime.config.database_path)


def main() -> None:
    """读取启动参数并运行测量主流程，异常时记录日志并失败退出。

    Args:
        无外部参数；配置路径从命令行读取。

    Returns:
        None  # 正常结束；程序故障时以退出码 1 结束
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    argument_parser = argparse.ArgumentParser(description="MVS 相机测量信号演示，频率仪接口待接入")
    argument_parser.add_argument(
        "--config", type=Path, default=Path(__file__).resolve().parents[1] / "config",
    )
    arguments = argument_parser.parse_args()
    try:
        asyncio.run(run_measurement_demo(arguments.config))
    except Exception:
        # 异常已经在业务入口记录，此处仅设置失败退出码。
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
