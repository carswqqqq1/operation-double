# Operation Double

Live trading stays off. This repository does not place an order, hold a private key, or connect a wallet. Nothing here calls an order endpoint.

## Weather paper books

These are our own paper books for daily city temperature markets, highest and lowest. They do not copy another wallet. The Bitcoin copy below is an older measured record, not this strategy.

A copied Bitcoin 5-minute print lost the only locked gain once it was held to resolution, and the price was usually already worse by the time it could have filled. These books do not repeat that. They do not trade 5-minute Bitcoin up/down.

There are 20 books. Each starts with $40.00 and has its own ledger. Tokyo is first. The others are NYC, London, Paris, Seoul, Chicago, Miami, Los Angeles, Dallas, Seattle, Atlanta, Toronto, Madrid, Shanghai, Beijing, Taipei, Munich, Wellington, Singapore, and Austin. Hong Kong is a live daily market and is not one of the books: it resolves from the Hong Kong Observatory, and these books only forecast a station whose coordinates were checked.

Each book may buy the Yes side of one temperature bin. The bin has to be the single most likely outcome in a public ensemble forecast at that city's resolution station. The order size is the minimum the exchange book publishes. On 2026-10-04 the live books that were checked said that minimum is 5 shares. The order is skipped when that minimum is missing, when fewer shares than the minimum are offered, when the minimum would cost more than $5, or when cash cannot cover it. The size is never cut below the minimum to make a trade fit.

Two prices are skipped even when the forecast likes them. An ask at 0.99 or higher is skipped: one miss costs almost the whole stake and the win is a few cents. An ask under 0.10 is skipped: a pile of small losers does not fit a $40 book unless the bin is already known to be mispriced, and a model probability is not that knowledge. The 30-wallet trade pull mentioned with this idea is not in the repository and was not downloaded.

The ask just observed is not a fill. The command waits (one second by default, or longer when the market publishes `secondsDelay`), reads the book again, and only then takes shares the second book is actually offering at the first ask or better. If that ask has moved up, the order is skipped. A price the second book did not show is never used.

The books hold to the station reading. They do not sell. A resolution pays $1 or $0 per share still held, with no fee. Open cost is cash already spent. It is not a gain. If the open shares pay nothing, cash is the book. A filled book is a paper record, not a promise of profit.

### Fee

Public docs still price crypto taker fees at `shares × 0.07 × price × (1 − price)`. Weather is a different row: `shares × 0.05 × price × (1 − price)`. Live weather markets also publish `feeSchedule.rate` 0.05 with exponent 1. These books use the market's published rate, and 0.05 when a weather market does not publish one. They do not use 0.07.

The formula result is USDC, rounded to 5 decimal places. Anything that rounds below 0.00001 USDC is zero. On a buy, that USDC fee is collected in shares at the fill price (`fee_usdc / price` fewer shares). Cash paid is the share price, not the fee on top. These books do not sell. A sell would pay the same USDC fee out of the proceeds. Resolutions have no fee.

The older Bitcoin paper module still records the fee the way that measured run recorded it. That record was not rewritten.

### Command

From the repository root:

```bash
python3 -m weather_paper
```

That prints cash, fees, open cost, and skips for each book, then `paper only; no live order`. It sends read-only HTTPS GETs to public event, book, and forecast endpoints. It does not place a live order. `--delay-seconds` changes the wait between the two book reads. The default is 1.

`python3 -m paper` is the older one-shot Bitcoin copy. It also places no order. It is not these temperature books, and running it rewrites `books/*.json`. Leave that record alone.

### One measured run (2026-10-04)

This is the print from one `python3 -m weather_paper` run. It is a record of that pass. It is not a promise.

```text
tokyo cash=39.30 fees=0.03010 open_cost=0.70 skips=5
nyc cash=33.20 fees=0.18500 open_cost=6.80 skips=2
london cash=35.45 fees=0.14088 open_cost=4.55 skips=3
paris cash=39.00 fees=0.04000 open_cost=1.0 skips=5
seoul cash=37.50 fees=0.06250 open_cost=2.5 skips=5
chicago cash=39.35 fees=0.02828 open_cost=0.65 skips=5
miami cash=31.45 fees=0.17928 open_cost=8.55 skips=1
los-angeles cash=38.25 fees=0.05688 open_cost=1.75 skips=5
dallas cash=36.60 fees=0.05440 open_cost=3.40 skips=5
seattle cash=35.25 fees=0.16104 open_cost=4.75 skips=3
atlanta cash=39.15 fees=0.03528 open_cost=0.85 skips=3
toronto cash=39.25 fees=0.03188 open_cost=0.75 skips=3
madrid cash=33.25 fees=0.10968 open_cost=6.75 skips=4
shanghai cash=39.30 fees=0.03010 open_cost=0.70 skips=5
beijing cash=35.90 fees=0.11970 open_cost=4.10 skips=4
taipei cash=34.85 fees=0.16898 open_cost=5.15 skips=3
munich cash=36.60 fees=0.11220 open_cost=3.40 skips=4
wellington cash=35.10 fees=0.11776 open_cost=4.90 skips=4
singapore cash=35.55 fees=0.11988 open_cost=4.45 skips=1
austin cash=35.975 fees=0.08447 open_cost=4.025 skips=4
```

Thirty-six paper buys were taken, all at the book's 5-share minimum, all with an ask from 0.10 up to but not including 0.99. Cash plus open cost is still $40 on every book, because the weather fee was taken in shares. On every fill, the second book read still showed the same ask; none of those orders had moved against the book during the one-second wait. Skips included cheap tails, a near-certain ask, no edge after the fee, an ambiguous forecast, a missing forecast date, and a book that did not offer the minimum. The ledgers for this pass are in gitignored `data/weather/books/`. They are not in git.

### Data layout

Paper state, market snapshots, and new downloads go in `data/`, which is gitignored. Do not commit trade dumps.

```text
data/weather/books/<city>.jsonl
```

One file per book. Each line is one JSON object. The file is append-only. A restart replays it and continues. Complete lines are not rewritten. A trailing partial line, from a crash mid-write, is cut so the next line can be appended.

Line types:

- `book_open` is the first line. It sets the $40 start, `paper_only`, and `live_orders: false`.
- `snapshot` is a book that was read, either the observation or the later fill book.
- `skip` is a decision not to trade, with a reason.
- `fill` is a paper buy taken from the second book only.
- `resolution` settles an open lot at 0 or 1 with no fee.

A repeated decision id does not apply twice. Cash, fees, open cost, and skip counts are rebuilt from these lines.

## A/B paper bots

`python3 -m weather_paper.ab` runs 20 paper bots until 8:00 AM America/Phoenix on October 4, 2026, then stops. `--once` runs a single pass. It sends the same read-only GETs as the city books. It does not place a live order.

Each bot starts at $40 and writes `data/bots/b01.jsonl` through `data/bots/b20.jsonl`. Those files are append-only and gitignored. A restart replays the current section and continues. The bots use different rules: edge size, price band, day-ahead dates, take-profit, open-position caps, high or low markets, Celsius or Fahrenheit, a few liquid cities, spread, and whether the market's ask agrees with the ensemble mode.

A buy is still not the first price. The command waits, reads the book again, and takes shares only when that second book still offers them at the first ask or better. One displayed size is used once. A second bot does not fill the same shares. The line records the fee rate, the USDC fee, and `fee_asset`. Buys pay the fee in shares. Sells pay it in USDC. Resolutions pay no fee. A weather market uses its published `feeSchedule.rate`, otherwise 0.05.

Cash above the $40 the current rule started with is a working rule. That bot is not reset. A rule that has lost at least $1 after a closed trade, or at least $2 on a real bid that can actually sell the minimum, is replaced. The file is copied to `data/bots/archive/` first. The old lines stay in the bot file. A new section then starts at $40 so the new rule is scored on its own. The replacement is a tighter rule, or a variant of a bot that is already above $40.

`data/bots/SUMMARY.txt` is the latest print of each bot's rule, section, cash, fees, open cost, and skips. The process stops on its own at 8:00 AM Phoenix.

### Stopped at 8:00 AM Phoenix (2026-10-04)

The A/B process stopped itself at 15:00 UTC. No live order was sent. No bot's current rule finished above the $40 it started with, so none was left as a working rule. Open cost is cash already spent. If those shares pay nothing, cash is the book.

The highest cash was `b09` on `one_open`: one fill, cash 39.35, fees 0.02828, open cost 0.65, 1 skip. That is the most cash left, not a realized profit.

```text
stopped 2026-10-04T15:00:17+00:00
best b09 one_open cash=39.35
b01 rule=modal_hold section=1 cash=32.25 fees=0.26264 open_cost=7.75 skips=0 fills=6 sells=0
b02 rule=edge_08 section=1 cash=33.45 fees=0.25624 open_cost=6.05 skips=3 fills=7 sells=0
b03 rule=edge_15 section=1 cash=32.35 fees=0.28374 open_cost=7.15 skips=4 fills=7 sells=0
b04 rule=band_20_60 section=1 cash=27.75 fees=0.37054 open_cost=10.90 skips=2 fills=7 sells=0
b05 rule=band_30_50 section=1 cash=28.75 fees=0.34264 open_cost=11.25 skips=5 fills=6 sells=0
b06 rule=day_ahead section=1 cash=30.50 fees=0.29690 open_cost=9.50 skips=6 fills=6 sells=0
b07 rule=exit_above_cost_tight_s2 section=2 cash=33.7616 fees=0.23394 open_cost=6.2384 skips=235 fills=6 sells=0
b08 rule=exit_plus_5c_tight_s2 section=2 cash=32.04208647058823529411764706 fees=0.40222 open_cost=8.3888 skips=0 fills=7 sells=1
b09 rule=one_open section=1 cash=39.35 fees=0.02828 open_cost=0.65 skips=1 fills=1 sells=0
b10 rule=two_open section=1 cash=38.60 fees=0.06016 open_cost=1.40 skips=1 fills=2 sells=0
b11 rule=largest_gap section=1 cash=32.30 fees=0.29792 open_cost=5.85 skips=8 fills=8 sells=0
b12 rule=high_only section=1 cash=34.10 fees=0.22846 open_cost=5.90 skips=9 fills=6 sells=0
b13 rule=low_only section=1 cash=21.90 fees=0.40582 open_cost=16.25 skips=2 fills=8 sells=0
b14 rule=liquid_hubs section=1 cash=29.60 fees=0.29632 open_cost=10.40 skips=3 fills=6 sells=0
b15 rule=tight_spread section=1 cash=30.65 fees=0.29404 open_cost=9.35 skips=2 fills=6 sells=0
b16 rule=fahrenheit_only section=1 cash=31.15 fees=0.29034 open_cost=8.35 skips=0 fills=7 sells=0
b17 rule=celsius_only section=1 cash=31.15 fees=0.28698 open_cost=8.85 skips=11 fills=6 sells=0
b18 rule=strong_mode section=1 cash=33.55 fees=0.25254 open_cost=5.95 skips=9 fills=7 sells=0
b19 rule=rally_tight_s2_tight_s3 section=3 cash=34.8314 fees=0.21182 open_cost=5.1686 skips=0 fills=6 sells=0
b20 rule=market_agrees section=1 cash=22.55 fees=0.33854 open_cost=17.45 skips=6 fills=6 sells=0
```

Buys paid the published 0.05 fee in shares. The one sell on the current `b08` section paid its fee in USDC. Resolutions paid no fee. Every fill had a first price and a second price. A price the second book did not show was not used.

Closed trades that paid 0, with no fee, were Los Angeles on `edge_08`, `edge_15`, `largest_gap`, `low_only`, `fahrenheit_only`, and `strong_mode`, and Seattle on `band_20_60`, `largest_gap`, and `low_only`. The first `exit_above_cost` section on `b07` also had a Los Angeles resolution at 0, after two sells whose proceeds cleared their cost. That section ended at about 29.49, under $40. No other bot was above $40, so `b07`, `b08`, and `b19` were each reset to a fresh $40 section on a tighter rule. The old lines stayed in the bot file and a copy was written under `data/bots/archive/`. Those new sections are the ones in the list above. `one_open` was not reset.

## Earlier Bitcoin copy record

Starting strategy for paper copies of @bosona on Polymarket Bitcoin up/down markets. Every figure in this document is a measured result from 2026-10-01. These results are a record. They are not a promise that a future book reaches the goal. The sentences below about going live are that old plan. They do not place an order, and the weather books do not use them.

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
