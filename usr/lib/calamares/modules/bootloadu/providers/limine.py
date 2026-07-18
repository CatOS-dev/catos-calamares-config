from __future__ import annotations

from .base import (
    BootloaduError,
    Provider,
    discover_kernels,
    enable_if_present,
    run_target,
    setup_snapper,
    write_text,
)


class LimineProvider(Provider):
    def execute(self) -> None:
        write_text(self.context.target_path("/etc/kernel/cmdline"), self.cmdline + "\n")
        write_text(
            self.context.target_path("/etc/default/limine"),
            "\n".join(
                [
                    f'TARGET_OS_NAME="{self.context.branding_name}"',
                    f'ESP_PATH="{self.context.esp_mount_point}"',
                    f"KERNEL_CMDLINE[default]={self.cmdline}",
                    "ENABLE_UKI=no",
                    "ENABLE_LIMINE_FALLBACK=yes",
                ]
            )
            + "\n",
        )
        run_target(["limine-install"], "install Limine")
        run_target(["limine-update"], "generate Limine kernel entries")

    def setup_snapshots(self) -> None:
        setup_snapper(self.context)
        run_target(["limine-btrfs", "generate"], "generate Limine snapshot entries")
        run_target(["limine-btrfs", "verify"], "verify Limine snapshot entries")
        enable_if_present(self.context, "limine-btrfs.service")

    def verify(self) -> None:
        config = self.context.esp_path / "limine.conf"
        if not config.is_file() or config.stat().st_size == 0:
            raise BootloaduError("Limine configuration was not generated")

        machine_id_path = self.context.target_path("/etc/machine-id")
        try:
            machine_id = machine_id_path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise BootloaduError(f"cannot read machine-id from {machine_id_path}: {error}") from error
        deployed = []
        for kernel in discover_kernels(self.context):
            directory = self.context.esp_path / machine_id / kernel.package
            if (directory / "vmlinuz").is_file() and (directory / "initramfs").is_file():
                deployed.append(kernel.package)
        if not deployed:
            raise BootloaduError("Limine did not deploy any bootable kernel assets")

        run_target(["limine-list"], "verify Limine configuration tree")
