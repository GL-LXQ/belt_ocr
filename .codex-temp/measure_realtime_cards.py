"""测量实时监测页在 3 台和 4 台设备下的卡片几何。"""

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


def report(page, title: str):
    """打印卡片、网格列和滚动条的当前尺寸。"""
    print(f"--- {title} ---")
    print("设备数:", len(page.devices), "卡片数:", len(page.machine_cards))
    content = page.scroll_area.widget()
    print("页面内容:", content.width(), "x", content.height(), "可视:", page.scroll_area.viewport().width(), page.scroll_area.viewport().height())
    print("内容最小高度:", content.minimumSizeHint().height(), "高度策略:", content.sizePolicy().verticalPolicy())
    print("网格最小高度:", page.cards_layout.minimumSize().height())
    print("横向滚动条最大值:", page.scroll_area.horizontalScrollBar().maximum())
    print("纵向滚动条最大值:", page.scroll_area.verticalScrollBar().maximum())
    for index, card in enumerate(page.machine_cards):
        print(f"  卡片{index}: 宽={card.width()} 高={card.height()} sizeHint={card.sizeHint().height()} "
              f"最小提示={card.minimumSizeHint().height()} 策略={card.sizePolicy().verticalPolicy()}")


def card_heights(page):
    """返回卡片高度和页面内容高度。"""
    content = page.scroll_area.widget()
    return [card.height() for card in page.machine_cards], content.height()


def main():
    """分别测量三台设备和四台设备时的卡片尺寸。"""
    application = QApplication(sys.argv)

    database_path = OUTPUT_DIRECTORY / "realtime-layout.sqlite3"
    database_path.unlink(missing_ok=True)
    with closing(sqlite3.connect(database_path)) as connection, connection:
        MachineRepo.create_table(connection)
    repository = MachineRepo(database_path)
    for number in (1, 2, 3):
        repository.insert(f"{number}号皮带机", f"CAM00{number}", f"FREQ00{number}")

    window = MainWindow(MachineService(repository))
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.resize(1600, 900)
    window.show()
    window.switch_page("realtime")
    application.processEvents()
    page = window.page_stack.widget(0)
    content = page.scroll_area.widget()
    report(page, "三台设备 1600x900")

    # 新增第四台设备后逐个尝试让滚动区重新计算。
    repository.insert("4号皮带机", "CAM004", "FREQ004")
    page.reload_devices()
    application.processEvents()
    print("重建后:", card_heights(page))

    content.updateGeometry()
    application.processEvents()
    print("A updateGeometry:", card_heights(page))

    content.layout().invalidate()
    content.layout().activate()
    application.processEvents()
    print("B 布局 invalidate+activate:", card_heights(page))

    page.scroll_area.setWidgetResizable(False)
    page.scroll_area.setWidgetResizable(True)
    application.processEvents()
    print("C 重设 widgetResizable:", card_heights(page))

    content.layout().setSizeConstraint(content.layout().SizeConstraint.SetMinAndMaxSize)
    application.processEvents()
    print("D SetMinAndMaxSize:", card_heights(page))

    page.reload_devices()
    application.processEvents()
    print("E 带约束重载:", card_heights(page))


if __name__ == "__main__":
    main()
