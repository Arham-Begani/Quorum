"""Mock flight/hotel/car inventory for Atlas Travel.

Seeded and deterministic: the same RUN_SEED always yields the same inventory,
so a scenario reproduces exactly and a demo recorded twice looks the same twice.
[I9]

No network. Real booking APIs are an explicit anti-goal: zero value, real risk,
possible ToS problems. (CLAUDE.md §12)
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from datetime import date, timedelta

RUN_SEED = int(os.environ.get("RUN_SEED", 1337))

ORIGIN = "SFO"
DESTINATION = "LIS"
WINDOW_START = date(2026, 9, 10)
WINDOW_DAYS = 14


@dataclass(frozen=True)
class Flight:
    number: str
    depart_date: str
    arrive_date: str
    price_usd: float


@dataclass(frozen=True)
class Hotel:
    name: str
    nightly_rate_usd: float
    neighbourhood: str


@dataclass(frozen=True)
class Transfer:
    provider: str
    slot: str          # ISO datetime string
    price_usd: float


HOTEL_NAMES = [
    ("Alfama Riverside", "Alfama"),
    ("Baixa Grand", "Baixa"),
    ("Chiado Boutique", "Chiado"),
    ("Belem Garden Inn", "Belem"),
]
TRANSFER_PROVIDERS = ["LisboaCars", "TejoTransfers", "AeroShuttle"]


class Inventory:
    def __init__(self, seed: int = RUN_SEED):
        self.seed = seed
        rng = random.Random(seed)
        self.flights = self._flights(rng)
        self.hotels = self._hotels(rng)
        self.transfers = self._transfers(rng)

    def _flights(self, rng: random.Random) -> list[Flight]:
        out = []
        for i in range(WINDOW_DAYS):
            d = WINDOW_START + timedelta(days=i)
            # long-haul westbound-to-eastbound: arrives the next calendar day
            out.append(Flight(
                number=f"AT{100 + i}",
                depart_date=d.isoformat(),
                arrive_date=(d + timedelta(days=1)).isoformat(),
                price_usd=round(rng.uniform(680, 1240), 2),
            ))
        return out

    def _hotels(self, rng: random.Random) -> list[Hotel]:
        return [Hotel(name, round(rng.uniform(140, 320), 2), hood)
                for name, hood in HOTEL_NAMES]

    def _transfers(self, rng: random.Random) -> list[Transfer]:
        out = []
        for i in range(WINDOW_DAYS):
            d = WINDOW_START + timedelta(days=i)
            for hour in (9, 14):
                out.append(Transfer(
                    provider=rng.choice(TRANSFER_PROVIDERS),
                    slot=f"{d.isoformat()}T{hour:02d}:00",
                    price_usd=round(rng.uniform(38, 96), 2),
                ))
        return out

    # -- lookups --------------------------------------------------------
    def flight_arriving(self, arrive_date: str) -> Flight | None:
        return next((f for f in self.flights if f.arrive_date == arrive_date), None)

    def hotel(self, name: str) -> Hotel | None:
        return next((h for h in self.hotels if h.name == name), None)

    def cheapest_hotel(self) -> Hotel:
        return min(self.hotels, key=lambda h: h.nightly_rate_usd)

    def transfer_at(self, slot: str) -> Transfer | None:
        return next((t for t in self.transfers if t.slot == slot), None)

    def summary(self) -> dict:
        return {
            "seed": self.seed,
            "route": f"{ORIGIN}-{DESTINATION}",
            "window": [WINDOW_START.isoformat(),
                       (WINDOW_START + timedelta(days=WINDOW_DAYS - 1)).isoformat()],
            "flights": len(self.flights),
            "hotels": len(self.hotels),
            "transfers": len(self.transfers),
        }
