from __future__ import annotations

import importlib.util
from pathlib import Path
import os
import subprocess
import tempfile
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


class RecoveryFailureContextTests(unittest.TestCase):
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
            "repository-metadata",
            error=error,
        )

        self.assertEqual(result[0], "Package Manager error")
        context = fake.storage.values["recovery.failureContext"]
        self.assertEqual(context["source"], "pacstrap")
        self.assertEqual(context["stage"], "repository-metadata")
        self.assertEqual(context["command"], "pacman -Slq")
        self.assertEqual(context["exitCode"], 1)
        self.assertIn("404", context["output"])

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

        module._failure(
            "Package Manager error",
            "Package installation failed",
            error,
            "package-install",
        )

        context = fake.storage.values["recovery.failureContext"]
        self.assertEqual(context["source"], "pacman")
        self.assertEqual(context["stage"], "package-install")
        self.assertEqual(context["command"], "pacman -S linux")
        self.assertIn("retrying mirror", context["output"])
        self.assertIn("Could not resolve host", context["output"])

    def test_target_keyring_refresh_is_idempotent_reconciliation(self) -> None:
        fake = FakeCalamares()
        module = load_module(
            "catos_test_pacman_keyring",
            "usr/lib/calamares/modules/pacman/main.py",
            fake,
            {"pkgcheck": module_stub("pkgcheck")},
        )

        module._refresh_target_keyring()

        self.assertEqual(fake.commands[0], ["pacman-key", "--init"])
        populate_command = fake.commands[1]
        self.assertEqual(populate_command[:2], ["/bin/sh", "-c"])
        self.assertEqual(
            populate_command[5:],
            ["archlinux", "catos", "arch4edu", "archlinuxcn"],
        )
        self.assertNotIn("blackarch", populate_command)

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

    def test_target_keyring_refresh_adds_cachyos_only_when_selected(self) -> None:
        fake = FakeCalamares()
        fake.storage.insert("packagechooser_repository", "cachyos")
        module = load_module(
            "catos_test_pacman_cachyos_keyring",
            "usr/lib/calamares/modules/pacman/main.py",
            fake,
            {"pkgcheck": module_stub("pkgcheck")},
        )

        module._refresh_target_keyring()

        self.assertIn("cachyos", fake.commands[1])
        self.assertNotIn("blackarch", fake.commands[1])

    def test_repository_recovery_performs_one_full_upgrade_before_package_work(self) -> None:
        fake = FakeCalamares()
        fake.storage.insert("hasInternet", True)
        fake.storage.insert("recovery.refreshRepositories", True)
        fake.module.job.configuration = {
            "backend": "pacman",
            "skip_if_no_internet": False,
            "update_db": False,
            "update_system": False,
            "operations": [],
            "pacman": {},
        }
        pkgcheck = module_stub("pkgcheck")
        pkgcheck.build_repo_index = lambda: (set(), set())
        pkgcheck.preprocess_operations = lambda **_kwargs: ([], 0)
        module = load_module(
            "catos_test_pacman_refresh",
            "usr/lib/calamares/modules/pacman/main.py",
            fake,
            {"pkgcheck": pkgcheck},
        )
        calls: list[str] = []

        class FakePacmanManager:
            def full_upgrade(self) -> None:
                calls.append("full_upgrade")

        module.PacmanManager = FakePacmanManager
        module._refresh_target_keyring = lambda: calls.append("keyring")

        result = module.run()

        self.assertIsNone(result)
        self.assertEqual(calls, ["keyring", "full_upgrade"])
        self.assertNotIn("recovery.refreshRepositories", fake.storage.values)

    def test_failed_repository_refresh_keeps_flag_and_skips_package_work(self) -> None:
        fake = FakeCalamares()
        fake.storage.insert("hasInternet", True)
        fake.storage.insert("recovery.refreshRepositories", True)
        fake.module.job.configuration = {
            "backend": "pacman",
            "skip_if_no_internet": False,
            "update_db": False,
            "update_system": False,
            "operations": [{"install": ["linux"]}],
            "pacman": {},
        }
        pkgcheck = module_stub("pkgcheck")
        pkgcheck.build_repo_index = mock.Mock(side_effect=AssertionError("package work must not start"))
        pkgcheck.preprocess_operations = mock.Mock(side_effect=AssertionError("package work must not start"))
        module = load_module(
            "catos_test_pacman_refresh_failure",
            "usr/lib/calamares/modules/pacman/main.py",
            fake,
            {"pkgcheck": pkgcheck},
        )

        class FakePacmanManager:
            def full_upgrade(self) -> None:
                raise subprocess.CalledProcessError(
                    1,
                    ["pacman", "-Syu", "--noconfirm"],
                    stderr="The requested URL returned error: 404",
                )

        module.PacmanManager = FakePacmanManager
        module._refresh_target_keyring = lambda: None

        result = module.run()

        self.assertEqual(result[0], "Package Manager error")
        self.assertIn("recovery.refreshRepositories", fake.storage.values)
        context = fake.storage.values["recovery.failureContext"]
        self.assertEqual(context["stage"], "repository-full-upgrade")
        self.assertEqual(context["command"], "pacman -Syu --noconfirm")
        self.assertIn("404", context["output"])
        pkgcheck.build_repo_index.assert_not_called()

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

    def test_pacstrap_sync_failure_stops_before_repository_query(self) -> None:
        fake = FakeCalamares()
        registry_error = type("RegistryError", (Exception,), {})
        module = load_module(
            "catos_test_pacstrap_sync_failure",
            "usr/lib/calamares/modules/pacstrap/main.py",
            fake,
            {
                "pkgcheck": module_stub("pkgcheck", filter_operation_list=lambda *_args: []),
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
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pacman_config = root / "pacman.conf"
            pacman_config.write_text("[options]\n", encoding="utf-8")
            fake.storage.insert("rootMountPoint", str(root))
            fake.storage.insert("hasInternet", True)
            fake.storage.insert("packagechooser_repository", "catos")
            fake.storage.insert("firmwareType", "bios")
            fake.module.job.configuration = {
                "basePackages": ["base"],
                "requiredPackages": [],
            }
            module.pacman_config_for = lambda _selection: str(pacman_config)
            module._maybe_sync_db_host = mock.Mock(
                side_effect=module.PacmanError(
                    "sync failed",
                    command=["pacman", "--config", str(pacman_config), "-Sy", "--noconfirm"],
                    returncode=1,
                    output="Could not resolve host: mirror.example",
                )
            )
            module._build_repo_index_host = mock.Mock(
                side_effect=AssertionError("repository query must not run after failed sync")
            )

            result = module.run()

        self.assertEqual(result[0], "Package Manager error")
        context = fake.storage.values["recovery.failureContext"]
        self.assertEqual(context["stage"], "repository-database-sync")
        self.assertIn("Could not resolve host", context["output"])
        module._build_repo_index_host.assert_not_called()

    def test_bootloader_failure_records_provider_and_phase(self) -> None:
        fake = FakeCalamares()
        fake.module.job.configuration = {"phase": "install"}
        fake.storage.insert("bootloader.selected", "limine")
        registry_error = type("RegistryError", (Exception,), {})
        module = load_module(
            "catos_test_bootloadu",
            "usr/lib/calamares/modules/bootloadu/main.py",
            fake,
            {
                "context": module_stub(
                    "context",
                    BootContext=type("BootContext", (), {}),
                    ContextError=type("ContextError", (Exception,), {}),
                ),
                "providers": module_stub("providers", PROVIDERS={}),
                "providers.base": module_stub(
                    "providers.base",
                    BootloaduError=type("BootloaduError", (Exception,), {}),
                    configure_mkinitcpio=lambda _context: None,
                ),
                "registry": module_stub(
                    "registry",
                    RegistryError=registry_error,
                    install_marker=lambda _registry: "/run/calamares/marker",
                    load_bootloader_registry=mock.Mock(side_effect=registry_error("registry unavailable")),
                    platform_supported=lambda *_args: True,
                    provider_profile=lambda *_args: {},
                ),
                "secureboot": module_stub(
                    "secureboot",
                    enable_target_secure_boot=lambda *_args: None,
                    prepare_secure_boot=lambda *_args: None,
                ),
            },
        )

        result = module.run()

        self.assertEqual(result[0], "Boot setup failed")
        context = fake.storage.values["recovery.failureContext"]
        self.assertEqual(context["source"], "bootloadu")
        self.assertEqual(context["stage"], "install")
        self.assertEqual(context["provider"], "limine")
        self.assertIn("registry unavailable", context["details"])

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
