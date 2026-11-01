from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BrandingRuntimeTests(unittest.TestCase):
    def test_branding_slideshows_load_in_qml_runtime(self) -> None:
        qml = shutil.which("qml6") or shutil.which("qml")
        self.assertIsNotNone(qml, "Qt QML runtime is required for branding validation")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            module = root / "calamares/slideshow"
            module.mkdir(parents=True)
            (module / "qmldir").write_text(
                "module calamares.slideshow\n"
                "Presentation 1.0 Presentation.qml\n"
                "Slide 1.0 Slide.qml\n",
                encoding="utf-8",
            )
            (module / "Presentation.qml").write_text(
                "import QtQuick 2.0\n"
                "Item { property bool activatedInCalamares: false; "
                "property int currentSlide: 0; function goToNextSlide() {} }\n",
                encoding="utf-8",
            )
            (module / "Slide.qml").write_text(
                "import QtQuick 2.0\nItem {}\n",
                encoding="utf-8",
            )
            environment = {
                **os.environ,
                "QT_QPA_PLATFORM": "offscreen",
                "QML2_IMPORT_PATH": str(root),
            }
            for base in ("etc/calamares", "usr/share/calamares-advanced"):
                slideshow = ROOT / base / "branding/default/show.qml"
                try:
                    result = subprocess.run(
                        [qml, "-I", str(root), str(slideshow)],
                        check=False,
                        capture_output=True,
                        text=True,
                        env=environment,
                        timeout=1,
                    )
                    stderr = result.stderr
                    self.assertEqual(result.returncode, 0, stderr)
                except subprocess.TimeoutExpired as error:
                    stderr = error.stderr or ""
                    if isinstance(stderr, bytes):
                        stderr = stderr.decode(errors="replace")
                self.assertNotIn("ReferenceError", stderr, base)
                self.assertNotIn("Unable to assign", stderr, base)
                self.assertNotIn("is not a type", stderr, base)



class ProcessGroupBehaviorTests(unittest.TestCase):
    def test_timeout_kills_child_processes(self) -> None:
        module_path = ROOT / "usr/lib/calamares/modules/paru/process.py"
        spec = importlib.util.spec_from_file_location("calamares_paru_process", module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as temporary:
            pidfile = Path(temporary) / "child.pid"
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
