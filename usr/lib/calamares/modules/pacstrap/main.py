#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import subprocess
import shutil
import time
import gettext

import libcalamares
from libcalamares.utils import gettext_path, gettext_languages

# Ensure local helper module is importable (pkgcheck.py in same directory)
sys.path.insert(0, "/usr/lib/calamares/modules/pacstrap")
sys.path.insert(0, "/usr/lib/calamares/modules/bootloadu")
import pkgcheck  # noqa: E402
from pacstrap_repository import (  # noqa: E402
    CACHYOS_SELECTION,
    install_repository_config,
    pacman_config_for,
    transform_packages,
)
from secureboot import secure_boot_enabled as host_secure_boot_enabled  # noqa: E402
from registry import (  # noqa: E402
    RegistryError,
    load_bootloader_registry,
    missing_required_packages,
    package_plan,
)


_translation = gettext.translation(
    "calamares-python",
    localedir=gettext_path(),
    languages=gettext_languages(),
    fallback=True,
)
_ = _translation.gettext
_n = _translation.ngettext

custom_status_message = None
status_update_time = 0
recent_output = []


class PacmanError(Exception):
    """Raised when host-side pacman/pacstrap returns non-zero."""

    def __init__(self, message, command=None, returncode=None, output=None):
        self.message = message
        self.cmd = command
        self.returncode = returncode
        self.output = output

    def __str__(self):
        return str(self.message)


def _command_text(command):
    if isinstance(command, (list, tuple)):
        return " ".join(str(part) for part in command)
    return str(command or "")


def _failure(summary, details, stage, error=None, output=None):
    context = {
        "source": "pacstrap",
        "stage": stage,
        "summary": str(summary),
        "details": str(details),
    }
    command = getattr(error, "cmd", None)
    if command:
        context["command"] = _command_text(command)
    returncode = getattr(error, "returncode", None)
    if returncode is not None:
        context["exitCode"] = int(returncode)
    captured = output
    if captured is None and error is not None:
        captured = getattr(error, "output", None)
    if captured is None:
        captured = "\n".join(recent_output)
    if captured:
        context["output"] = str(captured)
    libcalamares.globalstorage.insert("recovery.failureContext", context)
    return summary, details


def pretty_name():
    return _("Install base system")


def pretty_status_message():
    if custom_status_message is not None:
        return custom_status_message
    return None


def line_cb(line: str):
    """
    Writes every line to the debug log and displays it in calamares.
    """
    global custom_status_message
    global status_update_time
    global recent_output

    custom_status_message = line.strip()
    recent_output.append(line.rstrip())
    recent_output = recent_output[-200:]
    libcalamares.utils.debug("pacstrap: " + line.strip())

    # Throttle UI updates a bit
    if (time.time() - status_update_time) > 0.5:
        libcalamares.job.setprogress(0)
        status_update_time = time.time()


def run_in_host(command, line_func):
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
    )
    for line in proc.stdout:
        if line.strip():
            line_func(line)
    proc.wait()
    if proc.returncode != 0:
        raise PacmanError(
            f"Failed to run: {' '.join(command)} (rc={proc.returncode})",
            command=command,
            returncode=proc.returncode,
        )


def _host_capture_lines(command):
    """
    Run command on host and capture stdout lines. Raises PacmanError on failure.
    """
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
    )
    lines = []
    for line in proc.stdout:
        if line:
            lines.append(line.rstrip("\n"))
    proc.wait()
    if proc.returncode != 0:
        raise PacmanError(
            f"Failed to query: {' '.join(command)} (rc={proc.returncode})",
            command=command,
            returncode=proc.returncode,
            output="\n".join(lines),
        )
    return [l for l in lines if l]


def _has_internet():
    # Calamares commonly uses hasInternet; your module also sets "online" at the end.
    return bool(libcalamares.globalstorage.value("hasInternet")) or bool(
        libcalamares.globalstorage.value("online")
    )


def _maybe_sync_db_host(pacman_config):
    """
    Optional pacman -Sy before pkgcheck so the local sync DB isn't stale.
    Controlled by job config: sync_db (default True).
    """
    sync = libcalamares.job.configuration.get("sync_db", True)
    if not sync:
        libcalamares.utils.debug("sync_db disabled; skipping pacman -Sy.")
        return

    if not _has_internet():
        libcalamares.utils.warning("No internet detected; skipping pacman -Sy before pkgcheck.")
        return

    libcalamares.utils.debug("Syncing pacman database before pkgcheck (pacman -Sy)...")
    # Using run_in_host so output goes through line_cb for UI/log.
    run_in_host(
        ["pacman", "--config", pacman_config, "-Sy", "--noconfirm"],
        line_cb,
    )


def _build_repo_index_host(pacman_config):
    """
    Build (packages_set, groups_set) on host (live environment).
    """
    pkgs = set(
        _host_capture_lines(["pacman", "--config", pacman_config, "-Slq"])
    )
    groups = set(
        _host_capture_lines(["pacman", "--config", pacman_config, "-Sgq"])
    )
    libcalamares.utils.debug(f"[host] pacman repo index: {len(pkgs)} packages, {len(groups)} groups")
    return pkgs, groups


def run():
    """
    Installs the base system packages (pacstrap) and copies files post-installation.
    Also filters basePackages using pkgcheck: drops missing packages/groups with warnings.
    Optionally runs pacman -Sy before filtering (sync_db: true by default).
    """
    global custom_status_message
    global status_update_time
    global recent_output

    recent_output.clear()
    custom_status_message = None
    status_update_time = 0

    root_mount_point = libcalamares.globalstorage.value("rootMountPoint")

    if not root_mount_point:
        return (
            "No mount point for root partition in globalstorage",
            'globalstorage does not contain a "rootMountPoint" key, doing nothing',
        )

    if not os.path.exists(root_mount_point):
        return (
            "Bad mount point for root partition in globalstorage",
            'globalstorage["rootMountPoint"] is "{}", which does not exist, doing nothing'.format(
                root_mount_point
            ),
        )

    if not libcalamares.job.configuration:
        return "No configuration found", "Aborting due to missing configuration"

    if "basePackages" not in libcalamares.job.configuration:
        return "Package List Missing", "Cannot continue without list of packages to install"

    configured_packages = libcalamares.job.configuration["basePackages"]
    if not isinstance(configured_packages, list):
        return "Bad configuration", "basePackages must be a list"
    configured_required = libcalamares.job.configuration.get("requiredPackages", [])
    if not isinstance(configured_required, list):
        return "Bad configuration", "requiredPackages must be a list"
    base_packages = list(configured_packages)
    required_packages = list(configured_required)
    base_packages.extend(required_packages)

    repository_selection = (
        libcalamares.globalstorage.value("packagechooser_repository") or "catos"
    )
    if repository_selection not in {"catos", CACHYOS_SELECTION}:
        return (
            "Invalid repository selection",
            f"Unsupported repository selection: {repository_selection}",
        )
    pacman_config = pacman_config_for(repository_selection)
    if not os.path.isfile(pacman_config):
        return (
            "Repository configuration missing",
            f"Required pacman configuration does not exist: {pacman_config}",
        )

    bootloader = (
        libcalamares.globalstorage.value("bootloader.selected")
        or libcalamares.globalstorage.value("packagechooser_bootloader")
        or "grub"
    )
    partitions = libcalamares.globalstorage.value("partitions") or []
    root_filesystem = next(
        (partition.get("fs", "") for partition in partitions if partition.get("mountPoint") == "/"),
        "",
    )
    snapshots_enabled = bool(libcalamares.globalstorage.value("snapshots.enabled"))
    firmware = str(libcalamares.globalstorage.value("firmwareType") or "bios")
    secure_boot_active = firmware == "efi" and host_secure_boot_enabled()
    libcalamares.globalstorage.insert("secureboot.enabled", secure_boot_active)
    try:
        registry = load_bootloader_registry()
        boot_packages = package_plan(
                registry,
                str(bootloader),
                snapshots_enabled=snapshots_enabled,
                root_filesystem=root_filesystem,
                firmware=firmware,
                secure_boot_enabled=secure_boot_active,
            )
        boot_packages = transform_packages(boot_packages, repository_selection)
        base_packages.extend(boot_packages)
    except RegistryError as error:
        return "Invalid boot configuration", str(error)

    base_packages = transform_packages(base_packages, repository_selection)
    base_packages = list(dict.fromkeys(base_packages))
    libcalamares.utils.debug(
        f"Boot package plan: provider={bootloader}, snapshots={snapshots_enabled}, "
        f"rootfs={root_filesystem}, repository={repository_selection}, "
        f"pacman_config={pacman_config}, packages={base_packages}"
    )

    # Refresh the live environment's repository databases before deriving the
    # package plan. Continuing with stale metadata after a failed refresh can
    # select packages that the current mirrors no longer provide.
    try:
        _maybe_sync_db_host(pacman_config)
    except PacmanError as e:
        libcalamares.utils.warning(f"pacman database refresh failed: {e}")
        return _failure(
            "Package Manager error",
            "Could not refresh repository databases for base system installation",
            "repository-database-sync",
            error=e,
        )

    try:
        repo_pkgs, repo_groups = _build_repo_index_host(pacman_config)
        required_install_packages = list(dict.fromkeys(required_packages + boot_packages))
        missing_install_packages = missing_required_packages(
            required_install_packages, repo_pkgs, repo_groups
        )
        if missing_install_packages:
            details = "Missing required packages: " + ", ".join(missing_install_packages)
            return _failure(
                "Required installation packages are unavailable",
                details,
                "repository-metadata",
            )
        base_packages = pkgcheck.filter_operation_list(
            "basePackages",
            base_packages,
            repo_pkgs,
            repo_groups,
        )
    except PacmanError as e:
        libcalamares.utils.warning(str(e))
        return _failure(
            "Package Manager error",
            "Could not query repository metadata for base system install",
            "repository-metadata",
            error=e,
        )
    except Exception as e:
        libcalamares.utils.warning(f"pkgcheck failed: {e!s}")
        return _failure(
            "Package Manager error",
            "pkgcheck failed while preparing base system package list",
            "repository-metadata",
            error=e,
        )

    if not base_packages:
        libcalamares.utils.warning("All basePackages were filtered out (missing). Skipping pacstrap.")
        # Keep behavior: mark "online" and finish.
        libcalamares.globalstorage.insert("online", True)
        libcalamares.job.setprogress(1.0)
        return None

    # Keep pacstrap's default keyring-copy behavior. The preceding jobs have
    # already fetched and locally trusted all selected repository signing keys.
    pacstrap_command = [
        "pacstrap",
        "-C",
        pacman_config,
        root_mount_point,
    ] + base_packages
    try:
        run_in_host(pacstrap_command, line_cb)
    except PacmanError as pe:
        details = f"{pe}\nLast pacstrap output:\n" + "\n".join(recent_output)
        return _failure(
            "Failed to run pacstrap",
            details,
            "base-system-install",
            error=pe,
        )
    except Exception as e:
        return _failure(
            "Failed to run pacstrap",
            f"pacstrap failed: {e!s}",
            "base-system-install",
            error=e,
        )

    try:
        install_repository_config(root_mount_point, repository_selection)
    except Exception as error:
        return _failure(
            "Failed to install repository configuration",
            f"Could not copy pacman configuration into target: {error!s}",
            "repository-configuration",
            error=error,
        )

    # --- copy files post install ---
    copy_groups = {
        "postInstallFiles": libcalamares.job.configuration.get("postInstallFiles", []),
        "requiredPostInstallFiles": libcalamares.job.configuration.get(
            "requiredPostInstallFiles", []
        ),
        "requiredPostInstallExecutables": libcalamares.job.configuration.get(
            "requiredPostInstallExecutables", []
        ),
    }
    for key, value in copy_groups.items():
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            return "Bad configuration", f"{key} must be a list of file paths"

    required_files = set(copy_groups["requiredPostInstallFiles"])
    required_executables = set(copy_groups["requiredPostInstallExecutables"])
    files_to_copy = list(
        dict.fromkeys(
            copy_groups["postInstallFiles"]
            + copy_groups["requiredPostInstallFiles"]
            + copy_groups["requiredPostInstallExecutables"]
        )
    )

    for source_file in files_to_copy:
        if (
            repository_selection == CACHYOS_SELECTION
            and source_file == "/etc/pacman.conf"
        ):
            continue

        is_required = source_file in required_files or source_file in required_executables
        if not os.path.isfile(source_file):
            message = f"Installer file is missing: {source_file}"
            if is_required:
                return "Required installer file missing", message
            libcalamares.utils.warning(message)
            continue

        if source_file in required_executables and not os.access(source_file, os.X_OK):
            return (
                "Required installer helper is not executable",
                f"Installer helper is not executable: {source_file}",
            )

        try:
            libcalamares.utils.debug("Copying file {!s}".format(source_file))
            dest = os.path.normpath(root_mount_point + source_file)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(source_file, dest)
        except Exception as error:
            message = f"Failed to copy installer file {source_file}: {error!s}"
            if is_required:
                return "Failed to copy required installer file", message
            libcalamares.utils.warning(message)
            continue

        if source_file in required_executables and not os.access(dest, os.X_OK):
            return (
                "Required installer helper lost executable permissions",
                f"Copied installer helper is not executable: {dest}",
            )

    libcalamares.globalstorage.insert("online", True)
    libcalamares.job.setprogress(1.0)
    return None
