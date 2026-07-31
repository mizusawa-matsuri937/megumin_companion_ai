"""Accessible, bounded W16 settings and privacy-management dialog."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from app.schemas import FeatureActualState, FeatureName
from app.stt_runtime import (
    MANAGED_STT_PROFILE,
    SttRuntimeState,
    managed_stt_manifest,
    managed_stt_profiles,
    managed_stt_relative_paths,
)
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
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
    AudioOutputDevicesCommand,
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
    ProviderPreflightCommand,
    ProviderPreflightName,
    ProviderPreflightState,
    SecretRevokeCommand,
    SecretStoreCommand,
    SettingsSaveCommand,
    SttInstallCommand,
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
    "tts_provider_unsupported": "当前仅支持 mock、私有网关或兼容 GPT-SoVITS。",
    "tts_preset_required": "GPT-SoVITS 需要先配置默认 preset；请在 W19 配置向导完成预检。",
    "tts_reference_required": "GPT-SoVITS 需要填写 reference 资源。",
    "tts_reference_unavailable": "GPT-SoVITS 无法使用当前 reference 资源。",
    "tts_preset_unavailable": "GPT-SoVITS 默认 preset 端到端测试失败。",
    "tts_mock_active": "当前使用离线 Mock TTS，无需外部服务预检。",
    "tts_timeout": "GPT-SoVITS 预检超时。",
    "tts_unavailable": "GPT-SoVITS 服务不可用。",
    "tts_protocol_error": "目标地址不是受支持的 GPT-SoVITS API v2 服务。",
    "tts_closed": "GPT-SoVITS 预检连接已关闭。",
    "vts_disabled": "VTube Studio 当前未启用。",
    "vts_allow_or_auth_required": "请在 VTube Studio 内完成 Allow 或认证确认。",
    "vts_api_unavailable": "VTube Studio Plugin API 未启用或不可用。",
    "vts_auth_failed": "VTube Studio 授权未完成或已拒绝。",
    "vts_auth_revoked": "VTube Studio 授权已撤销，请重新 Allow。",
    "vts_model_missing": "VTube Studio 当前没有加载模型。",
    "vts_hotkey_missing": "当前模型缺少必需的 hotkey。",
    "vts_disconnected": "VTube Studio 连接中断。",
    "vts_timeout": "VTube Studio 预检超时。",
    "vts_unavailable": "VTube Studio 服务不可用。",
    "settings_invalid": "设置未通过本地 schema 校验。",
    "stt_platform_unsupported": "受管中文 STT 仅支持 Windows x64。",
    "stt_install_busy": "中文 STT 安装已在进行中。",
    "stt_download_failed": "中文 STT 运行时下载失败，请检查网络后重试。",
    "stt_download_too_large": "中文 STT 下载超过安全大小上限。",
    "stt_runtime_archive_invalid": "中文 STT 运行时压缩包不符合安全要求。",
    "stt_runtime_integrity_failed": "中文 STT 运行时完整性校验失败。",
    "stt_version_probe_failed": "中文 STT 运行时无法通过本地版本预检。",
    "stt_install_failed": "中文 STT 安装或修复失败。",
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
    "stt_runtime_installed": "中文离线 STT 已安装",
    "provider_preflight_completed": "TTS/VTS 联合预检已完成",
}
_PREFLIGHT_NAMES: dict[ProviderPreflightName, str] = {
    "tts_service": "GPT-SoVITS 服务",
    "tts_preset": "GPT-SoVITS 默认 preset",
    "tts_reference": "GPT-SoVITS reference",
    "vts_service": "VTube Studio API",
    "vts_authentication": "VTube Studio 授权",
    "vts_model": "VTube Studio 模型",
    "vts_hotkeys": "VTube Studio hotkey",
}
_PREFLIGHT_STATES = {
    ProviderPreflightState.pending: "待检查",
    ProviderPreflightState.running: "检查中",
    ProviderPreflightState.ready: "通过",
    ProviderPreflightState.skipped: "已跳过",
    ProviderPreflightState.failed: "失败",
    ProviderPreflightState.action_required: "等待用户操作",
    ProviderPreflightState.reconnecting: "连接中断/准备重连",
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
        self.tabs.addTab(self._build_avatar_tab(), "Avatar")
        self.tabs.addTab(self._build_preflight_tab(), "服务预检")
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
        self.tts_preset_name = self._line_edit("TTS 默认 preset 名称")
        self.tts_ref_audio_path = self._line_edit("TTS reference 资源")
        self.tts_ref_audio_scope = QComboBox(page)
        self.tts_ref_audio_scope.setAccessibleName("TTS reference 资源范围")
        self.tts_ref_audio_scope.addItem("由 GPT-SoVITS 服务解释", "service_resource")
        self.tts_ref_audio_scope.addItem("本机 loopback 普通文件", "local_file")
        self.tts_ref_audio_scope.currentIndexChanged.connect(self._mark_form_dirty_index)
        self.tts_prompt_text = self._line_edit("TTS reference 提示文本")
        self.tts_prompt_lang = self._line_edit("TTS reference 提示语言")
        self.vts_enabled = QCheckBox("启用 VTube Studio（重启后生效）", page)
        self.vts_uri = self._line_edit("VTS 地址")
        self.vts_plugin_name = self._line_edit("VTS 插件名称")
        self.vts_plugin_developer = self._line_edit("VTS 开发者名称")
        self.stt_enabled = QCheckBox("启用本地语音输入配置（W18 后生效）", page)
        self.stt_profile = QComboBox(page)
        self.stt_profile.setAccessibleName("受管 Whisper 模型配置档")
        self._sync_stt_profiles(MANAGED_STT_PROFILE)
        self.stt_profile.currentIndexChanged.connect(self._select_stt_profile)
        self.stt_profile_hint = QLabel(
            "选择已登记配置档会填入匹配的受管路径；手动修改路径仍可用，但会显示为非受管。",
            page,
        )
        self.stt_profile_hint.setWordWrap(True)
        self.stt_executable = self._line_edit("Whisper 可执行文件")
        self.stt_model_path = self._line_edit("Whisper 模型")
        self.stt_device = self._line_edit("音频设备")
        self.stt_runtime_status = QLabel("中文离线 STT：正在检查", page)
        self.stt_runtime_status.setWordWrap(True)
        self.stt_runtime_status.setAccessibleName("内置中文离线 STT 运行时状态")
        self.install_stt_runtime = QPushButton("安装/修复受管中文 Whisper STT", page)
        self.install_stt_runtime.setAccessibleName("安装或修复内置中文离线 STT")
        self.install_stt_runtime.clicked.connect(self._install_chinese_stt)
        self.output_device = QComboBox(page)
        self.output_device.setAccessibleName("播放输出设备")
        self.output_device.currentIndexChanged.connect(self._mark_form_dirty_index)
        self.system_playback_enabled = QCheckBox("启用本地系统播放（重启后生效）", page)
        self.system_playback_enabled.toggled.connect(self._mark_form_dirty_bool)
        self.audio_device_hint = QLabel("尚未读取播放设备。", page)
        self.audio_device_hint.setWordWrap(True)
        refresh_audio_devices = QPushButton("刷新播放设备", page)
        refresh_audio_devices.clicked.connect(self._refresh_audio_devices)
        output_device_row = QWidget(page)
        output_device_layout = QHBoxLayout(output_device_row)
        output_device_layout.setContentsMargins(0, 0, 0, 0)
        output_device_layout.addWidget(self.output_device, 1)
        output_device_layout.addWidget(refresh_audio_devices)
        self.startup_enabled = QCheckBox("登录后自动启动", page)
        for checkbox in (self.vts_enabled, self.stt_enabled, self.startup_enabled):
            checkbox.toggled.connect(self._mark_form_dirty_bool)
        form.addRow("LLM 提供方", self.llm_provider)
        form.addRow("LLM 基础地址", self.llm_base_url)
        form.addRow("LLM 模型", self.llm_model)
        form.addRow("TTS 提供方", self.tts_provider)
        form.addRow("TTS 基础地址", self.tts_base_url)
        form.addRow("TTS 默认 preset", self.tts_preset_name)
        form.addRow("TTS reference", self.tts_ref_audio_path)
        form.addRow("TTS reference 范围", self.tts_ref_audio_scope)
        form.addRow("TTS reference 文本", self.tts_prompt_text)
        form.addRow("TTS reference 语言", self.tts_prompt_lang)
        form.addRow("VTS", self.vts_enabled)
        form.addRow("VTS URI", self.vts_uri)
        form.addRow("VTS 插件名称", self.vts_plugin_name)
        form.addRow("VTS 开发者名称", self.vts_plugin_developer)
        form.addRow("本地 STT", self.stt_enabled)
        form.addRow("受管 Whisper 模型", self.stt_profile)
        form.addRow("配置档说明", self.stt_profile_hint)
        form.addRow("STT 可执行文件", self.stt_executable)
        form.addRow("STT 模型", self.stt_model_path)
        form.addRow("STT 设备（编号或名称）", self.stt_device)
        form.addRow("中文 STT 运行时", self.stt_runtime_status)
        form.addRow("中文 STT 安装", self.install_stt_runtime)
        form.addRow("本地系统播放", self.system_playback_enabled)
        form.addRow("播放输出设备", output_device_row)
        form.addRow("播放设备说明", self.audio_device_hint)
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

    def _build_avatar_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        guidance = QLabel(
            "这里只保存 Avatar Runtime 的安全开关和音量口型校准值；"
            "不显示或修改私人模型、动作、Expression、Hotkey ID 或资产路径。"
            "所有更改均在重启应用后生效。",
            page,
        )
        guidance.setWordWrap(True)
        layout.addWidget(guidance)

        controls = QGroupBox("Avatar Runtime 安全开关", page)
        controls_form = QFormLayout(controls)
        self.avatar_enabled = QCheckBox("启用 Avatar Runtime", controls)
        self.avatar_parameter_control_enabled = QCheckBox("启用 VTS 参数控制", controls)
        self.avatar_micro_motion_enabled = QCheckBox("启用程序微动作", controls)
        self.avatar_lip_sync_enabled = QCheckBox("启用音量口型", controls)
        self.avatar_body_motion_enabled = QCheckBox("启用主体动作", controls)
        self.avatar_auto_red_eye_enabled = QCheckBox("启用系统自动红眼", controls)
        for checkbox in (
            self.avatar_enabled,
            self.avatar_parameter_control_enabled,
            self.avatar_micro_motion_enabled,
            self.avatar_lip_sync_enabled,
            self.avatar_body_motion_enabled,
            self.avatar_auto_red_eye_enabled,
        ):
            checkbox.toggled.connect(self._mark_form_dirty_bool)
        controls_form.addRow("总开关", self.avatar_enabled)
        controls_form.addRow("参数控制", self.avatar_parameter_control_enabled)
        controls_form.addRow("程序微动作", self.avatar_micro_motion_enabled)
        controls_form.addRow("音量口型", self.avatar_lip_sync_enabled)
        controls_form.addRow("主体动作", self.avatar_body_motion_enabled)
        controls_form.addRow("自动红眼", self.avatar_auto_red_eye_enabled)
        layout.addWidget(controls)

        calibration = QGroupBox("音量口型校准", page)
        calibration_form = QFormLayout(calibration)
        self.avatar_mouth_noise_floor = self._avatar_spin_box(
            calibration,
            accessible_name="Avatar 口型噪声门限",
            minimum=0.0,
            maximum=0.999,
            decimals=3,
            step=0.005,
        )
        self.avatar_mouth_gain = self._avatar_spin_box(
            calibration,
            accessible_name="Avatar 口型增益",
            minimum=0.01,
            maximum=100.0,
            decimals=2,
            step=0.25,
        )
        self.avatar_mouth_attack_seconds = self._avatar_spin_box(
            calibration,
            accessible_name="Avatar 口型开启平滑时间",
            minimum=0.001,
            maximum=2.0,
            decimals=3,
            step=0.01,
            suffix=" 秒",
        )
        self.avatar_mouth_release_seconds = self._avatar_spin_box(
            calibration,
            accessible_name="Avatar 口型闭合平滑时间",
            minimum=0.001,
            maximum=5.0,
            decimals=3,
            step=0.01,
            suffix=" 秒",
        )
        calibration_form.addRow("噪声门限（0–1）", self.avatar_mouth_noise_floor)
        calibration_form.addRow("增益", self.avatar_mouth_gain)
        calibration_form.addRow("开启平滑", self.avatar_mouth_attack_seconds)
        calibration_form.addRow("闭合平滑", self.avatar_mouth_release_seconds)
        layout.addWidget(calibration)

        save = QPushButton("保存 Avatar 设置（重启后生效）", page)
        save.setAccessibleName("保存 Avatar 设置")
        save.clicked.connect(self._save_settings)
        layout.addWidget(save)
        layout.addStretch(1)
        return page

    def _build_preflight_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        guidance = QLabel(
            "联合预检只读取已保存设置。GPT-SoVITS 会收到固定短语“连接测试”，"
            "生成的 WAV 不播放并立即清理；VTube Studio 首次授权或授权失效时，"
            "必须在 VTube Studio 内完成 Allow。结果不显示 token、模型/hotkey ID、"
            "reference 路径、prompt 或 provider 响应正文。",
            page,
        )
        guidance.setWordWrap(True)
        layout.addWidget(guidance)
        group = QGroupBox("分阶段状态", page)
        grid = QGridLayout(group)
        self.preflight_labels: dict[ProviderPreflightName, QLabel] = {}
        for row, (name, title) in enumerate(_PREFLIGHT_NAMES.items()):
            grid.addWidget(QLabel(title, group), row, 0)
            value = QLabel("待运行", group)
            value.setAccessibleName(f"{title}预检状态")
            value.setWordWrap(True)
            grid.addWidget(value, row, 1)
            self.preflight_labels[name] = value
        layout.addWidget(group)
        self.run_provider_preflight = QPushButton("运行 TTS/VTS 联合预检", page)
        self.run_provider_preflight.setAccessibleName("运行 TTS 和 VTube Studio 联合预检")
        self.run_provider_preflight.clicked.connect(self._run_provider_preflight)
        layout.addWidget(self.run_provider_preflight)
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
        self.debug_avatar = QLabel("—", page)
        self.debug_avatar.setWordWrap(True)
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
        layout.addRow("Avatar", self.debug_avatar)
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

    def _avatar_spin_box(
        self,
        parent: QWidget,
        *,
        accessible_name: str,
        minimum: float,
        maximum: float,
        decimals: int,
        step: float,
        suffix: str = "",
    ) -> QDoubleSpinBox:
        control = QDoubleSpinBox(parent)
        control.setAccessibleName(accessible_name)
        control.setRange(minimum, maximum)
        control.setDecimals(decimals)
        control.setSingleStep(step)
        control.setKeyboardTracking(False)
        control.setSuffix(suffix)
        control.valueChanged.connect(self._mark_form_dirty_float)
        return control

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

    def _mark_form_dirty_index(self, _index: int) -> None:
        if not self._populating_form:
            self._form_dirty = True

    def _mark_form_dirty_float(self, _value: float) -> None:
        if not self._populating_form:
            self._form_dirty = True

    def _submit(self, command: ManagementCommand) -> bool:
        accepted = self._submit_command(command)
        if not accepted:
            self.status_label.setText("命令队列已满；未执行该操作。")
        return accepted

    def _refresh(self) -> None:
        self._submit(ManagementRefreshCommand())

    def _refresh_audio_devices(self) -> None:
        self._submit(AudioOutputDevicesCommand())

    def _run_provider_preflight(self) -> None:
        if self._model.settings is None:
            self.status_label.setText("设置尚未加载。")
            return
        if self._form_dirty:
            self.status_label.setText("请先保存设置，再运行 TTS/VTS 联合预检。")
            return
        if not self._confirm(
            "运行服务预检",
            "将使用已保存设置连接 GPT-SoVITS 与 VTube Studio。GPT-SoVITS 会合成固定短语"
            "“连接测试”，测试 WAV 不播放并立即清理；VTube Studio 可能要求你在其窗口中"
            "点击 Allow。是否继续？",
        ):
            return
        if self._submit(ProviderPreflightCommand()):
            self.run_provider_preflight.setEnabled(False)
            self.status_label.setText("正在运行 TTS/VTS 联合预检…")

    def _selected_stt_profile(self) -> str:
        value = self.stt_profile.currentData()
        return value if isinstance(value, str) else MANAGED_STT_PROFILE

    def _sync_stt_profiles(self, selected_profile: str) -> None:
        self.stt_profile.blockSignals(True)
        self.stt_profile.clear()
        for manifest in managed_stt_profiles():
            self.stt_profile.addItem(manifest.display_name, manifest.profile)
        selected_index = self.stt_profile.findData(selected_profile)
        self.stt_profile.setCurrentIndex(max(0, selected_index))
        self.stt_profile.blockSignals(False)

    def _select_stt_profile(self, index: int) -> None:
        if self._populating_form or index < 0:
            return
        try:
            executable, model_path = managed_stt_relative_paths(self._selected_stt_profile())
        except ValueError:
            self.status_label.setText("受管 Whisper 配置档无效。")
            return
        self.stt_executable.setText(executable.as_posix())
        self.stt_model_path.setText(model_path.as_posix())
        self._mark_form_dirty_index(index)

    def _install_chinese_stt(self) -> None:
        form = self._model.settings.form if self._model.settings is not None else None
        if form is None:
            self.status_label.setText("设置尚未加载。")
            return
        if self._form_dirty:
            self.status_label.setText("请先保存设置，再安装或修复所选 Whisper 配置档。")
            return
        try:
            profile_name = managed_stt_manifest(form.stt_profile).display_name
        except ValueError:
            self.status_label.setText("受管 Whisper 配置档无效。")
            return
        if not self._confirm(
            "安装中文离线 STT",
            f"将从受管清单指定的公开来源下载「{profile_name}」所需的 whisper.cpp 运行时和模型。"
            "下载内容会校验完整性并只保存到当前 Windows 用户的本地目录；不会访问麦克风，"
            "也不会自动启用语音输入。是否继续？",
        ):
            return
        if self._submit(SttInstallCommand()):
            self.install_stt_runtime.setEnabled(False)
            self.stt_runtime_status.setText(f"中文离线 STT（{profile_name}）：安装中…")

    def _selected_output_device_id(self) -> str:
        value = self.output_device.currentData()
        return value if isinstance(value, str) else ""

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
                        tts_preset_name=self.tts_preset_name.text(),
                        tts_ref_audio_path=self.tts_ref_audio_path.text(),
                        tts_ref_audio_scope=self._selected_tts_ref_audio_scope(),
                        tts_prompt_text=self.tts_prompt_text.text(),
                        tts_prompt_lang=self.tts_prompt_lang.text(),
                        vts_enabled=self.vts_enabled.isChecked(),
                        vts_uri=self.vts_uri.text(),
                        vts_plugin_name=self.vts_plugin_name.text(),
                        vts_plugin_developer=self.vts_plugin_developer.text(),
                        stt_enabled=self.stt_enabled.isChecked(),
                        stt_profile=self._selected_stt_profile(),
                        stt_executable=self.stt_executable.text(),
                        stt_model_path=self.stt_model_path.text(),
                        stt_device=self.stt_device.text(),
                        startup_enabled=self.startup_enabled.isChecked(),
                        output_device_id=self._selected_output_device_id(),
                        system_playback_enabled=self.system_playback_enabled.isChecked(),
                        avatar_enabled=self.avatar_enabled.isChecked(),
                        avatar_parameter_control_enabled=(
                            self.avatar_parameter_control_enabled.isChecked()
                        ),
                        avatar_micro_motion_enabled=self.avatar_micro_motion_enabled.isChecked(),
                        avatar_lip_sync_enabled=self.avatar_lip_sync_enabled.isChecked(),
                        avatar_body_motion_enabled=self.avatar_body_motion_enabled.isChecked(),
                        avatar_auto_red_eye_enabled=self.avatar_auto_red_eye_enabled.isChecked(),
                        avatar_mouth_noise_floor=self.avatar_mouth_noise_floor.value(),
                        avatar_mouth_gain=self.avatar_mouth_gain.value(),
                        avatar_mouth_attack_seconds=self.avatar_mouth_attack_seconds.value(),
                        avatar_mouth_release_seconds=self.avatar_mouth_release_seconds.value(),
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
        self._sync_provider_preflight()
        self._sync_features()
        self._sync_memory()
        self._sync_debug()
        self._sync_result()

    def _sync_settings(self) -> None:
        snapshot = self._model.settings
        if snapshot is None:
            return
        form = snapshot.form
        if not self._form_dirty:
            self._populating_form = True
            self._sync_stt_profiles(form.stt_profile)
            for widget, value in (
                (self.llm_provider, form.llm_provider),
                (self.llm_base_url, form.llm_base_url),
                (self.llm_model, form.llm_model),
                (self.tts_provider, form.tts_provider),
                (self.tts_base_url, form.tts_base_url),
                (self.tts_preset_name, form.tts_preset_name),
                (self.tts_ref_audio_path, form.tts_ref_audio_path),
                (self.tts_prompt_text, form.tts_prompt_text),
                (self.tts_prompt_lang, form.tts_prompt_lang),
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
            self.system_playback_enabled.setChecked(form.system_playback_enabled)
            self.avatar_enabled.setChecked(form.avatar_enabled)
            self.avatar_parameter_control_enabled.setChecked(form.avatar_parameter_control_enabled)
            self.avatar_micro_motion_enabled.setChecked(form.avatar_micro_motion_enabled)
            self.avatar_lip_sync_enabled.setChecked(form.avatar_lip_sync_enabled)
            self.avatar_body_motion_enabled.setChecked(form.avatar_body_motion_enabled)
            self.avatar_auto_red_eye_enabled.setChecked(form.avatar_auto_red_eye_enabled)
            self.avatar_mouth_noise_floor.setValue(form.avatar_mouth_noise_floor)
            self.avatar_mouth_gain.setValue(form.avatar_mouth_gain)
            self.avatar_mouth_attack_seconds.setValue(form.avatar_mouth_attack_seconds)
            self.avatar_mouth_release_seconds.setValue(form.avatar_mouth_release_seconds)
            scope_index = self.tts_ref_audio_scope.findData(form.tts_ref_audio_scope)
            self.tts_ref_audio_scope.setCurrentIndex(max(0, scope_index))
            self._populating_form = False
        selected_device_id = (
            self._selected_output_device_id() if self._form_dirty else form.output_device_id
        )
        self._sync_audio_output_devices(selected_device_id)
        self.llm_secret_state.setText(
            "LLM 密钥：已配置" if snapshot.llm_secret_configured else "LLM 密钥：未配置"
        )
        self.vts_secret_state.setText(
            "VTS 令牌：已配置" if snapshot.vts_secret_configured else "VTS 令牌：未配置"
        )
        runtime_state = snapshot.stt_runtime.state
        runtime_text = {
            SttRuntimeState.missing: "未安装",
            SttRuntimeState.installing: "安装中",
            SttRuntimeState.verified: "已验证，可离线使用",
            SttRuntimeState.integrity_failed: "完整性校验失败，可执行修复",
            SttRuntimeState.unmanaged: "使用手动配置的非受管运行时",
            SttRuntimeState.unsupported: "当前平台不受支持",
        }[runtime_state]
        try:
            profile_name = managed_stt_manifest(snapshot.stt_runtime.profile).display_name
        except ValueError:
            profile_name = snapshot.stt_runtime.profile
        self.stt_runtime_status.setText(f"中文离线 STT（{profile_name}）：{runtime_text}")
        self.install_stt_runtime.setEnabled(runtime_state is not SttRuntimeState.installing)

    def _selected_tts_ref_audio_scope(
        self,
    ) -> Literal["service_resource", "local_file"]:
        value = self.tts_ref_audio_scope.currentData()
        return "local_file" if value == "local_file" else "service_resource"

    def _sync_provider_preflight(self) -> None:
        checks = self._model.provider_preflight_checks
        for name, label in self.preflight_labels.items():
            check = checks.get(name)
            if check is None:
                label.setText("待运行")
                continue
            text = _PREFLIGHT_STATES[check.state]
            if check.missing_count:
                text += f"（缺少 {check.missing_count} 项）"
            if check.reason_code is not None:
                reason = _MANAGEMENT_REASON_TEXT.get(
                    check.reason_code,
                    check.reason_code,
                )
                text += f"：{reason}"
            label.setText(text)

    def _sync_audio_output_devices(self, selected_device_id: str) -> None:
        self.output_device.blockSignals(True)
        self.output_device.clear()
        self.output_device.addItem("系统默认输出", "")
        for device in self._model.audio_output_devices:
            label = device.label + ("（系统默认）" if device.is_default else "")
            self.output_device.addItem(label, device.device_id)
        selected_index = self.output_device.findData(selected_device_id)
        if selected_device_id and selected_index < 0:
            self.output_device.addItem("已保存的设备（当前未在列表中）", selected_device_id)
            selected_index = self.output_device.count() - 1
        self.output_device.setCurrentIndex(max(0, selected_index))
        self.output_device.blockSignals(False)
        if self._model.audio_output_devices_reason is not None:
            self.audio_device_hint.setText(
                "无法读取播放设备；下次播放将使用系统默认输出（"
                f"{self._model.audio_output_devices_reason}）。"
            )
        elif self._model.audio_output_devices_truncated:
            self.audio_device_hint.setText(
                "设备列表已截断；保存后下次启动生效，设备消失时将回退系统默认输出。"
            )
        else:
            self.audio_device_hint.setText(
                "保存后下次启动生效；若所选设备消失，MediaWorker 会回退系统默认输出。"
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
            layer_labels = {
                "parameter_control": "参数控制",
                "lip_sync": "音量口型",
                "body_motion": "主体动作",
                "automatic_red_eye": "自动红眼",
            }
            self.debug_avatar.setText(
                "；".join(
                    (
                        f"{layer_labels[layer.name]}："
                        f"{'可用' if layer.available else '不可用'}"
                        f"{f'（{layer.reason_code}）' if layer.reason_code else ''}"
                    )
                    for layer in debug.avatar_layers
                )
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
            if result.operation == "stt_runtime_install":
                self.install_stt_runtime.setEnabled(True)
            if result.operation == "provider_preflight":
                self.run_provider_preflight.setEnabled(True)
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
        if result.operation == "provider_preflight_completed":
            self.run_provider_preflight.setEnabled(True)

    def clear_sensitive(self) -> None:
        """Erase dialog-held credentials, paths and memory bodies before final exit."""

        self._clearing_sensitive = True
        for edit in (
            self.llm_provider,
            self.llm_base_url,
            self.llm_model,
            self.tts_provider,
            self.tts_base_url,
            self.tts_preset_name,
            self.tts_ref_audio_path,
            self.tts_prompt_text,
            self.tts_prompt_lang,
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
        self.output_device.clear()
        self.tts_ref_audio_scope.clear()
        for label in self.preflight_labels.values():
            label.setText("")
        self.audio_device_hint.setText("")
        self.memory_search.clear()
        self.memory_detail.clear()
        self.memory_tree.clear()
        self.confirmation_tree.clear()
        self.memory_detail_label.setText("")
        self.status_label.setText("")
