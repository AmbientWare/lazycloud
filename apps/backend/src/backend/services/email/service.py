import base64
import html
from datetime import datetime
from pathlib import Path

import resend
from loguru import logger
from resend import Emails

from backend.config import app_config

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
    ) -> Emails.SendResponse:
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

            response = resend.Emails.send(Emails.SendParams(**params))

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
    ) -> Emails.SendResponse:
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

            response = resend.Emails.send(Emails.SendParams(**params))

            logger.info(
                f"Sent ownership transfer invitation email to {email} for workspace {workspace_name}"
            )
            return response
        except Exception as e:
            logger.error(f"Failed to send ownership transfer email to {email}: {e}")
            raise

    def send_feedback(
        self,
        user_email: str,
        user_name: str | None,
        feedback_type: str,
        message: str,
        source: str = "cli",
    ) -> Emails.SendResponse:
        """Send user feedback email to support team"""
        try:
            type_labels = {
                "bug": "Bug Report",
                "feature": "Feature Request",
                "other": "General Feedback",
            }
            type_label = type_labels.get(feedback_type, "Feedback")

            subject = f"[{source.upper()}] {type_label} from {user_email}"

            # Escape user-provided content to prevent HTML injection
            safe_user_name = html.escape(user_name or "Unknown")
            safe_user_email = html.escape(user_email)
            safe_message = html.escape(message)

            html_content = f"""
            <html>
            <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #1a1a1a; max-width: 600px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #2563eb; border-bottom: 2px solid #2563eb; padding-bottom: 10px;">
                    [{source.upper()}] {type_label}
                </h2>
                <p><strong>From:</strong> {safe_user_name} ({safe_user_email})</p>
                <p><strong>Type:</strong> {type_label}</p>
                <div style="background-color: #f5f5f5; border-left: 4px solid #66CBFF; padding: 16px; margin-top: 16px;">
                    <pre style="white-space: pre-wrap; margin: 0; font-family: inherit;">{safe_message}</pre>
                </div>
                <p style="color: #666; font-size: 12px; margin-top: 24px;">
                    Reply directly to this email to respond to the user.
                </p>
            </body>
            </html>
            """

            params = {
                "from": f"LazyCloud <noreply@{LAZYCLOUD_DOMAIN}>",
                "to": [app_config.SUPPORT_EMAIL],
                "subject": subject,
                "html": html_content,
                "reply_to": user_email,
            }

            response = resend.Emails.send(Emails.SendParams(**params))

            logger.info(f"Sent feedback email from {user_email} ({feedback_type})")
            return response
        except Exception as e:
            logger.error(f"Failed to send feedback email from {user_email}: {e}")
            raise


email_service = EmailService()
