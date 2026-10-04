from datetime import datetime, timezone
from typing import Optional, List
from ..models.collection import CollectionMetadata
from ..models.metrics import SalesMetrics, FloorPriceMetrics, ListingMetrics
from ..models.trade import TradeEconomics
from ..models.filters import (
    DataQualityState,
    FilterResultStatus,
    FilterCriterionResult,
    FilterEvaluationReport,
)
from ..config.settings import FiltersConfig

class FilterEngine:
    """Evaluates candidate collections against deterministic quantitative filters."""

    def __init__(self, config: FiltersConfig):
        self.config = config

    def evaluate(
        self,
        collection: CollectionMetadata,
        sales_metrics: SalesMetrics,
        floor_metrics: FloorPriceMetrics,
        listing_metrics: ListingMetrics,
        trade_economics: TradeEconomics,
        detection_dt_utc: datetime,
    ) -> FilterEvaluationReport:
        report = FilterEvaluationReport(
            collection_slug=collection.slug,
            is_overall_pass=True,
        )
        now_iso = detection_dt_utc.isoformat()

        # 1. Project Age Filter (OpenSea Collection Age > 60 days)
        age_criterion = self._eval_project_age(collection, detection_dt_utc, now_iso)
        report.add_criterion(age_criterion)

        # 2. OpenSea Verification Filter (safelist_status in required_status)
        verif_criterion = self._eval_verification(collection, now_iso)
        report.add_criterion(verif_criterion)

        # 3. Listed Items Filter (< 6.0%)
        listed_criterion = self._eval_listed_items(listing_metrics, now_iso)
        report.add_criterion(listed_criterion)

        # 4. Trading Frequency Filter (<= 2 trades/day over 7 complete calendar days)
        sales_criterion = self._eval_trading_frequency(sales_metrics, now_iso)
        report.add_criterion(sales_criterion)

        # 5. 1-Day Floor Price Change (< 8.0%)
        floor_1d_criterion = self._eval_floor_change_1d(floor_metrics, now_iso)
        report.add_criterion(floor_1d_criterion)

        # 6. 7-Day Floor Price Change (< 10.0%)
        floor_7d_criterion = self._eval_floor_change_7d(floor_metrics, now_iso)
        report.add_criterion(floor_7d_criterion)

        # 7. Top Offer vs Floor (Advisory / Observe-Only unless enabled in config)
        offer_criterion = self._eval_offer_to_floor(trade_economics, now_iso)
        report.add_criterion(offer_criterion)

        # 8. Minimum Net Profit (modelled ROI after fees, royalty and gas)
        report.add_criterion(self._eval_net_profit(trade_economics, now_iso))

        # Rules switched off in Settings are still measured but never block (OBSERVE)
        for name, crit in report.criteria.items():
            if not self.is_enabled(name) and crit.result != FilterResultStatus.OBSERVE:
                would = crit.result.value
                crit.result = FilterResultStatus.OBSERVE
                crit.notes = f"Switched off (would have been {would}). " + (crit.notes or "")
        report.rejection_reasons = [
            r for r in report.rejection_reasons if self.is_enabled(r.split(":", 1)[0])
        ]

        # Determine overall pass/fail
        # Must pass all blocking criteria (PASS or OBSERVE). Any FAIL or DATA_INSUFFICIENT fails the candidate.
        blocking_results = [
            c.result for c in report.criteria.values()
            if c.result != FilterResultStatus.OBSERVE
        ]
        report.is_overall_pass = all(r == FilterResultStatus.PASS for r in blocking_results)

        return report

    def is_enabled(self, rule: str) -> bool:
        cfg = getattr(self.config, rule, None)
        return bool(getattr(cfg, "enabled", True)) if cfg is not None else True

    def _eval_project_age(
        self,
        collection: CollectionMetadata,
        detection_dt_utc: datetime,
        now_iso: str,
    ) -> FilterCriterionResult:
        threshold = f"> {self.config.project_age.min_age_days:.1f} days (OpenSea Collection Age)"
        formula = "detection_date - opensea_created_date"
        source = "GET /api/v2/collections/{slug} -> created_date"

        if not collection.created_date:
            return FilterCriterionResult(
                name="project_age",
                threshold=threshold,
                actual_value="UNKNOWN",
                unit=" days",
                formula=formula,
                result=FilterResultStatus.DATA_INSUFFICIENT,
                data_quality=DataQualityState.MISSING,
                timestamp=now_iso,
                source=source,
                notes="OpenSea created_date is missing or could not be determined.",
            )

        try:
            created_dt = datetime.strptime(collection.created_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            age_days = (detection_dt_utc - created_dt).total_seconds() / 86400.0
        except Exception as e:
            return FilterCriterionResult(
                name="project_age",
                threshold=threshold,
                actual_value="ERROR",
                unit=" days",
                formula=formula,
                result=FilterResultStatus.DATA_INSUFFICIENT,
                data_quality=DataQualityState.ERROR,
                timestamp=now_iso,
                source=source,
                notes=f"Failed to parse created_date '{collection.created_date}': {str(e)}",
            )

        is_pass = age_days > self.config.project_age.min_age_days
        return FilterCriterionResult(
            name="project_age",
            threshold=threshold,
            actual_value=round(age_days, 2),
            unit=" days",
            formula=formula,
            result=FilterResultStatus.PASS if is_pass else FilterResultStatus.FAIL,
            data_quality=DataQualityState.AVAILABLE,
            timestamp=now_iso,
            source=source,
            notes=f"OpenSea Collection Age is {age_days:.2f} days (calculated from OpenSea created_date, not off-chain project founding date).",
        )

    def _eval_verification(self, collection: CollectionMetadata, now_iso: str) -> FilterCriterionResult:
        threshold = f"in {self.config.verification.required_status}"
        formula = "safelist_status in required_status"
        source = "GET /api/v2/collections/{slug} -> safelist_status"

        actual_status = collection.safelist_status or "unknown"
        if actual_status == "unknown":
            return FilterCriterionResult(
                name="verification",
                threshold=threshold,
                actual_value=actual_status,
                unit="",
                formula=formula,
                result=FilterResultStatus.DATA_INSUFFICIENT,
                data_quality=DataQualityState.MISSING,
                timestamp=now_iso,
                source=source,
                notes="Verification status unknown (fails closed).",
            )

        is_pass = actual_status in self.config.verification.required_status
        return FilterCriterionResult(
            name="verification",
            threshold=threshold,
            actual_value=actual_status,
            unit="",
            formula=formula,
            result=FilterResultStatus.PASS if is_pass else FilterResultStatus.FAIL,
            data_quality=DataQualityState.AVAILABLE,
            timestamp=now_iso,
            source=source,
            notes=f"Blue tick verified: {'Yes' if is_pass else 'No'} (state: {actual_status}).",
        )

    def _eval_listed_items(self, listing_metrics: ListingMetrics, now_iso: str) -> FilterCriterionResult:
        threshold = f"< {self.config.listed_items.max_listed_pct:.2f}%"
        formula = "(listed_items / total_supply) * 100"
        source = "GET /api/v2/listings/collection/{slug}/all & stats total_supply"

        if listing_metrics.is_early_exit_exceeded:
            return FilterCriterionResult(
                name="listed_items",
                threshold=threshold,
                actual_value=round(listing_metrics.listed_percentage or 6.01, 2),
                unit="%",
                formula=formula,
                result=FilterResultStatus.FAIL,
                data_quality=DataQualityState.AVAILABLE,
                timestamp=now_iso,
                source=source,
                notes="Early exit: active listings exceeded max permitted count during pagination.",
            )

        if listing_metrics.data_quality != DataQualityState.AVAILABLE or listing_metrics.listed_percentage is None:
            return FilterCriterionResult(
                name="listed_items",
                threshold=threshold,
                actual_value="UNKNOWN",
                unit="%",
                formula=formula,
                result=FilterResultStatus.DATA_INSUFFICIENT,
                data_quality=listing_metrics.data_quality,
                timestamp=now_iso,
                source=source,
                notes="Total supply or active listings count unavailable.",
            )

        is_pass = listing_metrics.listed_percentage < self.config.listed_items.max_listed_pct
        return FilterCriterionResult(
            name="listed_items",
            threshold=threshold,
            actual_value=round(listing_metrics.listed_percentage, 2),
            unit="%",
            formula=formula,
            result=FilterResultStatus.PASS if is_pass else FilterResultStatus.FAIL,
            data_quality=DataQualityState.AVAILABLE,
            timestamp=now_iso,
            source=source,
            notes=f"Listed: {listing_metrics.listed_items} / {listing_metrics.total_supply} ({listing_metrics.listed_percentage:.2f}%).",
        )

    def _eval_trading_frequency(self, sales_metrics: SalesMetrics, now_iso: str) -> FilterCriterionResult:
        cfg = self.config.trading_frequency
        threshold = f"<= {cfg.max_threshold:.2f} {cfg.sale_count_mode}/day ({cfg.metric})"
        formula = f"{cfg.metric} over 7 complete calendar days"
        source = "GET /api/v2/events/collection/{slug}?event_type=sale"

        if sales_metrics.data_quality != DataQualityState.AVAILABLE:
            return FilterCriterionResult(
                name="trading_frequency",
                threshold=threshold,
                actual_value="INSUFFICIENT_HISTORY",
                unit=" trades/day",
                formula=formula,
                result=FilterResultStatus.DATA_INSUFFICIENT,
                data_quality=sales_metrics.data_quality,
                timestamp=now_iso,
                source=source,
                notes="Could not obtain 7 complete calendar days of sale history.",
            )

        # Select value based on sale_count_mode (transactions vs item_quantity)
        if cfg.sale_count_mode == "transactions":
            avg_val = sales_metrics.average_transactions_per_day
            max_val = sales_metrics.max_daily_transactions
        else:
            avg_val = sales_metrics.average_sales_items_per_day
            max_val = sales_metrics.max_daily_sales_items

        if cfg.metric == "average_daily_sales":
            eval_val = avg_val
            is_pass = eval_val <= cfg.max_threshold
        elif cfg.metric == "max_daily_sales":
            eval_val = float(max_val)
            is_pass = eval_val <= cfg.max_threshold
        else:  # both
            eval_val = avg_val
            is_pass = (avg_val <= cfg.max_threshold) and (max_val <= cfg.max_threshold)

        week_total = sales_7d_count(sales_metrics, cfg.sale_count_mode)
        notes = (f"Avg={avg_val:.2f}, Max={max_val}, 7d_total={sales_metrics.seven_day_sales_transactions} "
                 f"txs across 7 complete calendar days.")
        if cfg.min_sales_7d > 0:
            threshold += f" and >= {cfg.min_sales_7d} sales in 7 days"
            if week_total < cfg.min_sales_7d:
                is_pass = False
                notes = too_few_sales_reason(week_total, cfg.min_sales_7d) + ". " + notes

        return FilterCriterionResult(
            name="trading_frequency",
            threshold=threshold,
            actual_value=round(eval_val, 2),
            unit=f" {cfg.sale_count_mode}/day",
            formula=formula,
            result=FilterResultStatus.PASS if is_pass else FilterResultStatus.FAIL,
            data_quality=DataQualityState.AVAILABLE,
            timestamp=now_iso,
            source=source,
            notes=notes,
        )

    def _eval_floor_change_1d(self, floor_metrics: FloorPriceMetrics, now_iso: str) -> FilterCriterionResult:
        threshold = f"< {self.config.floor_change_1d.max_change_pct:.2f}%"
        formula = "abs((current_floor - floor_24h_ago) / floor_24h_ago) * 100"
        source = "GET /api/v2/collections/{slug}/floor_prices?timeframe=one_day"

        if floor_metrics.change_1d_abs_pct is None:
            return FilterCriterionResult(
                name="floor_change_1d",
                threshold=threshold,
                actual_value="UNKNOWN",
                unit="%",
                formula=formula,
                result=FilterResultStatus.DATA_INSUFFICIENT,
                data_quality=floor_metrics.data_quality,
                timestamp=now_iso,
                source=source,
                notes="Historical 1-day floor price data unavailable.",
            )

        is_pass = floor_metrics.change_1d_abs_pct < self.config.floor_change_1d.max_change_pct
        signed_str = f"{floor_metrics.change_1d_signed_pct:+.2f}%" if floor_metrics.change_1d_signed_pct is not None else "N/A"
        abs_str = f"{floor_metrics.change_1d_abs_pct:.2f}%" if floor_metrics.change_1d_abs_pct is not None else "N/A"
        return FilterCriterionResult(
            name="floor_change_1d",
            threshold=threshold,
            actual_value=round(floor_metrics.change_1d_abs_pct, 2),
            unit="%",
            formula=formula,
            result=FilterResultStatus.PASS if is_pass else FilterResultStatus.FAIL,
            data_quality=DataQualityState.AVAILABLE,
            timestamp=now_iso,
            source=source,
            notes=f"Signed change: {signed_str}, Absolute: {abs_str}.",
        )

    def _eval_floor_change_7d(self, floor_metrics: FloorPriceMetrics, now_iso: str) -> FilterCriterionResult:
        threshold = f"< {self.config.floor_change_7d.max_change_pct:.2f}%"
        formula = "abs((current_floor - floor_7d_ago) / floor_7d_ago) * 100"
        source = "GET /api/v2/collections/{slug}/floor_prices?timeframe=seven_days"

        if floor_metrics.change_7d_abs_pct is None:
            return FilterCriterionResult(
                name="floor_change_7d",
                threshold=threshold,
                actual_value="UNKNOWN",
                unit="%",
                formula=formula,
                result=FilterResultStatus.DATA_INSUFFICIENT,
                data_quality=floor_metrics.data_quality,
                timestamp=now_iso,
                source=source,
                notes="Historical 7-day floor price data unavailable.",
            )

        is_pass = floor_metrics.change_7d_abs_pct < self.config.floor_change_7d.max_change_pct
        signed_7d_str = f"{floor_metrics.change_7d_signed_pct:+.2f}%" if floor_metrics.change_7d_signed_pct is not None else "N/A"
        abs_7d_str = f"{floor_metrics.change_7d_abs_pct:.2f}%" if floor_metrics.change_7d_abs_pct is not None else "N/A"
        return FilterCriterionResult(
            name="floor_change_7d",
            threshold=threshold,
            actual_value=round(floor_metrics.change_7d_abs_pct, 2),
            unit="%",
            formula=formula,
            result=FilterResultStatus.PASS if is_pass else FilterResultStatus.FAIL,
            data_quality=DataQualityState.AVAILABLE,
            timestamp=now_iso,
            source=source,
            notes=f"Signed change: {signed_7d_str}, Absolute: {abs_7d_str}.",
        )

    OFFER_TO_FLOOR_FORMULAS = {
        "floor_premium_over_effective_offer": (
            "floor_premium_over_effective_offer_pct",
            "(current_floor - effective_entry_cost) / effective_entry_cost * 100, "
            "effective_entry_cost = modelled_entry_offer + royalty on target exit price",
            "Floor is {v:.2f}% above the modelled entry offer including royalty.",
        ),
        "floor_spread_to_entry_offer": (
            "floor_spread_to_entry_offer_pct",
            "(current_floor - modelled_entry_offer) / modelled_entry_offer * 100",
            "Floor is {v:.2f}% above the modelled entry offer.",
        ),
        "modelled_entry_offer_to_floor": (
            "entry_offer_to_floor_ratio_pct",
            "modelled_entry_offer / current_floor * 100",
            "Modelled entry offer is {v:.2f}% of floor.",
        ),
    }

    def _eval_offer_to_floor(self, trade_economics: TradeEconomics, now_iso: str) -> FilterCriterionResult:
        cfg = self.config.offer_to_floor
        threshold = f">= {cfg.min_ratio_pct:.2f}%" if cfg.enabled else "ADVISORY / OBSERVE ONLY"
        if cfg.formula not in self.OFFER_TO_FLOOR_FORMULAS:
            raise ValueError(f"Unknown offer_to_floor formula '{cfg.formula}'")
        field_name, formula, note_tmpl = self.OFFER_TO_FLOOR_FORMULAS[cfg.formula]
        source = "GET /api/v2/offers/collection/{slug} (collection offers) & stats floor"

        ratio = getattr(trade_economics.modelled, field_name)
        actual_val_str = f"{ratio:.2f}" if ratio is not None else "UNKNOWN"

        if not cfg.enabled:
            return FilterCriterionResult(
                name="offer_to_floor",
                threshold=threshold,
                actual_value=actual_val_str,
                unit="%",
                formula=formula,
                result=FilterResultStatus.OBSERVE,
                data_quality=DataQualityState.AVAILABLE if ratio is not None else DataQualityState.MISSING,
                timestamp=now_iso,
                source=source,
                notes="Advisory ratio recorded for evaluation; not blocking candidate decision.",
            )

        if ratio is None:
            return FilterCriterionResult(
                name="offer_to_floor",
                threshold=threshold,
                actual_value="UNKNOWN",
                unit="%",
                formula=formula,
                result=FilterResultStatus.DATA_INSUFFICIENT,
                data_quality=DataQualityState.MISSING,
                timestamp=now_iso,
                source=source,
                notes="Missing top offer or floor price.",
            )

        is_pass = ratio >= cfg.min_ratio_pct
        return FilterCriterionResult(
            name="offer_to_floor",
            threshold=threshold,
            actual_value=round(ratio, 2),
            unit="%",
            formula=formula,
            result=FilterResultStatus.PASS if is_pass else FilterResultStatus.FAIL,
            data_quality=DataQualityState.AVAILABLE,
            timestamp=now_iso,
            source=source,
            notes=note_tmpl.format(v=ratio),
        )

    def _eval_net_profit(self, trade_economics: TradeEconomics, now_iso: str) -> FilterCriterionResult:
        cfg = self.config.net_profit
        threshold = f">= {cfg.min_net_roi_pct:.2f}% net ROI" if cfg.enabled else "ADVISORY / OBSERVE ONLY"
        formula = "(target_exit - entry_offer - marketplace_fee - royalty - gas) / entry_offer * 100"
        source = "Trade model (collection offers, floor, fees)"
        mod = trade_economics.modelled
        roi = mod.estimated_roi_pct if mod.is_complete_and_reliable else None

        if not cfg.enabled:
            return FilterCriterionResult(
                name="net_profit",
                threshold=threshold,
                actual_value=round(roi, 2) if roi is not None else "UNKNOWN",
                unit="%",
                formula=formula,
                result=FilterResultStatus.OBSERVE,
                data_quality=DataQualityState.AVAILABLE if roi is not None else DataQualityState.MISSING,
                timestamp=now_iso,
                source=source,
                notes="Advisory ROI recorded; not blocking candidate decision.",
            )

        if roi is None:
            return FilterCriterionResult(
                name="net_profit",
                threshold=threshold,
                actual_value="UNKNOWN",
                unit="%",
                formula=formula,
                result=FilterResultStatus.DATA_INSUFFICIENT,
                data_quality=DataQualityState.MISSING,
                timestamp=now_iso,
                source=source,
                notes=f"Trade model incomplete: {mod.status_note}",
            )

        is_pass = roi >= cfg.min_net_roi_pct
        return FilterCriterionResult(
            name="net_profit",
            threshold=threshold,
            actual_value=round(roi, 2),
            unit="%",
            formula=formula,
            result=FilterResultStatus.PASS if is_pass else FilterResultStatus.FAIL,
            data_quality=DataQualityState.AVAILABLE,
            timestamp=now_iso,
            source=source,
            notes=f"Estimated net profit {mod.estimated_net_profit:.4f} {trade_economics.currency} on entry {mod.modelled_entry_offer:.4f}.",
        )


def sales_7d_count(sales_metrics: SalesMetrics, sale_count_mode: str) -> int:
    """Sales in the 7 complete calendar days, counted the same way as the trading-pace limit."""
    if sale_count_mode == "transactions":
        return int(sales_metrics.seven_day_sales_transactions or 0)
    return int(sales_metrics.seven_day_sales_items or 0)


def too_few_sales_reason(week_total: int, minimum: int) -> str:
    if week_total == 0:
        return "No sales in the last 7 days"
    return f"Only {week_total} sales in the last 7 days (needs at least {minimum})"
