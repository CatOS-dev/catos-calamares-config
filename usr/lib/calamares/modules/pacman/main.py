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
import time
from pathlib import Path

import libcalamares
from libcalamares.utils import check_target_env_call
from libcalamares.utils import gettext_path, gettext_languages

MODULE_DIR = Path(__file__).resolve().parent
MODULES_DIR = MODULE_DIR.parent
for path in (MODULE_DIR, MODULES_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
import pkgcheck
from package_progress import (
    PacmanTransactionTracker,
    RepositoryDatabaseSampler,
    TransferSampler,
    build_pacman_plan_command,
    format_repository_refresh_status,
    format_transfer_status,
    map_progress,
    parse_download_plan,
)


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
    return summary, details


def _target_cache_directories():
    root_mount_point = Path(str(libcalamares.globalstorage.value("rootMountPoint") or "/"))
    configured = []
    try:
        libcalamares.utils.target_env_process_output(
            ["pacman-conf", "CacheDir"],
            configured,
        )
    except Exception as error:
        libcalamares.utils.warning(f"Could not query target pacman cache directories: {error!s}")
    paths = []
    for raw_path in configured:
        target_path = str(raw_path).strip()
        if target_path:
            paths.append(root_mount_point / target_path.lstrip("/"))
    if not paths:
        paths.append(root_mount_point / "var/cache/pacman/pkg")
    return tuple(dict.fromkeys(paths))


def _target_download_plan(command):
    plan_command = build_pacman_plan_command(command)
    if plan_command is None:
        return None
    output = []
    libcalamares.utils.target_env_process_output(
        ["env", "LC_ALL=C", "LANG=C", *plan_command],
        output,
    )
    return parse_download_plan(output)


def _target_repository_refresh_sampler():
    repositories = []
    libcalamares.utils.target_env_process_output(["pacman-conf", "--repo-list"], repositories)
    dbpaths = []
    libcalamares.utils.target_env_process_output(["pacman-conf", "DBPath"], dbpaths)
    root_mount_point = Path(str(libcalamares.globalstorage.value("rootMountPoint") or "/"))
    dbpath = str(dbpaths[-1]).strip() if dbpaths else "/var/lib/pacman"
    sync_directory = root_mount_point / dbpath.lstrip("/") / "sync"
    return RepositoryDatabaseSampler(sync_directory, [str(item).strip() for item in repositories])


def _is_repository_refresh(command):
    short_options = "".join(
        argument[1:]
        for argument in command[1:]
        if argument.startswith("-") and not argument.startswith("--")
    )
    long_options = {argument for argument in command[1:] if argument.startswith("--")}
    return ("S" in short_options or "--sync" in long_options) and (
        "y" in short_options or "--refresh" in long_options
    )


def _operations_require_keyring(operations):
    install_keys = {"install", "try_install", "localInstall"}
    return any(bool(entry.get(key)) for entry in operations for key in install_keys)


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

        self.progress_fraction = 0.0
        self.package_phase_start = 0.0
        self.operation_start = 0.0
        self.operation_end = 1.0
        self.download_end = 0.70
        self.transaction_end = 0.96
        self.last_output_log_time = 0.0
        self.last_transfer_log_time = 0.0
        self.transaction_started = False
        self.transaction_tracker = PacmanTransactionTracker()
        self.current_output = []
        self.last_activity_emit_time = 0.0

        def line_cb(line: str):
            global custom_status_message
            global recent_output

            text = line.strip()
            if not text:
                return
            self.current_output.append(text)
            self.current_output[:] = self.current_output[-200:]
            recent_output[:] = self.current_output
            custom_status_message = "pacman: " + text

            transaction_ratio = self.transaction_tracker.observe(text)
            if transaction_ratio is not None:
                self.transaction_started = True
                candidate = map_progress(
                    self.download_end,
                    self.transaction_end,
                    transaction_ratio,
                )
                self.progress_fraction = max(self.progress_fraction, candidate)

            now = time.monotonic()
            if transaction_ratio is not None or now - self.last_activity_emit_time >= 0.25:
                libcalamares.job.setprogress(self.progress_fraction)
                self.last_activity_emit_time = now
            important = text.startswith(("error:", "warning:", "::"))
            if important or now - self.last_output_log_time >= 0.5:
                libcalamares.utils.debug("pacman: " + text)
                self.last_output_log_time = now

        self.line_cb = line_cb

    def _set_progress_range(self, start, end):
        self.operation_start = max(self.package_phase_start, float(start))
        self.operation_end = max(self.operation_start, float(end))
        self.progress_fraction = max(self.progress_fraction, self.operation_start)
        self.transaction_started = False
        self.transaction_tracker.reset()
        self.download_end = map_progress(self.operation_start, self.operation_end, 0.72)
        self.transaction_end = map_progress(self.operation_start, self.operation_end, 0.96)
        libcalamares.job.setprogress(self.progress_fraction)

    def reset_progress(self, *, preflight=False, progress_index=0, progress_count=1):
        if total_packages > 0 and group_packages > 0:
            group_start = map_progress(
                self.package_phase_start,
                1.0,
                completed_packages * 1.0 / total_packages,
            )
            group_end = map_progress(
                self.package_phase_start,
                1.0,
                min(1.0, (completed_packages + group_packages) * 1.0 / total_packages),
            )
            count = max(1, int(progress_count))
            index = min(count - 1, max(0, int(progress_index)))
            start = map_progress(group_start, group_end, index / count)
            end = map_progress(group_start, group_end, (index + 1) / count)
        elif preflight:
            start = self.package_phase_start
            end = max(start, 0.20)
        else:
            start = self.package_phase_start
            end = 1.0
        self._set_progress_range(start, end)

    def _report_transfer(self, snapshot):
        global custom_status_message
        if self.transaction_started:
            return
        custom_status_message = format_transfer_status(snapshot, _("Downloading packages"))
        candidate = map_progress(
            self.operation_start,
            self.download_end,
            snapshot.ratio,
        )
        self.progress_fraction = max(self.progress_fraction, candidate)
        libcalamares.job.setprogress(self.progress_fraction)
        now = time.monotonic()
        if now - self.last_transfer_log_time >= 2.0:
            libcalamares.utils.debug("pacman: " + custom_status_message)
            self.last_transfer_log_time = now

    def _report_repository_refresh(self, snapshot):
        global custom_status_message
        custom_status_message = format_repository_refresh_status(
            snapshot,
            _("Refreshing repository databases"),
        )
        candidate = map_progress(self.operation_start, self.operation_end, snapshot.ratio)
        self.progress_fraction = max(self.progress_fraction, candidate)
        libcalamares.job.setprogress(self.progress_fraction)
        now = time.monotonic()
        if now - self.last_transfer_log_time >= 2.0:
            libcalamares.utils.debug("pacman: " + custom_status_message)
            self.last_transfer_log_time = now

    def run_pacman(self, command, callback=False):
        """Run pacman with best-effort real download telemetry."""
        pacman_count = 0
        while pacman_count <= self.pacman_num_retries:
            pacman_count += 1
            self.transaction_started = False
            self.transaction_tracker.reset()
            self.current_output = []
            recent_output.clear()
            heartbeat_callback = None
            try:
                if callback:
                    try:
                        if _is_repository_refresh(command):
                            refresh_sampler = _target_repository_refresh_sampler()

                            def heartbeat_callback():
                                try:
                                    self._report_repository_refresh(refresh_sampler.sample())
                                except Exception as error:
                                    libcalamares.utils.warning(f"pacman repository telemetry failed: {error!s}")

                            heartbeat_callback()
                        else:
                            plan = _target_download_plan(command)
                            if plan is not None and plan.total_bytes > 0:
                                cache_directories = _target_cache_directories()
                                sampler = TransferSampler(plan, cache_directories)

                                def heartbeat_callback():
                                    try:
                                        self._report_transfer(sampler.sample())
                                    except Exception as error:
                                        libcalamares.utils.warning(f"pacman progress telemetry failed: {error!s}")

                                heartbeat_callback()
                                libcalamares.utils.debug(
                                    "pacman: planned {} package downloads".format(len(plan.downloads))
                                )
                            elif plan is not None:
                                global custom_status_message
                                custom_status_message = _("All required packages are cached; applying package changes")
                                self.progress_fraction = max(self.progress_fraction, self.download_end)
                                libcalamares.job.setprogress(self.progress_fraction)
                    except Exception as error:
                        libcalamares.utils.warning(f"pacman package telemetry unavailable: {error!s}")

                localized_command = ["env", "LC_ALL=C", "LANG=C", *command]
                if callback:
                    libcalamares.utils.target_env_process_output(
                        localized_command,
                        self.line_cb,
                        "",
                        0,
                        True,
                        heartbeat_callback,
                    )
                else:
                    libcalamares.utils.target_env_process_output(localized_command)
                if heartbeat_callback is not None:
                    heartbeat_callback()
            except subprocess.CalledProcessError:
                if pacman_count <= self.pacman_num_retries:
                    continue
                raise

            self.progress_fraction = max(self.progress_fraction, self.transaction_end)
            if callback:
                libcalamares.job.setprogress(self.progress_fraction)
            return

    def report_package_completion(self):
        if total_packages <= 0:
            return
        candidate = map_progress(
            self.package_phase_start,
            1.0,
            completed_packages * 1.0 / total_packages,
        )
        self.progress_fraction = max(self.progress_fraction, candidate)
        libcalamares.job.setprogress(self.progress_fraction)

    def update_db(self):
        command = ["pacman", "-Sy"]
        if self.pacman_disable_timeout:
            command.append("--disable-download-timeout")
        phase_end = max(self.package_phase_start, 0.05)
        self._set_progress_range(self.package_phase_start, phase_end)
        self.run_pacman(command, callback=True)
        self.progress_fraction = max(self.progress_fraction, phase_end)
        libcalamares.job.setprogress(self.progress_fraction)
        self.package_phase_start = max(self.package_phase_start, phase_end)

    def update_system(self):
        command = ["pacman", "-Su", "--noconfirm"]
        if self.pacman_disable_timeout:
            command.append("--disable-download-timeout")
        self.reset_progress(preflight=True)
        self.run_pacman(command, callback=True)
        self.package_phase_start = max(self.package_phase_start, self.operation_end)

    def full_upgrade(self):
        # Refresh first so the following read-only plan uses the exact databases
        # that the real upgrade transaction will consume.
        self.update_db()
        self.update_system()

    def install(self, pkgs, from_local=False, *, progress_index=0, progress_count=1):
        command = ["pacman", "-U" if from_local else "-S", "--noconfirm"]
        if self.pacman_needed_only:
            command.append("--needed")
        if self.pacman_disable_timeout:
            command.append("--disable-download-timeout")
        command += pkgs

        self.reset_progress(progress_index=progress_index, progress_count=progress_count)
        self.run_pacman(command, callback=True)

    def remove(self, pkgs, *, progress_index=0, progress_count=1):
        self.reset_progress(progress_index=progress_index, progress_count=progress_count)
        self.run_pacman(["pacman", "-Rs", "--noconfirm"] + pkgs, callback=True)

    # --- operations, keeping upstream semantics ---
    def install_package(
        self,
        packagedata,
        from_local=False,
        *,
        progress_index=0,
        progress_count=1,
    ):
        if isinstance(packagedata, str):
            self.install(
                [packagedata],
                from_local=from_local,
                progress_index=progress_index,
                progress_count=progress_count,
            )
        else:
            _run_script(packagedata.get("pre-script", ""))
            self.install(
                [packagedata["package"]],
                from_local=from_local,
                progress_index=progress_index,
                progress_count=progress_count,
            )
            _run_script(packagedata.get("post-script", ""))

    def remove_package(self, packagedata, *, progress_index=0, progress_count=1):
        if isinstance(packagedata, str):
            self.remove(
                [packagedata],
                progress_index=progress_index,
                progress_count=progress_count,
            )
        else:
            _run_script(packagedata.get("pre-script", ""))
            self.remove(
                [packagedata["package"]],
                progress_index=progress_index,
                progress_count=progress_count,
            )
            _run_script(packagedata.get("post-script", ""))

    def operation_install(self, package_list, from_local=False):
        if all(isinstance(x, str) for x in package_list):
            self.install(package_list, from_local=from_local)
        else:
            count = len(package_list)
            for index, package in enumerate(package_list):
                self.install_package(
                    package,
                    from_local=from_local,
                    progress_index=index,
                    progress_count=count,
                )

    def operation_try_install(self, package_list):
        count = len(package_list)
        for index, package in enumerate(package_list):
            try:
                self.install_package(package, progress_index=index, progress_count=count)
            except subprocess.CalledProcessError:
                libcalamares.utils.warning(f"Could not install package {package}")

    def operation_remove(self, package_list):
        if all(isinstance(x, str) for x in package_list):
            self.remove(package_list)
        else:
            count = len(package_list)
            for index, package in enumerate(package_list):
                self.remove_package(package, progress_index=index, progress_count=count)

    def operation_try_remove(self, package_list):
        count = len(package_list)
        for index, package in enumerate(package_list):
            try:
                self.remove_package(package, progress_index=index, progress_count=count)
            except subprocess.CalledProcessError:
                libcalamares.utils.warning(f"Could not remove package {package}")


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
        pkgman.report_package_completion()

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

    has_internet = bool(libcalamares.globalstorage.value("hasInternet"))
    update_db = libcalamares.job.configuration.get("update_db", False)
    update_system = libcalamares.job.configuration.get("update_system", False)

    operations = list(libcalamares.job.configuration.get("operations", []))
    if libcalamares.globalstorage.contains("packageOperations"):
        operations += list(libcalamares.globalstorage.value("packageOperations"))
    operations = [
        entry
        for entry in operations
        if "paru" not in str(entry.get("source", "")).lower()
        and "flatpak" not in str(entry.get("source", "")).lower()
    ]

    needs_keyring = (has_internet and (update_db or update_system)) or _operations_require_keyring(operations)
    if needs_keyring:
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

    if update_db and has_internet:
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

    if update_system and has_internet:
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
        # Everything got filtered out (or empty ops); any preflight work is complete.
        custom_status_message = None
        libcalamares.job.setprogress(1.0)
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
