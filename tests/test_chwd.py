import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "usr/lib/calamares/modules/chwd/main.py"


def load_module(root_mount_point: str):
    warnings = []
    libcalamares = types.ModuleType("libcalamares")
    utils = types.ModuleType("libcalamares.utils")
    utils.gettext_path = lambda: ""
    utils.gettext_languages = lambda: []
    utils.warning = warnings.append
    utils.debug = lambda _message: None
    libcalamares.utils = utils
    libcalamares.globalstorage = types.SimpleNamespace(value=lambda _key: root_mount_point)
    libcalamares.job = types.SimpleNamespace(setprogress=lambda _value: None)

    previous = {
        "libcalamares": sys.modules.get("libcalamares"),
        "libcalamares.utils": sys.modules.get("libcalamares.utils"),
    }
    sys.modules["libcalamares"] = libcalamares
    sys.modules["libcalamares.utils"] = utils
    try:
        spec = importlib.util.spec_from_file_location("catos_chwd", MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        for name, original in previous.items():
            if original is None:
                del sys.modules[name]
            else:
                sys.modules[name] = original
    return module, warnings


class ChwdTests(unittest.TestCase):
    def test_driver_configuration_failure_does_not_abort_installation(self):
        with tempfile.TemporaryDirectory() as root_mount_point:
            module, warnings = load_module(root_mount_point)
            with mock.patch.object(module, "run_in_host", side_effect=module.HostError("driver setup failed")):
                self.assertIsNone(module.run())

        self.assertEqual(
            warnings,
            ["chwd failed; continuing without automatic driver configuration: driver setup failed"],
        )

