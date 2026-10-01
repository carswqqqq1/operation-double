# Operation Double

Starting strategy for paper copies of @bosona on Polymarket Bitcoin up/down markets. Every figure in this document is a measured result from 2026-10-01. These results are a record. They are not a promise that a future book reaches the goal.

## Goal

$75 of paper cash, starting from $37.40, from repeatable fills.

Real money stays off until a paper book reaches $75 cash from those fills. One market resolving at $1 is not that proof. The real $37.40 stays off. Live only when a paper book has $75 cash from repeatable fills, not from one resolution.

No measured book reached $75.

## Trader

- Handle: @bosona
- Wallet: `0xc2ad03f79ca3f3c17d8c7de2612ce0c89b7d40ed`
- Markets: Polymarket Bitcoin up/down
- Public history: buys only

## Copy rules

A copy is his exact print:

- His exact share count. Scaling size is out. Five-share slices that were not his size were a mistake.
- The same side.
- The same market.
- His price or better, and only shares actually offered.
- Skip the copy if the book cannot fill that exact size at his price or better.
- Skip the copy if cash cannot cover it.
- Inventing a fill is out.
- Copying at a worse price is out.
- An unmarked open position is not a gain. If opens pay nothing, cash is the book.

Most skips in the measured books were `latency_worse_than_leader_price`.

## Fee

Polymarket crypto taker fee on every fill:

```text
shares × 0.07 × price × (1 − price)
```

The fee is worst near 50 cents. Fees ate small edges.

- Buys pay the fee in shares.
- Sells pay the fee in USDC.
- Resolutions have no fee.

## Same-minute exit rule

Copy his exact print, then sell that position in the same minute only if the bid is above paper cost.

If it cannot be sold above cost in that minute, do not carry it into the next minute.

Paper sells only when he has printed a sell, or when the same-minute bid is above paper cost. Do not invent a sell he did not make.

One resolution paying $1 is not proof the strategy works.

Selling in the same minute the position was bought, and only when the bid is above paper cost, is the only exit that booked a small gain instead of a resolution coin flip. It is not proven to $75.

## Measured results (2026-10-01)

Labeled as measured. No book reached $75.

### What did not work

#### Holding a copied position to resolution

The Cursor 5-minute-only book locked cash at $50.17042572333333333333333333 (realized +$12.77042572333333333333333333, fees $1.43735, open cost $0, 13 buys). Then three exact Up copies on `btc-updown-5m-1790879100` resolved at 0. Cash fell to $14.23842572333333333333333333, realized −$23.16157427666666666666666667, fees $3.35538, open cost $0, 16 buys. The locked cash is gone. Nothing was left open. Holding into the next minute wiped the only locked gain.

#### A one-resolution jump

An earlier book looked like cash about $94.54 / realized about +$55.54 because one Up resolution paid $1 (cost about $41.58). Later resolutions gave back about $74. Same-minute sells in that run made only about $0.87. That is not a repeatable edge.

#### Overnight book

The overnight $38 book stopped at about $39.34, up $1.34, mostly one resolution (`btc-updown-5m-1790832000` Up, 39.30 shares, cost $26.07, paid $39.30). It did not reach $76.

#### Selling in any minute

The Cursor any-minute book finished at cash $17.10680701696969696969696971, realized −$20.29319298303030303030303029, fees $7.66203, open cost $0, 23 copies, 74 skips. It had earlier been cash $39.46, realized +$2.06. Free selling lost money.

#### Skipping every buy over 60 cents

Skipping every buy over 60 cents did not produce $75. Last Cursor mark: cash $29.66279546, realized $0.11279546, fees $0.60161, open cost $7.85, 6 buys. Cash plus open cost about $37.51.

#### Half-cash copies

Last Cursor mark: cash $37.85715571428571428571428572, realized +$0.457155714285714285714285717, fees $1.17237, open cost $0, 6 buys. It had been $38.44 and slipped when paper sells were filled though he had not printed a sell. That is forbidden.

#### Must-exit-above-cost with a whole-position bid check

That check copied nothing. Flat at $37.40, 0 buys, 0 fees.

#### Worse price, invented fills, scaled size, unmarked opens

Copying at a worse price, inventing a fill, scaling share count, or counting an unmarked open position as a gain did not produce the goal. Most skips were `latency_worse_than_leader_price`. Fees ate small edges.

#### Local books on 2026-10-01

None at $75. If opens pay nothing, cash is the book.

| Book | Cash | Open |
| --- | --- | --- |
| original | $0.24 | about $52.48 unmarked |
| any-minute | $27.99 | about $34.93 |
| skip-over-60 | $1.15 | about $58.22 |
| half-cash | $0.05 | about $37.77 |
| 5-minute-only | $0.01 | about $40.42 |
| must-exit | $37.40 flat | 0 copies |

### What worked, narrowly

- Exact copies only: his exact share count, same side, same market, his price or better, and only shares actually offered.
- Selling in the same minute the position was bought, and only when the bid is above paper cost. That is the only exit that booked a small gain instead of a resolution coin flip. It is not proven to $75.
- Paper sells only when he has printed a sell, or when the same-minute bid is above paper cost. Do not invent a sell he did not make, and do not carry the position into the next minute.
- Real $37.40 stays off. Live only when a paper book has $75 cash from repeatable fills, not from one resolution.

## Paper simulator

Two paper books copy @bosona's public Bitcoin up/down prints. Each book starts at $37.40. The goal is still $75 cash from repeatable fills. Real money stays off. This simulator does not place an order, hold a private key, or run a loop.

1. `books/same_minute.json`. Exact copy, then sell that position in the same minute only if the bid is above paper cost. If the minute ends without that bid, the shares leave the book with no sale proceeds and no resolution payout. They are not carried into the next minute. This is the only exit that booked a small gain. It is not proven to $75.
2. `books/hold_to_resolution.json`. Control. Exact copy, then hold to resolution, so the measured failure that locked $50.17042572333333333333333333 and then fell to $14.23842572333333333333333333 stays visible. It is not a candidate.

A copy is his exact share count, the same side, the same market, his price or better, and only shares the public book is offering. The decision log records his transaction hash and the latency from his print to the decision. The copy is skipped when that exact size is not offered at his price or better, or when cash cannot cover it. Size is not scaled. A fill is not invented. A sell is copied only when he printed that sell. The same-minute exit is the only other sell.

The fee on every fill is `shares × 0.07 × price × (1 − price)`. Buys pay it in shares. Sells pay it in USDC. Resolutions have no fee.

These failed rules are not books: selling in any minute, skip-over-60 as a standalone edge, half-cash sizing, five-share slices that are not his size, copying at a worse price, and counting an unmarked open position as a gain. Open cost is the cost of shares still held. It is not added to cash. Realized is `cash + open cost − 37.40`, which keeps an open position at cost. If opens pay nothing, cash is the book.

From the repository root, one shot:

```bash
python3 -m paper
```

That command sends read-only HTTPS GETs to the public trades, book, and event endpoints, then writes `books/same_minute.json`, `books/hold_to_resolution.json`, and `runs/<UTC timestamp>.json`. Each record stores cash, realized, fees, open cost, copies, skips, and latency. It exits. It does not place a live order.

The first one-shot, `runs/20261001T220006Z.json`, is a measured record of that pass. It is not a promise.

- `same_minute`: cash 37.40, realized 0.00, fees 0, open cost 0, copies 0, skips 50. Latency milliseconds: count 50, min 178625, max 10455625, last 178625. Skips were 43 `minute_elapsed` and 7 `not_bitcoin_up_down`.
- `hold_to_resolution`: cash 35.5213, realized 0.0000, fees 0.13019391, open cost 1.8787, copies 2, skips 48. Latency milliseconds: count 50, min 178625, max 10455625, last 178625. The two copies were displayed asks at 0.01, better than his prices, on `btc-updown-5m-1790891700` (`0x1361c2aa84c68bd9b4a8b64c3a8fcbb2f2e9607f9a21f255dd9562d71182f825` and `0x9cf57f1e0af430fd97225724b02f323abc10c6656b4989ac8d2ec7beba078a92`). Open cost is still cost, not cash. Skips were 38 `book_unreadable`, 7 `not_bitcoin_up_down`, and 3 `latency_worse_than_leader_price`.

## Scope

The measured results above are a record, not a promise. The simulator is the paper path. Live orders stay out until a paper book has $75 cash from repeatable fills, not from one resolution.
