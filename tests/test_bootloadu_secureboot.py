from pathlib import Path
import json
import sys
import tempfile
import types
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "usr/lib/calamares/modules/bootloadu"
REGISTRY_PATH = ROOT / "usr/share/calamares/catos/bootloaders.yaml"

sys.path.insert(0, str(MODULE))
if "libcalamares" not in sys.modules:
    sys.modules["libcalamares"] = types.SimpleNamespace(
        utils=types.SimpleNamespace(
            target_env_call=lambda _args: 1,
            check_target_env_output=lambda _args: "",
            debug=lambda _message: None,
        )
    )

from providers.base import BootloaduError  # noqa: E402
from registry import RegistryError, load_bootloader_registry, package_plan  # noqa: E402
from secureboot import enable_target_secure_boot, prepare_secure_boot, secure_boot_enabled  # noqa: E402
import secureboot as secureboot_module  # noqa: E402


class Storage:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def value(self, key):
        return self.values.get(key)

    def insert(self, key, value):
        self.values[key] = value


class SecureBootTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = load_bootloader_registry(REGISTRY_PATH)

    def test_efi_variable_detection_reads_secure_boot_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            variable = root / "SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"
            variable.write_bytes(b"\x00\x00\x00\x00\x01")
            self.assertTrue(secure_boot_enabled(root))
            variable.write_bytes(b"\x00\x00\x00\x00\x00")
            self.assertFalse(secure_boot_enabled(root))


    def test_prepare_secure_boot_supports_uki_and_rejects_efistub(self):
        storage = Storage()
        supported = types.SimpleNamespace(provider_id="uki", firmware="efi")
        unsupported = types.SimpleNamespace(provider_id="efistub", firmware="efi")
        with mock.patch.object(secureboot_module, "secure_boot_enabled", return_value=True):
            self.assertTrue(prepare_secure_boot(storage, self.registry, supported))
            self.assertTrue(storage.value("secureboot.enabled"))
            with self.assertRaises(RegistryError):
                prepare_secure_boot(Storage(), self.registry, unsupported)

    def test_package_plan_adds_secure_boot_package_only_when_active(self):
        disabled = package_plan(
            self.registry,
            "grub",
            snapshots_enabled=False,
            root_filesystem="ext4",
            firmware="efi",
            secure_boot_enabled=False,
        )
        enabled = package_plan(
            self.registry,
            "grub",
            snapshots_enabled=False,
            root_filesystem="ext4",
            firmware="efi",
            secure_boot_enabled=True,
        )
        self.assertNotIn("catos-secureboot", disabled)
        self.assertIn("catos-secureboot", enabled)

    def test_secure_boot_supports_direct_uki_but_rejects_efistub(self):
        enabled = package_plan(
            self.registry,
            "uki",
            snapshots_enabled=False,
            root_filesystem="ext4",
            firmware="efi",
            secure_boot_enabled=True,
        )
        self.assertIn("catos-secureboot", enabled)
        with self.assertRaises(RegistryError):
            package_plan(
                self.registry,
                "efistub",
                snapshots_enabled=False,
                root_filesystem="ext4",
                firmware="efi",
                secure_boot_enabled=True,
            )

    def test_finalizer_enables_target_and_preserves_private_result(self):
        storage = Storage({"secureboot.enabled": True})
        context = types.SimpleNamespace(provider_id="grub", firmware="efi")
        payload = {
            "fingerprint": "AA:BB",
            "provider": "grub",
            "boot_chain_verified": True,
            "deployed_kernels_verified": 1,
            "enrollment_pending": True,
            "enrollment_password": "one-time-secret",
            "kernels_signed": 1,
        }
        with mock.patch.object(
            secureboot_module.libcalamares.utils,
            "check_target_env_output",
            return_value=json.dumps(payload),
            create=True,
        ) as output, mock.patch.object(
            secureboot_module.libcalamares.utils,
            "debug",
        ) as debug:
            result = enable_target_secure_boot(storage, self.registry, context)

        output.assert_called_once_with(
            [
                "catos-secureboot",
                "enable",
                "--provider",
                "grub",
                "--generate-enrollment-password",
                "--json",
            ]
        )
        self.assertEqual(result["fingerprint"], "AA:BB")
        self.assertEqual(storage.value("secureboot.enrollmentPassword"), "one-time-secret")
        self.assertEqual(storage.value("secureboot.certificateFingerprint"), "AA:BB")
        self.assertTrue(storage.value("secureboot.enrollmentPending"))
        self.assertFalse(any("one-time-secret" in str(call) for call in debug.call_args_list))

    def test_finalizer_rejects_an_unverified_or_wrong_boot_provider(self):
        storage = Storage({"secureboot.enabled": True})
        context = types.SimpleNamespace(provider_id="grub", firmware="efi")
        base = {
            "fingerprint": "AA:BB",
            "provider": "grub",
            "boot_chain_verified": True,
            "deployed_kernels_verified": 1,
            "enrollment_pending": True,
        }
        for override in (
            {"provider": "systemd-boot"},
            {"boot_chain_verified": False},
            {"deployed_kernels_verified": 0},
        ):
            with self.subTest(override=override), mock.patch.object(
                secureboot_module.libcalamares.utils,
                "check_target_env_output",
                return_value=json.dumps(base | override),
                create=True,
            ):
                with self.assertRaises(BootloaduError):
                    enable_target_secure_boot(storage, self.registry, context)

    def test_finalizer_is_noop_when_live_secure_boot_is_disabled(self):
        storage = Storage({"secureboot.enabled": False})
        context = types.SimpleNamespace(provider_id="grub", firmware="efi")
        with mock.patch.object(
            secureboot_module.libcalamares.utils,
            "check_target_env_output",
            create=True,
        ) as output:
            self.assertIsNone(enable_target_secure_boot(storage, self.registry, context))
        output.assert_not_called()


if __name__ == "__main__":
    unittest.main()
