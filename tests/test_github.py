import json
import subprocess
import urllib.error

import pytest

from upstream_watch.github import GitHub, runtime_token


def test_etag_and_headers(fake_api):
    routes, requests, base = fake_api
    routes["/repos/acme/widget"] = (200, {"full_name": "acme/widget"}, {"ETag": '"one"'})
    cache = {}
    api = GitHub(cache, base=base)
    assert api.metadata("acme/widget")["full_name"] == "acme/widget"
    assert api.metadata("acme/widget")["full_name"] == "acme/widget"
    headers = requests[-1][1]
    assert headers["If-None-Match"] == '"one"'
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["X-Github-Api-Version"] == "2022-11-28"
    assert headers["User-Agent"].startswith("upstream-watch/")
    assert "Authorization" not in headers


@pytest.mark.parametrize("status", [200, 403, 429])
def test_rate_limit_stops_requests(fake_api, status):
    routes, requests, base = fake_api
    routes["/repos/acme/widget"] = (status, {}, {"x-ratelimit-remaining": "0"})
    api = GitHub({}, base=base)
    api.metadata("acme/widget")
    api.releases("acme/widget")
    assert len(requests) == 1
    assert api.messages == ["GitHub API rate limit reached; continuing with git only"]


def test_no_secret_in_cache_errors_or_output(fake_api, capsys):
    routes, requests, base = fake_api
    token = "gh" + "p_" + "Z" * 36
    routes["/repos/acme/widget"] = (200, {"full_name": token}, {})
    cache = {}
    api = GitHub(cache, token=token, base=base)
    assert api.metadata("acme/widget")["full_name"] == "[redacted]"
    assert token not in json.dumps(cache)
    routes["/error"] = (500, {"message": token}, {})
    assert api.get("/error") is None
    assert token not in str(api.messages) + str(capsys.readouterr())
    assert api.sanitize("Authorization: Bearer " + token) == "Authorization: [redacted]"


def test_no_auth_redirect(fake_api):
    routes, requests, base = fake_api
    routes["/start"] = (302, {}, {"Location": "https://example.org/destination"})
    api = GitHub({}, token="synthetic-" + "credential", base=base)
    assert api.get("/start") is None
    assert len(requests) == 1


def test_network_failure_and_invalid_response(fake_api, monkeypatch):
    _, _, base = fake_api
    api = GitHub({}, base=base)

    def failure(*args, **kwargs):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(api.opener, "open", failure)
    assert api.get("/anything") is None
    assert "unavailable" in api.messages[0]

    class BadResponse:
        code = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return b"not json"

    monkeypatch.setattr(api.opener, "open", lambda *a, **k: BadResponse())
    assert api.get("/anything") is None
    assert "invalid JSON" in api.messages[-1]


@pytest.mark.parametrize(
    "response",
    [{}, {"object": {"type": "commit"}}, {"object": {"type": "tag", "sha": "unsafe/sha"}}],
)
def test_verification_shapes(fake_api, response):
    routes, _, base = fake_api
    routes["/repos/acme/widget/git/ref/tags/v1"] = (200, response, {})
    assert GitHub({}, base=base).verification("acme/widget", "v1") is None


def test_runtime_token_synthetic_only(monkeypatch):
    token = "gh" + "p_" + "a" * 36
    monkeypatch.setenv("GITHUB_TOKEN", token)
    assert runtime_token() == token
    monkeypatch.delenv("GITHUB_TOKEN")
    monkeypatch.setattr("upstream_watch.github.shutil.which", lambda name: "/example/gh")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, token + "\n", "")
    )
    assert runtime_token() == token
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "error")
    )
    assert runtime_token() is None

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("gh", 15)

    monkeypatch.setattr(subprocess, "run", timeout)
    assert runtime_token() is None
    monkeypatch.setattr("upstream_watch.github.shutil.which", lambda name: None)
    assert runtime_token() is None


def test_same_origin_transfer_redirect_and_request_budget(fake_api):
    routes, requests, base = fake_api
    routes["/repos/acme/widget"] = (301, {}, {"Location": base + "/repositories/123"})
    routes["/repositories/123"] = (200, {"full_name": "example-org/widget"}, {})
    routes["/repos/acme/widget/releases?per_page=10"] = (200, [], {})
    api = GitHub({}, base=base)
    assert api.metadata("acme/widget")["full_name"] == "example-org/widget"
    assert api.releases("acme/widget") == []
    assert len(requests) == 2


def test_redirect_budget_also_limits_chains(fake_api):
    routes, requests, base = fake_api
    routes["/repos/acme/widget"] = (301, {}, {"Location": base + "/first"})
    routes["/first"] = (301, {}, {"Location": base + "/second"})
    api = GitHub({}, base=base)
    assert api.metadata("acme/widget") == {}
    assert len(requests) == 2
