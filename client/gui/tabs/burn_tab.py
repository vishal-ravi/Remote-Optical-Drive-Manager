"""Burn an ISO to disc through the remote writer, then optionally verify it."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from common.logging_setup import format_bytes

from ..workers import FnTask, JobTask, TaskHost

log = logging.getLogger("rodm.client.gui.burn")

SPEEDS = ["auto", "1", "2", "4", "6", "8", "12", "16", "24", "32", "48"]


class BurnTab(QWidget, TaskHost):
    def __init__(self, window, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.window = window
        self.device = ""
        self.image = ""
        self._job_task: JobTask | None = None
        self._phase = "burn"
        self._pending_eject = False
        self._pending_verify = False

        # ---- image picker ---------------------------------------------- #
        self.image_combo = QComboBox(self)
        self.image_combo.setMinimumWidth(320)
        self.refresh_images_button = QPushButton("Refresh", self)
        self.refresh_images_button.clicked.connect(self.refresh_images)

        # ---- options ---------------------------------------------------- #
        self.speed_combo = QComboBox(self)
        for speed in SPEEDS:
            self.speed_combo.addItem("auto" if speed == "auto" else f"{speed}x")
        self.speed_combo.setCurrentIndex(0)
        self.simulate_box = QCheckBox("Simulate (dummy write, no dye burned)", self)
        self.overburn_box = QCheckBox("Allow overburning past capacity", self)
        self.eject_box = QCheckBox("Eject when finished", self)
        self.verify_box = QCheckBox("Verify against the source ISO after burning", self)
        self.verify_box.setChecked(True)

        options_form = QFormLayout()
        options_form.addRow("Write speed:", self.speed_combo)

        # ---- actions ----------------------------------------------------- #
        self.start_button = QPushButton("Start burn", self)
        self.start_button.clicked.connect(self.start_burn)
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_job)

        self.progress = QProgressBar(self)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p% %v")
        self.status_label = QLabel("Idle", self)
        self.status_label.setWordWrap(True)

        self.info_label = QLabel("", self)
        self.info_label.setWordWrap(True)

        # ---- agent images table ------------------------------------------ #
        self.images_model = QStandardItemModel(0, 2, self)
        self.images_model.setHorizontalHeaderLabels(["ISO image", "Size"])
        self.images_table = QTableView(self)
        self.images_table.setModel(self.images_model)
        self.images_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.images_table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.images_table.verticalHeader().setVisible(False)
        self.images_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.images_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.images_table.doubleClicked.connect(self._on_table_double_click)

        # ---- assemble ------------------------------------------------------ #
        image_box = QGroupBox("ISO image (stored on the agent)", self)
        image_layout = QVBoxLayout(image_box)
        combo_row = QHBoxLayout()
        combo_row.addWidget(self.image_combo, 1)
        combo_row.addWidget(self.refresh_images_button)
        image_layout.addLayout(combo_row)
        image_layout.addWidget(self.images_table, 1)

        options_box = QGroupBox("Burn options", self)
        options_layout = QVBoxLayout(options_box)
        options_layout.addLayout(options_form)
        options_layout.addWidget(self.simulate_box)
        options_layout.addWidget(self.overburn_box)
        options_layout.addWidget(self.verify_box)
        options_layout.addWidget(self.eject_box)
        action_row = QHBoxLayout()
        action_row.addWidget(self.start_button)
        action_row.addWidget(self.cancel_button)
        action_row.addStretch(1)
        options_layout.addLayout(action_row)

        progress_box = QGroupBox("Progress", self)
        progress_layout = QVBoxLayout(progress_box)
        progress_layout.addWidget(self.progress)
        progress_layout.addWidget(self.status_label)
        progress_layout.addWidget(self.info_label)

        layout = QVBoxLayout(self)
        layout.addWidget(image_box, 1)
        layout.addWidget(options_box)
        layout.addWidget(progress_box)

    # ------------------------------------------------------------------ #

    def set_device(self, device: str) -> None:
        self.device = device or ""
        self.start_button.setEnabled(bool(self.device))

    def set_image(self, image_path: str) -> None:
        self.image = image_path or ""
        index = self.image_combo.findData(self.image)
        if index >= 0:
            self.image_combo.setCurrentIndex(index)
        else:
            self.image_combo.insertItem(0, self.image.split("/")[-1], self.image)
            self.image_combo.setCurrentIndex(0)
        self.info_label.setText(f"Selected: {self.image}")

    def session(self):
        return getattr(self.window, "session", None)

    # ------------------------------------------------------------------ #
    # images
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
        entries = [e for e in (result.get("entries") or []) if not e.get("is_dir")]
        current = self.image_combo.currentData()
        self.image_combo.clear()
        self.images_model.removeRows(0, self.images_model.rowCount())
        for entry in sorted(entries, key=lambda e: e.get("name", "").lower()):
            self.image_combo.addItem(entry["name"], entry["path"])
            name_item = QStandardItem(entry["name"])
            size_item = QStandardItem(format_bytes(entry.get("size", 0)))
            size_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            self.images_model.appendRow([name_item, size_item])
        if current:
            index = self.image_combo.findData(current)
            if index >= 0:
                self.image_combo.setCurrentIndex(index)
        if not entries:
            self.info_label.setText(
                "No ISO images on the agent yet - create one in the Image tab."
            )

    def _on_table_double_click(self, index) -> None:
        row = index.row()
        item = self.images_model.item(row, 0)
        if item:
            self.image_combo.setCurrentIndex(self.image_combo.findData(item.text()))
            self.info_label.setText(f"Selected: {item.text()}")

    # ------------------------------------------------------------------ #
    # burn
    # ------------------------------------------------------------------ #

    def start_burn(self) -> None:
        session = self.session()
        if session is None or not session.connected:
            QMessageBox.warning(self, "Burn", "Not connected.")
            return
        if not self.device:
            QMessageBox.warning(self, "Burn", "Select a drive first.")
            return
        image = self.image_combo.currentData() or self.image
        if not image:
            QMessageBox.information(self, "Burn", "Choose an ISO image first.")
            return

        speed_text = self.speed_combo.currentText().replace("x", "")
        params = {
            "device": self.device,
            "iso": image,
            "speed": None if speed_text == "auto" else int(speed_text),
            "overburn": self.overburn_box.isChecked(),
            "simulate": self.simulate_box.isChecked(),
            "eject_after": False,  # ejected after verify instead
        }
        confirm = (
            "SIMULATE" if params["simulate"] else "BURN"
        )
        answer = QMessageBox.question(
            self,
            "Confirm burn",
            f"{confirm} {image} to {self.device} at "
            f"{'max' if params['speed'] is None else str(params['speed']) + 'x'} speed?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self._pending_verify = self.verify_box.isChecked()
        self._pending_eject = self.eject_box.isChecked()
        self._phase = "burn"
        self._start_job(session, "burn_iso", params, f"{confirm}ing {image} …")

    def _start_job(self, session, op: str, params: dict, label: str) -> None:
        if self._job_task is not None and self._job_task.isRunning():
            QMessageBox.information(self, "Busy", "A job is already running.")
            return
        self.status_label.setText(label)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.cancel_button.setEnabled(True)
        self.start_button.setEnabled(False)

        submit = FnTask(lambda: session.call(op, params))
        submit.done.connect(lambda job: self._watch(job, label))
        submit.failed.connect(self._on_error)
        self.start_task(submit)

    def _watch(self, job: dict, label: str) -> None:
        job_id = job.get("job_id")
        if not job_id:
            self._on_error("internal_error", "agent did not return a job id")
            return
        task = JobTask(self.session(), job_id)
        task.updated.connect(self._on_job_update)
        task.finished_state.connect(self._on_job_finished)
        task.failed.connect(self._on_error)
        self._job_task = self.start_task(task)

    def _on_job_update(self, update: dict) -> None:
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
        prefix = "Verify" if self._phase == "verify" else "Burn"
        message = job.get("message") or job.get("state") or ""
        self.status_label.setText(f"{prefix}: {message}")

    def _on_job_finished(self, job: dict) -> None:
        state = job.get("state")
        job_id = job.get("job_id")
        if state == "done":
            self.window.log(f"{self._phase} job {job_id} done")
            if self._phase == "burn" and self._pending_verify:
                self._phase = "verify"
                self.status_label.setText("Verifying burned disc …")
                self.progress.setValue(0)
                self._start_verify()
                return
            self._finish_pipeline(success=True, result=job.get("result") or {})
            return
        if state == "cancelled":
            self.status_label.setText("Cancelled.")
            self.window.log(f"{self._phase} job {job_id} cancelled", "warning")
            self._reset_buttons()
            return
        message = job.get("error") or "failed"
        self.status_label.setText(f"Failed: {message}")
        self.window.log(f"{self._phase} job {job_id} failed: {message}", "error")
        self.window.show_status(f"Burn failed: {message}")
        self._reset_buttons()

    def _start_verify(self) -> None:
        session = self.session()
        params = {"device": self.device, "iso": self.image_combo.currentData() or self.image}
        self._start_job(session, "verify_disc", params, "Verifying …")

    def _finish_pipeline(self, *, success: bool, result: dict) -> None:
        self._reset_buttons()
        if success:
            if self._phase == "verify":
                offset = result.get("match_offset")
                self.progress.setValue(100)
                self.status_label.setText(
                    "Verified: disc matches the source ISO "
                    f"(md5 {result.get('source_md5', '')[:16]}…, offset {offset})"
                )
                self.window.show_status("Verification passed")
                self.window.log(f"verify ok: {result.get('source_md5')}")
            else:
                self.progress.setValue(100)
                self.status_label.setText("Burn complete.")
                self.window.show_status("Burn complete")
            if self._pending_eject:
                self._eject()

    def _eject(self) -> None:
        session = self.session()
        if session is None or not self.device:
            return
        task = FnTask(lambda: session.call("eject", {"device": self.device}))
        task.done.connect(
            lambda _r: (
                self.window.show_status("Tray ejected"),
                self.window.log(f"ejected {self.device}"),
            )
        )
        task.failed.connect(self._on_error)
        self.start_task(task)

    def _reset_buttons(self) -> None:
        self.cancel_button.setEnabled(False)
        self.start_button.setEnabled(bool(self.device))
        self.progress.setRange(0, 100)

    def _on_error(self, code: str, message: str) -> None:
        self._reset_buttons()
        self.status_label.setText(f"[{code}] {message}")
        self.window.log(f"error: {message}", "error")
        self.window.show_status(message)

    def cancel_job(self) -> None:
        task = self._job_task
        if task is None or not task.isRunning():
            return
        job_id = task.job_id
        cancel = FnTask(lambda: self.session().cancel_job(job_id))
        cancel.done.connect(
            lambda _s: self.window.log(f"cancel requested for {job_id}", "warning")
        )
        cancel.failed.connect(self._on_error)
        self.start_task(cancel)
