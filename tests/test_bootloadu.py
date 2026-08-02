import importlib.util
from pathlib import Path
import shlex
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "usr/lib/calamares/modules/bootloadu"
REGISTRY_PATH = ROOT / "usr/share/calamares-advanced/modules/bootloaders.yaml"

sys.path.insert(0, str(MODULE))
if "libcalamares" not in sys.modules:
    sys.modules["libcalamares"] = types.SimpleNamespace(
        utils=types.SimpleNamespace(target_env_call=lambda _args: 1, debug=lambda _message: None)
    )
from context import ContextError, efi_device, root_partition  # noqa: E402
from providers import base as boot_base  # noqa: E402
from providers.firmware import FirmwareProvider  # noqa: E402
from providers.grub import GrubProvider  # noqa: E402
from providers.limine import LimineProvider  # noqa: E402
from providers.refind import RefindProvider  # noqa: E402
from providers.systemd_boot import SystemdBootProvider  # noqa: E402
import providers.grub as grub_provider  # noqa: E402
import providers.limine as limine_provider  # noqa: E402
import providers.refind as refind_provider  # noqa: E402
import providers.systemd_boot as systemd_boot_provider  # noqa: E402
from registry import (  # noqa: E402
    RegistryError,
    load_bootloader_registry,
    missing_required_packages,
    package_plan,
    platform_supported,
)

def parse_limine_semantics(source: str) -> tuple[dict[str, str], list[dict[str, object]]]:
    global_options: dict[str, str] = {}
    entries: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("/"):
            current = {"name": line.lstrip("/"), "options": {}}
            entries.append(current)
            continue
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        if current is None:
            global_options[key] = value
        else:
            options = current["options"]
            assert isinstance(options, dict)
            options[key] = value
    return global_options, entries


def parse_refind_boot_options(source: str) -> dict[str, list[str]]:
    parsed: dict[str, list[str]] = {}
    for line in source.splitlines():
        fields = shlex.split(line)
        if len(fields) != 2:
            raise AssertionError(f"invalid rEFInd boot option: {line}")
        parsed[fields[0]] = shlex.split(fields[1])
    return parsed


class BootloaduTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = load_bootloader_registry(REGISTRY_PATH)

    def test_package_plan_selects_snapshot_provider(self):
        plan = package_plan(
            self.registry,
            "limine",
            snapshots_enabled=True,
            root_filesystem="btrfs",
        )
        self.assertIn("limine-tool", plan)
        self.assertIn("catos-limine-theme", plan)
        self.assertIn("limine-btrfs", plan)
        self.assertIn("snapper", plan)
        self.assertNotIn("snap-pac", plan)
        self.assertEqual(len(plan), len(set(plan)))

    def test_refind_package_plan_installs_the_boot_manager_without_snapshot_support(self):
        plan = package_plan(
            self.registry,
            "refind",
            snapshots_enabled=False,
            root_filesystem="btrfs",
            firmware="efi",
        )
        self.assertIn("refind", plan)
        self.assertNotIn("refind-btrfs", plan)
        with self.assertRaises(RegistryError):
            package_plan(
                self.registry,
                "refind",
                snapshots_enabled=True,
                root_filesystem="btrfs",
                firmware="efi",
            )

    def test_grub_snapshot_plan_installs_watcher_dependency(self):
        plan = package_plan(
            self.registry,
            "grub",
            snapshots_enabled=True,
            root_filesystem="btrfs",
        )
        self.assertIn("grub-btrfs", plan)
        self.assertIn("inotify-tools", plan)
        self.assertIn("efibootmgr", plan)

    def test_required_boot_packages_cannot_be_silently_filtered(self):
        missing = missing_required_packages(
            ["grub", "linux", "catos-grub-theme-dark"],
            {"grub", "linux"},
            set(),
        )
        self.assertEqual(missing, ["catos-grub-theme-dark"])

    def test_snapshot_plan_rejects_incompatible_combinations(self):
        with self.assertRaises(RegistryError):
            package_plan(
                self.registry,
                "uki",
                snapshots_enabled=True,
                root_filesystem="btrfs",
            )
        with self.assertRaises(RegistryError):
            package_plan(
                self.registry,
                "grub",
                snapshots_enabled=True,
                root_filesystem="ext4",
            )

    def test_platform_matrix_is_not_a_cartesian_product(self):
        limine = self.registry["providers"]["limine"]
        self.assertTrue(platform_supported(limine, "efi", "x86_64"))
        self.assertFalse(platform_supported(limine, "bios", "x86_64"))
        self.assertFalse(platform_supported(limine, "efi", "aarch64"))
        refind = self.registry["providers"]["refind"]
        self.assertTrue(platform_supported(refind, "efi", "x86_64"))
        self.assertFalse(platform_supported(refind, "bios", "x86_64"))
        self.assertFalse(platform_supported(refind, "efi", "aarch64"))

    def test_snapshot_provider_appends_matching_overlayfs_hook(self):
        expected = {
            "grub": "grub-btrfs-overlayfs",
            "limine": "limine-btrfs-overlayfs",
            "systemd-boot": "sdboot-btrfs-overlayfs",
        }
        for provider_id, overlay_hook in expected.items():
            with self.subTest(provider=provider_id), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "etc").mkdir()
                (root / "etc/mkinitcpio.conf").write_text(
                    "HOOKS=(base udev block filesystems fsck)\n", encoding="utf-8"
                )
                context = types.SimpleNamespace(
                    provider_id=provider_id,
                    snapshots_enabled=True,
                    partitions=[{"mountPoint": "/", "fs": "btrfs"}],
                    target_path=lambda path: root / str(path).lstrip("/"),
                )
                with mock.patch.object(boot_base, "target_has", return_value=False):
                    boot_base.configure_mkinitcpio(context)
                result = (root / "etc/mkinitcpio.conf").read_text(encoding="utf-8")
                hooks = result.split("HOOKS=(", 1)[1].split(")", 1)[0].split()
                self.assertEqual(hooks[-1], overlay_hook)
                self.assertEqual(hooks.count(overlay_hook), 1)

    def test_mkinitcpio_omits_overlayfs_hook_without_snapshots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "etc").mkdir()
            (root / "etc/mkinitcpio.conf").write_text(
                "HOOKS=(base udev block filesystems fsck)\n", encoding="utf-8"
            )
            context = types.SimpleNamespace(
                provider_id="limine",
                snapshots_enabled=False,
                partitions=[{"mountPoint": "/", "fs": "btrfs"}],
                target_path=lambda path: root / str(path).lstrip("/"),
            )
            with mock.patch.object(boot_base, "target_has", return_value=False):
                boot_base.configure_mkinitcpio(context)
            result = (root / "etc/mkinitcpio.conf").read_text(encoding="utf-8")
            self.assertNotIn("btrfs-overlayfs", result)

    def test_mkinitcpio_configuration_embeds_keyfile_and_swap_hook(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "etc").mkdir()
            (root / "crypto_keyfile.bin").write_bytes(b"key")
            (root / "etc/mkinitcpio.conf").write_text(
                "MODULES=(existing)\nBINARIES=()\nFILES=()\n"
                "HOOKS=(base udev autodetect block filesystems fsck)\n",
                encoding="utf-8",
            )
            context = types.SimpleNamespace(
                root=root,
                partitions=[
                    {"mountPoint": "/", "fs": "btrfs", "luksMapperName": "cryptroot"},
                    {"mountPoint": "", "fs": "linuxswap", "luksMapperName": "cryptswap", "claimed": True},
                ],
                root_filesystem="btrfs",
                root_encrypted=True,
                target_path=lambda path: root / str(path).lstrip("/"),
            )
            with mock.patch.object(boot_base, "target_has", return_value=False):
                boot_base.configure_mkinitcpio(context)
            result = (root / "etc/mkinitcpio.conf").read_text(encoding="utf-8")
            self.assertIn("encrypt", result)
            self.assertIn("openswap", result)
            self.assertIn("resume", result)
            self.assertIn("FILES=(/crypto_keyfile.bin)", result)
            self.assertNotIn(" fsck", result)
            self.assertIn("MODULES=(existing)", result)

    def test_mkinitcpio_removes_stale_managed_keyfile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "etc").mkdir()
            config = root / "etc/mkinitcpio.conf"
            config.write_text(
                "FILES=(/crypto_keyfile.bin /etc/keep-me)\n"
                "HOOKS=(base udev filesystems fsck)\n",
                encoding="utf-8",
            )
            context = types.SimpleNamespace(
                partitions=[{"mountPoint": "/", "fs": "ext4"}],
                target_path=lambda path: root / str(path).lstrip("/"),
            )
            with mock.patch.object(boot_base, "target_has", return_value=False):
                boot_base.configure_mkinitcpio(context)
            result = config.read_text(encoding="utf-8")
            self.assertNotIn("/crypto_keyfile.bin", result)
            self.assertIn("/etc/keep-me", result)

    def test_grub_cryptodisk_is_disabled_with_unencrypted_separate_boot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = types.SimpleNamespace(
                root=root,
                branding_name="CatOS",
                root_encrypted=True,
                partitions=[
                    {"mountPoint": "/", "fs": "ext4", "luksMapperName": "cryptroot"},
                    {"mountPoint": "/boot", "fs": "ext4"},
                ],
                firmware="efi",
                architecture="x86_64",
                esp_mount_point="/boot/efi",
                target_path=lambda path: root / str(path).lstrip("/"),
            )
            with mock.patch.object(boot_base, "kernel_cmdline", return_value="quiet rw"), \
                 mock.patch.object(grub_provider, "build_initramfs"), \
                 mock.patch.object(grub_provider, "run_target"):
                GrubProvider(context, {}).execute()
            defaults = (root / "etc/default/grub.d/00-catos.cfg").read_text(encoding="utf-8")
            self.assertNotIn("GRUB_ENABLE_CRYPTODISK", defaults)

    def test_grub_theme_adjuster_uses_target_relative_command_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "run/calamares/adjust_grub_theme_after.sh"
            script.parent.mkdir(parents=True)
            script.write_text("#!/bin/sh\n", encoding="utf-8")
            context = types.SimpleNamespace(
                root=root,
                branding_name="CatOS",
                root_encrypted=False,
                partitions=[],
                firmware="efi",
                architecture="x86_64",
                esp_mount_point="/boot/efi",
                target_path=lambda path: root / str(path).lstrip("/"),
            )
            with mock.patch.object(boot_base, "kernel_cmdline", return_value="quiet rw"), \
                 mock.patch.object(grub_provider, "build_initramfs"), \
                 mock.patch.object(grub_provider, "run_target") as run_target:
                GrubProvider(context, {}).execute()
            commands = [call.args[0] for call in run_target.call_args_list]
            self.assertIn(["/run/calamares/adjust_grub_theme_after.sh"], commands)
            self.assertNotIn([str(script)], commands)

    def test_limine_install_applies_the_theme_and_enables_last_entry_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            esp = root / "boot/efi"
            esp.mkdir(parents=True)
            (esp / "limine.conf").write_text(
                "timeout: 3\nremember_last_entry: no\n/CatOS\nprotocol: linux\n",
                encoding="utf-8",
            )
            context = types.SimpleNamespace(
                root=root,
                branding_name="CatOS",
                esp_mount_point="/boot/efi",
                esp_path=esp,
                target_path=lambda path: root / str(path).lstrip("/"),
            )
            provider = LimineProvider.__new__(LimineProvider)
            provider.context = context
            provider.profile = {}
            provider.cmdline = "quiet rw"
            with mock.patch.object(limine_provider, "run_target") as run_target:
                provider.execute()

            rendered = (esp / "limine.conf").read_text(encoding="utf-8")
            global_options, entries = parse_limine_semantics(rendered)
            self.assertEqual(global_options["remember_last_entry"], "yes")
            self.assertEqual(entries[0]["name"], "CatOS")
            self.assertEqual(entries[0]["options"], {"protocol": "linux"})
            commands = [call.args[0] for call in run_target.call_args_list]
            self.assertEqual(commands[:2], [["limine-install"], ["limine-update"]])
            self.assertIn(["catos-limine-theme", "apply"], commands)

    def test_refind_provider_generates_boot_options_from_the_target_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            esp = root / "boot/efi"
            esp.mkdir(parents=True)
            boot = root / "boot"
            boot.mkdir(exist_ok=True)
            kernel = boot_base.Kernel(
                version="6.18.0-catos",
                package="linux-cachyos",
                image="/usr/lib/modules/6.18.0-catos/vmlinuz",
                initramfs="/boot/initramfs-linux-cachyos.img",
            )
            context = types.SimpleNamespace(
                root=root,
                branding_name="CatOS",
                esp_mount_point="/boot/efi",
                esp_path=esp,
                target_path=lambda path: root / str(path).lstrip("/"),
            )
            provider = RefindProvider.__new__(RefindProvider)
            provider.context = context
            provider.profile = {}
            provider.cmdline = "quiet rw root=UUID=abcd rootflags=subvol=@"
            with mock.patch.object(refind_provider, "build_initramfs", return_value=[kernel]), \
                 mock.patch.object(refind_provider, "run_target") as run_target:
                provider.execute()

            options = parse_refind_boot_options(
                (boot / "refind_linux.conf").read_text(encoding="utf-8")
            )
            standard = options["Boot with standard options"]
            single_user = options["Boot to single-user mode"]
            minimal = options["Boot with minimal options"]
            self.assertEqual(standard, shlex.split(provider.cmdline))
            self.assertEqual(single_user[:-1], standard)
            self.assertEqual(single_user[-1], "single")
            self.assertEqual(
                minimal,
                ["ro", "root=UUID=abcd", "rootflags=subvol=@"],
            )
            commands = [call.args[0] for call in run_target.call_args_list]
            self.assertEqual(commands[0], ["refind-install", "--yes"])

    def test_refind_verify_requires_a_boot_manager_and_bootable_kernel(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            esp = root / "boot/efi"
            refind = esp / "EFI/refind"
            refind.mkdir(parents=True)
            (refind / "refind_x64.efi").write_bytes(b"efi")
            (refind / "refind.conf").write_text("timeout 5\n", encoding="utf-8")
            (root / "boot").mkdir(exist_ok=True)
            (root / "boot/refind_linux.conf").write_text(
                '"Boot with standard options" "quiet rw root=UUID=abcd"\n',
                encoding="utf-8",
            )
            modules = root / "usr/lib/modules/6.18.0-catos"
            modules.mkdir(parents=True)
            (modules / "pkgbase").write_text("linux-cachyos\n", encoding="utf-8")
            (modules / "vmlinuz").write_bytes(b"kernel")
            context = types.SimpleNamespace(
                esp_path=esp,
                target_path=lambda path: root / str(path).lstrip("/"),
            )
            provider = RefindProvider.__new__(RefindProvider)
            provider.context = context
            provider.profile = {}
            provider.cmdline = "quiet rw root=UUID=abcd"
            with self.assertRaises(boot_base.BootloaduError):
                provider.verify()
            (root / "boot/vmlinuz-linux-cachyos").write_bytes(b"kernel")
            (root / "boot/initramfs-linux-cachyos.img").write_bytes(b"initramfs")
            provider.verify()

    def test_limine_verify_requires_deployed_kernel_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            esp = root / "boot/efi"
            esp.mkdir(parents=True)
            (esp / "limine.conf").write_text("/CatOS\n", encoding="utf-8")
            (root / "etc").mkdir()
            machine_id = "0123456789abcdef0123456789abcdef"
            (root / "etc/machine-id").write_text(machine_id + "\n", encoding="utf-8")
            modules = root / "usr/lib/modules/6.18.0"
            modules.mkdir(parents=True)
            (modules / "pkgbase").write_text("linux\n", encoding="utf-8")
            (modules / "vmlinuz").write_bytes(b"kernel")
            context = types.SimpleNamespace(
                esp_path=esp,
                target_path=lambda path: root / str(path).lstrip("/"),
            )
            provider = LimineProvider.__new__(LimineProvider)
            provider.context = context
            provider.profile = {}
            provider.cmdline = "quiet rw"
            with mock.patch("providers.limine.run_target"):
                with self.assertRaises(boot_base.BootloaduError):
                    provider.verify()
                directory = esp / machine_id / "linux"
                directory.mkdir(parents=True)
                (directory / "vmlinuz").write_bytes(b"kernel")
                (directory / "initramfs").write_bytes(b"initramfs")
                provider.verify()

    def test_grub_verify_requires_linux_menuentry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "boot/grub/grub.cfg"
            config.parent.mkdir(parents=True)
            context = types.SimpleNamespace(target_path=lambda path: root / str(path).lstrip("/"))
            provider = GrubProvider.__new__(GrubProvider)
            provider.context = context
            config.write_text("# header only\n", encoding="utf-8")
            with self.assertRaises(boot_base.BootloaduError):
                provider.verify()
            config.write_text(
                "menuentry 'CatOS' {\n\tlinux\t/vmlinuz-linux quiet\n\tinitrd\t/initramfs-linux.img\n}\n",
                encoding="utf-8",
            )
            provider.verify()

    def test_systemd_boot_verify_requires_normal_kernel_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            esp = root / "boot/efi"
            entries = esp / "loader/entries"
            entries.mkdir(parents=True)
            (esp / "loader/loader.conf").write_text("default @saved\n", encoding="utf-8")
            machine_id = "0123456789abcdef0123456789abcdef"
            (root / "etc").mkdir()
            (root / "etc/machine-id").write_text(machine_id + "\n", encoding="utf-8")
            modules = root / "usr/lib/modules/6.18.0"
            modules.mkdir(parents=True)
            (modules / "pkgbase").write_text("linux\n", encoding="utf-8")
            (modules / "vmlinuz").write_bytes(b"kernel")
            context = types.SimpleNamespace(
                esp_path=esp,
                esp_mount_point="/boot/efi",
                target_path=lambda path: root / str(path).lstrip("/"),
            )
            provider = SystemdBootProvider.__new__(SystemdBootProvider)
            provider.context = context
            provider.profile = {}
            provider.cmdline = "quiet rw"
            (entries / "snapshot.conf").write_text("title Snapshot\n", encoding="utf-8")
            with mock.patch("providers.systemd_boot.run_target"):
                with self.assertRaises(boot_base.BootloaduError):
                    provider.verify()
                directory = esp / machine_id / "6.18.0"
                directory.mkdir(parents=True)
                (directory / "linux").write_bytes(b"kernel")
                (directory / "initrd").write_bytes(b"initramfs")
                (entries / f"{machine_id}-6.18.0.conf").write_text(
                    "title CatOS\nversion 6.18.0\nlinux /%s/6.18.0/linux\ninitrd /%s/6.18.0/initrd\n"
                    % (machine_id, machine_id),
                    encoding="utf-8",
                )
                provider.verify()

    def test_systemd_boot_persists_boot_root_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = types.SimpleNamespace(
                root=root,
                esp_mount_point="/boot/efi",
                target_path=lambda path: root / str(path).lstrip("/"),
            )
            kernel = boot_base.Kernel("6.18.0", "linux", "/usr/lib/modules/6.18.0/vmlinuz", "/boot/initramfs-linux.img")
            with mock.patch.object(boot_base, "kernel_cmdline", return_value="quiet rw"), \
                 mock.patch.object(systemd_boot_provider, "discover_kernels", return_value=[kernel]), \
                 mock.patch.object(systemd_boot_provider, "run_target"):
                SystemdBootProvider(context, {}).execute()
            config = (root / "etc/kernel/install.conf.d/20-catos-bootloadu.conf").read_text(encoding="utf-8")
            self.assertIn("BOOT_ROOT=/boot/efi", config)
            self.assertIn("layout=bls", config)

    def test_firmware_provider_uses_plain_branding_label(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = types.SimpleNamespace(
                root=root,
                branding_name="CatOS",
                partitions=[
                    {"mountPoint": "/", "fs": "ext4", "device": "/dev/nvme0n1p2"},
                    {"mountPoint": "/boot/efi", "fs": "fat32", "device": "/dev/nvme0n1p1"},
                ],
                esp_mount_point="/boot/efi",
                target_path=lambda path: root / str(path).lstrip("/"),
            )
            provider = FirmwareProvider.__new__(FirmwareProvider)
            provider.context = context
            provider.profile = {}
            provider.cmdline = "quiet rw"
            provider.method = "uki"
            with mock.patch("providers.firmware.run_target"):
                provider.execute()
            config = (root / "etc/catos/firmware-boot.conf").read_text(encoding="utf-8")
            self.assertIn("label_prefix = CatOS", config)
            self.assertNotIn("machine_id", config)

    def test_setup_snapper_registers_pre_mounted_snapshot_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            template = root / "etc/snapper/config-templates/catos-root"
            template.parent.mkdir(parents=True)
            template.write_text('SUBVOLUME="/"\nFSTYPE="btrfs"\n', encoding="utf-8")
            snapshots = root / ".snapshots"
            snapshots.mkdir()
            conf_d = root / "etc/conf.d/snapper"
            conf_d.parent.mkdir(parents=True)
            conf_d.write_text('SNAPPER_CONFIGS="existing"\n', encoding="utf-8")
            context = types.SimpleNamespace(
                target_path=lambda path: root / str(path).lstrip("/"),
            )

            with mock.patch.object(boot_base, "run_target") as run_target:
                boot_base.setup_snapper(context)

            config = root / "etc/snapper/configs/root"
            self.assertEqual(config.read_text(encoding="utf-8"), template.read_text(encoding="utf-8"))
            self.assertEqual(conf_d.read_text(encoding="utf-8"), 'SNAPPER_CONFIGS="existing root"\n')
            self.assertEqual(snapshots.stat().st_mode & 0o777, 0o750)
            commands = [call.args[0] for call in run_target.call_args_list]
            self.assertNotIn(
                ["snapper", "--no-dbus", "-c", "root", "create-config", "--template", "catos-root", "/"],
                commands,
            )
            self.assertIn(["snapper", "--no-dbus", "-c", "root", "get-config"], commands)
            self.assertIn(["systemctl", "enable", "snapper-cleanup.timer"], commands)

    def test_partition_fact_helpers(self):
        partitions = [
            {"mountPoint": "/", "fs": "btrfs", "device": "/dev/nvme0n1p2"},
            {"mountPoint": "/boot/efi", "fs": "fat32", "device": "/dev/nvme0n1p1"},
        ]
        self.assertEqual(root_partition(partitions)["fs"], "btrfs")
        self.assertEqual(efi_device(partitions, "/boot/efi"), ("/dev/nvme0n1", 1))
        with self.assertRaises(ContextError):
            root_partition(partitions[1:])

if __name__ == "__main__":
    unittest.main()
