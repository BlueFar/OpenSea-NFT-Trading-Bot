import time
import random
import threading
import requests
from typing import Optional, Dict, Any
from ...utils.logging import setup_logger

logger = setup_logger("opensea_client")

class OpenSeaApiError(Exception):
    """Base exception for OpenSea API communication failures."""
    pass

class OpenSeaAuthError(OpenSeaApiError):
    """Raised when OpenSea API returns 401 Unauthorized or 403 Forbidden (e.g. invalid or expired API key)."""
    pass

class OpenSeaRateLimitError(OpenSeaApiError):
    """Raised when rate limit retries are exhausted."""
    pass

class OpenSeaNetworkError(OpenSeaApiError):
    """
    Raised when OpenSea could not be reached at all (no internet, DNS or timeouts on every retry).
    Callers must not treat this as missing data: the bot pauses instead of rejecting collections.
    """
    pass

class OpenSeaClient:
    """
    Resilient HTTP client for OpenSea v2 API.
    Handles adaptive rate-limit backoff, retries, headers, and secret sanitization.
    """

    BASE_URL = "https://api.opensea.io"

    def __init__(
        self,
        api_key: Optional[str] = None,
        max_retries: int = 5,
        backoff_factor: float = 2.0,
        request_delay: float = 0.5,
    ):
        self.api_key = api_key
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.request_delay = request_delay
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "NFT-Monitor-Bot/1.0",
        })
        if self.api_key:
            self.session.headers.update({"x-api-key": self.api_key})

        # Set when the bot is asked to stop, so waits between retries end at once
        self.stop_event = threading.Event()

        self.last_rate_limit_remaining: Optional[int] = None
        self.last_rate_limit_reset: Optional[int] = None

    def set_api_key(self, api_key: Optional[str]) -> None:
        """Switches to a new key (e.g. after it was changed in .env) without a restart."""
        self.api_key = api_key
        if api_key:
            self.session.headers["x-api-key"] = api_key
        else:
            self.session.headers.pop("x-api-key", None)

    def _sleep(self, seconds: float) -> None:
        if self.stop_event.wait(seconds):
            raise OpenSeaNetworkError("Stopping: request cancelled")

    def close(self):
        """Closes the underlying HTTP session."""
        try:
            self.session.close()
        except Exception:
            pass

    def _apply_rate_limit_headers(self, response: requests.Response):
        """Inspects rate limit headers returned by OpenSea."""
        remaining = response.headers.get("x-ratelimit-remaining")
        reset_ts = response.headers.get("x-ratelimit-reset")
        if remaining is not None:
            try:
                self.last_rate_limit_remaining = int(remaining)
            except ValueError:
                pass
        if reset_ts is not None:
            try:
                self.last_rate_limit_reset = int(reset_ts)
            except ValueError:
                pass

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Executes HTTP request with exponential backoff on 429 and transient errors."""
        url = f"{self.BASE_URL}{path}" if not path.startswith("http") else path

        # Polite inter-request spacing
        if self.request_delay > 0:
            self._sleep(self.request_delay)

        network_failures = 0
        for attempt in range(self.max_retries + 1):
            if self.stop_event.is_set():
                raise OpenSeaNetworkError("Stopping: request cancelled")
            try:
                resp = self.session.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_data,
                    timeout=15.0,
                )
                self._apply_rate_limit_headers(resp)

                if resp.status_code == 200:
                    return resp.json()

                if resp.status_code == 404:
                    logger.debug("OpenSea 404 Not Found: %s", path)
                    return None

                if resp.status_code == 429:
                    # Adaptive rate limit handling
                    retry_after = resp.headers.get("retry-after")
                    wait_time = float(retry_after) if retry_after else (self.backoff_factor ** attempt) + random.uniform(0.5, 1.5)
                    logger.warning("OpenSea 429 Rate Limit hit. Backing off for %.2fs (attempt %d/%d)...", wait_time, attempt + 1, self.max_retries)
                    self._sleep(wait_time)
                    continue

                if resp.status_code in (401, 403):
                    logger.error("OpenSea Authentication Error (%d) for %s. API key invalid or expired.", resp.status_code, path)
                    raise OpenSeaAuthError(f"OpenSea Authentication failed ({resp.status_code}) for {path}: API key invalid or expired.")

                if resp.status_code >= 500:
                    wait_time = (self.backoff_factor ** attempt) + random.uniform(0.5, 1.5)
                    logger.warning("OpenSea Server Error (%d) on %s. Retrying in %.2fs...", resp.status_code, path, wait_time)
                    self._sleep(wait_time)
                    continue

                logger.warning("OpenSea unexpected HTTP %d: %s", resp.status_code, resp.text[:200])
                return None

            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                network_failures += 1
                wait_time = (self.backoff_factor ** attempt) + random.uniform(0.5, 1.5)
                logger.warning("Network connection/timeout error (%s). Retrying in %.2fs (attempt %d/%d)...", str(e)[:100], wait_time, attempt + 1, self.max_retries)
                self._sleep(wait_time)
            except (OpenSeaAuthError, OpenSeaRateLimitError, OpenSeaNetworkError):
                raise
            except Exception as e:
                logger.error("Unexpected error requesting %s: %s", path, str(e))
                return None

        if network_failures == self.max_retries + 1:
            raise OpenSeaNetworkError(f"OpenSea unreachable for {path} after {network_failures} attempts")
        logger.error("Exceeded maximum retries (%d) for %s", self.max_retries, path)
        return None

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        return self.request("GET", path, params=params)

    def post(self, path: str, json_data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        return self.request("POST", path, json_data=json_data)
