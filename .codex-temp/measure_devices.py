"""测量设备管理页各列的推荐宽度，用于评估列宽方案。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton

from ui.main_window import MainWindow

OUTPUT_DIRECTORY = Path(__file__).resolve().parent
COLUMN_NAMES = ("ID", "机器名称", "相机序列号", "频率仪序列号", "状态", "创建时间", "更新时间", "操作")


def report(table, size):
    """打印指定尺寸下各列的内容宽度与当前列宽。"""
    print(f"--- window {size} viewport={table.viewport().width()} ---")
    for column, name in enumerate(COLUMN_NAMES):
        print(f"  {column} {name}: 当前={table.columnWidth(column)} 内容建议={table.sizeHintForColumn(column)}")
    container = table.cellWidget(0, 7)
    print("  操作单元格大小:", container.sizeHint().width(), container.width())
    for button in container.findChildren(QPushButton):
        print("   按钮:", button.text(), button.sizeHint().width(), button.width())
    print("  状态单元格大小:", table.cellWidget(0, 4).sizeHint().width())


def main():
    """输出两种窗口尺寸下的列宽参考数据。"""
    application = QApplication(sys.argv)
    window = MainWindow()
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    window.switch_page("devices")
    QApplication.processEvents()
    page = window.page_stack.currentWidget()
    table = page.table

    for size in ((1600, 900), (1280, 720)):
        window.resize(*size)
        QApplication.processEvents()
        report(table, size)
        table.grab().save(str(OUTPUT_DIRECTORY / f"devices-table-{size[0]}x{size[1]}.png"))


if __name__ == "__main__":
    main()
