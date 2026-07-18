from __future__ import annotations

from .base import BootloaduError, Provider, discover_kernels, enable_if_present, run_target, setup_snapper, write_text


class SystemdBootProvider(Provider):
    def execute(self) -> None:
        write_text(self.context.target_path("/etc/kernel/cmdline"), self.cmdline + "\n")
        write_text(
            self.context.target_path("/etc/kernel/install.conf.d/20-catos-bootloadu.conf"),
            f"BOOT_ROOT={self.context.esp_mount_point}\nlayout=bls\n",
        )
        run_target(["bootctl", f"--esp-path={self.context.esp_mount_point}", "install"], "install systemd-boot")
        for kernel in discover_kernels(self.context):
            run_target(["kernel-install", "add", kernel.version, kernel.image], f"install kernel {kernel.package}")

    def setup_snapshots(self) -> None:
        setup_snapper(self.context)
        run_target(["sdboot-btrfs", "generate"], "generate systemd-boot snapshot entries")
        run_target(["sdboot-btrfs", "verify"], "verify systemd-boot snapshot entries")
        enable_if_present(self.context, "sdboot-btrfs.service")

    def verify(self) -> None:
        loader = self.context.esp_path / "loader/loader.conf"
        entries = self.context.esp_path / "loader/entries"
        if not loader.is_file() or not entries.is_dir():
            raise BootloaduError("systemd-boot entries were not generated")

        machine_id_path = self.context.target_path("/etc/machine-id")
        try:
            machine_id = machine_id_path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise BootloaduError(f"cannot read machine-id from {machine_id_path}: {error}") from error
        bootable = []
        for kernel in discover_kernels(self.context):
            directory = self.context.esp_path / machine_id / kernel.version
            matching_entries = list(entries.glob(f"{machine_id}-{kernel.version}*.conf"))
            if (directory / "linux").is_file() and (directory / "initrd").is_file() and matching_entries:
                bootable.append(kernel.package)
        if not bootable:
            raise BootloaduError("systemd-boot did not generate a normal bootable kernel entry")

        run_target(["bootctl", f"--esp-path={self.context.esp_mount_point}", "is-installed"], "verify systemd-boot installation")
