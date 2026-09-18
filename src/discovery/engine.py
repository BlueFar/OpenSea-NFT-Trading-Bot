import time
from typing import List, Set, Optional
from ..providers.base import CollectionDataProvider
from ..storage.state_store import StateStore
from ..config.settings import DiscoveryConfig
from ..utils.logging import setup_logger

logger = setup_logger("discovery_engine")

class DiscoveryEngine:
    """
    Continuous progressive collection discovery engine.
    Uses general /api/v2/collections with persistent SQLite checkpointing,
    along with supplementary Top/Trending feeds and custom watchlist.
    """

    def __init__(
        self,
        provider: CollectionDataProvider,
        state_store: StateStore,
        config: DiscoveryConfig,
    ):
        self.provider = provider
        self.state_store = state_store
        self.config = config
        self._last_supplementary_run = 0.0

    def discover_next_batch(self) -> List[str]:
        """
        Progressively crawls N pages from the primary collections endpoint,
        updating the cursor in the SQLite state store.
        Also includes supplementary feeds periodically.
        """
        discovered_slugs: Set[str] = set()

        # 1. Primary: /api/v2/collections with checkpointed cursor
        cursor = self.state_store.get_checkpoint("collections")
        logger.info("Starting progressive discovery crawl from cursor: %s", cursor or "START")

        pages_crawled = 0
        current_cursor = cursor

        while pages_crawled < self.config.max_pages_per_cycle:
            slugs, next_cursor = self.provider.discover_collections(
                cursor=current_cursor,
                limit=self.config.batch_size,
            )
            if not slugs:
                logger.info("No more collections returned from primary endpoint. Resetting cursor.")
                current_cursor = None
                break

            for s in slugs:
                discovered_slugs.add(s)

            pages_crawled += 1
            current_cursor = next_cursor

            # Save checkpoint after each successful page
            self.state_store.save_checkpoint("collections", current_cursor)

            if not next_cursor:
                logger.info("Reached end of collections catalog. Resetting cursor checkpoint.")
                current_cursor = None
                self.state_store.save_checkpoint("collections", None)
                break

        logger.info("Crawled %d primary pages, discovered %d collections.", pages_crawled, len(discovered_slugs))

        # 2. Supplementary Feeds (Top, Trending, Watchlist)
        now = time.time()
        # Run supplementary feeds every 4 hours or on first run
        if now - self._last_supplementary_run > 14400.0 or self._last_supplementary_run == 0.0:
            self._last_supplementary_run = now

            if self.config.enable_top:
                try:
                    top_slugs = self.provider.discover_top_collections(limit=50)
                    for s in top_slugs:
                        discovered_slugs.add(s)
                    logger.debug("Added %d collections from top feed.", len(top_slugs))
                except Exception as e:
                    logger.warning("Failed to fetch top collections: %s", e)

            if self.config.enable_trending:
                try:
                    trending_slugs = self.provider.discover_trending_collections(limit=50)
                    for s in trending_slugs:
                        discovered_slugs.add(s)
                    logger.debug("Added %d collections from trending feed.", len(trending_slugs))
                except Exception as e:
                    logger.warning("Failed to fetch trending collections: %s", e)

        # 3. Watchlist from config
        if self.config.watchlist:
            for s in self.config.watchlist:
                discovered_slugs.add(s)

        return list(discovered_slugs)
