"""
Central-bank OMO simulation engine.

IMPORTANT: action names are from the CENTRAL BANK perspective, per the game:
- Repo: CB SELLS securities now and BUYS them back at maturity -> absorption now, injection later.
- Reverse Repo: CB BUYS securities now and SELLS them back at maturity -> injection now, absorption later.
- Buy Securities: outright CB purchase -> injection.
- Sell Securities: outright CB sale/new issue -> absorption.

Market inventory = T-bills held by commercial banks and is quantity-limited.
CB securities source for Repo / Sell Securities is treated as unlimited.

Rates are stored as percent values, e.g. 4.25 means 4.25%.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from statistics import NormalDist
from typing import Optional, Literal
import random
from decimal import Decimal, ROUND_HALF_UP

AuctionMethod = Literal["Interest-rate auction", "Volume auction"]
PricingMethod = Literal["Single-price", "Multi-price"]
OMOAction = Literal["Buy Securities", "Sell Securities", "Repo", "Reverse Repo"]


@dataclass
class Decision:
    auction_method: AuctionMethod
    omo_action: OMOAction
    volume: float
    pricing_method: Optional[PricingMethod] = None
    repo_rate: Optional[float] = None

    def validate(self):
        if self.volume < 0:
            raise ValueError("Volume must be >= 0.")

        if self.auction_method == "Interest-rate auction":
            if self.pricing_method not in ("Single-price", "Multi-price"):
                raise ValueError("Interest-rate auction requires Single-price or Multi-price.")
            # User-entered repo/reverse-repo rate is NOT used here.
            self.repo_rate = None

        elif self.auction_method == "Volume auction":
            # Pricing method is NOT applicable to volume auctions.
            self.pricing_method = None
            if self.repo_rate is None:
                raise ValueError("Volume auction requires the user-entered auction rate.")
            if not 0 <= self.repo_rate <= 10:
                raise ValueError("Auction rate must be between 0 and 10%.")
        else:
            raise ValueError("Unknown auction method.")


@dataclass
class TBillLot:
    security_id: str
    issue_date: date
    maturity_date: date
    rate: float
    face_value: float


@dataclass
class SecuritySlice:
    security_id: str
    face_value: float
    tbill_rate: float
    issue_date: date
    maturity_date: date
    price: float = 0.0


@dataclass
class RepoPosition:
    position_id: str
    bank: str
    action: OMOAction
    start_date: date
    maturity_date: date
    face_value: float
    initial_cash: float
    transaction_rate: float
    securities: list[SecuritySlice] = field(default_factory=list)
    status: str = "active"


@dataclass
class AuctionBid:
    rank: int
    bank: str
    bid_rate: float
    settlement_rate: float
    bid_volume: float
    real_volume: float
    price: float
    won: bool


@dataclass
class PhaseResult:
    phase: int
    phase_date: date
    decision: Decision
    scenario_liquidity_demand: float
    unmet_from_previous_phase: float
    real_liquidity_demand: float
    supply: float
    maturity_volume: float
    total_supply: float
    liquidity_gap: float
    liquidity_pressure: float
    liquidity_adjusted_volume: float
    previous_interbank_rate: float
    interbank_rate: float
    total_bid_volume: float
    total_real_volume: float
    win_rate: float
    auction_bids: list[AuctionBid]
    maturity_events: list[dict]

    def auction_table(self):
        return [asdict(x) for x in self.auction_bids]


class MarketTBillInventory:
    """Only commercial-bank / market holdings are tracked here."""

    def __init__(self):
        self.lots: list[TBillLot] = []
        self._counter = 1

    def add_lot(self, face_value, issue_date, rate, tenor_days=90):
        if face_value <= 1e-9:
            return None
        lot = TBillLot(
            security_id=f"SBV{self._counter:04d}",
            issue_date=issue_date,
            maturity_date=issue_date + timedelta(days=tenor_days),
            rate=float(rate),
            face_value=float(face_value),
        )
        self._counter += 1
        self.lots.append(lot)
        self.lots.sort(key=lambda x: (x.issue_date, x.security_id))
        return lot

    def total_face_value(self, current_date=None):
        return sum(
            x.face_value for x in self.lots
            if x.face_value > 1e-9
            and (current_date is None or x.maturity_date > current_date)
        )

    def eligible_face_value(self, current_date, min_remaining_days=0):
        cutoff = current_date + timedelta(days=min_remaining_days)
        return sum(x.face_value for x in self.lots
                   if x.face_value > 1e-9 and x.maturity_date >= cutoff)

    def take_fifo(self, requested, current_date, min_remaining_days=0):
        """FIFO; optionally require a minimum remaining maturity."""
        remaining = max(0.0, requested)
        cutoff = current_date + timedelta(days=min_remaining_days)
        result = []
        # Strict FIFO: among eligible SBV Bills, consume oldest issue first.
        for lot in sorted(self.lots, key=lambda x: (x.issue_date, x.security_id)):
            if remaining <= 1e-9:
                break
            if lot.maturity_date < cutoff or lot.face_value <= 1e-9:
                continue
            take = min(lot.face_value, remaining)
            lot.face_value -= take
            result.append(SecuritySlice(lot.security_id, take, lot.rate, lot.issue_date, lot.maturity_date))
            remaining -= take
        self._purge_empty()
        return result

    def return_slices(self, slices, current_date):
        """
        Return temporarily purchased securities to NHTMs.
        If a security has already reached its own maturity, do not recreate it.
        """
        for s in slices:
            if s.maturity_date <= current_date:
                continue
            original = next((x for x in self.lots if x.security_id == s.security_id), None)
            if original:
                original.face_value += s.face_value
            else:
                self.lots.append(TBillLot(
                    s.security_id, s.issue_date, s.maturity_date, s.tbill_rate, s.face_value
                ))

    def _purge_empty(self):
        """Remove T-bill lots whose remaining market quantity is exhausted."""
        self.lots = [x for x in self.lots if x.face_value > 1e-9]

    def process_maturities(self, current_date):
        cash = 0.0
        events = []
        survivors = []

        for lot in self.lots:
            if lot.maturity_date <= current_date:
                if lot.face_value > 1e-9:
                    amount = lot.face_value
                    cash += amount
                    events.append({
                        "type": "SBV Bill maturity", "id": lot.security_id,
                        "bank": "-", "action": "SBV Bill",
                        "issue_date": lot.issue_date.isoformat(),
                        "maturity_date": lot.maturity_date.isoformat(),
                        "rate": lot.rate, "volume": lot.face_value, "cash_flow": amount,
                    })
                # Matured lots are deleted from inventory immediately.
                continue

            if lot.face_value > 1e-9:
                survivors.append(lot)

        self.lots = survivors
        return cash, events

    def snapshot(self):
        self._purge_empty()
        # Player-facing SBV Bill inventory.
        return [{
            "sbv_bill_id": x.security_id,
            "issue_date": x.issue_date.isoformat(),
            "maturity_date": x.maturity_date.isoformat(),
            "rate": x.rate,
            "remaining_volume": x.face_value,
        } for x in self.lots]



def excel_round(value, ndigits=0):
    """Match Excel ROUND for game values: nearest, .5 away from zero."""
    q = Decimal("1").scaleb(-ndigits)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def whole(value):
    return int(excel_round(value, 0))


def whole_distribution(weights, total):
    """
    Allocate an integer total by weights. Every row is a whole unit and
    the rows sum exactly to the rounded total.
    """
    target = max(0, whole(total))
    if target == 0 or sum(weights) <= 0:
        return [0.0] * len(weights)

    raw = [w / sum(weights) * target for w in weights]
    alloc = [whole(x) for x in raw]
    diff = target - sum(alloc)

    if diff:
        # Add to the rows rounded down the most; subtract from rows rounded up the most.
        if diff > 0:
            order = sorted(range(len(raw)), key=lambda i: raw[i] - alloc[i], reverse=True)
            step = 1
        else:
            order = sorted(range(len(raw)), key=lambda i: raw[i] - alloc[i])
            step = -1

        k = 0
        while diff != 0:
            i = order[k % len(order)]
            if step > 0 or alloc[i] > 0:
                alloc[i] += step
                diff -= step
            k += 1

    return [float(x) for x in alloc]

def safe_normal(rng, mean, sd):
    u = min(max(rng.random(), 1e-12), 1 - 1e-12)
    return NormalDist(mean, sd).inv_cdf(u)


def price(face_value, rate_pct, days):
    return face_value / (1 + rate_pct * max(days, 0) / 36500)


def repayment(initial_cash, rate_pct, days):
    return initial_cash * (1 + rate_pct * days / 36500)


def rounded_distribution(weights, total):
    if total <= 0 or sum(weights) <= 0:
        return [0.0] * 5
    return [float(round(w / sum(weights) * total)) for w in weights]


class CentralBankGame:
    def __init__(self, start_date, initial_interbank_rate, floor=0.5, cap=5.0,
                 phase_days=30, tbill_tenor_days=90, repo_tenor_days=60, seed=None):
        self.current_date = start_date
        self.phase = 1
        self.interbank_rate = float(initial_interbank_rate)
        self.previous_liquidity_gap = 0.0
        self.floor = floor
        self.cap = cap
        self.phase_days = phase_days
        self.tbill_tenor_days = tbill_tenor_days
        self.repo_tenor_days = repo_tenor_days
        self.rng = random.Random(seed)
        self.market_inventory = MarketTBillInventory()
        self.repo_positions = []
        self.history = []
        self._repo_counter = 1

    def initial_tbill_rate(self):
        return excel_round(self.interbank_rate - safe_normal(self.rng, 0.64, 0.63), 2)

    def initialize_market_tbill(self, face_value=10000, rate=None):
        return self.market_inventory.add_lot(
            face_value, self.current_date,
            self.initial_tbill_rate() if rate is None else rate,
            self.tbill_tenor_days
        )

    def _banks(self):
        x = list("ABCDE")
        self.rng.shuffle(x)
        return x

    def _win_rate(self, action, rld):
        """
        Exact confirmed Excel logic.
        Injection (Buy / Reverse Repo):
          RLD < 0 -> Interbank - |Normal|, else Interbank + |Normal|
        Absorption (Sell / Repo):
          RLD > 0 -> Interbank - |Normal|, else Interbank + |Normal|
        """
        shock = abs(safe_normal(self.rng, 0.6, 0.58))
        injection = action in ("Buy Securities", "Reverse Repo")
        if injection:
            value = self.interbank_rate - shock if rld < 0 else self.interbank_rate + shock
        else:
            value = self.interbank_rate - shock if rld > 0 else self.interbank_rate + shock
        return excel_round(value, 2)

    def _interest_rates(self, win, action):
        rates = [excel_round(win, 2)]
        absorption = action in ("Sell Securities", "Repo")
        for _ in range(4):
            delta = self.rng.random() / 10
            value = rates[-1] + delta if absorption else rates[-1] - delta
            rates.append(excel_round(value, 2))
        return rates

    def _interest_total_bid(self, action, rld, volume):
        """
        Confirmed formula:
        - If action matches the sign of RLD, Total Bid = ABS(RLD).
        - If action goes against RLD, Total Bid = Volume * random 50%-80%.
        """
        injection = action in ("Buy Securities", "Reverse Repo")
        matches = (rld > 0) if injection else (rld < 0)
        if matches:
            return float(whole(abs(rld)))
        return float(whole(volume * self.rng.randint(50, 80) / 100))

    def _volume_total_bid(self, rld, volume, rate):
        raise RuntimeError("Use _volume_total_bid_for_action")

    def _volume_total_bid_for_action(self, action, rld, volume, rate):
        """
        Exact confirmed volume-auction formula.

        Injection (Buy / Reverse Repo):
          RLD < 0 -> use offered Volume bands (action is against market need)
          RLD >=0 -> use ABS(RLD) bands
          rate < floor: 10%-30%, and ONLY the Volume branch also gets 50%-80%
          floor <= rate <= cap: 90%-100%
          rate > cap: 80%-90%

        Absorption (Sell / Repo):
          RLD > 0 -> use offered Volume bands (action is against market need)
          RLD <=0 -> use ABS(RLD) bands
          rate < floor: 10%-30%, and ONLY the Volume branch also gets 50%-80%
          floor <= rate <= cap: 80%-90%
          rate > cap: 90%-100%
        """
        injection = action in ("Buy Securities", "Reverse Repo")
        matches = (rld > 0) if injection else (rld < 0)

        # This was reversed in v11. When action matches RLD, demand is ABS(RLD).
        # When action opposes RLD, bidding is driven by offered Volume.
        base = abs(rld) if matches else volume

        if rate < self.floor:
            factor = self.rng.randint(10, 30) / 100
            if not matches:  # Excel's Volume branch has the extra 50%-80% factor.
                factor *= self.rng.randint(50, 80) / 100
        elif rate <= self.cap:
            if injection:
                factor = self.rng.randint(90, 100) / 100
            else:
                factor = self.rng.randint(80, 90) / 100
        else:
            if injection:
                factor = self.rng.randint(80, 90) / 100
            else:
                factor = self.rng.randint(90, 100) / 100

        return float(whole(max(0.0, base * factor)))

    def _max_real(self, action, requested):
        # NHTM-held securities constrain CB purchases:
        # Buy Securities and Reverse Repo.
        if action == "Buy Securities":
            return min(requested, self.market_inventory.total_face_value(self.current_date))
        if action == "Reverse Repo":
            return min(requested, self.market_inventory.eligible_face_value(
                self.current_date, self.repo_tenor_days))
        # CB security source is unlimited:
        # Sell Securities and Repo.
        return requested

    def _market_slice_price(self, slices):
        total = 0.0
        for s in slices:
            s.price = price(s.face_value, s.tbill_rate,
                            (s.maturity_date - self.current_date).days)
            total += s.price
        return total

    def _new_cb_security_price(self, fv, auction_rate):
        return price(fv, auction_rate, self.tbill_tenor_days)

    def run_auction(self, decision, rld):
        decision.validate()
        banks = self._banks()

        if decision.auction_method == "Interest-rate auction":
            win = self._win_rate(decision.omo_action, rld)
            bid_rates = self._interest_rates(win, decision.omo_action)
            weights = [self.rng.random() for _ in range(5)]
            total_bid = self._interest_total_bid(decision.omo_action, rld, decision.volume)
            bid_vols = whole_distribution(weights, total_bid)

            # Interest-rate auction ordering follows the ACTION:
            # Buy Securities / Reverse Repo (injection): high -> low.
            # Sell Securities / Repo (absorption): LOW -> HIGH.
            # This remains true even if the player's action is opposite to
            # the current Real Liquidity Demand sign.
            rows = list(zip(banks, bid_rates, bid_vols))
            injection_action = decision.omo_action in ("Buy Securities", "Reverse Repo")
            rows.sort(key=lambda x: x[1], reverse=injection_action)
            banks = [x[0] for x in rows]
            bid_rates = [x[1] for x in rows]
            bid_vols = [x[2] for x in rows]

            settlement = [
                win if decision.pricing_method == "Single-price" else r
                for r in bid_rates
            ]

            wanted = whole(min(decision.volume, total_bid))
            total_real = whole(self._max_real(decision.omo_action, wanted))

            real_vols, left = [], int(total_real)
            for b in bid_vols:
                take = min(int(b), left)
                real_vols.append(float(take))
                left -= take

        else:
            win = round(float(decision.repo_rate), 2)
            bid_rates = [win] * 5
            settlement = bid_rates[:]
            weights = [5+self.rng.random(), 4+self.rng.random(), 3+self.rng.random(),
                       2+self.rng.random(), 1+self.rng.random()]
            total_bid = self._volume_total_bid_for_action(
                decision.omo_action, rld, decision.volume, float(decision.repo_rate)
            )
            bid_vols = whole_distribution(weights, total_bid)
            wanted = whole(min(decision.volume, total_bid))
            total_real = whole(self._max_real(decision.omo_action, wanted))

            if total_bid <= 1e-12 or total_real <= 0:
                real_vols = [0.0] * 5
            elif total_bid == total_real:
                real_vols = bid_vols[:]
            else:
                # Excel proportional allocation, reconciled so all security volumes
                # are whole numbers AND sum exactly to Total Real Volume.
                raw_weights = [x / total_bid for x in bid_vols]
                real_vols = whole_distribution(raw_weights, total_real)

        bids = []
        cash_abs = 0.0
        purchased_temp = []

        for i, rv in enumerate(real_vols):
            p = 0.0
            if rv > 1e-9:
                action = decision.omo_action

                if action == "Buy Securities":
                    slices = self.market_inventory.take_fifo(rv, self.current_date)
                    # Agreed rule: outright CB purchase uses each FIFO SBV Bill's
                    # own stored rate and its own remaining maturity; never average.
                    p = self._market_slice_price(slices)

                elif action == "Reverse Repo":
                    # CB buys NHTM T-bills now, then sells them back at maturity.
                    slices = self.market_inventory.take_fifo(
                        rv, self.current_date, min_remaining_days=self.repo_tenor_days)
                    p = self._market_slice_price(slices)
                    purchased_temp.extend(slices)

                elif action == "Sell Securities":
                    # CB sells/issues new T-bills; REAL volume becomes market inventory.
                    p = self._new_cb_security_price(rv, settlement[i])
                    self.market_inventory.add_lot(
                        rv, self.current_date, settlement[i], self.tbill_tenor_days
                    )

                elif action == "Repo":
                    # CB sells security now; source is unlimited. Temporary only.
                    p = self._new_cb_security_price(rv, settlement[i])

            p = excel_round(p, 2)
            cash_abs += p
            bids.append(AuctionBid(
                i+1, banks[i], excel_round(bid_rates[i], 2), excel_round(settlement[i], 2),
                float(whole(bid_vols[i])), float(whole(rv)), p, rv > 1e-9
            ))

        # Repo / Reverse Repo ledger: NEVER average rates.
        # Each winning bank keeps its exact settlement rate, volume and price.
        if decision.omo_action in ("Repo", "Reverse Repo") and cash_abs > 1e-9:
            rr_slices = list(purchased_temp)
            rr_index = 0
            rr_used = 0.0
            for b in bids:
                if not b.won or b.real_volume <= 1e-9:
                    continue
                bank_slices = []
                if decision.omo_action == "Reverse Repo":
                    needed = b.real_volume
                    while needed > 1e-9 and rr_index < len(rr_slices):
                        src = rr_slices[rr_index]
                        available = src.face_value - rr_used
                        take = min(needed, available)
                        if take > 1e-9:
                            sp = excel_round(
                                price(take, src.tbill_rate, (src.maturity_date-self.current_date).days), 2
                            )
                            bank_slices.append(SecuritySlice(src.security_id, take, src.tbill_rate,
                                src.issue_date, src.maturity_date, sp))
                        needed -= take
                        rr_used += take
                        if rr_used >= src.face_value - 1e-9:
                            rr_index += 1
                            rr_used = 0.0
                self.repo_positions.append(RepoPosition(
                    position_id=(f"RP{self._repo_counter:04d}" if decision.omo_action == "Repo"
                                 else f"RRP{self._repo_counter:04d}"),
                    bank=b.bank, action=decision.omo_action,
                    start_date=self.current_date, maturity_date=self.current_date + timedelta(days=self.repo_tenor_days),
                    face_value=b.real_volume, initial_cash=b.price, transaction_rate=b.settlement_rate,
                    securities=bank_slices))
                self._repo_counter += 1

        # Auction-time liquidity signs:
        # Repo / Sell Securities = absorption (-)
        # Reverse Repo / Buy Securities = injection (+)
        sign = +1 if decision.omo_action in ("Buy Securities", "Reverse Repo") else -1

        return dict(
            bids=bids, win_rate=win, total_bid_volume=total_bid,
            total_real_volume=float(whole(sum(b.real_volume for b in bids))),
            supply=excel_round(sign*cash_abs, 2)
        )

    def process_maturities(self):
        total, events = self.market_inventory.process_maturities(self.current_date)

        for pos in self.repo_positions:
            if pos.status != "active" or pos.maturity_date > self.current_date:
                continue

            value = excel_round(repayment(
                pos.initial_cash, pos.transaction_rate,
                (pos.maturity_date-pos.start_date).days
            ), 2)

            if pos.action == "Repo":
                # CB buys securities back -> pays NHTMs -> injection (+)
                flow = +value
            else:
                # Reverse Repo: CB sells securities back -> receives cash -> absorption (-)
                flow = -value
                self.market_inventory.return_slices(pos.securities, self.current_date)

            pos.status = "matured"
            total += flow
            events.append({
                "type": f"{pos.action} maturity", "id": pos.position_id,
                "bank": pos.bank, "action": pos.action,
                "issue_date": pos.start_date.isoformat(),
                "maturity_date": pos.maturity_date.isoformat(),
                "rate": pos.transaction_rate, "volume": pos.face_value,
                "cash_flow": flow, "repayment": value,
            })

        return round(total, 2), events

    def run_phase(self, scenario_liquidity_demand, decision):
        prev_rate = self.interbank_rate
        unmet = self.previous_liquidity_gap

        maturity, maturity_events = self.process_maturities()
        rld = scenario_liquidity_demand + unmet
        auction = self.run_auction(decision, rld)

        supply = auction["supply"]
        total_supply = excel_round(supply + maturity, 2)
        gap = excel_round(rld - total_supply, 2)
        pressure = 0.0 if abs(rld) < 1e-12 else gap / abs(rld)
        adjusted = excel_round(abs(total_supply) * pressure, 2)
        new_rate = excel_round(0.069 * prev_rate + 0.000123 * adjusted + 3.584, 2)

        result = PhaseResult(
            self.phase, self.current_date, decision,
            scenario_liquidity_demand, unmet, rld,
            supply, maturity, total_supply, gap, pressure, adjusted,
            prev_rate, new_rate,
            auction["total_bid_volume"], auction["total_real_volume"],
            auction["win_rate"], auction["bids"], maturity_events
        )

        self.history.append(result)
        self.previous_liquidity_gap = gap
        self.interbank_rate = new_rate
        self.phase += 1
        self.current_date += timedelta(days=self.phase_days)
        return result

    def tbill_inventory(self):
        return self.market_inventory.snapshot()

    def remaining_unmatured_tbill_volume(self):
        """Total T-bill face value still outstanding and not yet matured."""
        return self.market_inventory.total_face_value(self.current_date)

    def repo_inventory(self):
        # Same compact player-facing structure as SBV Bill inventory.
        # Matured positions are removed from the visible inventory.
        return [{
            "repo_id": p.position_id, "bank": p.bank, "action": p.action,
            "issue_date": p.start_date.isoformat(), "maturity_date": p.maturity_date.isoformat(),
            "rate": p.transaction_rate, "initial_price": p.initial_cash,
            "remaining_volume": p.face_value,
            "securities": [{"sbv_bill_id": x.security_id, "volume": x.face_value,
                            "sbv_bill_rate": x.tbill_rate, "maturity_date": x.maturity_date.isoformat(),
                            "price": x.price} for x in p.securities],
        } for p in self.repo_positions if p.status == "active"]
