# Command Usage Guide

> This is the English translation of [`command-usage.zh-TW.md`](command-usage.zh-TW.md); edit the Chinese version first, then sync this one.

This document collects common runtime commands: data updates (`tasks.update_db`), data maintenance, and backtesting (`run.py`).

## Data Update: `python -m tasks.update_db`

### Overview

`tasks.update_db` is the entrypoint of the data update pipeline. Use `--target` to choose one or more update targets.
If `--target` is omitted, the default is `no_tick` (all datasets except **both** tick targets).

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
| `dividend` | Ex-dividend / ex-rights table (adjustment factors + cash dividends) |
| `corporate_action` | Non-dividend corporate actions (capital reductions, splits, par-value changes) |
| `fs` | Financial statements (including the statement of changes in equity, which is queried per stock) |
| `mrr` | Monthly revenue report |
| `finmind` | All FinMind datasets (stock info + brokers + broker trading) |
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
| `futures_tick` | Futures tick trades (Shioaji → DolphinDB; requires the `[tick]` extra and credentials) |
| `all` | All datasets (including tick) |
| `no_tick` | All datasets except `tick` **and** `futures_tick` (default). Both need Shioaji credentials and the `[tick]` extra; without the exclusion a machine lacking them would exit 1 every night |

### Single Target Examples

```bash
# tick-by-tick trades
python -m tasks.update_db --target tick

# institutional chip data
python -m tasks.update_db --target chip

# closing prices
python -m tasks.update_db --target price

# margin trading balances
python -m tasks.update_db --target margin

# ex-dividend / ex-rights table (TWSE for listed, TPEx for OTC; full history)
python -m tasks.update_db --target dividend

# non-dividend corporate actions (scans the whole range every run: events are announced after the fact)
python -m tasks.update_db --target corporate_action

# financial statements
# The statement of changes in equity (equity_change) is queried per stock: one year-quarter is
# about 2,000 requests. Re-running only fills the difference set (stocks already in the table
# and stocks confirmed to have no data are skipped). Data shape and known limits:
# docs/pipeline/equity-change.md
python -m tasks.update_db --target fs

# monthly revenue report
python -m tasks.update_db --target mrr

# all FinMind datasets
python -m tasks.update_db --target finmind

# FinMind stock info (without warrants)
python -m tasks.update_db --target stock_info

# FinMind stock info (with warrants)
python -m tasks.update_db --target stock_info_with_warrant

# FinMind broker info
python -m tasks.update_db --target broker_info

# FinMind broker trading stats
python -m tasks.update_db --target broker_trading

# TAIFEX daily futures quotes (written to tw_futures.db, not tw_stock.db)
# One product per query and day/night sessions are queried separately, so
# requests = products × 2 × trading days. The first backfill of TX alone from
# DEFAULT_FUTURES_START_DATE (2015-01-01) is about 6,100 requests.
python -m tasks.update_db --target futures_price

# stock futures universe (written to tw_futures.db)
# The whole list is one GET; re-running on the same day does not create a second snapshot.
# The source has no listing / delisting date columns, so both are inferred by diffing
# snapshots — update daily, the sparser the snapshots the larger the date error.
# Downstream code must get the product list from
# FuturesStockUniverseUpdater.get_active_products(), never a hand-written list.
python -m tasks.update_db --target futures_stock_universe

# all datasets (including tick)
python -m tasks.update_db --target all

# all datasets except tick (same as default)
python -m tasks.update_db --target no_tick

# default behavior (same as no_tick)
python -m tasks.update_db
```

For the other futures targets (continuous contracts, margin, chip, tick) and their
backfill caveats, see [TW Futures Platform](../futures/tw-futures-platform.md), section 〈指令〉.

### Multi-Target Examples

```bash
python -m tasks.update_db --target chip price
python -m tasks.update_db --target chip price tick
python -m tasks.update_db --target stock_info broker_trading
```

### `--from`: pull the start date earlier

```bash
python -m tasks.update_db --target price --from 2013-01-01
```

**Rarely needed**: candidate dates are the difference set "calendar − already in
table − confirmed no data", so gaps in the middle are backfilled automatically.
Use `--from` only to start earlier than the default. It affects date-based targets
only; `fs` / `mrr` (year-quarter, year-month) are unaffected.

## Deleting one day of price data: `python -m tasks.delete_price_data`

**Previews by default** — one wrong date drops a whole day of quotes for
thousands of stocks, recoverable only by re-running the ETL.

```bash
# Report the row count only, no write
python -m tasks.delete_price_data --date 2025-07-13

# Actually delete; asks you to type the full date to confirm
python -m tasks.delete_price_data --date 2025-07-13 --apply

# For schedulers: skip the interactive confirmation
python -m tasks.delete_price_data --date 2025-07-13 --apply --yes
```

A non-interactive environment (no tty) without `--yes` refuses to run.

## Cleaning rotated logs: `python -m tasks.clean_logs`

```bash
python -m tasks.clean_logs                     # preview (no deletion)
python -m tasks.clean_logs --apply             # delete, keeping 30 days by default
python -m tasks.clean_logs --apply --bucket api --days 7
```

Only rotated files (timestamped names) are removed; active `xxx.log` files are kept.

## Backtest: `python run.py --strategy <StrategyClassName>`

Replace `<StrategyClassName>` with your strategy class name.

```bash
python run.py --strategy <StrategyClassName>
python run.py --strategy <StrategyClassName> --show   # open the charts in a browser
```

An unknown strategy name exits with code 2; `--mode live` is not implemented and exits with code 1.
Results are written to `results/<StrategyName>/`.
