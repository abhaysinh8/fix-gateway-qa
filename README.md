# fix-gateway-qa

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
- Synchronous and concurrent test client with per-request round-trip latency recording
- p50/p99/p99.9/max latency reporting with a persisted regression baseline
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

## Coming up

The next phase will add abusive-order-pattern surveillance. Later work will expand the
QA reports and exercise more demanding load and fault-injection scenarios.
