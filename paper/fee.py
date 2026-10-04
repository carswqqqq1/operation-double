"""Polymarket crypto taker fee.

shares × 0.07 × price × (1 − price)

Buys pay this amount in shares. Sells pay it in USDC.
Resolutions have no fee. The amount is largest near 50 cents.
"""

from decimal import Decimal

FEE_RATE = Decimal("0.07")


def taker_fee(shares: Decimal, price: Decimal) -> Decimal:
    if shares <= 0:
        raise ValueError("fee shares must be positive")
    if price < 0 or price > 1:
        raise ValueError("fee price must be between 0 and 1")
    return shares * FEE_RATE * price * (Decimal(1) - price)
