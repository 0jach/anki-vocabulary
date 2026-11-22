"""Loopback JSON HTTP without proxies, redirects, or third-party HTTP dependencies."""

import json
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class ServiceError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url: str, payload: dict | None = None, *, timeout: float = 10) -> dict:
    request = Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(
            request, timeout=timeout
        ) as response:
            data = response.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                raise ServiceError("Local service response exceeded 4 MiB")
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ServiceError("Local service returned a non-object JSON response")
            return value
    except HTTPError as exc:
        raise ServiceError(f"Local service returned HTTP {exc.code}") from exc
    except (URLError, OSError, ValueError) as exc:
        raise ServiceError(f"Local service request failed: {exc}") from exc
