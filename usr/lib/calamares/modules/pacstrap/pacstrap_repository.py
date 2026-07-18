from __future__ import annotations

from pathlib import Path
import shutil


CACHYOS_SELECTION = "cachyos"
GENERATED_DIR = Path("/run/calamares/cachyos")
GENERATED_PACMAN_CONFIG = GENERATED_DIR / "pacman.conf"
TARGET_PACMAN_CONFIG = GENERATED_DIR / "target-pacman.conf"
MIRRORLIST_NAMES = (
    "cachyos-mirrorlist",
    "cachyos-v3-mirrorlist",
    "cachyos-v4-mirrorlist",
)
PACKAGE_REPLACEMENTS = {
    "linux": "linux-cachyos",
    "linux-headers": "linux-cachyos-headers",
}


def pacman_config_for(selection: str) -> str:
    if selection == CACHYOS_SELECTION:
        return str(GENERATED_PACMAN_CONFIG)
    return "/etc/pacman.conf"


def transform_packages(packages: list[str], selection: str) -> list[str]:
    if selection != CACHYOS_SELECTION:
        return list(packages)

    transformed = [PACKAGE_REPLACEMENTS.get(package, package) for package in packages]
    return list(dict.fromkeys(transformed))


def install_repository_config(
    target_root: str | Path,
    selection: str,
    generated_dir: str | Path = GENERATED_DIR,
) -> bool:
    if selection != CACHYOS_SELECTION:
        return False

    target = Path(target_root)
    generated = Path(generated_dir)
    source_config = generated / TARGET_PACMAN_CONFIG.name
    source_mirrorlists = generated / "pacman.d"
    if not source_config.is_file():
        raise FileNotFoundError(f"missing generated pacman config: {source_config}")

    target_config = target / "etc/pacman.conf"
    target_config.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_config, target_config)

    target_mirrorlists = target / "etc/pacman.d"
    target_mirrorlists.mkdir(parents=True, exist_ok=True)
    for filename in MIRRORLIST_NAMES:
        source = source_mirrorlists / filename
        if not source.is_file():
            raise FileNotFoundError(f"missing generated CachyOS mirrorlist: {source}")
        shutil.copy2(source, target_mirrorlists / filename)
    return True
