#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Paru-only Calamares packages module (AUR best-effort).
# Package failures are non-fatal, but the temporary privilege setup is always
# removed before this module returns.

from string import Template
import gettext
import subprocess
import sys

import libcalamares
from libcalamares.utils import check_target_env_call
from libcalamares.utils import gettext_path, gettext_languages

sys.path.insert(0, "/usr/lib/calamares/modules/paru")
from process import ProcessTimeout, run_process_group  # noqa: E402


_translation = gettext.translation(
    "calamares-python",
    localedir=gettext_path(),
    languages=gettext_languages(),
    fallback=True,
)
_ = _translation.gettext
_n = _translation.ngettext


total_packages = 0
completed_packages = 0
group_packages = 0
custom_status_message = None

INSTALL = object()
REMOVE = object()
mode_packages = None
_OPERATION_KEYS = {"install", "try_install", "remove", "try_remove", "localInstall"}


def _change_mode(mode):
    global mode_packages
    mode_packages = mode
    if total_packages > 0:
        libcalamares.job.setprogress(completed_packages * 1.0 / total_packages)


def pretty_name():
    return _("Install packages.")


def pretty_status_message():
    if custom_status_message is not None:
        return custom_status_message

    if not group_packages:
        if total_packages > 0:
            message = _("Processing packages (%(count)d / %(total)d)")
        else:
            message = _("Install packages.")
    elif mode_packages is INSTALL:
        message = _n("Installing one package.", "Installing %(num)d packages.", group_packages)
    elif mode_packages is REMOVE:
        message = _n("Removing one package.", "Removing %(num)d packages.", group_packages)
    else:
        message = _("Install packages.")

    return message % {
        "num": group_packages,
        "count": completed_packages,
        "total": total_packages,
    }


def _normalize_locale(locale):
    locale = str(locale or "en").split(".", 1)[0].split("@", 1)[0]
    return locale.replace("_", "-").lower()


def subst_locale(plist):
    """Substitute package locale placeholders using Arch package-name syntax."""
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
        check_target_env_call(script.split(" "))


class ParuManager:
    backend = "paru"

    def __init__(self):
        paru_cfg = libcalamares.job.configuration.get("paru", {})
        if not isinstance(paru_cfg, dict):
            libcalamares.utils.warning("Job configuration *paru* will be ignored.")
            paru_cfg = {}

        self.paru_num_retries = paru_cfg.get("num_retries", 0)
        self.paru_disable_timeout = paru_cfg.get("disable_download_timeout", False)
        self.paru_needed_only = paru_cfg.get("needed_only", False)
        self.paru_timeout = paru_cfg.get("timeout", 0)
        self.root_mount_point = libcalamares.globalstorage.value("rootMountPoint")
        self.in_package_changes = False
        self.progress_fraction = 0.0
        self.prepared = self._prepare_target()

        def line_cb(line):
            global custom_status_message
            if line.startswith(":: "):
                self.in_package_changes = ("package" in line) or ("hooks" in line)
            elif self.in_package_changes and line.endswith("...\n"):
                custom_status_message = "paru: " + line.strip()
                libcalamares.job.setprogress(self.progress_fraction)
            libcalamares.utils.debug(line.strip())

        self.line_cb = line_cb

    def _prepare_target(self):
        if not self.root_mount_point:
            libcalamares.utils.warning("paru: target root mount point is missing; skipping AUR packages")
            return False

        command = (
            "printf '%s\\n' 'nobody ALL=(root) NOPASSWD: /usr/bin/pacman' "
            "> /etc/sudoers.d/calamares-paru && "
            "chmod 0440 /etc/sudoers.d/calamares-paru && "
            "mkdir -p /var/cache/paru_cache && "
            "chown nobody:nobody /var/cache/paru_cache && "
            "chage -E -1 nobody"
        )
        try:
            libcalamares.utils.target_env_process_output(["sh", "-c", command])
            return True
        except Exception as error:
            libcalamares.utils.warning(f"paru: failed to prepare temporary build account: {error!s}")
            return False

    def cleanup(self):
        command = (
            "rm -f /etc/sudoers.d/calamares-paru && "
            "rm -rf /var/cache/paru_cache && "
            "usermod -L nobody && "
            "chage -E 0 nobody"
        )
        try:
            libcalamares.utils.target_env_process_output(["sh", "-c", command])
            return True
        except Exception as error:
            libcalamares.utils.warning(f"paru: failed to remove temporary privileges: {error!s}")
            return False

    def reset_progress(self):
        self.in_package_changes = False
        self.progress_fraction = completed_packages * 1.0 / total_packages if total_packages else 0.0

    def _host_command(self, command):
        return [
            "arch-chroot",
            self.root_mount_point,
            "sudo",
            "-u",
            "nobody",
            "env",
            "HOME=/var/cache/paru_cache",
            "PWD=/var/cache/paru_cache",
            "XDG_CACHE_HOME=/var/cache/paru_cache",
            "XDG_DATA_HOME=/var/cache/paru_cache",
        ] + command

    def run_paru(self, command, callback=False):
        if not self.prepared:
            return False

        last_error = None
        host_command = self._host_command(command)
        for attempt in range(self.paru_num_retries + 1):
            try:
                run_process_group(
                    host_command,
                    timeout=self.paru_timeout,
                    line_func=self.line_cb if callback else lambda line: libcalamares.utils.debug(line.strip()),
                )
                return True
            except (ProcessTimeout, subprocess.CalledProcessError, OSError) as error:
                last_error = error
                if attempt < self.paru_num_retries:
                    continue

        libcalamares.utils.warning(
            "paru command failed (ignored): {!s} rc={!s}".format(
                getattr(last_error, "cmd", host_command),
                getattr(last_error, "returncode", 124 if isinstance(last_error, ProcessTimeout) else "?"),
            )
        )
        return False

    def install(self, pkgs, from_local=False):
        command = ["paru", "-U" if from_local else "-S", "--noconfirm", "--noprogressbar"]
        if self.paru_needed_only:
            command.append("--needed")
        if self.paru_disable_timeout:
            command.append("--disable-download-timeout")
        command += pkgs
        self.reset_progress()
        self.run_paru(command, callback=True)

    def remove(self, pkgs):
        self.reset_progress()
        self.run_paru(["paru", "-Rs", "--noconfirm"] + pkgs, callback=True)

    def update_db(self):
        self.run_paru(["paru", "-Sy"])

    def update_system(self):
        command = ["paru", "-Su", "--noconfirm"]
        if self.paru_disable_timeout:
            command.append("--disable-download-timeout")
        self.run_paru(command)

    def install_package(self, packagedata, from_local=False):
        try:
            if isinstance(packagedata, str):
                self.install([packagedata], from_local=from_local)
            else:
                _run_script(packagedata.get("pre-script", ""))
                self.install([packagedata["package"]], from_local=from_local)
                _run_script(packagedata.get("post-script", ""))
        except Exception as error:
            libcalamares.utils.warning(f"paru: install_package failed (ignored): {error!s}")

    def remove_package(self, packagedata):
        try:
            if isinstance(packagedata, str):
                self.remove([packagedata])
            else:
                _run_script(packagedata.get("pre-script", ""))
                self.remove([packagedata["package"]])
                _run_script(packagedata.get("post-script", ""))
        except Exception as error:
            libcalamares.utils.warning(f"paru: remove_package failed (ignored): {error!s}")

    def operation_install(self, package_list, from_local=False):
        for package in package_list:
            self.install_package(package, from_local=from_local)

    def operation_try_install(self, package_list):
        for package in package_list:
            self.install_package(package)

    def operation_remove(self, package_list):
        for package in package_list:
            self.remove_package(package)

    def operation_try_remove(self, package_list):
        for package in package_list:
            self.remove_package(package)


def run_operations(pkgman, entry):
    global group_packages, completed_packages

    for key, value in entry.items():
        if key == "source":
            libcalamares.utils.debug("Package-list from {!s}".format(value))
            continue
        if key not in _OPERATION_KEYS:
            libcalamares.utils.warning("Unknown package-operation key {!s}".format(key))
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

        completed_packages += len(package_list)
        if total_packages > 0:
            libcalamares.job.setprogress(completed_packages * 1.0 / total_packages)

    group_packages = 0
    _change_mode(None)


def run():
    global mode_packages, total_packages, completed_packages, group_packages, custom_status_message

    backend = libcalamares.job.configuration.get("backend", "paru")
    if backend != "paru":
        return "Bad backend", f'backend="{backend}" (paru-only module)'

    if libcalamares.job.configuration.get("skip_if_no_internet", False) and not libcalamares.globalstorage.value(
        "hasInternet"
    ):
        libcalamares.utils.warning("Paru package installation skipped: no internet")
        return None

    pkgman = ParuManager()
    cleanup_ok = False
    try:
        if libcalamares.job.configuration.get("update_db", False) and libcalamares.globalstorage.value("hasInternet"):
            pkgman.update_db()
        if libcalamares.job.configuration.get("update_system", False) and libcalamares.globalstorage.value(
            "hasInternet"
        ):
            pkgman.update_system()

        operations = list(libcalamares.job.configuration.get("operations", []))
        if libcalamares.globalstorage.contains("packageOperations"):
            operations += libcalamares.globalstorage.value("packageOperations")
        operations = [entry for entry in operations if "paru" in str(entry.get("source", "")).lower()]

        mode_packages = None
        completed_packages = 0
        group_packages = 0
        custom_status_message = None
        total_packages = sum(
            len(subst_locale(value))
            for entry in operations
            for key, value in entry.items()
            if key in _OPERATION_KEYS
        )

        for entry in operations:
            group_packages = 0
            libcalamares.utils.debug(pretty_name())
            try:
                run_operations(pkgman, entry)
            except Exception as error:
                libcalamares.utils.warning(f"paru: operation failed (ignored): {error!s}")
    finally:
        cleanup_ok = pkgman.cleanup()

    if not cleanup_ok:
        libcalamares.utils.warning(
            "paru: immediate cleanup failed; the mandatory final cleanup job will retry"
        )

    mode_packages = None
    libcalamares.job.setprogress(1.0)
    return None
