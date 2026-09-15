from __future__ import annotations

import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings


class OfflinePathError(ValueError):
    """A configured offline path escaped the private data root."""


@dataclass(frozen=True)
class OfflinePaths:
    root: Path

    @classmethod
    def from_settings(cls) -> "OfflinePaths":
        configured = getattr(settings, "OFFLINE_DATA_ROOT", None)
        if not configured:
            raise OfflinePathError("OFFLINE_DATA_ROOT is not configured")
        return cls(Path(configured).resolve())

    def _child(self, *parts: str) -> Path:
        target = (self.root.joinpath(*parts)).resolve()
        if target != self.root and self.root not in target.parents:
            raise OfflinePathError("offline data path escapes the private root")
        return target

    @property
    def database(self) -> Path:
        return self._child("database", "hr_evaluation.sqlite3")

    @property
    def reporting(self) -> Path:
        return self._child("reporting")

    @property
    def backups(self) -> Path:
        return self._child("backups")

    @property
    def logs(self) -> Path:
        return self._child("logs")

    @property
    def locks(self) -> Path:
        return self._child("locks")

    @property
    def lock(self) -> Path:
        return self._child("locks", "offline.lock")

    @property
    def secrets(self) -> Path:
        return self._child("secrets")

    @property
    def secret(self) -> Path:
        return self._child("secrets", "django-secret.key")

    @property
    def staging(self) -> Path:
        return self._child("staging")

    @property
    def exports(self) -> Path:
        return self._child("exports")

    @property
    def state(self) -> Path:
        return self._child("state")

    @property
    def dirty_marker(self) -> Path:
        return self._child("state", "dirty-start.json")

    @property
    def clean_marker(self) -> Path:
        return self._child("state", "clean-shutdown.json")

    @property
    def recovery_diagnostic(self) -> Path:
        return self._child("state", "recovery-diagnostic.json")

    @property
    def replacement_staging_parent(self) -> Path:
        """Future whole-root replacements must stage beside, never inside, root."""
        return self.root.parent.resolve()

    def ensure_layout(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in (
            self.database.parent,
            self.reporting,
            self.backups,
            self.logs,
            self.locks,
            self.secrets,
            self.staging,
            self.exports,
            self.state,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def ensure_secret(self) -> None:
        self.ensure_layout()
        if self.secret.exists():
            return
        secret = secrets.token_urlsafe(64)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            descriptor = os.open(self.secret, flags, stat.S_IRUSR | stat.S_IWUSR)
        except FileExistsError:
            return
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(secret)
        try:
            os.chmod(self.secret, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            # Windows ACL handling is best effort; the current user created the file.
            pass
