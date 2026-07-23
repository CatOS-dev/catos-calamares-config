#!/usr/bin/env python3
from __future__ import annotations

import gettext
from pathlib import Path
import sys

import libcalamares

MODULE_DIR = Path(__file__).resolve().parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from context import BootContext, ContextError  # noqa: E402
from providers import PROVIDERS  # noqa: E402
from providers.base import BootloaduError, configure_mkinitcpio  # noqa: E402
from registry import RegistryError, install_marker, load_bootloader_registry, platform_supported, provider_profile  # noqa: E402
from secureboot import enable_target_secure_boot, prepare_secure_boot  # noqa: E402

_ = gettext.translation(
    "calamares-python",
    localedir=libcalamares.utils.gettext_path(),
    languages=libcalamares.utils.gettext_languages(),
    fallback=True,
).gettext


def pretty_name():
    return _("Prepare the boot environment")


def run():
    try:
        registry_path = libcalamares.job.configuration.get("registry", "/usr/share/calamares/catos/bootloaders.yaml")
        registry = load_bootloader_registry(registry_path)
        context = BootContext.from_global_storage(
            libcalamares.globalstorage,
            libcalamares.job.configuration,
            registry.get("defaultProvider", "grub"),
        )
        phase = libcalamares.job.configuration.get("phase", "install")
        if phase == "secureboot":
            enable_target_secure_boot(libcalamares.globalstorage, registry, context)
            libcalamares.job.setprogress(1.0)
            return None
        if phase != "install":
            raise BootloaduError(f"unknown bootloadu phase: {phase}")

        prepare_secure_boot(libcalamares.globalstorage, registry, context)
        profile = provider_profile(registry, context.provider_id)
        if not platform_supported(profile, context.firmware, context.architecture):
            raise BootloaduError(
                f"{context.provider_id} is not supported on {context.firmware}/{context.architecture}"
            )
        if context.snapshots_enabled and not isinstance(profile.get("snapshots"), dict):
            raise BootloaduError(f"{context.provider_id} does not support bootable snapshots")
        provider_type = PROVIDERS.get(context.provider_id)
        if provider_type is None:
            raise BootloaduError(f"provider implementation is missing: {context.provider_id}")
        marker_path = install_marker(registry)
        marker_paths = [Path(marker_path), context.target_path(marker_path)]
        for marker in marker_paths:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch(exist_ok=True)
        configure_mkinitcpio(context)
        provider = provider_type(context, profile)
        provider.validate()
        provider.execute()
        if context.snapshots_enabled:
            if context.root_filesystem != "btrfs":
                raise BootloaduError("bootable snapshots require a Btrfs root filesystem")
            provider.setup_snapshots()
        provider.verify()
        for marker in marker_paths:
            marker.unlink(missing_ok=True)
        libcalamares.job.setprogress(1.0)
        return None
    except (BootloaduError, ContextError, RegistryError, OSError, ValueError) as error:
        libcalamares.utils.error(f"bootloadu: {error}")
        return _("Boot setup failed"), str(error)
