from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULT_REGISTRY_PATH = Path("/usr/share/calamares/catos/bootloaders.yaml")


class RegistryError(RuntimeError):
    pass


def load_bootloader_registry(path: str | Path = DEFAULT_REGISTRY_PATH) -> dict[str, Any]:
    registry_path = Path(path)
    try:
        data = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise RegistryError(f"cannot load bootloader registry {registry_path}: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("providers"), dict):
        raise RegistryError(f"invalid bootloader registry: {registry_path}")
    return data


def provider_profile(registry: dict[str, Any], provider_id: str) -> dict[str, Any]:
    profile = registry["providers"].get(provider_id)
    if not isinstance(profile, dict):
        raise RegistryError(f"unknown bootloader provider: {provider_id}")
    return profile


def platform_supported(profile: dict[str, Any], firmware: str, architecture: str) -> bool:
    aliases = {"amd64": "x86_64", "arm64": "aarch64"}
    architecture = aliases.get(architecture, architecture)
    for platform in profile.get("platforms", []):
        if platform.get("firmware") != firmware:
            continue
        architectures = [aliases.get(item, item) for item in platform.get("architectures", [])]
        if architecture in architectures:
            return True
    return False


def package_plan(
    registry: dict[str, Any],
    provider_id: str,
    *,
    snapshots_enabled: bool,
    root_filesystem: str,
) -> list[str]:
    profile = provider_profile(registry, provider_id)
    packages = list(profile.get("packages", [])) + list(profile.get("kernelPackages", []))
    if snapshots_enabled:
        snapshots = profile.get("snapshots")
        if not isinstance(snapshots, dict):
            raise RegistryError(f"{provider_id} does not support bootable snapshots")
        required = snapshots.get("requiredFileSystems", [])
        if root_filesystem not in required:
            raise RegistryError(
                f"{provider_id} snapshots require one of {required}, got {root_filesystem or 'unknown'}"
            )
        packages += list(registry.get("snapshotCommonPackages", []))
        packages += list(snapshots.get("packages", []))
    return list(dict.fromkeys(packages))



def missing_required_packages(
    required: list[str],
    available_packages: set[str],
    available_groups: set[str],
) -> list[str]:
    return [
        package
        for package in required
        if package not in available_packages and package not in available_groups
    ]

def install_marker(registry: dict[str, Any]) -> str:
    marker = registry.get("installMarker")
    if not isinstance(marker, str) or not marker.startswith("/"):
        raise RegistryError("installMarker must be an absolute path")
    return marker
