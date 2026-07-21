"""W15 tray controls with a deliberately small, non-command-bearing surface."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMenu,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
)

from desktop_client.ui.window import MainWindow


class PrivacyOverviewDialog(QDialog):
    """Static disclosure; it intentionally never reads transcript or settings values."""

    def __init__(self, parent: MainWindow) -> None:
        super().__init__(parent)
        self.setWindowTitle("隐私总览")
        self.setAccessibleName("隐私总览")
        layout = QVBoxLayout(self)
        overview = QLabel(
            "未发送草稿和当前窗口内容只保留在内存中，并会在退出时清除。\n\n"
            "已发送文字仅会按你的已配置服务进入对话流程；视觉、云视觉和主动发话默认关闭。"
            "发生异常退出后，下一次启动会进入安全模式，不会自动恢复视觉或主动功能。\n\n"
            "此页面不显示消息、密钥、文件路径或服务地址。",
            self,
        )
        overview.setWordWrap(True)
        overview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(overview)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self)
        buttons.rejected.connect(self.close)
        buttons.accepted.connect(self.close)
        layout.addWidget(buttons)


class DesktopTrayController(QObject):
    """Own one tray icon and five fixed actions; no action accepts user payloads."""

    def __init__(
        self,
        app: QApplication,
        window: MainWindow,
        *,
        on_exit: Callable[[], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent or app)
        self._window = window
        self._on_exit = on_exit
        self._tray: QSystemTrayIcon | None = None
        self._menu: QMenu | None = None
        self._stop_action: QAction | None = None
        self._recovery_timer: QTimer | None = None
        self._privacy_dialog: PrivacyOverviewDialog | None = None
        self._shutting_down = False
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        tray = QSystemTrayIcon(self)
        tray.setObjectName("desktop-tray")
        tray.setIcon(app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        tray.setToolTip("Megumin Companion（关闭窗口会隐藏到此处）")
        # QMenu needs a QWidget parent (the tray icon is only a QObject).
        # Keep a Python reference as well so the menu survives for the icon's
        # entire lifecycle.
        menu = QMenu(parent=window)
        show_action = menu.addAction("显示窗口")
        hide_action = menu.addAction("隐藏窗口")
        stop_action = menu.addAction("停止当前回复")
        privacy_action = menu.addAction("隐私总览")
        menu.addSeparator()
        exit_action = menu.addAction("退出")
        show_action.triggered.connect(lambda _checked=False: self._window.show_and_activate())
        hide_action.triggered.connect(lambda _checked=False: self._window.hide())
        stop_action.triggered.connect(lambda _checked=False: self._window.stop_current_turn())
        privacy_action.triggered.connect(lambda _checked=False: self.show_privacy_overview())
        exit_action.triggered.connect(lambda _checked=False: self._on_exit())
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_activated)
        self._tray = tray
        self._menu = menu
        self._stop_action = stop_action
        recovery_timer = QTimer(self)
        recovery_timer.setInterval(5_000)
        recovery_timer.timeout.connect(self._refresh_visible_icon)
        self._recovery_timer = recovery_timer

    @property
    def available(self) -> bool:
        return self._tray is not None

    def show(self) -> None:
        if self._tray is None or self._shutting_down:
            return
        self._refresh_visible_icon()
        if self._recovery_timer is not None:
            self._recovery_timer.start()

    def prepare_shutdown(self) -> None:
        self._shutting_down = True
        if self._recovery_timer is not None:
            self._recovery_timer.stop()
        if self._privacy_dialog is not None:
            self._privacy_dialog.close()
        if self._tray is not None:
            self._tray.hide()

    def show_privacy_overview(self) -> None:
        dialog = self._privacy_dialog
        if dialog is None:
            dialog = PrivacyOverviewDialog(self._window)
            dialog.finished.connect(self._clear_privacy_dialog)
            self._privacy_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _clear_privacy_dialog(self, _result: int = 0) -> None:
        self._privacy_dialog = None

    def _refresh_visible_icon(self) -> None:
        tray = self._tray
        if tray is None or self._shutting_down:
            return
        # Calling show again is idempotent and makes Qt recreate the shell icon
        # after Explorer restarts without accepting any external command.
        tray.show()
        if self._stop_action is not None:
            self._stop_action.setEnabled(self._window.can_stop_current_turn)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            if self._window.isVisible():
                self._window.hide()
            else:
                self._window.show_and_activate()
