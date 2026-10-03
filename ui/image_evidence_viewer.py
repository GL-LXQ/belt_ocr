"""按需展示同一次测量的只读证据图片。"""

from threading import Event
from typing import Callable

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QFrame,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    FluentIcon,
    MaskDialogBase,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    SubtitleLabel,
    TransparentPushButton,
    setCustomStyleSheet,
)

from src.controller.controller import AppController
from ui.image_evidence import (
    EVIDENCE_MESSAGES,
    REVIEW_CAPTIONS,
    EvidenceDecodedImage,
    EvidenceMeasurement,
    EvidenceReadFailure,
    EvidenceReadSignals,
    EvidenceReadTask,
    decode_evidence_image,
    read_measurement_evidence,
)
from ui.image_management_preview import PreviewMeasurement, draw_preview_image
from ui.pages.history_page import open_evidence_directory


class EvidenceImageCanvas(QGraphicsView):
    """等比例显示图片并提供缩放和平移。"""

    zoom_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        """建立只包含当前图片的图形场景。

        Args:
            parent: 所属查看器。

        Returns:
            None  # 图形场景和拖动行为已建立
        """
        super().__init__(parent)
        self.setObjectName("evidenceImageCanvas")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(200, 180)
        self.setAccessibleName("证据图片，可滚轮缩放并拖动平移")

        # 保留当前图片和适应窗口状态，不缓存其他测量图片。
        self.image_scene = QGraphicsScene(self)
        self.setScene(self.image_scene)
        self.image_item = self.image_scene.addPixmap(QPixmap())
        self.fit_mode = True

    def set_image(self, pixmap: QPixmap) -> None:
        """替换当前图片并恢复适应窗口。

        Args:
            pixmap: 当前内存图片，空图表示没有可显示内容。

        Returns:
            None  # 当前图片、场景边界和缩放已更新
        """
        self.image_item.setPixmap(pixmap)
        self.image_scene.setSceneRect(QRectF(pixmap.rect()))
        self.resetTransform()
        self.fit_mode = True
        self.fit_image()

    def fit_image(self) -> None:
        """将整张图片等比例放入当前视口。

        Args:
            无。

        Returns:
            None  # 完整图片可见并更新缩放提示
        """
        self.fit_mode = True
        if self.image_item.pixmap().isNull():
            return
        self.fitInView(self.image_item, Qt.AspectRatioMode.KeepAspectRatio)
        self.zoom_changed.emit(round(self.transform().m11() * 100))

    def set_zoom(self, scale: float) -> None:
        """以当前视口中心为基准设置图片比例。

        Args:
            scale: 目标缩放比例，1.0 表示 100%。

        Returns:
            None  # 比例限制在 10% 到 400%，空图不执行缩放
        """
        if self.image_item.pixmap().isNull():
            return
        center = self.mapToScene(self.viewport().rect().center())
        scale = min(4.0, max(0.1, scale))
        self.fit_mode = False
        self.resetTransform()
        self.scale(scale, scale)
        self.centerOn(center)
        self.zoom_changed.emit(round(scale * 100))

    def wheelEvent(self, event) -> None:
        """使用滚轮缩放当前图片。

        Args:
            event: Qt 滚轮事件。

        Returns:
            None  # 有垂直滚轮输入时更新缩放，否则沿用普通滚动
        """
        delta = event.angleDelta().y()
        if delta and not self.image_item.pixmap().isNull():
            factor = 1.2 if delta > 0 else 1 / 1.2
            self.set_zoom(self.transform().m11() * factor)
            event.accept()
            return
        super().wheelEvent(event)

    def resizeEvent(self, event) -> None:
        """在适应模式下随视口尺寸变化调整图片。

        Args:
            event: Qt 尺寸变化事件。

        Returns:
            None  # 适应模式继续显示整图，手动比例保持不变
        """
        super().resizeEvent(event)
        if self.fit_mode:
            self.fit_image()


class EvidenceViewer(MaskDialogBase):
    """左图右记录的证据查看器，原图只保留当前一张。"""

    record_requested = Signal(str)

    def __init__(self, parent: QWidget, stylesheet: str) -> None:
        """建立图片、缩略图条、测量信息和只读操作。

        Args:
            parent: 承载遮罩的主窗口或独立页面。
            stylesheet: 图片管理页面共用样式。

        Returns:
            None  # 查看器已建立但不显示
        """
        super().__init__(parent)
        self.setObjectName("evidenceViewer")
        self.widget.setObjectName("evidenceViewerContent")
        self.record: EvidenceMeasurement | PreviewMeasurement | None = None
        self.image_index = 0
        self.thumbnail_buttons: list[QToolButton] = []
        self.setWindowTitle("测量证据")

        # 工作线程按需建立，切换图片时只保留当前请求和可见缩略图。
        self.thread_pool: QThreadPool | None = None
        self.read_signals: EvidenceReadSignals | None = None
        self.read_generation = 0
        self.cancelled = Event()
        self.shutting_down = False
        self.loaded_image_index: int | None = None
        self.loaded_thumbnails: set[int] = set()
        self.thumbnail_timer = QTimer(self)
        self.thumbnail_timer.setSingleShot(True)
        self.thumbnail_timer.timeout.connect(self.load_visible_images)
        QApplication.instance().aboutToQuit.connect(self.shutdown)

        # 在遮罩卡片顶部显示标题、演示标识和关闭入口。
        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)
        heading = QHBoxLayout()
        heading.addWidget(SubtitleLabel("测量证据"))
        self.preview_badge = QLabel("演示数据 · 非真实记录")
        self.preview_badge.setObjectName("imagePreviewBadge")
        heading.addWidget(self.preview_badge)
        heading.addStretch()
        self.close_button = TransparentPushButton("关闭")
        self.close_button.setAccessibleName("关闭证据查看器")
        self.close_button.clicked.connect(self.close)
        heading.addWidget(self.close_button)
        layout.addLayout(heading)

        # 将图片区和独立滚动的信息栏并排放置。
        body = QHBoxLayout()
        body.setSpacing(20)
        body.addWidget(self.build_image_panel(), 1)
        body.addWidget(self.build_record_panel())
        layout.addLayout(body, 1)
        self.setStyleSheet(stylesheet)
        for widget in self.findChildren(QWidget):
            if isinstance(widget, (PushButton, TransparentPushButton, PlainTextEdit, SubtitleLabel, CaptionLabel)):
                setCustomStyleSheet(widget, stylesheet, stylesheet)
        self.hide()

    def build_image_panel(self) -> QWidget:
        """建立大图、异常占位、浏览工具栏和图片条。

        Args:
            无。

        Returns:
            QWidget()  # 左侧完整图片浏览区域
        """
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.image_stack = QStackedWidget()
        self.image_stack.setObjectName("evidenceImageStage")
        self.canvas = EvidenceImageCanvas()
        self.canvas.zoom_changed.connect(self.update_zoom_caption)
        self.image_stack.addWidget(self.canvas)

        # 损坏图片保留文件名和提示，不从本次列表中移除。
        placeholder = QWidget()
        placeholder.setObjectName("evidenceImagePlaceholder")
        placeholder_layout = QVBoxLayout(placeholder)
        placeholder_layout.setContentsMargins(28, 24, 28, 24)
        placeholder_layout.addStretch()
        self.image_error_title = QLabel()
        self.image_error_title.setObjectName("imageEmptyTitle")
        self.image_error_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_error_body = QLabel()
        self.image_error_body.setObjectName("imageMutedText")
        self.image_error_body.setWordWrap(True)
        self.image_error_body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder_layout.addWidget(self.image_error_title)
        placeholder_layout.addWidget(self.image_error_body)
        placeholder_layout.addStretch()
        self.image_stack.addWidget(placeholder)
        layout.addWidget(self.image_stack, 1)

        # 在大图下方显示完整文件名提示和当前张数。
        file_row = QHBoxLayout()
        self.filename_label = QLabel()
        self.filename_label.setObjectName("evidenceFilename")
        self.filename_label.setTextFormat(Qt.TextFormat.PlainText)
        self.filename_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.filename_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.image_position_label = QLabel()
        self.image_position_label.setObjectName("imageMutedText")
        file_row.addWidget(self.filename_label, 1)
        file_row.addWidget(self.image_position_label)
        layout.addLayout(file_row)

        # 提供有边界的前后翻阅和比例控制。
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        self.previous_image_button = TransparentPushButton("‹ 上一张")
        self.next_image_button = TransparentPushButton("下一张 ›")
        self.previous_image_button.clicked.connect(lambda: self.select_image(self.image_index - 1))
        self.next_image_button.clicked.connect(lambda: self.select_image(self.image_index + 1))
        self.zoom_out_button = PushButton("−")
        self.zoom_in_button = PushButton("+")
        self.zoom_out_button.setAccessibleName("缩小图片")
        self.zoom_in_button.setAccessibleName("放大图片")
        self.zoom_out_button.setFixedWidth(34)
        self.zoom_in_button.setFixedWidth(34)
        self.zoom_out_button.clicked.connect(lambda: self.canvas.set_zoom(self.canvas.transform().m11() / 1.2))
        self.zoom_in_button.clicked.connect(lambda: self.canvas.set_zoom(self.canvas.transform().m11() * 1.2))
        self.zoom_label = QLabel("--")
        self.zoom_label.setObjectName("imageMutedText")
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.zoom_label.setFixedWidth(52)
        self.fit_button = PushButton("适应窗口")
        self.actual_size_button = PushButton("100%")
        self.fit_button.clicked.connect(self.canvas.fit_image)
        self.actual_size_button.clicked.connect(lambda: self.canvas.set_zoom(1.0))
        toolbar.addWidget(self.previous_image_button)
        toolbar.addWidget(self.next_image_button)
        toolbar.addStretch()
        zoom_widgets = (
            self.zoom_out_button, self.zoom_label, self.zoom_in_button, self.fit_button, self.actual_size_button
        )
        for widget in zoom_widgets:
            toolbar.addWidget(widget)
        layout.addLayout(toolbar)

        # 每次打开测量时只建立该组的横向缩略图。
        self.thumbnail_scroll = QScrollArea()
        self.thumbnail_scroll.setObjectName("evidenceThumbnailScroll")
        self.thumbnail_scroll.setWidgetResizable(True)
        self.thumbnail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.thumbnail_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.thumbnail_scroll.setFixedHeight(100)
        self.thumbnail_body = QWidget()
        self.thumbnail_layout = QHBoxLayout(self.thumbnail_body)
        self.thumbnail_layout.setContentsMargins(0, 2, 0, 8)
        self.thumbnail_layout.setSpacing(8)
        self.thumbnail_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.thumbnail_scroll.setWidget(self.thumbnail_body)
        self.thumbnail_scroll.horizontalScrollBar().valueChanged.connect(lambda: self.thumbnail_timer.start(0))
        self.thumbnail_scroll.viewport().installEventFilter(self)
        self.thumbnail_group = QButtonGroup(self)
        self.thumbnail_group.setExclusive(True)
        layout.addWidget(self.thumbnail_scroll)
        return panel

    def build_record_panel(self) -> QScrollArea:
        """建立完整文字、测量摘要、折叠信息和记录操作。

        Args:
            无。

        Returns:
            QScrollArea()  # 右侧可独立滚动的只读测量栏
        """
        scroll = QScrollArea()
        scroll.setObjectName("evidenceRecordScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFixedWidth(300)
        body = QWidget()
        body.setObjectName("evidenceRecordBody")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(12)

        # 摘要只使用测量层级的信息，不伪造逐图采集时间。
        self.machine_label = QLabel()
        self.machine_label.setObjectName("evidenceMachineName")
        self.machine_label.setWordWrap(True)
        self.status_badge = QLabel()
        self.status_badge.setObjectName("imageReviewBadge")
        self.finished_at_label = QLabel()
        self.finished_at_label.setObjectName("imageMutedText")
        self.frequency_label = QLabel()
        self.frequency_label.setObjectName("imageMutedText")
        layout.addWidget(self.machine_label)
        layout.addWidget(self.status_badge, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.finished_at_label)
        layout.addWidget(self.frequency_label)

        # 完整文字独立滚动并允许选择复制。
        self.text_source_label = QLabel()
        self.text_source_label.setObjectName("evidenceSectionTitle")
        self.full_text_edit = PlainTextEdit()
        self.full_text_edit.setObjectName("evidenceFullText")
        self.full_text_edit.setReadOnly(True)
        self.full_text_edit.setAccessibleName("本次测量的完整有效文字")
        self.full_text_edit.setMinimumHeight(134)
        self.full_text_edit.setMaximumHeight(180)
        layout.addWidget(self.text_source_label)
        layout.addWidget(self.full_text_edit)
        self.copy_text_button = PushButton("复制文字")
        self.copy_text_button.clicked.connect(self.copy_effective_text)
        layout.addWidget(self.copy_text_button)

        # 默认折叠 Session 和目录路径，避免挤占主要信息。
        self.metadata_button = TransparentPushButton("展开记录信息 ▾")
        self.metadata_button.setCheckable(True)
        self.metadata_button.toggled.connect(self.toggle_metadata)
        layout.addWidget(self.metadata_button)
        self.metadata_panel = QWidget()
        metadata_layout = QVBoxLayout(self.metadata_panel)
        metadata_layout.setContentsMargins(0, 0, 0, 0)
        metadata_layout.setSpacing(6)
        metadata_layout.addWidget(QLabel("Session ID"))
        self.session_label = QLabel()
        self.path_label = QLabel()
        for label in (self.session_label, self.path_label):
            label.setObjectName("imageMutedText")
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        metadata_layout.addWidget(self.session_label)
        self.path_title = QLabel("证据目录")
        metadata_layout.addWidget(self.path_title)
        metadata_layout.addWidget(self.path_label)
        self.copy_path_button = PushButton("复制目录路径")
        self.copy_path_button.clicked.connect(self.copy_directory_path)
        metadata_layout.addWidget(self.copy_path_button)
        self.metadata_panel.hide()
        layout.addWidget(self.metadata_panel)
        layout.addStretch()

        # 真实记录可跳转历史详情，演示保持只读隔离。
        self.record_notice = QLabel()
        self.record_notice.setObjectName("imagePreviewNotice")
        self.record_notice.setWordWrap(True)
        layout.addWidget(self.record_notice)
        self.view_record_button = PrimaryPushButton("查看测量记录")
        self.open_directory_button = PushButton("打开证据文件夹")
        self.view_record_button.clicked.connect(self.request_record_detail)
        self.open_directory_button.clicked.connect(self.open_stored_directory)
        for button in (self.view_record_button, self.open_directory_button):
            button.setEnabled(False)
            button.setToolTip("读取真实测量详情后可用；演示没有对应业务记录或文件夹。")
            layout.addWidget(button)
        scroll.setWidget(body)
        return scroll

    def open_record(self, record: EvidenceMeasurement | PreviewMeasurement) -> None:
        """填充当前测量，并按需读取这一组的图片。

        Args:
            record: 最新真实详情与目录列表，或明确开启的内存演示。

        Returns:
            None  # 摘要、文字、缩略图和首张图片已显示
        """
        # 更新测量摘要和完整有效文字。
        self.clear_record()
        self.record = record
        preview = isinstance(record, PreviewMeasurement)
        self.preview_badge.setVisible(preview)
        self.machine_label.setText(record.machine_name)
        self.status_badge.setText(REVIEW_CAPTIONS[record.review_status])
        self.status_badge.setProperty("reviewStatus", record.review_status)
        self.status_badge.style().unpolish(self.status_badge)
        self.status_badge.style().polish(self.status_badge)
        self.finished_at_label.setText(f"测量完成  {record.finished_at:%Y-%m-%d %H:%M:%S}")
        frequency_text = "--" if record.frequency is None else f"{record.frequency:.1f} Hz"
        self.frequency_label.setText(f"最终频率  {frequency_text}")
        source = "人工最终结果" if record.reviewed_lines is not None else "OCR 识别结果"
        self.text_source_label.setText(f"完整文字 · {source}")
        self.full_text_edit.setPlainText("\n".join(record.effective_lines) or "暂无识别文字")
        self.copy_text_button.setEnabled(bool(record.effective_lines))
        self.copy_text_button.setText("复制文字")

        # 每次打开都折叠定位信息，真实路径沿用详情原值。
        self.session_label.setText(record.session_id)
        directory = f"DEMO / {record.machine_id} / {record.session_id}" if preview else record.evidence_directory
        self.path_label.setText(directory)
        self.path_title.setText("演示路径（未创建）" if preview else "证据目录")
        self.copy_path_button.setEnabled(not preview and bool(directory))
        self.metadata_button.setChecked(False)
        self.metadata_panel.hide()
        action = "前往复核" if record.review_status == "pending" else "查看测量记录"
        self.view_record_button.setText(f"{action} · 待接入" if preview else action)
        self.view_record_button.setEnabled(not preview)
        self.open_directory_button.setText("打开证据文件夹 · 待接入" if preview else "打开证据文件夹")
        self.open_directory_button.setEnabled(not preview and bool(directory))
        self.view_record_button.setToolTip("演示没有对应业务记录" if preview else "在历史记录中查看详情和复核")
        self.open_directory_button.setToolTip("演示没有对应文件夹" if preview else directory)
        self.record_notice.setText(
            "演示记录与占位图仅存于内存。" if preview else "现存数量来自本次目录读取，不表示原始证据完整。"
        )

        # 只生成当前测量的图片条。
        for image_index, image in enumerate(record.images):
            button = QToolButton()
            button.setObjectName("evidenceThumbnailButton")
            button.setFixedSize(104, 78)
            button.setCheckable(True)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            button.setIconSize(QSize(90, 48))
            readable = preview and image.readable
            button.setIcon(QIcon(draw_preview_image(image)) if readable else FluentIcon.PHOTO.icon())
            damaged = preview and not readable
            button.setText(f"{image_index + 1:02d} · 损坏" if damaged else f"{image_index + 1:02d}")
            button.setToolTip(image.filename)
            button.setAccessibleName(f"第 {image_index + 1} 张：{image.filename}")
            button.clicked.connect(lambda checked=False, index=image_index: self.select_image(index))
            self.thumbnail_group.addButton(button)
            self.thumbnail_layout.addWidget(button)
            self.thumbnail_buttons.append(button)
        self.thumbnail_scroll.setVisible(bool(record.images))

        # 根据当前主窗口尺寸展开大幅查看器。
        parent = self.parentWidget()
        self.widget.setFixedSize(min(1260, parent.width() - 64), min(790, parent.height() - 64))
        self.select_image(0)
        self.show()
        self.close_button.setFocus()
        QTimer.singleShot(0, self.canvas.fit_image)
        if not preview:
            self.thumbnail_timer.start(0)

    def select_image(self, image_index: int) -> None:
        """切换当前组内图片并保留无法解码文件的位置。

        Args:
            image_index: 当前测量图片下标。

        Returns:
            None  # 有效下标显示对应图片，越界请求不改变当前图片
        """
        record = self.record
        if record is None:
            return
        if record.images and not 0 <= image_index < len(record.images):
            return
        self.image_index = image_index
        image = record.images[image_index] if record.images else None
        preview = isinstance(record, PreviewMeasurement)
        readable = image is not None and preview and image.readable

        # 同步文件位置和导航边界。
        self.filename_label.setText(image.filename if image else "尚无可读取的图片列表")
        self.filename_label.setToolTip(self.filename_label.text())
        position = f"第 {image_index + 1} / {len(record.images)} 张" if image else record.image_count_caption
        self.image_position_label.setText(position)
        self.previous_image_button.setEnabled(bool(image) and image_index > 0)
        self.next_image_button.setEnabled(bool(image) and image_index < len(record.images) - 1)
        for button in (self.zoom_out_button, self.zoom_in_button, self.fit_button, self.actual_size_button):
            button.setEnabled(readable)

        # 可读图片完整适应窗口，异常图片显示明确原因。
        if image is not None and not preview:
            self.loaded_image_index = None
            self.canvas.set_image(QPixmap())
            self.image_error_title.setText("正在读取图片")
            self.image_error_body.setText(image.filename)
            self.image_stack.setCurrentIndex(1)
            self.zoom_label.setText("--")
        elif readable:
            self.image_stack.setCurrentIndex(0)
            self.canvas.set_image(draw_preview_image(image))
        else:
            state = "corrupt" if image is not None else record.evidence_state
            title, description = EVIDENCE_MESSAGES[state]
            self.canvas.set_image(QPixmap())
            self.image_error_title.setText(title)
            self.image_error_body.setText(f"{image.filename}\n{description}" if image else description)
            self.image_stack.setCurrentIndex(1)
            self.zoom_label.setText("--")

        # 将当前缩略图滚动到可见位置。
        if image:
            button = self.thumbnail_buttons[image_index]
            button.setChecked(True)
            self.thumbnail_scroll.ensureWidgetVisible(button)
        if not preview:
            self.cancel_reads()
            self.thumbnail_timer.start(0)

    def open_session(self, controller: AppController, session_id: str) -> None:
        """在后台重新读取点击的测量详情和该组目录列表。

        Args:
            controller: 现有测量 Controller。
            session_id: 卡片对应的测量编号。

        Returns:
            None  # 查看器立即显示读取提示，旧记录结果不会覆盖新选择
        """
        self.clear_record()
        self.preview_badge.hide()
        self.session_label.setText(session_id)
        self.image_error_title.setText("正在读取测量证据")
        self.image_error_body.setText("正在读取最新记录和该记录保存的图片列表。")
        self.record_notice.setText("图片按当前测量归组，不合并文字相同的其他测量。")

        # 先显示占位，再提交本次详情读取。
        parent = self.parentWidget()
        self.widget.setFixedSize(min(1260, parent.width() - 64), min(790, parent.height() - 64))
        self.show()
        self.close_button.setFocus()
        cancelled = self.cancelled
        self.start_read("group", lambda: read_measurement_evidence(controller, session_id, cancelled))

    def start_read(self, identity: object, read: Callable[[], object]) -> None:
        """将当前查看器的一次读取交给最多两个工作线程。

        Args:
            identity: group 或当前图片种类及下标。
            read: 本次有限的详情、目录或图片读取。

        Returns:
            None  # 任务已排入当前组的有限队列，关闭后不再提交
        """
        if self.shutting_down:
            return
        if self.thread_pool is None:
            self.thread_pool = QThreadPool(QApplication.instance())
            self.thread_pool.setMaxThreadCount(2)
            self.read_signals = EvidenceReadSignals(self.thread_pool)
            self.read_signals.completed.connect(self.accept_read, Qt.ConnectionType.QueuedConnection)

        # 队列只包含当前大图和当前视口中的少量缩略图。
        task = EvidenceReadTask(self.read_generation, identity, read, self.cancelled, self.read_signals)
        self.thread_pool.start(task)

    def cancel_reads(self) -> None:
        """取消旧代次，清空尚未运行的查看器任务。

        Args:
            无。

        Returns:
            None  # 正在读取的有限任务完成后丢弃结果，未运行任务已移除
        """
        self.cancelled.set()
        self.read_generation += 1
        self.cancelled = Event()
        if self.thread_pool is not None:
            self.thread_pool.clear()

    def load_visible_images(self) -> None:
        """只加载当前原图和缩略图条视口中的图片。

        Args:
            无。

        Returns:
            None  # 视口外图标已释放，队列最多包含当前原图和十二张缩略图
        """
        record = self.record
        if not self.isVisible() or not isinstance(record, EvidenceMeasurement):
            return
        self.cancel_reads()
        if not record.images:
            return

        # 根据当前滚动位置找出少量可见缩略图。
        left = self.thumbnail_scroll.horizontalScrollBar().value()
        right = left + self.thumbnail_scroll.viewport().width()
        visible_indices = [
            index for index, button in enumerate(self.thumbnail_buttons)
            if button.x() < right and button.x() + button.width() > left
        ][:12]
        for index in self.loaded_thumbnails - set(visible_indices):
            button = self.thumbnail_buttons[index]
            button.setIcon(FluentIcon.PHOTO.icon())
            button.setText(f"{index + 1:02d}")
        self.loaded_thumbnails.intersection_update(visible_indices)

        # 当前原图优先排入队列，不预读相邻原图。
        if self.loaded_image_index != self.image_index:
            image = record.images[self.image_index]
            self.start_read(("image", self.image_index), lambda path=image.path: decode_evidence_image(path))
        for index in visible_indices:
            if index not in self.loaded_thumbnails:
                path = record.images[index].path
                self.start_read(
                    ("thumbnail", index),
                    lambda image_path=path: decode_evidence_image(image_path, QSize(90, 48)),
                )

    @Slot(int, object, object)
    def accept_read(self, generation: int, identity: object, result: object) -> None:
        """只在主线程接收当前组的结果并建立 QPixmap。

        Args:
            generation: 结果所属的查看器代次。
            identity: group 或图片种类及下标。
            result: 最新详情、后台 QImage 或读取错误。

        Returns:
            None  # 当前组展示已更新，旧代次和已关闭查看器的结果被忽略
        """
        if generation != self.read_generation or not self.isVisible() or self.shutting_down:
            return
        if identity == "group":
            if isinstance(result, EvidenceMeasurement):
                self.open_record(result)
            else:
                self.image_error_title.setText("测量证据读取失败")
                message = result.message if isinstance(result, EvidenceReadFailure) else "该测量记录已不存在。"
                self.image_error_body.setText(message)
            return

        # 大图只更新当前选择，缩略图只更新当前视口对应按钮。
        kind, index = identity
        if not isinstance(self.record, EvidenceMeasurement):
            return
        if isinstance(result, EvidenceReadFailure):
            result = EvidenceDecodedImage(QImage(), "corrupt")
        pixmap = QPixmap.fromImage(result.image)
        if kind == "thumbnail":
            button = self.thumbnail_buttons[index]
            button.setIcon(QIcon(pixmap) if not pixmap.isNull() else FluentIcon.PHOTO.icon())
            button.setText(f"{index + 1:02d}" if not pixmap.isNull() else f"{index + 1:02d} · 不可读")
            self.loaded_thumbnails.add(index)
            return

        # 释放上一张原图，失败时保留实际文件名和组内位置。
        self.loaded_image_index = index
        readable = not pixmap.isNull()
        for button in (self.zoom_out_button, self.zoom_in_button, self.fit_button, self.actual_size_button):
            button.setEnabled(readable)
        self.canvas.set_image(pixmap)
        self.image_stack.setCurrentIndex(0 if readable else 1)
        if not readable:
            title, description = EVIDENCE_MESSAGES[result.state]
            self.image_error_title.setText(title)
            self.image_error_body.setText(f"{self.record.images[index].filename}\n{description}")
            self.zoom_label.setText("--")

    def request_record_detail(self) -> None:
        """将真实测量编号交回主窗口的现有历史详情入口。

        Args:
            无。

        Returns:
            None  # 已发出当前测量的跳转请求，演示不跳转
        """
        if isinstance(self.record, EvidenceMeasurement):
            self.record_requested.emit(self.record.session_id)

    def open_stored_directory(self) -> None:
        """复用历史记录的文件夹打开提示。

        Args:
            无。

        Returns:
            None  # 系统收到保存目录的打开请求，失败时显示原因和路径
        """
        if isinstance(self.record, EvidenceMeasurement):
            open_evidence_directory(self.record.evidence_directory, self.widget, self.record.evidence_state)

    def copy_directory_path(self) -> None:
        """复制真实记录保存的目录，便于打开失败时手动定位。

        Args:
            无。

        Returns:
            None  # 原样目录已复制，演示不提供业务目录
        """
        if isinstance(self.record, EvidenceMeasurement):
            QApplication.clipboard().setText(self.record.evidence_directory)

    def eventFilter(self, watched, event) -> bool:
        """在缩略图视口尺寸变化后重新安排可见图读取。

        Args:
            watched: 当前接收事件的控件。
            event: Qt 事件。

        Returns:
            False  # 尺寸事件继续交给原控件处理
        """
        scroll = getattr(self, "thumbnail_scroll", None)
        if scroll is not None and watched is scroll.viewport() and event.type() == QEvent.Type.Resize:
            self.thumbnail_timer.start(0)
        return super().eventFilter(watched, event)

    def shutdown(self) -> None:
        """关闭时清除未运行任务并等待正在读取的有限任务结束。

        Args:
            无。

        Returns:
            None  # 查看器线程池已结束，不留下运行中的 QThread
        """
        self.shutting_down = True
        self.clear_record()
        if self.thread_pool is not None:
            self.thread_pool.waitForDone()
            self.thread_pool.deleteLater()
            self.thread_pool = None

    def update_zoom_caption(self, percentage: int) -> None:
        """显示当前图片比例并更新缩放边界。

        Args:
            percentage: 当前图片的整数百分比。

        Returns:
            None  # 比例提示与放大缩小按钮已更新
        """
        self.zoom_label.setText(f"{percentage}%")
        self.zoom_label.setToolTip("适应窗口" if self.canvas.fit_mode else "手动缩放，可拖动查看")
        self.zoom_out_button.setEnabled(percentage > 10)
        self.zoom_in_button.setEnabled(percentage < 400)

    def toggle_metadata(self, expanded: bool) -> None:
        """切换 Session 和目录路径的信息区。

        Args:
            expanded: 是否展开定位信息。

        Returns:
            None  # 信息区和按钮文案同步
        """
        self.metadata_panel.setVisible(expanded)
        self.metadata_button.setText("收起记录信息 ▴" if expanded else "展开记录信息 ▾")

    def copy_effective_text(self) -> None:
        """复制本次测量的完整有效文字。

        Args:
            无。

        Returns:
            None  # 非空文字已复制到本机剪贴板
        """
        if self.record is not None and self.record.effective_lines:
            QApplication.clipboard().setText("\n".join(self.record.effective_lines))
            self.copy_text_button.setText("已复制")

    def done(self, code: int) -> None:
        """立即关闭查看器，避免旧关闭动画影响再次打开。

        Args:
            code: Qt 对话框返回码。

        Returns:
            None  # 清空当前测量后同步关闭，不留下延迟关闭任务
        """
        self.clear_record()
        QDialog.done(self, code)

    def clear_record(self) -> None:
        """释放当前测量的内存图片和文字展示状态。

        Args:
            无。

        Returns:
            None  # 记录、画布、缩略图与详情字段已清空
        """
        # 取消当前组读取并释放有限缩略图缓存。
        self.thumbnail_timer.stop()
        self.cancel_reads()
        self.loaded_image_index = None
        self.loaded_thumbnails.clear()

        # 清空测量引用和当前画布。
        self.record = None
        self.image_index = 0
        self.canvas.set_image(QPixmap())
        self.image_stack.setCurrentIndex(1)
        self.zoom_label.setText("--")

        # 移除本次测量的缩略图及按钮组引用。
        for button in self.thumbnail_buttons:
            self.thumbnail_group.removeButton(button)
            self.thumbnail_layout.removeWidget(button)
            button.hide()
            button.deleteLater()
        self.thumbnail_buttons = []
        self.thumbnail_scroll.hide()

        # 清空文字和定位信息，收起元数据区域。
        for label in (
            self.filename_label, self.image_position_label, self.image_error_title, self.image_error_body,
            self.machine_label, self.status_badge, self.finished_at_label, self.frequency_label,
            self.text_source_label, self.session_label, self.path_label,
        ):
            label.clear()
            label.setToolTip("")
        self.full_text_edit.clear()
        self.metadata_button.setChecked(False)
        self.metadata_panel.hide()

        # 没有当前图片时禁止导航、缩放和复制。
        for button in (
            self.previous_image_button, self.next_image_button, self.zoom_out_button, self.zoom_in_button,
            self.fit_button, self.actual_size_button, self.copy_text_button, self.copy_path_button,
            self.view_record_button, self.open_directory_button,
        ):
            button.setEnabled(False)

    def hideEvent(self, event) -> None:
        """隐藏后释放当前图片并使所有未完成结果失效。

        Args:
            event: Qt 隐藏事件。

        Returns:
            None  # 当前测量和后台任务已清理
        """
        self.clear_record()
        super().hideEvent(event)
