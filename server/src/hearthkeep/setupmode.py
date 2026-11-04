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
from pathlib import Path


class SetupMode:
    """The state of one setup run: active while the secret is held and setup isn't finished."""

    def __init__(self, secret: str | None = None) -> None:
        self._secret: str | None = secret if secret is not None else secrets.token_urlsafe(32)
        self.completed = False
        # Whether this run made the data folder (so it may still move it, before any download).
        # `cmd_setup` sets it for each folder it sets up.
        self.created_folder = False
        # Whether that folder was there before this run (empty, or new to Hearthkeep): a move
        # then takes only setup's own files out of it and keeps the folder itself.
        self.folder_existed = False
        # The folder the setup page chose to move the data to; `cmd_setup` moves it there.
        self.next_data_dir: Path | None = None
        # Whether to start Hearthkeep at login once setup is finished (chosen in the wizard).
        self.start_at_login = True
        # Whether the owner has chosen it in the wizard; until then a task that starts another
        # data folder is left alone (the switch then shows that and starts off).
        self.startup_chosen = False
        # Whether the wizard chose the chat model or a model server: that writes into this data
        # folder (models.toml, config.toml, the server's key), so the folder can't move after.
        self.chat_changed = False

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
