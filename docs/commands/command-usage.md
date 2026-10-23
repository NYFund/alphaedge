# Command Usage Guide

> This is the English translation of [`command-usage.zh-TW.md`](command-usage.zh-TW.md); edit the Chinese version first, then sync this one.

This document covers the five entrypoints under `apps/`: data updates (`apps.update_db`), data maintenance (`apps.delete_price_data`, `apps.clean_logs`), backtesting (`apps.backtest`), and live trading (`apps.live`).
`python -m apps.<entrypoint> --help` is the authoritative description of every flag.

## Data Update: `python -m apps.update_db`

### Overview

Use `--target` to choose one or more datasets to update; the default is `no_tick`.

- **Execution order is fixed** by the `TARGETS` registry in `apps/update_db.py`, regardless of the order given on the command line: `price` always runs before `chip` / `margin` / `tick` (they use the `price` table's trading days as their calendar).
- **A failing target does not stop the others**; if any target still fails after all runs (including retries), the process exits with code 1 and lists the failed and succeeded targets.
- On the way out it always removes rotated `logs/api/` files older than 7 days (this runs before the exit code is decided, so it also happens on failing days).

### Parameters

| Parameter | Description |
|-----------|-------------|
| `--target <target> [<target> ...]` | One or more datasets to update; default `no_tick` |
| `--from YYYY-MM-DD` | Override the start date of **date-based** targets (see `--from` below) |
| `--retry-failed ROUNDS` | Rerun **only the failed targets** up to this many more rounds (default `0`, no retry). Stops early once everything has succeeded; the exit code reflects the targets still failing after the last round |
| `--retry-wait MINUTES` | Minutes to wait before each retry round (default `60`; an exchange that blocks the source IP usually takes tens of minutes to lift it). The scheduled run uses `--retry-failed 4 --retry-wait 60` |

Negative values for `--retry-failed` or `--retry-wait` are a usage error (exit code 2).

### Target Reference

| Option | Description |
|--------|-------------|
| `price` | Closing prices |
| `tick` | Stock tick-by-tick trades (Shioaji ticks, stored in TimescaleDB) |
| `chip` | Institutional chip data |
| `margin` | Margin trading balances (financing / short-selling balances) |
| `short_sale_list` | Exchange list of securities that may be short-sold below the reference price, with daily suspension flags (from 2013-09-23; used by the backtest short checks) |
| `day_trade_list` | Exchange list of securities eligible for cash day trading, with the sell-first suspension flag (from 2014-01-06) |
| `dividend` | Ex-dividend / ex-rights table (adjustment factors + cash dividends) |
| `corporate_action` | Non-dividend corporate actions (capital reductions, splits, par-value changes) |
| `fs` | Financial statements (including the statement of changes in equity, queried per stock) |
| `mrr` | Monthly revenue report |
| `market_holiday` | Market holiday schedule (TWSE announcement; re-fetches last / this / next year every run, written to `tw_stock.db`). Primary source for the live pre-open trading-day check |
| `futures_price` | TAIFEX index futures daily quotes (written to `tw_futures.db`; products in `FUTURES_TARGET_PRODUCTS`) |
| `futures_stock_universe` | Stock futures universe (written to `tw_futures.db`; one snapshot per run date) |
| `futures_stock_price` | Stock futures quotes (product list from the universe, top `STOCK_FUTURES_TOP_N` by liquidity only). **Explicit only** |
| `futures_continuous` | Continuous futures contracts (fully rebuilt from `futures_price_daily`, no network access) |
| `futures_margin` | Current futures margin table (change series, written to `tw_futures.db`) |
| `futures_chip` | Futures chips (institutional investors, large traders, option PCR) |
| `finmind` | All FinMind datasets (stock info + brokers + broker trading). **Explicit only**: the current account level has no access to broker trading, so it would fail every night in the default set |
| `stock_info` | FinMind stock info (without warrants). **Explicit only** |
| `stock_info_with_warrant` | FinMind stock info (with warrants). **Explicit only** |
| `broker_info` | FinMind broker info. **Explicit only** |
| `broker_trading` | FinMind broker trading stats. **Explicit only** |
| `all` | Everything above **except** the explicit-only targets |
| `no_tick` | `all` minus `tick` (**default**). Tick needs Shioaji credentials, the `[tick]` extra and a running TimescaleDB; without the exclusion a machine lacking them would exit 1 every night |

Explicit-only targets are never selected by `all` / `no_tick`; name them in `--target`.

### Examples

```bash
python -m apps.update_db                                  # same as --target no_tick
python -m apps.update_db --target price                   # single target
python -m apps.update_db --target chip price tick         # several targets (still run in registry order)
python -m apps.update_db --target futures_stock_price     # an explicit-only target
python -m apps.update_db --retry-failed 4 --retry-wait 60 # rerun failed targets hourly, at most 4 rounds
```

### Per-target notes

- **`fs`**: the statement of changes in equity (`equity_change`) is queried per stock, about 2,000 requests per year-quarter; re-running only fills the difference set (stocks already stored and stocks confirmed to have no data are skipped). Data shape and known limits: [Statement of Changes in Equity](../pipeline/equity-change.md).
- **`dividend`**: TWSE for listed, TPEx for OTC; full history.
- **`corporate_action`**: scans the whole range every run — these events are announced after the fact.
- **`short_sale_list` / `day_trade_list`**: already part of `all` / `no_tick`; a backfill from the start is about 3,200 trading days × two requests per day, hours at the current throttle.
- **`market_holiday`**: one request per year; every run re-fetches last, this and next year and replaces each year whole. Next year's schedule is usually published in December; until then that year is logged as "not yet announced" and skipped, which is normal. Live trading refuses to start on a date whose year is not loaded if no other calendar source can answer.
- **`futures_price`**: one product per query and day/night sessions are queried separately, so requests = products × 2 × trading days; from `DEFAULT_FUTURES_START_DATE` (2015-01-01), the first backfill of TX alone is about 6,100 requests.
- **`futures_stock_universe`**: the whole list is one GET; re-running on the same day does not create a second snapshot. The source has no listing / delisting date columns, so both are inferred by diffing snapshots — update daily, since sparser snapshots mean larger date errors. Downstream code must get the product list from `FuturesStockUniverseAPI.get_products()`, never a hand-written list.

Backfill details for the futures targets (listing dates of new products, stock futures liquidity ranking, historical margin announcements) are in [TW Futures Platform](../futures/tw-futures-platform.md), section 〈2.3 指令〉.

### `--from`: pull the start date earlier

```bash
python -m apps.update_db --target price --from 2013-01-01
```

**Rarely needed**: candidate dates of date-based updaters are the difference set "calendar − already in table − confirmed no data", so gaps in the middle are backfilled automatically. Use `--from` only to start earlier than the default.

- It only affects targets with a date range; `fs` / `mrr` (year-quarter, year-month) and targets without a range (`futures_chip`, `futures_stock_universe`, `futures_margin`, `futures_continuous`, `market_holiday`, and the FinMind `stock_info` / `stock_info_with_warrant` / `broker_info`) are unaffected.
- **It cannot extend `futures_price` / `futures_stock_price` backwards**: the futures difference set starts at the product's first stored row, so a `--from` before that day backfills nothing. To extend earlier, call `FuturesPriceUpdater.update(start_date=..., end_date=..., resume=False)` directly.

## Deleting one day of price data: `python -m apps.delete_price_data`

Deletes every row for the given date from the `price` table in `tw_stock.db`. **Previews by default** — one wrong date drops a whole day of quotes for thousands of stocks, recoverable only by re-running the ETL.

```bash
# Report the row count only, no write
python -m apps.delete_price_data --date 2025-07-13

# Actually delete; asks you to type the full date to confirm
python -m apps.delete_price_data --date 2025-07-13 --apply

# For schedulers: skip the interactive confirmation
python -m apps.delete_price_data --date 2025-07-13 --apply --yes
```

- `--date` accepts `YYYY-MM-DD` or `YYYY/MM/DD` (e.g. `2025/7/13`).
- In a non-interactive environment (no tty), a run without `--yes` is treated as unconfirmed and cancelled.
- An unparsable date, a missing database, no confirmation, or rows remaining after deletion exits with code 1; a date that already has no data counts as success.

## Cleaning rotated logs: `python -m apps.clean_logs`

```bash
python -m apps.clean_logs                              # preview (no deletion)
python -m apps.clean_logs --apply                      # delete, keeping 30 days by default
python -m apps.clean_logs --apply --bucket api --days 7
```

| Parameter | Description |
|-----------|-------------|
| `--days DAYS` | Days to keep (default 30) |
| `--bucket {api,pipeline,backtest}` | Only clean the given bucket; repeatable. All three buckets when omitted |
| `--apply` | Actually delete; without it only lists the files that would be deleted |

Only rotated files with a timestamp in the name (`<name>.<timestamp>.log`) are removed; active `xxx.log` files are always kept — deleting a file loguru is still writing to makes the log silently disappear. `apps.update_db` cleans the `api` bucket with a 7-day retention on every run.

## Backtest: `python -m apps.backtest --strategy <StrategyClassName>`

`--strategy` takes **the strategy's class name**, one at a time; available strategies are the non-abstract subclasses under `strategies/stock/` and `strategies/futures/` (for example `VolumeBreakoutMomentumStrategy`, `MomentumFuturesStrategy`).

```bash
python -m apps.backtest --strategy <StrategyClassName>
python -m apps.backtest --strategy <StrategyClassName> --show      # open the charts in a browser
python -m apps.backtest --strategy <StrategyClassName> --no-show   # do not open them (overrides ALPHAEDGE_SHOW_FIGURES)
# override the strategy's backtest period and initial capital (all optional; defaults come from the strategy)
python -m apps.backtest --strategy <StrategyClassName> --start 2024-01-01 --end 2024-12-31 --capital 500000
# switch to TICK scale and read the whole market's ticks
python -m apps.backtest --strategy <StrategyClassName> --scale TICK --tick-universe all
```

| Parameter | Description |
|-----------|-------------|
| `--strategy` | Strategy class name (required, exactly one) |
| `--start` / `--end YYYY-MM-DD` | Override the backtest start / end date |
| `--capital` | Override the initial capital |
| `--scale {TICK,DAY}` | Backtest scale to use instead; only scales listed in the strategy's `supported_scales` are accepted |
| `--tick-universe {strategy,all}` | Which symbols a TICK backtest reads each day: `strategy` = the strategy's `get_live_symbols()` list plus holdings (default, same as live), `all` = the whole market (millions of rows a day, slower). Not allowed for DAY backtests |
| `--show` / `--no-show` | Whether to open the charts in a browser afterwards; when neither is given the `ALPHAEDGE_SHOW_FIGURES` environment variable decides, default off (charts are always saved to files) |

This entrypoint only knows backtest flags: a live flag such as `--production` is rejected by argparse as a usage error.

| Exit code | Meaning |
|:---:|---|
| 0 | Normal exit |
| 1 | Invalid strategy settings (a removed setting field, or the strategy's own `scale` contradicts `supported_scales`); the strategy enables the exchange-list checks (`ShortConstraint.check_short_sale_list` / `check_day_trade_list`) and the period starts before the list's start date or the list has missing days; or the period contains no trading day at all. The reason is printed to stderr — run the matching `update_db` target or change the period. Any other unexpected exception is also 1 |
| 2 | Usage error: unknown strategy name (available strategies are listed), unknown flag, start after end, start before the data start (2013-01-01 for stocks, 2015-01-01 for futures), non-positive capital, a `--scale` the strategy does not support, `--tick-universe` on a DAY backtest, or `strategy` universe on a TICK backtest whose strategy does not override `get_live_symbols()` |

Results are written to `results/<StrategyName>/` (the root can be changed with the `ALPHAEDGE_RESULTS_DIR` environment variable).

## Live trading: `python -m apps.live --strategy <StrategyClassName[,…]> --phase <phase>`

```bash
# simulation environment (default), open phase
python -m apps.live --strategy VolumeBreakoutMomentumStrategy --phase open
# run the full flow without sending orders
python -m apps.live --strategy VolumeBreakoutMomentumStrategy --phase close --dry-run
# production: both flags are required
python -m apps.live --strategy VolumeBreakoutMomentumStrategy --phase open --production --confirm-production
# after the next-day data update, re-compare the previous trading day's signal parity (no broker, no orders)
python -m apps.live --strategy VolumeBreakoutMomentumStrategy --phase parity --date 2026-10-15
# manually restore the trading mode (account level when no strategy is named); never schedule this
python -m apps.live --strategy VolumeBreakoutMomentumStrategy --phase open --resume-trading
# rebuild the attribution ledger from broker positions: plan only first, then rerun with --confirm-resync; no --phase
python -m apps.live --strategy VolumeBreakoutMomentumStrategy --resync-from-broker
```

| Flag | Description |
|------|-------------|
| `--strategy` | Strategy class name; comma-separate several (they share one account). Stock and futures phase windows do not overlap, so they run as two separately scheduled processes; mixing them in one command is rejected during assembly |
| `--phase` | `open` / `close` / `after_close` / `intraday` / `parity`; required except with `--resync-from-broker`. `after_close` sends no new orders (refreshes orders, reconciles, back-fills costs, handles leftovers, writes reports); `parity` re-compares the previous trading day after the next-day data update, with no broker connection and no orders |
| `--date YYYY-MM-DD` | Only with `--phase parity`: the trading day to compare, defaults to the latest date in the historical data |
| `--simulation` / `--production` | Simulation (default) / production; `--production` requires `--confirm-production`. There is deliberately no environment variable for production |
| `--broker` | `shioaji` (default) or `fake`. `fake` is only for tests that inject it directly; from the command line it is always rejected with exit code 2 |
| `--dry-run` | Run the full flow without actually sending orders |
| `--resume-trading [name ...]` | Manually restore the trading mode: with no names, the account level and every strategy; with names, only those strategies (they must be in `--strategy`), leaving the account level as is |
| `--resync-from-broker` / `--confirm-resync` | Rebuild the attribution ledger from broker positions; without `--confirm-resync` it only prints the plan |

All validation happens before any broker connection is made. Live trading **returns 1 only for unexpected exceptions**; every other outcome has its own exit code for the scheduler to act on:

| Exit code | Meaning |
|:---:|---|
| 0 | Normal exit |
| 1 | Unexpected exception |
| 2 | Usage error: unknown or duplicated strategy name, missing `--phase`, `--date` without `--phase parity`, `--production` without `--confirm-production`, the `fake` broker, a `--resume-trading` name that is not in `--strategy`, an invalid combination of the resync flags, strategies whose phase windows do not overlap, or a strategy setting live trading does not support (for example the futures `OPEN_INTEREST` roll rule) |
| 3 | Data not updated through the previous trading day, or today's trading-day status cannot be determined |
| 4 | Reconciliation mismatch, or a refused rebuild from broker positions |
| 5 | Kill switch triggered |
| 6 | Account-level trading mode was not NORMAL at last exit and `--resume-trading` was not given |
| 7 | `--resync-from-broker` only printed the rebuild plan (no `--confirm-resync`) |
| 143 | SIGTERM received: open orders cancelled and the run record written before exit |

**Keep `6` separate from `4` and `5`**: 4/5 mean "something broke today", 6 means "yesterday's problem is still unhandled". **`7` is not 0**: a plan-only run means the ledger still disagrees with the broker.
For the schedule and full operating procedure see [Live Deployment](../deployment/live-deployment.md).
