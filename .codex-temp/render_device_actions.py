"""渲染设备管理页和编辑弹窗，检查按钮接线与表单回填。"""

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from src.machine_service import MachineService
from src.repo.machine_repo import MachineRepo
from ui.main_window import MainWindow

OUTPUT_DIRECTORY = Path(__file__).resolve().parent


def main():
    """写入两台演示设备并渲染列表页与编辑弹窗。"""
    application = QApplication(sys.argv)

    # 准备独立的预览数据库并写入两台设备。
    database_path = OUTPUT_DIRECTORY / "device-preview.sqlite3"
    database_path.unlink(missing_ok=True)
    with closing(sqlite3.connect(database_path)) as connection, connection:
        MachineRepo.create_table(connection)
    repository = MachineRepo(database_path)
    repository.insert("1号皮带机", "MV-CA013456", "FM-1001", True, "1号产线检测设备")
    repository.insert("2号皮带机", "MV-CA013457", "FM-1002", False, "2号产线检测设备")

    # 渲染设备列表页。
    window = MainWindow(MachineService(repository))
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    window.switch_page("devices")
    application.processEvents()
    page = window.page_stack.currentWidget()
    page.grab().save(str(OUTPUT_DIRECTORY / "devices-page.png"))
    print("表格行数:", page.table.rowCount())

    # 打开第一台设备的编辑弹窗并渲染。
    page.edit_device(page.devices[0]["id"])
    application.processEvents()
    page.editor.grab().save(str(OUTPUT_DIRECTORY / "devices-edit-dialog.png"))
    print("弹窗标题:", page.editor.windowTitle())
    print("回填名称:", page.field_inputs["machine_name"].text())
    print("回填序列号:", page.field_inputs["camera_serial"].text(), page.field_inputs["frequency_meter_serial"].text())
    print("回填备注:", page.remark_input.toPlainText())
    print("启用状态:", page.enabled_checkbox.isChecked())


if __name__ == "__main__":
    main()
