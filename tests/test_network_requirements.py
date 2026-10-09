from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class InstallerNetworkRequirementsTests(unittest.TestCase):
    def test_welcome_does_not_require_connectivity(self) -> None:
        for config in (
            "etc/calamares/modules/welcome.conf",
            "usr/share/calamares-advanced/modules/welcome.conf",
        ):
            with self.subTest(config=config):
                requirements = yaml.safe_load((ROOT / config).read_text())["requirements"]
                self.assertNotIn("internet", requirements["check"])
                self.assertNotIn("internet", requirements["required"])
                self.assertNotIn("internetCheckUrl", requirements)

    def test_advanced_package_lists_keep_remote_then_local_fallback(self) -> None:
        for filename in ("netinstall", "software@netinstall", "paru_extra@netinstall"):
            with self.subTest(filename=filename):
                path = ROOT / "usr/share/calamares-advanced/modules" / f"{filename}.conf"
                sources = yaml.safe_load(path.read_text())["groupsUrl"]
                self.assertEqual(len(sources), 3)
                self.assertTrue(all(url.startswith("https://") for url in sources[:2]))
                self.assertEqual(sources[-1], f"file:///usr/share/calamares-advanced/modules/{filename}.yaml")


if __name__ == "__main__":
    unittest.main()
