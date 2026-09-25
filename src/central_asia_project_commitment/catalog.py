"""口径目录：单位族、币种汇率、枚举分类、保密级别。

所有跨意向比较都先经这里归一化，保证"同一块土地、同一班列窗口"
不会因为单位或币种不同而被当成两项不同资源。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import RuleViolation

# ---------- 单位换算（换算到基准单位的倍率） ----------
UNIT_FAMILIES: dict[str, dict[str, float]] = {
    "area": {"m2": 1.0, "mu": 666.6666666667, "ha": 10_000.0},
    "mass": {"kg": 1.0, "ton": 1_000.0},
    "volume": {"m3": 1.0, "l": 0.001},
    "teu": {"teu": 1.0},
    "duration": {"month": 1.0, "year": 12.0},
    "person_day": {"person_day": 1.0, "person_month": 21.75},
    "flow": {"ton_per_year": 1.0, "ton_per_month": 12.0},
    "count": {"unit": 1.0},
}


def convert(value: float, from_unit: str, to_unit: str) -> float:
    for family in UNIT_FAMILIES.values():
        if from_unit in family and to_unit in family:
            return value * family[from_unit] / family[to_unit]
    raise RuleViolation(f"不可换算的单位：{from_unit} -> {to_unit}")


def same_family(unit_a: str, unit_b: str) -> bool:
    return any(unit_a in f and unit_b in f for f in UNIT_FAMILIES.values())


# ---------- 枚举分类 ----------
INVESTMENT_STAGES = [
    "intent_mou",      # 意向备忘录
    "feasibility",     # 可研
    "contracting",     # 签约
    "construction",    # 建设
    "operation",       # 运营
]

# 保密级别由低到高：供给方的级别必须不低于需求方要求的级别
CONFIDENTIALITY_LEVELS = ["L1", "L2", "L3"]

TAX_PREFERENCES = {"national_zone", "cross_border_zone", "local_subsidy", "none"}
LOGISTICS_ROUTES = {"ln_central_asia_rail", "china_railway_express_t", "multimodal_caspian"}
AGRI_PRODUCTS = {"wheat", "cotton", "livestock", "fruit", "oil_crops", "feed"}
PROCESS_FIELDS = {"food_processing", "feed_milling", "textile", "cold_chain", "oil_pressing"}
EXPERT_FIELDS = {"customs_compliance", "agronomy", "food_safety", "finance_fx", "logistics_ops"}


@dataclass(frozen=True)
class Money:
    amount: float
    currency: str

    def to_dict(self) -> dict:
        return {"amount": self.amount, "currency": self.currency}


class FxTable:
    """版本化汇率表：rate = 1 单位外币兑换多少报告币种。

    每次报价固定使用某一汇率版本，事后汇率变动不改写已有承诺。
    """

    def __init__(self, reporting_currency: str = "CNY") -> None:
        self.reporting_currency = reporting_currency
        self._versions: list[dict[str, float]] = [{reporting_currency: 1.0}]

    @property
    def version(self) -> int:
        return len(self._versions) - 1

    def publish(self, rates: dict[str, float]) -> int:
        table = dict(self._versions[-1])
        for currency, rate in rates.items():
            if rate <= 0:
                raise RuleViolation("汇率必须为正数")
            table[currency] = rate
        table[self.reporting_currency] = 1.0
        self._versions.append(table)
        return self.version

    def rate(self, currency: str, fx_version: int | None = None) -> float:
        table = self._versions[fx_version if fx_version is not None else self.version]
        if currency not in table:
            raise RuleViolation(f"汇率表缺少币种：{currency}")
        return table[currency]

    def convert(self, money: Money, to_currency: str | None = None, fx_version: int | None = None) -> Money:
        target = to_currency or self.reporting_currency
        report_value = money.amount * self.rate(money.currency, fx_version)
        target_rate = self.rate(target, fx_version)
        return Money(round(report_value / target_rate, 2), target)
