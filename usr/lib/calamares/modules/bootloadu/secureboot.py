from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import libcalamares

from providers.base import BootloaduError
from registry import secure_boot_profile


DEFAULT_EFIVARS_ROOT = Path("/sys/firmware/efi/efivars")


def secure_boot_enabled(efivars_root: Path = DEFAULT_EFIVARS_ROOT) -> bool:
    if not efivars_root.is_dir():
        return False
    variables = sorted(efivars_root.glob("SecureBoot-*"))
    if not variables:
        return False
    try:
        data = variables[0].read_bytes()
    except OSError:
        return False
    return len(data) >= 5 and data[4] == 1


def _storage_enabled(storage: Any) -> bool:
    configured = storage.value("secureboot.enabled")
    if configured is None:
        configured = secure_boot_enabled()
        storage.insert("secureboot.enabled", configured)
    return bool(configured)


def prepare_secure_boot(storage: Any, registry: dict[str, Any], context: Any) -> bool:
    configured = storage.value("secureboot.enabled")
    enabled = bool(configured) if configured is not None else (
        context.firmware == "efi" and secure_boot_enabled()
    )
    storage.insert("secureboot.enabled", enabled)
    if enabled:
        if context.firmware != "efi":
            raise BootloaduError("Secure Boot was detected without EFI firmware")
        secure_boot_profile(registry, context.provider_id)
    return enabled


def enable_target_secure_boot(storage: Any, registry: dict[str, Any], context: Any) -> dict[str, Any] | None:
    if not _storage_enabled(storage):
        return None
    if context.firmware != "efi":
        raise BootloaduError("Secure Boot was detected without EFI firmware")

    secure_boot_profile(registry, context.provider_id)
    arguments = [
        "catos-secureboot",
        "enable",
        "--provider",
        context.provider_id,
        "--generate-enrollment-password",
        "--json",
    ]
    try:
        output = libcalamares.utils.check_target_env_output(arguments)
    except Exception as error:
        raise BootloaduError(f"CatOS Secure Boot setup failed: {error}") from error

    try:
        payload = json.loads(output)
    except (TypeError, json.JSONDecodeError) as error:
        raise BootloaduError("catos-secureboot returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise BootloaduError("catos-secureboot returned an invalid result")

    fingerprint = payload.get("fingerprint")
    enrollment_pending = payload.get("enrollment_pending")
    provider = payload.get("provider")
    boot_chain_verified = payload.get("boot_chain_verified")
    deployed_kernels_verified = payload.get("deployed_kernels_verified")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise BootloaduError("catos-secureboot did not return a certificate fingerprint")
    if not isinstance(enrollment_pending, bool):
        raise BootloaduError("catos-secureboot did not report enrollment state")
    if provider != context.provider_id:
        raise BootloaduError(
            f"catos-secureboot verified provider {provider!r}, expected {context.provider_id!r}"
        )
    if boot_chain_verified is not True:
        raise BootloaduError("catos-secureboot did not verify the installed boot chain")
    if context.provider_id == "grub" and (
        isinstance(deployed_kernels_verified, bool)
        or not isinstance(deployed_kernels_verified, int)
        or deployed_kernels_verified < 1
    ):
        raise BootloaduError("catos-secureboot did not verify a deployed GRUB kernel")

    storage.insert("secureboot.certificateFingerprint", fingerprint)
    storage.insert("secureboot.enrollmentPending", enrollment_pending)
    password = payload.get("enrollment_password")
    if password is not None:
        if not isinstance(password, str) or not password:
            raise BootloaduError("catos-secureboot returned an invalid enrollment password")
        storage.insert("secureboot.enrollmentPassword", password)

    libcalamares.utils.debug(
        "bootloadu: machine Secure Boot key prepared; enrollment pending="
        + str(enrollment_pending).lower()
    )
    return payload
