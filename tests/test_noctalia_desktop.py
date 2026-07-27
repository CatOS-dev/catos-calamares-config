import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


def load_yaml(relative: str):
    return yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))


def exec_sequence(settings: dict) -> list[str]:
    return next(phase["exec"] for phase in settings["sequence"] if "exec" in phase)


class NoctaliaDesktopIntegrationTests(unittest.TestCase):
    def test_desktop_setup_is_one_ordered_calamares_job(self) -> None:
        settings = load_yaml("usr/share/calamares-advanced/settings.conf")
        jobs = exec_sequence(settings)
        instances = {
            f"{entry['module']}@{entry['id']}"
            for entry in settings.get("instances", [])
        }
        self.assertIn("contextualprocess@desktop-setup", instances)
        self.assertNotIn("shellprocess@dm-state", instances)
        self.assertNotIn("contextualprocess@dm-autologin", instances)
        self.assertNotIn("contextualprocess@desktop-dm", instances)
        self.assertNotIn("contextualprocess@wm-profile", instances)
        expected = [
            "pacman@default",
            "users",
            "paru@default",
            "displaymanager",
            "services-systemd",
            "contextualprocess@desktop-setup",
            "shellprocess@final",
        ]
        positions = [jobs.index(item) for item in expected]
        self.assertEqual(positions, sorted(positions))

    def test_noctalia_desktop_id_is_consistent_and_greeter_is_optional(self) -> None:
        chooser = load_yaml("usr/share/calamares-advanced/modules/packagechooser_desktop.conf")
        chooser_items = {item["id"]: item for item in chooser["items"]}
        self.assertIn("Niri-noctalia", chooser_items)
        screenshot = ROOT / chooser_items["Niri-noctalia"]["screenshot"].lstrip("/")
        self.assertTrue(screenshot.is_file())
        self.assertGreater(screenshot.stat().st_size, 100_000)

        netinstall = load_yaml("usr/share/calamares-advanced/modules/netinstall.yaml")
        desktop_group = next(group for group in netinstall if group["name"] == "Desktop Environments")
        desktop = next(item for item in desktop_group["subgroups"] if item["name"] == "Niri-noctalia")
        core = next(group for group in desktop["subgroups"] if group.get("critical", False))
        self.assertTrue(
            {"niri", "noctalia", "xwayland-satellite", "catos-niri-noctaliav5"}
            <= set(core["packages"])
        )
        self.assertNotIn("greetd", core["packages"])
        self.assertNotIn("noctalia-greeter", core["packages"])
        dm_group = next(
            group for group in desktop["subgroups"] if group["name"] == "Recommended display manager"
        )
        self.assertFalse(dm_group.get("critical", False))
        self.assertEqual(set(dm_group["packages"]), {"greetd", "noctalia-greeter"})

        setup = load_yaml("usr/share/calamares-advanced/modules/contextualprocess_desktop-setup.conf")
        autologin_command = setup["autoLoginUser"]["*"]["command"]
        self.assertTrue(autologin_command.startswith("-"))
        self.assertIn("--autologin-user", autologin_command)
        self.assertIn("${gs[autoLoginUser]}", autologin_command)
        mappings = setup["packagechooser_desktop"]
        self.assertEqual(set(mappings), {"", "*"})
        command = mappings["*"]["command"]
        self.assertTrue(command.startswith("-"))
        self.assertIn("configure-selected-desktop", command)
        self.assertIn("${gs[packagechooser_desktop]}", command)
        self.assertNotIn("${gs[autoLoginUser]}", command)

    def test_old_selector_is_fully_replaced(self) -> None:
        self.assertFalse((ROOT / "etc/calamares/scripts/dmcheck").exists())
        script = ROOT / "etc/calamares/scripts/configure-display-manager"
        self.assertTrue(script.is_file())
        self.assertTrue(script.stat().st_mode & 0o111)
        setup_script = ROOT / "etc/calamares/scripts/configure-selected-desktop"
        self.assertTrue(setup_script.is_file())
        self.assertTrue(setup_script.stat().st_mode & 0o111)

        pacstrap = load_yaml("usr/share/calamares-advanced/modules/pacstrap.conf")
        required_executables = set(pacstrap["requiredPostInstallExecutables"])
        optional_files = set(pacstrap["postInstallFiles"])
        optional_helpers = {
            "/etc/calamares/scripts/configure-display-manager",
            "/etc/calamares/scripts/configure-selected-desktop",
            "/etc/calamares/scripts/activate-catdot-profile",
            "/etc/calamares/scripts/customize_airootfs.sh",
            "/etc/calamares/scripts/customize_before.sh",
        }
        self.assertTrue(optional_helpers <= optional_files)
        self.assertTrue(optional_helpers.isdisjoint(required_executables))
        for path in required_executables:
            source = ROOT / path.lstrip("/")
            self.assertTrue(source.is_file(), path)
            self.assertTrue(source.stat().st_mode & 0o111, path)
        for path in optional_helpers:
            source = ROOT / path.lstrip("/")
            self.assertTrue(source.is_file(), path)
            self.assertTrue(source.stat().st_mode & 0o111, path)
        self.assertNotIn("/etc/calamares/scripts/dmcheck", pacstrap["postInstallFiles"])

        production_files = [
            ROOT / "usr/share/calamares-advanced/settings.conf",
            *list((ROOT / "usr/share/calamares-advanced/modules").glob("*.conf")),
            *list((ROOT / "etc/calamares/modules").glob("*.conf")),
        ]
        references = [
            path.relative_to(ROOT)
            for path in production_files
            if "dmcheck" in path.read_text(encoding="utf-8", errors="ignore")
        ]
        self.assertEqual(references, [])

    def test_profile_package_does_not_require_optional_dm(self) -> None:
        pkgbuild = ROOT.parent / "catos-niri-noctaliav5/PKGBUILD"
        text = pkgbuild.read_text(encoding="utf-8")
        self.assertNotIn("noctalia-greeter", text)
        self.assertNotIn("greetd", text)

    def test_desktop_setup_without_autologin_or_helpers_is_nonfatal(self) -> None:
        source = ROOT / "etc/calamares/scripts/configure-selected-desktop"
        with tempfile.TemporaryDirectory() as temporary_directory:
            script = Path(temporary_directory) / source.name
            shutil.copy2(source, script)
            result = subprocess.run(
                [str(script), "Niri-noctalia", "test-user"],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("missing display-manager helper", result.stderr)
        self.assertIn("missing Catdot helper", result.stderr)

    def test_desktop_setup_without_autologin_preserves_cached_dm_state(self) -> None:
        source = ROOT / "etc/calamares/scripts/configure-selected-desktop"
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            script = directory / source.name
            helper = directory / "configure-display-manager"
            arguments = directory / "arguments"
            shutil.copy2(source, script)
            helper.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$@" > "$DM_ARGUMENTS"\n',
                encoding="utf-8",
            )
            helper.chmod(0o755)
            result = subprocess.run(
                [str(script), "Niri-dms", "test-user"],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "DM_ARGUMENTS": str(arguments)},
            )
            captured_arguments = arguments.read_text(encoding="utf-8").splitlines()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(captured_arguments[:2], ["--autologin-session", "niri"])
        self.assertNotIn("--autologin-user", captured_arguments)


if __name__ == "__main__":
    unittest.main()
