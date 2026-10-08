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

        # 1. Primary: /api/v2/collections, one checkpointed cursor per chain (or one overall if no chains set)
        for chain in (self.config.chains or [None]):
            discovered_slugs.update(self._crawl_primary(chain))

        logger.info("Primary crawl discovered %d collections.", len(discovered_slugs))

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

    def _crawl_primary(self, chain: Optional[str]) -> List[str]:
        """
        Crawls up to max_pages_per_cycle pages of /api/v2/collections for one chain.
        With order_by set (e.g. seven_day_volume), the crawl restarts from the top after
        max_depth_pages so the most active collections keep being rediscovered.
        """
        key = f"collections:{chain}" if chain else "collections"
        depth_key = f"{key}#depth"
        current_cursor = self.state_store.get_checkpoint(key)
        try:
            depth = int(self.state_store.get_checkpoint(depth_key) or 0)
        except ValueError:
            depth = 0
        if not current_cursor:
            depth = 0
        logger.info("Discovery crawl [%s] from cursor: %s (depth %d)", chain or "all chains", current_cursor or "START", depth)

        found: List[str] = []
        pages_crawled = 0
        while pages_crawled < self.config.max_pages_per_cycle:
            kwargs = {"cursor": current_cursor, "limit": self.config.batch_size}
            if chain:
                kwargs["chain"] = chain
            if self.config.order_by:
                kwargs["order_by"] = self.config.order_by
            slugs, next_cursor = self.provider.discover_collections(**kwargs)
            if not slugs:
                logger.info("No more collections returned for %s. Resetting cursor.", chain or "all chains")
                current_cursor = None
                depth = 0
                self.state_store.save_checkpoint(key, None)
                break

            found.extend(slugs)
            pages_crawled += 1
            depth += 1
            current_cursor = next_cursor

            if not next_cursor or (self.config.max_depth_pages and depth >= self.config.max_depth_pages):
                logger.info("Reached end of crawl window for %s. Restarting from the top next cycle.", chain or "all chains")
                current_cursor = None
                depth = 0
                self.state_store.save_checkpoint(key, None)
                break

            # Save checkpoint after each successful page
            self.state_store.save_checkpoint(key, current_cursor)

        self.state_store.save_checkpoint(depth_key, str(depth))
        return found
