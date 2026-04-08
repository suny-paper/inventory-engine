"""
SUN'Y Inventory Forecasting & Reorder Decision Engine — Core Calculations
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .config import (
    CONTAINER_CAPACITY_UNITS,
    EFFECTIVE_LEAD_TIME,
    HERO_ASINS,
    HERO_EMERGENCY_FILL_PCT,
    HERO_STOCKOUT_HORIZON,
    OVERSTOCK_THRESHOLD,
    STANDARD_SHIP_FILL_PCT,
    VELOCITY_7D_WEIGHT,
    VELOCITY_30D_WEIGHT,
    get_seasonality_multiplier,
    get_target_days_of_cover,
)


# ── Data classes ────────────────────────────────────────────────────────────

@dataclass
class InventorySnapshot:
    """Raw Amazon FBA inventory for one SKU."""
    sku: str
    asin: str
    fulfillable_on_hand: int = 0
    reserved: int = 0
    inbound_working: int = 0
    inbound_shipped: int = 0
    inbound_receiving: int = 0
    fc_transfer: int = 0

    @property
    def total_fba(self) -> int:
        return (self.fulfillable_on_hand + self.inbound_working
                + self.inbound_shipped + self.inbound_receiving)


@dataclass
class FactoryInput:
    """Manual factory production status for one SKU."""
    sku: str
    asin: str
    factory_ready_units: int = 0
    factory_wip_units: int = 0


@dataclass
class SalesData:
    """Daily sales aggregates for one SKU."""
    sku: str
    asin: str
    sales_7d: list[int] = field(default_factory=list)   # last 7 days
    sales_30d: list[int] = field(default_factory=list)  # last 30 days


@dataclass
class SKUAnalysis:
    """Full analysis result for a single SKU."""
    sku: str
    asin: str
    is_hero: bool

    # Inventory
    fba_on_hand: int
    fba_inbound: int
    factory_ready: int
    factory_wip: int
    total_supply: int

    # Velocity
    avg_7d: float
    avg_30d: float
    base_velocity: float
    seasonal_multiplier: float
    forecasted_daily_sales: float

    # Coverage
    days_of_cover: float
    target_min: int
    target_max: int

    # Decision
    status: str        # CRITICAL / AT_RISK / HEALTHY / OVERSTOCK
    action: str        # human-readable action
    required_units: int
    net_production_needed: int


@dataclass
class ShipmentPlan:
    """Container shipment recommendation."""
    allocations: list[dict]   # [{sku, asin, units, priority}]
    total_units: int
    container_fill_pct: float
    recommendation: str       # "SHIP NOW" / "WAIT" / "SHIP PARTIAL (HERO EMERGENCY)"


# ── Core calculation functions ──────────────────────────────────────────────

def compute_total_supply(inv: InventorySnapshot, factory: FactoryInput) -> int:
    return inv.total_fba + factory.factory_ready_units + factory.factory_wip_units


def compute_base_velocity(sales: SalesData) -> tuple[float, float, float]:
    avg_7d = sum(sales.sales_7d) / max(len(sales.sales_7d), 1)
    avg_30d = sum(sales.sales_30d) / max(len(sales.sales_30d), 1)
    weighted = (avg_7d * VELOCITY_7D_WEIGHT) + (avg_30d * VELOCITY_30D_WEIGHT)
    return avg_7d, avg_30d, weighted


def compute_days_of_cover(total_supply: int, forecasted_daily: float) -> float:
    if forecasted_daily <= 0:
        return 999.0  # effectively infinite
    return total_supply / forecasted_daily


def classify_status(days_of_cover: float, is_hero: bool) -> tuple[str, str]:
    """Return (status, action) based on days of cover."""
    if days_of_cover < EFFECTIVE_LEAD_TIME:
        return "CRITICAL", "Immediate production order. Ship partial container if needed."
    if days_of_cover < EFFECTIVE_LEAD_TIME + 14:
        return "AT_RISK", "Include in next shipment. Begin production if WIP insufficient."
    if days_of_cover > OVERSTOCK_THRESHOLD:
        return "OVERSTOCK", "Pause production. Consider reducing ads."
    return "HEALTHY", "Monitor."


def compute_reorder(
    forecasted_daily: float,
    total_supply: int,
    factory_wip: int,
    target_days: int,
) -> tuple[int, int]:
    """Return (required_units, net_production_needed)."""
    required = max(0, int(target_days * forecasted_daily) - total_supply)
    net_production = max(0, required - factory_wip)
    return required, net_production


# ── Full SKU analysis ──────────────────────────────────────────────────────

def analyze_sku(
    inv: InventorySnapshot,
    factory: FactoryInput,
    sales: SalesData,
    month: int | None = None,
    seasonality_overrides: dict[int, float] | None = None,
) -> SKUAnalysis:
    """Run the complete analysis pipeline for one SKU."""
    is_hero = inv.asin in HERO_ASINS
    total_supply = compute_total_supply(inv, factory)
    avg_7d, avg_30d, base_velocity = compute_base_velocity(sales)

    multiplier = get_seasonality_multiplier(month, seasonality_overrides)
    forecasted_daily = base_velocity * multiplier

    days_of_cover = compute_days_of_cover(total_supply, forecasted_daily)
    target_min, target_max = get_target_days_of_cover(month)
    status, action = classify_status(days_of_cover, is_hero)

    # Use midpoint of target range for reorder calc
    target_days = (target_min + target_max) // 2
    required_units, net_production = compute_reorder(
        forecasted_daily, total_supply, factory.factory_wip_units, target_days,
    )

    return SKUAnalysis(
        sku=inv.sku,
        asin=inv.asin,
        is_hero=is_hero,
        fba_on_hand=inv.fulfillable_on_hand,
        fba_inbound=inv.inbound_working + inv.inbound_shipped + inv.inbound_receiving,
        factory_ready=factory.factory_ready_units,
        factory_wip=factory.factory_wip_units,
        total_supply=total_supply,
        avg_7d=avg_7d,
        avg_30d=avg_30d,
        base_velocity=base_velocity,
        seasonal_multiplier=multiplier,
        forecasted_daily_sales=forecasted_daily,
        days_of_cover=days_of_cover,
        target_min=target_min,
        target_max=target_max,
        status=status,
        action=action,
        required_units=required_units,
        net_production_needed=net_production,
    )


# ── Container planning ──────────────────────────────────────────────────────

def plan_shipment(analyses: list[SKUAnalysis]) -> ShipmentPlan:
    """Build a shipment plan across all analyzed SKUs."""
    # Priority order: CRITICAL heroes, CRITICAL non-heroes, AT_RISK, others
    def sort_key(a: SKUAnalysis) -> tuple:
        status_rank = {"CRITICAL": 0, "AT_RISK": 1, "HEALTHY": 2, "OVERSTOCK": 3}
        return (0 if a.is_hero else 1, status_rank.get(a.status, 9))

    sorted_skus = sorted(analyses, key=sort_key)
    allocations: list[dict] = []
    remaining_capacity = CONTAINER_CAPACITY_UNITS
    hero_emergency = False

    for a in sorted_skus:
        if a.net_production_needed <= 0 and a.status not in ("CRITICAL", "AT_RISK"):
            continue
        # Units to ship = factory_ready (already produced, ready to go)
        units_to_ship = min(a.factory_ready, remaining_capacity)
        if units_to_ship <= 0:
            continue
        priority = "HERO" if a.is_hero else a.status
        allocations.append({
            "sku": a.sku,
            "asin": a.asin,
            "units": units_to_ship,
            "priority": priority,
        })
        remaining_capacity -= units_to_ship

        if a.is_hero and a.days_of_cover < HERO_STOCKOUT_HORIZON:
            hero_emergency = True

    total_units = sum(al["units"] for al in allocations)
    fill_pct = total_units / CONTAINER_CAPACITY_UNITS if CONTAINER_CAPACITY_UNITS else 0

    if hero_emergency and fill_pct >= HERO_EMERGENCY_FILL_PCT:
        recommendation = "SHIP NOW (HERO EMERGENCY)"
    elif fill_pct >= STANDARD_SHIP_FILL_PCT:
        recommendation = "SHIP NOW"
    elif hero_emergency:
        recommendation = "SHIP PARTIAL (HERO EMERGENCY)"
    else:
        recommendation = "WAIT — container not full enough"

    return ShipmentPlan(
        allocations=allocations,
        total_units=total_units,
        container_fill_pct=round(fill_pct, 4),
        recommendation=recommendation,
    )
