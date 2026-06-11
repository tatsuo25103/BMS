from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path


APP_NAME = "BMSDataCollector"
DISPLAY_NAME = "BMS Data Collector"
EXE_NAME = "BMSDataCollector.exe"
PORTABLE_ZIP_NAME = "BMSDataCollector_Portable.zip"
REQUIRED_PAYLOAD_FILES = (
    Path("python/pythonw.exe"),
    Path("app/main.py"),
    Path("app/app_version.py"),
)


class InstallationCancelled(Exception):
    pass


def resource_path(name: str) -> Path:
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return bundle_root / name


def message_box(title: str, message: str, flags: int = 0x40) -> int:
    return int(ctypes.windll.user32.MessageBoxW(None, message, title, flags))


def run_powershell(command: str, *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        check=True,
        text=True,
        capture_output=capture_output,
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


def safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    destination_root = destination.resolve()
    for entry in archive.infolist():
        target = (destination / entry.filename).resolve()
        try:
            target.relative_to(destination_root)
        except ValueError as exc:
            raise ValueError(f"Unsafe path in portable payload: {entry.filename}") from exc
    archive.extractall(destination)


def validate_payload(payload_root: Path) -> None:
    missing = [str(path) for path in REQUIRED_PAYLOAD_FILES if not (payload_root / path).is_file()]
    if missing:
        raise FileNotFoundError(f"Portable runtime payload is incomplete: {', '.join(missing)}")


def find_payload_root(extracted_root: Path) -> Path:
    candidates: list[Path] = []
    roots = [extracted_root]
    roots.extend(path for path in extracted_root.rglob("*") if path.is_dir())
    for candidate in roots:
        if all((candidate / path).is_file() for path in REQUIRED_PAYLOAD_FILES):
            candidates.append(candidate)
    if not candidates:
        raise FileNotFoundError("Portable runtime payload is incomplete.")
    candidates.sort(key=lambda path: len(path.relative_to(extracted_root).parts))
    return candidates[0]


def remove_tree_with_retry(path: Path, *, attempts: int = 5) -> None:
    if not path.exists():
        return
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.3 * (attempt + 1))
    if last_error is not None:
        raise last_error


def stop_installed_processes(install_dir: Path) -> None:
    escaped = str(install_dir.resolve()).replace("'", "''")
    script = f"""
$root = '{escaped}'
$processes = Get-CimInstance Win32_Process | Where-Object {{
    ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) -or
    ($_.CommandLine -and $_.CommandLine.IndexOf($root, [StringComparison]::OrdinalIgnoreCase) -ge 0)
}}
foreach ($process in $processes) {{
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}}
"""
    run_powershell(script)
    time.sleep(0.5)


def install_portable(
    portable_zip: Path,
    install_dir: Path,
    *,
    stop_running: bool = True,
    create_links: bool = True,
) -> tuple[Path, list[str]]:
    parent = install_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging_container = Path(tempfile.mkdtemp(prefix=f".{APP_NAME}-staging-", dir=parent))
    backup_dir = parent / f".{APP_NAME}-backup"
    payload_root: Path | None = None
    old_install_moved = False

    try:
        with zipfile.ZipFile(portable_zip, "r") as archive:
            safe_extract(archive, staging_container)
        payload_root = find_payload_root(staging_container)
        validate_payload(payload_root)

        if stop_running and install_dir.exists():
            stop_installed_processes(install_dir)
        remove_tree_with_retry(backup_dir)

        if install_dir.exists():
            os.replace(install_dir, backup_dir)
            old_install_moved = True

        try:
            os.replace(payload_root, install_dir)
            payload_root = None
            validate_payload(install_dir)
            pythonw = install_dir / "python" / "pythonw.exe"
            main_script = install_dir / "app" / "main.py"
            if create_links:
                create_shortcuts(pythonw, f'"{main_script}"')
        except Exception:
            remove_tree_with_retry(install_dir)
            if old_install_moved and backup_dir.exists():
                os.replace(backup_dir, install_dir)
                old_install_moved = False
            raise

        remove_tree_with_retry(backup_dir)
        old_install_moved = False
        return pythonw, [str(pythonw), str(main_script)]
    finally:
        if old_install_moved and backup_dir.exists() and not install_dir.exists():
            os.replace(backup_dir, install_dir)
        remove_tree_with_retry(staging_container)


def install(*, confirm_update: bool = True) -> tuple[Path, list[str]]:
    install_dir = Path.home() / "AppData" / "Local" / "Programs" / APP_NAME
    portable_zip = resource_path(PORTABLE_ZIP_NAME)
    if portable_zip.exists():
        if install_dir.exists() and confirm_update:
            answer = message_box(
                f"{DISPLAY_NAME} Setup",
                "An existing installation was found.\n\n"
                "Continuing will close the running application and update it. "
                "CSV logs stored in Downloads will not be removed.\n\n"
                "Continue?",
                0x24,
            )
            if answer != 6:
                raise InstallationCancelled()
        return install_portable(portable_zip, install_dir)

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
        target, launch_command = install(confirm_update=not silent)
    except InstallationCancelled:
        return 0
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
