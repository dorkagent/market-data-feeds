# market-data-feeds

Scheduled market-data feed scrapers powering a paper-trading research stack.

## What this is

A collection of small Python scrapers that pull public market data on a schedule
and commit the results as JSON. Each feed runs as its own GitHub Actions
workflow, on a cron schedule, with manual `workflow_dispatch` for reruns.

## Data sources

- SEC EDGAR (filings search, Form 4 insider transactions, fails-to-deliver, 13F)
- FINRA (short sale volume, short interest)
- CBOE (options gamma exposure, put/call ratios)
- U.S. Treasury Fiscal Data (auction results)
- Congressional trading disclosures
- Polymarket (event market probabilities)
- GDELT (news tone)
- OpenInsider (insider trading)
- Reddit r/wallstreetbets (retail sentiment)
- CoinGecko (crypto prices, keyless)

## How it runs

Each workflow in `.github/workflows/` runs its script from `scripts/`. The script
fetches the data and writes JSON under `hidden_files/<feed>/`, which the workflow
commits back to the repo. Downstream consumers pull the JSON files from the repo.
Workflows that run at the same time share a concurrency group so their pushes do
not race.

## Notes

- Feeds that require API keys live in a separate private repo and are not
  published here. Everything in this repo uses public, keyless data sources.
- Outputs are data snapshots for research. They are not trading signals.
