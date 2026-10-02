"""在同一日历中编辑开始日期和结束日期。"""

from PySide6.QtCore import QDate
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import SegmentedWidget
from qfluentwidgets.components.date_time.fast_calendar_view import FastCalendarView


class InlineCalendarView(FastCalendarView):
    """选择日期后保持显示的内嵌快速日历。"""

    def _onDayItemClicked(self, selected_date: QDate) -> None:
        """更新选中日期，不关闭日历和外层弹窗。

        Args:
            selected_date: 本次点击的日期。

        Returns:
            返回示例：
                None  # 日历保持显示，日期变化时通知草稿编辑器
        """
        if selected_date != self.date:
            self.date = selected_date
            self.dateChanged.emit(selected_date)


class DateRangePicker(QWidget):
    """保存日期范围草稿，并共用一个内嵌日历。"""

    def __init__(self, start_date: QDate, end_date: QDate, parent: QWidget | None = None) -> None:
        """建立开始和结束日期入口及内嵌日历。

        Args:
            start_date: 打开时的开始日期。
            end_date: 打开时的结束日期。
            parent: 所属筛选弹层。

        Returns:
            返回示例：
                None  # 日期草稿已建立，默认编辑开始日期
        """
        super().__init__(parent)
        self.setObjectName("historyDateRange")
        self.start_date = start_date
        self.end_date = end_date
        self.active_endpoint = "start"

        # 在日历上方显示开始日期和结束日期。
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.endpoint_tabs = SegmentedWidget(self)
        self.endpoint_tabs.setFixedHeight(40)
        self.start_button = self.endpoint_tabs.addItem("start", "", onClick=lambda: self.select_endpoint("start"))
        self.end_button = self.endpoint_tabs.addItem("end", "", onClick=lambda: self.select_endpoint("end"))
        self.start_button.setAccessibleName("开始日期")
        self.end_button.setAccessibleName("结束日期")
        self.update_endpoint_labels()
        layout.addWidget(self.endpoint_tabs)

        # 年、月、日视图都保留在同一控件内。
        self.calendar = InlineCalendarView(self)
        self.calendar.setResetEnabled(False)
        self.calendar.dateChanged.connect(self.update_selected_date)
        layout.addWidget(self.calendar)
        self.select_endpoint("start")

    def select_endpoint(self, endpoint: str) -> None:
        """切换正在编辑的日期并恢复对应日历页。

        Args:
            endpoint: start 表示开始日期，end 表示结束日期。

        Returns:
            返回示例：
                None  # 当前入口和日历选中日期已同步
        """
        self.active_endpoint = endpoint
        self.endpoint_tabs.setCurrentItem(endpoint)
        selected_date = self.start_date if endpoint == "start" else self.end_date
        self.calendar.stackedWidget.setCurrentWidget(self.calendar.dayView)
        self.calendar.setDate(selected_date)

    def update_selected_date(self, selected_date: QDate) -> None:
        """更新当前草稿日期并同步交叉的日期边界。

        Args:
            selected_date: 用户在内嵌日历中选中的日期。

        Returns:
            返回示例：
                None  # 草稿满足开始日期不晚于结束日期，未应用查询条件
        """
        # 修改当前端点，交叉时将另一端点同步为同一天。
        if self.active_endpoint == "start":
            self.start_date = selected_date
            if self.start_date > self.end_date:
                self.end_date = selected_date
        else:
            self.end_date = selected_date
            if self.end_date < self.start_date:
                self.start_date = selected_date

        # 在两个入口中显示最新草稿日期。
        self.update_endpoint_labels()

    def update_endpoint_labels(self) -> None:
        """刷新两个日期入口中的草稿文字。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 开始和结束入口显示各自的草稿日期
        """
        self.start_button.setText(f"开始 {self.start_date.toString('yyyy-MM-dd')}")
        self.end_button.setText(f"结束 {self.end_date.toString('yyyy-MM-dd')}")
