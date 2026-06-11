from __future__ import annotations

import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = PROJECT_ROOT / "packaging" / "installer.py"
SPEC = importlib.util.spec_from_file_location("bms_installer", INSTALLER_PATH)
assert SPEC is not None and SPEC.loader is not None
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def create_payload_zip(path: Path, *, prefix: str = "", version: str = "1.0.1") -> None:
    entries = {
        "python/pythonw.exe": b"pythonw",
        "app/main.py": b"print('main')",
        "app/app_version.py": f'APP_VERSION = "{version}"\n'.encode("ascii"),
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(f"{prefix}{name}", data)


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="bms-installer-tests-")
        self.root = Path(self.temp_dir.name)
        self.install_dir = self.root / "Programs" / "BMSDataCollector"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def install_zip(self, zip_path: Path) -> None:
        installer.install_portable(
            zip_path,
            self.install_dir,
            stop_running=False,
            create_links=False,
        )

    def test_fresh_install_with_direct_payload(self) -> None:
        zip_path = self.root / "direct.zip"
        create_payload_zip(zip_path)
        self.install_zip(zip_path)
        self.assertTrue((self.install_dir / "python/pythonw.exe").is_file())
        self.assertTrue((self.install_dir / "app/main.py").is_file())

    def test_fresh_install_with_nested_payload(self) -> None:
        zip_path = self.root / "nested.zip"
        create_payload_zip(zip_path, prefix="outer/release/BMSDataCollector/")
        self.install_zip(zip_path)
        self.assertTrue((self.install_dir / "app/app_version.py").is_file())
        self.assertFalse((self.install_dir / "outer").exists())

    def test_update_replaces_old_install(self) -> None:
        old_zip = self.root / "old.zip"
        new_zip = self.root / "new.zip"
        create_payload_zip(old_zip, version="1.0.0")
        create_payload_zip(new_zip, prefix="BMSDataCollector/", version="1.0.1")
        self.install_zip(old_zip)
        (self.install_dir / "old-only.txt").write_text("old", encoding="ascii")

        self.install_zip(new_zip)

        version_text = (self.install_dir / "app/app_version.py").read_text(encoding="ascii")
        self.assertIn('"1.0.1"', version_text)
        self.assertFalse((self.install_dir / "old-only.txt").exists())

    def test_invalid_update_keeps_old_install(self) -> None:
        old_zip = self.root / "old.zip"
        invalid_zip = self.root / "invalid.zip"
        create_payload_zip(old_zip, version="1.0.0")
        self.install_zip(old_zip)
        with zipfile.ZipFile(invalid_zip, "w") as archive:
            archive.writestr("app/main.py", b"broken")

        with self.assertRaises(FileNotFoundError):
            self.install_zip(invalid_zip)

        version_text = (self.install_dir / "app/app_version.py").read_text(encoding="ascii")
        self.assertIn('"1.0.0"', version_text)

    def test_post_install_failure_restores_old_install(self) -> None:
        old_zip = self.root / "old.zip"
        new_zip = self.root / "new.zip"
        create_payload_zip(old_zip, version="1.0.0")
        create_payload_zip(new_zip, version="1.0.1")
        self.install_zip(old_zip)

        with mock.patch.object(installer, "create_shortcuts", side_effect=RuntimeError("shortcut failed")):
            with self.assertRaises(RuntimeError):
                installer.install_portable(
                    new_zip,
                    self.install_dir,
                    stop_running=False,
                    create_links=True,
                )

        version_text = (self.install_dir / "app/app_version.py").read_text(encoding="ascii")
        self.assertIn('"1.0.0"', version_text)

    def test_zip_path_traversal_is_rejected(self) -> None:
        unsafe_zip = self.root / "unsafe.zip"
        with zipfile.ZipFile(unsafe_zip, "w") as archive:
            archive.writestr("../outside.txt", b"unsafe")

        with self.assertRaises(ValueError):
            self.install_zip(unsafe_zip)

        self.assertFalse((self.root / "outside.txt").exists())

    def test_process_stop_filter_does_not_match_command_line(self) -> None:
        script = installer.build_stop_process_script(self.install_dir, 12345)
        self.assertIn("$excludedProcessIds = @($PID, 12345)", script)
        self.assertIn("$_.ExecutablePath.StartsWith($root", script)
        self.assertNotIn("$_.CommandLine", script)


if __name__ == "__main__":
    unittest.main()
