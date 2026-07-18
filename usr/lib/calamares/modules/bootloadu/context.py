from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import platform
import re
from typing import Any


class ContextError(RuntimeError):
    pass


def normalize_architecture(value: str) -> str:
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(value, value)


def root_partition(partitions: list[dict[str, Any]]) -> dict[str, Any]:
    for partition in partitions:
        if partition.get("mountPoint") == "/":
            return partition
    raise ContextError("root partition is missing")


def efi_device(partitions: list[dict[str, Any]], mount_point: str) -> tuple[str, int]:
    for partition in partitions:
        if partition.get("mountPoint") != mount_point:
            continue
        device = str(partition.get("device", ""))
        for pattern in (r"^(?P<disk>/dev/(?:nvme\d+n\d+|mmcblk\d+))p(?P<part>\d+)$", r"^(?P<disk>/dev/[a-zA-Z]+)(?P<part>\d+)$"):
            match = re.match(pattern, device)
            if match:
                return match.group("disk"), int(match.group("part"))
        raise ContextError(f"cannot split EFI partition device: {device}")
    raise ContextError(f"EFI partition mounted at {mount_point} is missing")


@dataclass(frozen=True)
class BootContext:
    root: Path
    provider_id: str
    firmware: str
    architecture: str
    partitions: list[dict[str, Any]]
    root_filesystem: str
    root_uuid: str
    root_subvolume: str
    esp_mount_point: str
    snapshots_enabled: bool
    bootloader_install_path: str
    branding_name: str

    @classmethod
    def from_global_storage(cls, storage: Any, configuration: dict[str, Any], default_provider: str) -> "BootContext":
        partitions = list(storage.value("partitions") or [])
        root = Path(storage.value("rootMountPoint") or "")
        if not root.is_absolute() or not root.exists():
            raise ContextError(f"invalid target root: {root}")
        provider_id = (
            storage.value("bootloader.selected")
            or storage.value("packagechooser_bootloader")
            or configuration.get("defaultProvider")
            or default_provider
        )
        root_part = root_partition(partitions)
        boot_loader = storage.value("bootLoader") or {}
        branding = storage.value("branding") or {}
        return cls(
            root=root,
            provider_id=str(provider_id),
            firmware=str(storage.value("firmwareType") or "bios"),
            architecture=normalize_architecture(platform.machine()),
            partitions=partitions,
            root_filesystem=str(root_part.get("fs", "")),
            root_uuid=str(root_part.get("uuid", "")),
            root_subvolume=str(storage.value("btrfsRootSubvolume") or ""),
            esp_mount_point=str(storage.value("efiSystemPartition") or "/boot/efi"),
            snapshots_enabled=bool(storage.value("snapshots.enabled")),
            bootloader_install_path=str(boot_loader.get("installPath", "")),
            branding_name=str(branding.get("bootloaderEntryName") or "CatOS"),
        )

    def target_path(self, path: str | Path) -> Path:
        return self.root / str(path).lstrip("/")

    @property
    def root_partition(self) -> dict[str, Any]:
        return root_partition(self.partitions)

    @property
    def esp_path(self) -> Path:
        return self.target_path(self.esp_mount_point)

    @property
    def root_encrypted(self) -> bool:
        return bool(self.root_partition.get("luksMapperName"))
