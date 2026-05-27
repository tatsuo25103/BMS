from __future__ import annotations

from dataclasses import dataclass
import json
import re
import socket
import urllib.error
import urllib.request


APP_VERSION = "1.0.0"
GITHUB_REPOSITORY = "tatsuo25103/BMS"
GITHUB_RELEASES_URL = f"https://github.com/{GITHUB_REPOSITORY}/releases"
UPDATE_CHECK_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class LatestVersionInfo:
    version: str
    download_url: str


def is_newer_version(latest: str, current: str = APP_VERSION) -> bool:
    latest_parts = _version_parts(latest)
    current_parts = _version_parts(current)
    if not latest_parts or not current_parts:
        return False
    size = max(len(latest_parts), len(current_parts))
    latest_parts += (0,) * (size - len(latest_parts))
    current_parts += (0,) * (size - len(current_parts))
    return latest_parts > current_parts


def fetch_latest_github_version(timeout: float = UPDATE_CHECK_TIMEOUT_SECONDS) -> LatestVersionInfo | None:
    latest_release_url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
    try:
        data = _github_json(latest_release_url, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            return None
    except (OSError, TimeoutError, urllib.error.URLError, socket.timeout):
        # 客戶端離線、DNS 失敗或網路逾時時，安靜略過更新檢查。
        return None
    else:
        version = str(data.get("tag_name") or data.get("name") or "").strip()
        if version:
            return LatestVersionInfo(
                version=version,
                download_url=str(data.get("html_url") or GITHUB_RELEASES_URL),
            )

    tags_url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/tags?per_page=1"
    try:
        data = _github_json(tags_url, timeout)
    except (OSError, TimeoutError, urllib.error.URLError, socket.timeout):
        # Release 不存在時才會查 tag；離線狀態不提醒、不阻塞。
        return None
    if isinstance(data, list) and data:
        version = str(data[0].get("name") or "").strip()
        if version:
            return LatestVersionInfo(version=version, download_url=f"https://github.com/{GITHUB_REPOSITORY}/tags")
    return None


def _github_json(url: str, timeout: float) -> object:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"BMSDataCollector/{APP_VERSION}",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _version_parts(value: str) -> tuple[int, ...]:
    match = re.search(r"(\d+(?:\.\d+)*)", value)
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))
