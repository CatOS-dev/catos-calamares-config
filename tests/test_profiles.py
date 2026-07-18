from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
import time
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


def load_yaml(relative: str):
    with (ROOT / relative).open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def exec_sequence(settings: dict) -> list[str]:
    for phase in settings["sequence"]:
        if "exec" in phase:
            return phase["exec"]
    raise AssertionError("settings has no exec phase")


class ProfileTests(unittest.TestCase):
    def test_advanced_job_order_and_instances(self):
        settings = load_yaml("usr/share/calamares-advanced/settings.conf")
        jobs = exec_sequence(settings)
        self.assertNotIn("zfs", jobs)
        self.assertNotIn("zfshostid", jobs)
        self.assertIn("pacstrap@default", jobs)
        self.assertIn("bootloadu", jobs)
        for legacy_job in ("shellprocess@grub", "grubcfg", "bootloader", "initcpiocfg", "initcpio"):
            self.assertNotIn(legacy_job, jobs)
        instances = {
            f"{item['module']}@{item['id']}"
            for item in settings.get("instances", [])
        }
        self.assertNotIn("shellprocess@grub", instances)
        self.assertLess(jobs.index("paru@default"), jobs.index("bootloadu"))
        self.assertLess(jobs.index("bootloadu"), jobs.index("services-systemd"))
        self.assertLess(jobs.index("services-systemd"), jobs.index("shellprocess@final"))
        self.assertLess(jobs.index("shellprocess@final"), jobs.index("preservefiles"))
        self.assertLess(jobs.index("preservefiles"), jobs.index("umount"))

    def test_offline_profile_does_not_offer_unavailable_snapshots(self):
        offline = load_yaml("etc/calamares/modules/partition.conf")
        advanced = load_yaml("usr/share/calamares-advanced/modules/partition.conf")
        self.assertFalse(offline["allowSnapshots"])
        self.assertNotEqual(advanced.get("allowSnapshots", True), False)

    def test_offline_job_order(self):
        settings = load_yaml("etc/calamares/settings.conf")
        jobs = exec_sequence(settings)
        self.assertNotIn("zfs", jobs)
        self.assertNotIn("zfshostid", jobs)
        self.assertIn("bootloadu", jobs)
        for legacy_job in ("shellprocess@grub", "grubcfg", "bootloader", "initcpiocfg", "initcpio"):
            self.assertNotIn(legacy_job, jobs)
        self.assertLess(jobs.index("packages"), jobs.index("bootloadu"))
        self.assertLess(jobs.index("bootloadu"), jobs.index("services-systemd"))
        self.assertLess(jobs.index("preservefiles"), jobs.index("umount"))

    def test_domestic_https_connectivity_checks(self):
        for relative in (
            "etc/calamares/modules/welcome.conf",
            "usr/share/calamares-advanced/modules/welcome.conf",
        ):
            urls = load_yaml(relative)["requirements"]["internetCheckUrl"]
            self.assertIsInstance(urls, list)
            self.assertGreaterEqual(len(urls), 2)
            self.assertTrue(all(url.startswith("https://") for url in urls))
            self.assertTrue(all("example.com" not in url for url in urls))

    def test_install_state_is_private(self):
        for base in ("etc/calamares", "usr/share/calamares-advanced"):
            preserve = load_yaml(f"{base}/modules/preservefiles.conf")
            destinations = {item["from"]: item for item in preserve["files"]}
            self.assertEqual(destinations["log"]["dest"], "/home/${USER}/installation.log")
            self.assertEqual(destinations["config"]["dest"], "/home/${USER}/installation-state.json")
            self.assertEqual(destinations["log"]["perm"], "root:root:0600")
            self.assertEqual(destinations["config"]["perm"], "root:root:0600")

    def test_zfs_is_not_offered_by_profiles(self):
        for relative in (
            "etc/calamares/modules/partition.conf",
            "usr/share/calamares-advanced/modules/partition.conf",
        ):
            config = load_yaml(relative)
            self.assertNotIn("zfs", config.get("availableFileSystemTypes", []))
            self.assertNotIn("allowZfsEncryption", config)
            for override in config.get("bootloaderOverrides", []):
                self.assertNotIn("zfs", override.get("availableFileSystemTypes", []))
                self.assertNotIn("allowZfsEncryption", override)

        services = load_yaml("usr/share/calamares-advanced/modules/services-systemd.conf")
        unit_names = {unit["name"] for unit in services["units"]}
        self.assertFalse(any("zfs" in name.lower() for name in unit_names))
        self.assertNotIn("multi-user.target", unit_names)
        self.assertFalse({"firewalld", "ufw"}.issubset(unit_names))

    def test_default_kernel_and_selected_bootloader_only(self):
        init_script = (ROOT / "etc/calamares/modules/shellprocess-init.conf").read_text(encoding="utf-8")
        self.assertIn("vmlinuz-linux ", init_script)
        self.assertNotIn("vmlinuz-linux-lts", init_script)

        pacstrap_conf = load_yaml("usr/share/calamares-advanced/modules/pacstrap.conf")
        self.assertNotIn("grub", pacstrap_conf["basePackages"])

        pacstrap_main = (ROOT / "usr/lib/calamares/modules/pacstrap/main.py").read_text(encoding="utf-8")
        self.assertNotIn("zfs-utils", pacstrap_main)
        registry = load_yaml("usr/share/calamares/catos/bootloaders.yaml")
        for provider in registry["providers"].values():
            self.assertEqual(provider["kernelPackages"], ["linux", "linux-headers"])

    def test_unified_bootloader_assets_and_install_marker(self):
        registry = load_yaml("usr/share/calamares/catos/bootloaders.yaml")
        self.assertEqual(
            set(registry["providers"]),
            {"grub", "limine", "systemd-boot", "uki", "efistub"},
        )
        self.assertEqual(registry["installMarker"], "/run/calamares/bootloadu-installing")

        for relative in (
            "etc/calamares/modules/partition.conf",
            "usr/share/calamares-advanced/modules/partition.conf",
        ):
            partition_config = load_yaml(relative)
            self.assertEqual(
                partition_config["bootloaderProfilesFile"],
                "/usr/share/calamares/catos/bootloaders.yaml",
            )

        module = ROOT / "usr/lib/calamares/modules/bootloadu"
        for relative in (
            "module.desc",
            "main.py",
            "context.py",
            "registry.py",
            "providers/base.py",
            "providers/grub.py",
            "providers/limine.py",
            "providers/systemd_boot.py",
            "providers/uki.py",
            "providers/efistub.py",
            "providers/firmware.py",
        ):
            self.assertTrue((module / relative).is_file(), relative)

        for relative in (
            "etc/calamares/modules/shellprocess-init.conf",
            "usr/share/calamares-advanced/modules/shellprocess-init.conf",
        ):
            init_config = load_yaml(relative)
            self.assertEqual(
                init_config["script"][:4],
                [
                    "mkdir -p /run/calamares",
                    "touch /run/calamares/bootloadu-installing",
                    "mkdir -p ${ROOT}/run/calamares",
                    "touch ${ROOT}/run/calamares/bootloadu-installing",
                ],
            )
            self.assertIn(
                "cp /etc/calamares/scripts/adjust_grub_theme_after.sh ${ROOT}/run/calamares/adjust_grub_theme_after.sh",
                init_config["script"],
            )

    def test_bootloader_registry_drives_pacstrap(self):
        registry = load_yaml("usr/share/calamares/catos/bootloaders.yaml")
        pacstrap = (ROOT / "usr/lib/calamares/modules/pacstrap/main.py").read_text(encoding="utf-8")
        self.assertIn("load_bootloader_registry", pacstrap)
        self.assertNotIn('if bootloader == "grub"', pacstrap)
        self.assertNotIn('elif bootloader == "limine"', pacstrap)

        pacstrap_config = load_yaml("usr/share/calamares-advanced/modules/pacstrap.conf")
        self.assertIn("mkinitcpio-openswap", pacstrap_config["basePackages"])

        software = load_yaml("usr/share/calamares-advanced/modules/software@netinstall.yaml")
        snapshot_groups = [group for group in software if group.get("name") == "Snapshot"]
        self.assertFalse(snapshot_groups)

        package_root = ROOT.parent.parent / "CatOS-PKGBUILD"
        for package in (
            "limine-btrfs",
            "sdboot-btrfs",
            "catos-systemd-boot-config",
            "catos-snapper-config",
            "catos-firmware-boot",
        ):
            self.assertTrue((package_root / package / "PKGBUILD").is_file(), package)

        for provider in ("uki", "efistub"):
            self.assertIn(
                "catos-firmware-boot",
                registry["providers"][provider]["packages"],
            )

        firmware_updater = package_root / "catos-firmware-boot/catos-firmware-boot-update"
        firmware_hook = package_root / "catos-firmware-boot/95-catos-firmware-boot.hook"
        self.assertIn("bootloadu-installing", firmware_updater.read_text(encoding="utf-8"))
        self.assertIn("catos-firmware-boot-update --hook", firmware_hook.read_text(encoding="utf-8"))

    def test_limine_deploy_hook_tracks_limine_tool(self):
        hook = (
            ROOT.parent / "limine-tool/packaging/arch/rootfs/usr/share/libalpm/hooks/80-limine-efi-deploy.hook"
        ).read_text(encoding="utf-8")
        self.assertIn("Target = limine-tool", hook)

    def test_branding_and_schema_ids(self):
        bootloader = load_yaml("etc/calamares/modules/bootloader.conf")
        self.assertEqual(bootloader["bootloaderEntryName"], "CatOS")

        schema_ids = []
        for relative in (
            "usr/lib/calamares/modules/pacstrap/pacstrap.schema.yaml",
            "usr/lib/calamares/modules/pacman/pacman.schema.yaml",
            "usr/lib/calamares/modules/paru/paru.schema.yaml",
        ):
            schema_ids.append(load_yaml(relative)["$id"])
        self.assertEqual(len(schema_ids), len(set(schema_ids)))
        pacstrap_schema = load_yaml("usr/lib/calamares/modules/pacstrap/pacstrap.schema.yaml")
        self.assertIn("sync_db", pacstrap_schema["properties"])

    def test_source_metadata_is_not_counted_as_packages(self):
        for relative in (
            "usr/lib/calamares/modules/pacman/main.py",
            "usr/lib/calamares/modules/paru/main.py",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            function = source[source.index("def run_operations"):]
            self.assertLess(function.index('if key == "source"'), function.index("package_list = subst_locale"))

    def test_paru_cleanup_is_defensive(self):
        paru = (ROOT / "usr/lib/calamares/modules/paru/main.py").read_text(encoding="utf-8")
        self.assertIn("/etc/sudoers.d/calamares-paru", paru)
        self.assertIn("chage -E 0 nobody", paru)
        self.assertIn("rm -rf /var/cache/paru_cache", paru)

        final = (ROOT / "usr/share/calamares-advanced/modules/shellprocess-final.conf").read_text(encoding="utf-8")
        self.assertIn("calamares-paru", final)
        self.assertIn("chage -E 0 nobody", final)

    def test_desktop_profiles_are_functionally_grouped(self):
        chooser = load_yaml("usr/share/calamares-advanced/modules/packagechooser_desktop.conf")
        netinstall = load_yaml("usr/share/calamares-advanced/modules/netinstall.yaml")

        chooser_ids = [item["id"] for item in chooser["items"] if item["id"]]
        desktop_group = next(group for group in netinstall if group["name"] == "Desktop Environments")
        visible_desktop_names = [
            desktop["name"] for desktop in desktop_group["subgroups"] if not desktop.get("hidden", False)
        ]
        ukui = next(desktop for desktop in desktop_group["subgroups"] if desktop["name"] == "UKUI")

        self.assertEqual(chooser_ids, visible_desktop_names)
        self.assertNotIn("Niri-dms", chooser_ids)
        self.assertNotIn("Hyprland-dms", chooser_ids)
        self.assertNotIn("UKUI", chooser_ids)
        for desktop_id in ("Deepin-Desktop", "Sway", "Labwc", "Wayfire"):
            self.assertIn(desktop_id, chooser_ids)
        self.assertTrue(ukui["hidden"])
        self.assertFalse(ukui["selected"])
        self.assertFalse(desktop_group["hidden"])
        self.assertFalse(desktop_group["selected"])

        for desktop in desktop_group["subgroups"]:
            self.assertNotIn("packages", desktop)
            self.assertGreaterEqual(len(desktop["subgroups"]), 3)
            critical_groups = [group for group in desktop["subgroups"] if group["critical"]]
            self.assertEqual(len(critical_groups), 1, desktop["name"])
            self.assertTrue(all(group["selected"] for group in desktop["subgroups"]))
            self.assertTrue(all(group.get("packages") for group in desktop["subgroups"]))

        expected_function_groups = {
            "Sway": {"Sway core", "Desktop services", "File integration", "Wayland tools", "CatOS customization"},
            "Labwc": {"Labwc core", "Desktop services", "File integration", "Wayland tools", "CatOS customization"},
            "Wayfire": {"Wayfire core", "Desktop services", "File integration", "Wayland tools", "CatOS customization"},
            "Deepin-Desktop": {"DDE core", "DDE integration", "DDE applications", "DDE appearance"},
        }
        desktops = {desktop["name"]: desktop for desktop in desktop_group["subgroups"]}
        for desktop_name, subgroup_names in expected_function_groups.items():
            self.assertEqual(
                {subgroup["name"] for subgroup in desktops[desktop_name]["subgroups"]},
                subgroup_names,
            )

    def test_desktop_package_cleanup(self):
        chooser = load_yaml("usr/share/calamares-advanced/modules/packagechooser_desktop.conf")
        netinstall = load_yaml("usr/share/calamares-advanced/modules/netinstall.yaml")

        for item in chooser["items"]:
            self.assertLessEqual(len(item["description"]), 80)
            self.assertIn("description[zh_CN]", item)
            screenshot = item["screenshot"]
            if screenshot.startswith("/"):
                self.assertTrue((ROOT / screenshot.lstrip("/")).is_file(), screenshot)

        all_packages = []
        for top_group in netinstall:
            if top_group["name"] == "Base-devel + Common packages":
                package_tools = next(
                    subgroup for subgroup in top_group["subgroups"] if subgroup["name"] == "packages management"
                )
                self.assertNotIn("octopi", package_tools["packages"])
            if top_group["name"] == "Desktop Environments":
                for desktop in top_group["subgroups"]:
                    for subgroup in desktop["subgroups"]:
                        all_packages.extend(subgroup["packages"])

        removed_packages = {
            "plasma-meta",
            "gnome",
            "gnome-appfolders-manager",
            "sassc",
            "metacity",
            "lightdm-gtk-greeter-settings",
            "xfce4-datetime-plugin",
            "baka-mplayer",
            "qt5-translations",
        }
        self.assertTrue(removed_packages.isdisjoint(all_packages))

    def test_display_manager_fallbacks_are_exact(self):
        dmcheck = (ROOT / "etc/calamares/scripts/dmcheck").read_text(encoding="utf-8")
        self.assertIn('pacman -Qq "$1"', dmcheck)
        self.assertIn("enable_display_manager ddm ddm.service", dmcheck)
        self.assertIn("multi-user.target.wants/ly@tty2.service", dmcheck)
        self.assertNotIn("pacman -Qs", dmcheck)
        self.assertEqual(dmcheck.count("plasmalogin.service"), 1)

    def test_process_group_timeout_kills_children(self):
        module_path = ROOT / "usr/lib/calamares/modules/paru/process.py"
        spec = importlib.util.spec_from_file_location("calamares_paru_process", module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as tmpdir:
            pidfile = Path(tmpdir) / "child.pid"
            command = ["bash", "-c", f"sleep 30 & echo $! > {pidfile}; wait"]
            start = time.monotonic()
            with self.assertRaises(module.ProcessTimeout):
                module.run_process_group(command, timeout=0.2, terminate_grace=0.2)
            self.assertLess(time.monotonic() - start, 3.0)

            child_pid = int(pidfile.read_text(encoding="utf-8"))
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)


if __name__ == "__main__":
    unittest.main()
