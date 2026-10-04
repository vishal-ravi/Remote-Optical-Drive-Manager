"""Optional queue-based burn workflow: burn several ISOs back to back."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)


from ..workers import FnTask, QueueTask, TaskHost

log = logging.getLogger("rodm.client.gui.queue")


class QueueTab(QWidget, TaskHost):
    def __init__(self, window, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.window = window
        self.device = ""
        self._task: QueueTask | None = None
        self._images: list[dict] = []

        self.image_combo = QComboBox(self)
        self.image_combo.setMinimumWidth(260)
        self.add_button = QPushButton("Add to queue", self)
        self.add_button.clicked.connect(self.add_current)
        self.refresh_button = QPushButton("Refresh ISOs", self)
        self.refresh_button.clicked.connect(self.refresh_images)

        self.model = QStandardItemModel(0, 3, self)
        self.model.setHorizontalHeaderLabels(["ISO image", "Device", "Status"])
        self.table = QTableView(self)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )

        self.remove_button = QPushButton("Remove selected", self)
        self.remove_button.clicked.connect(self.remove_selected)
        self.clear_button = QPushButton("Clear", self)
        self.clear_button.clicked.connect(self.clear_all)

        self.start_button = QPushButton("Start queue", self)
        self.start_button.clicked.connect(self.start_queue)
        self.stop_button = QPushButton("Stop", self)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_queue)

        self.status_label = QLabel("Queue is empty.", self)
        self.status_label.setWordWrap(True)

        top = QHBoxLayout()
        top.addWidget(QLabel("ISO:", self))
        top.addWidget(self.image_combo, 1)
        top.addWidget(self.add_button)
        top.addWidget(self.refresh_button)
        top.addStretch(1)

        edit_row = QHBoxLayout()
        edit_row.addWidget(self.remove_button)
        edit_row.addWidget(self.clear_button)
        edit_row.addStretch(1)
        edit_row.addWidget(self.start_button)
        edit_row.addWidget(self.stop_button)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        layout.addLayout(edit_row)
        layout.addWidget(self.status_label)

    # ------------------------------------------------------------------ #

    def set_device(self, device: str) -> None:
        self.device = device or ""

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
        task.failed.connect(lambda code, msg: self.status_label.setText(msg))
        self.start_task(task)

    def _on_images(self, result: dict) -> None:
        self._images = [e for e in (result.get("entries") or []) if not e.get("is_dir")]
        current = self.image_combo.currentData()
        self.image_combo.clear()
        for entry in sorted(self._images, key=lambda e: e.get("name", "").lower()):
            self.image_combo.addItem(entry["name"], entry["path"])
        if current:
            index = self.image_combo.findData(current)
            if index >= 0:
                self.image_combo.setCurrentIndex(index)

    # ------------------------------------------------------------------ #
    # queue management
    # ------------------------------------------------------------------ #

    def add_current(self) -> None:
        image = self.image_combo.currentData()
        if not image:
            QMessageBox.information(self, "Queue", "No ISO image selected.")
            return
        device = self.device or "/dev/sr0"
        row = [
            QStandardItem(image.split("/")[-1]),
            QStandardItem(device),
            QStandardItem("pending"),
        ]
        row[0].setData(image, Qt.ItemDataRole.UserRole)
        self.model.appendRow(row)
        self.status_label.setText(f"{self.model.rowCount()} item(s) queued.")

    def remove_selected(self) -> None:
        rows = sorted(
            {index.row() for index in self.table.selectionModel().selectedIndexes()},
            reverse=True,
        )
        for row in rows:
            self.model.removeRow(row)
        self.status_label.setText(f"{self.model.rowCount()} item(s) queued.")

    def clear_all(self) -> None:
        self.model.removeRows(0, self.model.rowCount())
        self.status_label.setText("Queue is empty.")

    def queue_items(self) -> list[dict]:
        items = []
        for row in range(self.model.rowCount()):
            name_item = self.model.item(row, 0)
            device_item = self.model.item(row, 1)
            image = name_item.data(Qt.ItemDataRole.UserRole) or name_item.text()
            items.append(
                {
                    "row": row,
                    "name": name_item.text(),
                    "iso": image,
                    "device": device_item.text(),
                }
            )
        return items

    # ------------------------------------------------------------------ #
    # run
    # ------------------------------------------------------------------ #

    def start_queue(self) -> None:
        session = self.session()
        if session is None or not session.connected:
            self.status_label.setText("Not connected - connect to a host first.")
            self.window.show_status("Not connected")
            return
        items = self.queue_items()
        if not items:
            self.status_label.setText("Add at least one ISO to the queue first.")
            return
        if self._task is not None and self._task.isRunning():
            self.status_label.setText("The queue is already running.")
            return

        self._set_row_status(
            {item["row"]: "pending" for item in items}
        )

        def runner(item: dict) -> str:
            job = session.call(
                "burn_iso",
                {
                    "device": item["device"],
                    "iso": item["iso"],
                    "speed": None,
                    "eject_after": False,
                },
                timeout=60,
            )
            return job["job_id"]

        task = QueueTask(session, items, runner)
        task.item_started.connect(self._on_item_started)
        task.item_progress.connect(self._on_item_progress)
        task.item_finished.connect(self._on_item_finished)
        task.item_failed.connect(self._on_item_failed)
        task.waiting_for_media.connect(self._on_waiting)
        task.queue_done.connect(self._on_queue_done)
        task.failed.connect(self._on_error)
        self._task = self.start_task(task)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.status_label.setText("Queue running…")

    def stop_queue(self) -> None:
        if self._task is not None:
            self._task.stop()
            self.status_label.setText("Stopping queue…")

    # ------------------------------------------------------------------ #

    def _set_row_status(self, mapping: dict[int, str]) -> None:
        for row, text in mapping.items():
            if 0 <= row < self.model.rowCount():
                self.model.item(row, 2).setText(text)

    def _on_item_started(self, payload: dict) -> None:
        index = payload["index"]
        items = self.queue_items()
        if index < len(items):
            self._set_row_status({items[index]["row"]: "burning…"})
        self.status_label.setText(f"Burning {payload['item']['name']} …")

    def _on_item_progress(self, payload: dict) -> None:
        job = payload.get("job") or {}
        progress = job.get("progress", -1)
        index = payload["index"]
        items = self.queue_items()
        if index < len(items):
            if progress is None or progress < 0:
                text = f"burning… {job.get('message', '')}"
            else:
                text = f"burning… {progress * 100:.0f}%"
            self._set_row_status({items[index]["row"]: text})
        self.status_label.setText(str(job.get("message") or "burning…"))

    def _on_item_finished(self, payload: dict) -> None:
        items = self.queue_items()
        index = payload["index"]
        if index < len(items):
            self._set_row_status({items[index]["row"]: "done"})
        self.window.log(f"queue item {index} finished: {payload['item']['name']}")
        self.status_label.setText(f"Finished {payload['item']['name']}.")

    def _on_item_failed(self, payload: dict) -> None:
        items = self.queue_items()
        index = payload["index"]
        error = payload.get("error") or "failed"
        if index < len(items):
            self._set_row_status({items[index]["row"]: f"failed: {error}"})
        self.window.log(f"queue item {index} failed: {error}", "error")
        self.status_label.setText(f"Queue stopped: {error}")

    def _on_waiting(self, payload: dict) -> None:
        item = payload.get("item") or {}
        QMessageBox.information(
            self,
            "Insert next disc",
            f"Insert a blank disc for {item.get('name')} "
            f"into {item.get('device')}, then press OK.",
        )
        if self._task is not None:
            self._task.proceed()

    def _on_queue_done(self, results: list) -> None:
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        done = sum(1 for r in results if (r.get("job") or {}).get("state") == "done")
        self.status_label.setText(
            f"Queue finished: {done}/{len(results)} image(s) burned."
        )
        self.window.log(f"queue finished: {done}/{len(results)}")
        self.window.show_status(self.status_label.text())
        if getattr(self.window, "burn_tab", None) is not None:
            self.window.burn_tab.refresh_images()

    def _on_error(self, code: str, message: str) -> None:
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.status_label.setText(f"[{code}] {message}")
        self.window.log(f"queue error: {message}", "error")
