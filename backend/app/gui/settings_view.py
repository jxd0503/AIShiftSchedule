from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..errors import DomainError
from ..services import settings_service
from . import theme
from .ai_worker import AiWorker

PRIORITY_LABELS = {
    1: "个人显式偏好（最高）",
    2: "班次时长合理性",
    3: "全局人数 / 覆盖",
    4: "8 小时偏好（最易让步）",
}


class SettingsView(QWidget):
    """设置页：模型接口配置、连接测试与软约束权重。"""

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict[str, Any]] = []
        self._worker: AiWorker | None = None
        self._build_ui()
        self.reload()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(10)

        titles = QVBoxLayout()
        titles.setSpacing(4)
        title = QLabel("模型设置")
        title.setObjectName("PageTitle")
        titles.addWidget(title)
        subtitle = theme.hint_label("配置 AI 模型接口与软约束权重，保存在本机数据库中")
        titles.addWidget(subtitle)
        layout.addLayout(titles)
        layout.addSpacing(6)

        layout.addWidget(self._build_model_group())
        layout.addWidget(self._build_weights_group(), 1)

        # 空间不足时整页滚动，避免输入框被布局压缩导致文字裁切
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

    def _build_model_group(self) -> QGroupBox:
        group = QGroupBox("接口配置")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setSpacing(10)
        hint = theme.hint_label(
            "用于把自然语言约束解析为结构化规则，支持任意 OpenAI 兼容接口。"
        )
        layout.addWidget(hint)

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("https://api.deepseek.com/v1")
        form.addRow("接口地址", self.base_url_edit)
        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("deepseek-chat")
        form.addRow("模型名称", self.model_edit)
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText("填写服务商提供的密钥")
        form.addRow("API Key", self.api_key_edit)
        layout.addLayout(form)

        actions = QHBoxLayout()
        self.save_btn = QPushButton("保存")
        self.save_btn.setProperty("class", "primary")
        self.save_btn.clicked.connect(self._save)
        actions.addWidget(self.save_btn)
        self.test_btn = QPushButton("测试连接")
        self.test_btn.clicked.connect(self._test)
        actions.addWidget(self.test_btn)
        actions.addStretch()
        layout.addLayout(actions)

        self.test_result = QLabel()
        self.test_result.setWordWrap(True)
        self.test_result.setProperty("class", "hint")
        layout.addWidget(self.test_result)

        note = theme.hint_label(
            "密钥保存在本机数据目录的 SQLite 数据库中；「测试连接」会发送一次极短的对话请求。"
        )
        note.setProperty("class", "muted")
        layout.addWidget(note)
        return group

    def _build_weights_group(self) -> QGroupBox:
        group = QGroupBox("软约束权重")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setSpacing(10)
        hint = theme.hint_label(
            "权重越高越不该被牺牲：个人显式偏好 > 班次时长合理性 > 全局人数 / 覆盖 > 8 小时偏好。"
            "硬约束不进入目标函数，任何软权重都不会挤掉硬约束。"
        )
        layout.addWidget(hint)

        self.weight_table = QTableWidget(0, 6)
        self.weight_table.setHorizontalHeaderLabels(
            ["优先级", "指标", "计权单位", "当前权重", "默认值", "理由"]
        )
        header = self.weight_table.horizontalHeader()
        for col in range(5):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.Stretch)
        self.weight_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.weight_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.weight_table.verticalHeader().setVisible(False)
        self.weight_table.verticalHeader().setDefaultSectionSize(40)
        layout.addWidget(self.weight_table, 1)

        actions = QHBoxLayout()
        reset_btn = QPushButton("全部恢复默认")
        reset_btn.clicked.connect(self._restore_defaults)
        actions.addWidget(reset_btn)
        actions.addStretch()
        save_weights_btn = QPushButton("保存权重")
        save_weights_btn.setProperty("class", "primary")
        save_weights_btn.clicked.connect(self._save)
        actions.addWidget(save_weights_btn)
        layout.addLayout(actions)
        return group

    # ------------------------------------------------------------------ 数据

    def reload(self) -> None:
        try:
            values = settings_service.load_settings()
        except DomainError as error:
            QMessageBox.warning(self, "加载失败", str(error))
            return
        self.base_url_edit.setText(values["llm_base_url"])
        self.model_edit.setText(values["llm_model"])
        self.api_key_edit.setText(values["llm_api_key"])
        self._rows = list(values["soft_weights"])
        self._fill_weight_table()

    def _fill_weight_table(self) -> None:
        self.weight_table.setRowCount(len(self._rows))
        for row, item in enumerate(self._rows):
            values = [
                PRIORITY_LABELS.get(item["priority"], str(item["priority"])),
                str(item["label"]),
                str(item["unit"]),
            ]
            for col, text in enumerate(values):
                self.weight_table.setItem(row, col, QTableWidgetItem(text))
            spin = QDoubleSpinBox()
            spin.setRange(0, 1000000)
            spin.setDecimals(0)
            spin.setSingleStep(10)
            spin.setValue(float(item["value"]))
            self.weight_table.setCellWidget(row, 3, spin)
            self.weight_table.setItem(
                row, 4, QTableWidgetItem(str(item["default"]))
            )
            self.weight_table.setItem(
                row, 5, QTableWidgetItem(str(item["rationale"]))
            )

    def _collect_weights(self) -> dict[str, float]:
        weights: dict[str, float] = {}
        for row, item in enumerate(self._rows):
            spin = self.weight_table.cellWidget(row, 3)
            if isinstance(spin, QDoubleSpinBox):
                weights[str(item["key"])] = float(spin.value())
        return weights

    def _restore_defaults(self) -> None:
        for row, item in enumerate(self._rows):
            spin = self.weight_table.cellWidget(row, 3)
            if isinstance(spin, QDoubleSpinBox):
                spin.setValue(float(item["default"]))

    def _save(self) -> None:
        try:
            settings_service.save_settings(
                self.base_url_edit.text(),
                self.model_edit.text(),
                self.api_key_edit.text(),
                self._collect_weights(),
            )
        except DomainError as error:
            QMessageBox.warning(self, "保存失败", str(error))
            return
        QMessageBox.information(self, "已保存", "设置已保存")
        self._rows = list(settings_service.load_settings()["soft_weights"])
        self._fill_weight_table()

    # ------------------------------------------------------------------ 连接测试

    def _test(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "提示", "测试正在进行，请稍候")
            return
        self.test_result.setText("正在连接…")
        self.test_btn.setEnabled(False)
        worker = AiWorker(
            settings_service.test_connection,
            self.base_url_edit.text(),
            self.model_edit.text(),
            self.api_key_edit.text(),
        )
        worker.succeeded.connect(self._on_test_ok)
        worker.failed.connect(self._on_test_failed)
        worker.finished.connect(self._on_test_done)
        self._worker = worker
        worker.start()

    def _on_test_ok(self, result: tuple[str, float]) -> None:
        message, elapsed = result
        self.test_result.setText(f"连接成功，耗时 {elapsed:.1f} 秒，模型回复：{message}")

    def _on_test_failed(self, message: str) -> None:
        self.test_result.setText(message)

    def _on_test_done(self) -> None:
        self._worker = None
        self.test_btn.setEnabled(True)

    def shutdown(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(5000)
