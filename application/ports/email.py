"""Email delivery boundary used by account authentication."""

from typing import Protocol


class EmailSender(Protocol):
    """Deliver plain-text account authentication messages."""

    @property
    def configured(self) -> bool:
        """Whether a usable delivery channel has been configured."""
        ...

    def send(self, recipient: str, subject: str, body: str) -> None:
        """Deliver one message or raise an operational delivery error."""
        ...
