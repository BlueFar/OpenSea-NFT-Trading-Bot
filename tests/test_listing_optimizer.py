from unittest.mock import MagicMock
from src.providers.opensea.provider import OpenSeaProvider

def test_listing_optimizer_early_exit():
    """Validates that pagination stops on page 1 when listings exceed threshold."""
    mock_client = MagicMock()
    # Page 1 returns 50 listings and next="page2"
    mock_client.get.return_value = {
        "listings": [{"order_hash": f"0x{i}", "chain": "ethereum", "status": "ACTIVE"} for i in range(50)],
        "next": "cursor_page_2"
    }

    provider = OpenSeaProvider(mock_client)
    # Threshold is 30 listings (e.g. 6% of 500 supply)
    count, is_early_exit = provider.get_active_listings_count("test-slug", early_exit_threshold=30)

    # Must exit immediately on page 1 with count=50 and is_early_exit=True
    assert is_early_exit is True
    assert count == 50
    # Must only have called OpenSea client once! (never fetched page 2)
    assert mock_client.get.call_count == 1

def test_listing_optimizer_full_pagination_under_threshold():
    """Validates full pagination when listings remain under the threshold."""
    mock_client = MagicMock()
    # Page 1 returns 10 listings and next="page2"
    # Page 2 returns 15 listings and next=None
    mock_client.get.side_effect = [
        {
            "listings": [{"order_hash": f"0x{i}", "chain": "ethereum", "status": "ACTIVE"} for i in range(10)],
            "next": "cursor_page_2"
        },
        {
            "listings": [{"order_hash": f"0x{i+10}", "chain": "ethereum", "status": "ACTIVE"} for i in range(15)],
            "next": None
        }
    ]

    provider = OpenSeaProvider(mock_client)
    # Threshold is 50 listings
    count, is_early_exit = provider.get_active_listings_count("test-slug", early_exit_threshold=50)

    # Must paginate fully and return total count = 25, is_early_exit=False
    assert is_early_exit is False
    assert count == 25
    assert mock_client.get.call_count == 2
