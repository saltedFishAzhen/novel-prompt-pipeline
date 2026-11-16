"""Sanitized errors safe for logs and command-line output."""


class ImageError(Exception):
    """A local validation, API, or output error without raw service details."""
