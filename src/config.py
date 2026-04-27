"""
SUN'Y Inventory Forecasting & Reorder Decision Engine — Configuration
"""

from __future__ import annotations

import os
from datetime import date

# ── HERO ASINs (highest priority — must not stock out during peak) ──────────
HERO_ASINS = [
    "B0DSLXZQ89",  # ESC-Chair-Swhite
    "B0DNY7MQG3",  # ESC-Chair-NNavy
    "B0DNY8QZJ9",  # ESC-Chair-Nsand
    "B0DNYB5RPF",  # ESC-Chair-Sgreen
]

# ── Logistics constants ─────────────────────────────────────────────────────
CONTAINER_CAPACITY_UNITS = 1076
SHIP_LEAD_DAYS = 37
AMAZON_BUFFER_DAYS = 14
FACTORY_BUFFER_DAYS = 40
EFFECTIVE_LEAD_TIME = SHIP_LEAD_DAYS + AMAZON_BUFFER_DAYS  # 51 days

# ── Seasonality multipliers (editable via Seasonality Config sheet) ─────────
DEFAULT_SEASONALITY = {
    1: 0.4, 2: 0.4, 3: 0.4,   # Jan–Mar
    4: 0.7,                     # Apr
    5: 1.2,                     # May
    6: 1.8,                     # Jun
    7: 2.0,                     # Jul
    8: 1.5,                     # Aug
    9: 0.8,                     # Sep
    10: 0.5, 11: 0.5, 12: 0.5, # Oct–Dec
}

# ── Target days of cover ────────────────────────────────────────────────────
PRE_PEAK_MONTHS = [4, 5]
PEAK_MONTHS = [6, 7]
PRE_PEAK_TARGET_MIN = 60
PRE_PEAK_TARGET_MAX = 75
PEAK_TARGET_MIN = 75
PEAK_TARGET_MAX = 90
DEFAULT_TARGET_DAYS = 75  # fallback for non-peak months

# ── Status thresholds ───────────────────────────────────────────────────────
# CRITICAL:  days_of_cover < EFFECTIVE_LEAD_TIME
# AT_RISK:   days_of_cover < EFFECTIVE_LEAD_TIME + 14
# HEALTHY:   within target range
# OVERSTOCK: > 90 days

OVERSTOCK_THRESHOLD = 90

# ── Container shipping rules ────────────────────────────────────────────────
STANDARD_SHIP_FILL_PCT = 0.95   # Ship when >= 95% full
HERO_EMERGENCY_FILL_PCT = 0.70  # Ship at 70% if HERO will stock out within 30 days
HERO_STOCKOUT_HORIZON = 30      # days

# ── Velocity weighting ──────────────────────────────────────────────────────
VELOCITY_7D_WEIGHT = 0.7
VELOCITY_30D_WEIGHT = 0.3

# ── Cloud SQL (populated by the consolidated Amazon data pipeline) ─────────
DATABASE_URL = os.getenv("DATABASE_URL", "")

# ── Google Sheets credentials ──────────────────────────────────────────────
GOOGLE_SHEETS_CREDENTIALS_JSON = os.getenv("GOOGLE_SHEETS_CREDENTIALS_JSON", "")
GOOGLE_SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID", "")

# ── Timezone ────────────────────────────────────────────────────────────────
TIMEZONE = "America/Los_Angeles"  # PST/PDT


def get_target_days_of_cover(month: int | None = None) -> tuple[int, int]:
    """Return (min, max) target days of cover for the given month."""
    if month is None:
        month = date.today().month
    if month in PEAK_MONTHS:
        return PEAK_TARGET_MIN, PEAK_TARGET_MAX
    if month in PRE_PEAK_MONTHS:
        return PRE_PEAK_TARGET_MIN, PRE_PEAK_TARGET_MAX
    return DEFAULT_TARGET_DAYS, OVERSTOCK_THRESHOLD


def get_seasonality_multiplier(month: int | None = None,
                               overrides: dict[int, float] | None = None) -> float:
    """Return the seasonality multiplier for a month."""
    if month is None:
        month = date.today().month
    if overrides and month in overrides:
        return overrides[month]
    return DEFAULT_SEASONALITY.get(month, 1.0)
