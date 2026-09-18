import math
from typing import Optional, Dict, Any
from datetime import datetime, timezone
from ..providers.base import CollectionDataProvider
from ..models.collection import CollectionMetadata
from ..models.metrics import ListingMetrics, SalesMetrics, FloorPriceMetrics
from ..models.trade import TradeEconomics
from ..models.filters import FilterEvaluationReport, DataQualityState
from ..metrics.calculator import (
    compute_sales_metrics,
    compute_floor_metrics,
    compute_listing_metrics,
)
from ..trade_model.calculator import compute_trade_economics
from ..filters.engine import FilterEngine
from ..storage.state_store import StateStore
from ..storage.file_writer import write_candidate_info_md
from ..config.settings import BotConfig
from ..utils.time import (
    now_utc,
    now_local,
    current_daily_folder_name,
    get_seven_complete_calendar_days,
    get_calendar_day_utc_bounds,
)
from ..utils.logging import setup_logger

logger = setup_logger("collector_orchestrator")

class CollectionEvaluator:
    """
    Coordinates data collection, metric computation, and filter evaluation for a collection.
    Applies cheap-to-expensive early-exit short-circuiting to minimize unnecessary API calls.
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

    def evaluate_collection(
        self,
        slug: str,
        dry_run: bool = False,
        stop_on_first_failure: bool = True,
    ) -> Optional[FilterEvaluationReport]:
        """
        Evaluates a single collection.
        If stop_on_first_failure is True, halts early when a hard filter fails.
        """
        dt_utc = now_utc()
        tz_name = self.config.general.bot_timezone
        dt_local = now_local(tz_name)
        date_str = dt_local.strftime("%Y-%m-%d")

        logger.info("Evaluating collection: %s", slug)

        # -------------------------------------------------------------
        # STEP 1: Collection Metadata (Cheap API call)
        # -------------------------------------------------------------
        collection = self.provider.get_collection(slug)
        if not collection:
            logger.warning("[%s] Rejected: Collection details could not be retrieved.", slug)
            return None

        # Early check: Project Age
        if not collection.created_date:
            logger.info("[%s] Rejected: OpenSea created_date is missing.", slug)
            if stop_on_first_failure:
                return None
        else:
            try:
                created_dt = datetime.strptime(collection.created_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                age_days = (dt_utc - created_dt).total_seconds() / 86400.0
                if age_days <= self.config.filters.project_age.min_age_days:
                    logger.info("[%s] Rejected: OpenSea Collection Age = %.1f days (<= %.1f)", slug, age_days, self.config.filters.project_age.min_age_days)
                    if stop_on_first_failure:
                        return None
            except Exception as e:
                logger.warning("[%s] Could not parse created_date '%s': %s", slug, collection.created_date, e)
                if stop_on_first_failure:
                    return None

        # Early check: Verification Status
        actual_status = collection.safelist_status or "unknown"
        if actual_status not in self.config.filters.verification.required_status:
            logger.info("[%s] Rejected: Verification status is '%s' (required: %s)", slug, actual_status, self.config.filters.verification.required_status)
            if stop_on_first_failure:
                return None

        # Early check: Total Supply
        total_supply = collection.total_supply
        if total_supply is None or total_supply <= 0:
            logger.info("[%s] Rejected: Total supply is %s (must be > 0)", slug, total_supply)
            if stop_on_first_failure:
                return None

        # -------------------------------------------------------------
        # STEP 2: Active Listings (With Early-Exit Pagination Optimization)
        # -------------------------------------------------------------
        max_allowed_listings = math.floor(total_supply * (self.config.filters.listed_items.max_listed_pct / 100.0))
        listed_count, is_early_exit = self.provider.get_active_listings_count(
            slug=slug,
            early_exit_threshold=max_allowed_listings,
        )

        if listed_count is None:
            logger.warning("[%s] Failed to retrieve active listings count from API. Rejecting (fails closed).", slug)
            if stop_on_first_failure:
                return None

        listing_metrics = compute_listing_metrics(
            total_supply=total_supply,
            listed_items=listed_count,
            is_early_exit=is_early_exit,
        )

        if is_early_exit or (listing_metrics.listed_percentage is not None and listing_metrics.listed_percentage >= self.config.filters.listed_items.max_listed_pct):
            pct_display = f"{listing_metrics.listed_percentage:.2f}%" if listing_metrics.listed_percentage else "exceeded"
            logger.info("[%s] Rejected: Listed percentage %s (>= %.2f%%)", slug, pct_display, self.config.filters.listed_items.max_listed_pct)
            if stop_on_first_failure:
                return None

        # -------------------------------------------------------------
        # STEP 3: 7-Day Sales History & Trading Frequency
        # -------------------------------------------------------------
        seven_days = get_seven_complete_calendar_days(tz_name)
        start_ts, _ = get_calendar_day_utc_bounds(seven_days[0], tz_name)
        sale_events = self.provider.get_sale_events(slug, after_timestamp=start_ts)

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
            if stop_on_first_failure:
                return None
        else:
            sales_metrics = compute_sales_metrics(
                events=sale_events,
                tz_name=tz_name,
                has_sufficient_history=True,
            )

        # Early check: Trading Frequency
        cfg_tf = self.config.filters.trading_frequency
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
                "[%s] Rejected: Trading frequency = %.2f %s/day (> %.2f threshold)",
                slug, eval_trades_val, cfg_tf.sale_count_mode, cfg_tf.max_threshold
            )
            if stop_on_first_failure:
                return None

        # -------------------------------------------------------------
        # STEP 4: Floor Prices & Historical Changes
        # -------------------------------------------------------------
        stats = self.provider.get_collection_stats(slug)
        current_floor = stats.floor_price if stats else None
        floor_currency = stats.floor_price_symbol if stats else "ETH"

        floor_points_1d = self.provider.get_floor_price_history(slug, timeframe="one_day")
        floor_points_7d = self.provider.get_floor_price_history(slug, timeframe="seven_days")

        floor_metrics = compute_floor_metrics(
            current_floor=current_floor,
            floor_points_1d=floor_points_1d,
            floor_points_7d=floor_points_7d,
            currency=floor_currency,
        )

        if floor_metrics.change_1d_abs_pct is None or floor_metrics.change_1d_abs_pct >= self.config.filters.floor_change_1d.max_change_pct:
            chg_str = f"{floor_metrics.change_1d_abs_pct:.2f}%" if floor_metrics.change_1d_abs_pct is not None else "UNKNOWN"
            logger.info("[%s] Rejected: 1-Day floor change %s (>= %.2f%%)", slug, chg_str, self.config.filters.floor_change_1d.max_change_pct)
            if stop_on_first_failure:
                return None

        if floor_metrics.change_7d_abs_pct is None or floor_metrics.change_7d_abs_pct >= self.config.filters.floor_change_7d.max_change_pct:
            chg_str = f"{floor_metrics.change_7d_abs_pct:.2f}%" if floor_metrics.change_7d_abs_pct is not None else "UNKNOWN"
            logger.info("[%s] Rejected: 7-Day floor change %s (>= %.2f%%)", slug, chg_str, self.config.filters.floor_change_7d.max_change_pct)
            if stop_on_first_failure:
                return None

        # -------------------------------------------------------------
        # STEP 5: Top Offer & Trade Economics
        # -------------------------------------------------------------
        top_offer = self.provider.get_top_offer(slug)
        observed_top_offer_val = top_offer.price_value if top_offer else None
        top_offer_curr = top_offer.price_currency if top_offer else "WETH"

        trade_economics = compute_trade_economics(
            current_floor=current_floor,
            observed_top_offer=observed_top_offer_val,
            floor_currency=floor_currency,
            top_offer_currency=top_offer_curr,
            collection=collection,
            entry_offer_premium_pct=self.config.trade_model.entry_offer_premium_pct,
            target_sale_discount_from_floor_pct=self.config.trade_model.target_sale_discount_from_floor_pct,
            gas_estimate_eth=self.config.trade_model.gas_estimate_eth,
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

        if not report.is_overall_pass:
            logger.info("[%s] Filter Evaluation: FAIL. Reasons: %s", slug, "; ".join(report.rejection_reasons))
            self.state_store.record_candidate(slug, date_str, is_pass=False, reasons="; ".join(report.rejection_reasons))
            return report

        logger.info("[%s] ALL DETERMINISTIC FILTERS PASSED! (Candidate detected for %s)", slug, date_str)

        # -------------------------------------------------------------
        # STEP 7: Handoff Creation & Deduplication
        # -------------------------------------------------------------
        if self.state_store.is_candidate_recorded_today(slug, date_str):
            logger.info("[%s] Already recorded as candidate for today (%s). Skipping duplicate file creation.", slug, date_str)
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
            )
            logger.info("[%s] Successfully created candidate dossier: %s", slug, info_path)
            self.state_store.record_candidate(slug, date_str, is_pass=True, reasons="PASS")

        return report
