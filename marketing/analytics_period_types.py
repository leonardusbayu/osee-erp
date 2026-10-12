from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True)
class PeriodScope:
    organization: object
    start: date
    end: date
    observation_end: date
    beginning: datetime
    cutoff: datetime
    today: date
    group_by: str
    filters: dict
    cash_available: bool
    spend_available: bool


@dataclass
class LeadFacts:
    attribution_fingerprint: str
    linked: dict
    invoice_index: dict
    selected: list
    selected_ids: set
    invoice_ids: list


@dataclass
class PaymentFacts:
    applied_by_invoice: dict
    paid_by_invoice: dict
    collectible: dict


@dataclass
class PeriodBuckets:
    summary: dict
    rows: dict = field(default_factory=dict)
    total_cash: Decimal = Decimal(0)
    attributed_cash: Decimal = Decimal(0)
    total_invoice_refunds: Decimal = Decimal(0)
    attributed_invoice_refunds: Decimal = Decimal(0)


@dataclass
class PeriodResults:
    summary: dict
    rows: list
    coverage: dict


@dataclass(frozen=True)
class CampaignSources:
    campaigns: list
    confirmed: dict
