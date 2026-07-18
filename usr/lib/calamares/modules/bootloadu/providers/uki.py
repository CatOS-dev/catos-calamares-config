from __future__ import annotations

from .firmware import FirmwareProvider


class UkiProvider(FirmwareProvider):
    method = "uki"
