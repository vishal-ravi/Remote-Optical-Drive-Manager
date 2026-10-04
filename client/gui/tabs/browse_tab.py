"""Browse the contents of the disc in the remote drive."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from common.logging_setup import format_bytes
from common.protocol import AgentError

from ..workers import FnTask, TaskHost, TransferTask

log = logging.getLogger("rodm.client.gui.browse")

DOWNLOAD_ROLE = Qt.ItemDataRole.UserRole + 1
IS_DIR_ROLE = Qt.ItemDataRole.UserRole + 2


class BrowseTab(QWidget, TaskHost):
    def __init__(self, window, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.window = window
        self.mountpoint = ""
        self.current_path = "/"
        self.device = ""
        self._tasks: list = []

        # -- controls ---------------------------------------------------- #
        self.device_label = QLabel("No drive selected", self)
        self.mount_button = QPushButton("Mount", self)
        self.unmount_button = QPushButton("Unmount", self)
        self.refresh_button = QPushButton("Refresh", self)
        self.eject_button = QPushButton("Eject", self)

        self.mount_button.clicked.connect(self.mount)
        self.unmount_button.clicked.connect(self.unmount)
        self.refresh_button.clicked.connect(self.refresh)
        self.eject_button.clicked.connect(self.eject)

        self.path_edit = QLineEdit("/", self)
        self.path_edit.returnPressed.connect(self._go_to_edit)
        self.go_button = QPushButton("Go", self)
        self.go_button.clicked.connect(self._go_to_edit)
        self.up_button = QPushButton("Up", self)
        self.up_button.clicked.connect(self.go_up)

        self.download_button = QPushButton("Download selected…", self)
        self.download_button.clicked.connect(self.download_selected)
        self.download_button.setEnabled(False)

        self.hint = QLabel("Connect to a host and select a drive to browse.", self)
        self.hint.setWordWrap(True)

        # -- table ------------------------------------------------------- #
        self.model = QStandardItemModel(0, 3, self)
        self.model.setHorizontalHeaderLabels(["Name", "Size", "Modified"])
        self.table = QTableView(self)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.doubleClicked.connect(self._on_double_click)
        self.table.selectionModel().selectionChanged.connect(self._on_selection)

        # -- layout ------------------------------------------------------ #
        row1 = QHBoxLayout()
        row1.addWidget(self.device_label, 1)
        row1.addWidget(self.mount_button)
        row1.addWidget(self.unmount_button)
        row1.addWidget(self.refresh_button)
        row1.addWidget(self.eject_button)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Path:", self))
        row2.addWidget(self.path_edit, 1)
        row2.addWidget(self.go_button)
        row2.addWidget(self.up_button)
        row2.addWidget(self.download_button)

        layout = QVBoxLayout(self)
        layout.addLayout(row1)
        layout.addLayout(row2)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.hint)

    # ------------------------------------------------------------------ #
    # state
    # ------------------------------------------------------------------ #

    def set_device(self, device: str) -> None:
        self.device = device or ""
        self.device_label.setText(self.device or "No drive selected")
        self.current_path = "/"
        self.path_edit.setText("/")
        self.mountpoint = ""
        self.model.removeRows(0, self.model.rowCount())
        self.hint.setText(
            f"Ready to browse {self.device}." if self.device else "No drive selected."
        )

    def session(self):
        return getattr(self.window, "session", None)

    def _require(self):
        session = self.session()
        if session is None or not session.connected:
            raise AgentError("not connected", "connection_error")
        if not self.device:
            raise AgentError("select a drive first", "validation_error")
        return session

    # ------------------------------------------------------------------ #
    # navigation
    # ------------------------------------------------------------------ #

    def refresh(self) -> None:
        try:
            session = self._require()
        except AgentError as exc:
            self.hint.setText(exc.message)
            return
        self.window.show_status(f"Reading {self.device}:{self.current_path} …")
        path = self.current_path
        task = FnTask(lambda: session.call("browse", {"device": self.device, "path": path}))
        task.done.connect(lambda result, p=path: self._on_listing(p, result))
        task.failed.connect(self._on_error)
        self._track(task)

    def navigate(self, path: str) -> None:
        self.current_path = "/" + path.strip("/") if path.strip("/") else "/"
        self.path_edit.setText(self.current_path)
        self.refresh()

    def go_up(self) -> None:
        parts = [p for p in self.current_path.split("/") if p]
        if parts:
            parts.pop()
        self.navigate("/".join(parts))

    def _go_to_edit(self) -> None:
        self.navigate(self.path_edit.text())

    # ------------------------------------------------------------------ #
    # callbacks
    # ------------------------------------------------------------------ #

    def _on_listing(self, path: str, result: dict) -> None:
        self.mountpoint = result.get("mountpoint") or ""
        entries = result.get("entries") or []
        self.model.removeRows(0, self.model.rowCount())
        for entry in entries:
            name_item = QStandardItem(
                f"{entry['name']}/" if entry["is_dir"] else entry["name"]
            )
            name_item.setData(entry["path"], DOWNLOAD_ROLE)
            name_item.setData(bool(entry["is_dir"]), IS_DIR_ROLE)
            size_item = QStandardItem(
                "" if entry["is_dir"] else format_bytes(entry["size"])
            )
            size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            import datetime

            stamp = datetime.datetime.fromtimestamp(entry["mtime"]).strftime(
                "%Y-%m-%d %H:%M"
            )
            date_item = QStandardItem(stamp)
            self.model.appendRow([name_item, size_item, date_item])
        self.current_path = "/" + (result.get("path") or "").strip("/")
        if not self.current_path:
            self.current_path = "/"
        self.path_edit.setText(self.current_path)
        self.hint.setText(
            f"{len(entries)} item(s) in {self.current_path} on {self.device}"
        )
        self.window.show_status(f"Listed {len(entries)} item(s) from {self.device}")
        self.window.log(f"browse {self.device}:{self.current_path} -> {len(entries)} entries")

    def _on_error(self, code: str, message: str) -> None:
        self.hint.setText(f"[{code}] {message}")
        self.window.show_status(message)
        self.window.log(f"browse failed: {message}", "warning" if code == "no_media" else "error")

    # ------------------------------------------------------------------ #
    # actions
    # ------------------------------------------------------------------ #

    def mount(self) -> None:
        self._simple_action("mount", "Mounted")

    def unmount(self) -> None:
        self._simple_action("unmount", "Unmounted")

    def eject(self) -> None:
        if (
            QMessageBox.question(
                self,
                "Eject media",
                f"Eject the disc from {self.device}?",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._simple_action("eject", "Ejected")
        self.model.removeRows(0, self.model.rowCount())

    def _simple_action(self, op: str, label: str) -> None:
        try:
            session = self._require()
        except AgentError as exc:
            self.hint.setText(exc.message)
            return
        task = FnTask(lambda: session.call(op, {"device": self.device}))
        task.done.connect(
            lambda _result, label=label, op=op: (
                self.window.show_status(f"{label} {self.device}"),
                self.window.log(f"{op} {self.device} ok"),
                self.refresh() if op in ("mount", "unmount") else None,
            )
        )
        task.failed.connect(self._on_error)
        self._track(task)

    def selected_files(self) -> list[dict]:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedIndexes()})
        files = []
        for row in rows:
            name_index = self.model.index(row, 0)
            if self.model.data(name_index, IS_DIR_ROLE):
                continue
            files.append(
                {
                    "name": self.model.item(row, 0).text().rstrip("/"),
                    "path": self.model.data(name_index, DOWNLOAD_ROLE),
                }
            )
        return files

    def download_selected(self) -> None:
        files = self.selected_files()
        if not files:
            QMessageBox.information(self, "Download", "Select one or more files first.")
            return
        try:
            session = self._require()
        except AgentError as exc:
            self.hint.setText(exc.message)
            return
        directory = QFileDialog.getExistingDirectory(self, "Save files to folder")
        if not directory:
            return
        mountpoint = self.mountpoint
        for entry in files:
            target = Path(directory) / Path(entry["name"]).name
            remote = f"{mountpoint.rstrip('/')}/{entry['path'].lstrip('/')}"
            task = TransferTask(
                "download",
                session,
                local_path=target,
                remote_path=remote,
                device=self.device,
                media_path=entry["path"],
                parent=self,
            )
            task.progressed.connect(
                lambda done, total, name=entry["name"]: self.window.show_status(
                    f"Downloading {name}: {format_bytes(done)} / {format_bytes(total)}"
                )
            )
            task.done.connect(
                lambda result, name=entry["name"]: (
                    self.window.log(f"downloaded {name} -> {result['path']}"),
                    self.window.show_status(f"Downloaded {name}"),
                )
            )
            task.failed.connect(self._on_error)
            self._track(task)

    # ------------------------------------------------------------------ #

    def _on_selection(self, *_args) -> None:
        self.download_button.setEnabled(bool(self.selected_files()))

    def _on_double_click(self, index) -> None:
        row = index.row()
        name_index = self.model.index(row, 0)
        if self.model.data(name_index, IS_DIR_ROLE):
            path = self.model.data(name_index, DOWNLOAD_ROLE)
            self.navigate(path)

    def _track(self, task) -> None:
        self.start_task(task)
