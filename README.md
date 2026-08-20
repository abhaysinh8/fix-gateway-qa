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
python -m fixgateway.reporting.benchmark_runner
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

## Reproducible benchmark results

Run the benchmark suite with:

```bash
python -m fixgateway.reporting.benchmark_runner
```

The command writes the full sample distributions, configuration, host metadata, and
95% confidence intervals to
[`reports/benchmark_results.json`](reports/benchmark_results.json). Workload sizes are
configurable through CLI flags; run `python -m
fixgateway.reporting.benchmark_runner --help` for all options.

Latest local run: **20 August 2026**, Windows 11, CPython 3.14.3, Intel64 Family 6
Model 140 (4 physical / 8 logical cores), 15.7 GiB RAM. Each in-process result used
seven measured rounds after warm-up: 10,000 operations per round, except market-data
replay at 1,000 sequences per round.

| In-process operation | Mean latency | p95 latency | Median throughput |
|---|---:|---:|---:|
| FIX encode | 9.621 µs | 11.374 µs | 109,466 messages/s |
| FIX decode | 19.080 µs | 25.835 µs | 57,736 messages/s |
| Semantic message validation | 46.095 µs | 52.260 µs | 21,931 validations/s |
| Two-transition order lifecycle | 1.753 µs | 1.860 µs | 578,741 lifecycles/s |
| Two-message market-data replay | 76.534 µs | 83.111 µs | 13,098 replays/s |

| TCP order-entry mode | Sample | p50 | p95 | p99 | Mean ± sample SD | Batch throughput |
|---|---:|---:|---:|---:|---:|---:|
| Sequential persistent session | 200 orders | 0.213 ms | 0.471 ms | 0.751 ms | 0.253 ± 0.113 ms | 3,320 orders/s |
| 20-way concurrent sessions | 100 orders | 4.673 ms | 9.014 ms | 10.320 ms | 4.939 ± 2.135 ms | 716 orders/s |

These are **local loopback QA-harness measurements, not production exchange capacity
claims**. The mock exchange's artificial processing delay was disabled. Sequential
orders reuse one logged-on FIX session; the concurrent latency measurement excludes
Logon, while its batch-throughput figure includes connection setup. Host load, Python
version, power policy, and CI virtualization will affect results.

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
| 12 | Unified demo, report, and benchmark statistics | `run_demo.py`, `tests/test_report_generator.py`, `tests/test_benchmark_runner.py` |

## Capabilities at a glance

- Hand-built SOH-delimited FIX 4.4 encoder, decoder, stream framing, and validation
- Deterministic order lifecycle model with property-based transition testing
- Real TCP mock exchange and synchronous/concurrent client
- Latency regression baselines and duration-based memory/latency soak trends
- Statistical micro/TCP benchmarks with percentiles, dispersion, throughput, and 95% CIs
- Decimal financial arithmetic with explicit float-drift demonstrations
- Reconstructable, queryable order audit trail and state-machine cross-checking
- Market-data snapshot/incremental replay with conservative sequence-gap recovery
- Delay, drop, duplicate, and reorder chaos with idempotent bounded retries
- Structured educational surveillance heuristics
- One-command unified HTML evidence report

## Resume bullet suggestion

- Built a **77-test FIX 4.4 trading-infrastructure QA suite** in Python spanning protocol
  conformance, Hypothesis-based lifecycle modeling, Decimal precision regression,
  audit reconstruction, market-data gap detection, chaos/idempotency testing, latency
  baselines, soak testing, surveillance heuristics, and automated HTML evidence.

- Designed a reproducible statistical benchmark harness reporting percentiles,
  dispersion, throughput, and 95% mean confidence intervals; measured **0.751 ms p99
  sequential TCP order-entry latency at 3.3k orders/s** across 200 local loopback orders
  and **~579k modeled order lifecycles/s** across seven 10,000-iteration rounds on the
  documented reference machine.
