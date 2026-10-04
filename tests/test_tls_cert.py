"""TLS certificate generation and SAN parsing (agent side).

Regression: the SAN parser used to return only the first address of a line,
so every agent restart considered the certificate stale and regenerated it,
invalidating the client's pinned fingerprint.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def generated(tmp_path):
    from agent import http_api

    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    http_api.generate_tls_pair(cert, key)
    return cert, key


class TestCertRefreshLogic:
    def test_all_san_ips_are_parsed(self, generated):
        from agent import http_api

        cert, _ = generated
        ips = http_api._cert_ip_sans(cert)
        assert "127.0.0.1" in ips
        local = http_api._local_ips()
        assert set(local) <= set(ips), f"local={local} sans={ips}"

    def test_fresh_cert_needs_no_refresh(self, generated):
        from agent import http_api

        cert, _ = generated
        assert http_api._cert_needs_refresh(cert) is False

    def test_foreign_interface_ip_triggers_refresh(self, generated, monkeypatch):
        from agent import http_api

        cert, _ = generated
        monkeypatch.setattr(http_api, "_local_ips", lambda: ["127.0.0.1", "10.9.9.9"])
        assert http_api._cert_needs_refresh(cert) is True

    def test_missing_cert_triggers_refresh(self, tmp_path):
        from agent import http_api

        assert http_api._cert_needs_refresh(tmp_path / "absent.crt") is True
