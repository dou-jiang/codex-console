"""Registration-facing client boundary for the phase 1 core."""

from dataclasses import dataclass, field

from .oauth import OAuthManager
from .openai_http import OpenAIHTTPClient
from .sentinel import build_sentinel_pow_token


@dataclass(slots=True)
class OpenAIRegistrationClient:
    """Minimal wrapper that groups the registration-facing client helpers."""

    proxy_url: str | None = None
    http_client: OpenAIHTTPClient = field(init=False)
    oauth_manager: OAuthManager = field(init=False)

    def __post_init__(self) -> None:
        self.http_client = OpenAIHTTPClient(proxy_url=self.proxy_url)
        self.oauth_manager = OAuthManager(proxy_url=self.proxy_url)

    def build_sentinel_token(self) -> str:
        """Build a Sentinel proof token using the current HTTP client user agent."""

        user_agent = self.http_client.default_headers.get("User-Agent", "")
        return build_sentinel_pow_token(user_agent)


__all__ = ["OpenAIRegistrationClient"]
