from __future__ import annotations

from .firmware import FirmwareProvider


class EfistubProvider(FirmwareProvider):
    method = "efistub"
