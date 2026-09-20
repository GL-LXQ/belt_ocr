"""渲染设备管理页，用于检查列宽与对齐效果。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from ui.main_window import MainWindow

OUTPUT_DIRECTORY = Path(__file__).resolve().parent


def render(window: MainWindow, size, name: str):
    """按指定尺寸渲染窗口并保存截图和指标。"""
    window.resize(*size)
    window.switch_page("devices")
    QApplication.processEvents()
    page = window.page_stack.currentWidget()
    table = page.table
    print(f"--- {name} {size} ---")
    print("viewport:", table.viewport().width(), table.viewport().height())
    print("column widths:", [table.columnWidth(index) for index in range(table.columnCount())])
    print("hscroll range:", table.horizontalScrollBar().minimum(), table.horizontalScrollBar().maximum())
    for row in range(table.rowCount()):
        for column in (1, 2, 3, 5):
            item = table.item(row, column)
            hint = table.sizeHintForColumn(column)
            print(f"  row{row} col{column} text={item.text()!r} textwidth={hint}")
    window.grab().save(str(OUTPUT_DIRECTORY / f"devices-{name}.png"))
    table.grab().save(str(OUTPUT_DIRECTORY / f"devices-table-{name}.png"))


def main():
    """渲染两种窗口尺寸的设备管理页。"""
    application = QApplication(sys.argv)
    window = MainWindow()
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    render(window, (1600, 900), "1600x900")
    render(window, (1280, 720), "1280x720")


if __name__ == "__main__":
    main()
