"""Opaque keyset cursors for task and transcript pages."""

from __future__ import annotations

from application.pagination import decode_time_cursor, encode_time_cursor

encode_cursor = encode_time_cursor
decode_cursor = decode_time_cursor
