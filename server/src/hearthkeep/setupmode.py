"""Setup mode: the one-time secret that lets the browser `hearthkeep setup` opened fetch the
first-device enrolment code.

The secret is made here, held only in memory, compared in constant time and dropped as soon
as the first passkey exists. It reaches the browser only in the URL fragment of the setup link
(never sent to the server as part of a URL) and comes back only in POST bodies, under the
usual CSRF and Origin checks. Closing the setup window ends the process, and the secret with
it."""

from __future__ import annotations

import hmac
import secrets


class SetupMode:
    """The state of one setup run: active while the secret is held and setup isn't finished."""

    def __init__(self, secret: str | None = None) -> None:
        self._secret: str | None = secret if secret is not None else secrets.token_urlsafe(32)
        self.completed = False
        # Whether this run made the data folder (so it may still move it, before any download).
        self.created_folder = False

    def __repr__(self) -> str:  # never the secret, in a traceback or a debug print
        return f"SetupMode(active={self.active}, completed={self.completed})"

    @property
    def secret(self) -> str | None:
        """The secret, used only to build the setup link; None once setup is finished."""
        return self._secret

    @property
    def active(self) -> bool:
        return self._secret is not None and not self.completed

    def check(self, candidate: str) -> bool:
        """Whether `candidate` is the secret, compared in constant time; False when setup is
        not active."""
        secret = self._secret
        if secret is None or self.completed or not isinstance(candidate, str):
            return False
        return hmac.compare_digest(candidate.encode("utf-8"), secret.encode("utf-8"))

    def complete(self) -> None:
        """The first passkey exists: drop the secret, for good."""
        self._secret = None
        self.completed = True


def setup_url(origin: str, secret: str) -> str:
    """The setup link: the secret rides in the fragment, which browsers never send."""
    return f"{origin.rstrip('/')}/setup#{secret}"
