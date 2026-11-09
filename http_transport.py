"""Authenticated phone requests must never follow an unexpected redirect."""
from __future__ import annotations

import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Let urllib raise HTTPError for the original response. Following a
        # redirect could disclose its Authorization header, or hide that a
        # command already reached the original server before a later failure.
        return None


def open_http(request: urllib.request.Request, timeout: float):
    """Open one request without changing AstrBot's process-wide URL opener."""
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)
