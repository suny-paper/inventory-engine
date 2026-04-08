"""
SUN'Y Inventory Forecasting & Reorder Decision Engine — Daily Orchestrator

Run this script daily (via GitHub Actions or manually) to:
1. Pull FBA inventory + sales from Amazon SP-API
2. Read factory inputs + seasonality overrides from Google Sheets
3. Run forecasting & decision logic for each SKU
4. Write results to Google Sheets (dashboard, shipment planner, history)
"""

from __future__ import annotations

import logging
import sys

from .amazon_api import SPAPIClient
from .calculations import (
    FactoryInput,
    InventorySnapshot,
    SalesData,
    analyze_sku,
    plan_shipment,
)
from .config import HERO_ASINS
from .sheets_writer import (
    append_inventory_history,
    append_sales_history,
    ensure_tabs_exist,
    read_factory_input,
    read_seasonality_overrides,
    write_dashboard,
    write_shipment_planner,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def run() -> None:
    """Execute the full daily pipeline."""
    logger.info("=== SUN'Y Inventory Engine — Daily Run ===")

    # Step 0: Ensure Google Sheet tabs exist
    logger.info("Ensuring sheet tabs exist...")
    ensure_tabs_exist()

    # Step 1: Read factory inputs + seasonality from Google Sheets
    logger.info("Reading factory inputs from Google Sheets...")
    factory_rows = read_factory_input()
    factory_map: dict[str, FactoryInput] = {}
    for row in factory_rows:
        factory_map[row["asin"]] = FactoryInput(
            sku=row["sku"],
            asin=row["asin"],
            factory_ready_units=row["factory_ready_units"],
            factory_wip_units=row["factory_wip_units"],
        )

    logger.info("Reading seasonality overrides...")
    seasonality_overrides = read_seasonality_overrides()

    # Step 2: Pull Amazon data
    logger.info("Fetching Amazon SP-API data...")
    client = SPAPIClient()
    inventories, sales_list = client.fetch_all(HERO_ASINS)

    # Build lookup maps
    inv_map: dict[str, InventorySnapshot] = {inv.asin: inv for inv in inventories}
    sales_map: dict[str, SalesData] = {s.asin: s for s in sales_list}

    # Step 3: Analyze each HERO ASIN
    logger.info("Running analysis for %d ASINs...", len(HERO_ASINS))
    analyses = []
    for asin in HERO_ASINS:
        inv = inv_map.get(asin, InventorySnapshot(sku="UNKNOWN", asin=asin))
        factory = factory_map.get(asin, FactoryInput(sku=inv.sku, asin=asin))
        sales = sales_map.get(asin, SalesData(sku=inv.sku, asin=asin))

        # Backfill SKU on sales if missing
        if not sales.sku:
            sales.sku = inv.sku

        analysis = analyze_sku(
            inv=inv,
            factory=factory,
            sales=sales,
            seasonality_overrides=seasonality_overrides,
        )
        analyses.append(analysis)

        status_emoji = {
            "CRITICAL": "🔴", "AT_RISK": "🟠",
            "HEALTHY": "🟡", "OVERSTOCK": "🔵",
        }.get(analysis.status, "⚪")
        logger.info(
            "  %s %s (%s): %s — %.0f days cover — %s",
            status_emoji, analysis.sku, analysis.asin,
            analysis.status, analysis.days_of_cover, analysis.action,
        )

    # Step 4: Build shipment plan
    logger.info("Building shipment plan...")
    shipment = plan_shipment(analyses)
    logger.info(
        "  Container: %d/%d units (%.0f%%) — %s",
        shipment.total_units, 1076,
        shipment.container_fill_pct * 100,
        shipment.recommendation,
    )

    # Step 5: Write to Google Sheets
    logger.info("Writing to Google Sheets...")
    write_dashboard(analyses)
    write_shipment_planner(shipment)
    append_inventory_history(analyses)
    append_sales_history(analyses)

    logger.info("=== Daily run complete ===")


if __name__ == "__main__":
    try:
        run()
    except Exception:
        logger.exception("Daily run failed")
        sys.exit(1)
