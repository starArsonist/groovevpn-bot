from dataclasses import dataclass


@dataclass(frozen=True)
class Tariff:
    gb: int
    price_rub: int


TARIFFS: list[Tariff] = [
    Tariff(gb=50, price_rub=130),
    Tariff(gb=150, price_rub=250),
    Tariff(gb=450, price_rub=449),
]


def get_tariff(gb: int) -> Tariff | None:
    return next((t for t in TARIFFS if t.gb == gb), None)
