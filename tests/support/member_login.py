"""Obtain member sessions through real email proof in isolated test runtimes."""

import re


class CapturedCodeMail:
    """Capture only the email adapter boundary for a test sign-in."""

    configured = True

    def send(self, recipient, subject, body):
        """Record the generated code without weakening the identity service."""
        self.code = re.search(r"\b\d{6}\b", body).group()


def member_token(client, payload):
    """Sign in a provisioned member; provisioning itself never returns a token."""
    auth = client.app.state.authentication_services.authentication
    previous_sender, previous_key = auth.email_sender, auth.email_login_hmac_key
    mail = CapturedCodeMail()
    try:
        auth.email_sender = mail
        auth.email_login_hmac_key = "test-email-proof-" * 4
        user = payload["user"]
        challenge = auth.start_email_login(user["email"], org_id=user["org_id"])
        _, _, _, token = auth.verify_email_login(challenge, mail.code)
        return token
    finally:
        auth.email_sender, auth.email_login_hmac_key = previous_sender, previous_key
