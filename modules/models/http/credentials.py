"""Request models shared across related API route modules."""

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ProviderCredentialRequest(BaseModel):
    """Credential rotation payload shared by model and plugin endpoints."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    api_key: SecretStr = Field(min_length=1, max_length=4096)
