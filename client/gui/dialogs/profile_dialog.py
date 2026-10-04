"""Create / edit a host connection profile."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from common.protocol import AgentError
from ...profiles import Profile
from ...transport import ClientSession, SshTransport
from ..workers import FnTask, TaskHost

log = logging.getLogger("rodm.client.gui.profile")


class ProfileDialog(QDialog, TaskHost):
    def __init__(self, profile: Profile | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connection profile")
        self.setMinimumWidth(520)
        self.result_profile: Profile | None = None
        self._existing = profile
        self._test_task: FnTask | None = None
        self._token_task: FnTask | None = None

        # ---- ssh ------------------------------------------------------- #
        self.name_edit = QLineEdit(profile.name if profile else "", self)
        self.host_edit = QLineEdit(profile.host if profile else "", self)
        self.user_edit = QLineEdit(profile.user if profile else "", self)
        self.port_edit = QLineEdit(str(profile.port if profile else 22), self)
        self.key_edit = QLineEdit(profile.key_path if profile else "", self)
        self.key_edit.setPlaceholderText("~/.ssh/id_ed25519 (leave empty for defaults)")
        self.agent_edit = QLineEdit(
            profile.agent_cmd if profile else Profile(name="", host="").agent_cmd, self
        )

        ssh_form = QFormLayout()
        ssh_form.addRow("Name:", self.name_edit)
        ssh_form.addRow("Host / IP:", self.host_edit)
        ssh_form.addRow("SSH user:", self.user_edit)
        ssh_form.addRow("SSH port:", self.port_edit)
        ssh_form.addRow("SSH key:", self.key_edit)
        ssh_form.addRow("Agent command:", self.agent_edit)

        ssh_box = QGroupBox("Remote host (SSH)", self)
        ssh_box.setLayout(ssh_form)

        # ---- rest ------------------------------------------------------- #
        self.rest_enabled = QCheckBox("Use the REST API for progress/transfer", self)
        self.rest_enabled.setChecked(profile.rest_enabled if profile else True)
        self.rest_scheme = QComboBox(self)
        self.rest_scheme.addItems(["https", "http"])
        if profile:
            index = self.rest_scheme.findText(profile.rest_scheme or "https")
            self.rest_scheme.setCurrentIndex(max(index, 0))
        self.rest_port = QLineEdit(str(profile.rest_port if profile else 8443), self)
        self.token_edit = QLineEdit(profile.rest_token if profile else "", self)
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_button = QPushButton("Fetch via SSH", self)
        self.token_button.clicked.connect(self.fetch_token)
        token_row = QHBoxLayout()
        token_row.addWidget(self.token_edit, 1)
        token_row.addWidget(self.token_button)

        rest_form = QFormLayout()
        rest_form.addRow(self.rest_enabled)
        rest_form.addRow("Scheme:", self.rest_scheme)
        rest_form.addRow("REST port:", self.rest_port)
        rest_form.addRow("Bearer token:", token_row)

        rest_box = QGroupBox("REST API (optional)", self)
        rest_box.setLayout(rest_form)

        # ---- actions ---------------------------------------------------- #
        self.feedback = QLabel("", self)
        self.feedback.setWordWrap(True)

        self.test_button = QPushButton("Test connection", self)
        self.test_button.clicked.connect(self.test_connection)

        self.trust_button = QPushButton("Forget certificate", self)
        self.trust_button.setToolTip(
            "Delete the pinned TLS certificate of this host; the next "
            "'Test connection' pins it again (needed after the agent "
            "regenerates its certificate)."
        )
        self.trust_button.clicked.connect(self.forget_certificate)
        self.trust_button.setEnabled(bool(profile and profile.tls_cert_file))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        top = QHBoxLayout()
        top.addWidget(self.test_button, 1)
        top.addWidget(self.trust_button)
        top.addWidget(buttons)

        layout = QVBoxLayout(self)
        layout.addWidget(ssh_box)
        layout.addWidget(rest_box)
        layout.addWidget(self.feedback)
        layout.addLayout(top)

    # ------------------------------------------------------------------ #

    def _profile_from_form(self) -> Profile:
        name = self.name_edit.text().strip()
        host = self.host_edit.text().strip()
        if not name:
            raise ValueError("profile name is required")
        if not host:
            raise ValueError("host is required")
        try:
            port = int(self.port_edit.text().strip() or 22)
            rest_port = int(self.rest_port.text().strip() or 8443)
        except ValueError as exc:
            raise ValueError("ports must be numbers") from exc

        data = {
            "name": name,
            "host": host,
            "user": self.user_edit.text().strip(),
            "port": port,
            "key_path": self.key_edit.text().strip(),
            "agent_cmd": self.agent_edit.text().strip()
            or Profile(name="x", host="x").agent_cmd,
            "rest_enabled": self.rest_enabled.isChecked(),
            "rest_port": rest_port,
            "rest_scheme": self.rest_scheme.currentText(),
            "rest_token": self.token_edit.text().strip(),
        }
        if self._existing is not None:
            profile = self._existing
            for key, value in data.items():
                setattr(profile, key, value)
            return profile
        return Profile(**data)

    def _accept(self) -> None:
        try:
            profile = self._profile_from_form()
        except ValueError as exc:
            QMessageBox.warning(self, "Profile", str(exc))
            return
        self.result_profile = profile
        self.accept()

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def test_connection(self) -> None:
        try:
            profile = self._profile_from_form()
        except ValueError as exc:
            QMessageBox.warning(self, "Profile", str(exc))
            return
        self.feedback.setText("Testing…")
        self.test_button.setEnabled(False)
        session = ClientSession(profile, timeout=12)

        def run() -> dict:
            try:
                session.connect()
                version = session.call("version", timeout=12)
                drives = session.call("list_drives", timeout=12)
                return {
                    "status": session.status(),
                    "version": version,
                    "drives": drives.get("drives", []),
                }
            finally:
                session.close()

        task = FnTask(run)
        task.done.connect(lambda result: self._on_test_ok(profile, result))
        task.failed.connect(self._on_test_failed)
        self._test_task = self.start_task(task)

    def _on_test_ok(self, profile: Profile, result: dict) -> None:
        self.test_button.setEnabled(True)
        status = result.get("status", {})
        transport = status.get("transport", "?")
        version = (result.get("version") or {}).get("version", "?")
        drives = result.get("drives") or []
        names = ", ".join(d.get("device", "?") for d in drives) or "none"
        self.feedback.setText(
            f"OK via {transport} (agent {version}); drives: {names}"
        )
        # keep trust material discovered during the test
        if profile.tls_cert_file:
            self._existing = profile if self._existing is profile else self._existing

    def _on_test_failed(self, code: str, message: str) -> None:
        self.test_button.setEnabled(True)
        self.feedback.setText(f"[{code}] {message}")

    def fetch_token(self) -> None:
        try:
            profile = self._profile_from_form()
        except ValueError as exc:
            QMessageBox.warning(self, "Profile", str(exc))
            return
        self.token_button.setEnabled(False)
        self.feedback.setText("Fetching token via SSH…")

        def run() -> str:
            transport = SshTransport(profile, timeout=12)
            try:
                transport.connect()
                out = transport.run_command(f"{profile.agent_cmd} token", timeout=12)
                if not out.strip():
                    raise AgentError("agent returned no token", "tool_failed")
                return out.strip().splitlines()[-1].strip()
            finally:
                transport.close()

        task = FnTask(run)
        task.done.connect(self._on_token)
        task.failed.connect(self._on_test_failed)
        self._token_task = self.start_task(task)

    def forget_certificate(self) -> None:
        profile = self._existing
        if profile is None or not profile.tls_cert_file:
            return
        path = Path(profile.tls_cert_file)
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            log.warning("could not delete %s: %s", path, exc)
        profile.tls_cert_file = ""
        profile.tls_fingerprint = ""
        self.trust_button.setEnabled(False)
        self.feedback.setText(
            "Pinned certificate removed - press 'Test connection', then Save."
        )

    def _on_token(self, token: str) -> None:
        self.token_button.setEnabled(True)
        self.token_edit.setText(token)
        self.feedback.setText("Token retrieved over SSH.")

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        # never destroy the dialog while a test/token thread is running
        if not self.stop_tasks(4000):
            self.feedback.setText(
                "Still waiting for the running network test - close again shortly."
            )
            event.ignore()
            return
        event.accept()
