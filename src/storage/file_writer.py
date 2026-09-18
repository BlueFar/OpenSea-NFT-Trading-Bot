import os
import re
from typing import Dict, Any, Optional
from datetime import datetime
from ..models.collection import CollectionMetadata
from ..models.metrics import SalesMetrics, FloorPriceMetrics, ListingMetrics
from ..models.trade import TradeEconomics
from ..models.filters import FilterEvaluationReport, FilterResultStatus

def sanitize_folder_name(name: str) -> str:
    r"""
    Sanitizes a project name for safe filesystem usage across all OS platforms.
    Replaces / \ : * ? " < > | and control characters with '-'.
    """
    if not name:
        return "unnamed_project"
    # Replace illegal filesystem characters
    sanitized = re.sub(r'[\/\\:\*\?"<>\|\x00-\x1f]', '-', name)
    # Collapse multiple consecutive hyphens or spaces
    sanitized = re.sub(r'[-\s]+', '-', sanitized).strip(' .-_')
    return sanitized or "unnamed_project"

def render_info_md(
    collection: CollectionMetadata,
    sales_metrics: SalesMetrics,
    floor_metrics: FloorPriceMetrics,
    listing_metrics: ListingMetrics,
    trade_economics: TradeEconomics,
    filter_report: FilterEvaluationReport,
    detection_dt_utc: datetime,
    detection_dt_local: datetime,
    tz_name: str = "Asia/Kolkata",
    bot_version: str = "1.0.0",
) -> str:
    """
    Renders the human-readable and machine-parseable Info.md markdown handoff file.
    Follows the exact 14-section specification required for the Stage 2 AI researcher.
    """
    primary_contract = collection.contracts[0].address if collection.contracts else "UNKNOWN"
    primary_chain = collection.contracts[0].chain if collection.contracts else "ethereum"
    
    # Calculate project age
    project_age_days_str = "UNKNOWN"
    if collection.created_date:
        try:
            created_dt = datetime.strptime(collection.created_date, "%Y-%m-%d")
            diff = (detection_dt_utc.date() - created_dt.date()).days
            project_age_days_str = f"{diff} days"
        except Exception:
            pass

    # OpenSea verification
    is_verified_bool = (collection.safelist_status == "verified")
    opensea_verified_str = "Yes" if is_verified_bool else "No"
    
    # Listed items
    listed_pct_str = f"{listing_metrics.listed_percentage:.2f}%" if listing_metrics.listed_percentage is not None else "UNKNOWN"
    if listing_metrics.is_early_exit_exceeded:
        listed_pct_str = f">{listed_pct_str} (exceeded threshold early)"
    total_supply_str = f"{collection.total_supply:,}" if collection.total_supply is not None else "UNKNOWN"
    listed_items_str = f"{listing_metrics.listed_items:,}" if listing_metrics.listed_items is not None else "UNKNOWN"

    # Daily sales table
    sales_table_rows = []
    for rec in sales_metrics.daily_breakdown:
        status_str = "Complete" if rec.is_complete_day else "In-Progress"
        sales_table_rows.append(f"| {rec.date_str} | {rec.sales_transactions} | {rec.sales_items} | {status_str} |")
    if sales_metrics.today_in_progress_record:
        rec = sales_metrics.today_in_progress_record
        sales_table_rows.append(f"| {rec.date_str} | {rec.sales_transactions} | {rec.sales_items} | In-Progress (Today) |")
    sales_table_md = "\n".join(sales_table_rows)

    # Floor metrics
    curr_floor_str = f"{floor_metrics.current_floor:.4f} {floor_metrics.floor_currency}" if floor_metrics.current_floor is not None else "UNKNOWN"
    floor_1d_str = f"{floor_metrics.floor_1d_ago:.4f} {floor_metrics.floor_currency}" if floor_metrics.floor_1d_ago is not None else "UNKNOWN"
    chg_1d_str = f"{floor_metrics.change_1d_signed_pct:+.2f}%" if floor_metrics.change_1d_signed_pct is not None else "UNKNOWN"
    floor_7d_str = f"{floor_metrics.floor_7d_ago:.4f} {floor_metrics.floor_currency}" if floor_metrics.floor_7d_ago is not None else "UNKNOWN"
    chg_7d_str = f"{floor_metrics.change_7d_signed_pct:+.2f}%" if floor_metrics.change_7d_signed_pct is not None else "UNKNOWN"

    # Top Offer
    obs = trade_economics.observed
    asm = trade_economics.assumptions
    mod = trade_economics.modelled
    top_offer_str = f"{obs.observed_top_offer:.4f} {obs.top_offer_currency}" if obs.observed_top_offer is not None else "UNKNOWN"
    royalty_pct_str = f"{obs.creator_royalty_pct:.2f}%" if obs.creator_royalty_pct is not None else "UNKNOWN"
    mp_fee_pct_str = f"{obs.marketplace_fee_pct:.2f}%" if obs.marketplace_fee_pct is not None else "UNKNOWN"
    
    # Ratios
    offer_to_floor_ratio_str = f"{mod.entry_offer_to_floor_ratio_pct:.2f}%" if mod.entry_offer_to_floor_ratio_pct is not None else "UNKNOWN"
    floor_spread_ratio_str = f"{mod.floor_spread_to_entry_offer_pct:.2f}%" if mod.floor_spread_to_entry_offer_pct is not None else "UNKNOWN"

    # Trade Economics modelled values
    modelled_entry_str = f"{mod.modelled_entry_offer:.4f} {trade_economics.currency}" if mod.modelled_entry_offer is not None else "UNKNOWN"
    target_exit_str = f"{mod.target_exit_price:.4f} {trade_economics.currency}" if mod.target_exit_price is not None else "UNKNOWN"
    gross_spread_str = f"{mod.gross_spread:.4f} {trade_economics.currency}" if mod.gross_spread is not None else "UNKNOWN"
    est_mp_fee_str = f"{mod.estimated_marketplace_fee:.4f} {trade_economics.currency}" if mod.estimated_marketplace_fee is not None else "UNKNOWN"
    est_royalty_str = f"{mod.estimated_creator_royalty:.4f} {trade_economics.currency}" if mod.estimated_creator_royalty is not None else "UNKNOWN"
    est_gas_str = f"{mod.estimated_gas_cost:.4f} {trade_economics.currency}" if mod.estimated_gas_cost is not None else "UNKNOWN"
    est_net_profit_str = f"{mod.estimated_net_profit:.4f} {trade_economics.currency}" if mod.estimated_net_profit is not None else "UNKNOWN"
    est_roi_str = f"{mod.estimated_roi_pct:.2f}%" if mod.estimated_roi_pct is not None else "UNKNOWN"
    est_margin_str = f"{mod.estimated_profit_margin_pct:.2f}%" if mod.estimated_profit_margin_pct is not None else "UNKNOWN"

    # Filter Criteria Section
    filter_sections = []
    for crit_name, crit in filter_report.criteria.items():
        title = "OpenSea Collection Age" if crit_name == "project_age" else crit.name.replace('_', ' ').title()
        filter_sections.append(f"""### {title}
- **Threshold**: {crit.threshold}
- **Actual Value**: {crit.actual_value}{crit.unit}
- **Formula**: `{crit.formula}`
- **Data Quality**: `{crit.data_quality.value}`
- **Result**: **{crit.result.value}**
- **Notes**: {crit.notes or 'None'}""")
    filter_sections_md = "\n\n".join(filter_sections)

    final_result_str = "PASS" if filter_report.is_overall_pass else "FAIL"

    md = f"""# Project: {collection.name}

## Snapshot

- **Detection Date**: {detection_dt_local.strftime('%Y-%m-%d')} ({tz_name})
- **Detection Time**: {detection_dt_local.strftime('%H:%M:%S')} ({tz_name})
- **Data Timestamp (UTC)**: {detection_dt_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}
- **Bot Version**: {bot_version}

## Collection Identity

- **Project Name**: {collection.name}
- **OpenSea Collection**: {collection.slug}
- **OpenSea URL**: {collection.opensea_url or f"https://opensea.io/collection/{collection.slug}"}
- **Blockchain**: {primary_chain}
- **Contract Address**: `{primary_contract}`
- **Collection Slug**: {collection.slug}
- **OpenSea Collection Age**: {project_age_days_str} (calculated from OpenSea created_date)
- **OpenSea Created Date**: {collection.created_date or 'UNKNOWN'}

## Collection References

- **Website**: {collection.project_url or 'None'}
- **X / Twitter**: {f"https://x.com/{collection.twitter_username}" if collection.twitter_username else 'None'}
- **Discord**: {collection.discord_url or 'None'}
- **Instagram**: {f"https://instagram.com/{collection.instagram_username}" if collection.instagram_username else 'None'}
- **Telegram**: {collection.telegram_url or 'None'}
- **Wiki**: {collection.wiki_url or 'None'}

## Collection Size

- **Total Supply**: {total_supply_str}
- **Owners**: {collection.total_supply if collection.total_supply is not None else 'UNKNOWN'}
- **Listed Items**: {listed_items_str}
- **Listed Percentage**: {listed_pct_str}

## OpenSea Status

- **OpenSea Verified**: {opensea_verified_str}
- **OpenSea Verification State**: `{collection.safelist_status or 'unknown'}`
- **Trust & Safety Status**: {'Disabled' if collection.is_disabled else 'Active'} | {'NSFW' if collection.is_nsfw else 'Clean'}

## Trading Activity — 7 Days

- **7-Day Total Transactions**: {sales_metrics.seven_day_sales_transactions}
- **7-Day Total Items Sold**: {sales_metrics.seven_day_sales_items}
- **Average Transactions / Day**: {sales_metrics.average_transactions_per_day:.2f}
- **Average Items Sold / Day**: {sales_metrics.average_sales_items_per_day:.2f}
- **Maximum Transactions in One Day**: {sales_metrics.max_daily_transactions}
- **Minimum Transactions in One Day**: {sales_metrics.min_daily_transactions}

### Daily Sales Breakdown ({tz_name} Midnight-to-Midnight):

| Date ({tz_name}) | Trades (Transactions) | Items Sold | Status |
|:---|---:|---:|:---|
{sales_table_md}

## Floor Price

- **Current Floor**: {curr_floor_str}
- **Floor Currency**: {floor_metrics.floor_currency}
- **1-Day Floor (24h Ago)**: {floor_1d_str}
- **1-Day Floor Change**: {chg_1d_str}
- **7-Day Floor (7d Ago)**: {floor_7d_str}
- **7-Day Floor Change**: {chg_7d_str}

## Top Offer

- **Observed Top Offer**: {top_offer_str}
- **Offer Currency**: {obs.top_offer_currency}
- **Creator Royalty**: {royalty_pct_str}
- **OpenSea Marketplace Fee**: {mp_fee_pct_str}
- **Modelled Entry Offer / Floor Ratio**: {offer_to_floor_ratio_str}
- **Floor / Modelled Entry Offer Spread**: {floor_spread_ratio_str}

## Trade Economics

> {trade_economics.disclaimer}

### A. Observed Market Data (Factual / API-Sourced)
- **Observed Top Offer**: {top_offer_str}
- **Current Floor**: {curr_floor_str}
- **Marketplace Fee (from OpenSea API)**: {mp_fee_pct_str}
- **Creator Royalty (from OpenSea API)**: {royalty_pct_str}
- **Fee Data Reliability**: {'RELIABLE' if obs.fees_reliable else 'INCOMPLETE / UNKNOWN (applicable OpenSea marketplace fee not confirmed)'}

### B. Model Assumptions (Configured Trading Strategy Parameters)
- **[ASSUMPTION] Entry-Offer Premium**: +{asm.entry_offer_premium_pct:.1f}% above observed top offer
- **[ASSUMPTION] Target Exit Discount**: -{asm.target_sale_discount_from_floor_pct:.1f}% below current floor
- **[ASSUMPTION] Gas Estimate**: {asm.gas_estimate_eth:.4f} ETH

### C. Modelled Results (Theoretical Estimates)
- **Modelled Entry Offer (Hypothetical Buy Price)**: {modelled_entry_str}
- **Target Exit Price (Hypothetical Sell Price)**: {target_exit_str}
- **Gross Spread (Exit - Entry)**: {gross_spread_str}
- **Estimated Selling Marketplace Fee**: {est_mp_fee_str}
- **Estimated Creator Royalty Fee**: {est_royalty_str}
- **Estimated Total Gas Cost**: {est_gas_str}
- **Estimated Net Profit**: {est_net_profit_str}
- **Estimated ROI**: {est_roi_str}
- **Estimated Profit Margin**: {est_margin_str}
- **Economics Status**: {mod.status_note}

## BOT Filter Conditions

{filter_sections_md}

## BOT Final Result

**{final_result_str}**

## Data Sources

- OpenSea Collection Details API: `GET /api/v2/collections/{collection.slug}`
- OpenSea Collection Stats API: `GET /api/v2/collections/{collection.slug}/stats`
- OpenSea Historical Floor Price API: `GET /api/v2/collections/{collection.slug}/floor_prices`
- OpenSea Collection Events API: `GET /api/v2/events/collection/{collection.slug}?event_type=sale`
- OpenSea Collection Listings API: `GET /api/v2/listings/collection/{collection.slug}/all`
- OpenSea Collection Offers API: `GET /api/v2/offers/collection/{collection.slug}/all`

## Notes

This project satisfies all deterministic quantitative filters in Stage 1 BOT monitoring.
It has been recorded into the daily candidate directory for downstream qualitative evaluation by OpenClaw and AI.
Fundamentals.md is intentionally omitted at this stage.
"""
    return md

def write_candidate_info_md(
    data_root: str,
    date_str: str,
    collection: CollectionMetadata,
    sales_metrics: SalesMetrics,
    floor_metrics: FloorPriceMetrics,
    listing_metrics: ListingMetrics,
    trade_economics: TradeEconomics,
    filter_report: FilterEvaluationReport,
    detection_dt_utc: datetime,
    detection_dt_local: datetime,
    tz_name: str = "Asia/Kolkata",
    bot_version: str = "1.0.0",
) -> str:
    """
    Atomically writes Info.md to DATA_ROOT/YYYY-MM-DD/<Sanitized_Project>/Info.md
    Uses Info.md.tmp -> fsync -> os.replace.
    Never creates Fundamentals.md.
    Returns the absolute path of the generated Info.md file.
    """
    sanitized_name = sanitize_folder_name(collection.name or collection.slug)
    project_dir = os.path.join(data_root, date_str, sanitized_name)
    os.makedirs(project_dir, exist_ok=True)

    info_path = os.path.join(project_dir, "Info.md")
    tmp_path = os.path.join(project_dir, "Info.md.tmp")

    content = render_info_md(
        collection=collection,
        sales_metrics=sales_metrics,
        floor_metrics=floor_metrics,
        listing_metrics=listing_metrics,
        trade_economics=trade_economics,
        filter_report=filter_report,
        detection_dt_utc=detection_dt_utc,
        detection_dt_local=detection_dt_local,
        tz_name=tz_name,
        bot_version=bot_version,
    )

    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp_path, info_path)
    return info_path
