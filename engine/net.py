"""Portable verified HTTPS settings for stdlib network calls."""

from __future__ import annotations

import os
import ssl


def https_context() -> ssl.SSLContext:
    """Use Python's CA store, with the macOS system bundle as a safe fallback."""
    candidates = [
        os.environ.get("SSL_CERT_FILE"),
        "/etc/ssl/cert.pem",
        "/private/etc/ssl/cert.pem",
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return ssl.create_default_context(cafile=path)
    return ssl.create_default_context()
