"""Refusals the waitlist raises for input a visitor can correct."""

from __future__ import annotations


class InvalidInput(ValueError):
    """The request is well formed but its content is refused (HTTP 422)."""
