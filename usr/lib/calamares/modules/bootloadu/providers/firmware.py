from __future__ import annotations

from context import efi_device
from .base import Provider, run_target, write_text


class FirmwareProvider(Provider):
    method = ""

    def execute(self) -> None:
        write_text(self.context.target_path("/etc/kernel/cmdline"), self.cmdline + "\n")
        disk, partition = efi_device(self.context.partitions, self.context.esp_mount_point)
        write_text(
            self.context.target_path("/etc/catos/firmware-boot.conf"),
            "\n".join(
                [
                    "[boot]",
                    f"method = {self.method}",
                    f"esp_path = {self.context.esp_mount_point}",
                    f"efi_disk = {disk}",
                    f"efi_partition = {partition}",
                    f"label_prefix = {self.context.branding_name}",
                    "default_kernel = linux",
                ]
            )
            + "\n",
        )
        run_target(
            ["catos-firmware-boot-update", "--force"],
            f"prepare {self.method} direct firmware boot artifacts",
        )

    def verify(self) -> None:
        run_target(
            ["catos-firmware-boot-update", "--force", "--verify"],
            f"verify {self.method} direct firmware boot artifacts",
        )
