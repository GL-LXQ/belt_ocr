"""提供不依赖界面框架的业务请求结果。"""

from dataclasses import dataclass


@dataclass
class Result:
    """保存界面请求的成功状态、数据和提示。"""

    success: bool
    data: object | None = None
    message: str = ""

    @classmethod
    def ok(cls, data: object | None = None, message: str = "") -> "Result":
        """创建成功结果。

        Args:
            data: 返回给页面的业务数据。
            message: 返回给页面的提示。

        Returns:
            Result(
                success=True,  # 请求成功
                data="data",  # 页面数据
                message="",  # 页面提示
            )
        """
        return cls(
            success=True,
            data=data,
            message=message,
        )

    @classmethod
    def error(cls, message: str, data: object | None = None) -> "Result":
        """创建失败结果。

        Args:
            message: 返回给页面的失败提示。
            data: 返回给页面的附加数据。

        Returns:
            Result(
                success=False,  # 请求失败
                data=True,  # 页面附加数据
                message="失败",  # 失败提示
            )
        """
        return cls(
            success=False,
            data=data,
            message=message,
        )
