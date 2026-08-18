"""Unit tests for version_bump.py.

HTTP is mocked via an injectable fake opener so tests are hermetic and fast.
Covers the semver heuristic, pagination, login failures, and the integrated
next_version flow.
"""

from __future__ import annotations

import json
import socket
import sys
import urllib.error

import pytest

sys.path.insert(0, "/mnt/disco2/INFORMATION_ANDRES_LOBATON/Proyectos-de-Codigo-2026/laurel-infra-manager")
from app.modules.integrations.docker import version_bump as vb  # noqa: E402


# ----------------------------------------------------------------------------
# Test doubles
# ----------------------------------------------------------------------------


class FakeResp:
    """Minimal http.client response: context-manager + .read() returning JSON bytes."""

    def __init__(self, data):
        self._data = json.dumps(data).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeOpener:
    """Sequential fake opener: returns queued responses for each urlopen() call.

    Each entry is either a dict (returned as JSON body) or an Exception instance
    (raised). Records every call for inspection.
    """

    def __init__(self, responses):
        self._queue = list(responses)
        self.calls = []  # list[(url, data_or_None, headers_dict)]

    def __call__(self, req, timeout=15):
        url = getattr(req, "full_url", None) or str(req)
        data = getattr(req, "data", None)
        raw_hdrs = getattr(req, "headers", None)
        # urllib headers come back as an email.message.Message (iterable of
        # (k, v) pairs). Normalize to a lowercase-key dict so tests can do
        # case-insensitive lookups regardless of how Message titlecases them.
        if raw_hdrs is not None and not isinstance(raw_hdrs, dict):
            raw_hdrs = dict(raw_hdrs)
        headers = {k.lower(): v for k, v in (raw_hdrs or {}).items()}
        self.calls.append((url, data, headers, timeout))
        if not self._queue:
            raise AssertionError(f"FakeOpener exhausted; unexpected call: {url}")
        item = self._queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        # If the item already implements the context-manager protocol
        # (custom test doubles), pass it through. Otherwise wrap the raw
        # payload (dict / str / bytes) in FakeResp.
        if hasattr(item, "__enter__") and hasattr(item, "__exit__"):
            return item
        return FakeResp(item)


def login_ok(token: str = "jwt-test") -> dict:
    return {"token": token}


def tags_page(results, next_url=None):
    return {"results": results, "next": next_url}


def tag(name):
    return {"name": name, "last_updated": "2026-01-01T00:00:00Z"}


# ----------------------------------------------------------------------------
# Semver parsing
# ----------------------------------------------------------------------------


class TestHighestSemver:
    def test_empty_returns_zero_zero_zero(self):
        assert vb._highest_semver([]) == (0, 0, 0)

    def test_single_v_prefix(self):
        assert vb._highest_semver([tag("v1.2.3")]) == (1, 2, 3)

    def test_single_no_prefix(self):
        assert vb._highest_semver([tag("0.0.1")]) == (0, 0, 1)

    def test_picks_max_patch(self):
        tags = [tag("0.0.1"), tag("0.0.4"), tag("0.0.2"), tag("0.0.4")]
        assert vb._highest_semver(tags) == (0, 0, 4)

    def test_picks_max_minor(self):
        tags = [tag("0.5.0"), tag("0.2.9"), tag("0.10.0"), tag("0.3.7")]
        # naive tuple comparison picks 0.10.0 (10 > 5); semver-correct would
        # treat 0.10 > 0.5. The current heuristic relies on Python tuple ordering
        # which is *wrong* for semver once a minor reaches 10. Document this
        # here so the behaviour is explicit and the test fails loudly if it
        # changes accidentally.
        assert vb._highest_semver(tags) == (0, 10, 0)

    def test_picks_max_major(self):
        tags = [tag("1.0.0"), tag("2.0.0"), tag("9.9.9"), tag("2.1.0")]
        assert vb._highest_semver(tags) == (9, 9, 9)

    def test_ignores_non_semver_tags(self):
        tags = [
            tag("latest"),
            tag("dd0997e3c2119"),
            tag("main"),
            tag("0.0.2"),
            tag("release-2026-01"),
            tag(""),  # empty string -> regex fails
        ]
        assert vb._highest_semver(tags) == (0, 0, 2)

    def test_accepts_strings_and_dicts(self):
        # Strings and dicts mixed; both shapes must work.
        tags = ["v0.0.1", tag("0.0.3"), "not-a-tag", tag("0.0.2")]
        assert vb._highest_semver(tags) == (0, 0, 3)

    def test_rejects_non_string_dict_values(self):
        tags = [{"name": 123}, {"name": None}, {"name": "v0.0.1"}]
        assert vb._highest_semver(tags) == (0, 0, 1)


# ----------------------------------------------------------------------------
# next_version (integration with the HTTP layer)
# ----------------------------------------------------------------------------


class TestNextVersion:
    def test_no_tags_returns_0_0_1(self):
        opener = FakeOpener([login_ok(), tags_page([])])
        assert vb.next_version("u", "p", repo="r", opener=opener) == "0.0.1"

    def test_single_tag_bumps_patch(self):
        opener = FakeOpener([login_ok("t"), tags_page([tag("v0.0.1")])])
        assert vb.next_version("u", "p", repo="r", opener=opener) == "0.0.2"

    def test_max_tag_wins_among_mixed(self):
        opener = FakeOpener([
            login_ok("t"),
            tags_page([tag("v0.0.1"), tag("latest"), tag("v0.2.7"), tag("0.5.0")]),
        ])
        assert vb.next_version("u", "p", repo="r", opener=opener) == "0.5.1"

    def test_follows_pagination(self):
        opener = FakeOpener([
            login_ok("t"),
            tags_page([tag("v0.0.1"), tag("v0.0.2")], next_url="https://hub.docker.com/v2/repositories/u/r/tags/?page=2"),
            tags_page([tag("v0.0.4"), tag("0.0.9")]),  # last page: no `next`
        ])
        assert vb.next_version("u", "p", repo="r", opener=opener) == "0.0.10"

    def test_pagination_more_than_two_pages(self):
        # `next` URLs are resolved against the current page (urljoin), so the
        # relative strings below become absolute and the fetcher keeps going.
        opener = FakeOpener([
            login_ok("t"),
            tags_page([tag("v0.0.1")], next_url="?page=2"),
            tags_page([tag("v0.0.2")], next_url="?page=3"),
            tags_page([tag("v0.0.3")]),
        ])
        assert vb.next_version("u", "p", repo="r", opener=opener) == "0.0.4"

    def test_stops_when_next_is_none(self):
        # JSON `null` and empty string both end pagination.
        opener = FakeOpener([
            login_ok("t"),
            tags_page([tag("v0.0.1")], next_url=None),
        ])
        assert vb.next_version("u", "p", repo="r", opener=opener) == "0.0.2"

    def test_stops_when_next_is_literal_null_string(self):
        # Some middlewares serialize null as the literal string "null"; treat
        # that defensively as end-of-pagination.
        opener = FakeOpener([
            login_ok("t"),
            tags_page([tag("v0.0.1")], next_url="null"),
            tags_page([tag("v9.9.9")]),  # must NOT be fetched
        ])
        assert vb.next_version("u", "p", repo="r", opener=opener) == "0.0.2"
        # Only login + first page should have been called.
        assert len(opener.calls) == 2

    def test_stops_when_next_is_empty_string(self):
        opener = FakeOpener([
            login_ok("t"),
            tags_page([tag("v0.0.1")], next_url=""),
        ])
        assert vb.next_version("u", "p", repo="r", opener=opener) == "0.0.2"

    def test_resolves_relative_next_url(self):
        # The fetcher stores `next` as a relative URL on the first response.
        # The code should resolve it against the prior absolute URL.
        base = "https://hub.docker.com/v2/repositories/u/r/tags/?page_size=100"
        opener = FakeOpener([
            login_ok("t"),
            tags_page([tag("v0.0.1")], next_url="?page=2"),
            tags_page([tag("v0.0.3")]),
        ])
        vb.next_version("u", "p", repo="r", opener=opener)
        # calls: 0=login, 1=first tags page (url=base), 2=second tags page
        # (url=resolved via urljoin against the base).
        from urllib.parse import urljoin as _urljoin
        resolved_url = opener.calls[2][0]
        assert resolved_url == _urljoin(base, "?page=2")

    def test_login_http_error_raises(self):
        opener = FakeOpener([
            urllib.error.HTTPError(
                url="https://hub.docker.com/v2/users/login/",
                code=401,
                msg="Unauthorized",
                hdrs=None,
                fp=None,
            )
        ])
        with pytest.raises(vb.DockerHubError, match="login failed"):
            vb.next_version("u", "p", repo="r", opener=opener)

    def test_login_no_token_raises(self):
        opener = FakeOpener([{"detail": "no token for you"}])
        with pytest.raises(vb.DockerHubError, match="no token"):
            vb.next_version("u", "p", repo="r", opener=opener)

    def test_tags_http_error_raises(self):
        opener = FakeOpener([
            login_ok("t"),
            urllib.error.HTTPError(
                url="https://hub.docker.com/v2/repositories/u/r/tags/",
                code=503,
                msg="Service Unavailable",
                hdrs=None,
                fp=None,
            ),
        ])
        with pytest.raises(vb.DockerHubError, match="tags fetch failed"):
            vb.next_version("u", "p", repo="r", opener=opener)

    def test_login_network_error_wrapped(self):
        # socket.timeout propagates raw; should become DockerHubError instead.
        opener = FakeOpener([socket.timeout("read timed out")])
        with pytest.raises(vb.DockerHubError, match="network error"):
            vb.next_version("u", "p", repo="r", opener=opener)

    def test_login_invalid_json_wrapped(self):
        # Login responds with non-JSON body -> DockerHubError, not JSONDecodeError.
        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"<html>500</html>"
        opener = FakeOpener([_Resp()])
        with pytest.raises(vb.DockerHubError, match="invalid JSON"):
            vb.next_version("u", "p", repo="r", opener=opener)

    def test_tags_invalid_json_wrapped(self):
        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"not json"
        opener = FakeOpener([login_ok("t"), _Resp()])
        with pytest.raises(vb.DockerHubError, match="tags fetch.*invalid JSON"):
            vb.next_version("u", "p", repo="r", opener=opener)

    def test_login_non_dict_response_wrapped(self):
        # Login returns a bare JSON string like `"oops"` — valid JSON but
        # no dict, hence no token. Should surface as DockerHubError.
        opener = FakeOpener(["oops"])
        with pytest.raises(vb.DockerHubError, match="no token"):
            vb.next_version("u", "p", repo="r", opener=opener)

    def test_next_as_non_string_bails_safely(self):
        # Defensive: if `next` is a dict (broken upstream), don't crash.
        opener = FakeOpener([
            login_ok("t"),
            tags_page([tag("v0.0.5")], next_url={"oops": "dict"}),
        ])
        assert vb.next_version("u", "p", repo="r", opener=opener) == "0.0.6"

    def test_login_token_must_be_string(self):
        # Token field present but not a string -> DockerHubError.
        opener = FakeOpener([{"token": 12345}, tags_page([])])
        with pytest.raises(vb.DockerHubError, match="no token"):
            vb.next_version("u", "p", repo="r", opener=opener)

    def test_sends_login_json_body_with_password(self):
        # Login must POST {"username","password"} as JSON and not expose the
        # password in query string / URL.
        opener = FakeOpener([login_ok("t"), tags_page([])])
        vb.next_version("alice", "p4ss!", repo="r", opener=opener)
        login_url, login_data, login_headers, _ = opener.calls[0]
        assert login_url == vb.LOGIN_URL
        body = login_data.decode() if isinstance(login_data, bytes) else login_data
        assert "p4ss!" in body
        assert "p4ss!" not in login_url
        assert login_headers.get("content-type") == "application/json"

    def test_sends_jwt_authorization_on_each_page(self):
        opener = FakeOpener([
            login_ok("jwt-xyz"),
            tags_page([tag("v0.0.1")], next_url="?page=2"),
            tags_page([tag("v0.0.2")]),
        ])
        vb.next_version("u", "p", repo="r", opener=opener)
        # calls[0] is login; calls[1] + calls[2] are tag pages.
        for i in (1, 2):
            url, _data, headers, _ = opener.calls[i]
            assert "hub.docker.com/v2/repositories/u/r/tags/" in url
            assert headers.get("authorization") == "JWT jwt-xyz"

    def test_timeout_propagated(self):
        opener = FakeOpener([login_ok("t"), tags_page([])])
        vb.next_version("u", "p", repo="r", opener=opener, timeout=7.5)
        assert opener.calls[0][3] == 7.5
        assert opener.calls[1][3] == 7.5

    def test_empty_results_with_only_next_does_not_loop(self):
        # A page with no results but a `next` URL must keep iterating. This
        # guards against premature termination on empty pages.
        opener = FakeOpener([
            login_ok("t"),
            tags_page([], next_url="page-2"),
            tags_page([tag("v9.9.9")]),
        ])
        assert vb.next_version("u", "p", repo="r", opener=opener) == "9.9.10"


# ----------------------------------------------------------------------------
# CLI entry point
# ----------------------------------------------------------------------------


class TestCLI:
    def test_missing_credentials_exits_nonzero(self, monkeypatch, capsys):
        # Wipe env so the CLI can't find credentials, and pass argv without
        # credential positional args.
        for k in ("DOCKERHUB_USER", "DOCKERHUB_PASSWORD", "DOCKERHUB_REPO"):
            monkeypatch.delenv(k, raising=False)
        rc = vb.main(["version_bump.py"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "usage" in err.lower()

    def test_uses_env_credentials_and_default_repo(self, monkeypatch, capsys):
        monkeypatch.setenv("DOCKERHUB_USER", "alice")
        monkeypatch.setenv("DOCKERHUB_PASSWORD", "p4ss!")
        opener = FakeOpener([login_ok("t"), tags_page([])])
        # Patch the module attribute; version_bump resolves it at call time.
        monkeypatch.setattr(urllib.request, "urlopen", opener)
        rc = vb.main(["version_bump.py"])
        out = capsys.readouterr().out.strip()
        assert rc == 0
        assert out == "0.0.1"
        # Login + tags page must have been issued.
        assert len(opener.calls) == 2
        # The tags URL must use the default repo (laurel_notas-test) and the
        # env-supplied user.
        tags_url = opener.calls[1][0]
        assert "/repositories/alice/laurel_notas-test/tags/" in tags_url
        # The login body must include the password and the JSON Content-Type.
        _url, login_data, login_headers, _ = opener.calls[0]
        body = login_data.decode() if isinstance(login_data, bytes) else login_data
        assert "p4ss!" in body
        assert login_headers.get("content-type") == "application/json"

    def test_cli_uses_positional_credentials(self, monkeypatch, capsys):
        monkeypatch.delenv("DOCKERHUB_USER", raising=False)
        monkeypatch.delenv("DOCKERHUB_PASSWORD", raising=False)
        opener = FakeOpener([login_ok("t"), tags_page([])])
        monkeypatch.setattr(urllib.request, "urlopen", opener)
        rc = vb.main(["version_bump.py", "alice", "p4ss!", "custom_repo"])
        out = capsys.readouterr().out.strip()
        assert rc == 0
        assert out == "0.0.1"
        tags_url = opener.calls[1][0]
        assert "/repositories/alice/custom_repo/tags/" in tags_url
