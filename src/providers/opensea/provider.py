import time
from typing import List, Optional, Tuple, Dict, Any
from ..base import CollectionDataProvider
from .client import OpenSeaClient
from .parser import (
    parse_collection_metadata,
    parse_collection_stats,
    parse_floor_price_history,
    parse_sale_events,
    parse_listings,
    parse_offers,
)
from ...models.collection import (
    CollectionMetadata,
    CollectionStats,
    FloorPricePoint,
    SaleEvent,
    Listing,
    Offer,
)
from ...utils.logging import setup_logger

logger = setup_logger("opensea_provider")

class OpenSeaProvider(CollectionDataProvider):
    """
    OpenSea API v2 Data Provider implementation.
    Includes memory caching, batch optimization, pagination safeguards, and early-exit listings.
    """

    def __init__(self, client: OpenSeaClient):
        self.client = client
        self._cache: Dict[str, Tuple[float, Any]] = {}

    def _get_cache(self, key: str, ttl: float) -> Optional[Any]:
        if key in self._cache:
            ts, val = self._cache[key]
            if time.time() - ts < ttl:
                return val
            del self._cache[key]
        return None

    def _set_cache(self, key: str, val: Any):
        self._cache[key] = (time.time(), val)

    def get_collection(self, slug: str) -> Optional[CollectionMetadata]:
        cache_key = f"col:{slug}"
        cached = self._get_cache(cache_key, ttl=3600.0)
        if cached:
            return cached

        data = self.client.get(f"/api/v2/collections/{slug}")
        if not data:
            return None

        metadata = parse_collection_metadata(data)
        self._set_cache(cache_key, metadata)
        return metadata

    def get_collections_batch(self, slugs: List[str]) -> List[CollectionMetadata]:
        if not slugs:
            return []

        # OpenSea batch collections endpoint
        data = self.client.post("/api/v2/collections/batch", json_data={"slugs": slugs})
        if not data or "collections" not in data:
            # Fallback to single fetches if batch fails or returns empty
            results = []
            for s in slugs:
                col = self.get_collection(s)
                if col:
                    results.append(col)
            return results

        results = []
        for raw_col in data["collections"]:
            meta = parse_collection_metadata(raw_col)
            self._set_cache(f"col:{meta.slug}", meta)
            results.append(meta)
        return results

    def get_collection_stats(self, slug: str) -> Optional[CollectionStats]:
        cache_key = f"stats:{slug}"
        cached = self._get_cache(cache_key, ttl=300.0)
        if cached:
            return cached

        data = self.client.get(f"/api/v2/collections/{slug}/stats")
        if not data:
            return None

        stats = parse_collection_stats(data)
        self._set_cache(cache_key, stats)
        return stats

    def get_floor_price_history(self, slug: str, timeframe: str) -> List[FloorPricePoint]:
        cache_key = f"floor:{slug}:{timeframe}"
        cached = self._get_cache(cache_key, ttl=600.0)
        if cached:
            return cached

        data = self.client.get(
            f"/api/v2/collections/{slug}/floor_prices",
            params={"timeframe": timeframe}
        )
        if not data:
            return []

        points = parse_floor_price_history(data)
        self._set_cache(cache_key, points)
        return points

    def get_sale_events(
        self,
        slug: str,
        after_timestamp: int,
        max_pages: int = 20,
    ) -> Optional[List[SaleEvent]]:
        """
        Fetches sale events occurring after the given timestamp.
        Returns None if the API request fails, preventing false zero-sales counts.
        """
        all_events = []
        next_cursor = None
        seen_cursors = set()

        for page_idx in range(max_pages):
            params: Dict[str, Any] = {
                "event_type": "sale",
                "after": after_timestamp,
                "limit": 100,
            }
            if next_cursor:
                if next_cursor in seen_cursors:
                    logger.warning("Repeated cursor detected for %s sales events. Halting pagination.", slug)
                    break
                seen_cursors.add(next_cursor)
                params["next"] = next_cursor

            data = self.client.get(f"/api/v2/events/collection/{slug}", params=params)
            if data is None:
                if page_idx == 0:
                    logger.warning("Failed to retrieve sale events for %s on page 0. Returning None.", slug)
                    return None
                break

            events = parse_sale_events(data)
            all_events.extend(events)

            next_cursor = data.get("next")
            if not next_cursor:
                break

        return all_events

    def get_active_listings_count(
        self,
        slug: str,
        early_exit_threshold: Optional[int] = None,
    ) -> Tuple[Optional[int], bool]:
        """
        Paginates active listings. If early_exit_threshold is provided and
        the accumulated count exceeds it, halts pagination immediately.
        Returns (accumulated_count, is_early_exit_exceeded).
        If API fails, returns (None, False) rather than assuming zero listings.
        """
        total_count = 0
        next_cursor = None
        seen_cursors = set()
        max_pages = 30

        for page_idx in range(max_pages):
            params: Dict[str, Any] = {"limit": 100}
            if next_cursor:
                if next_cursor in seen_cursors:
                    logger.warning("Repeated cursor detected for %s listings. Halting pagination.", slug)
                    break
                seen_cursors.add(next_cursor)
                params["next"] = next_cursor

            data = self.client.get(f"/api/v2/listings/collection/{slug}/all", params=params)
            if data is None:
                if page_idx == 0:
                    logger.warning("Failed to retrieve active listings for %s on page 0. Returning (None, False).", slug)
                    return None, False
                break

            listings, next_cursor = parse_listings(data)
            total_count += len(listings)

            # Early-exit optimization
            if early_exit_threshold is not None and total_count > early_exit_threshold:
                logger.debug(
                    "Collection %s listings count (%d) exceeded threshold (%d) on page %d. Stopping pagination early.",
                    slug, total_count, early_exit_threshold, page_idx + 1
                )
                return total_count, True

            if not next_cursor:
                break

        return total_count, False

    def get_top_offer(self, slug: str) -> Optional[Offer]:
        """Fetches the highest active offer for a collection."""
        data = self.client.get(f"/api/v2/offers/collection/{slug}/all", params={"limit": 50})
        if not data:
            return None

        offers, _ = parse_offers(data)
        if not offers:
            return None

        # Return offer with highest price_value
        best_offer = max(offers, key=lambda x: x.price_value)
        return best_offer

    def discover_collections(
        self,
        cursor: Optional[str] = None,
        limit: int = 50,
        chain: Optional[str] = None,
    ) -> Tuple[List[str], Optional[str]]:
        """Queries general /api/v2/collections endpoint."""
        params: Dict[str, Any] = {"limit": limit}
        if cursor:
            params["next"] = cursor
        if chain:
            params["chain"] = chain

        data = self.client.get("/api/v2/collections", params=params)
        if not data:
            return [], None

        slugs = []
        for c in data.get("collections", []):
            if isinstance(c, dict):
                slug = c.get("collection") or c.get("slug")
                if slug:
                    slugs.append(slug)

        next_cursor = data.get("next")
        return slugs, next_cursor

    def discover_top_collections(self, limit: int = 50) -> List[str]:
        """Queries /api/v2/collections/top."""
        data = self.client.get(
            "/api/v2/collections/top",
            params={"limit": limit, "sort_by": "seven_days_sales"}
        )
        if not data:
            return []

        slugs = []
        for c in data.get("collections", []):
            if isinstance(c, dict):
                slug = c.get("collection") or c.get("slug")
                if slug:
                    slugs.append(slug)
        return slugs

    def discover_trending_collections(self, limit: int = 50) -> List[str]:
        """Queries /api/v2/collections/trending."""
        data = self.client.get(
            "/api/v2/collections/trending",
            params={"limit": limit, "timeframe": "seven_days"}
        )
        if not data:
            return []

        slugs = []
        for c in data.get("collections", []):
            if isinstance(c, dict):
                slug = c.get("collection") or c.get("slug")
                if slug:
                    slugs.append(slug)
        return slugs
