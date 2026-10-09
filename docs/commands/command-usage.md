# Command Usage Guide

> This is the English translation of [`command-usage.zh-TW.md`](command-usage.zh-TW.md); edit the Chinese version first, then sync this one.

This document collects common runtime commands: data updates (`apps.update_db`), data maintenance, backtesting (`apps.backtest`), and live trading (`apps.live`).

## Data Update: `python -m apps.update_db`

### Overview

`apps.update_db` is the entrypoint of the data update pipeline. Use `--target` to choose one or more update targets.
If `--target` is omitted, the default is `no_tick` (all datasets except **both** tick targets and the explicit-only targets).
A failing target does not stop the others, but the run exits with code 1 at the end; it also removes rotated `logs/api/` files older than 7 days on the way out.

### Parameter

- `--target <target> [<target> ...]`: one or multiple update targets.
- `--from YYYY-MM-DD`: pull the start date earlier than the default (see below).

### Target Reference

| Option | Description |
| --- | --- |
| `tick` | Tick-by-tick trades (Shioaji ticks) |
| `chip` | Institutional chip data |
| `price` | Closing prices |
| `margin` | Margin trading balances (financing / short-selling balances) |
| `short_sale_list` | Exchange list of securities that may be short-sold below the reference price, with daily suspension flags (from 2013-09-23; used by the backtest short checks) |
| `day_trade_list` | Exchange list of securities eligible for cash day trading, with the sell-first suspension flag (from 2014-01-06) |
| `dividend` | Ex-dividend / ex-rights table (adjustment factors + cash dividends) |
| `corporate_action` | Non-dividend corporate actions (capital reductions, splits, par-value changes) |
| `fs` | Financial statements (including the statement of changes in equity, which is queried per stock) |
| `mrr` | Monthly revenue report |
| `finmind` | All FinMind datasets (stock info + brokers + broker trading). **Not included in `all` or `no_tick`**: the current account level has no access to broker trading, so it would fail every night; run it by name |
| `stock_info` | FinMind stock info (without warrants) |
| `stock_info_with_warrant` | FinMind stock info (with warrants) |
| `broker_info` | FinMind broker info |
| `broker_trading` | FinMind broker trading stats |
| `futures_price` | TAIFEX daily futures quotes (written to `tw_futures.db`; products in `FUTURES_TARGET_PRODUCTS`) |
| `futures_stock_universe` | Stock futures universe (written to `tw_futures.db`; one snapshot per run date) |
| `futures_stock_price` | Stock futures quotes (product list from the universe, top-N by liquidity by default) |
| `futures_continuous` | Continuous futures contracts (rebuilt from `futures_price_daily`, no network access) |
| `futures_margin` | Futures margin (change series, written to `tw_futures.db`) |
| `futures_chip` | Futures chips (institutional investors, large traders, option PCR) |
| `market_holiday` | Market holiday schedule (TWSE announcement; re-fetches last / this / next year every run, written to `tw_stock.db`). Primary source for the live pre-open trading-day check |
| `futures_tick` | Futures tick trades (Shioaji → DolphinDB; requires the `[tick]` extra and credentials). **Not included in `all` or `no_tick`**: it has no resume record and re-running writes duplicate rows, so it only runs when named explicitly |
| `all` | All datasets (including tick; excludes `futures_tick`, `futures_stock_price` and `finmind`) |
| `no_tick` | All datasets except `tick` **and** `futures_tick` (default). Both need Shioaji credentials and the `[tick]` extra; without the exclusion a machine lacking them would exit 1 every night |

### Single Target Examples

```bash
# tick-by-tick trades
python -m apps.update_db --target tick

# institutional chip data
python -m apps.update_db --target chip

# closing prices
python -m apps.update_db --target price

# margin trading balances
python -m apps.update_db --target margin

# ex-dividend / ex-rights table (TWSE for listed, TPEx for OTC; full history)
python -m apps.update_db --target dividend

# non-dividend corporate actions (scans the whole range every run: events are announced after the fact)
python -m apps.update_db --target corporate_action

# financial statements
# The statement of changes in equity (equity_change) is queried per stock: one year-quarter is
# about 2,000 requests. Re-running only fills the difference set (stocks already in the table
# and stocks confirmed to have no data are skipped). Data shape and known limits:
# docs/pipeline/equity-change.md
python -m apps.update_db --target fs

# monthly revenue report
python -m apps.update_db --target mrr

# all FinMind datasets
python -m apps.update_db --target finmind

# FinMind stock info (without warrants)
python -m apps.update_db --target stock_info

# FinMind stock info (with warrants)
python -m apps.update_db --target stock_info_with_warrant

# FinMind broker info
python -m apps.update_db --target broker_info

# FinMind broker trading stats
python -m apps.update_db --target broker_trading

# TAIFEX daily futures quotes (written to tw_futures.db, not tw_stock.db)
# One product per query and day/night sessions are queried separately, so
# requests = products × 2 × trading days. The first backfill of TX alone from
# DEFAULT_FUTURES_START_DATE (2015-01-01) is about 6,100 requests.
python -m apps.update_db --target futures_price

# stock futures universe (written to tw_futures.db)
# The whole list is one GET; re-running on the same day does not create a second snapshot.
# The source has no listing / delisting date columns, so both are inferred by diffing
# snapshots — update daily, the sparser the snapshots the larger the date error.
# Downstream code must get the product list from
# FuturesStockUniverseUpdater.get_active_products(), never a hand-written list.
python -m apps.update_db --target futures_stock_universe

# short-sale-below-reference list and cash day-trading list (already part of all / no_tick; this runs them alone)
# A backfill from the start is about 3,200 trading days × two requests per day — hours at the current throttle
python -m apps.update_db --target short_sale_list day_trade_list

# market holiday schedule (written to the market_holiday table in tw_stock.db)
# One request per year; every run re-fetches last, this and next year and replaces each year whole.
# Next year's schedule is usually published in December; until then that year is logged as
# "not yet announced" and skipped, which is normal. Live trading refuses to start on a date whose
# year is not loaded (unless another source can answer).
python -m apps.update_db --target market_holiday

# all datasets (including tick)
python -m apps.update_db --target all

# all datasets except tick (same as default)
python -m apps.update_db --target no_tick

# default behavior (same as no_tick)
python -m apps.update_db
```

For the other futures targets (continuous contracts, margin, chip, tick) and their
backfill caveats, see [TW Futures Platform](../futures/tw-futures-platform.md), section 〈指令〉.

### Multi-Target Examples

```bash
python -m apps.update_db --target chip price
python -m apps.update_db --target chip price tick
python -m apps.update_db --target stock_info broker_trading
```

### `--from`: pull the start date earlier

```bash
python -m apps.update_db --target price --from 2013-01-01
```

**Rarely needed**: candidate dates are the difference set "calendar − already in
table − confirmed no data", so gaps in the middle are backfilled automatically.
Use `--from` only to start earlier than the default. It affects date-based targets
only; `fs` / `mrr` (year-quarter, year-month) are unaffected.

## Deleting one day of price data: `python -m apps.delete_price_data`

**Previews by default** — one wrong date drops a whole day of quotes for
thousands of stocks, recoverable only by re-running the ETL.

```bash
# Report the row count only, no write
python -m apps.delete_price_data --date 2025-07-13

# Actually delete; asks you to type the full date to confirm
python -m apps.delete_price_data --date 2025-07-13 --apply

# For schedulers: skip the interactive confirmation
python -m apps.delete_price_data --date 2025-07-13 --apply --yes
```

A non-interactive environment (no tty) without `--yes` refuses to run.

## Cleaning rotated logs: `python -m apps.clean_logs`

```bash
python -m apps.clean_logs                     # preview (no deletion)
python -m apps.clean_logs --apply             # delete, keeping 30 days by default
python -m apps.clean_logs --apply --bucket api --days 7
```

Only rotated files (timestamped names) are removed; active `xxx.log` files are kept.

## Backtest: `python -m apps.backtest --strategy <StrategyClassName>`

Replace `<StrategyClassName>` with your strategy class name.

```bash
python -m apps.backtest --strategy <StrategyClassName>
python -m apps.backtest --strategy <StrategyClassName> --show   # open the charts in a browser
python -m apps.backtest --strategy <StrategyClassName> --no-show   # do not open them (overrides ALPHAEDGE_SHOW_FIGURES)
# override the strategy's backtest period and initial capital (all optional; defaults come from the strategy)
python -m apps.backtest --strategy <StrategyClassName> --start 2024-01-01 --end 2024-12-31 --capital 500000
```

An unknown strategy name, a start date after the end date, a start date before the data start
(2013-01-01 for stocks, 2015-01-01 for futures), or a non-positive capital exits with code 2. If the strategy enables the
exchange-list checks (`check_short_sale_list` / `check_day_trade_list`) and the period starts before the list's start
date or the list has missing days, the reason is printed and the run exits with code 1 (run the matching `update_db`
target or move the start date). Results are written to `results/<StrategyName>/`.

## Live trading: `python -m apps.live --strategy <StrategyClassName[,…]> --phase <phase>`

```bash
# simulation environment (default), stock open phase
python -m apps.live --strategy VolumeBreakoutMomentumStrategy --phase open
# run the full flow without sending orders
python -m apps.live --strategy VolumeBreakoutMomentumStrategy --phase close --dry-run
# production: both flags are required
python -m apps.live --strategy VolumeBreakoutMomentumStrategy --phase open --production --confirm-production
# manually restore the trading mode (account level when no strategy is named); never schedule this
python -m apps.live --strategy VolumeBreakoutMomentumStrategy --phase open --resume-trading
# rebuild the attribution ledger from broker positions: plan only first, then rerun with --confirm-resync; no --phase
python -m apps.live --strategy VolumeBreakoutMomentumStrategy --resync-from-broker
```

| Flag | Description |
|------|-------------|
| `--strategy` | Strategy class name; comma-separate several (they share one account; stocks and futures cannot be mixed) |
| `--phase` | `open` / `close` / `after_close` / `intraday`; required except with `--resync-from-broker` |
| `--simulation` / `--production` | Simulation (default) / production; `--production` requires `--confirm-production` |
| `--broker` | `shioaji` (default) or `fake` (tests only, refused in production) |
| `--dry-run` | Run the full flow without actually sending orders |
| `--resume-trading [name ...]` | Manually restore the trading mode |
| `--resync-from-broker` / `--confirm-resync` | Rebuild the attribution ledger from broker positions; without `--confirm-resync` it only prints the plan |

Live trading **returns 1 only for unexpected exceptions**; every other outcome has its own code. These are the codes a scheduler should act on:

| Exit code | Meaning |
|:---:|---|
| 0 | Normal exit |
| 1 | Unexpected exception |
| 2 | Usage error: unknown strategy name, missing `--phase`, `--production` without `--confirm-production`, or an invalid flag combination |
| 3 | Data not updated through the previous trading day, or today's trading-day status cannot be determined |
| 4 | Reconciliation mismatch, or a refused rebuild from broker positions |
| 5 | Kill switch triggered |
| 6 | Account-level trading mode was not NORMAL at last exit and `--resume-trading` was not given |
| 7 | `--resync-from-broker` only printed the rebuild plan (no `--confirm-resync`) |
| 143 | SIGTERM received: open orders cancelled and the run record written before exit |

**Keep `6` separate from `4` and `5`**: 4/5 mean "something broke today", 6 means
"yesterday's problem is still unhandled". See [Live Deployment](../deployment/live-deployment.md).
