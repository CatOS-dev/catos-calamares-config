from __future__ import annotations

import shlex

from .base import (
    BootloaduError,
    Provider,
    build_initramfs,
    discover_kernels,
    run_target,
    write_text,
)


class RefindProvider(Provider):
    def execute(self) -> None:
        build_initramfs(self.context)
        write_text(self.context.target_path("/etc/kernel/cmdline"), self.cmdline + "\n")
        run_target(["refind-install", "--yes"], "install rEFInd")
        write_text(
            self.context.target_path("/boot/refind_linux.conf"),
            self._boot_options(),
        )

    def verify(self) -> None:
        directory = self.context.esp_path / "EFI/refind"
        binary = directory / "refind_x64.efi"
        config = directory / "refind.conf"
        options = self.context.target_path("/boot/refind_linux.conf")
        if not binary.is_file() or not config.is_file() or not options.is_file():
            raise BootloaduError("rEFInd installation is incomplete")
        option_text = options.read_text(encoding="utf-8")
        if not option_text.strip() or self.cmdline not in option_text:
            raise BootloaduError("rEFInd kernel options do not match the installed system")

        bootable = [
            kernel.package
            for kernel in discover_kernels(self.context)
            if self.context.target_path(f"/boot/vmlinuz-{kernel.package}").is_file()
            and self.context.target_path(kernel.initramfs).is_file()
        ]
        if not bootable:
            raise BootloaduError("rEFInd did not find a kernel with a matching initramfs")

    def _boot_options(self) -> str:
        minimal = [
            token
            for token in shlex.split(self.cmdline)
            if token.startswith(("root=", "rootflags=", "cryptdevice="))
        ]
        minimal_line = " ".join(["ro", *minimal])
        return "\n".join(
            [
                f'"Boot with standard options" "{self.cmdline}"',
                f'"Boot to single-user mode" "{self.cmdline} single"',
                f'"Boot with minimal options" "{minimal_line}"',
            ]
        ) + "\n"
