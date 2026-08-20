# fix-gateway-qa

`fix-gateway-qa` is a QA/testing portfolio project modeled on the workflows used to
test electronic-trading infrastructure. Its first phase implements FIX 4.4 message
encoding, decoding, wire-integrity checks, and order-message conformance validation
without relying on a third-party FIX engine.

## Current capabilities

- Hand-written, SOH-delimited FIX 4.4 encoder and decoder
- Automatic `BodyLength(9)` and `CheckSum(10)` generation
- Required-field and enum validation for common order-flow messages
- Non-negative quantity/price checks
- Execution Report accounting invariant: `CumQty + LeavesQty == OrderQty`
- Unit coverage for valid, malformed, and corrupted wire messages

## Run the tests

Python 3.11 or newer is required.

```bash
python -m pip install -r requirements.txt
python -m pytest
```

## Coming up

Later phases will add a mock exchange and FIX client for order-lifecycle testing,
latency and regression measurements, abusive-order-pattern surveillance, and richer
QA reporting. Hypothesis is included now so those phases can add property-based and
stateful lifecycle tests.

