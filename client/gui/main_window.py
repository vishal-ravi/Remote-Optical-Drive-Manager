"""Main application window: host selection, drive panel and feature tabs."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGroupBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from common.logging_setup import format_bytes, setup_logging
from common.protocol import DriveInfo

from ..profiles import Profile, ProfileStore
from ..transport import ClientSession
from .dialogs import ProfileDialog
from .tabs import BrowseTab, BurnTab, ImageTab, LogTab, QtLogHandler, QueueTab
from .workers import FnTask, TaskHost

log = logging.getLogger("rodm.client.gui")

DRIVE_ROLE = Qt.ItemDataRole.UserRole


class MainWindow(QMainWindow, TaskHost):
    def __init__(self, store: ProfileStore | None = None) -> None:
        super().__init__()
        self.store = store or ProfileStore()
        self.session: ClientSession | None = None
        self.current_profile: Profile | None = None
        self.drives: list[DriveInfo] = []
        self._connect_task: FnTask | None = None
        self._drives_task: FnTask | None = None

        self.setWindowTitle("Remote Optical Drive Manager")
        self.resize(1180, 780)

        self._build_toolbar()
        self._build_central()
        self._build_statusbar()
        self._build_menu()
        self._attach_logging()

        self._reload_profiles()
        QTimer.singleShot(0, self._initial_connect_hint)

    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Connection", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        toolbar.addWidget(QLabel(" Host: "))
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(220)
        self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        toolbar.addWidget(self.profile_combo)

        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self.toggle_connection)
        toolbar.addWidget(self.connect_button)

        self.manage_button = QPushButton("Profiles…")
        self.manage_button.clicked.connect(self.manage_profiles)
        toolbar.addWidget(self.manage_button)

        toolbar.addSeparator()

        self.refresh_button = QPushButton("Refresh drives")
        self.refresh_button.clicked.connect(self.refresh_drives)
        self.refresh_button.setEnabled(False)
        toolbar.addWidget(self.refresh_button)

        self.eject_button = QPushButton("Eject")
        self.eject_button.clicked.connect(self.eject_drive)
        self.eject_button.setEnabled(False)
        toolbar.addWidget(self.eject_button)

        self.connection_label = QLabel(" offline ")
        self.connection_label.setStyleSheet(
            "QLabel { color: #888; padding: 2px 8px; }"
        )
        toolbar.addWidget(self.connection_label)

    def _build_central(self) -> None:
        # -- left: drives ------------------------------------------------ #
        self.drives_list = QListWidget()
        self.drives_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.drives_list.currentItemChanged.connect(self._on_drive_selected)

        self.details_label = QLabel("Not connected.")
        self.details_label.setWordWrap(True)
        self.details_label.setTextFormat(Qt.TextFormat.PlainText)

        details_box = QGroupBox("Drive status", self)
        details_layout = QVBoxLayout(details_box)
        details_layout.addWidget(self.details_label, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(4, 4, 4, 4)
        left_layout.addWidget(QLabel("Optical drives"))
        left_layout.addWidget(self.drives_list, 1)
        left_layout.addWidget(details_box)

        # -- right: tabs -------------------------------------------------- #
        self.tabs = QTabWidget()
        self.browse_tab = BrowseTab(self)
        self.image_tab = ImageTab(self)
        self.burn_tab = BurnTab(self)
        self.queue_tab = QueueTab(self)
        self.log_tab = LogTab()
        self.tabs.addTab(self.browse_tab, "Browse")
        self.tabs.addTab(self.image_tab, "Image")
        self.tabs.addTab(self.burn_tab, "Burn")
        self.tabs.addTab(self.queue_tab, "Queue")
        self.tabs.addTab(self.log_tab, "Log")

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 880])
        self.setCentralWidget(splitter)

    def _build_statusbar(self) -> None:
        status = QStatusBar()
        self.setStatusBar(status)
        self.status_label = QLabel("Ready")
        self.job_label = QLabel("")
        status.addWidget(self.status_label, 1)
        status.addPermanentWidget(self.job_label)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        manage = QAction("Manage profiles…", self)
        manage.triggered.connect(self.manage_profiles)
        file_menu.addAction(manage)
        file_menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        help_menu = self.menuBar().addMenu("&Help")
        about = QAction("&About", self)
        about.triggered.connect(self._about)
        help_menu.addAction(about)

    def _attach_logging(self) -> None:
        setup_logging("rodm", log_dir=_client_log_dir(), level=logging.INFO)
        self.log_handler = QtLogHandler(logging.DEBUG)
        self.log_handler.setLevel(logging.DEBUG)
        logging.getLogger("rodm").addHandler(self.log_handler)
        logging.getLogger("rodm").setLevel(logging.DEBUG)
        logging.getLogger("rodm.client").propagate = True
        self.log_handler.recorded.connect(self.log_tab.append)

    # ------------------------------------------------------------------ #
    # profiles
    # ------------------------------------------------------------------ #

    def _reload_profiles(self) -> None:
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        try:
            self.profiles = self.store.load()
        except ValueError as exc:
            log.error("cannot load profiles: %s", exc)
            self.profiles = []
        for profile in self.profiles:
            self.profile_combo.addItem(
                f"{profile.name}  ({profile.ssh_target})", profile.id
            )
        self.profile_combo.blockSignals(False)
        if self.profiles:
            self.profile_combo.setCurrentIndex(0)
            self._on_profile_changed()

    def selected_profile(self) -> Profile | None:
        index = self.profile_combo.currentIndex()
        if index < 0 or index >= len(self.profiles):
            return None
        return self.profiles[index]

    def _on_profile_changed(self, *_args) -> None:
        if self.session is not None and self.session.connected:
            self.disconnect_session(silent=True)
        profile = self.selected_profile()
        if profile is None:
            self.connection_label.setText(" offline ")
            return
        self.connection_label.setText(f" {profile.name}: not connected ")

    def manage_profiles(self) -> None:
        profile = self.selected_profile()
        dialog = ProfileDialog(profile, self)
        if dialog.exec() and dialog.result_profile is not None:
            saved = self.store.upsert(dialog.result_profile)
            log.info("saved profile %s (%s)", saved.name, saved.host)
            self._reload_profiles()
            for index, item_profile in enumerate(self.profiles):
                if item_profile.id == saved.id:
                    self.profile_combo.setCurrentIndex(index)
                    break

    # ------------------------------------------------------------------ #
    # connection
    # ------------------------------------------------------------------ #

    def toggle_connection(self) -> None:
        if self.session is not None and self.session.connected:
            self.disconnect_session()
        else:
            self.connect_session()

    def connect_session(self) -> None:
        profile = self.selected_profile()
        if profile is None:
            QMessageBox.information(
                self, "Connect", "Create a host profile first (Profiles…)."
            )
            return
        if self._connect_task is not None and self._connect_task.isRunning():
            return
        self.connect_button.setEnabled(False)
        self.connect_button.setText("Connecting…")
        self.show_status(f"Connecting to {profile.ssh_target} …")
        self.log(f"connecting to {profile.ssh_target}")

        session = ClientSession(profile, timeout=15)

        def run() -> dict:
            session.connect()
            version = session.call("version", timeout=15)
            drives = session.call("list_drives", timeout=20)
            return {"version": version, "drives": drives, "status": session.status()}

        task = FnTask(run)
        task.done.connect(lambda result: self._on_connected(profile, session, result))
        task.failed.connect(self._on_connect_failed)
        self._connect_task = self.start_task(task)

    def _on_connected(self, profile: Profile, session: ClientSession, result: dict) -> None:
        self.session = session
        self.current_profile = profile
        self.connect_button.setEnabled(True)
        self.connect_button.setText("Disconnect")
        self.refresh_button.setEnabled(True)

        status = result.get("status", {})
        transport = status.get("transport", "?")
        errors = status.get("errors") or []
        for message in errors:
            self.log(message, "warning")
        version = (result.get("version") or {}).get("version", "?")
        self.connection_label.setText(
            f" {profile.name}: {transport.upper()} (agent {version}) "
        )
        self.connection_label.setStyleSheet(
            "QLabel { color: #3a9a3a; padding: 2px 8px; font-weight: bold; }"
        )
        self.show_status(f"Connected to {profile.name} via {transport.upper()}")
        self.log(f"connected via {transport} (agent {version})")
        if errors:
            self.show_status(f"Connected via {transport}, but: {errors[0]}")

        # persist trust material discovered during the handshake
        self.store.upsert(profile)

        self._apply_drives(result.get("drives") or {})
        if getattr(self, "image_tab", None) is not None:
            self.image_tab.refresh_images()
            self.burn_tab.refresh_images()
            self.queue_tab.refresh_images()

    def _on_connect_failed(self, code: str, message: str) -> None:
        self.connect_button.setEnabled(True)
        self.connect_button.setText("Connect")
        self.connection_label.setText(" offline ")
        self.connection_label.setStyleSheet(
            "QLabel { color: #b03030; padding: 2px 8px; }"
        )
        self.show_status(f"Connection failed: {message}")
        self.log(f"connection failed [{code}]: {message}", "error")
        QMessageBox.warning(self, "Connect", f"[{code}]\n{message}")

    def disconnect_session(self, silent: bool = False) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None
        self.current_profile = None
        self.connect_button.setText("Connect")
        self.connect_button.setEnabled(True)
        self.refresh_button.setEnabled(False)
        self.eject_button.setEnabled(False)
        self.connection_label.setText(" offline ")
        self.connection_label.setStyleSheet(
            "QLabel { color: #888; padding: 2px 8px; }"
        )
        self.drives_list.clear()
        self.details_label.setText("Not connected.")
        for tab in (self.browse_tab, self.image_tab, self.burn_tab, self.queue_tab):
            tab.set_device("")
        if not silent:
            self.show_status("Disconnected")
            self.log("disconnected")

    # ------------------------------------------------------------------ #
    # drives
    # ------------------------------------------------------------------ #

    def refresh_drives(self) -> None:
        if self.session is None or not self.session.connected:
            return
        if self._drives_task is not None and self._drives_task.isRunning():
            return
        session = self.session
        task = FnTask(lambda: session.call("list_drives", timeout=20))
        task.done.connect(lambda result: self._apply_drives(result))
        task.failed.connect(
            lambda code, message: self.show_status(f"Drive scan failed: {message}")
        )
        self._drives_task = self.start_task(task)

    def _apply_drives(self, payload: dict) -> None:
        raw = payload.get("drives") if isinstance(payload, dict) else payload
        self.drives = [DriveInfo.from_dict(d) for d in (raw or [])]
        previous = self.drives_list.currentItem()
        previous_device = previous.data(DRIVE_ROLE) if previous else None

        self.drives_list.blockSignals(True)
        self.drives_list.clear()
        for drive in self.drives:
            label = f"{drive.device}"
            if drive.media_present:
                label += f"  •  {drive.label or drive.disc_type or 'disc'}"
            else:
                label += "  •  empty"
            item = QListWidgetItem(label)
            item.setData(DRIVE_ROLE, drive.device)
            self.drives_list.addItem(item)
        self.drives_list.blockSignals(False)

        if not self.drives:
            self.details_label.setText(
                "No optical drives detected on the remote host."
            )
            self.set_device("")
            return

        target_index = 0
        if previous_device:
            for index, drive in enumerate(self.drives):
                if drive.device == previous_device:
                    target_index = index
                    break
        self.drives_list.setCurrentRow(target_index)
        self._show_drive_details(self.drives[target_index])
        self.set_device(self.drives[target_index].device)
        self.show_status(f"Found {len(self.drives)} optical drive(s)")

    def _on_drive_selected(self, current: QListWidgetItem | None, _previous=None) -> None:
        if current is None:
            return
        device = current.data(DRIVE_ROLE)
        for drive in self.drives:
            if drive.device == device:
                self._show_drive_details(drive)
                break
        self.set_device(device)

    def _show_drive_details(self, drive: DriveInfo) -> None:
        lines = [
            f"Device   : {drive.device}",
            f"Drive    : {(drive.vendor + ' ' + drive.model).strip() or 'unknown'}",
            f"Media    : {'inserted' if drive.media_present else 'empty'}",
        ]
        if drive.media_present:
            lines += [
                f"Type     : {drive.disc_type or 'unknown'}",
                f"Label    : {drive.label or '-'}",
                f"Filesys  : {drive.fstype or '-'}",
                f"Capacity : {format_bytes(int(drive.size)) if drive.size else '?'}",
            ]
        lines += [
            f"Recorder : {'yes' if drive.writable else 'read-only'}",
            f"Mounted  : {drive.mountpoint or 'no'}",
        ]
        self.details_label.setText("\n".join(lines))
        self.eject_button.setEnabled(bool(self.session))

    def set_device(self, device: str) -> None:
        self.browse_tab.set_device(device)
        self.image_tab.set_device(device)
        self.burn_tab.set_device(device)
        self.queue_tab.set_device(device)
        if device and self.current_profile is not None:
            if self.current_profile.last_device != device:
                self.current_profile.last_device = device
                self.store.upsert(self.current_profile)

    def eject_drive(self) -> None:
        self.browse_tab.eject()

    # ------------------------------------------------------------------ #
    # helpers used by tabs
    # ------------------------------------------------------------------ #

    def show_status(self, message: str) -> None:
        self.status_label.setText(message)

    def log(self, message: str, level: str = "info") -> None:
        if not message:
            return
        getattr(log, level.lower(), log.info)(message)

    # ------------------------------------------------------------------ #

    def _initial_connect_hint(self) -> None:
        if not self.profiles:
            self.show_status("No host profiles yet - open Profiles… to add one")
            self.log("no profiles configured")
        else:
            self.show_status("Select a host and press Connect")

    def _about(self) -> None:
        QMessageBox.about(
            self,
            "About Remote Optical Drive Manager",
            "Remote Optical Drive Manager\n\n"
            "Operate a CD/DVD drive attached to another Linux machine "
            "over SSH and a token-authenticated REST API.\n\n"
            "Device access is restricted to optical drives only.",
        )

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        # every background thread must be finished before Qt may destroy the
        # widgets that own them - otherwise the process aborts
        hosts = [self, self.browse_tab, self.image_tab, self.burn_tab, self.queue_tab]
        pending = [host for host in hosts if not host.stop_tasks(3000)]
        if pending:
            self.show_status(
                "Closing cancelled: a background task is still running "
                "(try again in a moment)"
            )
            event.ignore()
            return
        if self.session is not None:
            self.session.close()
            self.session = None
        event.accept()


def _client_log_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "rodm" / "logs"
