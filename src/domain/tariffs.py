from dataclasses import dataclass


@dataclass(frozen=True)
class Tariff:
    gb: int
    price_rub: int
    badge: str = ""

    @property
    def price_per_gb(self) -> float:
        return self.price_rub / self.gb


TARIFFS: list[Tariff] = [
    Tariff(gb=50, price_rub=130),
    Tariff(gb=150, price_rub=250, badge="🔥 популярный"),
    Tariff(gb=450, price_rub=449, badge="💎 максимальная выгода"),
]


def get_tariff(gb: int) -> Tariff | None:
    return next((t for t in TARIFFS if t.gb == gb), None)
