from measurement_executor import MeasurementExecutor


# 创建供后续信号入口复用的测量执行器。
measurement_executor = MeasurementExecutor()


def main() -> None:
    """运行程序入口。"""
    print("测量执行器已初始化。")


if __name__ == "__main__":
    main()
