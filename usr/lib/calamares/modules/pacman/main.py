#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# === This file is part of Calamares - <https://calamares.io> ===
#
#   SPDX-License-Identifier: GPL-3.0-or-later
#
# Calamares - Modular Installer Framework
# pacman module, by Aromatic symwww@outlook.com

from string import Template
import subprocess
import gettext
import sys
import os

import libcalamares
from libcalamares.utils import check_target_env_call
from libcalamares.utils import gettext_path, gettext_languages

sys.path.insert(0, "/usr/lib/calamares/modules/pacman")
import pkgcheck


_translation = gettext.translation(
    "calamares-python",
    localedir=gettext_path(),
    languages=gettext_languages(),
    fallback=True,
)
_ = _translation.gettext
_n = _translation.ngettext


# --- Progress state (module-global, as in upstream) ---
total_packages = 0
completed_packages = 0
group_packages = 0

custom_status_message = None
recent_output = []

INSTALL = object()
REMOVE = object()
mode_packages = None


def _change_mode(mode):
    global mode_packages
    mode_packages = mode
    # Avoid divide-by-zero elsewhere; total_packages can be 0 early.
    if total_packages > 0:
        libcalamares.job.setprogress(completed_packages * 1.0 / total_packages)


def pretty_name():
    return _("Install packages.")


def pretty_status_message():
    if custom_status_message is not None:
        return custom_status_message

    if not group_packages:
        if total_packages > 0:
            s = _("Processing packages (%(count)d / %(total)d)")
        else:
            s = _("Install packages.")
    elif mode_packages is INSTALL:
        s = _n("Installing one package.", "Installing %(num)d packages.", group_packages)
    elif mode_packages is REMOVE:
        s = _n("Removing one package.", "Removing %(num)d packages.", group_packages)
    else:
        s = _("Install packages.")

    return s % {
        "num": group_packages,
        "count": completed_packages,
        "total": total_packages,
    }


def _normalize_locale(locale):
    locale = str(locale or "en").split(".", 1)[0].split("@", 1)[0]
    return locale.replace("_", "-").lower()


# --- Helpers ---
def subst_locale(plist):
    """
    Locale-aware list of packages.
    Substitutes ${LOCALE} with the selected BCP47 locale; drops LOCALE-packages if locale is 'en'.
    """
    locale = _normalize_locale(libcalamares.globalstorage.value("locale"))

    result = []
    for packagedata in plist:
        if isinstance(packagedata, str):
            packagename = packagedata
            output = packagedata
        else:
            packagename = packagedata.get("package")
            output = dict(packagedata)

        if packagename is None:
            continue

        if locale == "en" and "LOCALE" in packagename:
            continue

        packagename = Template(packagename).safe_substitute(LOCALE=locale)
        if isinstance(output, str):
            result.append(packagename)
        else:
            output["package"] = packagename
            result.append(output)

    return result


def _run_script(script):
    if script:
        # keep behavior: split by spaces, same as upstream
        check_target_env_call(script.split(" "))


def _keyring_populate_command(keyrings, keyring_dir="/usr/share/pacman/keyrings"):
    return [
        "/bin/sh",
        "-c",
        """
set -eu
keyring_dir="$1"
shift
if [ ! -f "${keyring_dir}/archlinux.gpg" ]; then
    printf 'required keyring is missing: %s/archlinux.gpg\n' "$keyring_dir" >&2
    exit 1
fi
available=""
for keyring in "$@"; do
    if [ -f "${keyring_dir}/${keyring}.gpg" ]; then
        available="${available} ${keyring}"
    else
        printf 'optional keyring is unavailable: %s/%s.gpg\n' "$keyring_dir" "$keyring" >&2
    fi
done
[ -z "$available" ] || pacman-key --populate $available
""",
        "catos-keyring-refresh",
        keyring_dir,
        *keyrings,
    ]


def _refresh_target_keyring():
    """Reconcile only the keyrings approved by the selected repository profile."""
    keyrings = ["archlinux", "catos", "arch4edu", "archlinuxcn"]
    if libcalamares.globalstorage.value("packagechooser_repository") == "cachyos":
        keyrings.append("cachyos")

    check_target_env_call(["pacman-key", "--init"])
    check_target_env_call(_keyring_populate_command(keyrings))


def _command_text(command):
    if isinstance(command, (list, tuple)):
        return " ".join(str(part) for part in command)
    return str(command or "")


def _text_output(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _failure(summary, description, error, stage):
    command = getattr(error, "cmd", None)
    returncode = getattr(error, "returncode", None)
    outputs = list(recent_output)
    for attribute in ("stdout", "stderr", "output"):
        captured = _text_output(getattr(error, attribute, None)).strip()
        if captured and captured not in outputs:
            outputs.append(captured)
    output = "\n".join(outputs[-200:])
    details = "{}\nCommand: {}\nExit code: {}".format(
        description,
        _command_text(command) or "unknown",
        returncode if returncode is not None else "unknown",
    )
    if output:
        details += "\nLast pacman output:\n" + output
    context = {
        "source": "pacman",
        "stage": stage,
        "summary": str(summary),
        "details": str(description),
        "command": _command_text(command),
        "output": output,
    }
    if returncode is not None:
        context["exitCode"] = int(returncode)
    libcalamares.globalstorage.insert("recovery.failureContext", context)
    return summary, details


# --- Pacman backend only ---
class PacmanManager:
    backend = "pacman"

    def __init__(self):
        # Pacman-specific config (same keys as upstream module)
        pacman_cfg = libcalamares.job.configuration.get("pacman", None)
        if pacman_cfg is None:
            pacman_cfg = {}
        if type(pacman_cfg) is not dict:
            libcalamares.utils.warning("Job configuration *pacman* will be ignored.")
            pacman_cfg = {}

        self.pacman_num_retries = pacman_cfg.get("num_retries", 0)
        self.pacman_disable_timeout = pacman_cfg.get("disable_download_timeout", False)
        self.pacman_needed_only = pacman_cfg.get("needed_only", False)

        self.in_package_changes = False
        self.progress_fraction = 0.0

        def line_cb(line: str):
            # Minimal pacman output handling (no spy / experimental hooks)
            global custom_status_message
            global recent_output

            recent_output.append(line.rstrip())
            recent_output[:] = recent_output[-200:]
            if line.startswith(":: "):
                self.in_package_changes = ("package" in line) or ("hooks" in line)
            else:
                if self.in_package_changes and line.endswith("...\n"):
                    custom_status_message = "pacman: " + line.strip()
                    libcalamares.job.setprogress(self.progress_fraction)

            libcalamares.utils.debug(line.strip())

        self.line_cb = line_cb

    def reset_progress(self):
        self.in_package_changes = False
        if total_packages > 0:
            self.progress_fraction = completed_packages * 1.0 / total_packages
        else:
            self.progress_fraction = 0.0

    def run_pacman(self, command, callback=False):
        """
        Call pacman in a loop until it is successful or retries exhausted.
        """
        pacman_count = 0
        while pacman_count <= self.pacman_num_retries:
            pacman_count += 1
            try:
                if callback:
                    libcalamares.utils.target_env_process_output(command, self.line_cb)
                else:
                    libcalamares.utils.target_env_process_output(command)
                return
            except subprocess.CalledProcessError:
                if pacman_count <= self.pacman_num_retries:
                    continue
                raise

    def update_db(self):
        self.run_pacman(["pacman", "-Sy"])

    def update_system(self):
        command = ["pacman", "-Su", "--noconfirm"]
        if self.pacman_disable_timeout:
            command.append("--disable-download-timeout")
        self.run_pacman(command)

    def full_upgrade(self):
        command = ["pacman", "-Syu", "--noconfirm"]
        if self.pacman_disable_timeout:
            command.append("--disable-download-timeout")
        self.reset_progress()
        self.run_pacman(command, callback=True)

    def install(self, pkgs, from_local=False):
        command = ["pacman", "-U" if from_local else "-S", "--noconfirm", "--noprogressbar"]
        if self.pacman_needed_only:
            command.append("--needed")
        if self.pacman_disable_timeout:
            command.append("--disable-download-timeout")
        command += pkgs

        self.reset_progress()
        self.run_pacman(command, callback=True)

    def remove(self, pkgs):
        self.reset_progress()
        self.run_pacman(["pacman", "-Rs", "--noconfirm"] + pkgs, callback=True)

    # --- operations, keeping upstream semantics ---
    def install_package(self, packagedata, from_local=False):
        if isinstance(packagedata, str):
            self.install([packagedata], from_local=from_local)
        else:
            _run_script(packagedata.get("pre-script", ""))
            self.install([packagedata["package"]], from_local=from_local)
            _run_script(packagedata.get("post-script", ""))

    def remove_package(self, packagedata):
        if isinstance(packagedata, str):
            self.remove([packagedata])
        else:
            _run_script(packagedata.get("pre-script", ""))
            self.remove([packagedata["package"]])
            _run_script(packagedata.get("post-script", ""))

    def operation_install(self, package_list, from_local=False):
        if all(isinstance(x, str) for x in package_list):
            self.install(package_list, from_local=from_local)
        else:
            for p in package_list:
                self.install_package(p, from_local=from_local)

    def operation_try_install(self, package_list):
        for p in package_list:
            try:
                self.install_package(p)
            except subprocess.CalledProcessError:
                libcalamares.utils.warning(f"Could not install package {p}")

    def operation_remove(self, package_list):
        if all(isinstance(x, str) for x in package_list):
            self.remove(package_list)
        else:
            for p in package_list:
                self.remove_package(p)

    def operation_try_remove(self, package_list):
        for p in package_list:
            try:
                self.remove_package(p)
            except subprocess.CalledProcessError:
                libcalamares.utils.warning(f"Could not remove package {p}")


def run_operations(pkgman: PacmanManager, entry: dict):
    global group_packages, completed_packages

    for key, value in entry.items():
        if key == "source":
            libcalamares.utils.debug("Package-list from {!s}".format(value))
            continue

        package_list = subst_locale(value)
        group_packages = len(package_list)

        if key == "install":
            _change_mode(INSTALL)
            pkgman.operation_install(package_list)
        elif key == "try_install":
            _change_mode(INSTALL)
            pkgman.operation_try_install(package_list)
        elif key == "remove":
            _change_mode(REMOVE)
            pkgman.operation_remove(package_list)
        elif key == "try_remove":
            _change_mode(REMOVE)
            pkgman.operation_try_remove(package_list)
        elif key == "localInstall":
            _change_mode(INSTALL)
            pkgman.operation_install(package_list, from_local=True)
        else:
            libcalamares.utils.warning("Unknown package-operation key {!s}".format(key))

        completed_packages += len(package_list)
        if total_packages > 0:
            libcalamares.job.setprogress(completed_packages * 1.0 / total_packages)

    group_packages = 0
    _change_mode(None)


def run():
    global mode_packages, total_packages, completed_packages, group_packages, custom_status_message, recent_output

    recent_output.clear()

    # pacman-only: optional guard (if someone misconfigures)
    backend = libcalamares.job.configuration.get("backend", "pacman")
    if backend != "pacman":
        return "Bad backend", f'backend="{backend}" (pacman-only module)'

    skip_this = libcalamares.job.configuration.get("skip_if_no_internet", False)
    if skip_this and not libcalamares.globalstorage.value("hasInternet"):
        libcalamares.utils.warning("Package installation has been skipped: no internet")
        return None

    pkgman = PacmanManager()

    try:
        _refresh_target_keyring()
    except Exception as e:
        libcalamares.utils.warning(f"target keyring refresh failed: {e!s}")
        return _failure(
            _("Package signing key setup failed"),
            _("The target package keyring could not be initialized or populated."),
            e,
            "keyring",
        )

    recovery_refresh = bool(libcalamares.globalstorage.value("recovery.refreshRepositories"))
    has_internet = bool(libcalamares.globalstorage.value("hasInternet"))
    update_db = libcalamares.job.configuration.get("update_db", False)
    update_system = libcalamares.job.configuration.get("update_system", False)

    if recovery_refresh:
        if not has_internet:
            return _failure(
                _("Package Manager error"),
                _("Network is unavailable; the required full repository refresh was not attempted."),
                RuntimeError("Network is unreachable"),
                "repository-full-upgrade",
            )
        try:
            pkgman.full_upgrade()
        except subprocess.CalledProcessError as e:
            libcalamares.utils.warning(str(e))
            libcalamares.utils.debug("stdout:" + str(getattr(e, "stdout", "")))
            libcalamares.utils.debug("stderr:" + str(getattr(e, "stderr", "")))
            return _failure(
                _("Package Manager error"),
                _("The package manager could not fully refresh repositories and upgrade the target system."),
                e,
                "repository-full-upgrade",
            )
        libcalamares.globalstorage.remove("recovery.refreshRepositories")

    if not recovery_refresh and update_db and has_internet:
        try:
            pkgman.update_db()
        except subprocess.CalledProcessError as e:
            libcalamares.utils.warning(str(e))
            libcalamares.utils.debug("stdout:" + str(getattr(e, "stdout", "")))
            libcalamares.utils.debug("stderr:" + str(getattr(e, "stderr", "")))
            return _failure(
                _("Package Manager error"),
                _("The package manager could not make changes to the installed system."),
                e,
                "repository-database-sync",
            )

    if not recovery_refresh and update_system and has_internet:
        try:
            pkgman.update_system()
        except subprocess.CalledProcessError as e:
            libcalamares.utils.warning(str(e))
            libcalamares.utils.debug("stdout:" + str(getattr(e, "stdout", "")))
            libcalamares.utils.debug("stderr:" + str(getattr(e, "stderr", "")))
            return _failure(
                _("Package Manager error"),
                _("The package manager could not update the system."),
                e,
                "system-update",
            )

    operations = libcalamares.job.configuration.get("operations", [])
    if libcalamares.globalstorage.contains("packageOperations"):
        operations += libcalamares.globalstorage.value("packageOperations")

    # --- FILTER OUT unsupported sources (paru, flatpak) ---
    filtered_ops = []
    for entry in operations:
        src = str(entry.get("source", "")).lower()
        if "paru" in src or "flatpak" in src:
            continue
        filtered_ops.append(entry)
    operations = filtered_ops

    # --- preprocess package lists (drop missing pkgs/groups) ---
    try:
        repo_pkgs, repo_groups = pkgcheck.build_repo_index()
        operations, filtered_total = pkgcheck.preprocess_operations(
            operations=operations,
            subst_locale_fn=subst_locale,
            repo_pkgs=repo_pkgs,
            repo_groups=repo_groups,
        )
    except subprocess.CalledProcessError as e:
        libcalamares.utils.warning(str(e))
        libcalamares.utils.debug("stdout:" + str(getattr(e, "stdout", "")))
        libcalamares.utils.debug("stderr:" + str(getattr(e, "stderr", "")))
        return _failure(
            _("Repository metadata query failed"),
            _("The package manager could not query repository metadata."),
            e,
            "repository-metadata",
        )

    mode_packages = None
    total_packages = filtered_total
    completed_packages = 0
    group_packages = 0
    custom_status_message = None

    if not total_packages:
        # Everything got filtered out (or empty ops)
        return None

    for entry in operations:
        group_packages = 0
        libcalamares.utils.debug(pretty_name())
        try:
            run_operations(pkgman, entry)
        except subprocess.CalledProcessError as e:
            libcalamares.utils.warning(str(e))
            libcalamares.utils.debug("stdout:" + str(getattr(e, "stdout", "")))
            libcalamares.utils.debug("stderr:" + str(getattr(e, "stderr", "")))
            return _failure(
                _("Package Manager error"),
                _("The package manager could not make changes to the installed system."),
                e,
                "package-install",
            )

    mode_packages = None
    libcalamares.job.setprogress(1.0)
    return None
