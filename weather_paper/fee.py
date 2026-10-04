"""Weather taker fee from the public Polymarket schedule.

https://docs.polymarket.com/trading/fees

    fee_usdc = shares × fee_rate × price × (1 − price)

Crypto fee_rate is still 0.07. Weather fee_rate is 0.05, and the live
weather markets we checked publish feeSchedule.rate 0.05. These books use
the market's published rate. When a weather market does not publish one,
they use 0.05. They do not use the crypto rate.

The formula result is USDC. It is rounded to 5 decimal places. A fee that
rounds below 0.00001 USDC is zero.

Buys collect that USDC amount in shares at the fill price
(fee_shares = fee_usdc / price). The cash paid is the share notional.
Sells collect the same USDC amount from the proceeds. These books do not
sell. Resolutions have no fee.
"""

from decimal import Decimal, ROUND_HALF_UP

WEATHER_FEE_RATE = Decimal("0.05")
CRYPTO_FEE_RATE = Decimal("0.07")
FEE_QUANTUM = Decimal("0.00001")


def taker_fee_usdc(shares: Decimal, price: Decimal, rate: Decimal) -> Decimal:
    if shares <= 0:
        raise ValueError("fee shares must be positive")
    if price < 0 or price > 1:
        raise ValueError("fee price must be between 0 and 1")
    if rate < 0:
        raise ValueError("fee rate must be zero or positive")
    raw = shares * rate * price * (Decimal(1) - price)
    fee = raw.quantize(FEE_QUANTUM, rounding=ROUND_HALF_UP)
    if fee < FEE_QUANTUM:
        return Decimal(0)
    return fee


def fee_rate_for(market: dict) -> Decimal | None:
    """Published weather rate, or None when the schedule is not the documented curve."""
    if market.get("feesEnabled") is False:
        return Decimal(0)
    schedule = market.get("feeSchedule") or {}
    if not isinstance(schedule, dict):
        return None
    if schedule.get("exponent") is not None:
        exponent = Decimal(str(schedule["exponent"]))
        if exponent != 1:
            return None
    if schedule.get("rate") is not None:
        rate = Decimal(str(schedule["rate"]))
        if rate < 0:
            return None
        return rate
    return WEATHER_FEE_RATE
