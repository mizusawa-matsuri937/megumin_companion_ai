"""Accessible, bounded W16 settings and privacy-management dialog."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from app.schemas import FeatureActualState, FeatureName
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from desktop_client.ui.contracts import (
    DesktopSettingsForm,
    FeatureSetCommand,
    ManagementCommand,
    ManagementDebugCommand,
    ManagementRefreshCommand,
    MemoryClearCommand,
    MemoryConfirmCommand,
    MemoryDeleteCommand,
    MemoryDetailCommand,
    MemoryExportCommand,
    MemoryListCommand,
    MemoryUpdateCommand,
    SecretRevokeCommand,
    SecretStoreCommand,
    SettingsSaveCommand,
)
from desktop_client.ui.management import ManagementViewModel

_FEATURE_NAMES = {
    FeatureName.recent_history: "最近历史",
    FeatureName.long_term_memory: "长期记忆",
    FeatureName.vision: "视觉感知",
    FeatureName.cloud_vision: "云视觉",
    FeatureName.proactive: "主动发话",
}
_ACTUAL_STATE_NAMES = {
    FeatureActualState.disabled: "已停用",
    FeatureActualState.enabling: "启用中",
    FeatureActualState.enabled: "已启用",
    FeatureActualState.disabling: "停用中",
    FeatureActualState.failed: "失败",
}
_FEATURE_ENABLE_CONFIRMATIONS = {
    FeatureName.recent_history: (
        "启用最近历史",
        "确定启用最近历史吗？新的对话会按当前保留策略保存到本机。",
    ),
    FeatureName.long_term_memory: (
        "启用长期记忆",
        "确定启用长期记忆吗？新的记忆建议可能需要你确认后保存。",
    ),
    FeatureName.vision: (
        "启用视觉感知",
        "确定启用视觉感知吗？它只应处理你明确指定的窗口；当前 W16 尚未接入真实截图。",
    ),
    FeatureName.cloud_vision: (
        "启用云视觉",
        "确定启用云视觉吗？后续接线后，脱敏后的指定窗口图像可能发送到配置的云服务；"
        "当前 W16 尚未上传图像。",
    ),
    FeatureName.proactive: (
        "启用主动发话",
        "确定启用主动发话吗？后续接线后，它可能在符合隐私与冷却条件时主动提示。",
    ),
}
_MANAGEMENT_REASON_TEXT = {
    "secret_required": "真实 LLM 需要先保存 DPAPI 密钥。",
    "llm_model_required": "真实 LLM 需要填写模型名。",
    "tts_provider_unsupported": "当前仅支持 mock 或已配置的 GPT-SoVITS。",
    "tts_preset_required": "GPT-SoVITS 需要先配置默认 preset；请在 W19 配置向导完成预检。",
    "settings_invalid": "设置未通过本地 schema 校验。",
    "invalid_management_input": "输入无效或超出允许范围。",
    "private_state_runtime_disabled": "当前后端未提供本地记忆/功能运行时。",
    "memory_not_found": "这条长期记忆已不存在。",
    "confirmation_not_found": "这条待确认建议已不存在或已过期。",
    "long_term_memory_disabled": "长期记忆功能当前已关闭。",
    "credential_content_forbidden": "长期记忆不能保存凭据内容。",
    "export_path_invalid": "导出位置必须是有效的绝对文件路径。",
    "export_directory_unavailable": "导出目录不可用。",
    "export_destination_invalid": "导出目标不是可安全写入的普通文件。",
    "export_destination_exists": "导出目标已存在，未覆盖。",
    "export_write_failed": "导出文件写入失败。",
    "secret_invalid_contract": "密钥输入不符合安全存储要求。",
    "secret_protection_unavailable": "当前 Windows 用户的 DPAPI 保护不可用。",
    "secret_corrupt": "已保存的加密密钥损坏。",
    "secret_decrypt_failed": "已保存的加密密钥无法读取。",
    "secret_purpose_mismatch": "已保存的密钥用途不匹配。",
    "secret_io_failed": "加密密钥文件无法访问。",
    "management_failed": "操作未完成，请查看无内容错误码。",
}
_MANAGEMENT_OPERATION_TEXT = {
    "settings_saved": "设置已保存",
    "secret_stored": "密钥或令牌已保存",
    "secret_revoked": "密钥或令牌已移除",
    "feature_set": "功能状态已更新",
    "memory_updated": "长期记忆已更新",
    "memory_deleted": "长期记忆已删除",
    "history_cleared": "最近历史已清空",
    "memories_cleared": "长期记忆已清空",
    "memory_confirmed": "记忆建议已处理",
    "memory_exported": "长期记忆已导出",
}


class SettingsDialog(QDialog):
    """A local UI only; every mutable operation is delegated to BackendThread."""

    def __init__(
        self,
        model: ManagementViewModel,
        submit_command: Callable[[ManagementCommand], bool],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._model = model
        self._submit_command = submit_command
        self._populating_form = False
        self._form_dirty = False
        self._clearing_sensitive = False
        self._feature_buttons: dict[FeatureName, QPushButton] = {}
        self.setWindowTitle("设置与隐私")
        self.setAccessibleName("设置与隐私")
        self.resize(820, 650)

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget(self)
        self.tabs.setAccessibleName("设置页面")
        self.tabs.addTab(self._build_connection_tab(), "连接与设备")
        self.tabs.addTab(self._build_features_tab(), "功能与隐私")
        self.tabs.addTab(self._build_memory_tab(), "历史与记忆")
        self.tabs.addTab(self._build_debug_tab(), "调试状态")
        layout.addWidget(self.tabs)
        self.status_label = QLabel("正在读取设置…", self)
        self.status_label.setAccessibleName("设置操作状态")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
        self.sync_from_model()

    def reject(self) -> None:
        """Discard a hidden dialog's drafts and plaintext secret widgets."""

        self.llm_secret.clear()
        self.vts_secret.clear()
        self._form_dirty = False
        if not self._clearing_sensitive:
            self.sync_from_model()
        super().reject()

    def _build_connection_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self.llm_provider = self._line_edit("LLM 提供方")
        self.llm_base_url = self._line_edit("LLM 地址")
        self.llm_model = self._line_edit("LLM 模型")
        self.tts_provider = self._line_edit("TTS 提供方")
        self.tts_base_url = self._line_edit("TTS 地址")
        self.vts_enabled = QCheckBox("启用 VTube Studio（重启后生效）", page)
        self.vts_uri = self._line_edit("VTS 地址")
        self.vts_plugin_name = self._line_edit("VTS 插件名称")
        self.vts_plugin_developer = self._line_edit("VTS 开发者名称")
        self.stt_enabled = QCheckBox("启用本地语音输入配置（W18 后生效）", page)
        self.stt_executable = self._line_edit("Whisper 可执行文件")
        self.stt_model_path = self._line_edit("Whisper 模型")
        self.stt_device = self._line_edit("音频设备")
        self.startup_enabled = QCheckBox("登录后自动启动", page)
        for checkbox in (self.vts_enabled, self.stt_enabled, self.startup_enabled):
            checkbox.toggled.connect(self._mark_form_dirty_bool)
        form.addRow("LLM 提供方", self.llm_provider)
        form.addRow("LLM 基础地址", self.llm_base_url)
        form.addRow("LLM 模型", self.llm_model)
        form.addRow("TTS 提供方", self.tts_provider)
        form.addRow("TTS 基础地址", self.tts_base_url)
        form.addRow("VTS", self.vts_enabled)
        form.addRow("VTS URI", self.vts_uri)
        form.addRow("VTS 插件名称", self.vts_plugin_name)
        form.addRow("VTS 开发者名称", self.vts_plugin_developer)
        form.addRow("本地 STT", self.stt_enabled)
        form.addRow("STT 可执行文件", self.stt_executable)
        form.addRow("STT 模型", self.stt_model_path)
        form.addRow("STT 设备（编号或名称）", self.stt_device)
        form.addRow("启动项", self.startup_enabled)
        layout.addLayout(form)
        save = QPushButton("保存设置（需要重启才能应用）", page)
        save.setAccessibleName("保存设置")
        save.clicked.connect(self._save_settings)
        layout.addWidget(save)

        secrets = QGroupBox("密钥与令牌", page)
        secrets_layout = QGridLayout(secrets)
        guidance = QLabel(
            "输入仅写入当前 Windows 用户的 DPAPI 加密存储，不会回填或显示明文。",
            secrets,
        )
        guidance.setWordWrap(True)
        secrets_layout.addWidget(guidance, 0, 0, 1, 3)
        self.llm_secret = self._secret_edit("LLM API 密钥")
        self.llm_secret_state = QLabel("LLM 密钥：未知", secrets)
        save_llm = QPushButton("保存 LLM 密钥", secrets)
        save_llm.clicked.connect(lambda: self._store_secret("llm"))
        revoke_llm = QPushButton("移除 LLM 密钥", secrets)
        revoke_llm.clicked.connect(lambda: self._revoke_secret("llm"))
        secrets_layout.addWidget(self.llm_secret, 1, 0)
        secrets_layout.addWidget(save_llm, 1, 1)
        secrets_layout.addWidget(revoke_llm, 1, 2)
        secrets_layout.addWidget(self.llm_secret_state, 2, 0, 1, 3)
        self.vts_secret = self._secret_edit("VTS 认证令牌")
        self.vts_secret_state = QLabel("VTS 令牌：未知", secrets)
        save_vts = QPushButton("保存 VTS 令牌", secrets)
        save_vts.clicked.connect(lambda: self._store_secret("vts"))
        revoke_vts = QPushButton("移除 VTS 令牌", secrets)
        revoke_vts.clicked.connect(lambda: self._revoke_secret("vts"))
        secrets_layout.addWidget(self.vts_secret, 3, 0)
        secrets_layout.addWidget(save_vts, 3, 1)
        secrets_layout.addWidget(revoke_vts, 3, 2)
        secrets_layout.addWidget(self.vts_secret_state, 4, 0, 1, 3)
        layout.addWidget(secrets)
        layout.addStretch(1)
        return page

    def _build_features_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        disclosure = QLabel(
            "视觉感知仅应处理你明确指定的窗口；云视觉是独立开关，启用后可能将脱敏后的指定窗口图像发送到配置的云服务。"
            "关闭视觉或主动发话时，界面会等待后端完成停止屏障后才显示最终状态。"
            "当前 W16 不会声称已经接入真实截图或云上传。",
            page,
        )
        disclosure.setWordWrap(True)
        disclosure.setAccessibleName("视觉和云端隐私说明")
        layout.addWidget(disclosure)
        self.feature_tree = QTreeWidget(page)
        self.feature_tree.setAccessibleName("功能状态")
        self.feature_tree.setColumnCount(5)
        self.feature_tree.setHeaderLabels(["功能", "期望状态", "实际状态", "原因", "操作"])
        self._feature_items: dict[FeatureName, QTreeWidgetItem] = {}
        for feature in FeatureName:
            item = QTreeWidgetItem([_FEATURE_NAMES[feature], "—", "—", "—", ""])
            self.feature_tree.addTopLevelItem(item)
            button = QPushButton("读取中", self.feature_tree)
            button.setAccessibleName(f"切换{_FEATURE_NAMES[feature]}")
            button.clicked.connect(
                lambda _checked=False, target=feature: self._toggle_feature(target)
            )
            self.feature_tree.setItemWidget(item, 4, button)
            self._feature_items[feature] = item
            self._feature_buttons[feature] = button
        self.feature_tree.resizeColumnToContents(0)
        layout.addWidget(self.feature_tree, 1)
        refresh = QPushButton("刷新功能状态", page)
        refresh.clicked.connect(self._refresh)
        layout.addWidget(refresh)
        return page

    def _build_memory_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        hint = QLabel(
            "删除先从业务可见面移除，底层清理为 best effort；"
            "不会承诺 SSD、备份或系统快照中的物理擦除。"
            "所有清空和删除操作都要求再次确认。",
            page,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        search_row = QHBoxLayout()
        self.memory_search = self._line_edit("搜索长期记忆")
        self.memory_search.setPlaceholderText("搜索长期记忆")
        search = QPushButton("搜索", page)
        search.clicked.connect(self._search_memory)
        refresh = QPushButton("刷新列表", page)
        refresh.clicked.connect(lambda: self._submit(MemoryListCommand()))
        search_row.addWidget(self.memory_search, 1)
        search_row.addWidget(search)
        search_row.addWidget(refresh)
        layout.addLayout(search_row)
        self.memory_tree = QTreeWidget(page)
        self.memory_tree.setAccessibleName("长期记忆列表")
        self.memory_tree.setColumnCount(4)
        self.memory_tree.setHeaderLabels(["内容", "类型", "敏感度", "更新时间"])
        self.memory_tree.itemSelectionChanged.connect(self._load_selected_memory)
        layout.addWidget(self.memory_tree, 1)
        self.memory_list_notice = QLabel("", page)
        layout.addWidget(self.memory_list_notice)

        editor_group = QGroupBox("所选长期记忆", page)
        editor_layout = QVBoxLayout(editor_group)
        self.memory_detail_label = QLabel("选择一项以查看或编辑。", editor_group)
        self.memory_detail = QPlainTextEdit(editor_group)
        self.memory_detail.setAccessibleName("长期记忆内容")
        self.memory_detail.setEnabled(False)
        edit_row = QHBoxLayout()
        self.save_memory = QPushButton("保存编辑", editor_group)
        self.save_memory.setEnabled(False)
        self.save_memory.clicked.connect(self._save_memory)
        self.delete_memory = QPushButton("删除所选记忆", editor_group)
        self.delete_memory.setEnabled(False)
        self.delete_memory.clicked.connect(self._delete_memory)
        edit_row.addStretch(1)
        edit_row.addWidget(self.delete_memory)
        edit_row.addWidget(self.save_memory)
        editor_layout.addWidget(self.memory_detail_label)
        editor_layout.addWidget(self.memory_detail)
        editor_layout.addLayout(edit_row)
        layout.addWidget(editor_group)

        management_row = QHBoxLayout()
        clear_history = QPushButton("清空最近历史", page)
        clear_history.clicked.connect(lambda: self._clear_memory("history"))
        clear_memories = QPushButton("清空长期记忆", page)
        clear_memories.clicked.connect(lambda: self._clear_memory("memories"))
        export = QPushButton("导出长期记忆", page)
        export.clicked.connect(self._export_memories)
        management_row.addWidget(clear_history)
        management_row.addWidget(clear_memories)
        management_row.addWidget(export)
        management_row.addStretch(1)
        layout.addLayout(management_row)

        confirmations = QGroupBox("待确认的记忆建议", page)
        confirmations_layout = QVBoxLayout(confirmations)
        self.confirmation_tree = QTreeWidget(confirmations)
        self.confirmation_tree.setAccessibleName("待确认记忆")
        self.confirmation_tree.setColumnCount(3)
        self.confirmation_tree.setHeaderLabels(["建议内容", "证据摘录", "到期时间"])
        confirmation_row = QHBoxLayout()
        approve = QPushButton("批准所选建议", confirmations)
        approve.clicked.connect(lambda: self._confirm_selected(True))
        reject = QPushButton("拒绝所选建议", confirmations)
        reject.clicked.connect(lambda: self._confirm_selected(False))
        confirmation_row.addStretch(1)
        confirmation_row.addWidget(reject)
        confirmation_row.addWidget(approve)
        confirmations_layout.addWidget(self.confirmation_tree)
        confirmations_layout.addLayout(confirmation_row)
        layout.addWidget(confirmations)
        return page

    def _build_debug_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QFormLayout(page)
        self.debug_version = QLabel("—", page)
        self.debug_capabilities = QLabel("—", page)
        self.debug_queues = QLabel("—", page)
        self.debug_error_code = QLabel("—", page)
        self.debug_note = QLabel(
            "仅显示版本、能力、稳定错误码和队列占用；不会显示消息、记忆、路径或密钥内容。",
            page,
        )
        self.debug_note.setWordWrap(True)
        refresh = QPushButton("刷新调试状态", page)
        refresh.clicked.connect(lambda: self._submit(ManagementDebugCommand()))
        layout.addRow("版本", self.debug_version)
        layout.addRow("能力", self.debug_capabilities)
        layout.addRow("桥队列", self.debug_queues)
        layout.addRow("最近错误码", self.debug_error_code)
        layout.addRow("说明", self.debug_note)
        layout.addRow("", refresh)
        return page

    def _line_edit(self, accessible_name: str) -> QLineEdit:
        edit = QLineEdit(self)
        edit.setAccessibleName(accessible_name)
        edit.textEdited.connect(self._mark_form_dirty)
        return edit

    def _secret_edit(self, accessible_name: str) -> QLineEdit:
        # A credential must not mark unrelated settings as a persistent draft.
        # It is erased immediately after submission and whenever the dialog is
        # dismissed, so the form's normal refresh path remains available.
        edit = QLineEdit(self)
        edit.setAccessibleName(accessible_name)
        edit.setEchoMode(QLineEdit.EchoMode.Password)
        edit.setPlaceholderText(accessible_name)
        return edit

    def _mark_form_dirty(self, _value: str) -> None:
        if not self._populating_form:
            self._form_dirty = True

    def _mark_form_dirty_bool(self, _value: bool) -> None:
        if not self._populating_form:
            self._form_dirty = True

    def _submit(self, command: ManagementCommand) -> bool:
        accepted = self._submit_command(command)
        if not accepted:
            self.status_label.setText("命令队列已满；未执行该操作。")
        return accepted

    def _refresh(self) -> None:
        self._submit(ManagementRefreshCommand())

    def _save_settings(self) -> None:
        try:
            form = self._model.settings.form if self._model.settings is not None else None
            if form is None:
                self.status_label.setText("设置尚未加载。")
                return
            self._submit(
                SettingsSaveCommand(
                    payload=DesktopSettingsForm(
                        llm_provider=self.llm_provider.text(),
                        llm_base_url=self.llm_base_url.text(),
                        llm_model=self.llm_model.text(),
                        tts_provider=self.tts_provider.text(),
                        tts_base_url=self.tts_base_url.text(),
                        vts_enabled=self.vts_enabled.isChecked(),
                        vts_uri=self.vts_uri.text(),
                        vts_plugin_name=self.vts_plugin_name.text(),
                        vts_plugin_developer=self.vts_plugin_developer.text(),
                        stt_enabled=self.stt_enabled.isChecked(),
                        stt_executable=self.stt_executable.text(),
                        stt_model_path=self.stt_model_path.text(),
                        stt_device=self.stt_device.text(),
                        startup_enabled=self.startup_enabled.isChecked(),
                    )
                )
            )
        except ValueError:
            self.status_label.setText("设置字段无效，请检查必填项和长度。")

    def _store_secret(self, secret_id: str) -> None:
        edit = self.llm_secret if secret_id == "llm" else self.vts_secret
        value = edit.text()
        if not value.strip():
            self.status_label.setText("请输入非空密钥或令牌。")
            return
        try:
            accepted = self._submit(
                SecretStoreCommand(
                    secret_id="llm" if secret_id == "llm" else "vts",
                    value=value,
                )
            )
        except ValueError:
            self.status_label.setText("密钥或令牌无效。")
            return
        if accepted:
            # The command owns a transient Python string until BackendThread
            # writes it.  Clear every Qt-owned copy immediately and never echo it.
            edit.clear()

    def _revoke_secret(self, secret_id: str) -> None:
        label = "LLM 密钥" if secret_id == "llm" else "VTS 令牌"
        if self._confirm("移除密钥", f"确定移除保存的{label}吗？此操作无法恢复。"):
            self._submit(SecretRevokeCommand(secret_id="llm" if secret_id == "llm" else "vts"))

    def _toggle_feature(self, feature: FeatureName) -> None:
        state = self._model.feature_states.get(feature)
        if state is None:
            return
        enabled = not state.desired_enabled
        if enabled:
            title, message = _FEATURE_ENABLE_CONFIRMATIONS[feature]
            if not self._confirm(title, message):
                return
        button = self._feature_buttons[feature]
        button.setEnabled(False)
        button.setText("正在等待停止屏障…" if not enabled else "正在启用…")
        if not self._submit(FeatureSetCommand(feature=feature, enabled=enabled)):
            # A full bounded bridge means no command was sent; restore the
            # actionable state rather than leaving the control stranded.
            self._sync_features()
            button.setEnabled(True)

    def _search_memory(self) -> None:
        try:
            self._submit(MemoryListCommand(query=self.memory_search.text()))
        except ValueError:
            self.status_label.setText("搜索内容无效。")

    def _load_selected_memory(self) -> None:
        selected = self.memory_tree.selectedItems()
        if not selected:
            return
        memory_id = selected[0].data(0, Qt.ItemDataRole.UserRole)
        if isinstance(memory_id, str):
            self._submit(MemoryDetailCommand(memory_id=memory_id))

    def _save_memory(self) -> None:
        detail = self._model.memory_detail
        if detail is None or detail.item is None:
            return
        if not self._confirm("保存编辑", "确定保存对这条长期记忆的编辑吗？"):
            return
        try:
            self._submit(
                MemoryUpdateCommand(
                    memory_id=detail.item.memory_id,
                    content=self.memory_detail.toPlainText(),
                )
            )
        except ValueError:
            self.status_label.setText("记忆内容不能为空且不能超过允许长度。")

    def _delete_memory(self) -> None:
        detail = self._model.memory_detail
        if detail is None or detail.item is None:
            return
        if self._confirm(
            "删除长期记忆",
            "确定删除所选长期记忆吗？该删除先立即隐藏，物理清理仍可能待处理。",
        ):
            self._submit(MemoryDeleteCommand(memory_id=detail.item.memory_id))

    def _clear_memory(self, target: Literal["history", "memories"]) -> None:
        label = "最近历史" if target == "history" else "长期记忆"
        if self._confirm("清空数据", f"确定清空所有{label}吗？此操作需要后端清理，无法撤销。"):
            self._submit(MemoryClearCommand(target=target))

    def _confirm_selected(self, approved: bool) -> None:
        selected = self.confirmation_tree.selectedItems()
        if not selected:
            self.status_label.setText("请选择一条记忆建议。")
            return
        confirmation_id = selected[0].data(0, Qt.ItemDataRole.UserRole)
        action = "批准并保存" if approved else "拒绝"
        if isinstance(confirmation_id, str) and self._confirm(
            "确认记忆建议",
            f"确定{action}所选记忆建议吗？该决定会改变长期记忆状态。",
        ):
            self._submit(MemoryConfirmCommand(confirmation_id=confirmation_id, approved=approved))

    def _export_memories(self) -> None:
        destination, _ = QFileDialog.getSaveFileName(
            self,
            "导出长期记忆",
            "megumin-memories.json",
            "JSON files (*.json)",
        )
        if not destination:
            return
        if not self._confirm(
            "导出长期记忆",
            "确定导出吗？若目标文件已存在，将被覆盖。导出文件可能包含私密记忆和最小来源证据。",
        ):
            return
        self._submit(MemoryExportCommand(destination=destination, overwrite=True))

    def _confirm(self, title: str, message: str) -> bool:
        response = QMessageBox.question(
            self,
            title,
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        # PySide6's native dialog path may return the underlying integer rather
        # than the enum singleton, so identity comparison would turn an explicit
        # Yes click into a silent cancellation.
        return response == QMessageBox.StandardButton.Yes

    def sync_from_model(self) -> None:
        self._sync_settings()
        self._sync_features()
        self._sync_memory()
        self._sync_debug()
        self._sync_result()

    def _sync_settings(self) -> None:
        snapshot = self._model.settings
        if snapshot is None:
            return
        if not self._form_dirty:
            self._populating_form = True
            form = snapshot.form
            for widget, value in (
                (self.llm_provider, form.llm_provider),
                (self.llm_base_url, form.llm_base_url),
                (self.llm_model, form.llm_model),
                (self.tts_provider, form.tts_provider),
                (self.tts_base_url, form.tts_base_url),
                (self.vts_uri, form.vts_uri),
                (self.vts_plugin_name, form.vts_plugin_name),
                (self.vts_plugin_developer, form.vts_plugin_developer),
                (self.stt_executable, form.stt_executable),
                (self.stt_model_path, form.stt_model_path),
                (self.stt_device, form.stt_device),
            ):
                widget.setText(value)
            self.vts_enabled.setChecked(form.vts_enabled)
            self.stt_enabled.setChecked(form.stt_enabled)
            self.startup_enabled.setChecked(form.startup_enabled)
            self._populating_form = False
        self.llm_secret_state.setText(
            "LLM 密钥：已配置" if snapshot.llm_secret_configured else "LLM 密钥：未配置"
        )
        self.vts_secret_state.setText(
            "VTS 令牌：已配置" if snapshot.vts_secret_configured else "VTS 令牌：未配置"
        )

    def _sync_features(self) -> None:
        last_result = self._model.last_result
        if last_result is not None and last_result.operation == "feature_set":
            for button in self._feature_buttons.values():
                button.setEnabled(True)
        for feature, item in self._feature_items.items():
            state = self._model.feature_states.get(feature)
            button = self._feature_buttons[feature]
            if state is None:
                item.setText(1, "—")
                item.setText(2, "不可用")
                item.setText(3, "private_state_runtime_disabled")
                button.setText("不可用")
                button.setEnabled(False)
                continue
            item.setText(1, "希望启用" if state.desired_enabled else "希望停用")
            item.setText(2, _ACTUAL_STATE_NAMES[state.actual_state])
            item.setText(3, state.reason_code or "—")
            button.setText(
                "启用"
                if not state.desired_enabled
                else (
                    "关闭并等待停止"
                    if feature in {FeatureName.vision, FeatureName.proactive}
                    else "停用"
                )
            )
            if state.actual_state in {
                FeatureActualState.enabling,
                FeatureActualState.disabling,
            }:
                button.setEnabled(False)
            elif not button.isEnabled() and self._model.last_result is None:
                button.setEnabled(True)

    def _sync_memory(self) -> None:
        self.memory_tree.blockSignals(True)
        self.memory_tree.clear()
        for summary in self._model.memory_items:
            item = QTreeWidgetItem(
                [
                    summary.content_preview,
                    summary.memory_type.value,
                    summary.sensitivity.value,
                    summary.updated_at.isoformat(timespec="seconds"),
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, summary.memory_id)
            self.memory_tree.addTopLevelItem(item)
        self.memory_tree.blockSignals(False)
        self.memory_list_notice.setText(
            "结果已截断为前 20 条。" if self._model.memory_list_truncated else ""
        )
        self.confirmation_tree.clear()
        for confirmation in self._model.confirmations:
            item = QTreeWidgetItem(
                [
                    confirmation.content_preview,
                    confirmation.evidence_preview,
                    confirmation.expires_at.isoformat(timespec="seconds"),
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, confirmation.confirmation_id)
            self.confirmation_tree.addTopLevelItem(item)
        detail = self._model.memory_detail
        if detail is None or detail.item is None:
            self.memory_detail.setEnabled(False)
            self.save_memory.setEnabled(False)
            self.delete_memory.setEnabled(False)
            self.memory_detail.clear()
            if detail is not None and detail.reason_code is not None:
                self.memory_detail_label.setText(f"记忆不可用：{detail.reason_code}")
            elif detail is None:
                self.memory_detail_label.setText("选择一项以查看或编辑。")
            return
        self.memory_detail.setEnabled(True)
        self.save_memory.setEnabled(True)
        self.delete_memory.setEnabled(True)
        detail_label = " / ".join(
            (
                detail.item.memory_type.value,
                detail.item.sensitivity.value,
                detail.item.status.value,
            )
        )
        self.memory_detail_label.setText(detail_label)
        if self.memory_detail.toPlainText() != detail.item.content:
            self.memory_detail.setPlainText(detail.item.content)

    def _sync_debug(self) -> None:
        debug = self._model.debug
        if debug is not None:
            self.debug_version.setText(debug.version)
            self.debug_capabilities.setText(
                f"文字聊天：{'可用' if debug.capabilities.text_chat else '不可用'}；"
                f"停止：{'可用' if debug.capabilities.turn_cancel else '不可用'}"
            )
            self.debug_queues.setText(
                f"命令 {debug.command_queue_count}/{debug.command_queue_capacity}；"
                f"事件 {debug.event_queue_count}/{debug.event_queue_capacity}"
            )
        result = self._model.last_result
        self.debug_error_code.setText(
            result.reason_code if result is not None and result.reason_code is not None else "无"
        )

    def _sync_result(self) -> None:
        result = self._model.last_result
        if result is None:
            return
        if result.reason_code is not None:
            reason = _MANAGEMENT_REASON_TEXT.get(
                result.reason_code,
                f"操作未完成（{result.reason_code}）。",
            )
            self.status_label.setText(reason)
            return
        suffix = "；重启后生效" if result.restart_required else ""
        if result.cleanup_pending:
            suffix += "；底层清理待处理"
        operation = _MANAGEMENT_OPERATION_TEXT.get(result.operation, result.operation)
        self.status_label.setText(f"操作完成：{operation}{suffix}")
        if result.operation == "settings_saved":
            self._form_dirty = False

    def clear_sensitive(self) -> None:
        """Erase dialog-held credentials, paths and memory bodies before final exit."""

        self._clearing_sensitive = True
        for edit in (
            self.llm_provider,
            self.llm_base_url,
            self.llm_model,
            self.tts_provider,
            self.tts_base_url,
            self.vts_uri,
            self.vts_plugin_name,
            self.vts_plugin_developer,
            self.stt_executable,
            self.stt_model_path,
            self.stt_device,
            self.llm_secret,
            self.vts_secret,
        ):
            edit.clear()
        self.memory_search.clear()
        self.memory_detail.clear()
        self.memory_tree.clear()
        self.confirmation_tree.clear()
        self.memory_detail_label.setText("")
        self.status_label.setText("")
