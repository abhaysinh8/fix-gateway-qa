# fix-gateway-qa

## Project Overview

`fix-gateway-qa` is a twelve-phase Python QA portfolio project modeled on the
workflows used to test electronic-trading infrastructure. It builds the protocol,
exchange simulator, client, fault injection, independent evidence checks, and reporting
as one executable system rather than a collection of isolated examples.

**Protocol conformance.** The FIX layer implements FIX 4.4 message encoding, decoding,
framing, BodyLength/CheckSum calculation, and semantic validation for common order-flow
messages. The numerical regression layer uses `Decimal` for tick rounding, notional,
weighted average fill price, PnL, and currency rounding, with deliberately naive float
implementations that make fixed-point drift visible in tests and the demo.

**Lifecycle correctness.** A formal order state machine defines the only valid order
transitions and retains full transition history. Hypothesis explores arbitrary valid
and invalid event sequences, while TCP lifecycle tests drive the same model through an
in-memory exchange. A normalized audit trail can reconstruct each lifecycle using only
logged messages, check timestamp and event completeness, and cross-check the result
against the live state machine.

**Performance.** Every client request captures round-trip latency. The regression
check reports p50, p99, p99.9, and max and compares p99 with a versioned JSON baseline.
The duration-based soak runner adds burst and sustained order/cancel traffic while
sampling process memory, connection count, and rolling latency to expose leaks or
degradation over time.

**Resilience.** Configurable chaos injects response delays, drops, duplicate Execution
Reports, and cross-order reordering. The client deduplicates by ExecID, isolates local
state by ClOrdID, times out cleanly, and performs bounded idempotent retries; the
exchange recognizes a retried ClOrdID without creating or filling an order twice.

**Market integrity.** The market-data subsystem encodes FIX-like snapshots and
incrementals, reconstructs aggregate books, validates checksums, and detects gaps,
duplicates, and out-of-order delivery. Its recovery policy marks a book stale until a
fresh snapshot arrives. Structured `spoofing-like` and `layering-like` detectors supply
educational surveillance signals. These are simplified QA heuristics—not real
surveillance, MiFID reporting, or compliance determinations.

Jinja2 ties all of that evidence together in a self-contained
[sample HTML report](reports/report.html), with a clearly labeled section for the test
suite, latency baseline, precision examples, audit completeness, market-data replay,
chaos/idempotency, soak time series, and surveillance matches.

## Architecture

```text
fix/ (messages, framing, validation, Decimal precision)
  └─> exchange/ (state machine, order book, TCP mock exchange)
       <─TCP─> client/ (requests, latency, retries, idempotency)
                  │
                  ├─> audit/         reconstructs exchange wire history
                  ├─> surveillance/  analyzes order-action timelines
                  ├─> marketdata/    reuses FIX wire integrity for W/X replay
                  └─> reporting/     latency + soak metrics + unified HTML
```

The order-entry exchange and client are the live core. Audit and surveillance consume
their output, market data is an adjacent conformance subsystem built on the same FIX
primitives, and reporting assembles evidence from every subsystem.

## How to run

Python 3.11 or newer is required.

```bash
python -m pip install -r requirements.txt
python run_demo.py
pytest
```

`python run_demo.py` runs the full story and refreshes `reports/report.html`. Add
`--chaos` to enable low-rate chaos on the main mixed-order flow; the demo always runs
its deterministic chaos/idempotency proof. Use `--soak-seconds N` to change the short
demo soak duration. The full test suite is available directly through `pytest`.

The latency check can also run independently:

```bash
python -m fixgateway.reporting.run_latency_check --orders 200
```

Set `UPDATE_BASELINE=1` to intentionally replace
`reports/latency_baseline.json`. `LATENCY_ORDER_COUNT` and
`LATENCY_REGRESSION_THRESHOLD` tune the pytest latency run; the default p99 regression
budget is 20 percent.

| Phase | Focus | Tests / executable evidence |
|---:|---|---|
| 1 | FIX encode/decode and validation | `tests/test_message_encoding.py`, `tests/test_message_validation.py` |
| 2 | State machine and order book | `tests/test_state_machine_property.py`, `tests/test_order_book.py` |
| 3 | TCP exchange/client lifecycle | `tests/test_order_lifecycle.py` |
| 4 | Latency percentiles and baseline | `tests/test_latency.py` |
| 5 | Surveillance heuristics | `tests/test_surveillance_patterns.py` |
| 6 | HTML report and timeout chaos | `tests/test_report_generator.py`, `tests/test_chaos_mode.py` |
| 7 | Decimal precision regression | `tests/test_precision_regression.py` |
| 8 | Audit completeness and reconstruction | `tests/test_audit_trail.py` |
| 9 | Market-data replay and gap detection | `tests/test_market_data_conformance.py` |
| 10 | Duplicate/reorder chaos and idempotency | `tests/test_chaos_idempotency.py` |
| 11 | Duration-based soak health | `tests/test_soak.py` |
| 12 | Unified demo and report | `run_demo.py`, `tests/test_report_generator.py` |

## Capabilities at a glance

- Hand-built SOH-delimited FIX 4.4 encoder, decoder, stream framing, and validation
- Deterministic order lifecycle model with property-based transition testing
- Real TCP mock exchange and synchronous/concurrent client
- Latency regression baselines and duration-based memory/latency soak trends
- Decimal financial arithmetic with explicit float-drift demonstrations
- Reconstructable, queryable order audit trail and state-machine cross-checking
- Market-data snapshot/incremental replay with conservative sequence-gap recovery
- Delay, drop, duplicate, and reorder chaos with idempotent bounded retries
- Structured educational surveillance heuristics
- One-command unified HTML evidence report

## Resume bullet suggestion

Built a FIX protocol trading infrastructure QA suite in Python spanning protocol
conformance, model-based lifecycle testing, latency regression tracking, market data
conformance/gap detection, chaos/idempotency testing, and rule-based surveillance—with
automated HTML reporting, modeled on real trading infrastructure QA workflows.
