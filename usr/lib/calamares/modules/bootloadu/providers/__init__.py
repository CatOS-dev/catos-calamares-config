from .efistub import EfistubProvider
from .grub import GrubProvider
from .limine import LimineProvider
from .systemd_boot import SystemdBootProvider
from .uki import UkiProvider

PROVIDERS = {
    "grub": GrubProvider,
    "limine": LimineProvider,
    "systemd-boot": SystemdBootProvider,
    "uki": UkiProvider,
    "efistub": EfistubProvider,
}
