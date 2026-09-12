from __future__ import annotations

import logging
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from cryptography import x509
from gateway.tunnel_certificates import TunnelCertificateService
from networking.tunnel_tls import TunnelCredentials, agent_certificate_request
from shared.http.agent_identity import TUNNEL_CERTIFICATE_RENEWAL_MARGIN_SECONDS
from shared.timestamps import utc_now

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ControlPlaneTunnelIdentity:
    service: TunnelCertificateService
    _directory: tempfile.TemporaryDirectory[str] = field(
        default_factory=lambda: tempfile.TemporaryDirectory(prefix="lazycloud-control-identity-"),
        init=False,
        repr=False,
    )
    _instance_id: str = field(default_factory=lambda: str(uuid4()), init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    credentials: TunnelCredentials = field(init=False)

    def __post_init__(self) -> None:
        directory = Path(self._directory.name)
        self.credentials = TunnelCredentials(directory / "key.pem", directory / "certificate.json")
        try:
            self._renew()
        except BaseException:
            self._directory.cleanup()
            raise
        self._thread = threading.Thread(
            target=self._run, name="control-tunnel-identity", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise RuntimeError("Control plane tunnel certificate renewal did not stop")
        self._directory.cleanup()

    def _renew(self) -> None:
        certificate = self.service.issue_control_plane(
            agent_certificate_request(self.credentials.key_path), self._instance_id
        )
        self.credentials.install(
            certificate_pem=certificate.certificate_pem,
            trust_bundle_pem=certificate.trust_bundle_pem,
        )

    def _run(self) -> None:
        while not self._stop.wait(30):
            try:
                certificate = x509.load_pem_x509_certificate(
                    self.credentials.certificate_pem.encode()
                )
                if certificate.not_valid_after_utc <= utc_now() + timedelta(
                    seconds=TUNNEL_CERTIFICATE_RENEWAL_MARGIN_SECONDS
                ):
                    self._renew()
            except Exception:
                LOGGER.exception("Control plane tunnel certificate renewal failed")
