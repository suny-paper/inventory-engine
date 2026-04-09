"""
SUN'Y Inventory Engine — Google Sheets Dashboard Writer

Creates and updates the shared Google Sheet with all required tabs:
  1. Live Dashboard
  2. Shipment Planner
  3. Factory Input
  4. Seasonality Config
  5. Historical Data
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from .calculations import SKUAnalysis, ShipmentPlan
from .config import (
    DEFAULT_SEASONALITY,
    GOOGLE_SHEETS_CREDENTIALS_JSON,
    GOOGLE_SPREADSHEET_ID,
    TIMEZONE,
)

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Sheet tab names
TAB_DASHBOARD = "Live Dashboard"
TAB_SHIPMENT = "Shipment Planner"
TAB_FACTORY = "Factory Input"
TAB_SEASONALITY = "Seasonality Config"
TAB_HISTORY = "Historical Data"


def _get_sheets_service():
    """Authenticate and return a Google Sheets API service object."""
    creds_info = json.loads(GOOGLE_SHEETS_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


def _pst_now() -> str:
    """Current timestamp in PST as ISO string."""
    import pytz
    tz = pytz.timezone(TIMEZONE)
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")


# ── Tab: Live Dashboard ────────────────────────────────────────────────────

DASHBOARD_HEADERS = [
    # Decision columns first — scan left-to-right for action
    "Status", "Action", "SKU", "ASIN", "Hero?",
    # Key metrics
    "Days of Cover", "Stockout Est", "Target Min", "Target Max",
    # Inventory breakdown
    "FBA On Hand", "FBA Inbound", "Factory Ready", "Factory WIP",
    "WIP Date Complete", "Days Until WIP Done", "Total Supply",
    # Velocity & forecast
    "Forecasted Daily Sales", "Base Velocity", "7d Avg", "30d Avg", "Seasonal Mult",
    # Reorder
    "Required Units", "Net Production Needed",
    "Timestamp",
]


def write_dashboard(analyses: list[SKUAnalysis]) -> None:
    """Overwrite the Live Dashboard tab with current analysis."""
    service = _get_sheets_service()
    ts = _pst_now()

    rows = [DASHBOARD_HEADERS]
    for a in analyses:
        wip_days_str = str(a.days_until_wip_complete) if a.days_until_wip_complete >= 0 else ""
        rows.append([
            a.status, a.action, a.sku, a.asin, "YES" if a.is_hero else "",
            round(a.days_of_cover, 1), a.stockout_est, a.target_min, a.target_max,
            a.fba_on_hand, a.fba_inbound, a.factory_ready, a.factory_wip,
            a.wip_date_complete, wip_days_str, a.total_supply,
            round(a.forecasted_daily_sales, 2), round(a.base_velocity, 2),
            round(a.avg_7d, 2), round(a.avg_30d, 2), a.seasonal_multiplier,
            a.required_units, a.net_production_needed,
            ts,
        ])

    service.spreadsheets().values().update(
        spreadsheetId=GOOGLE_SPREADSHEET_ID,
        range=f"'{TAB_DASHBOARD}'!A1",
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()
    logger.info("Live Dashboard updated with %d SKUs", len(analyses))


# ── Tab: Shipment Planner ──────────────────────────────────────────────────

SHIPMENT_HEADERS = [
    "Timestamp", "SKU", "ASIN", "Units", "Priority",
    "Total Units", "Container Fill %", "Recommendation",
]


def write_shipment_planner(plan: ShipmentPlan) -> None:
    """Overwrite the Shipment Planner tab."""
    service = _get_sheets_service()
    ts = _pst_now()

    rows = [SHIPMENT_HEADERS]
    for alloc in plan.allocations:
        rows.append([
            ts, alloc["sku"], alloc["asin"], alloc["units"], alloc["priority"],
            "", "", "",
        ])
    # Summary row
    rows.append([
        ts, "TOTAL", "", plan.total_units, "",
        plan.total_units,
        f"{plan.container_fill_pct * 100:.1f}%",
        plan.recommendation,
    ])

    service.spreadsheets().values().update(
        spreadsheetId=GOOGLE_SPREADSHEET_ID,
        range=f"'{TAB_SHIPMENT}'!A1",
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()
    logger.info("Shipment Planner updated")


# ── Tab: Factory Input (read) ──────────────────────────────────────────────

def read_factory_input() -> list[dict]:
    """Read factory input from the editable Factory Input tab.

    Expected columns: SKU, ASIN, factory_ready_units, factory_wip_units, wip_date_complete
    """
    service = _get_sheets_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SPREADSHEET_ID,
        range=f"'{TAB_FACTORY}'!A2:E100",
    ).execute()
    rows = result.get("values", [])
    factory_data = []
    for row in rows:
        if len(row) < 4:
            continue
        factory_data.append({
            "sku": row[0],
            "asin": row[1],
            "factory_ready_units": int(row[2] or 0),
            "factory_wip_units": int(row[3] or 0),
            "wip_date_complete": row[4].strip() if len(row) > 4 and row[4] else "",
        })
    return factory_data


# ── Tab: Seasonality Config (read) ─────────────────────────────────────────

def read_seasonality_overrides() -> dict[int, float]:
    """Read seasonality multiplier overrides from the Seasonality Config tab.

    Expected columns: Month (1-12), Multiplier
    """
    service = _get_sheets_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SPREADSHEET_ID,
        range=f"'{TAB_SEASONALITY}'!A2:B13",
    ).execute()
    rows = result.get("values", [])
    overrides: dict[int, float] = {}
    for row in rows:
        if len(row) < 2:
            continue
        try:
            overrides[int(row[0])] = float(row[1])
        except (ValueError, IndexError):
            continue
    return overrides


# ── Tab: Historical Data (append-only) ─────────────────────────────────────

INVENTORY_HISTORY_HEADERS = [
    "Timestamp", "SKU", "ASIN",
    "FBA On Hand", "FBA Inbound", "Factory Ready", "Factory WIP",
    "WIP Date Complete", "Total Supply", "Forecasted Daily", "Days of Cover",
    "Stockout Est", "Status",
]

SALES_HISTORY_HEADERS = [
    "Date", "SKU", "ASIN", "7d Avg", "30d Avg", "Base Velocity",
]


def append_inventory_history(analyses: list[SKUAnalysis]) -> None:
    """Append today's inventory snapshot to Historical Data (inventory section)."""
    service = _get_sheets_service()
    ts = _pst_now()

    rows = []
    for a in analyses:
        rows.append([
            ts, a.sku, a.asin,
            a.fba_on_hand, a.fba_inbound, a.factory_ready, a.factory_wip,
            a.wip_date_complete, a.total_supply, round(a.forecasted_daily_sales, 2),
            round(a.days_of_cover, 1), a.stockout_est, a.status,
        ])

    service.spreadsheets().values().append(
        spreadsheetId=GOOGLE_SPREADSHEET_ID,
        range=f"'{TAB_HISTORY}'!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()
    logger.info("Appended %d rows to inventory history", len(rows))


def append_sales_history(analyses: list[SKUAnalysis]) -> None:
    """Append today's sales velocity data to Historical Data."""
    service = _get_sheets_service()
    ts = _pst_now()[:10]  # date only

    rows = []
    for a in analyses:
        rows.append([
            ts, a.sku, a.asin,
            round(a.avg_7d, 2), round(a.avg_30d, 2), round(a.base_velocity, 2),
        ])

    service.spreadsheets().values().append(
        spreadsheetId=GOOGLE_SPREADSHEET_ID,
        range=f"'{TAB_HISTORY}'!M1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()
    logger.info("Appended %d rows to sales history", len(rows))


# ── Initialize sheet tabs ──────────────────────────────────────────────────

def ensure_tabs_exist() -> None:
    """Create the required tabs if they don't already exist and seed headers."""
    service = _get_sheets_service()
    sheet_meta = service.spreadsheets().get(
        spreadsheetId=GOOGLE_SPREADSHEET_ID,
    ).execute()
    existing = {s["properties"]["title"] for s in sheet_meta["sheets"]}

    tabs_to_create = []
    for tab in [TAB_DASHBOARD, TAB_SHIPMENT, TAB_FACTORY, TAB_SEASONALITY, TAB_HISTORY]:
        if tab not in existing:
            tabs_to_create.append({
                "addSheet": {"properties": {"title": tab}}
            })

    if tabs_to_create:
        service.spreadsheets().batchUpdate(
            spreadsheetId=GOOGLE_SPREADSHEET_ID,
            body={"requests": tabs_to_create},
        ).execute()
        logger.info("Created tabs: %s", [t["addSheet"]["properties"]["title"] for t in tabs_to_create])

    # Seed Factory Input headers if empty
    if TAB_FACTORY not in existing:
        service.spreadsheets().values().update(
            spreadsheetId=GOOGLE_SPREADSHEET_ID,
            range=f"'{TAB_FACTORY}'!A1:E1",
            valueInputOption="RAW",
            body={"values": [["SKU", "ASIN", "factory_ready_units", "factory_wip_units", "wip_date_complete"]]},
        ).execute()

    # Seed Seasonality Config with defaults if empty
    if TAB_SEASONALITY not in existing:
        month_names = [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ]
        rows = [["Month", "Multiplier"]]
        for m in range(1, 13):
            rows.append([f"{m} - {month_names[m-1]}", DEFAULT_SEASONALITY[m]])
        service.spreadsheets().values().update(
            spreadsheetId=GOOGLE_SPREADSHEET_ID,
            range=f"'{TAB_SEASONALITY}'!A1",
            valueInputOption="RAW",
            body={"values": rows},
        ).execute()

    # Seed Historical Data headers
    if TAB_HISTORY not in existing:
        service.spreadsheets().values().update(
            spreadsheetId=GOOGLE_SPREADSHEET_ID,
            range=f"'{TAB_HISTORY}'!A1",
            valueInputOption="RAW",
            body={"values": [INVENTORY_HISTORY_HEADERS]},
        ).execute()
        service.spreadsheets().values().update(
            spreadsheetId=GOOGLE_SPREADSHEET_ID,
            range=f"'{TAB_HISTORY}'!M1",
            valueInputOption="RAW",
            body={"values": [SALES_HISTORY_HEADERS]},
        ).execute()
