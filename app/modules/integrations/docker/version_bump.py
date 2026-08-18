"""Version auto-increment from Docker Hub tags.

Source-of-truth: existing tags in `docker.io/{user}/{repo}`.
Policy: highest semver (`^v?MAJOR.MINOR.PATCH$`) wins; bump PATCH by 1.

Designed to be importable and unit-testable: HTTP calls go through
`urllib.request.urlopen` which is overridable via the `opener` kwarg
(the tests inject a fake opener). The module also runs as a script:
reads DOCKERHUB_USER / DOCKERHUB_PASSWORD / DOCKERHUB_REPO from env
(REPO defaults to "laurel_notas-test") and prints the next version.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Callable, Iterable, Optional, Tuple
from urllib.parse import urljoin

SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
DEFAULT_REPO = "laurel_notas-test"
PAGE_SIZE = 100
DEFAULT_TIMEOUT = 15
LOGIN_URL = "https://hub.docker.com/v2/users/login/"
TAGS_URL_TEMPLATE = "https://hub.docker.com/v2/repositories/{user}/{repo}/tags/?page_size={n}"


class DockerHubError(RuntimeError):
    """Raised when Docker Hub rejects a request or returns an unexpected body."""


def _read_json(response) -> dict:
    return json.loads(response.read())


def login(user: str, password: str, *, opener: Optional[Callable] = None,
          timeout: float = DEFAULT_TIMEOUT) -> str:
    """Exchange credentials for a JWT. Raises DockerHubError on failure.

    `opener` defaults to `urllib.request.urlopen`, resolved at call time so
    tests can monkeypatch the module attribute.
    """
    if opener is None:
        opener = urllib.request.urlopen
    body = json.dumps({"username": user, "password": password}).encode("utf-8")
    req = urllib.request.Request(
        LOGIN_URL, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with opener(req, timeout=timeout) as resp:
            data = _read_json(resp)
    except urllib.error.HTTPError as exc:
        raise DockerHubError(f"dockerhub login failed: HTTP {exc.code}") from exc
    except (OSError, TimeoutError) as exc:
        raise DockerHubError(f"dockerhub login network error: {exc}") from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise DockerHubError(f"dockerhub login returned invalid JSON: {exc}") from exc
    token = data.get("token") if isinstance(data, dict) else None
    if not token or not isinstance(token, str):
        raise DockerHubError("dockerhub login returned no token")
    return token


def _iter_tag_pages(user: str, repo: str, token: str, *,
                    opener: Optional[Callable] = None,
                    timeout: float = DEFAULT_TIMEOUT,
                    page_size: int = PAGE_SIZE) -> Iterable[list]:
    """Yield each page's `results` list; follows `next` until exhausted.

    Defensive: treats JSON `null`, empty string and the literal string
    "null" as end-of-pagination, and resolves relative `next` URLs against
    the current page URL.
    """
    if opener is None:
        opener = urllib.request.urlopen
    url = TAGS_URL_TEMPLATE.format(user=user, repo=repo, n=page_size)
    headers = {"Authorization": "JWT " + token}
    while url:
        req = urllib.request.Request(url, headers=headers)
        try:
            with opener(req, timeout=timeout) as resp:
                page = _read_json(resp)
        except urllib.error.HTTPError as exc:
            raise DockerHubError(f"dockerhub tags fetch failed: HTTP {exc.code}") from exc
        except (OSError, TimeoutError) as exc:
            raise DockerHubError(f"dockerhub tags fetch network error: {exc}") from exc
        except (ValueError, json.JSONDecodeError) as exc:
            raise DockerHubError(f"dockerhub tags fetch returned invalid JSON: {exc}") from exc
        results = page.get("results") if isinstance(page, dict) else None
        yield results or []
        next_val = page.get("next") if isinstance(page, dict) else None
        if not next_val or next_val == "null":
            url = None
        elif not isinstance(next_val, str):
            # Defensive: Docker Hub always returns str|None; anything else is
            # an upstream contract violation. Bail out instead of crashing.
            url = None
        else:
            url = urljoin(url, next_val)


def _highest_semver(tags) -> Tuple[int, int, int]:
    """Return the (major, minor, patch) of the highest semver tag in `tags`.

    Accepts either a list of dicts (`{"name": "..."}`) or a list of strings.
    Non-semver tags (e.g. "latest", "dd0997e") are ignored.
    """
    best = (0, 0, 0)
    for t in tags:
        name = t["name"] if isinstance(t, dict) else t
        if not isinstance(name, str):
            continue
        m = SEMVER_RE.match(name)
        if not m:
            continue
        v = tuple(int(x) for x in m.groups())
        if v > best:
            best = v
    return best


def next_version(user: str, password: str, repo: str = DEFAULT_REPO, *,
                 opener: Optional[Callable] = None,
                 timeout: float = DEFAULT_TIMEOUT,
                 page_size: int = PAGE_SIZE) -> str:
    """Compute the next version string 'MAJOR.MINOR.PATCH'.

    Patches the highest semver tag in the Docker Hub repo by +1. If no
    semver tags exist, the result is "0.0.1".
    """
    if opener is None:
        opener = urllib.request.urlopen
    token = login(user, password, opener=opener, timeout=timeout)
    highest = (0, 0, 0)
    for results in _iter_tag_pages(
        user, repo, token, opener=opener, timeout=timeout, page_size=page_size
    ):
        h = _highest_semver(results)
        if h > highest:
            highest = h
    maj, mn, pa = highest
    return f"{maj}.{mn}.{pa + 1}"


def _resolve_env_or_arg(env: str, argv: list, arg_index: int, fallback: Optional[str]) -> Optional[str]:
    val = os.environ.get(env)
    if val:
        return val
    if argv and len(argv) > arg_index:
        candidate = argv[arg_index]
        if candidate:
            return candidate
    return fallback


def main(argv: Optional[list] = None) -> int:
    # If argv is None, fall back to sys.argv (preserving program name at [0]).
    argv = sys.argv if argv is None else list(argv)
    user = _resolve_env_or_arg("DOCKERHUB_USER", argv, 1, None)
    password = _resolve_env_or_arg("DOCKERHUB_PASSWORD", argv, 2, None)
    repo = _resolve_env_or_arg("DOCKERHUB_REPO", argv, 3, DEFAULT_REPO)
    if not user or not password:
        sys.stderr.write(
            "usage: DOCKERHUB_USER=.. DOCKERHUB_PASSWORD=.. "
            "[DOCKERHUB_REPO=..] python3 version_bump.py\n"
        )
        return 2
    print(next_version(user, password, repo))
    return 0


if __name__ == "__main__":
    sys.exit(main())
