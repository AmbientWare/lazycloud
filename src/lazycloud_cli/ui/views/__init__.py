"""View components for LazyCloud CLI commands."""

from lazycloud_cli.ui.views.auth import AuthView
from lazycloud_cli.ui.views.deploy import DeployView
from lazycloud_cli.ui.views.destroy import DestroyView
from lazycloud_cli.ui.views.init import InitView
from lazycloud_cli.ui.views.list import ListView
from lazycloud_cli.ui.views.service import ServiceView
from lazycloud_cli.ui.views.status import StatusView

__all__ = [
    "AuthView",
    "DeployView",
    "DestroyView",
    "InitView",
    "ListView",
    "StatusView",
    "ServiceView",
]
