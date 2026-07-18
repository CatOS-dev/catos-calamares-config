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
        self.assertNotIn("luksopenswaphookcfg", jobs)
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

    def test_gpg_daemons_are_stopped_before_umount(self):
        for base in ("etc/calamares", "usr/share/calamares-advanced"):
            final = load_yaml(f"{base}/modules/shellprocess-final.conf")
            commands = [
                item.get("command") if isinstance(item, dict) else item
                for item in final["script"]
            ]
            cleanup = 'gpgconf --homedir "$(pacman-conf GPGDir)" --kill all'
            self.assertIn(cleanup, commands, base)
            self.assertEqual(commands.index(cleanup), len(commands) - 2, base)

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
        self.assertNotIn("luksopenswaphookcfg", jobs)
        self.assertIn("bootloadu", jobs)
        for legacy_job in ("shellprocess@grub", "grubcfg", "bootloader", "initcpiocfg", "initcpio"):
            self.assertNotIn(legacy_job, jobs)
        self.assertLess(jobs.index("packages"), jobs.index("bootloadu"))
        self.assertLess(jobs.index("bootloadu"), jobs.index("services-systemd"))
        self.assertLess(jobs.index("preservefiles"), jobs.index("umount"))

    def test_install_state_is_private(self):
        for base in ("etc/calamares", "usr/share/calamares-advanced"):
            preserve = load_yaml(f"{base}/modules/preservefiles.conf")
            destinations = {item["from"]: item for item in preserve["files"]}
            self.assertEqual(destinations["log"]["dest"], "/home/${USER}/installation.log")
            self.assertEqual(destinations["config"]["dest"], "/home/${USER}/installation-state.json")
            self.assertEqual(destinations["log"]["perm"], "root:root:0600")
            self.assertEqual(destinations["config"]["perm"], "root:root:0600")

    def test_bootloader_registry_is_wired_into_profiles(self):
        registry_path = "/usr/share/calamares/catos/bootloaders.yaml"
        registry = load_yaml(registry_path.lstrip("/"))

        self.assertIn(registry["defaultProvider"], registry["providers"])
        self.assertTrue(registry["installMarker"].startswith("/run/calamares/"))
        for provider_name, provider in registry["providers"].items():
            self.assertTrue(provider.get("displayName"), provider_name)
            self.assertTrue(provider.get("platforms"), provider_name)
            self.assertTrue(provider.get("packages"), provider_name)
            self.assertTrue(provider.get("kernelPackages"), provider_name)

        for relative in (
            "etc/calamares/modules/partition.conf",
            "usr/share/calamares-advanced/modules/partition.conf",
        ):
            partition_config = load_yaml(relative)
            self.assertEqual(partition_config["bootloaderProfilesFile"], registry_path)

        module = ROOT / "usr/lib/calamares/modules/bootloadu"
        for relative in (
            "module.desc",
            "main.py",
            "context.py",
            "registry.py",
            "providers/base.py",
        ):
            self.assertTrue((module / relative).is_file(), relative)

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
        self.assertIn("requiredPackages", pacstrap_schema["properties"])

    def test_advanced_profile_generates_machine_id(self):
        config = load_yaml("usr/share/calamares-advanced/modules/machineid.conf")
        self.assertTrue(config["systemd"])
        self.assertEqual(config["systemd-style"], "uuid")
        self.assertTrue(config["dbus"])
        self.assertTrue(config["dbus-symlink"])

    def test_advanced_pacstrap_bootstraps_keys_before_install(self):
        config = load_yaml("usr/share/calamares-advanced/modules/pacstrap.conf")
        self.assertNotIn("catos-keyring", config["basePackages"])
        self.assertIn("chwd", config["requiredPackages"])
        self.assertIn("fish", config["requiredPackages"])
        self.assertNotIn("mkinitcpio-openswap", config["basePackages"])

        settings = load_yaml("usr/share/calamares-advanced/settings.conf")
        jobs = exec_sequence(settings)
        self.assertLess(jobs.index("shellprocess@init"), jobs.index("pacstrap@default"))
        init_config = load_yaml("usr/share/calamares-advanced/modules/shellprocess-init.conf")
        init_commands = [
            item.get("command") if isinstance(item, dict) else item
            for item in init_config["script"]
        ]
        self.assertNotIn("/usr/bin/pacman -Sy --noconfirm", init_commands)
        self.assertLess(jobs.index("pacstrap@default"), jobs.index("networkcfg"))
        self.assertLess(jobs.index("networkcfg"), jobs.index("shellprocess@before"))

        script = (ROOT / "etc/calamares/scripts/create-pacman-keyring").read_text(
            encoding="utf-8"
        )
        self.assertIn("47BCD014C8A99B55AADAEE58F57BDFADBFCF8A1E", script)
        self.assertIn("hkps://keyserver.ubuntu.com", script)
        self.assertIn("CCED9BE21E1173C61DC1C9407931B6D628C8D3BA", script)
        self.assertIn("ARCH4EDU_BOOTSTRAP_KEY", script)
        self.assertIn("B5971F2C5C10A9A08C60030F786C63F330D7CB92", script)
        self.assertIn("ARCHLINUXCN_BOOTSTRAP_KEY", script)
        self.assertNotIn("3A9917BF0DED5C13F69AC68FABEC0A1208037BE9", script)
        self.assertIn("pacman-key --populate archlinux || exit 1", script)
        self.assertIn("install_keyring_package archlinux-keyring || exit 1", script)
        self.assertIn("install_keyring_package archlinuxcn-keyring || exit 1", script)
        self.assertNotIn("SigLevel = Optional TrustAll", script)
        self.assertIn("trap cleanup_keyring_daemons EXIT", script)
        self.assertIn("gpgconf --homedir", script)

    def test_desktop_profiles_match_the_chooser(self):
        chooser = load_yaml("usr/share/calamares-advanced/modules/packagechooser_desktop.conf")
        netinstall = load_yaml("usr/share/calamares-advanced/modules/netinstall.yaml")

        chooser_ids = [item["id"] for item in chooser["items"] if item["id"]]
        desktop_group = next(group for group in netinstall if group["name"] == "Desktop Environments")
        visible_desktops = [
            desktop for desktop in desktop_group["subgroups"] if not desktop.get("hidden", False)
        ]
        self.assertEqual(chooser_ids, [desktop["name"] for desktop in visible_desktops])

        desktops = {desktop["name"]: desktop for desktop in visible_desktops}
        for desktop in visible_desktops:
            self.assertNotIn("packages", desktop)
            subgroups = desktop.get("subgroups", [])
            self.assertTrue(subgroups, desktop["name"])
            critical_groups = [group for group in subgroups if group.get("critical", False)]
            self.assertEqual(len(critical_groups), 1, desktop["name"])
            self.assertTrue(all(group.get("packages") for group in subgroups), desktop["name"])

        required_core_packages = {
            "Niri-dms": {"niri", "ly", "xwayland-satellite", "catos-niri-dms"},
            "Hyprland-dms": {"hyprland", "ly", "hyprlock", "hyprpicker", "catos-hyprland-dms"},
        }
        for desktop_name, packages in required_core_packages.items():
            self.assertIn(desktop_name, desktops)
            core = next(group for group in desktops[desktop_name]["subgroups"] if group.get("critical", False))
            self.assertTrue(packages.issubset(core["packages"]))

    def test_desktop_chooser_assets_exist(self):
        chooser = load_yaml("usr/share/calamares-advanced/modules/packagechooser_desktop.conf")

        ids = [item["id"] for item in chooser["items"]]
        self.assertEqual(len(ids), len(set(ids)))
        for item in chooser["items"]:
            self.assertTrue(item.get("name"))
            self.assertTrue(item.get("description"))
            screenshot = item.get("screenshot")
            self.assertTrue(screenshot)
            if screenshot.startswith("/"):
                self.assertTrue((ROOT / screenshot.lstrip("/")).is_file(), screenshot)

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
