"""渲染设备管理页的现有布局与两个候选列宽方案。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QHeaderView

from ui.main_window import MainWindow

OUTPUT_DIRECTORY = Path(__file__).resolve().parent

# 候选一：每列最小宽度与剩余空间的分配权重。
MINIMUM_WIDTHS = (48, 120, 148, 128, 96, 172, 172, 128)
WEIGHTS = (0.0, 0.34, 0.10, 0.10, 0.06, 0.14, 0.14, 0.12)


def make_window(application):
    """创建不显示到屏幕的设备管理页窗口。"""
    window = MainWindow()
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    window.switch_page("devices")
    QApplication.processEvents()
    return window


def widths_for_proportional(viewport_width: int):
    """按最小宽度和权重计算各列宽度。"""
    extra_width = max(0, viewport_width - sum(MINIMUM_WIDTHS))
    return [minimum + round(extra_width * weight) for minimum, weight in zip(MINIMUM_WIDTHS, WEIGHTS)]


def main():
    """渲染现有布局与两个候选方案的表格截图。"""
    application = QApplication(sys.argv)
    window = make_window(application)
    table = window.page_stack.currentWidget().table

    for size in ((1600, 900), (1280, 720)):
        window.resize(*size)
        QApplication.processEvents()
        viewport_width = table.viewport().width()

        # 现有布局：固定列宽 + 最后一列拉伸。
        current = [table.columnWidth(column) for column in range(table.columnCount())]
        print(f"=== {size} viewport={viewport_width} ===")
        print("现有:", current)
        table.grab().save(str(OUTPUT_DIRECTORY / f"candidate-current-{size[0]}.png"))

        # 候选一：最小宽度 + 按权重分配剩余空间。
        proportional = widths_for_proportional(viewport_width)
        for column, width in enumerate(proportional):
            table.setColumnWidth(column, width)
        QApplication.processEvents()
        print("候选一（比例分配）:", proportional, "合计", sum(proportional))
        table.grab().save(str(OUTPUT_DIRECTORY / f"candidate-proportional-{size[0]}.png"))

        # 候选二：机器名称与两个时间列等分剩余空间。
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        for column, width in ((0, 56), (1, 120), (2, 150), (3, 130), (4, 108), (7, 140)):
            table.setColumnWidth(column, width)
        for column in (1, 5, 6):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        QApplication.processEvents()
        stretch = [table.columnWidth(column) for column in range(table.columnCount())]
        print("候选二（等分拉伸）:", stretch, "合计", sum(stretch))
        table.grab().save(str(OUTPUT_DIRECTORY / f"candidate-stretch-{size[0]}.png"))

        # 恢复固定模式，避免影响下一轮尺寸统计。
        for column in range(table.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)


if __name__ == "__main__":
    main()
