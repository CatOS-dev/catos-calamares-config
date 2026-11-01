from __future__ import annotations

import importlib.util
from pathlib import Path
import os
import subprocess
import tempfile
import time
import sys
import types
import unittest

from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class FakeGlobalStorage:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def insert(self, key: str, value: object) -> None:
        self.values[key] = value

    def value(self, key: str) -> object:
        return self.values.get(key)

    def contains(self, key: str) -> bool:
        return key in self.values

    def remove(self, key: str) -> None:
        self.values.pop(key, None)


class FakeCalamares:
    def __init__(self) -> None:
        self.storage = FakeGlobalStorage()
        self.commands: list[list[str]] = []
        self.module = types.ModuleType("libcalamares")
        self.module.__path__ = []
        self.utils = types.ModuleType("libcalamares.utils")
        self.utils.gettext_path = lambda: ""
        self.utils.gettext_languages = lambda: ["en"]
        self.utils.debug = lambda _message: None
        self.utils.warning = lambda _message: None
        self.utils.error = lambda _message: None
        self.utils.target_env_process_output = lambda _args, _callback: 0
        self.utils.check_target_env_call = self._record_command
        self.module.utils = self.utils
        self.module.globalstorage = self.storage
        self.module.job = types.SimpleNamespace(
            configuration={},
            setprogress=lambda _progress: None,
        )

    def _record_command(self, command: list[str]) -> int:
        self.commands.append(list(command))
        return 0


def module_stub(name: str, **attributes: object) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def load_module(name: str, relative: str, fake: FakeCalamares, dependencies: dict[str, types.ModuleType]):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    injected = {
        name: module,
        "libcalamares": fake.module,
        "libcalamares.utils": fake.utils,
        **dependencies,
    }
    with mock.patch.dict(sys.modules, injected):
        spec.loader.exec_module(module)
    return module


class PackageOutputTests(unittest.TestCase):
    def test_pacstrap_failure_preserves_command_and_output(self) -> None:
        fake = FakeCalamares()
        registry_error = type("RegistryError", (Exception,), {})
        module = load_module(
            "catos_test_pacstrap",
            "usr/lib/calamares/modules/pacstrap/main.py",
            fake,
            {
                "pkgcheck": module_stub("pkgcheck"),
                "pacstrap_repository": module_stub(
                    "pacstrap_repository",
                    CACHYOS_SELECTION="cachyos",
                    install_repository_config=lambda *_args, **_kwargs: None,
                    pacman_config_for=lambda *_args, **_kwargs: "/etc/pacman.conf",
                    transform_packages=lambda packages, _selection: packages,
                ),
                "secureboot": module_stub("secureboot", secure_boot_enabled=lambda: False),
                "registry": module_stub(
                    "registry",
                    RegistryError=registry_error,
                    load_bootloader_registry=lambda: {},
                    missing_required_packages=lambda *_args: [],
                    package_plan=lambda *_args, **_kwargs: [],
                ),
            },
        )
        error = module.PacmanError(
            "repository query failed",
            command=["pacman", "-Slq"],
            returncode=1,
            output="The requested URL returned error: 404",
        )

        result = module._failure(
            "Package Manager error",
            "Could not query repository metadata",
            error=error,
        )

        self.assertEqual(result[0], "Package Manager error")
        self.assertIn("Command: pacman -Slq", result[1])
        self.assertIn("Exit code: 1", result[1])
        self.assertIn("404", result[1])

    def test_pacman_failure_includes_streamed_and_subprocess_output(self) -> None:
        fake = FakeCalamares()
        module = load_module(
            "catos_test_pacman",
            "usr/lib/calamares/modules/pacman/main.py",
            fake,
            {"pkgcheck": module_stub("pkgcheck")},
        )
        module.recent_output[:] = ["retrying mirror"]
        error = subprocess.CalledProcessError(
            1,
            ["pacman", "-S", "linux"],
            output="failed retrieving file",
            stderr="Could not resolve host: mirror.example",
        )

        result = module._failure(
            "Package Manager error",
            "Package installation failed",
            error,
        )

        self.assertIn("Command: pacman -S linux", result[1])
        self.assertIn("retrying mirror", result[1])
        self.assertIn("Could not resolve host", result[1])


    def test_keyring_populate_command_filters_missing_keyrings_without_losing_first_argument(self) -> None:
        fake = FakeCalamares()
        module = load_module(
            "catos_test_pacman_keyring_command",
            "usr/lib/calamares/modules/pacman/main.py",
            fake,
            {"pkgcheck": module_stub("pkgcheck")},
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            keyrings = root / "keyrings"
            binaries = root / "bin"
            keyrings.mkdir()
            binaries.mkdir()
            (keyrings / "archlinux.gpg").touch()
            (keyrings / "catos.gpg").touch()
            log = root / "pacman-key.log"
            fake_pacman_key = binaries / "pacman-key"
            fake_pacman_key.write_text(
                '#!/bin/sh\nprintf "%s\n" "$@" > "$PACMAN_KEY_LOG"\n',
                encoding="utf-8",
            )
            fake_pacman_key.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{binaries}:{environment['PATH']}"
            environment["PACMAN_KEY_LOG"] = str(log)

            subprocess.run(
                module._keyring_populate_command(
                    ["archlinux", "catos", "arch4edu"],
                    str(keyrings),
                ),
                check=True,
                env=environment,
            )

            self.assertEqual(
                log.read_text(encoding="utf-8").splitlines(),
                ["--populate", "archlinux", "catos"],
            )


    def test_remove_only_operations_do_not_require_keyring_refresh(self) -> None:
        fake = FakeCalamares()
        fake.module.job.configuration = {
            "backend": "pacman",
            "skip_if_no_internet": False,
            "update_db": False,
            "update_system": False,
            "operations": [{"try_remove": ["live-only-package"]}],
            "pacman": {},
        }
        pkgcheck = module_stub("pkgcheck")
        pkgcheck.build_repo_index = lambda: ({"live-only-package"}, set())
        pkgcheck.preprocess_operations = lambda **_kwargs: ([{"try_remove": ["live-only-package"]}], 1)
        module = load_module(
            "catos_test_remove_without_keyring",
            "usr/lib/calamares/modules/pacman/main.py",
            fake,
            {"pkgcheck": pkgcheck},
        )
        keyring = mock.Mock(side_effect=AssertionError("remove-only work must not refresh keyrings"))
        module._refresh_target_keyring = keyring

        class FakePacmanManager:
            package_phase_start = 0.0

            def operation_try_remove(self, packages) -> None:
                self.removed = list(packages)

            def report_package_completion(self) -> None:
                return None

        module.PacmanManager = FakePacmanManager

        self.assertIsNone(module.run())
        keyring.assert_not_called()

    def test_keyring_population_fails_when_archlinux_keyring_is_missing(self) -> None:
        fake = FakeCalamares()
        module = load_module(
            "catos_test_missing_required_keyring",
            "usr/lib/calamares/modules/pacman/main.py",
            fake,
            {"pkgcheck": module_stub("pkgcheck")},
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            keyrings = root / "keyrings"
            binaries = root / "bin"
            keyrings.mkdir()
            binaries.mkdir()
            log = root / "pacman-key.log"
            fake_pacman_key = binaries / "pacman-key"
            fake_pacman_key.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$@" > "$PACMAN_KEY_LOG"\n',
                encoding="utf-8",
            )
            fake_pacman_key.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{binaries}:{environment['PATH']}"
            environment["PACMAN_KEY_LOG"] = str(log)

            result = subprocess.run(
                module._keyring_populate_command(["archlinux", "catos"], str(keyrings)),
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("required keyring is missing", result.stderr)
        self.assertFalse(log.exists())

    def test_pacstrap_nonzero_exit_preserves_process_failure(self) -> None:
        fake = FakeCalamares()
        registry_error = type("RegistryError", (Exception,), {})
        module = load_module(
            "catos_test_pacstrap_failed_process",
            "usr/lib/calamares/modules/pacstrap/main.py",
            fake,
            {
                "pkgcheck": module_stub("pkgcheck"),
                "pacstrap_repository": module_stub(
                    "pacstrap_repository",
                    CACHYOS_SELECTION="cachyos",
                    install_repository_config=lambda *_args, **_kwargs: None,
                    pacman_config_for=lambda *_args, **_kwargs: "/etc/pacman.conf",
                    transform_packages=lambda packages, _selection: packages,
                ),
                "secureboot": module_stub("secureboot", secure_boot_enabled=lambda: False),
                "registry": module_stub(
                    "registry",
                    RegistryError=registry_error,
                    load_bootloader_registry=lambda: {},
                    missing_required_packages=lambda *_args: [],
                    package_plan=lambda *_args, **_kwargs: [],
                ),
            },
        )

        with self.assertRaises(module.PacmanError) as caught:
            module.run_in_host(
                ["sh", "-c", "printf 'download failed\\n'; exit 7"],
                lambda _frame: None,
            )

        self.assertEqual(caught.exception.returncode, 7)
        self.assertIn("download failed", caught.exception.output)
        self.assertEqual(caught.exception.cmd[:2], ["sh", "-c"])

    def test_pacstrap_reads_carriage_return_frames_immediately(self) -> None:
        fake = FakeCalamares()
        registry_error = type("RegistryError", (Exception,), {})
        module = load_module(
            "catos_test_pacstrap_frames",
            "usr/lib/calamares/modules/pacstrap/main.py",
            fake,
            {
                "pkgcheck": module_stub("pkgcheck"),
                "pacstrap_repository": module_stub(
                    "pacstrap_repository",
                    CACHYOS_SELECTION="cachyos",
                    install_repository_config=lambda *_args, **_kwargs: None,
                    pacman_config_for=lambda *_args, **_kwargs: "/etc/pacman.conf",
                    transform_packages=lambda packages, _selection: packages,
                ),
                "secureboot": module_stub("secureboot", secure_boot_enabled=lambda: False),
                "registry": module_stub(
                    "registry",
                    RegistryError=registry_error,
                    load_bootloader_registry=lambda: {},
                    missing_required_packages=lambda *_args: [],
                    package_plan=lambda *_args, **_kwargs: [],
                ),
            },
        )
        frames: list[str] = []

        module.run_in_host(["sh", "-c", "printf 'downloading\rinstalling\nfinished'"], frames.append)

        self.assertEqual(frames, ["downloading", "installing", "finished"])

    def test_pacstrap_heartbeat_runs_while_command_is_silent(self) -> None:
        fake = FakeCalamares()
        registry_error = type("RegistryError", (Exception,), {})
        module = load_module(
            "catos_test_pacstrap_heartbeat",
            "usr/lib/calamares/modules/pacstrap/main.py",
            fake,
            {
                "pkgcheck": module_stub("pkgcheck"),
                "pacstrap_repository": module_stub(
                    "pacstrap_repository",
                    CACHYOS_SELECTION="cachyos",
                    install_repository_config=lambda *_args, **_kwargs: None,
                    pacman_config_for=lambda *_args, **_kwargs: "/etc/pacman.conf",
                    transform_packages=lambda packages, _selection: packages,
                ),
                "secureboot": module_stub("secureboot", secure_boot_enabled=lambda: False),
                "registry": module_stub(
                    "registry",
                    RegistryError=registry_error,
                    load_bootloader_registry=lambda: {},
                    missing_required_packages=lambda *_args: [],
                    package_plan=lambda *_args, **_kwargs: [],
                ),
            },
        )
        heartbeats: list[float] = []

        module.run_in_host(
            ["sh", "-c", "sleep 0.18"],
            lambda _frame: None,
            lambda: heartbeats.append(time.monotonic()),
            heartbeat_interval=0.04,
        )

        self.assertGreaterEqual(len(heartbeats), 2)

    def test_pacstrap_plan_uses_target_cache_directory(self) -> None:
        fake = FakeCalamares()
        registry_error = type("RegistryError", (Exception,), {})
        module = load_module(
            "catos_test_pacstrap_plan",
            "usr/lib/calamares/modules/pacstrap/main.py",
            fake,
            {
                "pkgcheck": module_stub("pkgcheck"),
                "pacstrap_repository": module_stub(
                    "pacstrap_repository",
                    CACHYOS_SELECTION="cachyos",
                    install_repository_config=lambda *_args, **_kwargs: None,
                    pacman_config_for=lambda *_args, **_kwargs: "/etc/pacman.conf",
                    transform_packages=lambda packages, _selection: packages,
                ),
                "secureboot": module_stub("secureboot", secure_boot_enabled=lambda: False),
                "registry": module_stub(
                    "registry",
                    RegistryError=registry_error,
                    load_bootloader_registry=lambda: {},
                    missing_required_packages=lambda *_args: [],
                    package_plan=lambda *_args, **_kwargs: [],
                ),
            },
        )
        captured: list[list[str]] = []
        observed: dict[str, object] = {}
        with tempfile.TemporaryDirectory() as host_db_temporary, tempfile.TemporaryDirectory() as temporary:
            host_db = Path(host_db_temporary)
            (host_db / "sync").mkdir()

            def capture(command):
                if command[0] == "pacman-conf":
                    return [str(host_db)]
                captured.append(command)
                dbpath = Path(command[command.index("--dbpath") + 1])
                observed["local_empty"] = (dbpath / "local").is_dir() and not any((dbpath / "local").iterdir())
                observed["sync_target"] = (dbpath / "sync").resolve()
                return ["linux\t6.15-1\t1024\thttps://repo.example/linux.pkg.tar.zst"]

            module._host_capture_lines = capture
            plan, cache = module._download_plan("/etc/pacman.conf", temporary, ["linux"])

            self.assertEqual(plan.total_bytes, 1024)
            self.assertEqual(cache, Path(temporary) / "var/cache/pacman/pkg")
            command = captured[0]
            self.assertIn(str(cache), command)
            self.assertIn(module.PACMAN_PRINT_FORMAT, command)
            self.assertIn("--root", command)
            self.assertEqual(command[command.index("--root") + 1], temporary)
            self.assertTrue(observed["local_empty"])
            self.assertEqual(observed["sync_target"], host_db / "sync")

    def test_pacman_install_uses_real_download_telemetry(self) -> None:
        fake = FakeCalamares()
        progresses: list[float] = []
        fake.module.job.setprogress = progresses.append
        fake.module.job.configuration = {"pacman": {"num_retries": 0}}
        pkgcheck = module_stub("pkgcheck")
        with tempfile.TemporaryDirectory() as temporary:
            fake.storage.insert("rootMountPoint", temporary)
            calls: list[tuple[list[str], tuple[object, ...]]] = []

            def process_output(arguments, callback=None, *extra):
                calls.append((list(arguments), extra))
                if "--print-format" in arguments:
                    callback.append(
                        "linux\t6.15-1\t1024\thttps://repo.example/linux.pkg.tar.zst\n"
                    )
                    return 0
                if arguments[:2] == ["pacman-conf", "CacheDir"]:
                    callback.append("/var/cache/pacman/pkg\n")
                    return 0
                cache = Path(temporary) / "var/cache/pacman/pkg"
                cache.mkdir(parents=True, exist_ok=True)
                heartbeat = extra[-1]
                self.assertTrue(callable(heartbeat))
                (cache / "linux.pkg.tar.zst.part").write_bytes(b"x" * 512)
                heartbeat()
                callback("( 1/2) checking package integrity")
                (cache / "linux.pkg.tar.zst.part").unlink()
                (cache / "linux.pkg.tar.zst").write_bytes(b"x" * 1024)
                callback("( 2/2) installing linux")
                return 0

            fake.utils.target_env_process_output = process_output
            module = load_module(
                "catos_test_pacman_progress",
                "usr/lib/calamares/modules/pacman/main.py",
                fake,
                {"pkgcheck": pkgcheck},
            )
            module.total_packages = 1
            module.group_packages = 1
            module.completed_packages = 0
            manager = module.PacmanManager()

            manager.install(["linux"])

        actual_arguments, actual_extra = calls[-1]
        self.assertNotIn("--noprogressbar", actual_arguments)
        self.assertEqual(actual_extra[:3], ("", 0, True))
        self.assertTrue(callable(actual_extra[3]))
        self.assertTrue(any(0.0 < value < 1.0 for value in progresses))
        self.assertGreaterEqual(max(progresses), 0.95)
        self.assertEqual(progresses, sorted(progresses))



    def test_pacman_telemetry_failure_does_not_fail_package_install(self) -> None:
        fake = FakeCalamares()
        fake.module.job.configuration = {"pacman": {"num_retries": 0}}
        fake.storage.insert("rootMountPoint", "/tmp")
        actual_calls: list[list[str]] = []
        warnings: list[str] = []
        fake.utils.warning = warnings.append

        def process_output(arguments, callback=None, *extra):
            if "--print-format" in arguments:
                raise RuntimeError("planning unavailable")
            actual_calls.append(list(arguments))
            if callback is not None and callable(callback):
                callback("( 1/1) installing linux")
            return 0

        fake.utils.target_env_process_output = process_output
        module = load_module(
            "catos_test_pacman_telemetry_fallback",
            "usr/lib/calamares/modules/pacman/main.py",
            fake,
            {"pkgcheck": module_stub("pkgcheck")},
        )
        module.total_packages = 1
        module.group_packages = 1
        manager = module.PacmanManager()

        manager.install(["linux"])

        self.assertEqual(len(actual_calls), 1)
        self.assertIn("linux", actual_calls[0])
        self.assertTrue(any("telemetry unavailable" in message for message in warnings))

    def test_pacman_heartbeat_failure_does_not_interrupt_command(self) -> None:
        fake = FakeCalamares()
        fake.module.job.configuration = {"pacman": {"num_retries": 0}}
        fake.storage.insert("rootMountPoint", "/tmp")
        warnings: list[str] = []
        fake.utils.warning = warnings.append

        def process_output(arguments, callback=None, *extra):
            if "--print-format" in arguments:
                callback.append(
                    "linux\t6.15-1\t1024\thttps://repo.example/linux.pkg.tar.zst\n"
                )
                return 0
            if arguments[:2] == ["pacman-conf", "CacheDir"]:
                callback.append("/var/cache/pacman/pkg\n")
                return 0
            heartbeat = extra[-1]
            heartbeat()
            callback("( 1/1) installing linux")
            return 0

        fake.utils.target_env_process_output = process_output
        module = load_module(
            "catos_test_pacman_heartbeat_failure",
            "usr/lib/calamares/modules/pacman/main.py",
            fake,
            {"pkgcheck": module_stub("pkgcheck")},
        )
        module.total_packages = 1
        module.group_packages = 1
        manager = module.PacmanManager()
        manager._report_transfer = mock.Mock(side_effect=RuntimeError("sampling failed"))

        manager.install(["linux"])

        self.assertTrue(any("progress telemetry failed" in message for message in warnings))


    def test_pacman_retry_reopens_download_progress_phase(self) -> None:
        fake = FakeCalamares()
        fake.module.job.configuration = {"pacman": {"num_retries": 0}}
        module = load_module(
            "catos_test_pacman_retry_phase",
            "usr/lib/calamares/modules/pacman/main.py",
            fake,
            {"pkgcheck": module_stub("pkgcheck")},
        )
        manager = module.PacmanManager()
        manager.transaction_started = True
        module._target_download_plan = lambda _command: None

        def process_output(_arguments, _callback, *_extra):
            self.assertFalse(manager.transaction_started)
            return 0

        fake.utils.target_env_process_output = process_output
        manager.run_pacman(["pacman", "-S", "--noconfirm", "linux"], callback=True)


    def test_pacman_package_completion_keeps_preflight_offset_monotonic(self) -> None:
        fake = FakeCalamares()
        progresses: list[float] = []
        fake.module.job.setprogress = progresses.append
        fake.module.job.configuration = {"pacman": {}}
        module = load_module(
            "catos_test_pacman_completion_offset",
            "usr/lib/calamares/modules/pacman/main.py",
            fake,
            {"pkgcheck": module_stub("pkgcheck")},
        )
        module.total_packages = 4
        module.completed_packages = 0
        manager = module.PacmanManager()
        manager.package_phase_start = 0.20

        def complete_operation(_command, callback=False):
            self.assertTrue(callback)
            manager.progress_fraction = manager.operation_end
            fake.module.job.setprogress(manager.progress_fraction)

        manager.run_pacman = complete_operation
        module.run_operations(manager, {"install": ["one", "two"]})

        self.assertEqual(progresses, sorted(progresses))
        self.assertAlmostEqual(progresses[-1], 0.60)

    def test_pacman_failure_uses_only_current_command_output(self) -> None:
        fake = FakeCalamares()
        fake.module.job.configuration = {"pacman": {"num_retries": 0}}
        module = load_module(
            "catos_test_pacman_current_output",
            "usr/lib/calamares/modules/pacman/main.py",
            fake,
            {"pkgcheck": module_stub("pkgcheck")},
        )
        module._target_download_plan = lambda _command: None
        manager = module.PacmanManager()
        calls = 0

        def process_output(arguments, callback=None, *_extra):
            nonlocal calls
            calls += 1
            if calls == 1:
                callback("error: Could not resolve host: old.example")
                raise subprocess.CalledProcessError(1, arguments)
            callback("error: failed to prepare transaction (could not satisfy dependencies)")
            raise subprocess.CalledProcessError(1, arguments)

        fake.utils.target_env_process_output = process_output
        with self.assertRaises(subprocess.CalledProcessError):
            manager.run_pacman(["pacman", "-S", "optional"], callback=True)
        try:
            manager.run_pacman(["pacman", "-S", "required"], callback=True)
        except subprocess.CalledProcessError as error:
            result = module._failure(
                "Package Manager error",
                "Package installation failed",
                error,
            )

        self.assertNotIn("old.example", result[1])
        self.assertIn("could not satisfy dependencies", result[1])


    def test_pacstrap_telemetry_fallback_keeps_progress_monotonic(self) -> None:
        fake = FakeCalamares()
        registry_error = type("RegistryError", (Exception,), {})
        progresses: list[float] = []
        fake.module.job.setprogress = progresses.append
        fake.module.job.configuration = {
            "basePackages": ["base"],
            "requiredPackages": [],
            "postInstallFiles": [],
            "requiredPostInstallFiles": [],
            "requiredPostInstallExecutables": [],
            "sync_db": False,
        }
        module = load_module(
            "catos_test_pacstrap_telemetry_fallback_progress",
            "usr/lib/calamares/modules/pacstrap/main.py",
            fake,
            {
                "pkgcheck": module_stub(
                    "pkgcheck",
                    filter_operation_list=lambda _key, items, _packages, _groups: list(items),
                ),
                "pacstrap_repository": module_stub(
                    "pacstrap_repository",
                    CACHYOS_SELECTION="cachyos",
                    install_repository_config=lambda *_args, **_kwargs: None,
                    pacman_config_for=lambda *_args, **_kwargs: "/etc/pacman.conf",
                    transform_packages=lambda packages, _selection: list(packages),
                ),
                "secureboot": module_stub("secureboot", secure_boot_enabled=lambda: False),
                "registry": module_stub(
                    "registry",
                    RegistryError=registry_error,
                    load_bootloader_registry=lambda: {},
                    missing_required_packages=lambda *_args: [],
                    package_plan=lambda *_args, **_kwargs: [],
                ),
            },
        )
        with tempfile.TemporaryDirectory() as temporary:
            fake.storage.insert("rootMountPoint", temporary)
            fake.storage.insert("packagechooser_repository", "catos")
            fake.storage.insert("hasInternet", False)
            module._build_repo_index_host = lambda _config: ({"base"}, set())
            module._download_plan = mock.Mock(side_effect=RuntimeError("planning unavailable"))

            def run_in_host(_command, callback, _heartbeat=None):
                callback("starting package transaction")

            module.run_in_host = run_in_host
            result = module.run()

        self.assertIsNone(result)
        self.assertEqual(progresses, sorted(progresses))
        self.assertGreaterEqual(progresses[1], 0.05)



    def test_pacman_refresh_without_welcome_internet_probe(self) -> None:
        fake = FakeCalamares()
        fake.module.job.configuration = {"backend": "pacman", "update_db": True, "operations": []}
        module = load_module(
            "catos_test_pacman_no_network_probe",
            "usr/lib/calamares/modules/pacman/main.py",
            fake,
            {
                "pkgcheck": module_stub(
                    "pkgcheck",
                    build_repo_index=lambda: (set(), set()),
                    preprocess_operations=lambda **_kwargs: ([], 0),
                ),
            },
        )
        calls = []
        module._refresh_target_keyring = lambda: calls.append("keyring")
        module.PacmanManager = lambda: types.SimpleNamespace(
            update_db=lambda: calls.append("refresh"),
            update_system=lambda: calls.append("upgrade"),
        )

        self.assertFalse(fake.storage.contains("hasInternet"))
        self.assertIsNone(module.run())
        self.assertEqual(calls, ["keyring", "refresh"])

    def test_paru_refresh_without_welcome_internet_probe(self) -> None:
        fake = FakeCalamares()
        fake.module.job.configuration = {
            "backend": "paru",
            "update_db": True,
            "update_system": True,
            "operations": [],
        }
        module = load_module(
            "catos_test_paru_no_network_probe",
            "usr/lib/calamares/modules/paru/main.py",
            fake,
            {"process": module_stub("process", ProcessTimeout=RuntimeError, run_process_group=lambda *_args: None)},
        )
        calls = []
        module.ParuManager = lambda: types.SimpleNamespace(
            update_db=lambda: calls.append("refresh"),
            update_system=lambda: calls.append("upgrade"),
            cleanup=lambda: calls.append("cleanup") or True,
        )

        self.assertFalse(fake.storage.contains("hasInternet"))
        self.assertIsNone(module.run())
        self.assertEqual(calls, ["refresh", "upgrade", "cleanup"])



    def test_bootloader_command_failure_preserves_real_output(self) -> None:
        fake = FakeCalamares()

        def fail_command(arguments, callback) -> None:
            callback("probing EFI variables")
            callback("efibootmgr: No space left on device")
            raise subprocess.CalledProcessError(
                7,
                arguments,
                output="efibootmgr stdout",
                stderr="efibootmgr stderr",
            )

        fake.utils.target_env_process_output = fail_command
        module = load_module(
            "catos_test_bootloadu_base_output",
            "usr/lib/calamares/modules/bootloadu/providers/base.py",
            fake,
            {},
        )

        with self.assertRaises(module.BootloaduError) as captured:
            module.run_target(["efibootmgr", "--create"], "register EFI entry")

        error = captured.exception
        self.assertEqual(error.command, ["efibootmgr", "--create"])
        self.assertEqual(error.returncode, 7)
        self.assertIn("probing EFI variables", error.output)
        self.assertIn("No space left on device", error.output)
        self.assertIn("efibootmgr stderr", error.output)


if __name__ == "__main__":
    unittest.main()
