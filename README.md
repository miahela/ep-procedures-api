# EP API Test Script

A Python script for exploring the [European Parliament Open Data API (v2)](https://data.europarl.europa.eu/en/developer-corner).

## What it does

1. **Fetches procedures** from the EP Atom feed (DEA and COD types), going back up to 3 months if today's feed is empty
2. **Resolves MEP details** (name, nationality, political group) with a local JSON cache to avoid redundant requests
3. **Sweeps committee meetings** and their foreseen/scheduled activities over a configurable horizon, cross-referencing them to the fetched procedures
4. **Saves structured output** (CSV + JSON) and prints a pass/fail test report

## Requirements

- Python 3.12+
- `requests`

```bash
pip install -r requirements.txt
```

## Usage

```bash
python ep_api_test.py
```

Output files are written to `ep_api_test_v2_output/`.

## Configuration

All tuning parameters are at the top of the script:

| Variable | Default | Description |
|---|---|---|
| `MAX_PROCEDURES` | 50 | Max procedures to process per run |
| `LOOKBACK_MONTHS` | 3 | How far back to search if today's feed is empty |
| `MEETINGS_HORIZON` | 50 | Days ahead to look for scheduled meetings |
| `REQ_DELAY` | 0.5s | Delay between requests (EP rate limit: 500 req / 5 min) |

## Output files

| File | Description |
|---|---|
| `procedures_meta.json/csv` | One row per procedure with type, title, stage, counts |
| `participations_resolved.csv` | All participations with resolved MEP names |
| `events.csv` | All activity events extracted from procedures |
| `scheduled_from_procedure.csv` | Foreseen activities embedded in procedure objects |
| `meetings.csv` | Committee meetings in the upcoming horizon |
| `foreseen_activities.csv` | All foreseen agenda items per meeting |
| `foreseen_cross_refs.csv` | Foreseen items that reference a tracked procedure |
| `mep_cache.json` | Cached MEP details (reused across runs) |
| `activity_type_inventory.json` | Count of each activity type seen |
| `test_report.json/csv` | Pass/fail check results |
