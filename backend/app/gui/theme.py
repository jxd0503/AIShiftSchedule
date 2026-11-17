"""应用主题：天蓝色配色、字体、全局样式表与少量界面辅助函数。"""

from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPalette, QPixmap
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QPushButton, QWidget

from ..config import resource_root

PRIMARY = "#0284c7"
PRIMARY_HOVER = "#0369a1"
PRIMARY_PRESSED = "#075985"
PRIMARY_SOFT = "#e0f2fe"

TEXT = "#1e2025"
TEXT_SECONDARY = "#6b7280"
TEXT_MUTED = "#9ca3af"

BORDER = "#e5e7eb"
BORDER_INPUT = "#d1d5db"
ROW_SELECTED = "#e5e7eb"
PAGE_BG = "#f4f9fd"
CARD_BG = "#ffffff"

SUCCESS_BG = "#ecfdf5"
SUCCESS_FG = "#15803d"
WARNING_BG = "#fffbeb"
WARNING_FG = "#b45309"
DANGER_BG = "#fef2f2"
DANGER_FG = "#b91c1c"


def resource_file(name: str) -> Path:
    return resource_root() / "backend" / "resources" / name


class _TabWheelBlocker(QObject):
    """吞掉选项卡栏上的滚轮事件，避免滚动时误切换选项卡。"""

    def eventFilter(self, obj, event):  # noqa: N802
        if event.type() == QEvent.Wheel:
            return True
        return super().eventFilter(obj, event)


def disable_tab_wheel_scroll(tab_widget) -> None:
    tab_widget.tabBar().installEventFilter(_TabWheelBlocker(tab_widget))


def logo_pixmap(height: int = 22) -> QPixmap:
    path = resource_file("app_logo.png")
    pixmap = QPixmap(str(path))
    if pixmap.isNull():
        return pixmap
    return pixmap.scaledToHeight(height, Qt.SmoothTransformation)


def app_icon() -> QIcon:
    return QIcon(str(resource_file("app_logo.ico")))


def set_role(button: QPushButton, role: str) -> None:
    button.setProperty("class", role)
    repolish(button)


def repolish(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def card_frame() -> QFrame:
    frame = QFrame()
    frame.setObjectName("Card")
    return frame


def hint_label(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setProperty("class", "hint")
    return label


_QSS_TEMPLATE = """
QWidget {{
    color: {text};
    font-size: 13px;
}}
QMainWindow, QDialog {{
    background: {page_bg};
}}
QFrame#AppHeader {{
    background: #ffffff;
    border-bottom: 1px solid {border};
}}
QFrame#PageRoot {{
    background: {page_bg};
}}
QFrame#Card {{
    background: {card_bg};
    border: 1px solid {border};
    border-radius: 8px;
}}
QScrollArea {{
    background: transparent;
    border: none;
}}
QLabel#AppBrand {{
    font-size: 16px;
    font-weight: 600;
    color: {text};
}}
QLabel#PageTitle {{
    font-size: 22px;
    font-weight: 700;
    color: {text};
}}
QLabel#SectionTitle {{
    font-size: 15px;
    font-weight: 600;
    color: {text};
}}
QLabel[class="hint"] {{
    color: {text_secondary};
    font-size: 12px;
}}
QLabel[class="muted"] {{
    color: {text_muted};
    font-size: 12px;
}}
QLabel#StatusPill {{
    background: #f3f4f6;
    color: {text_secondary};
    border-radius: 11px;
    padding: 4px 14px;
    font-size: 12px;
}}
QLabel#StatusPill[state="success"] {{
    background: {success_bg};
    color: {success_fg};
}}
QPushButton {{
    background: #ffffff;
    color: {text};
    border: 1px solid {border_input};
    border-radius: 6px;
    padding: 5px 12px;
}}
QPushButton:hover {{
    border-color: {primary};
    color: {primary};
}}
QPushButton:pressed {{
    background: {primary_soft};
}}
QPushButton:disabled {{
    background: #f9fafb;
    border-color: {border};
    color: {text_muted};
}}
QPushButton[class="primary"] {{
    background: {primary};
    border-color: {primary};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton[class="primary"]:hover {{
    background: {primary_hover};
    border-color: {primary_hover};
    color: #ffffff;
}}
QPushButton[class="primary"]:pressed {{
    background: {primary_pressed};
    border-color: {primary_pressed};
    color: #ffffff;
}}
QPushButton[class="primary"]:disabled {{
    background: #bae6fd;
    border-color: #bae6fd;
    color: #ffffff;
}}
QPushButton[class="link"] {{
    background: transparent;
    border: none;
    color: {text_secondary};
    padding: 4px 2px;
    text-align: left;
}}
QPushButton[class="link"]:hover {{
    color: {primary};
}}
QPushButton[class="nav"] {{
    background: transparent;
    border: none;
    border-radius: 4px;
    color: #4b5563;
    padding: 6px 10px;
}}
QPushButton[class="nav"]:hover {{
    background: {primary_soft};
    color: {primary};
}}
QPushButton[class="nav"][active="true"] {{
    color: {primary};
    font-weight: 600;
}}
QPushButton[class="danger"] {{
    color: {danger_fg};
    border-color: #fecaca;
}}
QPushButton[class="danger"]:hover {{
    background: {danger_bg};
    border-color: {danger_fg};
    color: {danger_fg};
}}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTimeEdit, QDateEdit,
QPlainTextEdit, QTextEdit {{
    background: #ffffff;
    color: {text};
    border: 1px solid {border_input};
    border-radius: 6px;
    padding: 4px 8px;
    selection-background-color: #bae6fd;
    selection-color: {text};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QTimeEdit:focus, QDateEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {primary};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QTimeEdit:disabled, QDateEdit:disabled {{
    background: #f9fafb;
    color: {text_muted};
}}
QComboBox::drop-down {{
    border: none;
    width: 18px;
}}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button,
QDateEdit::up-button, QDateEdit::down-button {{
    width: 0;
    height: 0;
    border: none;
}}
QTimeEdit {{
    padding: 4px 26px 4px 10px;
}}
QTimeEdit::up-button, QTimeEdit::down-button {{
    subcontrol-origin: border;
    width: 20px;
    background: transparent;
    border: none;
}}
QTimeEdit::up-button {{
    subcontrol-position: top right;
    border-top-right-radius: 5px;
    height: 13px;
}}
QTimeEdit::down-button {{
    subcontrol-position: bottom right;
    border-bottom-right-radius: 5px;
    height: 13px;
}}
QTimeEdit::up-button:hover, QTimeEdit::down-button:hover {{
    background: {primary_soft};
}}
QTimeEdit::up-arrow {{
    image: url({up_arrow});
}}
QTimeEdit::down-arrow {{
    image: url({down_arrow});
}}
QComboBox QAbstractItemView {{
    background: #ffffff;
    border: 1px solid {border};
    border-radius: 6px;
    outline: none;
    selection-background-color: {row_selected};
    selection-color: {text};
}}
QCheckBox {{
    spacing: 8px;
    color: {text};
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {border_input};
    border-radius: 4px;
    background: #ffffff;
}}
QCheckBox::indicator:hover {{
    border-color: {primary};
}}
QCheckBox::indicator:checked {{
    background: {primary};
    border-color: {primary};
    image: url({check_icon});
}}
QTabWidget::pane {{
    background: {card_bg};
    border: 1px solid {border};
    border-radius: 8px;
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {text_secondary};
    border: none;
    border-bottom: 2px solid transparent;
    padding: 4px 14px;
    margin-right: 4px;
}}
QTabBar::tab:hover {{
    color: {primary};
}}
QTabBar::tab:selected {{
    color: {primary};
    border-bottom: 2px solid {primary};
    font-weight: 600;
}}
QTabWidget#InnerTabs::pane {{
    background: transparent;
    border: none;
    border-radius: 0;
}}
QTableWidget, QTableView {{
    background: #ffffff;
    alternate-background-color: #f7fbfe;
    border: 1px solid {border};
    border-radius: 8px;
    gridline-color: #eef0f2;
    selection-background-color: {row_selected};
    selection-color: {text};
    outline: none;
}}
QTableWidget#CardTable, QTableWidget#CardTable QHeaderView {{
    background: transparent;
    border: none;
    border-radius: 0;
}}
QTableWidget::item:selected, QTableView::item:selected {{
    background: {row_selected};
    color: {text};
}}
QTableWidget::item:focus, QTableView::item:focus {{
    outline: none;
    border: none;
}}
QTableWidget::item, QTableView::item {{
    padding: 4px 6px;
}}
/* 日历弹窗内部同样是 QTableView；通用的单元格内边距会挤压文本区，
   导致两位数日期被省略成「...」，这里重置为 0 */
QCalendarWidget QAbstractItemView::item {{
    padding: 0px;
}}
QHeaderView {{
    background: #ffffff;
}}
QHeaderView::section {{
    background: #ffffff;
    color: {text_secondary};
    border: none;
    border-bottom: 1px solid {border};
    padding: 6px 8px;
    font-size: 12px;
    font-weight: 600;
}}
QTableCornerButton::section {{
    background: #ffffff;
    border: none;
}}
QGroupBox {{
    background: {card_bg};
    border: 1px solid {border};
    border-radius: 8px;
    margin-top: 14px;
    padding: 18px 16px 14px 16px;
    color: {text};
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 16px;
    top: 2px;
    padding: 0 4px;
    background: {card_bg};
    color: {text};
}}
QProgressBar {{
    background: #f3f4f6;
    border: none;
    border-radius: 7px;
    height: 14px;
    text-align: center;
    color: #4b5563;
    font-size: 11px;
}}
QProgressBar::chunk {{
    background: {primary};
    border-radius: 7px;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: #cbd5e1;
    border-radius: 5px;
    min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{
    background: #94a3b8;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: #cbd5e1;
    border-radius: 5px;
    min-width: 32px;
}}
QScrollBar::handle:horizontal:hover {{
    background: #94a3b8;
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0;
    height: 0;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}
QSplitter::handle {{
    background: transparent;
}}
QSplitter::handle:vertical {{
    height: 10px;
}}
QToolTip {{
    background: #ffffff;
    color: {text};
    border: 1px solid {border};
    padding: 4px 6px;
}}
"""


def stylesheet() -> str:
    check_icon = resource_file("check.png").as_posix()
    up_arrow = resource_file("up_arrow.png").as_posix()
    down_arrow = resource_file("down_arrow.png").as_posix()
    return _QSS_TEMPLATE.format(
        text=TEXT,
        text_secondary=TEXT_SECONDARY,
        text_muted=TEXT_MUTED,
        border=BORDER,
        border_input=BORDER_INPUT,
        row_selected=ROW_SELECTED,
        page_bg=PAGE_BG,
        card_bg=CARD_BG,
        primary=PRIMARY,
        primary_hover=PRIMARY_HOVER,
        primary_pressed=PRIMARY_PRESSED,
        primary_soft=PRIMARY_SOFT,
        success_bg=SUCCESS_BG,
        success_fg=SUCCESS_FG,
        danger_bg=DANGER_BG,
        danger_fg=DANGER_FG,
        check_icon=check_icon,
        up_arrow=up_arrow,
        down_arrow=down_arrow,
    )


def install(app: QApplication) -> None:
    font = QFont("Microsoft YaHei UI", 10)
    font.setStyleHint(QFont.SansSerif)
    app.setFont(font)

    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(PAGE_BG))
    palette.setColor(QPalette.WindowText, QColor(TEXT))
    palette.setColor(QPalette.Base, QColor("#ffffff"))
    palette.setColor(QPalette.AlternateBase, QColor("#f7fbfe"))
    palette.setColor(QPalette.Text, QColor(TEXT))
    palette.setColor(QPalette.Button, QColor("#ffffff"))
    palette.setColor(QPalette.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.Highlight, QColor(ROW_SELECTED))
    palette.setColor(QPalette.HighlightedText, QColor(TEXT))
    palette.setColor(QPalette.PlaceholderText, QColor(TEXT_MUTED))
    palette.setColor(QPalette.ToolTipBase, QColor("#ffffff"))
    palette.setColor(QPalette.ToolTipText, QColor(TEXT))
    app.setPalette(palette)

    app.setStyleSheet(stylesheet())
