from __future__ import annotations

import re
import shlex

from .base import (
    BootloaduError,
    Provider,
    build_initramfs,
    enable_if_present,
    has_unencrypted_separate_boot,
    run_target,
    setup_snapper,
    write_text,
)


class GrubProvider(Provider):
    def execute(self) -> None:
        defaults = [
            f"GRUB_DISTRIBUTOR={shlex.quote(self.context.branding_name)}",
            f"GRUB_CMDLINE_LINUX_DEFAULT={shlex.quote(self.cmdline)}",
        ]
        if self.context.root_encrypted and not has_unencrypted_separate_boot(self.context):
            defaults.append("GRUB_ENABLE_CRYPTODISK=y")
        write_text(self.context.target_path("/etc/default/grub.d/00-catos.cfg"), "\n".join(defaults) + "\n")
        build_initramfs(self.context)
        theme_adjuster = self.context.target_path("/run/calamares/adjust_grub_theme_after.sh")
        if theme_adjuster.is_file():
            run_target(["/run/calamares/adjust_grub_theme_after.sh"], "adjust the CatOS GRUB theme")
        if self.context.firmware == "efi":
            target = "arm64-efi" if self.context.architecture == "aarch64" else "x86_64-efi"
            run_target(
                ["grub-install", f"--target={target}", f"--efi-directory={self.context.esp_mount_point}", "--bootloader-id=CatOS", "--recheck"],
                "install GRUB for UEFI",
            )
        else:
            if not self.context.bootloader_install_path:
                raise BootloaduError("BIOS GRUB install path is missing")
            run_target(["grub-install", "--target=i386-pc", "--recheck", self.context.bootloader_install_path], "install GRUB for BIOS")
        run_target(["grub-mkconfig", "-o", "/boot/grub/grub.cfg"], "generate GRUB configuration")

    def setup_snapshots(self) -> None:
        setup_snapper(self.context)
        enable_if_present(self.context, "grub-btrfsd.service")
        run_target(["grub-mkconfig", "-o", "/boot/grub/grub.cfg"], "regenerate GRUB snapshot entries")

    def verify(self) -> None:
        config = self.context.target_path("/boot/grub/grub.cfg")
        if not config.is_file() or config.stat().st_size == 0:
            raise BootloaduError("GRUB configuration was not generated")
        content = config.read_text(encoding="utf-8", errors="replace")
        lines = [line.lstrip() for line in content.splitlines()]
        has_linux_entry = "menuentry " in content and any(
            re.match(r"^(?:linux|linuxefi)\s+", line) for line in lines
        )
        has_initramfs = any(
            re.match(r"^(?:initrd|initrdefi)\s+", line) for line in lines
        )
        if not has_linux_entry or not has_initramfs:
            raise BootloaduError("GRUB did not generate a bootable Linux menu entry")
