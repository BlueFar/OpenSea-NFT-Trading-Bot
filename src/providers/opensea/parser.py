from typing import Dict, Any, List, Optional, Tuple
from ...models.collection import (
    Contract,
    Fee,
    CollectionMetadata,
    CollectionStats,
    FloorPricePoint,
    SaleEvent,
    Listing,
    Offer,
)

# Seaport item types: 2 = ERC721, 3 = ERC1155, 4 = ERC721 with criteria, 5 = ERC1155 with criteria
NFT_ITEM_TYPES = (2, 3, 4, 5)

def parse_collection_metadata(data: Dict[str, Any]) -> CollectionMetadata:
    """Parses OpenSea collection details JSON response."""
    slug = data.get("collection") or data.get("slug") or ""
    name = data.get("name") or slug
    
    contracts = []
    for c in data.get("contracts", []):
        if isinstance(c, dict) and c.get("address"):
            contracts.append(Contract(
                address=c["address"],
                chain=c.get("chain", "ethereum"),
            ))

    fees = []
    for f in data.get("fees", []):
        if isinstance(f, dict) and "fee" in f:
            try:
                fees.append(Fee(
                    fee=float(f["fee"]),
                    recipient=f.get("recipient", ""),
                    required=bool(f.get("required", True)),
                ))
            except (ValueError, TypeError):
                pass

    total_supply = None
    if data.get("total_supply") is not None:
        try:
            total_supply = int(data["total_supply"])
        except (ValueError, TypeError):
            pass

    return CollectionMetadata(
        slug=slug,
        name=name,
        description=data.get("description"),
        created_date=data.get("created_date"),
        opensea_url=data.get("opensea_url") or f"https://opensea.io/collection/{slug}",
        project_url=data.get("project_url"),
        twitter_username=data.get("twitter_username"),
        discord_url=data.get("discord_url"),
        instagram_username=data.get("instagram_username"),
        telegram_url=data.get("telegram_url"),
        wiki_url=data.get("wiki_url"),
        safelist_status=data.get("safelist_status"),
        is_disabled=bool(data.get("is_disabled", False)),
        is_nsfw=bool(data.get("is_nsfw", False)),
        total_supply=total_supply,
        contracts=contracts,
        fees=fees,
        raw_data=data,
    )

def parse_collection_stats(data: Dict[str, Any]) -> CollectionStats:
    """Parses OpenSea collection stats JSON response."""
    total = data.get("total", {})
    floor_price = None
    if total.get("floor_price") is not None:
        try:
            floor_price = float(total["floor_price"])
        except (ValueError, TypeError):
            pass

    one_day_sales = None
    seven_day_sales = None
    for item in data.get("intervals", []):
        interval = item.get("interval")
        sales = item.get("sales")
        if interval == "one_day" and sales is not None:
            one_day_sales = int(sales)
        elif interval == "seven_days" and sales is not None:
            seven_day_sales = int(sales)

    return CollectionStats(
        floor_price=floor_price,
        floor_price_symbol=total.get("floor_price_symbol") or "ETH",
        num_owners=total.get("num_owners"),
        total_sales=total.get("sales"),
        one_day_sales=one_day_sales,
        seven_day_sales=seven_day_sales,
        raw_data=data,
    )

def parse_floor_price_history(data: Dict[str, Any]) -> List[FloorPricePoint]:
    """Parses time-series floor price points from OpenSea floor_prices response."""
    points: List[FloorPricePoint] = []
    raw_points = data.get("floor_prices", [])
    for p in raw_points:
        if not isinstance(p, dict) or "time" not in p or "token_unit" not in p:
            continue
        try:
            points.append(FloorPricePoint(
                time=int(p["time"]),
                token_unit=float(p["token_unit"]),
                symbol=p.get("symbol", "ETH"),
                chain=p.get("chain"),
                usd_price=p.get("usd_price"),
            ))
        except (ValueError, TypeError):
            pass
    return points

def parse_sale_events(data: Dict[str, Any]) -> List[SaleEvent]:
    """Parses sale events from OpenSea events collection response."""
    events: List[SaleEvent] = []
    raw_events = data.get("asset_events", [])
    for ev in raw_events:
        if not isinstance(ev, dict) or ev.get("event_type") != "sale":
            continue

        chain = ev.get("chain", "ethereum")
        ts = ev.get("event_timestamp") or ev.get("closing_date") or 0
        tx = ev.get("transaction")
        order_hash = ev.get("order_hash")
        
        nft_data = ev.get("nft") or {}
        contract = nft_data.get("contract")
        token_id = nft_data.get("identifier")
        
        # Quantity
        quantity = 1
        if ev.get("quantity") is not None:
            try:
                quantity = int(ev["quantity"])
            except (ValueError, TypeError):
                quantity = 1

        # Price
        price_val = None
        price_curr = "ETH"
        payment = ev.get("payment") or {}
        if "quantity" in payment:
            decimals = int(payment.get("decimals", 18))
            try:
                raw_qty = float(payment["quantity"])
                price_val = raw_qty / (10 ** decimals)
            except (ValueError, TypeError):
                pass
            price_curr = payment.get("symbol", "ETH")

        event_id = f"{chain}:{order_hash or tx}:{contract}:{token_id}:{ts}"

        events.append(SaleEvent(
            event_id=event_id,
            chain=chain,
            event_timestamp=int(ts),
            transaction=tx,
            order_hash=order_hash,
            contract_address=contract,
            token_id=token_id,
            quantity=quantity,
            seller=ev.get("seller"),
            buyer=ev.get("buyer"),
            price_value=price_val,
            price_currency=price_curr,
            protocol_address=ev.get("protocol_address"),
            token_standard=(nft_data.get("token_standard") or None),
        ))
    return events


def parse_order_info(data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Optional[str]]]:
    """
    How a sale happened, from GET /api/v2/orders/chain/{chain}/protocol/{protocol}/{order_hash}.
    The order's first offered item tells: coins (itemType 0/1) = a bid someone accepted, an NFT (2-5) = a listing
    someone bought. For bids, "criteria" says which kind: whole collection, trait, or null for one specific item.
    Returns {"kind": "listing" | "offer", "offer_type": "collection" | "trait" | "item" | None}, or None.
    """
    order = (data or {}).get("order")
    if not isinstance(order, dict):
        return None
    params = (order.get("protocol_data") or {}).get("parameters") or {}
    offer = params.get("offer") or []
    try:
        item_type = int(offer[0].get("itemType")) if offer and isinstance(offer[0], dict) else None
    except (TypeError, ValueError):
        item_type = None
    if item_type in (2, 3, 4, 5):
        return {"kind": "listing", "offer_type": None}
    if item_type not in (0, 1):
        return None

    crit = order.get("criteria")
    if isinstance(crit, dict):
        if crit.get("traits") or crit.get("numeric_traits") or crit.get("trait"):
            offer_type = "trait"
        elif crit.get("encoded_token_ids") in (None, "*"):
            offer_type = "collection"
        else:
            offer_type = "trait"  # a chosen set of items
    else:
        nft = next((c for c in params.get("consideration") or []
                    if isinstance(c, dict) and str(c.get("itemType")) in ("2", "3", "4", "5")), None)
        if nft is None:
            offer_type = None
        elif str(nft.get("itemType")) in ("2", "3"):
            offer_type = "item"
        else:
            offer_type = "collection" if str(nft.get("identifierOrCriteria")) == "0" else "trait"
    return {"kind": "offer", "offer_type": offer_type}


def parse_nft_rarity_rank(data: Optional[Dict[str, Any]]) -> Optional[int]:
    """OpenSea rarity rank from GET /api/v2/chain/{chain}/contract/{addr}/nfts/{id} (1 = rarest); None if none."""
    rarity = ((data or {}).get("nft") or {}).get("rarity")
    if not isinstance(rarity, dict):
        return None
    try:
        rank = int(rarity.get("rank"))
    except (TypeError, ValueError):
        return None
    return rank if rank > 0 else None

def parse_listings(data: Dict[str, Any]) -> Tuple[List[Listing], Optional[str]]:
    """Parses listings and returns (list_of_listings, next_cursor)."""
    listings: List[Listing] = []
    next_cursor = data.get("next")
    
    for item in data.get("listings", []):
        if not isinstance(item, dict):
            continue
        status = item.get("status", "ACTIVE")
        if status != "ACTIVE":
            continue

        order_hash = item.get("order_hash") or ""
        chain = item.get("chain", "ethereum")
        
        price_obj = item.get("price", {}).get("current", {})
        decimals = int(price_obj.get("decimals", 18))
        price_val = 0.0
        if "value" in price_obj:
            try:
                price_val = float(price_obj["value"]) / (10 ** decimals)
            except (ValueError, TypeError):
                pass

        currency = price_obj.get("currency", "ETH")
        remaining_qty = 1
        if item.get("remaining_quantity") is not None:
            try:
                remaining_qty = int(item["remaining_quantity"])
            except (ValueError, TypeError):
                pass

        # Identify the listed token so several listings of one NFT count once
        token_key = None
        offer_items = ((item.get("protocol_data") or {}).get("parameters") or {}).get("offer") or []
        nft_items = [o for o in offer_items if isinstance(o, dict) and o.get("itemType") in NFT_ITEM_TYPES]
        if nft_items:
            token_key = f"{str(nft_items[0].get('token', '')).lower()}:{nft_items[0].get('identifierOrCriteria')}"

        listings.append(Listing(
            order_hash=order_hash,
            chain=chain,
            price_value=price_val,
            price_currency=currency,
            status=status,
            remaining_quantity=remaining_qty,
            order_created_at=item.get("order_created_at"),
            token_key=token_key,
        ))

    return listings, next_cursor

def parse_offers(data: Dict[str, Any]) -> Tuple[List[Offer], Optional[str]]:
    """Parses offers and returns (list_of_offers, next_cursor)."""
    offers: List[Offer] = []
    next_cursor = data.get("next")

    for item in data.get("offers", []):
        if not isinstance(item, dict):
            continue
        status = item.get("status", "ACTIVE")
        if status != "ACTIVE":
            continue

        order_hash = item.get("order_hash") or ""
        chain = item.get("chain", "ethereum")
        
        price_obj = item.get("price", {})
        decimals = int(price_obj.get("decimals", 18))
        price_val = 0.0
        if "value" in price_obj:
            try:
                price_val = float(price_obj["value"]) / (10 ** decimals)
            except (ValueError, TypeError):
                pass

        # A collection offer for N items quotes the total price; convert to price per NFT
        consideration = ((item.get("protocol_data") or {}).get("parameters") or {}).get("consideration") or []
        nft_qty = 1
        for cons in consideration:
            if isinstance(cons, dict) and cons.get("itemType") in NFT_ITEM_TYPES:
                try:
                    nft_qty = max(1, int(cons.get("startAmount", 1)))
                except (ValueError, TypeError):
                    nft_qty = 1
                break
        price_val = price_val / nft_qty

        currency = price_obj.get("currency", "WETH")
        remaining_qty = 1
        if item.get("remaining_quantity") is not None:
            try:
                remaining_qty = int(item["remaining_quantity"])
            except (ValueError, TypeError):
                pass

        offers.append(Offer(
            order_hash=order_hash,
            chain=chain,
            price_value=price_val,
            price_currency=currency,
            status=status,
            remaining_quantity=remaining_qty,
            order_created_at=item.get("order_created_at"),
        ))

    return offers, next_cursor
