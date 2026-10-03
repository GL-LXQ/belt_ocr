"""共用证据目录的系统打开请求和失败提示。"""

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QWidget
from qfluentwidgets import InfoBar


def open_evidence_directory(directory: str | Path, parent: QWidget, evidence_state: str | None = None) -> bool:
    """沿用已读取的目录状态并请求系统文件管理器打开。

    Args:
        directory: 测量记录保存的证据目录路径。
        parent: 打开失败提示所属的控件。
        evidence_state: 后台目录读取的状态；None 表示沿用历史页现有可用目录。

    Returns:
        返回示例：
            True  # 系统已接受打开请求，不代表文件管理器已完成显示
            False  # 目录不可访问或系统拒绝请求，已显示原始路径
    """
    # 沿用后台目录状态，不在主线程重新访问磁盘。
    saved_path = str(directory)
    if not saved_path:
        failure_message = "该记录未保存证据目录路径。"
    elif evidence_state == "missing_directory":
        failure_message = "证据目录不存在或已不是文件夹。"
    elif evidence_state == "access_denied":
        failure_message = "没有权限访问证据目录。"
    elif evidence_state == "read_error":
        failure_message = "访问证据目录失败，请刷新后重试。"
    else:
        # 系统打开请求仍在主线程执行。
        directory_url = QUrl.fromLocalFile(saved_path)
        if QDesktopServices.openUrl(directory_url):
            return True
        failure_message = "系统未能打开证据目录，请检查系统文件夹打开功能。"

    # 在失败提示中保留路径，供操作者核对实际保存位置。
    InfoBar.error(
        "证据文件夹打开失败",
        f"{failure_message}\n保存的路径：{saved_path or '（空）'}",
        duration=-1,
        parent=parent,
    )
    return False
