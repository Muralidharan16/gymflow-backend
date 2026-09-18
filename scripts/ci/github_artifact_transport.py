#!/usr/bin/env python3
"""Safe GitHub Actions artifact transport helpers.

GitHub's artifact archive endpoint is authenticated and responds with a signed
cross-host redirect. The GitHub bearer token must never be forwarded to the
blob host. This module deliberately handles the first redirect itself and then
downloads the signed URL without GitHub authorization headers.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request


_GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download_github_artifact(url: str, token: str, *, timeout: int = 120) -> bytes:
    if not token:
        raise RuntimeError("GitHub token is required for artifact download")

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"api.github.com", "github.com"}:
        raise RuntimeError(f"unexpected GitHub artifact API URL: {url!r}")

    request = urllib.request.Request(
        url,
        headers={
            **_GITHUB_HEADERS,
            "Authorization": f"Bearer {token}",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect())

    try:
        opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code not in {301, 302, 303, 307, 308}:
            raise
        location = exc.headers.get("Location")
        if not location:
            raise RuntimeError("GitHub artifact response omitted redirect Location") from exc
    else:
        raise RuntimeError("GitHub artifact endpoint did not return a signed redirect")

    signed = urllib.parse.urlparse(location)
    if signed.scheme != "https" or not signed.hostname:
        raise RuntimeError("GitHub artifact redirect is not an absolute HTTPS URL")
    if signed.hostname in {"api.github.com", "github.com"}:
        raise RuntimeError("GitHub artifact redirect did not leave the API origin")

    # Intentionally do not attach Authorization, Accept, or API-version headers.
    signed_request = urllib.request.Request(
        location,
        headers={"User-Agent": "doers-p10-certification"},
    )
    with urllib.request.urlopen(signed_request, timeout=timeout) as response:
        return response.read()
