from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re
import shlex

import libcalamares


class BootloaduError(RuntimeError):
    pass


def run_target(arguments: list[str], description: str) -> None:
    libcalamares.utils.debug(f"bootloadu: {description}: {shlex.join(arguments)}")
    result = libcalamares.utils.target_env_call(arguments)
    if result != 0:
        raise BootloaduError(f"{description} failed with exit code {result}")


def target_has(program: str) -> bool:
    return libcalamares.utils.target_env_call(["sh", "-c", f"command -v {shlex.quote(program)} >/dev/null 2>&1"]) == 0


def replace_efi_entry(
    label: str,
    disk: str,
    partition: int,
    loader: str,
    options: str | None = None,
) -> None:
    cleanup = r'''
label=$1
efibootmgr | awk -v wanted="$label" '
    /^Boot[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]/ {
        id = substr($1, 5, 4)
        $1 = ""
        sub(/^ +/, "")
        if ($0 == wanted || index($0, wanted " ") == 1) print id
    }
' | while IFS= read -r id; do
    [ -n "$id" ] && efibootmgr --bootnum "$id" --delete-bootnum
done
'''
    run_target(["sh", "-c", cleanup, "bootloadu", label], f"remove old EFI entries for {label}")
    command = [
        "efibootmgr",
        "--create",
        "--disk",
        disk,
        "--part",
        str(partition),
        "--label",
        label,
        "--loader",
        loader,
    ]
    if options:
        command += ["--unicode", options]
    run_target(command, f"register EFI entry {label}")


def write_text(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".bootloadu.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _mkinitcpio_arrays(path: Path) -> dict[str, list[str]]:
    arrays = {"HOOKS": [], "MODULES": [], "FILES": [], "BINARIES": []}
    if not path.is_file():
        return arrays
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*(HOOKS|MODULES|FILES|BINARIES)=\((.*)\)\s*$", line)
        if not match:
            continue
        try:
            arrays[match.group(1)] = shlex.split(match.group(2))
        except ValueError as error:
            raise BootloaduError(f"cannot parse {match.group(1)} in {path}: {error}") from error
    return arrays


def mkinitcpio_uses_systemd(context) -> bool:
    hooks = _mkinitcpio_arrays(context.target_path("/etc/mkinitcpio.conf"))["HOOKS"]
    return "systemd" in hooks


def has_unencrypted_separate_boot(context) -> bool:
    return any(
        partition.get("mountPoint") == "/boot" and not partition.get("luksMapperName")
        for partition in context.partitions
    )


def kernel_cmdline(context) -> str:
    parameters = ["quiet", "rw"]
    if target_has("plymouth"):
        parameters.append("splash")

    use_systemd_naming = target_has("dracut") or mkinitcpio_uses_systemd(context)
    separate_boot = has_unencrypted_separate_boot(context)
    root = context.root_partition
    if root.get("luksMapperName"):
        luks_uuid = root.get("luksUuid", "")
        mapper = root["luksMapperName"]
        if use_systemd_naming:
            parameters.append(f"rd.luks.uuid={luks_uuid}")
            parameters.append(f"rd.luks.name={luks_uuid}={mapper}")
            if not separate_boot and context.target_path("/crypto_keyfile.bin").is_file():
                parameters.append("rd.luks.key=/crypto_keyfile.bin")
        else:
            parameters.append(f"cryptdevice=UUID={luks_uuid}:{mapper}")
        parameters.append(f"root=/dev/mapper/{mapper}")
    elif context.root_uuid:
        parameters.append(f"root=UUID={context.root_uuid}")

    if context.root_filesystem == "btrfs" and context.root_subvolume:
        parameters.append(f"rootflags=subvol={context.root_subvolume}")

    for partition in context.partitions:
        if partition.get("fs") != "linuxswap" or not partition.get("claimed"):
            continue
        mapper = partition.get("luksMapperName")
        if mapper:
            if use_systemd_naming and partition.get("luksUuid"):
                luks_uuid = partition["luksUuid"]
                parameters.append(f"rd.luks.uuid={luks_uuid}")
                parameters.append(f"rd.luks.name={luks_uuid}={mapper}")
            parameters.append(f"resume=/dev/mapper/{mapper}")
        elif partition.get("uuid"):
            parameters.append(f"resume=UUID={partition['uuid']}")

    return " ".join(dict.fromkeys(item for item in parameters if item and not item.endswith("=")))


def _write_mkinitcpio_arrays(path: Path, arrays: dict[str, list[str]]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(arrays)
    output: list[str] = []
    for line in lines:
        match = re.match(r"\s*(HOOKS|MODULES|FILES|BINARIES)=", line)
        if not match:
            output.append(line)
            continue
        key = match.group(1)
        if key in remaining:
            output.append(f"{key}=({' '.join(remaining.pop(key))})")
    for key in ("MODULES", "BINARIES", "FILES", "HOOKS"):
        if key in remaining:
            output.append(f"{key}=({' '.join(remaining.pop(key))})")
    write_text(path, "\n".join(output) + "\n")


def configure_mkinitcpio(context) -> None:
    path = context.target_path("/etc/mkinitcpio.conf")
    arrays = _mkinitcpio_arrays(path)
    arrays["FILES"] = [path for path in arrays["FILES"] if path != "/crypto_keyfile.bin"]
    use_systemd = "systemd" in arrays["HOOKS"]

    if use_systemd:
        hooks = ["systemd", "autodetect", "microcode", "kms", "modconf", "block", "keyboard", "sd-vconsole"]
    else:
        hooks = ["base", "udev", "autodetect", "microcode", "kms", "modconf", "block", "keyboard", "keymap", "consolefont"]

    if target_has("plymouth"):
        hooks.append("plymouth")

    uses_btrfs = False
    uses_lvm = False
    encrypted_root = False
    encrypted_swap = False
    has_swap = False
    for partition in context.partitions:
        filesystem = str(partition.get("fs", ""))
        if filesystem == "linuxswap" and not partition.get("claimed"):
            continue
        if filesystem == "linuxswap":
            has_swap = True
            encrypted_swap = encrypted_swap or bool(partition.get("luksMapperName"))
        if filesystem == "btrfs":
            uses_btrfs = True
        if "lvm2" in filesystem:
            uses_lvm = True
        if partition.get("mountPoint") == "/" and partition.get("luksMapperName"):
            encrypted_root = True
        if partition.get("mountPoint") == "/usr":
            hooks.append("usr")

    if encrypted_root:
        hooks.append("sd-encrypt" if use_systemd else "encrypt")
        keyfile = context.target_path("/crypto_keyfile.bin")
        if not has_unencrypted_separate_boot(context) and keyfile.is_file():
            arrays["FILES"].append("/crypto_keyfile.bin")
    if uses_lvm:
        hooks.append("lvm2")
    if encrypted_root and encrypted_swap and not use_systemd:
        hooks.append("openswap")
    if has_swap:
        hooks.append("resume")
    hooks.append("filesystems")
    if not uses_btrfs:
        hooks.append("fsck")

    arrays["HOOKS"] = list(dict.fromkeys(hooks))
    arrays["MODULES"] = list(dict.fromkeys(arrays["MODULES"]))
    arrays["BINARIES"] = list(dict.fromkeys(arrays["BINARIES"]))
    arrays["FILES"] = list(dict.fromkeys(arrays["FILES"]))
    _write_mkinitcpio_arrays(path, arrays)


@dataclass(frozen=True)
class Kernel:
    version: str
    package: str
    image: str
    initramfs: str


def discover_kernels(context) -> list[Kernel]:
    modules = context.target_path("/usr/lib/modules")
    kernels: list[Kernel] = []
    if not modules.is_dir():
        raise BootloaduError("/usr/lib/modules is missing in target")
    for version_dir in sorted(modules.iterdir()):
        pkgbase = version_dir / "pkgbase"
        image = version_dir / "vmlinuz"
        if not pkgbase.is_file() or not image.is_file():
            continue
        package = pkgbase.read_text(encoding="utf-8").strip()
        if not package:
            continue
        kernels.append(
            Kernel(
                version=version_dir.name,
                package=package,
                image=f"/usr/lib/modules/{version_dir.name}/vmlinuz",
                initramfs=f"/boot/initramfs-{package}.img",
            )
        )
    if not kernels:
        raise BootloaduError("no installed kernels found")
    return kernels


def build_initramfs(context) -> list[Kernel]:
    configure_mkinitcpio(context)
    run_target(["mkinitcpio", "-P"], "generate initramfs")
    kernels = discover_kernels(context)
    missing = [kernel.initramfs for kernel in kernels if not context.target_path(kernel.initramfs).is_file()]
    if missing:
        raise BootloaduError(f"missing generated initramfs: {', '.join(missing)}")
    return kernels


def setup_snapper(context) -> None:
    config = context.target_path("/etc/snapper/configs/root")
    if not config.exists():
        run_target(["snapper", "--no-dbus", "-c", "root", "create-config", "--template", "catos-root", "/"], "create Snapper root configuration")
    run_target(["systemctl", "enable", "snapper-cleanup.timer"], "enable Snapper cleanup timer")


def enable_if_present(context, unit: str) -> None:
    unit_paths = [
        context.target_path(f"/usr/lib/systemd/system/{unit}"),
        context.target_path(f"/etc/systemd/system/{unit}"),
    ]
    if any(path.exists() for path in unit_paths):
        run_target(["systemctl", "enable", unit], f"enable {unit}")


class Provider:
    def __init__(self, context, profile):
        self.context = context
        self.profile = profile
        self.cmdline = kernel_cmdline(context)

    def validate(self) -> None:
        if self.context.firmware == "efi" and not self.context.esp_path.is_dir():
            raise BootloaduError(f"EFI system partition is not mounted at {self.context.esp_mount_point}")

    def execute(self) -> None:
        raise NotImplementedError

    def setup_snapshots(self) -> None:
        raise BootloaduError(f"{self.context.provider_id} does not support bootable snapshots")

    def verify(self) -> None:
        raise NotImplementedError
