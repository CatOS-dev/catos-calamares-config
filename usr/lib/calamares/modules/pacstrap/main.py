#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import selectors
import sys
import subprocess
import shutil
import tempfile
import time
import gettext
from collections import deque
from pathlib import Path

import libcalamares
from libcalamares.utils import gettext_path, gettext_languages

# Ensure installer helper modules are importable both installed and from tests.
MODULE_DIR = Path(__file__).resolve().parent
MODULES_DIR = MODULE_DIR.parent
for path in (MODULE_DIR, MODULES_DIR / "bootloadu", MODULES_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
import pkgcheck  # noqa: E402
from pacstrap_repository import (  # noqa: E402
    CACHYOS_SELECTION,
    install_repository_config,
    pacman_config_for,
    transform_packages,
)
from secureboot import secure_boot_enabled as host_secure_boot_enabled  # noqa: E402
from recovery_context import build_failure_context  # noqa: E402
from package_progress import (  # noqa: E402
    PACMAN_PRINT_FORMAT,
    PacmanTransactionTracker,
    RepositoryDatabaseSampler,
    TerminalFrameDecoder,
    TransferSampler,
    format_repository_refresh_status,
    format_transfer_status,
    map_progress,
    parse_download_plan,
)
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


def _failure(summary, details, stage, error=None, output=None, category=None, reason_code=None):
    captured = output
    if captured is None and error is not None:
        captured = getattr(error, "output", None)
    if captured is None:
        captured = "\n".join(recent_output)
    context = build_failure_context(
        source="pacstrap",
        stage=stage,
        summary=str(summary),
        details=str(details),
        command=getattr(error, "cmd", None),
        exit_code=getattr(error, "returncode", None),
        output=str(captured or ""),
        category=category,
        reason_code=reason_code,
    )
    libcalamares.globalstorage.insert("recovery.failureContext", context)
    return summary, details


def pretty_name():
    return _("Install base system")


def pretty_status_message():
    if custom_status_message is not None:
        return custom_status_message
    return None


def line_cb(line: str):
    """Record a complete pacstrap terminal frame without fabricating progress."""
    global custom_status_message
    global status_update_time
    global recent_output

    text = line.strip()
    if not text:
        return
    custom_status_message = text
    recent_output.append(text)
    recent_output = recent_output[-200:]

    # Native package progress can produce many carriage-return frames. Keep the
    # visible log live without flooding the persistent diagnostics log.
    now = time.monotonic()
    important = text.startswith(("error:", "warning:", "==>", "::"))
    if important or now - status_update_time >= 0.5:
        libcalamares.utils.debug("pacstrap: " + text)
        status_update_time = now


def run_in_host(command, line_func, heartbeat_func=None, heartbeat_interval=0.5):
    heartbeat_interval = max(0.05, float(heartbeat_interval))
    environment = os.environ.copy()
    environment.update({"LC_ALL": "C", "LANG": "C"})
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        env=environment,
    )
    if proc.stdout is None:
        proc.kill()
        raise PacmanError(f"Failed to capture output: {' '.join(command)}", command=command)

    decoder = TerminalFrameDecoder()
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    output_frames = deque(maxlen=200)
    reached_eof = False
    last_heartbeat = time.monotonic()
    try:
        while not reached_eof:
            select_timeout = min(0.25, heartbeat_interval) if heartbeat_func is not None else 0.25
            events = selector.select(timeout=select_timeout)
            for key, _mask in events:
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    reached_eof = True
                    break
                for frame in decoder.feed(chunk):
                    if frame.strip():
                        output_frames.append(frame.strip())
                        line_func(frame)
            if proc.poll() is not None and not events:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if chunk:
                    for frame in decoder.feed(chunk):
                        if frame.strip():
                            output_frames.append(frame.strip())
                            line_func(frame)
                else:
                    reached_eof = True
            now = time.monotonic()
            if heartbeat_func is not None and now - last_heartbeat >= heartbeat_interval:
                try:
                    heartbeat_func()
                except Exception as error:
                    libcalamares.utils.warning(f"pacstrap progress telemetry failed: {error!s}")
                last_heartbeat = now
        for frame in decoder.finish():
            if frame.strip():
                output_frames.append(frame.strip())
                line_func(frame)
    finally:
        selector.close()
        proc.stdout.close()

    proc.wait()
    if heartbeat_func is not None:
        try:
            heartbeat_func()
        except Exception as error:
            libcalamares.utils.warning(f"pacstrap progress telemetry failed: {error!s}")
    if proc.returncode != 0:
        raise PacmanError(
            f"Failed to run: {' '.join(command)} (rc={proc.returncode})",
            command=command,
            returncode=proc.returncode,
            output="\n".join(output_frames),
        )


def _host_capture_lines(command):
    """
    Run command on host and capture stdout lines. Raises PacmanError on failure.
    """
    environment = os.environ.copy()
    environment.update({"LC_ALL": "C", "LANG": "C"})
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
        env=environment,
    )
    if proc.stdout is None:
        proc.kill()
        raise PacmanError(f"Failed to capture output: {' '.join(command)}", command=command)
    lines = []
    try:
        for line in proc.stdout:
            if line:
                lines.append(line.rstrip("\n"))
    finally:
        proc.stdout.close()
    proc.wait()
    if proc.returncode != 0:
        raise PacmanError(
            f"Failed to query: {' '.join(command)} (rc={proc.returncode})",
            command=command,
            returncode=proc.returncode,
            output="\n".join(lines),
        )
    return [l for l in lines if l]


def _download_plan(pacman_config, root_mount_point, packages):
    cache_directory = Path(root_mount_point) / "var/cache/pacman/pkg"
    cache_directory.mkdir(parents=True, exist_ok=True)
    configured_dbpath = _host_capture_lines(["pacman-conf", "-c", pacman_config, "DBPath"])
    host_dbpath = Path(configured_dbpath[-1] if configured_dbpath else "/var/lib/pacman")
    host_sync = host_dbpath / "sync"
    if not host_sync.is_dir():
        raise PacmanError(f"Pacman sync database directory is unavailable: {host_sync}")

    with tempfile.TemporaryDirectory(prefix="calamares-pacstrap-plan-") as temporary:
        plan_dbpath = Path(temporary)
        (plan_dbpath / "local").mkdir()
        (plan_dbpath / "sync").symlink_to(host_sync, target_is_directory=True)
        command = [
            "pacman",
            "--config",
            pacman_config,
            "--root",
            root_mount_point,
            "--dbpath",
            str(plan_dbpath),
            "-Sp",
            "--print-format",
            PACMAN_PRINT_FORMAT,
            "--cachedir",
            str(cache_directory),
            "--noconfirm",
            *packages,
        ]
        plan = parse_download_plan(_host_capture_lines(command))
    return plan, cache_directory


def _transfer_reporter(progress_start, progress_end, phase_state):
    last_log_time = 0.0
    last_progress = progress_start

    def report(snapshot):
        nonlocal last_log_time, last_progress
        global custom_status_message
        if phase_state["transaction_started"]:
            return
        custom_status_message = format_transfer_status(snapshot, _("Downloading packages"))
        last_progress = max(last_progress, map_progress(progress_start, progress_end, snapshot.ratio))
        phase_state["progress"] = max(phase_state["progress"], last_progress)
        libcalamares.job.setprogress(phase_state["progress"])
        now = time.monotonic()
        if now - last_log_time >= 2.0:
            libcalamares.utils.debug("pacstrap: " + custom_status_message)
            last_log_time = now

    return report


def _has_internet():
    # Calamares commonly uses hasInternet; your module also sets "online" at the end.
    return bool(libcalamares.globalstorage.value("hasInternet")) or bool(
        libcalamares.globalstorage.value("online")
    )


def _repository_refresh_sampler(pacman_config):
    repositories = _host_capture_lines(["pacman-conf", "-c", pacman_config, "--repo-list"])
    configured_dbpath = _host_capture_lines(["pacman-conf", "-c", pacman_config, "DBPath"])
    dbpath = Path(configured_dbpath[-1] if configured_dbpath else "/var/lib/pacman")
    return RepositoryDatabaseSampler(dbpath / "sync", repositories)


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

    global custom_status_message
    custom_status_message = _("Refreshing repository databases")
    libcalamares.job.setprogress(0.02)
    libcalamares.utils.debug("Syncing pacman database before pkgcheck (pacman -Sy)...")
    heartbeat = None
    try:
        sampler = _repository_refresh_sampler(pacman_config)

        def heartbeat():
            global custom_status_message
            snapshot = sampler.sample()
            custom_status_message = format_repository_refresh_status(
                snapshot,
                _("Refreshing repository databases"),
            )
            libcalamares.job.setprogress(map_progress(0.02, 0.04, snapshot.ratio))

        heartbeat()
    except Exception as error:
        libcalamares.utils.warning(f"pacstrap repository telemetry unavailable: {error!s}")

    # Using run_in_host so output goes through line_cb for UI/log.
    run_in_host(
        ["pacman", "--config", pacman_config, "-Sy", "--noconfirm"],
        line_cb,
        heartbeat,
    )
    libcalamares.job.setprogress(0.04)


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
    libcalamares.job.setprogress(0.01)

    root_mount_point = libcalamares.globalstorage.value("rootMountPoint")

    if not root_mount_point:
        return _failure(
            _("No mount point for root partition in globalstorage"),
            _('globalstorage does not contain a "rootMountPoint" key, doing nothing'),
            "target-root",
            category="unknown",
            reason_code="pacstrap.target-root.missing",
        )

    if not os.path.exists(root_mount_point):
        return _failure(
            _("Bad mount point for root partition in globalstorage"),
            _('globalstorage["rootMountPoint"] is "{root}", which does not exist, doing nothing').format(
                root=root_mount_point
            ),
            "target-root",
            category="unknown",
            reason_code="pacstrap.target-root.unavailable",
        )

    if not libcalamares.job.configuration:
        return _failure(
            _("No configuration found"),
            _("Aborting due to missing configuration"),
            "module-configuration",
            category="unknown",
            reason_code="pacstrap.module-configuration.missing",
        )

    if "basePackages" not in libcalamares.job.configuration:
        return _failure(
            _("Package List Missing"),
            _("Cannot continue without list of packages to install"),
            "module-configuration",
            category="unknown",
            reason_code="pacstrap.module-configuration.base-packages-missing",
        )

    configured_packages = libcalamares.job.configuration["basePackages"]
    if not isinstance(configured_packages, list):
        return _failure(
            _("Bad configuration"),
            _("basePackages must be a list"),
            "module-configuration",
            category="unknown",
            reason_code="pacstrap.module-configuration.base-packages-invalid",
        )
    configured_required = libcalamares.job.configuration.get("requiredPackages", [])
    if not isinstance(configured_required, list):
        return _failure(
            _("Bad configuration"),
            _("requiredPackages must be a list"),
            "module-configuration",
            category="unknown",
            reason_code="pacstrap.module-configuration.required-packages-invalid",
        )
    base_packages = list(configured_packages)
    required_packages = list(configured_required)
    base_packages.extend(required_packages)

    repository_selection = (
        libcalamares.globalstorage.value("packagechooser_repository") or "catos"
    )
    if repository_selection not in {"catos", CACHYOS_SELECTION}:
        return _failure(
            _("Invalid repository selection"),
            _("Unsupported repository selection: {repository}").format(repository=repository_selection),
            "repository-configuration",
            category="repository-configuration",
            reason_code="pacstrap.repository-configuration.selection-invalid",
        )
    pacman_config = pacman_config_for(repository_selection)
    if not os.path.isfile(pacman_config):
        return _failure(
            _("Repository configuration missing"),
            _("Required pacman configuration does not exist: {path}").format(path=pacman_config),
            "repository-configuration",
            category="repository-configuration",
            reason_code="pacstrap.repository-configuration.pacman-config-missing",
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
        return _failure(
            _("Invalid boot configuration"),
            _("Bootloader package plan is invalid: {error}").format(error=error),
            "boot-package-plan",
            error=error,
            category="unknown",
            reason_code="pacstrap.boot-package-plan.invalid",
        )

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
            _("Package Manager error"),
            _("Could not refresh repository databases for base system installation"),
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
            details = _("Missing required packages: {packages}").format(
                packages=", ".join(missing_install_packages)
            )
            return _failure(
                _("Required installation packages are unavailable"),
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
            _("Package Manager error"),
            _("Could not query repository metadata for base system install"),
            "repository-metadata",
            error=e,
        )
    except Exception as e:
        libcalamares.utils.warning(f"pkgcheck failed: {e!s}")
        return _failure(
            _("Package Manager error"),
            _("pkgcheck failed while preparing base system package list"),
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
    heartbeat_func = None
    phase_state = {"transaction_started": False, "progress": 0.05}
    transaction_tracker = PacmanTransactionTracker()
    try:
        plan, cache_directory = _download_plan(pacman_config, root_mount_point, base_packages)
        if plan.total_bytes > 0:
            custom_status_message = _("Preparing package downloads")
            libcalamares.job.setprogress(0.05)
            sampler = TransferSampler(plan, [cache_directory])
            reporter = _transfer_reporter(0.05, 0.78, phase_state)
            heartbeat_func = lambda: reporter(sampler.sample())
            heartbeat_func()
            libcalamares.utils.debug(
                "pacstrap: planned {} package downloads ({})".format(
                    len(plan.downloads),
                    format_transfer_status(
                        TransferSampler(plan, [cache_directory]).sample(),
                        _("Download plan"),
                    ),
                )
            )
        else:
            custom_status_message = _("All required packages are cached; installing packages")
            phase_state["progress"] = max(phase_state["progress"], 0.78)
            libcalamares.job.setprogress(phase_state["progress"])
    except Exception as error:
        heartbeat_func = None
        libcalamares.utils.warning(f"pacstrap download telemetry unavailable: {error!s}")

    def install_output(frame):
        line_cb(frame)
        transaction_ratio = transaction_tracker.observe(frame)
        if transaction_ratio is not None:
            phase_state["transaction_started"] = True
            phase_state["progress"] = max(
                phase_state["progress"],
                map_progress(0.80, 0.96, transaction_ratio),
            )
        libcalamares.job.setprogress(phase_state["progress"])

    try:
        run_in_host(pacstrap_command, install_output, heartbeat_func)
    except PacmanError as pe:
        details = _("{error}\nLast pacstrap output:\n{output}").format(
            error=pe, output="\n".join(recent_output)
        )
        return _failure(
            _("Failed to run pacstrap"),
            details,
            "base-system-install",
            error=pe,
        )
    except Exception as e:
        return _failure(
            _("Failed to run pacstrap"),
            _("pacstrap failed: {error}").format(error=e),
            "base-system-install",
            error=e,
        )

    libcalamares.job.setprogress(0.97)
    custom_status_message = _("Finalizing the base system")
    try:
        install_repository_config(root_mount_point, repository_selection)
    except Exception as error:
        return _failure(
            _("Failed to install repository configuration"),
            _("Could not copy pacman configuration into target: {error}").format(error=error),
            "repository-configuration",
            error=error,
        )

    # --- copy files post install ---
    libcalamares.job.setprogress(0.98)
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
            return _failure(
                _("Bad configuration"),
                _("{key} must be a list of file paths").format(key=key),
                "module-configuration",
                category="unknown",
                reason_code="pacstrap.module-configuration.post-install-files-invalid",
            )

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
            message = _("Installer file is missing: {path}").format(path=source_file)
            if is_required:
                return _failure(
                    _("Required installer file missing"),
                    message,
                    "post-install-files",
                    category="unknown",
                    reason_code="pacstrap.post-install-files.required-file-missing",
                )
            libcalamares.utils.warning(message)
            continue

        if source_file in required_executables and not os.access(source_file, os.X_OK):
            return _failure(
                _("Required installer helper is not executable"),
                _("Installer helper is not executable: {path}").format(path=source_file),
                "post-install-files",
                category="unknown",
                reason_code="pacstrap.post-install-files.source-not-executable",
            )

        try:
            libcalamares.utils.debug("Copying file {!s}".format(source_file))
            dest = os.path.normpath(root_mount_point + source_file)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(source_file, dest)
        except Exception as error:
            message = _("Failed to copy installer file {path}: {error}").format(
                path=source_file, error=error
            )
            if is_required:
                return _failure(
                    _("Failed to copy required installer file"),
                    message,
                    "post-install-files",
                    error=error,
                    category="unknown",
                    reason_code="pacstrap.post-install-files.copy-failed",
                )
            libcalamares.utils.warning(message)
            continue

        if source_file in required_executables and not os.access(dest, os.X_OK):
            return _failure(
                _("Required installer helper lost executable permissions"),
                _("Copied installer helper is not executable: {path}").format(path=dest),
                "post-install-files",
                category="unknown",
                reason_code="pacstrap.post-install-files.target-not-executable",
            )

    libcalamares.globalstorage.insert("online", True)
    libcalamares.job.setprogress(1.0)
    return None
