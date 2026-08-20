# fix-gateway-qa

## Project Overview

`fix-gateway-qa` is an end-to-end QA portfolio project for an electronic order
gateway. It demonstrates hand-built FIX 4.4 protocol conformance testing, model-based
order lifecycle testing through a formal state machine, and realistic TCP integration
against a controllable mock exchange. The exchange also has an opt-in chaos mode that
injects delayed or dropped Execution Reports so client timeout behavior can be tested.

The performance layer records round-trip request latency and guards p99 against a saved
regression baseline. A deliberately simplified rule-based detector produces structured
`spoofing-like` and `layering-like` educational signals. These are QA heuristics—not a
real surveillance or compliance system. Jinja2 combines the suite summary, latency
percentiles, and heuristic matches into a self-contained HTML report.

The numerical regression layer contrasts exact Decimal-based tick rounding, notional,
weighted-average fill price, PnL, and currency calculations with deliberately naive
float implementations. Hypothesis fuzzing protects against fixed-point drift across
small ticks, large prices, and large order quantities.

The audit layer exposes a normalized inbound/outbound message trail, checks timestamp
and lifecycle completeness, reconstructs order histories without live process state,
and cross-checks those histories against the formal state machine. It is a simplified
educational analog of regulatory completeness controls, not a compliance system.

Run the complete demonstration:

```bash
python run_demo.py
```

Run the full test suite directly:

```bash
pytest
```

The committed [sample HTML report](reports/report.html) shows the report layout: status
cards for test counts, proportional latency bars for p50 through max, and a table of
structured surveillance-pattern details. Open it in any browser; it has no external
asset dependencies.

`fix-gateway-qa` is a QA/testing portfolio project modeled on the workflows used to
test electronic-trading infrastructure. It implements FIX 4.4 message encoding,
decoding, wire-integrity checks, order-message conformance validation, and a tested
order lifecycle model without relying on a third-party FIX engine.

## Current capabilities

- Hand-written, SOH-delimited FIX 4.4 encoder and decoder
- Automatic `BodyLength(9)` and `CheckSum(10)` generation
- Required-field and enum validation for common order-flow messages
- Non-negative quantity/price checks
- Execution Report accounting invariant: `CumQty + LeavesQty == OrderQty`
- Deterministic order state machine with transition history and closed terminal states
- In-memory order-book stub supporting resting, partial-fill, full-fill, and cancel flows
- Hypothesis properties that exercise arbitrary valid and invalid event sequences
- Real TCP mock exchange with FIX Logon, lifecycle responses, and wire-message logging
- Configurable chaos injection for delayed or dropped Execution Reports
- Synchronous and concurrent test client with per-request round-trip latency recording
- p50/p99/p99.9/max latency reporting with a persisted regression baseline
- Structured, rule-based spoofing-like and layering-like test heuristics
- Self-contained Jinja2 HTML report generation and a one-command end-to-end demo
- Decimal-based price, notional, weighted-average, PnL, and currency arithmetic
- Property-based precision and rounding regression coverage with naive float contrasts
- Audit-log completeness, ordering, lifecycle reconstruction, and state cross-checks
- Unit coverage for valid, malformed, and corrupted wire messages

## Run the tests

Python 3.11 or newer is required.

```bash
python -m pip install -r requirements.txt
python -m pytest
```

Run the latency check by itself:

```bash
python -m fixgateway.reporting.run_latency_check --orders 200
```

Set `UPDATE_BASELINE=1` to intentionally replace `reports/latency_baseline.json`.
`LATENCY_ORDER_COUNT` and `LATENCY_REGRESSION_THRESHOLD` configure the pytest latency
run; the default regression budget is 20 percent above the saved p99.

## Surveillance disclaimer

The pattern detector is a deliberately simplified educational heuristic for QA and
portfolio demonstrations. It is not a real trade-surveillance or compliance system,
and its pattern labels are not compliance determinations.

## Status

Phases 1–8 are implemented. Phase 9 can build on this foundation; the project can also
be extended with higher-volume load generation, session recovery, sequence-gap
handling, and deeper fault injection.
