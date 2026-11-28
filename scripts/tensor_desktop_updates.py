"""Tell Desktop users about newer releases, without ever installing one."""

import http.client
import json
import os
from pathlib import Path
import re
import time
from urllib import request

from inkly import __version__
import tensor_desktop_preferences as preferences
from tensor_desktop_setup import REPOSITORY

SLUG = REPOSITORY.removeprefix("https://github.com/").removesuffix(".git")
API_URL = f"https://api.github.com/repos/{SLUG}/releases?per_page=30"
RELEASES_URL = f"https://github.com/{SLUG}/releases"
CACHE = "desktop-updates.json"
PREFERENCE = "check_updates"
INTERVAL_SECONDS = 24 * 60 * 60
MAX_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 5
VERSION = re.compile(r"(?:0|[1-9][0-9]{0,8})(?:\.(?:0|[1-9][0-9]{0,8})){2}")


def version_key(version):
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        return None
    return tuple(int(part) for part in version.split("."))


def release_info(tag):
    """Only a Desktop version tag can produce a link; never trust a supplied URL."""
    if not isinstance(tag, str) or not tag.startswith("desktop-v"):
        return None
    version = tag[len("desktop-v") :]
    if version_key(version) is None:
        return None
    return {"tag": tag, "version": version, "url": f"{RELEASES_URL}/tag/{tag}"}


def newest(releases):
    """Highest version among published Desktop releases, including previews."""
    if not isinstance(releases, list):
        raise ValueError("Expected a list of releases")
    candidates = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") is not False:
            continue
        info = release_info(release.get("tag_name"))
        if info:
            candidates.append(info)
    return max(candidates, key=lambda info: version_key(info["version"]), default=None)


def fetch_latest():
    req = request.Request(
        API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"InklyDesktop/{__version__}",
        },
    )
    with request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("GitHub release response is too large")
    return newest(json.loads(data))


def enabled():
    """An environment override also disables checks for managed or offline use."""
    return os.environ.get("INKLY_DESKTOP_UPDATE_CHECK") != "0"


def newer(release, current):
    installed = version_key(current)
    available = version_key(release["version"]) if release else None
    return (
        release
        if installed is not None and available is not None and available > installed
        else None
    )


def recent(checked_at, now):
    if not isinstance(checked_at, (int, float)) or isinstance(checked_at, bool):
        return False
    try:
        return 0 <= now - checked_at < INTERVAL_SECONDS
    except OverflowError:
        return False


def check(directory, current=__version__):
    """Check at most once per day; failures do not interrupt normal Desktop use."""
    if not enabled() or not preferences.load(directory, PREFERENCE):
        return None
    path = Path(directory) / CACHE
    cache = preferences.read_json(path)
    # Store only a checked version tag, not a potentially edited external URL.
    latest = release_info(cache.get("latest"))
    now = time.time()
    if recent(cache.get("checked_at"), now):
        return newer(latest, current)
    try:
        latest = fetch_latest()
    except (OSError, ValueError, http.client.HTTPException):
        pass  # Keep the last confirmed answer, if any; no offline error dialog.
    try:
        preferences.write_json(
            path, {"checked_at": now, "latest": latest["tag"] if latest else None}
        )
    except OSError:
        pass  # A read-only settings directory must not prevent a notice.
    return newer(latest, current)
