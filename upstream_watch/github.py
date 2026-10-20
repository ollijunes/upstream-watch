"""Conditional GitHub requests with a run-wide rate-limit circuit breaker."""

import http.client
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .changelog import release_body
from .config import WatchError
from .gitutil import SHA, redact, scrub


class NotPublic(WatchError):
    pass


def runtime_token() -> str | None:
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    if shutil.which("gh"):
        try:
            result = subprocess.run(  # noqa: S603,S607
                ["gh", "auth", "token", "--hostname", "github.com"],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, client):
        self.client = client

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        origin = urllib.parse.urlsplit(self.client.base)
        target = urllib.parse.urlsplit(newurl)
        if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
            return None
        if not self.client.consume():
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GitHub:
    def __init__(
        self,
        cache: dict,
        *,
        token: str | None = None,
        base: str = "https://api.github.com",
        opener=None,
    ):
        self.token = token
        self.cache = cache
        for url, entry in list(cache.items()):
            del cache[url]
            route = urllib.parse.urlsplit(url).path + "?" + urllib.parse.urlsplit(url).query
            if "/git/ref/" not in route:
                cache[self.sanitize(url)] = {
                    "etag": self.sanitize(entry.get("etag", "")),
                    "data": cache_fields(route, scrub(entry.get("data"), token)),
                }
        self.base = base
        self.stopped = False
        self.messages: list[str] = []
        self.counts: dict[str, int] = {}
        self.active_repo = ""
        self.opener = opener or urllib.request.build_opener(SameOriginRedirect(self))

    def consume(self) -> bool:
        count = self.counts.get(self.active_repo, 0)
        if not self.token and count >= 2:
            return False
        self.counts[self.active_repo] = count + 1
        return True

    def sanitize(self, text: str) -> str:
        return redact(text, self.token)

    def get(self, route: str):
        if self.stopped:
            return None
        match = re.match(r"/repos/([^/]+/[^/?]+)", route)
        self.active_repo = match[1].casefold() if match else route
        if not self.consume():
            return None
        url = self.base + route
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"upstream-watch/{__version__}",
        }
        cached = self.cache.get(self.sanitize(url), {})
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if self.token and self.base == "https://api.github.com":
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers)  # noqa: S310 - fixed API or test URL.
        try:
            try:
                response = self.opener.open(request, timeout=15)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                if response.headers.get("x-ratelimit-remaining") == "0":
                    self.stopped = True
                    self.messages.append("GitHub API rate limit reached; continuing with git only")
                if response.code == 304:
                    return scrub(cached.get("data"), self.token)
                if response.code == 404:
                    raise NotPublic("not-public")
                if response.code != 200:
                    if not self.stopped:
                        self.messages.append(
                            f"GitHub API returned HTTP {response.code}; using git only"
                        )
                    return None
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    self.messages.append("GitHub API response exceeds 1 MiB; using git only")
                    return None
                data = scrub(json.loads(raw.decode("utf-8")), self.token)
                if isinstance(data, dict) and data.get("private"):
                    raise NotPublic("not-public")
                data = cache_fields(route, data)
                if data is not None and "/git/ref/" not in route:
                    self.cache[self.sanitize(url)] = {
                        "etag": self.sanitize(response.headers.get("ETag", "")),
                        "data": data,
                    }
                return data
        except (OSError, http.client.HTTPException) as exc:
            self.messages.append(
                self.sanitize(f"GitHub API unavailable; continuing with git only: {exc}")
            )
            return None
        except (ValueError, UnicodeError):
            self.messages.append("GitHub API returned invalid JSON; using git only")
            return None

    def metadata(self, full_name: str):
        data = self.get(f"/repos/{full_name}")
        if isinstance(data, dict) and data.get("private"):
            raise NotPublic("not-public")
        return data if isinstance(data, dict) else {}

    def releases(self, full_name: str) -> list:
        data = self.get(f"/repos/{full_name}/releases?per_page=10")
        return data if isinstance(data, list) else []

    def verification(self, full_name: str, tag: str) -> dict | None:
        reference = self.get(f"/repos/{full_name}/git/ref/tags/{urllib.parse.quote(tag, safe='')}")
        if not isinstance(reference, dict) or reference.get("object", {}).get("type") != "tag":
            return None
        sha = reference["object"]["sha"]
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            return None
        data = self.get(f"/repos/{full_name}/git/tags/{sha}")
        return data.get("verification", {}) if isinstance(data, dict) else None


def cache_fields(route: str, data):
    if "/releases?" in route:
        if not isinstance(data, list):
            return []
        return [
            {
                **{
                    k: row[k]
                    for k in ("name", "tag_name", "published_at", "prerelease")
                    if k in row
                },
                "body": release_body(row.get("body") or ""),
            }
            for row in data[:10]
            if isinstance(row, dict)
        ]
    if not isinstance(data, dict):
        return None
    if "/git/ref/" in route:
        obj = data.get("object", {})
        return {"object": {k: obj[k] for k in ("type", "sha") if k in obj}}
    if "/git/tags/" in route:
        verification = data.get("verification", {})
        return {
            "verification": {
                k: verification[k] for k in ("verified", "reason") if k in verification
            }
        }
    result = {k: data[k] for k in ("full_name", "archived", "default_branch", "fork") if k in data}
    for key, field in (("owner", "login"), ("parent", "full_name")):
        if isinstance(data.get(key), dict) and field in data[key]:
            result[key] = {field: data[key][field]}
    return result
