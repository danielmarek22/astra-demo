"""Opt-in hosted discovery; page reading still belongs to Astra's reader."""

import os

from app.tools.web.contracts import WebSearchError
from app.tools.web_search import BoundedSearchClient


class LangSearchClient(BoundedSearchClient):
    provider_name = "LangSearch"
    publication_date_source = "langsearch.datePublished"

    def __init__(self, *, api_key_env: str = "LANGSEARCH_API_KEY", **settings):
        # Keep credentials off configurable origins, redirects, trace payloads and YAML.
        settings.pop("base_url", None)
        settings["max_retries"] = 0
        super().__init__(base_url="https://api.langsearch.com", **settings)
        self.api_key_env = api_key_env
        key = os.environ.get(api_key_env, "").strip()
        self._api_key = key if key and key.isascii() and all(33 <= ord(char) <= 126 for char in key) else None
        self.is_configured = self._api_key is not None

    def probe(self) -> bool:
        # A search consumes account allowance, even during startup. Configuration
        # readiness is separate; remote availability is learned on actual use.
        return False

    def _search_params(self, query: str, limit: int, time_range: str | None) -> dict:
        if self._api_key is None:
            raise WebSearchError("unavailable", f"LangSearch needs a valid key in {self.api_key_env}; set it before starting Astra.")
        return {
            "query": query, "count": min(50, max(1, limit)),
            "contents": {"text": False},
            "freshness": {None: "noLimit", "day": "oneDay", "month": "oneMonth", "year": "oneYear"}[time_range],
        }

    def _http_request(self, params: dict) -> tuple[str, str, dict]:
        return "POST", f"{self.base_url}/v1/web-search", {
            "json": params, "headers": {"Authorization": f"Bearer {self._api_key}"},
        }

    def _normalize_payload(self, payload: object) -> dict:
        if not isinstance(payload, dict) or payload.get("code") not in (200, "200"):
            raise WebSearchError("invalid_response", "LangSearch did not return a successful search response.")
        data = payload.get("data")
        pages = data.get("webPages") if isinstance(data, dict) else None
        values = pages.get("value") if isinstance(pages, dict) else None
        if not isinstance(values, list):
            raise WebSearchError("invalid_response", "LangSearch JSON must contain a data.webPages.value list.")
        return {"results": [
            {"title": item.get("name"), "url": item.get("url"),
             "content": item.get("snippet"), "publishedDate": item.get("datePublished")}
            if isinstance(item, dict) else item for item in values
        ]}
