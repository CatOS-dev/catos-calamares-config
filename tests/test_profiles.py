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
        self.assertIn("shellprocess@grub", jobs)
        instances = {
            f"{item['module']}@{item['id']}"
            for item in settings.get("instances", [])
        }
        self.assertIn("shellprocess@grub", instances)
        self.assertLess(jobs.index("paru@default"), jobs.index("shellprocess@final"))
        self.assertLess(jobs.index("shellprocess@final"), jobs.index("preservefiles"))
        self.assertLess(jobs.index("preservefiles"), jobs.index("umount"))

    def test_offline_job_order(self):
        settings = load_yaml("etc/calamares/settings.conf")
        jobs = exec_sequence(settings)
        self.assertNotIn("zfs", jobs)
        self.assertNotIn("zfshostid", jobs)
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
        self.assertIn('["linux", "linux-headers"]', pacstrap_main)

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
