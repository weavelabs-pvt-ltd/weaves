"""Validated request model used to begin connector OAuth."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _OAuthStart(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    display_name: str = Field(min_length=2, max_length=160)
    include_thread_replies: bool = False
