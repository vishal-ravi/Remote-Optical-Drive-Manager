"""Create ISO images: rip a physical disc or package local files."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from common.logging_setup import format_bytes

from ..workers import FnTask, JobTask, TaskHost, TransferTask

log = logging.getLogger("rodm.client.gui.image")


class ImageTab(QWidget, TaskHost):
    images_refreshed = Signal(list)

    def __init__(self, window, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.window = window
        self.device = ""
        self._job_task: JobTask | None = None
        self._transfer_tasks: list[TransferTask] = []
        self._pending_staged: list[str] = []
        self._pending_name = ""
        self._pending_label = ""

        # ---- rip group ------------------------------------------------- #
        self.rip_name = QLineEdit(self)
        self.rip_name.setPlaceholderText("backup-disc")
        self.rip_overwrite = QCheckBox("Overwrite if the ISO already exists", self)
        self.rip_button = QPushButton("Rip disc to ISO", self)
        self.rip_button.clicked.connect(self.start_rip)

        rip_form = QFormLayout()
        rip_form.addRow("ISO name:", self.rip_name)
        rip_form.addRow("", self.rip_overwrite)

        # ---- files group ----------------------------------------------- #
        self.files_label = QLabel("No files selected", self)
        self.files_label.setWordWrap(True)
        self.pick_button = QPushButton("Choose files…", self)
        self.pick_button.clicked.connect(self.choose_files)
        self.pick_dir_button = QPushButton("Choose folder…", self)
        self.pick_dir_button.clicked.connect(self.choose_folder)
        self.iso_label = QLineEdit("MY_DATA", self)
        self.iso_label.setMaxLength(32)
        self.build_button = QPushButton("Build ISO from files", self)
        self.build_button.clicked.connect(self.start_build)

        files_form = QFormLayout()
        files_form.addRow("Volume label:", self.iso_label)

        # ---- progress --------------------------------------------------- #
        self.progress = QProgressBar(self)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p% %v")
        self.status_label = QLabel("Idle", self)
        self.status_label.setWordWrap(True)
        self.cancel_button = QPushButton("Cancel job", self)
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_job)

        # ---- images on agent -------------------------------------------- #
        self.images_model = QStandardItemModel(0, 2, self)
        self.images_model.setHorizontalHeaderLabels(["ISO image", "Size"])
        self.images_table = QTableView(self)
        self.images_table.setModel(self.images_model)
        self.images_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.images_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.images_table.verticalHeader().setVisible(False)
        self.images_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.images_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.images_table.doubleClicked.connect(self._use_in_burn)

        self.refresh_images_button = QPushButton("Refresh list", self)
        self.refresh_images_button.clicked.connect(self.refresh_images)
        self.use_in_burn_button = QPushButton("Use in Burn tab", self)
        self.use_in_burn_button.clicked.connect(self._use_in_burn)

        # ---- assemble ---------------------------------------------------- #
        rip_box = QGroupBox("Rip a physical disc on the remote drive", self)
        rip_layout = QVBoxLayout(rip_box)
        rip_layout.addLayout(rip_form)
        rip_layout.addWidget(self.rip_button)

        files_box = QGroupBox("Create an ISO from local files", self)
        files_layout = QVBoxLayout(files_box)
        pick_row = QHBoxLayout()
        pick_row.addWidget(self.pick_button)
        pick_row.addWidget(self.pick_dir_button)
        files_layout.addLayout(pick_row)
        files_layout.addWidget(self.files_label)
        files_layout.addLayout(files_form)
        files_layout.addWidget(self.build_button)

        progress_box = QGroupBox("Progress", self)
        progress_layout = QVBoxLayout(progress_box)
        progress_layout.addWidget(self.progress)
        row = QHBoxLayout()
        row.addWidget(self.status_label, 1)
        row.addWidget(self.cancel_button)
        progress_layout.addLayout(row)

        images_box = QGroupBox("ISO images stored on the agent", self)
        images_layout = QVBoxLayout(images_box)
        images_layout.addWidget(self.images_table, 1)
        image_buttons = QHBoxLayout()
        image_buttons.addWidget(self.refresh_images_button)
        image_buttons.addWidget(self.use_in_burn_button)
        image_buttons.addStretch(1)
        images_layout.addLayout(image_buttons)

        layout = QVBoxLayout(self)
        layout.addWidget(rip_box)
        layout.addWidget(files_box)
        layout.addWidget(progress_box)
        layout.addWidget(images_box, 1)

    # ------------------------------------------------------------------ #

    def set_device(self, device: str) -> None:
        self.device = device or ""
        enabled = bool(self.device)
        self.rip_button.setEnabled(enabled)

    def session(self):
        return getattr(self.window, "session", None)

    # ------------------------------------------------------------------ #
    # rip
    # ------------------------------------------------------------------ #

    def start_rip(self) -> None:
        session = self.session()
        if session is None or not session.connected:
            QMessageBox.warning(self, "Rip", "Not connected.")
            return
        if not self.device:
            QMessageBox.warning(self, "Rip", "Select a drive first.")
            return
        name = self.rip_name.text().strip() or "disc-backup"
        self._start_job(
            session,
            "create_iso_from_disc",
            {
                "device": self.device,
                "name": name,
                "overwrite": self.rip_overwrite.isChecked(),
            },
            label=f"Ripping {self.device} to {name}.iso",
        )

    # ------------------------------------------------------------------ #
    # files -> iso
    # ------------------------------------------------------------------ #

    def choose_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Choose files to put in the ISO")
        if not paths:
            return
        self._selected_paths = paths
        names = [Path(p).name for p in paths]
        shown = ", ".join(names[:5]) + ("…" if len(names) > 5 else "")
        self.files_label.setText(f"{len(paths)} file(s): {shown}")

    def choose_folder(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Choose a folder to package")
        if not directory:
            return
        self._selected_paths = [directory]
        self.files_label.setText(f"folder: {Path(directory).name}")

    def start_build(self) -> None:
        session = self.session()
        if session is None or not session.connected:
            QMessageBox.warning(self, "Build ISO", "Not connected.")
            return
        paths = getattr(self, "_selected_paths", None)
        if not paths:
            QMessageBox.information(self, "Build ISO", "Choose files or a folder first.")
            return
        name = self.rip_name.text().strip() or Path(paths[0]).stem

        # stage files on the agent first, then start the ISO job
        self.status_label.setText(f"Staging {len(paths)} item(s) on the agent…")
        self.progress.setRange(0, 0)
        self.build_button.setEnabled(False)
        self.pick_button.setEnabled(False)
        self.pick_dir_button.setEnabled(False)

        batch = f"stage/incoming/{name}"

        def upload_all() -> list[str]:
            results = []
            for path in paths:
                source = Path(path)
                relative = f"{batch}/{source.name}"
                session.upload(source, relative)
                results.append(relative)
            return results

        task = FnTask(upload_all)
        task.done.connect(lambda staged_paths: self._begin_build(staged_paths, name))
        task.failed.connect(self._on_error)
        self.start_task(task)

    def _begin_build(self, staged: list[str], name: str) -> None:
        session = self.session()
        self.build_button.setEnabled(True)
        self.pick_button.setEnabled(True)
        self.pick_dir_button.setEnabled(True)
        if not staged:
            return
        # a directory source is staged as a folder; genisoimage wants its parent
        source = staged[0] if len(staged) == 1 else f"stage/incoming/{name}"
        if len(staged) > 1:
            source = f"stage/incoming/{name}"
        self._start_job(
            session,
            "create_iso_from_files",
            {
                "source": source,
                "name": name,
                "label": self.iso_label.text().strip() or "RODM_DISC",
            },
            label=f"Building {name}.iso from {len(staged)} item(s)",
        )

    # ------------------------------------------------------------------ #
    # job plumbing
    # ------------------------------------------------------------------ #

    def _start_job(self, session, op: str, params: dict, label: str) -> None:
        if self._job_task is not None and self._job_task.isRunning():
            QMessageBox.information(self, "Busy", "A job is already running.")
            return
        self.status_label.setText(f"{label} …")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.cancel_button.setEnabled(True)

        submit = FnTask(lambda: session.call(op, params))
        submit.done.connect(lambda job: self._watch(job, label))
        submit.failed.connect(self._on_error)
        self.start_task(submit)

    def _watch(self, job: dict, label: str) -> None:
        job_id = job.get("job_id")
        if not job_id:
            self._on_error("internal_error", "agent did not return a job id")
            return
        session = self.session()
        task = JobTask(session, job_id)
        task.updated.connect(self._on_job_update)
        task.finished_state.connect(self._on_job_finished)
        task.failed.connect(self._on_error)
        self._job_task = self.start_task(task)

    def _on_job_update(self, update: dict) -> None:
        if update.get("type") != "status":
            if update.get("type") == "event":
                event = update.get("event") or {}
                if event.get("kind") == "log":
                    self.window.log(str((event.get("data") or {}).get("message", "")))
            return
        job = update.get("job") or {}
        progress = job.get("progress", -1)
        if progress is None or progress < 0:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(progress * 100))
        message = job.get("message") or job.get("state") or ""
        self.status_label.setText(str(message))

    def _on_job_finished(self, job: dict) -> None:
        self.cancel_button.setEnabled(False)
        self.progress.setRange(0, 100)
        state = job.get("state")
        if state == "done":
            result = job.get("result") or {}
            name = result.get("name") or result.get("image_name") or ""
            size = int(result.get("size") or 0)
            self.progress.setValue(100)
            self.status_label.setText(
                f"Done: {name} ({format_bytes(size)})" if size else f"Done: {name}"
            )
            self.window.log(f"job {job.get('job_id')} done: {name}")
            self.window.show_status(f"Created {name}")
            self.refresh_images()
        elif state == "cancelled":
            self.status_label.setText("Cancelled.")
            self.window.log(f"job {job.get('job_id')} cancelled", "warning")
        else:
            message = job.get("error") or "failed"
            self.status_label.setText(f"Failed: {message}")
            self.window.log(f"job {job.get('job_id')} failed: {message}", "error")
            self.window.show_status(f"Job failed: {message}")

    def _on_error(self, code: str, message: str) -> None:
        self.cancel_button.setEnabled(False)
        self.progress.setRange(0, 100)
        self.status_label.setText(f"[{code}] {message}")
        self.window.log(f"error: {message}", "error")
        self.window.show_status(message)

    def cancel_job(self) -> None:
        task = self._job_task
        if task is None or not task.isRunning():
            return
        session = self.session()
        job_id = task.job_id
        cancel = FnTask(lambda: session.cancel_job(job_id))
        cancel.done.connect(
            lambda _snap: self.window.log(f"cancel requested for {job_id}", "warning")
        )
        cancel.failed.connect(self._on_error)
        self.start_task(cancel)

    # ------------------------------------------------------------------ #
    # images on agent
    # ------------------------------------------------------------------ #

    def refresh_images(self) -> None:
        session = self.session()
        if session is None or not session.connected:
            return
        task = FnTask(lambda: session.call("stage_list", {"path": "images"}))
        task.done.connect(self._on_images)
        task.failed.connect(self._on_error)
        self.start_task(task)

    def _on_images(self, result: dict) -> None:
        entries = result.get("entries") or []
        self.images_model.removeRows(0, self.images_model.rowCount())
        for entry in sorted(entries, key=lambda e: e.get("name", "").lower()):
            if entry.get("is_dir"):
                continue
            name_item = QStandardItem(entry["name"])
            name_item.setData(entry["path"], Qt.ItemDataRole.UserRole)
            size_item = QStandardItem(format_bytes(entry.get("size", 0)))
            size_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            self.images_model.appendRow([name_item, size_item])
        self.images_refreshed.emit([e for e in entries if not e.get("is_dir")])

    def selected_image(self) -> str | None:
        rows = {
            index.row() for index in self.images_table.selectionModel().selectedIndexes()
        }
        if not rows:
            return None
        row = sorted(rows)[0]
        item = self.images_model.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _use_in_burn(self, *_args) -> None:
        image = self.selected_image()
        if not image:
            QMessageBox.information(self, "Burn", "Select an ISO image first.")
            return
        burn_tab = getattr(self.window, "burn_tab", None)
        if burn_tab is not None:
            burn_tab.set_image(image)
            self.window.tabs.setCurrentWidget(burn_tab)
