# Historical financial statement set ingestion

For DCF computation there is the necessity to project the cash flows of the company for the next 5 years (v1). For this reason, it is necessary to have 10 years of history to reconstruct a tendency exponential CAGR-fade method (v1).

## Goal

Modify the ingest phase to allow automatic ingestion of the whole historical set. For v1 we set a static 10y history with hard reject when a company was listed less than 10y ago.

## Attention

It would be useful to preserve the possibility to ingest a single statement for a single fiscal year for testing-debugging purposes.
