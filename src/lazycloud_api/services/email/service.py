import base64
from datetime import datetime
from pathlib import Path

import resend
from loguru import logger

from lazycloud_api.config import app_config

LAZYCLOUD_DOMAIN = "lazycloud.dev"
TEMPLATES_DIR = Path(__file__).parent / "templates"
ASSETS_DIR = Path(__file__).parent / "assets"
LOGO_PATH = ASSETS_DIR / "lazycloud.png"


class EmailService:
    """Service for sending emails via Resend"""

    def __init__(self):
        resend.api_key = app_config.RESEND_API_KEY

    def _load_template(self, template_name: str) -> str:
        """Load email template from templates directory"""
        template_path = TEMPLATES_DIR / template_name
        if not template_path.exists():
            raise FileNotFoundError(f"Email template not found: {template_name}")
        return template_path.read_text(encoding="utf-8")

    def _render_template(
        self, template_name: str, context: dict[str, str | int]
    ) -> str:
        """Render email template with context variables"""
        template = self._load_template(template_name)
        return template.format(**context)

    def _get_logo_attachment(self) -> dict | None:
        """Get logo as inline attachment if logo file exists"""
        if not LOGO_PATH.exists():
            return None

        try:
            # Read logo file and encode as base64
            logo_content = LOGO_PATH.read_bytes()
            logo_base64 = base64.b64encode(logo_content).decode("utf-8")

            return {
                "content": logo_base64,
                "filename": "lazycloud.png",
                "content_id": "lazycloud-logo",
            }
        except Exception as e:
            logger.warning(f"Failed to load logo file: {e}")
            return None

    def send_workspace_invitation(
        self,
        email: str,
        workspace_name: str,
        inviter_name: str,
        workspaces_url: str,
        expiration_days: int = 14,
    ) -> dict:
        """Send workspace invitation email"""
        try:
            # Get logo attachment if available
            logo_attachment = self._get_logo_attachment()
            logo_html = (
                '<img src="cid:lazycloud-logo" alt="LazyCloud" style="height: 48px; width: auto; vertical-align: middle; display: inline-block;" />'
                if logo_attachment
                else ""
            )

            html_content = self._render_template(
                "workspace_invitation.html",
                {
                    "workspace_name": workspace_name,
                    "inviter_name": inviter_name,
                    "workspaces_url": workspaces_url,
                    "expiration_days": expiration_days,
                    "logo_html": logo_html,
                    "current_year": datetime.now().year,
                },
            )

            params = {
                "from": f"LazyCloud <noreply@{LAZYCLOUD_DOMAIN}>",
                "to": [email],
                "subject": f"You've been invited to join {workspace_name} on LazyCloud",
                "html": html_content,
            }

            # Add logo as inline attachment if available
            if logo_attachment:
                params["attachments"] = [logo_attachment]

            response = resend.Emails.send(params)

            logger.info(
                f"Sent invitation email to {email} for workspace {workspace_name}"
            )
            return response
        except Exception as e:
            logger.error(f"Failed to send invitation email to {email}: {e}")
            raise

    def send_ownership_transfer_invitation(
        self,
        email: str,
        workspace_name: str,
        current_owner_name: str,
        workspaces_url: str,
        expiration_days: int = 14,
    ) -> dict:
        """Send ownership transfer invitation email"""
        try:
            # Get logo attachment if available
            logo_attachment = self._get_logo_attachment()
            logo_html = (
                '<img src="cid:lazycloud-logo" alt="LazyCloud" style="height: 48px; width: auto; vertical-align: middle; display: inline-block;" />'
                if logo_attachment
                else ""
            )

            html_content = self._render_template(
                "ownership_transfer.html",
                {
                    "workspace_name": workspace_name,
                    "current_owner_name": current_owner_name,
                    "workspaces_url": workspaces_url,
                    "expiration_days": expiration_days,
                    "logo_html": logo_html,
                    "current_year": datetime.now().year,
                },
            )

            params = {
                "from": f"LazyCloud <noreply@{LAZYCLOUD_DOMAIN}>",
                "to": [email],
                "subject": f"Ownership Transfer Request: {workspace_name} on LazyCloud",
                "html": html_content,
            }

            # Add logo as inline attachment if available
            if logo_attachment:
                params["attachments"] = [logo_attachment]

            response = resend.Emails.send(params)

            logger.info(
                f"Sent ownership transfer invitation email to {email} for workspace {workspace_name}"
            )
            return response
        except Exception as e:
            logger.error(f"Failed to send ownership transfer email to {email}: {e}")
            raise


email_service = EmailService()
