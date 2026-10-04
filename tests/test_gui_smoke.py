"""Smoke-test the GUI construction on the offscreen Qt platform."""

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


@pytest.fixture()
def window(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("RODM_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("RODM_STATE_DIR", str(tmp_path / "state"))
    from client.gui.main_window import MainWindow
    from client.profiles import ProfileStore

    win = MainWindow(store=ProfileStore(tmp_path / "config" / "profiles.json"))
    yield win
    win.close()
    win.deleteLater()


class TestWindowConstruction:
    def test_tabs_exist(self, window):
        assert window.tabs.count() == 5
        labels = [window.tabs.tabText(i) for i in range(window.tabs.count())]
        assert labels == ["Browse", "Image", "Burn", "Queue", "Log"]

    def test_starts_disconnected(self, window):
        assert window.session is None
        assert window.connect_button.text() == "Connect"
        assert window.refresh_button.isEnabled() is False
        assert "Not connected" in window.details_label.text()

    def test_profile_combo_empty_without_profiles(self, window):
        assert window.profile_combo.count() == 0

    def test_show_status_and_log(self, window):
        window.show_status("hello status")
        assert window.status_label.text() == "hello status"
        window.log("a message")
        window.log("a warning", "warning")

    def test_set_device_propagates_to_tabs(self, window):
        window.set_device("/dev/sr0")
        assert window.browse_tab.device == "/dev/sr0"
        assert window.image_tab.device == "/dev/sr0"
        assert window.burn_tab.device == "/dev/sr0"
        assert window.queue_tab.device == "/dev/sr0"
        assert window.browse_tab.device_label.text() == "/dev/sr0"

    def test_disconnect_resets_tabs(self, window):
        window.set_device("/dev/sr1")
        window.disconnect_session(silent=True)
        assert window.browse_tab.device == ""
        assert window.burn_tab.device == ""

    def test_drive_details_render(self, window):
        from common.protocol import DriveInfo

        drive = DriveInfo(
            device="/dev/sr0",
            model="DVDW",
            vendor="HL-DT-ST",
            media_present=True,
            label="MOVIE",
            fstype="udf",
            writable=True,
            mountpoint=None,
            size=str(4 * 1024 * 1024 * 1024),
            disc_type="DVD-ROM",
        )
        window.drives = [drive]
        window._show_drive_details(drive)
        text = window.details_label.text()
        assert "/dev/sr0" in text
        assert "MOVIE" in text
        assert "inserted" in text
        assert "DVD-ROM" in text

    def test_log_tab_appends_and_trims(self, window):
        log_tab = window.log_tab
        for index in range(20):
            log_tab.append("INFO", f"line {index}")
        assert "line 19" in log_tab.view.toPlainText()
        log_tab.clear()
        assert log_tab.view.toPlainText() == ""


class TestQueueAndBrowseHelpers:
    def test_queue_items_reflect_model(self, window):
        from PySide6.QtGui import QStandardItem

        window.queue_tab.model.appendRow(
            [
                QStandardItem("a.iso"),
                QStandardItem("/dev/sr0"),
                QStandardItem("pending"),
            ]
        )
        items = window.queue_tab.queue_items()
        assert items == [
            {"row": 0, "name": "a.iso", "iso": "a.iso", "device": "/dev/sr0"}
        ]

    def test_queue_requires_connection(self, window, qapp):
        window.queue_tab.start_queue()
        # no session -> no crash, no task
        assert window.queue_tab._task is None

    def test_browse_requires_connection(self, window):
        window.browse_tab.refresh()
        assert "not connected" in window.browse_tab.hint.text().lower() or (
            "connection" in window.browse_tab.hint.text().lower()
        )

    def test_burn_tab_image_combo(self, window):
        window.burn_tab.set_image("images/test.iso")
        assert window.burn_tab.image_combo.currentData() == "images/test.iso"


class TestTaskLifetime:
    """Regression: a background QThread must never be destroyed while running."""

    def test_every_host_is_a_task_host(self, window):
        hosts = (
            window,
            window.browse_tab,
            window.image_tab,
            window.burn_tab,
            window.queue_tab,
        )
        for host in hosts:
            assert hasattr(host, "start_task")
            assert hasattr(host, "stop_tasks")

    def test_start_task_keeps_reference_and_waits(self, window, qapp):
        import time

        from client.gui.workers import FnTask

        task = FnTask(lambda: time.sleep(0.4))
        window.start_task(task)
        assert task in window._bg_tasks
        assert task.isRunning() is True
        assert window.tasks_busy is True

        assert window.stop_tasks(3000) is True
        assert window.tasks_busy is False

    def test_stop_tasks_reports_still_running(self, window, qapp):
        import time

        from client.gui.workers import FnTask

        task = FnTask(lambda: time.sleep(2.5))
        window.start_task(task)
        assert window.stop_tasks(100) is False
        assert window.stop_tasks(5000) is True

    def test_finished_tasks_are_released_on_next_start(self, window, qapp):
        from client.gui.workers import FnTask

        first = FnTask(lambda: None)
        window.start_task(first)
        assert first.wait(3000)
        second = FnTask(lambda: None)
        window.start_task(second)
        assert window._bg_tasks == [second]  # finished task was released
        assert second.wait(3000)

    def test_refresh_images_tracks_its_task(self, window, qapp):
        class FakeSession:
            connected = True

            def call(self, op, params=None, timeout=None):
                return {"entries": []}

            def close(self):
                pass

        window.session = FakeSession()
        for tab in (window.image_tab, window.burn_tab, window.queue_tab):
            tab.refresh_images()
            assert len(tab._bg_tasks) == 1, type(tab).__name__
        qapp.processEvents()
        for tab in (window.image_tab, window.burn_tab, window.queue_tab):
            assert tab.stop_tasks(3000) is True
        window.session = None
