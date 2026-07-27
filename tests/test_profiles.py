from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
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
        self.assertLess(jobs.index("shellprocess@final"), jobs.index("bootloadu@secureboot"))
        self.assertLess(jobs.index("bootloadu@secureboot"), jobs.index("preservefiles"))
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
        self.assertIn("bootloadu", jobs)
        for legacy_job in ("shellprocess@grub", "grubcfg", "bootloader", "initcpiocfg", "initcpio"):
            self.assertNotIn(legacy_job, jobs)
        self.assertLess(jobs.index("packages"), jobs.index("bootloadu"))
        self.assertLess(jobs.index("bootloadu"), jobs.index("services-systemd"))
        self.assertLess(jobs.index("shellprocess@final"), jobs.index("bootloadu@secureboot"))
        self.assertLess(jobs.index("bootloadu@secureboot"), jobs.index("preservefiles"))
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

    def test_bootloader_chooser_uses_global_storage_visibility_expressions(self):
        chooser = load_yaml("usr/share/calamares-advanced/modules/packagechooser_bootloader.conf")
        items = {item["id"]: item for item in chooser["items"]}

        self.assertNotIn("visibleWhen", items["grub"])
        for provider in ("limine", "systemd-boot", "uki", "efistub"):
            self.assertIn("firmwareType == 'efi'", items[provider].get("visibleWhen", []), provider)
        self.assertIn("secureboot.enabled != true", items["efistub"]["visibleWhen"])
        self.assertNotIn("secureboot.enabled != true", items["uki"]["visibleWhen"])

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

    def test_keyring_bootstrap_executes_expected_operations(self):
        script = ROOT / "etc/calamares/scripts/create-pacman-keyring"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            call_log = root / "calls.log"
            gpg_dir = root / "gnupg"
            mock = bin_dir / "mock-command"
            mock.write_text(
                "#!/bin/sh\n"
                "name=${0##*/}\n"
                "printf '%s %s\\n' \"$name\" \"$*\" >> \"$CALL_LOG\"\n"
                "if [ \"$name\" = pacman-conf ]; then printf '%s\\n' \"$MOCK_GPG_DIR\"; fi\n",
                encoding="utf-8",
            )
            mock.chmod(0o755)
            for command in ("pacman", "pacman-key", "pacman-conf", "gpgconf", "sleep"):
                (bin_dir / command).symlink_to(mock)
            result = subprocess.run(
                [str(script)],
                check=False,
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "CALL_LOG": str(call_log),
                    "MOCK_GPG_DIR": str(gpg_dir),
                },
            )
            calls = call_log.read_text(encoding="utf-8").splitlines()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(
            calls,
            [
                "pacman-conf GPGDir",
                "pacman-key --init",
                "pacman-key --populate archlinux",
                "pacman-key --keyserver hkps://keyserver.ubuntu.com --recv-keys 47BCD014C8A99B55AADAEE58F57BDFADBFCF8A1E",
                "pacman-key --lsign-key 47BCD014C8A99B55AADAEE58F57BDFADBFCF8A1E",
                "pacman-key --keyserver hkps://keyserver.ubuntu.com --recv-keys CCED9BE21E1173C61DC1C9407931B6D628C8D3BA",
                "pacman-key --lsign-key CCED9BE21E1173C61DC1C9407931B6D628C8D3BA",
                "pacman-key --keyserver hkps://keyserver.ubuntu.com --recv-keys B5971F2C5C10A9A08C60030F786C63F330D7CB92",
                "pacman-key --lsign-key B5971F2C5C10A9A08C60030F786C63F330D7CB92",
                "pacman -Sy --noconfirm --needed archlinux-keyring",
                "pacman -Sy --noconfirm --needed archlinuxcn-keyring",
                "pacman-key --populate",
                f"gpgconf --homedir {gpg_dir} --kill all",
            ],
        )

    def test_netinstall_sources_use_online_mirrors_with_local_fallback(self):
        configs = {
            "netinstall.conf": "netinstall.yaml",
            "software@netinstall.conf": "software@netinstall.yaml",
            "paru_extra@netinstall.conf": "paru_extra@netinstall.yaml",
        }

        for config_name, groups_name in configs.items():
            config = load_yaml(
                f"usr/share/calamares-advanced/modules/{config_name}"
            )
            self.assertEqual(
                config["groupsUrl"],
                [
                    f"https://repo.aromatic05.top/x86_64/netinstall/{groups_name}",
                    f"https://pkgs.catos.info/x86_64/netinstall/{groups_name}",
                    f"file:///usr/share/calamares-advanced/modules/{groups_name}",
                ],
                config_name,
            )

    def test_desktop_profiles_match_the_chooser(self):
        chooser = load_yaml("usr/share/calamares-advanced/modules/packagechooser_desktop.conf")
        netinstall = load_yaml("usr/share/calamares-advanced/modules/netinstall.yaml")

        chooser_ids = [item["id"] for item in chooser["items"] if item["id"]]
        desktop_group = next(group for group in netinstall if group["name"] == "Desktop Environments")
        self.assertFalse(desktop_group.get("selected", True))
        visible_desktops = [
            desktop for desktop in desktop_group["subgroups"] if not desktop.get("hidden", False)
        ]
        self.assertEqual(chooser_ids, [desktop["name"] for desktop in visible_desktops])

        desktops = {desktop["name"]: desktop for desktop in visible_desktops}
        for desktop in visible_desktops:
            self.assertFalse(desktop.get("selected", True), desktop["name"])
            self.assertNotIn("packages", desktop)
            subgroups = desktop.get("subgroups", [])
            self.assertTrue(subgroups, desktop["name"])
            self.assertFalse(
                any(group.get("selected", False) for group in subgroups),
                desktop["name"],
            )
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

        gnome_packages = {
            package
            for group in desktops["GNOME-Desktop"]["subgroups"]
            for package in group["packages"]
        }
        required_gnome_packages = {
            "baobab",
            "gnome-calendar",
            "gnome-characters",
            "gnome-clocks",
            "gnome-font-viewer",
            "gnome-logs",
            "gnome-remote-desktop",
            "gnome-shell-extensions",
            "gnome-software",
            "gnome-user-share",
            "gnome-weather",
            "grilo-plugins",
            "gst-thumbnailers",
            "gvfs-dnssd",
            "gvfs-goa",
            "gvfs-onedrive",
            "gvfs-wsdd",
            "malcontent",
            "rygel",
            "simple-scan",
            "snapshot",
        }
        self.assertTrue(
            required_gnome_packages.issubset(gnome_packages),
            sorted(required_gnome_packages - gnome_packages),
        )
        self.assertIn("ptyxis", gnome_packages)
        self.assertNotIn("gnome-console", gnome_packages)

        all_desktop_packages = {
            desktop_name: {
                package
                for group in desktop["subgroups"]
                for package in group["packages"]
            }
            for desktop_name, desktop in desktops.items()
        }
        for desktop_name, packages in all_desktop_packages.items():
            self.assertNotIn("catos-tela-icon-theme-blue", packages, desktop_name)

        for desktop_name in ("Niri-dms", "Hyprland-dms", "Sway", "Labwc", "Wayfire"):
            self.assertIn("tela-circle-icon-theme-all", all_desktop_packages[desktop_name])
            self.assertNotIn("tela-icon-theme-git", all_desktop_packages[desktop_name])

        self.assertIn("tela-icon-theme-git", all_desktop_packages["KDE-Desktop"])
        self.assertNotIn("tela-circle-icon-theme-all", all_desktop_packages["KDE-Desktop"])
        self.assertIn("catos-kwin-decoration", all_desktop_packages["KDE-Desktop"])
        self.assertNotIn(
            "kwin-decoration-sierra-breeze-enhanced-for-catos",
            all_desktop_packages["KDE-Desktop"],
        )

    def test_desktop_setup_runs_after_package_installation(self):
        settings = load_yaml("usr/share/calamares-advanced/settings.conf")
        jobs = exec_sequence(settings)
        instances = {
            f"{item['module']}@{item['id']}"
            for item in settings.get("instances", [])
        }
        self.assertIn("contextualprocess@desktop-setup", instances)
        self.assertLess(jobs.index("users"), jobs.index("contextualprocess@desktop-setup"))
        self.assertLess(jobs.index("pacman@default"), jobs.index("contextualprocess@desktop-setup"))
        self.assertLess(jobs.index("paru@default"), jobs.index("contextualprocess@desktop-setup"))
        self.assertLess(jobs.index("contextualprocess@desktop-setup"), jobs.index("shellprocess@final"))

        chooser = load_yaml("usr/share/calamares-advanced/modules/packagechooser_desktop.conf")
        self.assertEqual(chooser["method"], "netinstall-select")
        contextual = load_yaml("usr/share/calamares-advanced/modules/contextualprocess_desktop-setup.conf")
        self.assertFalse(contextual["dontChroot"])
        autologin_command = contextual["autoLoginUser"]["*"]["command"]
        self.assertTrue(autologin_command.startswith("-"))
        self.assertIn("--autologin-user", autologin_command)
        self.assertIn("${gs[autoLoginUser]}", autologin_command)
        mappings = contextual["packagechooser_desktop"]
        self.assertEqual(set(mappings), {"", "*"})
        command = mappings["*"]["command"]
        self.assertTrue(command.startswith("-"))
        self.assertIn("${USER}", command)
        self.assertNotIn("${gs[autoLoginUser]}", command)

        script_path = ROOT / "etc/calamares/scripts/activate-catdot-profile"
        self.assertTrue(script_path.stat().st_mode & 0o111)
        pacstrap = load_yaml("usr/share/calamares-advanced/modules/pacstrap.conf")
        self.assertIn(
            "/etc/calamares/scripts/activate-catdot-profile",
            pacstrap["postInstallFiles"],
        )
        self.assertNotIn(
            "/etc/calamares/scripts/activate-catdot-profile",
            pacstrap["requiredPostInstallExecutables"],
        )
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

    def test_cachyos_repository_is_online_profile_only(self):
        advanced = load_yaml("usr/share/calamares-advanced/settings.conf")
        offline = load_yaml("etc/calamares/settings.conf")

        advanced_instances = {
            f"{item['module']}@{item['id']}": item
            for item in advanced.get("instances", [])
        }
        self.assertIn("packagechooser@repository", advanced_instances)
        self.assertIn("contextualprocess@repository", advanced_instances)

        advanced_show = next(phase["show"] for phase in advanced["sequence"] if "show" in phase)
        advanced_exec = exec_sequence(advanced)
        self.assertIn("packagechooser@repository", advanced_show)
        self.assertLess(
            advanced_exec.index("shellprocess@init"),
            advanced_exec.index("contextualprocess@repository"),
        )
        self.assertLess(
            advanced_exec.index("contextualprocess@repository"),
            advanced_exec.index("pacstrap@default"),
        )

        chooser = load_yaml(
            "usr/share/calamares-advanced/modules/packagechooser_repository.conf"
        )
        self.assertEqual(chooser["method"], "legacy")
        self.assertEqual(chooser["default"], "catos")
        self.assertEqual([item["id"] for item in chooser["items"]], ["catos", "cachyos"])

        offline_instances = {
            f"{item['module']}@{item['id']}"
            for item in offline.get("instances", [])
        }
        self.assertNotIn("packagechooser@repository", offline_instances)
        self.assertNotIn("contextualprocess@repository", offline_instances)

    def test_cachyos_bootstrap_runs_unconditionally_before_package_jobs(self):
        bootstrap = "/etc/calamares/scripts/bootstrap-cachyos"

        advanced_before = load_yaml(
            "usr/share/calamares-advanced/modules/shellprocess-before.conf"
        )
        advanced_commands = [
            item.get("command") if isinstance(item, dict) else item
            for item in advanced_before["script"]
        ]
        self.assertIn(bootstrap, advanced_commands)
        self.assertLess(
            advanced_commands.index(bootstrap),
            advanced_commands.index("/etc/calamares/scripts/create-pacman-keyring"),
        )

        advanced_jobs = exec_sequence(
            load_yaml("usr/share/calamares-advanced/settings.conf")
        )
        self.assertLess(
            advanced_jobs.index("pacstrap@default"),
            advanced_jobs.index("shellprocess@before"),
        )
        self.assertLess(
            advanced_jobs.index("shellprocess@before"),
            advanced_jobs.index("pacman@default"),
        )

        pacstrap = load_yaml("usr/share/calamares-advanced/modules/pacstrap.conf")
        self.assertIn(bootstrap, pacstrap["requiredPostInstallExecutables"])

        repository_job = load_yaml(
            "usr/share/calamares-advanced/modules/contextualprocess_repository.conf"
        )
        self.assertEqual(
            repository_job["packagechooser_repository"]["cachyos"],
            "/usr/bin/python3 /etc/calamares/scripts/cachyos-repository.py",
        )

if __name__ == "__main__":
    unittest.main()
