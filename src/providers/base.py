from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple
from ..models.collection import (
    CollectionMetadata,
    CollectionStats,
    FloorPricePoint,
    SaleEvent,
    Listing,
    Offer,
)

class CollectionDataProvider(ABC):
    """Abstract interface for NFT marketplace data providers."""

    @abstractmethod
    def get_collection(self, slug: str) -> Optional[CollectionMetadata]:
        """Fetches metadata, contracts, links, and fees for a collection."""
        pass

    @abstractmethod
    def get_collections_batch(self, slugs: List[str]) -> List[CollectionMetadata]:
        """Fetches metadata for multiple collections in a single batch request."""
        pass

    @abstractmethod
    def get_collection_stats(self, slug: str) -> Optional[CollectionStats]:
        """Fetches current stats (floor price, owner count, volume) for a collection."""
        pass

    @abstractmethod
    def get_floor_price_history(self, slug: str, timeframe: str) -> List[FloorPricePoint]:
        """Fetches time-series floor price points (e.g. timeframe='one_day', 'seven_days')."""
        pass

    @abstractmethod
    def get_sale_events(self, slug: str, after_timestamp: int, **kwargs) -> Optional[List[SaleEvent]]:
        """Fetches sale events occurring after the given Unix epoch timestamp. Returns None if API fails."""
        pass

    def get_order_info(self, chain: str, protocol_address: str, order_hash: str) -> Optional[Dict[str, Optional[str]]]:
        """Whether a sale was a listing bought or an offer accepted. None = unknown (the default)."""
        return None

    def get_nft_rarity(self, chain: str, contract: str, token_id: str) -> Tuple[bool, Optional[int]]:
        """(looked up, rarity rank or None). Default: not looked up."""
        return False, None

    @abstractmethod
    def get_active_listings_count(
        self,
        slug: str,
        early_exit_threshold: Optional[int] = None,
    ) -> Tuple[Optional[int], bool]:
        """
        Fetches active listings count. If early_exit_threshold is provided,
        aborts pagination as soon as accumulated count exceeds the threshold.
        Returns (count, is_early_exit_exceeded). If API fails, count is None.
        """
        pass

    @abstractmethod
    def get_top_offer(self, slug: str, currency_filter=None) -> Optional[Offer]:
        """Fetches the highest active offer/bid for a collection."""
        pass

    @abstractmethod
    def discover_collections(
        self,
        cursor: Optional[str] = None,
        limit: int = 50,
        chain: Optional[str] = None,
        order_by: Optional[str] = None,
    ) -> Tuple[List[str], Optional[str]]:
        """
        Discovers collection slugs via general collections endpoint.
        Returns (list_of_slugs, next_cursor).
        """
        pass

    @abstractmethod
    def discover_top_collections(self, limit: int = 50) -> List[str]:
        """Discovers top collections ranked by volume/sales."""
        pass

    @abstractmethod
    def discover_trending_collections(self, limit: int = 50) -> List[str]:
        """Discovers trending collections."""
        pass
