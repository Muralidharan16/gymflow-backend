import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings

logger = logging.getLogger("doers.email")


# ─── Email template ──────────────────────────────────────────────────────────

def _build_email(
    owner_name: str,
    org_name: str,
    verify_url: str,
) -> tuple[str, str]:
    """Returns (plain_text, html) tuple."""

    plain = f"""
Hi {owner_name},

You're one step away from setting up {org_name} on Doers.

Verify your email here (expires in 10 minutes):
{verify_url}

If you didn't create a Doers account, ignore this email.

— The Doers Team
""".strip()

    html = f"""
<!DOCTYPE html>
<html lang="en">
<body style="margin:0;padding:0;background:#f5f3ef;font-family:Inter,system-ui,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0">
    <tr>
      <td align="center" style="padding:48px 16px;">
        <table width="520" cellpadding="0" cellspacing="0"
               style="background:#ffffff;border-radius:16px;overflow:hidden;
                      border:1px solid #e8e4de;">

          <!-- Header -->
          <tr>
            <td align="center"
                style="padding:40px;border-bottom:1px solid #ede9e4;">
              <h1 style="margin:0;font-family:Georgia,serif;font-size:28px;
                         font-weight:400;color:#1a1a1a;letter-spacing:-0.02em;">
                Doers
              </h1>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:40px;">
              <p style="margin:0 0 16px;color:#5a5a5a;font-size:15px;line-height:1.7;">
                Hi <strong style="color:#1a1a1a;">{owner_name}</strong>,
              </p>
              <p style="margin:0 0 24px;color:#5a5a5a;font-size:15px;line-height:1.7;">
                You're one step away from setting up
                <strong style="color:#1a1a1a;">{org_name}</strong> on Doers.
                Click the button below to verify your email.
                This link expires in <strong>10 minutes</strong> and
                can only be used once.
              </p>

              <!-- CTA button -->
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td align="center" style="padding:8px 0 32px;">
                    <a href="{verify_url}"
                       style="display:inline-block;background:#1a1a1a;color:#ffffff;
                              text-decoration:none;padding:14px 36px;
                              border-radius:50px;font-size:13px;font-weight:500;
                              letter-spacing:0.06em;text-transform:uppercase;">
                      Verify My Email
                    </a>
                  </td>
                </tr>
              </table>

              <!-- Fallback link -->
              <p style="margin:0 0 8px;color:#8a8a8a;font-size:12px;line-height:1.6;">
                If the button doesn't work, paste this into your browser:
              </p>
              <p style="margin:0 0 24px;word-break:break-all;">
                <a href="{verify_url}"
                   style="color:#1a1a1a;font-size:12px;">{verify_url}</a>
              </p>

              <p style="margin:0;color:#8a8a8a;font-size:12px;line-height:1.6;">
                Didn't create a Doers account? You can safely ignore this email.
              </p>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td align="center"
                style="padding:20px 40px;background:#faf9f7;
                       border-top:1px solid #ede9e4;">
              <p style="margin:0;color:#9a9a9a;font-size:11px;">
                © 2025 Doers. All rights reserved.
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
    return plain, html


# ─── Providers ───────────────────────────────────────────────────────────────

async def _send_via_smtp(
    to_email: str,
    subject: str,
    plain: str,
    html: str,
) -> bool:
    """Send via Mailtrap SMTP (development)."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.MAIL_FROM
    msg["To"] = to_email
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(settings.MAIL_SERVER, settings.MAIL_PORT, timeout=10) as server:
            server.ehlo()
            server.starttls()
            server.login(settings.MAIL_USERNAME, settings.MAIL_PASSWORD)
            server.sendmail(settings.MAIL_FROM, [to_email], msg.as_string())

        logger.info("SMTP | verification email sent → %s", to_email)
        return True

    except smtplib.SMTPAuthenticationError:
        logger.error(
            "SMTP | authentication failed — "
            "check MAIL_USERNAME and MAIL_PASSWORD in .env"
        )
        return False

    except smtplib.SMTPConnectError:
        logger.error(
            "SMTP | could not connect to %s:%s — "
            "check MAIL_SERVER and MAIL_PORT in .env",
            settings.MAIL_SERVER,
            settings.MAIL_PORT,
        )
        return False

    except smtplib.SMTPException as exc:
        logger.exception("SMTP | error sending to %s: %s", to_email, exc)
        return False

    except Exception as exc:
        logger.exception("SMTP | unexpected error sending to %s: %s", to_email, exc)
        return False


async def _send_via_resend(
    to_email: str,
    subject: str,
    plain: str,
    html: str,
) -> bool:
    """Send via Resend API (production)."""
    if not settings.RESEND_API_KEY:
        logger.error("Resend | RESEND_API_KEY is not set in .env")
        return False

    try:
        import resend
        resend.api_key = settings.RESEND_API_KEY
        resend.Emails.send({
            "from":    settings.MAIL_FROM,
            "to":      [to_email],
            "subject": subject,
            "html":    html,
            "text":    plain,
        })
        logger.info("Resend | verification email sent → %s", to_email)
        return True

    except Exception as exc:
        logger.exception("Resend | error sending to %s: %s", to_email, exc)
        return False


# ─── Public interface ─────────────────────────────────────────────────────────

async def send_verification_email(
    email: str,
    owner_name: str,
    org_name: str,
    raw_token: str,
) -> bool:
    verify_url = f"{settings.BACKEND_BASE_URL}/auth/verify?token={raw_token}"
    subject = "Verify your Doers account"
    plain, html = _build_email(owner_name, org_name, verify_url)

    if settings.MAIL_PROVIDER == "resend":
        return await _send_via_resend(email, subject, plain, html)

    return await _send_via_smtp(email, subject, plain, html)