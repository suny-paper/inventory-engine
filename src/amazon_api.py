"""
SUN'Y Inventory Engine — Cloud SQL Data Reader

Reads FBA inventory and sales data from the Cloud SQL `suny_dash` database,
which is populated by the consolidated Amazon data pipeline (SUN-681).

Replaces the previous SP-API direct-call approach.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

from .calculations import InventorySnapshot, SalesData
from .config import DATABASE_URL, HERO_ASINS

logger = logging.getLogger(__name__)


class CloudSQLReader:
    """Read Amazon inventory + sales data from Cloud SQL."""

    def __init__(self) -> None:
        self._conn = psycopg2.connect(DATABASE_URL)

    def close(self) -> None:
        self._conn.close()

    # ── FBA Inventory ───────────────────────────────────────────────────

    def get_fba_inventory(self, asins: list[str] | None = None) -> list[InventorySnapshot]:
        """Read FBA inventory from inventory_levels joined with skus."""
        if asins is None:
            asins = HERO_ASINS

        with self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """
                SELECT
                    s.sku,
                    s.asin,
                    COALESCE(MAX(CASE WHEN il.location_code = 'fba_on_hand'  THEN il.quantity END), 0) AS fulfillable_on_hand,
                    COALESCE(MAX(CASE WHEN il.location_code = 'fba_reserved' THEN il.quantity END), 0) AS reserved,
                    COALESCE(MAX(CASE WHEN il.location_code = 'fba_incoming' THEN il.quantity END), 0) AS inbound_total
                FROM skus s
                LEFT JOIN inventory_levels il ON il.sku_id = s.id
                WHERE s.asin = ANY(%s)
                  AND s.is_active = true
                GROUP BY s.sku, s.asin
                """,
                (asins,),
            )
            rows = cur.fetchall()

        snapshots: list[InventorySnapshot] = []
        for row in rows:
            snapshot = InventorySnapshot(
                sku=row["sku"],
                asin=row["asin"],
                fulfillable_on_hand=row["fulfillable_on_hand"],
                reserved=row["reserved"],
                # Pipeline stores combined inbound; map to inbound_shipped for total_fba calc
                inbound_working=0,
                inbound_shipped=row["inbound_total"],
                inbound_receiving=0,
                fc_transfer=0,
            )
            snapshots.append(snapshot)
            logger.info(
                "  DB SKU %s (%s): on_hand=%d, reserved=%d, inbound=%d",
                snapshot.sku, snapshot.asin,
                snapshot.fulfillable_on_hand,
                snapshot.reserved,
                snapshot.inbound_shipped,
            )

        logger.info("FBA inventory read from DB: %d matching SKUs", len(snapshots))
        return snapshots

    # ── Sales Data ──────────────────────────────────────────────────────

    def get_sales_data(
        self, asins: list[str] | None = None, days_back: int = 30
    ) -> list[SalesData]:
        """Read daily sales from sales_events joined with skus."""
        if asins is None:
            asins = HERO_ASINS

        today = date.today()
        start_30 = today - timedelta(days=days_back)
        start_7 = today - timedelta(days=7)

        with self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """
                SELECT s.asin, s.sku, se.sale_date, se.units
                FROM sales_events se
                JOIN skus s ON s.id = se.sku_id
                WHERE s.asin = ANY(%s)
                  AND s.is_active = true
                  AND se.sale_date >= %s
                ORDER BY s.asin, se.sale_date
                """,
                (asins, start_30),
            )
            rows = cur.fetchall()

        sales_map: dict[str, SalesData] = {
            asin: SalesData(sku="", asin=asin) for asin in asins
        }

        for row in rows:
            asin = row["asin"]
            if asin not in sales_map:
                continue
            if not sales_map[asin].sku:
                sales_map[asin].sku = row["sku"]
            units = row["units"]
            sales_map[asin].sales_30d.append(units)
            if row["sale_date"] >= start_7:
                sales_map[asin].sales_7d.append(units)

        for asin, sd in sales_map.items():
            total_30d = sum(sd.sales_30d)
            total_7d = sum(sd.sales_7d)
            logger.info(
                "  DB ASIN %s: 30d=%d units (%d days), 7d=%d units (%d days)",
                asin, total_30d, len(sd.sales_30d), total_7d, len(sd.sales_7d),
            )

        return list(sales_map.values())

    # ── Convenience: fetch everything ──────────────────────────────────

    def fetch_all(
        self, asins: list[str] | None = None
    ) -> tuple[list[InventorySnapshot], list[SalesData]]:
        """Fetch both inventory and sales in one call."""
        inventories = self.get_fba_inventory(asins)
        sales = self.get_sales_data(asins)
        return inventories, sales
