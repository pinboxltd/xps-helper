"""Check GitHub for a newer release of XPS Helper.

Deliberately conservative. This module only *checks* and *reports*; it never
downloads or replaces the running executable on its own. Replacing a running
Windows binary is the kind of thing that half-succeeds and leaves a corrupt app,
so the download-and-install step is handed to the user: the release asset is a
zip, and the release page is opened for them.

The repository is public, so no credentials are involved.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from . import constants as C

log = logging.getLogger(__name__)

#: Where releases are published.
RELEASES_API = "https://api.github.com/repos/pinboxltd/xps-helper/releases/latest"
RELEASES_PAGE = "https://github.com/pinboxltd/xps-helper/releases/latest"

#: The asset CI attaches to every release.
ASSET_NAME = "xps-helper-windows.zip"

#: Bounded so a hung network never wedges the caller.
TIMEOUT_SECONDS = 8


@dataclass(frozen=True)
class ReleaseInfo:
    """What a check found."""

    version: str          # e.g. "0.3.1", without the leading v
    tag: str              # e.g. "v0.3.1"
    url: str              # release page
    asset_url: str | None  # direct zip download, if the asset exists
    notes: str

    @property
    def is_newer(self) -> bool:
        return is_newer_than(self.version, C.APP_VERSION)


class UpdateCheckError(RuntimeError):
    """The check could not be completed."""


def _parse_version(text: str) -> tuple[int, ...]:
    """Turn "1.2.3" into (1, 2, 3), ignoring any suffix.

    Deliberately forgiving: a tag like ``v0.3.1-beta`` or ``0.3.1+build`` must not
    crash the check, and an unparsable tag should compare as older rather than
    claiming an update is available.
    """
    cleaned = str(text).strip().lstrip("vV")
    match = re.match(r"^(\d+(?:\.\d+)*)", cleaned)
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer_than(candidate: str, current: str) -> bool:
    """Whether ``candidate`` is a strictly newer version than ``current``."""
    new = _parse_version(candidate)
    old = _parse_version(current)
    if not new:
        return False
    if not old:
        return True
    # Pad so 0.4 and 0.4.0 compare equal instead of by length.
    width = max(len(new), len(old))
    new += (0,) * (width - len(new))
    old += (0,) * (width - len(old))
    return new > old


def check(timeout: int = TIMEOUT_SECONDS) -> ReleaseInfo | None:
    """Return the latest release if it is newer than this build, else None.

    Raises :class:`UpdateCheckError` when the network or the reply is unusable.
    A caller showing this in a UI must treat that as "could not check" rather
    than "up to date" -- the two are very different to a user.
    """
    release = _fetch_release(timeout=timeout)
    if release is None or not release.is_newer:
        return None
    return release


def _fetch_release(timeout: int = TIMEOUT_SECONDS) -> ReleaseInfo | None:
    request = urllib.request.Request(
        RELEASES_API,
        headers={
            "Accept": "application/vnd.github+json",
            # GitHub rejects requests without a User-Agent.
            "User-Agent": f"{C.APP_NAME}/{C.APP_VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URL
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 404 simply means no release has been published yet.
        if exc.code == 404:
            return None
        raise UpdateCheckError(f"GitHub returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateCheckError(f"could not reach GitHub: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise UpdateCheckError("GitHub returned something that was not JSON") from exc

    return _parse_release(payload)


def _parse_release(payload: dict[str, Any]) -> ReleaseInfo | None:
    tag = str(payload.get("tag_name") or "").strip()
    if not tag:
        return None

    asset_url = None
    for asset in payload.get("assets") or []:
        if str(asset.get("name") or "") == ASSET_NAME:
            asset_url = asset.get("browser_download_url") or None
            break

    return ReleaseInfo(
        version=tag.lstrip("vV"),
        tag=tag,
        url=str(payload.get("html_url") or RELEASES_PAGE),
        asset_url=asset_url,
        notes=str(payload.get("body") or ""),
    )
