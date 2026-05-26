from __future__ import annotations

import ctypes
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


APP_NAME = "BMSDataCollector"
DISPLAY_NAME = "BMS Data Collector"
EXE_NAME = "BMSDataCollector.exe"
PORTABLE_ZIP_NAME = "BMSDataCollector_Portable.zip"


def resource_path(name: str) -> Path:
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return bundle_root / name


def message_box(title: str, message: str, flags: int = 0x40) -> None:
    ctypes.windll.user32.MessageBoxW(None, message, title, flags)


def run_powershell(command: str) -> None:
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def create_shortcuts(target: Path, arguments: str = "") -> None:
    escaped_target = str(target).replace("'", "''")
    escaped_arguments = arguments.replace("'", "''")
    shortcut_script = f"""
$target = '{escaped_target}'
$arguments = '{escaped_arguments}'
$shell = New-Object -ComObject WScript.Shell
$desktopShortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) '{DISPLAY_NAME}.lnk'
$shortcut = $shell.CreateShortcut($desktopShortcut)
$shortcut.TargetPath = $target
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = Split-Path $target
$shortcut.Description = '{DISPLAY_NAME}'
$shortcut.Save()
$programFolder = Join-Path ([Environment]::GetFolderPath('Programs')) '{DISPLAY_NAME}'
New-Item -ItemType Directory -Force -Path $programFolder | Out-Null
$menuShortcut = Join-Path $programFolder '{DISPLAY_NAME}.lnk'
$shortcut = $shell.CreateShortcut($menuShortcut)
$shortcut.TargetPath = $target
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = Split-Path $target
$shortcut.Description = '{DISPLAY_NAME}'
$shortcut.Save()
"""
    run_powershell(shortcut_script)


def install() -> tuple[Path, list[str]]:
    install_dir = Path.home() / "AppData" / "Local" / "Programs" / APP_NAME
    portable_zip = resource_path(PORTABLE_ZIP_NAME)
    if portable_zip.exists():
        if install_dir.exists():
            shutil.rmtree(install_dir)
        install_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(portable_zip, "r") as archive:
            archive.extractall(install_dir)
        pythonw = install_dir / "python" / "pythonw.exe"
        main_script = install_dir / "app" / "main.py"
        if not pythonw.exists() or not main_script.exists():
            raise FileNotFoundError("Portable runtime payload is incomplete.")
        create_shortcuts(pythonw, f'"{main_script}"')
        return pythonw, [str(pythonw), str(main_script)]

    source = resource_path(EXE_NAME)
    if not source.exists():
        raise FileNotFoundError(f"Installer payload is missing: {source}")

    install_dir.mkdir(parents=True, exist_ok=True)
    target = install_dir / EXE_NAME
    shutil.copy2(source, target)
    create_shortcuts(target)
    return target, [str(target)]


def main() -> int:
    silent = any(arg.lower() in {"/s", "--silent"} for arg in sys.argv[1:])
    no_launch = any(arg.lower() in {"/nolaunch", "--no-launch"} for arg in sys.argv[1:])
    try:
        target, launch_command = install()
    except Exception as exc:
        if not silent:
            message_box(f"{DISPLAY_NAME} Setup", f"Installation failed:\n{exc}", 0x10)
        return 1

    if not silent:
        message_box(f"{DISPLAY_NAME} Setup", f"Installation complete:\n{target}")
    if not no_launch:
        subprocess.Popen(launch_command, cwd=str(Path(launch_command[0]).parent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
