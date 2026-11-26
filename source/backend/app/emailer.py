"""Small SMTP delivery helper for dashboard emails."""
from __future__ import annotations
import os
import smtplib
from email.message import EmailMessage

def send_invitation(*, recipient: str, dashboard_name: str | None, dashboard_url: str) -> None:
    host = os.environ.get("GMAIL_SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("GMAIL_SMTP_PORT", "587"))
    sender = os.environ.get("GMAIL_SMTP_FROM", "").strip()
    password = os.environ.get("GMAIL_SMTP_PASSWORD", "").strip()
    if not sender or not password:
        raise RuntimeError("GMAIL_SMTP_FROM and GMAIL_SMTP_PASSWORD must be configured")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    access_text = f"access {dashboard_name}" if dashboard_name else "create a new Home Energy dashboard"
    message["Subject"] = f"Invitation to {dashboard_name}" if dashboard_name else "Invitation to create a Home Energy dashboard"
    message.set_content(f"""You have been invited to {access_text} ({dashboard_url}).

You will also receive an email from Tailscale notifying you that ej.sta.ana@gmail.com has shared a device with you. Follow the instructions in that email.

1. Join the private Tailscale network from https://login.tailscale.com/start.
2. Install Tailscale on desktop from https://tailscale.com/download and sign in with the invited account.
3. On mobile, install Tailscale from the App Store or Google Play and sign in with the same account.
4. Once Tailscale is connected, open {dashboard_url} and choose Sign in with Google.
5. Sign in using this email address: {recipient}

This invitation expires in seven days. {"You will see the configured dashboard after signing in; no provider credentials need to be entered." if dashboard_name else "After signing in, create and configure your own dashboard."}
""")
    # Gmail supports implicit TLS on 465 and STARTTLS on 587.  Keep both
    # deployment-friendly so the SMTP port in the production environment is
    # the single source of truth.
    client = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
    with client(host, port, timeout=15) as smtp:
        if port != 465:
            smtp.starttls()
        smtp.login(sender, password)
        smtp.send_message(message)
