import tempfile
import os
from unittest.mock import MagicMock
from src.discovery.engine import DiscoveryEngine
from src.storage.state_store import StateStore
from src.config.settings import DiscoveryConfig

def test_discovery_progressive_checkpointing():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = os.path.join(temp_dir, "bot.db")
        state_store = StateStore(db_path)

        mock_provider = MagicMock()
        # Page 1: returns ["slug1", "slug2"] and next="cursor_p2"
        # Page 2: returns ["slug3"] and next="cursor_p3"
        mock_provider.discover_collections.side_effect = [
            (["slug1", "slug2"], "cursor_p2"),
            (["slug3"], "cursor_p3"),
        ]
        mock_provider.discover_top_collections.return_value = ["slug_top"]
        mock_provider.discover_trending_collections.return_value = ["slug_trending"]

        config = DiscoveryConfig(
            primary_source="collections",
            batch_size=50,
            max_pages_per_cycle=2,
            enable_top=True,
            enable_trending=True,
            watchlist=["slug_watch"],
        )

        discovery = DiscoveryEngine(mock_provider, state_store, config)
        slugs = discovery.discover_next_batch()

        # Check all discovered slugs are included and deduplicated
        assert set(slugs) == {"slug1", "slug2", "slug3", "slug_top", "slug_trending", "slug_watch"}
        
        # Check cursor checkpoint was updated to cursor_p3
        assert state_store.get_checkpoint("collections") == "cursor_p3"
