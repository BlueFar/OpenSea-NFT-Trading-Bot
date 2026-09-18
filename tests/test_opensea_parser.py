from src.providers.opensea.parser import (
    parse_collection_metadata,
    parse_collection_stats,
    parse_floor_price_history,
    parse_sale_events,
    parse_listings,
    parse_offers,
)

def test_parse_collection_metadata():
    raw = {
        "collection": "doodles-official",
        "name": "Doodles",
        "description": "A community-driven collectibles project",
        "created_date": "2021-10-17",
        "opensea_url": "https://opensea.io/collection/doodles-official",
        "safelist_status": "verified",
        "total_supply": 10000,
        "contracts": [{"address": "0x8a90cab2b38dba80c64b7734e58ee1db38b8992e", "chain": "ethereum"}],
        "fees": [
            {"fee": 0.005, "recipient": "opensea", "required": True},
            {"fee": 0.05, "recipient": "0xcreator", "required": True},
        ],
    }
    meta = parse_collection_metadata(raw)
    assert meta.slug == "doodles-official"
    assert meta.name == "Doodles"
    assert meta.created_date == "2021-10-17"
    assert meta.safelist_status == "verified"
    assert meta.total_supply == 10000
    assert len(meta.contracts) == 1
    assert meta.contracts[0].address == "0x8a90cab2b38dba80c64b7734e58ee1db38b8992e"
    assert len(meta.fees) == 2

def test_parse_collection_stats():
    raw = {
        "total": {
            "floor_price": 1.75,
            "floor_price_symbol": "ETH",
            "num_owners": 5100,
            "sales": 32000,
        },
        "intervals": [
            {"interval": "one_day", "sales": 4, "volume": 7.0, "volume_symbol": "ETH"},
            {"interval": "seven_days", "sales": 18, "volume": 31.5, "volume_symbol": "ETH"},
        ]
    }
    stats = parse_collection_stats(raw)
    assert stats.floor_price == 1.75
    assert stats.floor_price_symbol == "ETH"
    assert stats.num_owners == 5100
    assert stats.one_day_sales == 4
    assert stats.seven_day_sales == 18

def test_parse_sale_events():
    raw = {
        "asset_events": [
            {
                "event_type": "sale",
                "chain": "ethereum",
                "event_timestamp": 1690000000,
                "transaction": "0xabc123",
                "order_hash": "0xorder999",
                "nft": {"contract": "0xcontract", "identifier": "42"},
                "quantity": 1,
                "payment": {"quantity": "1500000000000000000", "decimals": 18, "symbol": "ETH"},
            },
            {
                "event_type": "transfer",  # Non-sale event, should be ignored
                "chain": "ethereum",
            }
        ]
    }
    events = parse_sale_events(raw)
    assert len(events) == 1
    assert events[0].price_value == 1.5
    assert events[0].token_id == "42"
    assert events[0].transaction == "0xabc123"
