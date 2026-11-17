"""月份选择弹窗，替代 Qt 原生日历。

排班是按月进行的，选到具体日期没有意义，因此这里只提供按年切换的月份网格，
并使用主题色高亮当前选中月份。
"""

from PySide6.QtCore import QDate, QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import theme

_NAV_BTN_STYLE = f"""
QPushButton {{
    background: transparent;
    border: none;
    color: {theme.TEXT_SECONDARY};
    font-size: 18px;
    font-weight: 600;
    padding: 0;
}}
QPushButton:hover {{
    color: {theme.PRIMARY};
}}
"""

_TITLE_STYLE = (
    f"font-size: 14px; font-weight: 600; color: {theme.TEXT};"
    "background: transparent; border: none;"
)

_DAY_TEMPLATE = """
QPushButton {{
    background: {bg};
    color: {fg};
    border: {border};
    border-radius: 6px;
    padding: 0;
    font-size: 13px;
}}
QPushButton:hover {{
    background: {hover_bg};
    color: {hover_fg};
    border: 1px solid {hover_border};
}}
"""


def _day_style(
    bg: str,
    fg: str,
    border: str,
    hover_bg: str = theme.PRIMARY_SOFT,
    hover_fg: str = theme.PRIMARY,
) -> str:
    return _DAY_TEMPLATE.format(
        bg=bg,
        fg=fg,
        border=border,
        hover_bg=hover_bg,
        hover_fg=hover_fg,
        hover_border=theme.PRIMARY,
    )


class MonthPopup(QFrame):
    """只选择月份的视图：按年切换，点击月份发出 month_selected 信号。"""

    month_selected = Signal(QDate)

    def __init__(self, initial: QDate | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self._current = QDate(initial) if initial is not None else QDate.currentDate()
        if not self._current.isValid():
            self._current = QDate.currentDate()
        self._selected = QDate(self._current.year(), self._current.month(), 1)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 14)
        root.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(6)
        self.prev_btn = QPushButton("‹")
        self.prev_btn.setFixedSize(30, 30)
        self.prev_btn.setCursor(Qt.PointingHandCursor)
        self.prev_btn.setStyleSheet(_NAV_BTN_STYLE)
        self.prev_btn.clicked.connect(lambda: self._shift_years(-1))
        self.next_btn = QPushButton("›")
        self.next_btn.setFixedSize(30, 30)
        self.next_btn.setCursor(Qt.PointingHandCursor)
        self.next_btn.setStyleSheet(_NAV_BTN_STYLE)
        self.next_btn.clicked.connect(lambda: self._shift_years(1))
        self.title_label = QLabel()
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setStyleSheet(_TITLE_STYLE)
        header.addWidget(self.prev_btn)
        header.addWidget(self.title_label, 1)
        header.addWidget(self.next_btn)
        root.addLayout(header)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        self._cells: list[QPushButton] = []
        for index in range(12):
            btn = QPushButton(f"{index + 1}月")
            btn.setFixedSize(64, 32)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _checked=False, month=index + 1: self._on_month(month))
            self._cells.append(btn)
            grid.addWidget(btn, index // 4, index % 4)
        root.addLayout(grid)

        self._rebuild()

    def selectedMonth(self) -> QDate:
        return self._selected

    def _shift_years(self, delta: int) -> None:
        self._current = QDate(self._current.year() + delta, self._current.month(), 1)
        self._rebuild()

    def _on_month(self, month: int) -> None:
        date = QDate(self._current.year(), month, 1)
        self._selected = date
        self._current = QDate(date.year(), date.month(), 1)
        self._rebuild()
        self.month_selected.emit(date)

    def _rebuild(self) -> None:
        self.title_label.setText(f"{self._current.year()}年")
        today = QDate.currentDate()
        current_month = QDate(today.year(), today.month(), 1)
        for index, btn in enumerate(self._cells):
            date = QDate(self._current.year(), index + 1, 1)
            if date == self._selected:
                btn.setStyleSheet(_day_style(
                    theme.PRIMARY, "#ffffff", f"1px solid {theme.PRIMARY}",
                    hover_bg=theme.PRIMARY_HOVER, hover_fg="#ffffff",
                ))
            elif date == current_month:
                btn.setStyleSheet(_day_style("#ffffff", theme.PRIMARY, f"1px solid {theme.PRIMARY}"))
            else:
                btn.setStyleSheet(_day_style("transparent", theme.TEXT, "1px solid transparent"))


def _place_below(dialog: QDialog, anchor: QWidget) -> None:
    """把弹窗放到锚点控件下方，并保证不超出屏幕。"""
    pos = anchor.mapToGlobal(QPoint(0, anchor.height() + 6))
    screen = anchor.screen()
    if screen is not None:
        available = screen.availableGeometry()
        hint = dialog.sizeHint()
        if pos.x() + hint.width() > available.right():
            pos.setX(max(available.left(), available.right() - hint.width() - 8))
        if pos.y() + hint.height() > available.bottom():
            pos.setY(max(available.top(), pos.y() - hint.height() - 36))
    dialog.move(pos)


class MonthPopupDialog(QDialog):
    """锚定在控件旁的月份弹窗，点击外部自动关闭。"""

    def __init__(self, anchor: QWidget, initial: QDate | None = None) -> None:
        super().__init__(anchor, Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._chosen: QDate | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.calendar = MonthPopup(initial, self)
        layout.addWidget(self.calendar)
        self.calendar.month_selected.connect(self._on_selected)

    def _on_selected(self, date: QDate) -> None:
        self._chosen = QDate(date)
        self.accept()

    def chosenDate(self) -> QDate | None:
        return self._chosen

    @staticmethod
    def pick(anchor: QWidget, initial: QDate | None = None) -> QDate | None:
        dialog = MonthPopupDialog(anchor, initial)
        _place_below(dialog, anchor)
        dialog.exec()
        return dialog.chosenDate()
