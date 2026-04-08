"""
SUN'Y Inventory Engine — Amazon SP-API Data Fetcher

Pulls FBA inventory and sales data via the Selling Partner API.
Requires SP-API credentials configured in environment variables.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any

import requests

from .calculations import FactoryInput, InventorySnapshot, SalesData
from .config import (
    HERO_ASINS,
    SP_API_CLIENT_ID,
    SP_API_CLIENT_SECRET,
    SP_API_MARKETPLACE_ID,
    SP_API_REFRESH_TOKEN,
)

logger = logging.getLogger(__name__)

TOKEN_URL = "https://api.amazon.com/auth/o2/token"
SP_API_BASE = "https://sellingpartnerapi-na.amazon.com"


class SPAPIClient:
    """Minimal Amazon SP-API client for inventory + sales data."""

    def __init__(self) -> None:
        self._access_token: str | None = None

    def _get_access_token(self) -> str:
        if self._access_token:
            return self._access_token
        logger.info("Requesting SP-API access token...")
        resp = requests.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": SP_API_REFRESH_TOKEN,
            "client_id": SP_API_CLIENT_ID,
            "client_secret": SP_API_CLIENT_SECRET,
        }, timeout=30)
        resp.raise_for_status()
        self._access_token = resp.json()["access_token"]
        logger.info("SP-API access token obtained successfully")
        return self._access_token

    def _headers(self) -> dict[str, str]:
        return {
            "x-amz-access-token": self._get_access_token(),
            "Content-Type": "application/json",
        }

    # ── FBA Inventory ───────────────────────────────────────────────────

    def get_fba_inventory(self, asins: list[str] | None = None) -> list[InventorySnapshot]:
        """Fetch FBA inventory summaries via the FBA Inventory API.

        Handles pagination via nextToken to ensure all SKUs are retrieved.
        """
        if asins is None:
            asins = HERO_ASINS

        asin_set = set(asins)
        url = f"{SP_API_BASE}/fba/inventory/v1/summaries"
        snapshots: list[InventorySnapshot] = []
        next_token: str | None = None
        page = 0

        try:
            while True:
                page += 1
                params: dict[str, Any] = {
                    "details": "true",
                    "granularityType": "Marketplace",
                    "granularityId": SP_API_MARKETPLACE_ID,
                    "marketplaceIds": SP_API_MARKETPLACE_ID,
                }
                if next_token:
                    params["nextToken"] = next_token

                logger.info("Fetching FBA inventory page %d...", page)
                resp = requests.get(url, headers=self._headers(), params=params, timeout=60)
                resp.raise_for_status()
                data = resp.json()

                payload = data.get("payload", {})
                summaries = payload.get("inventorySummaries", [])
                logger.info("  Page %d: %d inventory summaries returned", page, len(summaries))

                for item in summaries:
                    asin = item.get("asin", "")
                    if asin not in asin_set:
                        continue
                    inv_details = item.get("inventoryDetails", {})
                    reserved_qty = inv_details.get("reservedQuantity", {})
                    snapshot = InventorySnapshot(
                        sku=item.get("sellerSku", ""),
                        asin=asin,
                        fulfillable_on_hand=inv_details.get("fulfillableQuantity", 0),
                        reserved=reserved_qty.get("totalReservedQuantity", 0) if isinstance(reserved_qty, dict) else 0,
                        inbound_working=inv_details.get("inboundWorkingQuantity", 0),
                        inbound_shipped=inv_details.get("inboundShippedQuantity", 0),
                        inbound_receiving=inv_details.get("inboundReceivingQuantity", 0),
                        fc_transfer=0,
                    )
                    snapshots.append(snapshot)
                    logger.info("  Found HERO SKU %s (%s): on_hand=%d, inbound_work=%d, inbound_ship=%d, inbound_recv=%d",
                                snapshot.sku, asin,
                                snapshot.fulfillable_on_hand,
                                snapshot.inbound_working,
                                snapshot.inbound_shipped,
                                snapshot.inbound_receiving)

                # Check for more pages
                next_token = payload.get("nextToken")
                if not next_token:
                    break
                # Stop if we already found all ASINs
                found_asins = {s.asin for s in snapshots}
                if asin_set <= found_asins:
                    logger.info("  All %d target ASINs found, stopping pagination", len(asin_set))
                    break

            logger.info("FBA inventory fetch complete: %d matching SKUs found across %d pages", len(snapshots), page)

        except requests.exceptions.HTTPError as e:
            logger.error("SP-API inventory HTTP error: %s — Response: %s", e, e.response.text[:500] if e.response else "N/A")
        except Exception:
            logger.exception("Failed to fetch FBA inventory")

        return snapshots

    # ── Sales Data ──────────────────────────────────────────────────────

    def get_sales_data(self, asins: list[str] | None = None,
                       days_back: int = 30) -> list[SalesData]:
        """Fetch daily sales for the given ASINs via the Sales API (getOrderMetrics).

        Note: The Sales API returns aggregate metrics (not per-ASIN).
        We fetch per-ASIN by making individual calls when needed.
        """
        if asins is None:
            asins = HERO_ASINS

        today = date.today()
        start_30 = today - timedelta(days=days_back)
        start_7 = today - timedelta(days=7)

        url = f"{SP_API_BASE}/sales/v1/orderMetrics"
        sales_map: dict[str, SalesData] = {
            asin: SalesData(sku="", asin=asin) for asin in asins
        }

        # Fetch per-ASIN to get individual sales data
        for asin in asins:
            try:
                params = {
                    "marketplaceIds": SP_API_MARKETPLACE_ID,
                    "interval": f"{start_30.isoformat()}T00:00:00-07:00--{today.isoformat()}T23:59:59-07:00",
                    "granularity": "Day",
                    "granularityTimeZone": "America/Los_Angeles",
                    "asin": asin,
                }
                logger.info("Fetching sales data for ASIN %s...", asin)
                resp = requests.get(url, headers=self._headers(), params=params, timeout=60)
                resp.raise_for_status()
                data = resp.json()

                metrics = data.get("payload", [])
                logger.info("  ASIN %s: %d daily metrics returned", asin, len(metrics))

                for metric in metrics:
                    units = metric.get("unitCount", 0)
                    interval_str = metric.get("interval", "")
                    metric_date = interval_str[:10]
                    sales_map[asin].sales_30d.append(units)
                    if metric_date >= start_7.isoformat():
                        sales_map[asin].sales_7d.append(units)

                total_30d = sum(sales_map[asin].sales_30d)
                total_7d = sum(sales_map[asin].sales_7d)
                logger.info("  ASIN %s totals: 30d=%d units (%d days), 7d=%d units (%d days)",
                            asin, total_30d, len(sales_map[asin].sales_30d),
                            total_7d, len(sales_map[asin].sales_7d))

            except requests.exceptions.HTTPError as e:
                logger.error("SP-API sales HTTP error for %s: %s — Response: %s",
                             asin, e, e.response.text[:500] if e.response else "N/A")
            except Exception:
                logger.exception("Failed to fetch sales data for ASIN %s", asin)

        return list(sales_map.values())

    # ── Convenience: fetch everything ──────────────────────────────────

    def fetch_all(self, asins: list[str] | None = None) -> tuple[
        list[InventorySnapshot], list[SalesData]
    ]:
        """Fetch both inventory and sales in one call."""
        inventories = self.get_fba_inventory(asins)
        sales = self.get_sales_data(asins)
        return inventories, sales
