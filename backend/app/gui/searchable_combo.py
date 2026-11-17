"""可搜索下拉框：用于几百条班次代码等长列表。"""

from typing import Any, Iterable

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import QComboBox, QWidget


class SearchableComboBox(QComboBox):
    """可输入关键字过滤的下拉框。

    - 输入关键字时按「包含」匹配过滤选项，并保持弹出列表打开；
    - ``pinned`` 选项（如「休息」「保留当前自定义时间」）在过滤时始终保留；
    - 清空关键字或失去焦点时恢复完整列表与上一次的有效选择。
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        placeholder: str = "输入关键字搜索…",
    ) -> None:
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.lineEdit().setPlaceholderText(placeholder)
        self.lineEdit().installEventFilter(self)
        self._items: list[tuple[str, object, bool]] = []
        self._keyword = ""
        self._last_data: object = None
        self.lineEdit().textEdited.connect(self._apply_filter)
        self.lineEdit().editingFinished.connect(self._restore_selection)
        self.activated.connect(self._remember)
        self.currentIndexChanged.connect(self._remember)

    # ---------------------------------------------------------------- 数据
    def set_items(self, items: Iterable[tuple[str, object, bool]]) -> None:
        """整体设置选项；每项为 (显示文本, 数据, 是否在过滤时保留)。"""
        self._items = [(str(text), data, bool(pinned)) for text, data, pinned in items]
        self._keyword = ""
        self._rebuild()

    def append_item(self, text: str, data: Any = None, pinned: bool = False) -> None:
        self._items.append((str(text), data, bool(pinned)))
        self._rebuild()

    def currentData(self, role: int = Qt.UserRole):  # noqa: N802 - 覆盖 Qt 命名
        # 用户输入关键字但尚未点选时，退回上一次有效选择，避免保存为空值
        if self.currentIndex() < 0:
            return self._last_data
        return super().currentData(role)

    # ---------------------------------------------------------------- 内部
    def eventFilter(self, obj, event):  # noqa: N802 - 覆盖 Qt 命名
        # 进入输入框时全选已有内容，直接输入即可替换为搜索词
        if obj is self.lineEdit() and event.type() == QEvent.FocusIn:
            QTimer.singleShot(0, self.lineEdit().selectAll)
        return super().eventFilter(obj, event)

    def _apply_filter(self, text: str) -> None:
        self._keyword = text
        self._rebuild()

    def _rebuild(self) -> None:
        keyword = self._keyword.strip().lower()
        self.blockSignals(True)
        self.clear()
        for text, data, pinned in self._items:
            if pinned or not keyword or keyword in text.lower():
                self.addItem(text, data)
        if keyword:
            self.setCurrentIndex(-1)
            self.setEditText(self._keyword)
        else:
            index = self.findData(self._last_data)
            if index < 0 and self.count():
                index = 0
            self.setCurrentIndex(index)
            if index >= 0:
                self._last_data = self.itemData(index)
        self.blockSignals(False)
        if keyword and self.isVisible():
            self.showPopup()

    def _remember(self, index: int) -> None:
        if 0 <= index < self.count():
            self._last_data = self.itemData(index)
            # 选中有效项后退出过滤状态，恢复完整列表供下次展开
            if self._keyword:
                self._keyword = ""
                # 延后重建，避免在弹出列表激活过程中重置模型
                QTimer.singleShot(0, self._rebuild)

    def _restore_selection(self) -> None:
        if self.currentIndex() >= 0 or not self._items:
            return
        self._keyword = ""
        self._rebuild()
