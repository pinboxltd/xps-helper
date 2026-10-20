"""Tests for the GitHub update check.

The check is deliberately read-only: it reports, and hands the user a URL. These
tests pin the version comparison, because "is 0.3.10 newer than 0.3.9" answered
by string comparison is exactly the sort of bug that ships.
"""

from __future__ import annotations

import json

import pytest

from xpshelper import constants as C
from xpshelper import updates


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "new,current,expected",
    [
        ("0.3.1", "0.3.0", True),
        ("0.3.0", "0.3.1", False),
        ("0.3.1", "0.3.1", False),
        ("0.4", "0.3.9", True),
        ("v1.0.0", "0.9.9", True),
        ("1.0.0", "1.0", False),      # equal once padded
        ("0.3.10", "0.3.9", True),    # string comparison would get this wrong
        ("0.3.1-beta", "0.3.1", False),
        ("garbage", "0.3.1", False),  # unparsable must not claim an update
        ("0.3.1", "", True),
    ],
)
def test_version_comparison(new, current, expected):
    assert updates.is_newer_than(new, current) is expected


def test_parsing_ignores_prefixes_and_suffixes():
    assert updates._parse_version("v0.3.1") == (0, 3, 1)
    assert updates._parse_version("0.3.1+build7") == (0, 3, 1)
    assert updates._parse_version("nonsense") == ()


# ---------------------------------------------------------------------------
# Release parsing
# ---------------------------------------------------------------------------
def _payload(tag="v9.9.9", asset=True, url="https://example.invalid/rel"):
    assets = []
    if asset:
        assets.append(
            {
                "name": updates.ASSET_NAME,
                "browser_download_url": "https://example.invalid/xps.zip",
            }
        )
    return {
        "tag_name": tag,
        "html_url": url,
        "body": "notes here",
        "assets": assets,
    }


def test_release_parsing_reads_the_asset():
    info = updates._parse_release(_payload())
    assert info is not None
    assert info.version == "9.9.9"
    assert info.tag == "v9.9.9"
    assert info.asset_url == "https://example.invalid/xps.zip"
    assert info.is_newer is True


def test_a_release_without_our_asset_still_reports():
    """The page link is still useful, so a missing asset is not a failure."""
    info = updates._parse_release(_payload(asset=False))
    assert info is not None
    assert info.asset_url is None
    assert info.url


def test_a_payload_without_a_tag_is_ignored():
    assert updates._parse_release({"assets": []}) is None
    assert updates._parse_release({"tag_name": "   "}) is None


def test_an_older_release_is_not_newer():
    info = updates._parse_release(_payload(tag="v0.0.1"))
    assert info is not None
    assert info.is_newer is False


# ---------------------------------------------------------------------------
# check()
# ---------------------------------------------------------------------------
def test_check_returns_none_when_already_latest(monkeypatch):
    monkeypatch.setattr(
        updates, "_fetch_release",
        lambda timeout=updates.TIMEOUT_SECONDS: updates._parse_release(
            _payload(tag=f"v{C.APP_VERSION}")
        ),
    )
    assert updates.check() is None


def test_check_returns_the_release_when_newer(monkeypatch):
    monkeypatch.setattr(
        updates, "_fetch_release",
        lambda timeout=updates.TIMEOUT_SECONDS: updates._parse_release(
            _payload(tag="v99.0.0")
        ),
    )
    found = updates.check()
    assert found is not None
    assert found.version == "99.0.0"


def test_check_returns_none_when_no_release_exists(monkeypatch):
    monkeypatch.setattr(
        updates, "_fetch_release", lambda timeout=updates.TIMEOUT_SECONDS: None
    )
    assert updates.check() is None


def test_network_failure_raises_rather_than_reporting_uptodate(monkeypatch):
    """'Could not check' must never be presented as 'up to date'."""
    import urllib.error

    def boom(*_a, **_k):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(updates.urllib.request, "urlopen", boom)
    with pytest.raises(updates.UpdateCheckError):
        updates.check()


def test_http_404_is_treated_as_no_release(monkeypatch):
    """A repository with no releases published yet is not an error."""
    import urllib.error

    def not_found(*_a, **_k):
        raise urllib.error.HTTPError(
            updates.RELEASES_API, 404, "Not Found", {}, None
        )

    monkeypatch.setattr(updates.urllib.request, "urlopen", not_found)
    assert updates._fetch_release() is None


def test_http_error_other_than_404_is_an_error(monkeypatch):
    import urllib.error

    def server_error(*_a, **_k):
        raise urllib.error.HTTPError(
            updates.RELEASES_API, 500, "Server Error", {}, None
        )

    monkeypatch.setattr(updates.urllib.request, "urlopen", server_error)
    with pytest.raises(updates.UpdateCheckError):
        updates._fetch_release()


def test_the_check_uses_the_public_api_and_sends_a_user_agent():
    """GitHub rejects requests without a User-Agent, so it must be set."""
    assert updates.RELEASES_API.startswith("https://api.github.com/")
    assert "pinboxltd/xps-helper" in updates.RELEASES_API


def test_the_checker_never_downloads_the_asset():
    """It reports a URL; installing is the user's decision.

    A self-replacing executable is exactly the kind of thing that leaves a
    half-written binary behind, so no download code belongs in this module.
    """
    import inspect

    source = inspect.getsource(updates)
    for forbidden in ("urlretrieve", "shutil.copy", "os.replace", "os.rename"):
        assert forbidden not in source, f"update check must not {forbidden}"


def test_the_json_serialises():
    info = updates._parse_release(_payload())
    assert json.dumps({"version": info.version, "url": info.url})
