import math
import time
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timezone
from ..providers.base import CollectionDataProvider
from ..models.collection import CollectionMetadata, FloorPricePoint, SaleEvent
from ..models.metrics import ListingMetrics, SalesMetrics, FloorPriceMetrics
from ..models.trade import TradeEconomics
from ..models.filters import FilterEvaluationReport, FilterResultStatus, DataQualityState
from ..metrics.calculator import (
    compute_sales_metrics,
    compute_floor_metrics,
    compute_listing_metrics,
)
from ..trade_model.calculator import compute_trade_economics
from ..filters.engine import FilterEngine, sales_7d_count, too_few_sales_reason
from ..metrics.floor_sales import (
    classify_floor_sales, classify_offer_sales, too_few_floor_sales_reason, too_few_offer_sales_reason,
)
from ..utils.prices import prices_from_payment_tokens, usd_rate
from ..storage.state_store import StateStore
from ..storage.file_writer import write_candidate_info_md
from ..config.settings import BotConfig
from ..config.chains import currency_groups, same_currency, gas_estimate
from ..utils.notify import mac_notification
from ..utils.time import (
    now_utc,
    now_local,
    current_daily_folder_name,
    get_seven_complete_calendar_days,
    get_calendar_day_utc_bounds,
)
from ..utils.logging import setup_logger

logger = setup_logger("collector_orchestrator")

# Rules in the order the evaluator checks them, for the dashboard's "checked so far" lists
RULE_ORDER = [
    "project_age", "verification", "listed_items", "trading_frequency",
    "floor_change_1d", "floor_change_7d", "offer_to_floor", "net_profit",
]

# New OpenSea lookups allowed per check; answers are saved, so later checks only look up new sales
ORDER_LOOKUPS_PER_CHECK = 20
RARITY_LOOKUPS_PER_CHECK = 10
UNKNOWN_ORDER_RETRY_SECONDS = 86400       # an order OpenSea couldn't return is asked for again after a day
RARITY_REFRESH_SECONDS = 30 * 86400
MIN_SUPPLY_FOR_RARITY = 20                # rarity ranks mean little in a tiny collection


def _item_key(ev: SaleEvent) -> Optional[str]:
    if ev.chain and ev.contract_address and ev.token_id is not None:
        return f"{ev.chain}:{ev.contract_address.lower()}:{ev.token_id}"
    return None


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


class CollectionEvaluator:
    """
    Coordinates data collection, metric computation, and filter evaluation for a collection.
    Applies cheap-to-expensive early-exit short-circuiting to minimize unnecessary API calls.
    Rules switched off in Settings never cause an early exit; they are still measured where
    the data is fetched anyway.
    """

    def __init__(
        self,
        provider: CollectionDataProvider,
        state_store: StateStore,
        config: BotConfig,
    ):
        self.provider = provider
        self.state_store = state_store
        self.config = config
        self.filter_engine = FilterEngine(config.filters)
        self.last_details: Optional[Dict[str, Any]] = None  # details of the latest full evaluation
        self._prices_saved_at: Dict[str, float] = {}
        self._older_sales: Dict[str, Tuple[Tuple[int, int], List[SaleEvent]]] = {}
        try:  # dollar prices seen before, until this run sees fresh ones
            self._last_prices: Dict[str, float] = {k: v["usd"] for k, v in state_store.get_usd_prices().items()}
        except Exception:
            self._last_prices = {}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def rule_limits(self) -> Dict[str, Tuple[Optional[float], str, str]]:
        """rule -> (limit, kind, unit). kind: "max" (value must stay under), "min" (must reach), "bool"."""
        f = self.config.filters
        return {
            "project_age": (f.project_age.min_age_days, "min", "days"),
            "verification": (None, "bool", ""),
            "listed_items": (f.listed_items.max_listed_pct, "max", "%"),
            "trading_frequency": (f.trading_frequency.max_threshold, "max", "per day"),
            "floor_change_1d": (f.floor_change_1d.max_change_pct, "max", "%"),
            "floor_change_7d": (f.floor_change_7d.max_change_pct, "max", "%"),
            "offer_to_floor": (f.offer_to_floor.min_ratio_pct, "min", "%"),
            "net_profit": (f.net_profit.min_net_roi_pct, "min", "%"),
        }

    def _save_prices(self, prices: Dict[str, float]) -> None:
        """Saves OpenSea's dollar prices, at most once every 10 minutes per coin."""
        now = time.time()
        due = {k: v for k, v in prices.items() if now - self._prices_saved_at.get(k, 0) > 600}
        self._last_prices.update(prices)
        if due:
            try:
                self.state_store.set_usd_prices(due)
                for k in due:
                    self._prices_saved_at[k] = now
            except Exception as e:  # prices are a nice-to-have; never stop a check over them
                logger.debug("Could not save USD prices: %s", e)

    def _enabled(self, rule: str) -> bool:
        return self.filter_engine.is_enabled(rule)

    def _reject_early(self, slug: str, date_str: str, filter_name: str, reason: str,
                      ctx: Optional[Dict[str, Any]] = None, value: Any = None,
                      limit_info: Optional[tuple] = None, keep_shortlisted: bool = False) -> None:
        """Records an early-exit rejection so the status funnel shows which filter is the bottleneck."""
        details = dict(ctx or {})
        details["rule"] = filter_name
        details["value"] = _num(value) if value is not None and not isinstance(value, str) else value
        limit, kind, unit = limit_info or self.rule_limits().get(filter_name, (None, "", ""))
        details.update({"limit": limit, "kind": kind, "unit": unit, "reason": reason, "early_exit": True})
        self.state_store.record_candidate(
            slug, date_str, is_pass=False, reasons=f"{filter_name}: {reason}",
            reject_filter=filter_name, details=details,
        )
        if not keep_shortlisted and filter_name in ("project_age", "verification", "total_supply",
                                                    "listed_items", "trading_frequency"):
            self.state_store.set_shortlisted(slug, False)

    def _pace_limit(self, sales_metrics: SalesMetrics, value: Any) -> Tuple[Optional[float], str, str, Any]:
        """Which part of Trading pace to show: sales per day, sales this week, or sales at floor price."""
        cfg = self.config.filters.trading_frequency
        if sales_metrics.data_quality != DataQualityState.AVAILABLE:
            return cfg.max_threshold, "max", "per day", value
        pace = _num(value)
        if pace is not None and pace > cfg.max_threshold:
            return cfg.max_threshold, "max", "per day", value
        week = sales_7d_count(sales_metrics, cfg.sale_count_mode)
        if cfg.min_sales_7d > 0 and week < cfg.min_sales_7d:
            return cfg.min_sales_7d, "min", "sales in 7 days", week
        fs = sales_metrics.floor_sales
        if cfg.min_floor_sales_7d > 0 and fs is not None and fs.floor_sales < cfg.min_floor_sales_7d:
            return cfg.min_floor_sales_7d, "min", "floor sales in 7 days", fs.floor_sales
        os_ = sales_metrics.offer_sales
        if cfg.min_offer_sales_14d > 0 and os_ is not None and os_.offer_sales < cfg.min_offer_sales_14d:
            return cfg.min_offer_sales_14d, "min", "offer sales in 14 days", os_.offer_sales
        return cfg.max_threshold, "max", "per day", value

    def _sale_lookups(self, events: List[SaleEvent]):
        """
        How each sale happened and how rare the item is, for one check. Saved answers are used first;
        at most ORDER_LOOKUPS_PER_CHECK / RARITY_LOOKUPS_PER_CHECK new OpenSea calls are made.
        """
        orders = self.state_store.get_sale_orders(e.order_hash for e in events if e.order_hash)
        ranks = self.state_store.get_rarity(k for k in (_item_key(e) for e in events) if k)
        budget = {"orders": ORDER_LOOKUPS_PER_CHECK, "rarity": RARITY_LOOKUPS_PER_CHECK}
        get_order = getattr(self.provider, "get_order_info", None)
        get_rarity = getattr(self.provider, "get_nft_rarity", None)

        def order_info(ev: SaleEvent) -> Optional[Dict[str, Any]]:
            h = ev.order_hash
            if not h or not ev.protocol_address or not ev.chain or get_order is None:
                return None
            now = time.time()
            cached = orders.get(h)
            if cached and (cached["kind"] in ("listing", "offer")
                           or now - (cached["checked_at"] or 0) < UNKNOWN_ORDER_RETRY_SECONDS):
                return {"kind": cached["kind"], "offer_type": cached["offer_type"]} \
                    if cached["kind"] in ("listing", "offer") else None
            if budget["orders"] <= 0:
                return None
            budget["orders"] -= 1
            info = get_order(ev.chain, ev.protocol_address, h)
            info = info if isinstance(info, dict) else None
            kind = (info or {}).get("kind") or "unknown"
            offer_type = (info or {}).get("offer_type")
            self.state_store.set_sale_order(h, kind, offer_type)
            orders[h] = {"kind": kind, "offer_type": offer_type, "checked_at": int(now)}
            return info

        def rarity_rank(ev: SaleEvent) -> Optional[int]:
            key = _item_key(ev)
            if not key or (ev.token_standard or "").lower() == "erc1155" or get_rarity is None:
                return None  # ERC1155 ranks are over editions, not items, so "rarest 10%" doesn't fit
            now = time.time()
            cached = ranks.get(key)
            if cached:
                failed = cached["rank"] == -1  # OpenSea couldn't return the item last time: ask again after a day
                if now - (cached["checked_at"] or 0) < (UNKNOWN_ORDER_RETRY_SECONDS if failed else RARITY_REFRESH_SECONDS):
                    return None if failed else cached["rank"]
            if budget["rarity"] <= 0:
                return None
            budget["rarity"] -= 1
            res = get_rarity(ev.chain, ev.contract_address, ev.token_id)
            ok, rank = res if isinstance(res, tuple) and len(res) == 2 else (False, None)
            rank = rank if isinstance(rank, int) else None
            self.state_store.set_rarity(key, rank if ok else -1)
            ranks[key] = {"rank": rank if ok else -1, "checked_at": int(now)}
            return rank if ok else None

        return order_info, rarity_rank

    def _floor_reference_points(self, slug: str, now_ts: int, timeframe: str,
                                notes: Optional[List[str]] = None) -> List[FloorPricePoint]:
        """
        Returns the historical floor reference for the timeframe.
        Uses the bot's own snapshots first, then (after downtime such as a power cut) the nearest
        snapshot within the wider gap-fallback window, then the provider endpoint if configured.
        """
        fh = self.config.floor_history
        if timeframe == "one_day":
            target, tol, gap = now_ts - 86400, fh.one_day_tolerance_hours * 3600, fh.gap_fallback_1d_hours * 3600
        else:
            target, tol, gap = now_ts - 7 * 86400, fh.seven_day_tolerance_hours * 3600, fh.gap_fallback_7d_hours * 3600

        snap = self.state_store.get_floor_snapshot_near(slug, target, tol)
        if not snap and gap > tol:
            snap = self.state_store.get_floor_snapshot_near(slug, target, gap)
            if snap and notes is not None:
                hours = (now_ts - snap["ts"]) / 3600.0
                label = "1-day" if timeframe == "one_day" else "7-day"
                notes.append(
                    f"{label} floor change uses the bot's floor from {hours:.1f} hours ago "
                    f"(no snapshot inside the normal window, e.g. after the bot was offline)."
                )
        if snap:
            return [FloorPricePoint(time=snap["ts"], token_unit=snap["floor_price"], symbol=snap["currency"])]
        if fh.use_provider_endpoint:
            return self.provider.get_floor_price_history(slug, timeframe=timeframe)
        return []

    def _chain_of(self, slug: str, collection: CollectionMetadata) -> Optional[str]:
        if collection.contracts and collection.contracts[0].chain:
            return collection.contracts[0].chain
        return self.state_store.get_collection_chain(slug)

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------
    def evaluate_collection(
        self,
        slug: str,
        dry_run: bool = False,
        stop_on_first_failure: bool = True,
    ) -> Optional[FilterEvaluationReport]:
        """
        Evaluates a single collection.
        If stop_on_first_failure is True, halts early when a hard filter that is switched on fails.
        Raises OpenSeaNetworkError when OpenSea cannot be reached, so nothing is recorded as failed.
        """
        dt_utc = now_utc()
        tz_name = self.config.general.bot_timezone
        dt_local = now_local(tz_name)
        date_str = dt_local.strftime("%Y-%m-%d")
        filters = self.config.filters
        stop = stop_on_first_failure

        logger.info("Evaluating collection: %s", slug)

        # -------------------------------------------------------------
        # STEP 1: Collection Metadata (Cheap API call)
        # -------------------------------------------------------------
        collection = self.provider.get_collection(slug)
        if not collection:
            logger.warning("[%s] Rejected: Collection details could not be retrieved.", slug)
            if stop:
                self._reject_early(slug, date_str, "collection_fetch", "collection details could not be retrieved")
            return None

        token_prices = prices_from_payment_tokens((collection.raw_data or {}).get("payment_tokens"))
        if stop and token_prices:
            self._save_prices(token_prices)

        chain = self._chain_of(slug, collection)
        ctx: Dict[str, Any] = {
            "name": collection.name,
            "chain": chain,
            "image": (collection.raw_data or {}).get("image_url"),
            "checked": [],
        }
        if stop:
            self.state_store.set_collection_info(slug, chain=chain, safelist_status=collection.safelist_status)

        # Early check: Project Age
        if not collection.created_date:
            logger.info("[%s] Rejected: OpenSea created_date is missing.", slug)
            if stop and self._enabled("project_age"):
                self._reject_early(slug, date_str, "project_age", "created_date missing", ctx)
                return None
        else:
            try:
                created_dt = datetime.strptime(collection.created_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                age_days = (dt_utc - created_dt).total_seconds() / 86400.0
                if age_days <= filters.project_age.min_age_days:
                    logger.info("[%s] OpenSea Collection Age = %.1f days (<= %.1f)", slug, age_days, filters.project_age.min_age_days)
                    if stop and self._enabled("project_age"):
                        self._reject_early(slug, date_str, "project_age", f"{age_days:.1f} days", ctx, age_days)
                        return None
            except Exception as e:
                logger.warning("[%s] Could not parse created_date '%s': %s", slug, collection.created_date, e)
                if stop and self._enabled("project_age"):
                    self._reject_early(slug, date_str, "project_age", f"unparseable created_date '{collection.created_date}'", ctx)
                    return None
        ctx["checked"].append("project_age")

        # Early check: Verification Status
        actual_status = collection.safelist_status or "unknown"
        if actual_status not in filters.verification.required_status:
            logger.info("[%s] Verification status is '%s' (required: %s)", slug, actual_status, filters.verification.required_status)
            if stop and self._enabled("verification"):
                self._reject_early(slug, date_str, "verification", f"status '{actual_status}'", ctx, actual_status)
                return None
        ctx["checked"].append("verification")

        # Early check: Total Supply (needed for the listed share; always required)
        total_supply = collection.total_supply
        if total_supply is None or total_supply <= 0:
            logger.info("[%s] Rejected: Total supply is %s (must be > 0)", slug, total_supply)
            if stop:
                self._reject_early(slug, date_str, "total_supply", f"total supply {total_supply}", ctx)
                return None

        # -------------------------------------------------------------
        # STEP 2: Active Listings (With Early-Exit Pagination Optimization)
        # -------------------------------------------------------------
        max_allowed_listings = (
            math.floor(total_supply * (filters.listed_items.max_listed_pct / 100.0))
            if total_supply and self._enabled("listed_items") else None
        )
        listed_count, is_early_exit = self.provider.get_active_listings_count(
            slug=slug,
            early_exit_threshold=max_allowed_listings,
        )

        if listed_count is None:
            logger.warning("[%s] Failed to retrieve active listings count from API. Rejecting (fails closed).", slug)
            if stop and self._enabled("listed_items"):
                self._reject_early(slug, date_str, "listed_items", "listings API failed", ctx)
                return None

        listing_metrics = compute_listing_metrics(
            total_supply=total_supply,
            listed_items=listed_count,
            is_early_exit=is_early_exit,
        )

        if is_early_exit or (listing_metrics.listed_percentage is not None and listing_metrics.listed_percentage >= filters.listed_items.max_listed_pct):
            pct_display = f"{listing_metrics.listed_percentage:.2f}%" if listing_metrics.listed_percentage else "exceeded"
            logger.info("[%s] Listed percentage %s (>= %.2f%%)", slug, pct_display, filters.listed_items.max_listed_pct)
            if stop and self._enabled("listed_items"):
                ctx["listed"] = listed_count
                ctx["supply"] = total_supply
                self._reject_early(slug, date_str, "listed_items", f"listed {pct_display}", ctx,
                                   listing_metrics.listed_percentage)
                return None
        ctx["checked"].append("listed_items")

        # -------------------------------------------------------------
        # STEP 3: 7-Day Sales History & Trading Frequency
        # -------------------------------------------------------------
        cfg_tf = filters.trading_frequency
        seven_days = get_seven_complete_calendar_days(tz_name)
        start_ts, _ = get_calendar_day_utc_bounds(seven_days[0], tz_name)
        _, end_ts = get_calendar_day_utc_bounds(seven_days[-1], tz_name)
        stop_above = 7 * cfg_tf.max_threshold if (stop and self._enabled("trading_frequency")) else None
        sale_events = self.provider.get_sale_events(
            slug, after_timestamp=start_ts,
            stop_above=stop_above, count_before_ts=end_ts, count_mode=cfg_tf.sale_count_mode,
        )

        if sale_events is None:
            logger.warning("[%s] Failed to retrieve sale events from API. Failing closed with DATA_INSUFFICIENT.", slug)
            sales_metrics = SalesMetrics(
                seven_day_sales_items=0,
                seven_day_sales_transactions=0,
                average_sales_items_per_day=0.0,
                average_transactions_per_day=0.0,
                max_daily_sales_items=0,
                max_daily_transactions=0,
                min_daily_sales_items=0,
                min_daily_transactions=0,
                data_quality=DataQualityState.ERROR,
            )
            if stop and self._enabled("trading_frequency"):
                self._reject_early(slug, date_str, "trading_frequency", "sale events API failed", ctx)
                return None
        else:
            sales_metrics = compute_sales_metrics(
                events=sale_events,
                tz_name=tz_name,
                has_sufficient_history=True,
            )

        # Early check: Trading Frequency
        eval_trades_val = (
            sales_metrics.average_transactions_per_day
            if cfg_tf.sale_count_mode == "transactions"
            else sales_metrics.average_sales_items_per_day
        )
        if cfg_tf.metric == "max_daily_sales":
            eval_trades_val = float(
                sales_metrics.max_daily_transactions
                if cfg_tf.sale_count_mode == "transactions"
                else sales_metrics.max_daily_sales_items
            )

        if eval_trades_val > cfg_tf.max_threshold:
            logger.info(
                "[%s] Trading frequency = %.2f %s/day (> %.2f threshold)",
                slug, eval_trades_val, cfg_tf.sale_count_mode, cfg_tf.max_threshold
            )
            if stop and self._enabled("trading_frequency"):
                self._reject_early(slug, date_str, "trading_frequency", f"{eval_trades_val:.2f} {cfg_tf.sale_count_mode}/day",
                                   ctx, eval_trades_val)
                return None
        week_total = sales_7d_count(sales_metrics, cfg_tf.sale_count_mode)
        if sale_events is not None and cfg_tf.min_sales_7d > 0 and week_total < cfg_tf.min_sales_7d:
            logger.info("[%s] Only %d sales in 7 days (< %d minimum)", slug, week_total, cfg_tf.min_sales_7d)
            if stop and self._enabled("trading_frequency"):
                ctx["sales_7d"] = week_total
                self._reject_early(slug, date_str, "trading_frequency",
                                   too_few_sales_reason(week_total, cfg_tf.min_sales_7d), ctx, week_total,
                                   limit_info=(cfg_tf.min_sales_7d, "min", "sales in 7 days"))
                return None
        ctx["checked"].append("trading_frequency")

        # Passed the cheap structural filters: re-check hourly so floor history keeps building
        if stop:
            self.state_store.set_shortlisted(slug, True)

        # -------------------------------------------------------------
        # STEP 4: Floor Prices & Historical Changes
        # -------------------------------------------------------------
        stats = self.provider.get_collection_stats(slug)
        current_floor = stats.floor_price if stats else None
        floor_currency = stats.floor_price_symbol if stats else "ETH"

        now_ts = int(dt_utc.timestamp())
        data_notes: List[str] = []
        floor_points_1d = self._floor_reference_points(slug, now_ts, "one_day", data_notes)
        floor_points_7d = self._floor_reference_points(slug, now_ts, "seven_days", data_notes)
        if current_floor is not None and current_floor > 0:
            self.state_store.record_floor_snapshot(
                slug, now_ts, current_floor, floor_currency,
                retention_days=self.config.floor_history.snapshot_retention_days,
            )

        floor_metrics = compute_floor_metrics(
            current_floor=current_floor,
            floor_points_1d=floor_points_1d,
            floor_points_7d=floor_points_7d,
            currency=floor_currency,
        )

        if current_floor is None:
            logger.info("[%s] Rejected: current floor price unavailable.", slug)
            if stop:
                self._reject_early(slug, date_str, "floor_price", "current floor unavailable", ctx)
                return None

        for label, change, max_pct in (
            ("1d", floor_metrics.change_1d_abs_pct, filters.floor_change_1d.max_change_pct),
            ("7d", floor_metrics.change_7d_abs_pct, filters.floor_change_7d.max_change_pct),
        ):
            rule = f"floor_change_{label}"
            if not self._enabled(rule):
                continue
            if change is None:
                logger.info("[%s] Rejected: no floor reference from %s ago yet (bot floor history still building).", slug, label)
                if stop:
                    self._reject_early(slug, date_str, f"floor_history_{label}", f"no floor snapshot from {label} ago yet", ctx)
                    return None
            elif change >= max_pct:
                logger.info("[%s] Rejected: %s floor change %.2f%% (>= %.2f%%)", slug, label, change, max_pct)
                if stop:
                    self._reject_early(slug, date_str, rule, f"{change:.2f}%", ctx, change)
                    return None
            ctx["checked"].append(rule)

        # Trading pace, last part: were last week's sales bought at about the floor price, or were they
        # all accepted offers? Uses the sales already downloaded and the bot's own floor snapshots.
        groups = currency_groups(chain)
        lookups = None
        if sale_events is not None and current_floor:
            window = 6 * 3600
            lookups = self._sale_lookups(sale_events)
            floor_sales = classify_floor_sales(
                sale_events, start_ts, end_ts, current_floor, floor_currency, groups,
                self.state_store.get_floor_snapshots(slug, start_ts - window, end_ts + window),
                min_pct=cfg_tf.floor_sale_min_pct, max_pct=cfg_tf.floor_sale_max_pct,
                count_mode=cfg_tf.sale_count_mode,
                order_info=lookups[0], rarity_rank=lookups[1],
                supply=total_supply if (total_supply or 0) >= MIN_SUPPLY_FOR_RARITY else None,
                rare_pct=cfg_tf.rare_item_pct,
            )
            sales_metrics.floor_sales = floor_sales
            need = cfg_tf.min_floor_sales_7d
            ctx["floor_sales"] = floor_sales.summary(need)
            if need > 0 and floor_sales.floor_sales < need:
                logger.info("[%s] %s", slug, floor_sales.breakdown())
                if stop and self._enabled("trading_frequency"):
                    if "trading_frequency" in ctx["checked"]:
                        ctx["checked"].remove("trading_frequency")
                    # Stays shortlisted: hourly floor snapshots keep building, so a floor sale later counts
                    self._reject_early(slug, date_str, "trading_frequency",
                                       too_few_floor_sales_reason(floor_sales, need), ctx, floor_sales.floor_sales,
                                       limit_info=(need, "min", "floor sales in 7 days"), keep_shortlisted=True)
                    return None

        # Trading pace, the other side: did any seller accept a collection offer (the bid this strategy
        # places) in the last 14 days? The week before the 7 days above is downloaded only when needed.
        need_os = cfg_tf.min_offer_sales_14d
        if sale_events is not None and current_floor and need_os > 0:
            window = 6 * 3600
            start14 = start_ts - 7 * 86400
            events14 = list(sale_events)

            def offer_sales_in(evs):
                return classify_offer_sales(
                    evs, start14, end_ts, current_floor, floor_currency, groups,
                    self.state_store.get_floor_snapshots(slug, start14 - window, end_ts + window),
                    min_pct=cfg_tf.floor_sale_min_pct, max_pct=cfg_tf.floor_sale_max_pct,
                    count_mode=cfg_tf.sale_count_mode, order_info=lookups[0],
                )

            offer_sales = offer_sales_in(events14)
            older_failed = False
            if offer_sales.offer_sales < need_os:
                # Sales from 8-14 days ago don't change, so they are downloaded once a day per collection
                cached = self._older_sales.get(slug)
                if cached and cached[0] == (start14, start_ts):
                    older = cached[1]
                else:
                    older = self.provider.get_sale_events(slug, after_timestamp=start14, before_timestamp=start_ts,
                                                          max_pages=5)
                    if isinstance(older, list):
                        if len(self._older_sales) > 5000:
                            self._older_sales.clear()
                        self._older_sales[slug] = ((start14, start_ts), older)
                if not isinstance(older, list):
                    older_failed = True
                else:
                    events14 = older + events14
                    offer_sales = offer_sales_in(events14)
            sales_metrics.offer_sales = offer_sales
            ctx["offer_sales"] = offer_sales.summary(need_os)
            if offer_sales.offer_sales < need_os:
                reason = ("sale events API failed (8-14 days ago)" if older_failed
                          else too_few_offer_sales_reason(offer_sales, need_os))
                logger.info("[%s] %s", slug, reason)
                if stop and self._enabled("trading_frequency"):
                    if "trading_frequency" in ctx["checked"]:
                        ctx["checked"].remove("trading_frequency")
                    # A failed download is not a near miss: no limit is shown for it
                    limit_info = (None, "", "") if older_failed else (need_os, "min", "offer sales in 14 days")
                    self._reject_early(slug, date_str, "trading_frequency", reason, ctx,
                                       None if older_failed else offer_sales.offer_sales,
                                       limit_info=limit_info, keep_shortlisted=True)
                    return None

        # -------------------------------------------------------------
        # STEP 5: Top Offer & Trade Economics
        # -------------------------------------------------------------
        top_offer = self.provider.get_top_offer(
            slug, currency_filter=lambda cur: same_currency(cur, floor_currency, groups)
        )
        observed_top_offer_val = top_offer.price_value if top_offer else None
        top_offer_curr = top_offer.price_currency if top_offer else "WETH"
        gas = gas_estimate(
            chain, floor_currency,
            overrides=self.config.trade_model.chain_gas,
            ethereum_default=self.config.trade_model.gas_estimate_eth,
        )

        trade_economics = compute_trade_economics(
            current_floor=current_floor,
            observed_top_offer=observed_top_offer_val,
            floor_currency=floor_currency,
            top_offer_currency=top_offer_curr,
            collection=collection,
            entry_offer_premium_pct=self.config.trade_model.entry_offer_premium_pct,
            target_sale_discount_from_floor_pct=self.config.trade_model.target_sale_discount_from_floor_pct,
            gas_estimate_eth=gas,
            fallback_marketplace_fee_pct=self.config.trade_model.marketplace_fee_pct,
            currency_groups=groups,
        )

        # -------------------------------------------------------------
        # STEP 6: Run Full Filter Engine
        # -------------------------------------------------------------
        report = self.filter_engine.evaluate(
            collection=collection,
            sales_metrics=sales_metrics,
            floor_metrics=floor_metrics,
            listing_metrics=listing_metrics,
            trade_economics=trade_economics,
            detection_dt_utc=dt_utc,
        )

        details = self._full_details(ctx, collection, stats, listing_metrics, sales_metrics,
                                     floor_metrics, trade_economics, report, data_notes)
        # Dollar rates at the time of the check, so the dashboard can show "worth $X when found"
        rates = dict(self._last_prices)
        rates.update(token_prices)
        details["usd_rate"] = usd_rate(details.get("currency"), rates)
        details["offer_usd_rate"] = usd_rate(details.get("offer_currency") or details.get("currency"), rates)
        self.last_details = details

        if not report.is_overall_pass:
            logger.info("[%s] Filter Evaluation: FAIL. Reasons: %s", slug, "; ".join(report.rejection_reasons))
            failed = [name for name, c in report.criteria.items()
                      if c.result in (FilterResultStatus.FAIL, FilterResultStatus.DATA_INSUFFICIENT)]
            first_failed = failed[0] if failed else None
            if first_failed:
                limit, kind, unit = self.rule_limits().get(first_failed, (None, "", ""))
                value = _num(report.criteria[first_failed].actual_value)
                if first_failed == "trading_frequency":
                    limit, kind, unit, value = self._pace_limit(sales_metrics, value)
                details.update({"rule": first_failed, "value": value,
                                "limit": limit, "kind": kind, "unit": unit, "failed_rules": failed})
            if stop:
                self.state_store.record_candidate(
                    slug, date_str, is_pass=False,
                    reasons="; ".join(report.rejection_reasons),
                    reject_filter=first_failed,
                    details=details,
                )
            return report

        logger.info("[%s] ALL DETERMINISTIC FILTERS PASSED! (Candidate detected for %s)", slug, date_str)

        # -------------------------------------------------------------
        # STEP 7: Handoff Creation & Deduplication
        # -------------------------------------------------------------
        if self.state_store.is_candidate_recorded_today(slug, date_str):
            logger.info("[%s] Already recorded as candidate for today (%s). Skipping duplicate file creation.", slug, date_str)
            if stop:
                self.state_store.set_last_result_pass(slug)  # passing again after a failed check earlier today
            return report

        if dry_run:
            logger.info("[DRY-RUN] [%s] Would write candidate dossier to %s/%s/Info.md", slug, date_str, slug)
        else:
            info_path = write_candidate_info_md(
                data_root=self.config.general.data_root,
                date_str=date_str,
                collection=collection,
                sales_metrics=sales_metrics,
                floor_metrics=floor_metrics,
                listing_metrics=listing_metrics,
                trade_economics=trade_economics,
                filter_report=report,
                detection_dt_utc=dt_utc,
                detection_dt_local=dt_local,
                tz_name=tz_name,
                num_owners=stats.num_owners if stats else None,
                data_notes=data_notes,
            )
            logger.info("[%s] Successfully created candidate dossier: %s", slug, info_path)
            details["info_md"] = info_path
            details["found_at"] = dt_local.isoformat()
            self.state_store.record_candidate(slug, date_str, is_pass=True, reasons="PASS", details=details)
            mod = trade_economics.modelled
            spread_txt = f" Spread {mod.floor_premium_over_effective_offer_pct:.0f}%." \
                if mod.floor_premium_over_effective_offer_pct is not None else ""
            self.state_store.log_event("candidate", f"New candidate: {collection.name}.{spread_txt}")
            self._notify_candidate(collection, trade_economics)

        return report

    def _notify_candidate(self, collection: CollectionMetadata, te: TradeEconomics) -> None:
        rt = self.config.runtime
        if not rt.notify_new_candidates:
            return
        mod = te.modelled
        spread = mod.floor_premium_over_effective_offer_pct
        parts = []
        if spread is not None:
            parts.append(f"Spread {spread:.0f}%")
        if mod.estimated_net_profit is not None:
            parts.append(f"about {mod.estimated_net_profit:.4g} {te.currency} profit")
        mac_notification(f"New candidate: {collection.name}", " · ".join(parts) or "Passed every rule", rt.notification_sound)

    def _full_details(self, ctx, collection, stats, listing_metrics: ListingMetrics, sales_metrics: SalesMetrics,
                      floor_metrics: FloorPriceMetrics, te: TradeEconomics, report: FilterEvaluationReport,
                      data_notes: List[str]) -> Dict[str, Any]:
        """Everything the dashboard needs to draw a candidate card, its side panel and near misses."""
        mod, obs, asm = te.modelled, te.observed, te.assumptions
        limits = self.rule_limits()
        rules = {}
        for name, c in report.criteria.items():
            limit, kind, unit = limits.get(name, (None, "", ""))
            value = _num(c.actual_value) if _num(c.actual_value) is not None else str(c.actual_value)
            if name == "trading_frequency":
                limit, kind, unit, value = self._pace_limit(sales_metrics, value)
            rules[name] = {
                "result": c.result.value,
                "enabled": self._enabled(name),
                "value": value,
                "limit": limit, "kind": kind, "unit": unit, "note": c.notes,
            }
        return {
            **ctx,
            "checked": list(RULE_ORDER),
            "early_exit": False,
            "currency": te.currency,
            "floor": floor_metrics.current_floor,
            "floor_1d": floor_metrics.floor_1d_ago,
            "floor_7d": floor_metrics.floor_7d_ago,
            "move_1d": floor_metrics.change_1d_signed_pct,
            "move_7d": floor_metrics.change_7d_signed_pct,
            "offer": obs.observed_top_offer,
            "offer_currency": obs.top_offer_currency,
            "spread": mod.floor_premium_over_effective_offer_pct,
            "roi": mod.estimated_roi_pct,
            "net": mod.estimated_net_profit,
            "entry": mod.modelled_entry_offer,
            "exit": mod.target_exit_price,
            "fee_pct": obs.marketplace_fee_pct,
            "fee_source": obs.marketplace_fee_source,
            "fee_amount": mod.estimated_marketplace_fee,
            "royalty_pct": obs.creator_royalty_pct,
            "royalty_amount": mod.estimated_creator_royalty,
            "gas": asm.gas_estimate_eth,
            "bid_premium_pct": asm.entry_offer_premium_pct,
            "sell_discount_pct": asm.target_sale_discount_from_floor_pct,
            "economics_note": mod.status_note,
            "supply": collection.total_supply,
            "owners": stats.num_owners if stats else None,
            "listed": listing_metrics.listed_items,
            "listed_pct": listing_metrics.listed_percentage,
            "sales_daily": [[d.date_str, d.sales_transactions] for d in sales_metrics.daily_breakdown],
            "sales_7d": sales_metrics.seven_day_sales_transactions,
            "pace": sales_metrics.average_transactions_per_day,
            "created_date": collection.created_date,
            "verified": collection.safelist_status == "verified",
            "opensea_url": collection.opensea_url or f"https://opensea.io/collection/{collection.slug}",
            "rules": rules,
            "data_notes": data_notes,
        }
