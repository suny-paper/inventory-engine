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
        resp = requests.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": SP_API_REFRESH_TOKEN,
            "client_id": SP_API_CLIENT_ID,
            "client_secret": SP_API_CLIENT_SECRET,
        }, timeout=30)
        resp.raise_for_status()
        self._access_token = resp.json()["access_token"]
        return self._access_token

    def _headers(self) -> dict[str, str]:
        return {
            "x-amz-access-token": self._get_access_token(),
            "Content-Type": "application/json",
        }

    # ── FBA Inventory ───────────────────────────────────────────────────

    def get_fba_inventory(self, asins: list[str] | None = None) -> list[InventorySnapshot]:
        """Fetch FBA inventory summaries via the FBA Inventory API."""
        if asins is None:
            asins = HERO_ASINS

        params: dict[str, Any] = {
            "details": "true",
            "granularityType": "Marketplace",
            "granularityId": SP_API_MARKETPLACE_ID,
            "marketplaceIds": SP_API_MARKETPLACE_ID,
        }

        url = f"{SP_API_BASE}/fba/inventory/v1/summaries"
        snapshots: list[InventorySnapshot] = []

        try:
            resp = requests.get(url, headers=self._headers(), params=params, timeout=60)
            resp.raise_for_status()
            data = resp.json()

            for item in data.get("payload", {}).get("inventorySummaries", []):
                asin = item.get("asin", "")
                if asin not in asins:
                    continue
                inv_details = item.get("inventoryDetails", {})
                snapshots.append(InventorySnapshot(
                    sku=item.get("sellerSku", ""),
                    asin=asin,
                    fulfillable_on_hand=inv_details.get("fulfillableQuantity", 0),
                    reserved=inv_details.get("reservedQuantity", {}).get("totalReservedQuantity", 0),
                    inbound_working=inv_details.get("researchingQuantity", {}).get("totalResearchingQuantity", 0),
                    inbound_shipped=inv_details.get("inboundShippedQuantity", 0),
                    inbound_receiving=inv_details.get("inboundReceivingQuantity", 0),
                    fc_transfer=0,
                ))
        except Exception:
            logger.exception("Failed to fetch FBA inventory")

        return snapshots

    # ── Sales Data ──────────────────────────────────────────────────────

    def get_sales_data(self, asins: list[str] | None = None,
                       days_back: int = 30) -> list[SalesData]:
        """Fetch daily sales for the given ASINs via the Sales API (getOrderMetrics)."""
        if asins is None:
            asins = HERO_ASINS

        today = date.today()
        start_30 = today - timedelta(days=days_back)
        start_7 = today - timedelta(days=7)

        url = f"{SP_API_BASE}/sales/v1/orderMetrics"
        sales_map: dict[str, SalesData] = {
            asin: SalesData(sku="", asin=asin) for asin in asins
        }

        try:
            # 30-day window, daily granularity
            params = {
                "marketplaceIds": SP_API_MARKETPLACE_ID,
                "interval": f"{start_30.isoformat()}T00:00:00Z--{today.isoformat()}T23:59:59Z",
                "granularity": "Day",
                "granularityTimeZone": "America/Los_Angeles",
                "asin": ",".join(asins),
            }
            resp = requests.get(url, headers=self._headers(), params=params, timeout=60)
            resp.raise_for_status()
            data = resp.json()

            for metric in data.get("payload", []):
                asin = metric.get("asin", "")
                if asin not in sales_map:
                    continue
                units = metric.get("unitCount", 0)
                metric_date = metric.get("interval", "")[:10]
                sales_map[asin].sales_30d.append(units)
                if metric_date >= start_7.isoformat():
                    sales_map[asin].sales_7d.append(units)

        except Exception:
            logger.exception("Failed to fetch sales data")

        return list(sales_map.values())

    # ── Convenience: fetch everything ──────────────────────────────────

    def fetch_all(self, asins: list[str] | None = None) -> tuple[
        list[InventorySnapshot], list[SalesData]
    ]:
        """Fetch both inventory and sales in one call."""
        inventories = self.get_fba_inventory(asins)
        sales = self.get_sales_data(asins)
        return inventories, sales
