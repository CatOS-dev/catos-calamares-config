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


class PacmanError(Exception):
    """Raised when host-side pacman/pacstrap returns non-zero."""

    def __init__(self, message):
        self.message = message

    def __str__(self):
        return str(self.message)


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

    custom_status_message = line.strip()
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
        raise PacmanError(f"Failed to run: {' '.join(command)} (rc={proc.returncode})")


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
        raise PacmanError(f"Failed to query: {' '.join(command)} (rc={proc.returncode})")
    return [l for l in lines if l]


def _has_internet():
    # Calamares commonly uses hasInternet; your module also sets "online" at the end.
    return bool(libcalamares.globalstorage.value("hasInternet")) or bool(
        libcalamares.globalstorage.value("online")
    )


def _maybe_sync_db_host():
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
    run_in_host(["pacman", "-Sy", "--noconfirm"], line_cb)


def _build_repo_index_host():
    """
    Build (packages_set, groups_set) on host (live environment).
    """
    pkgs = set(_host_capture_lines(["pacman", "-Slq"]))
    groups = set(_host_capture_lines(["pacman", "-Sgq"]))
    libcalamares.utils.debug(f"[host] pacman repo index: {len(pkgs)} packages, {len(groups)} groups")
    return pkgs, groups


def run():
    """
    Installs the base system packages (pacstrap) and copies files post-installation.
    Also filters basePackages using pkgcheck: drops missing packages/groups with warnings.
    Optionally runs pacman -Sy before filtering (sync_db: true by default).
    """
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
    base_packages = list(configured_packages)

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
    try:
        registry = load_bootloader_registry()
        boot_packages = package_plan(
                registry,
                str(bootloader),
                snapshots_enabled=snapshots_enabled,
                root_filesystem=root_filesystem,
            )
        base_packages.extend(boot_packages)
    except RegistryError as error:
        return "Invalid boot configuration", str(error)

    base_packages = list(dict.fromkeys(base_packages))
    libcalamares.utils.debug(
        f"Boot package plan: provider={bootloader}, snapshots={snapshots_enabled}, "
        f"rootfs={root_filesystem}, packages={base_packages}"
    )

    # --- NEW: optional sync + pkgcheck filtering (host-side) ---
    try:
        _maybe_sync_db_host()
    except PacmanError as e:
        # Don't abort just because sync failed; continue with existing DB.
        libcalamares.utils.warning(f"pacman -Sy failed; continuing with existing sync DB: {e}")

    try:
        repo_pkgs, repo_groups = _build_repo_index_host()
        missing_boot_packages = missing_required_packages(boot_packages, repo_pkgs, repo_groups)
        if missing_boot_packages:
            return (
                "Required boot packages are unavailable",
                "Missing required boot packages: " + ", ".join(missing_boot_packages),
            )
        base_packages = pkgcheck.filter_operation_list(
            "basePackages",
            base_packages,
            repo_pkgs,
            repo_groups,
        )
    except PacmanError as e:
        libcalamares.utils.warning(str(e))
        return "Package Manager error", "Could not query repository metadata for base system install"
    except Exception as e:
        libcalamares.utils.warning(f"pkgcheck failed: {e!s}")
        return "Package Manager error", "pkgcheck failed while preparing base system package list"

    if not base_packages:
        libcalamares.utils.warning("All basePackages were filtered out (missing). Skipping pacstrap.")
        # Keep behavior: mark "online" and finish.
        libcalamares.globalstorage.insert("online", True)
        libcalamares.job.setprogress(1.0)
        return None

    # Keep pacstrap's default keyring-copy behavior. shellprocess@init has
    # already fetched and locally trusted the CatOS and Arch4Edu signing keys.
    pacstrap_command = ["pacstrap", root_mount_point] + base_packages
    try:
        run_in_host(pacstrap_command, line_cb)
    except PacmanError as pe:
        return "Failed to run pacstrap", format(pe)
    except Exception as e:
        return "Failed to run pacstrap", f"pacstrap failed: {e!s}"

    # --- copy files post install ---
    if "postInstallFiles" in libcalamares.job.configuration:
        files_to_copy = libcalamares.job.configuration["postInstallFiles"]
        for source_file in files_to_copy:
            if os.path.exists(source_file):
                try:
                    libcalamares.utils.debug("Copying file {!s}".format(source_file))
                    dest = os.path.normpath(root_mount_point + source_file)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.copy2(source_file, dest)
                except Exception as e:
                    libcalamares.utils.warning(
                        "Failed to copy file {!s}, error {!s}".format(source_file, e)
                    )

    libcalamares.globalstorage.insert("online", True)
    libcalamares.job.setprogress(1.0)
    return None