# OpenRoad — Free VIN research

A standalone FastAPI app and a single static HTML/JS page. No build step, signup, analytics, or collection of names, email addresses, or phone numbers. Nothing here connects to the publishing application or its VPS.

## Live demo

[Open OpenRoad](https://vin-report-ai--nel5uzlt.preview.devinapps.com) — the Devin preview requires **Devin organization sign-in** to access the hosting layer. The application itself has no signup or personal-information form.

## Architecture

The static browser UI calls FastAPI `GET /api/report/{vin}`. A language-keyed **24-hour SQLite cache** short-circuits repeat lookups. On a miss, **NHTSA vPIC** decodes the VIN, then **NHTSA recalls and complaints**, **You.com Search** (`ydc-index.io/v1/search`) and **You.com Research** (`api.you.com/v1/research`) run concurrently. The **Manheim vault sample layer** adds clearly labeled illustrative grades and auction-price context for sample VINs; other VINs use conservative market estimates or explicitly report insufficient data. The UI escapes HTML and renders a supported Markdown subset with clickable source-number citations.

## Run locally

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# Set YOU_API_KEY in the process environment through your shell/secret manager.
# Example variable name: export YOU_API_KEY=... (use your real key privately).
# Never put the key in source control, command output, or a committed file.
uvicorn app:app --port 8080 --no-access-log
```

Open http://127.0.0.1:8080. With no You.com key, public NHTSA data still works and the research sections explicitly show unavailable. Print / Save PDF uses the browser print dialog. The language toggle persists locally and requests English or Simplified Chinese research.

```sh
curl http://127.0.0.1:8080/api/report/1HGCM82633A004352
curl 'http://127.0.0.1:8080/api/report/1N4AL3AP8JC123456?lang=zh'
python -m pytest -q
```

## Data sources and limits

- [NHTSA vPIC](https://vpic.nhtsa.dot.gov/api/) `DecodeVinValues/{vin}?format=json`: manufacturer-submitted vehicle identity. VIN format validation excludes I/O/Q; this is not a check-digit certification. Decoder warnings are displayed.
- [NHTSA recalls](https://api.nhtsa.gov/recalls/recallsByVehicle?make=Honda&model=Accord&modelYear=2003) and [complaints](https://api.nhtsa.gov/complaints/complaintsByVehicle?make=Honda&model=Accord&modelYear=2003): **model/year**, not VIN-specific repair status or verified failure rates. Includes component counts and recent incident summaries. Safety star ratings are not included.
- [You.com Search](https://you.com/docs/api-reference/search/v1-search): GET `https://ydc-index.io/v1/search`, `query` and `count`, authenticated with `X-API-Key`. Search results include source links and snippets.
- [You.com Research](https://you.com/docs/api-reference/research/v1-research): POST `https://api.you.com/v1/research`, body `{"input":"…","research_effort":"lite"}`, same header. Uses `output.content` and `output.sources`. No Agents API. Sources are clickable under the plain-text briefing. Research calls may incur API costs.
- `data/manheim_vault.json`: six **synthetic demonstration** records modeled after `VehicleCandidate` from the separate used-car repository. Real-decodable VIN identities; mileage, grades, auction locations, condition notes and all prices are **illustrative, not verified Manheim data or actual inventory**. Car illustrations are explicitly labeled placeholders, not auction photos. Never use these samples as real valuations. Editorial hero photo from Unsplash is not a vehicle-specific image.
- Vault retail reference = illustrative MMR average + $4,000, mirroring the collector's `MMR_AVG_PRICE_MARKUP`. Non-vault estimates require at least three distinct listing URLs matching the decoded year/make/model. Finance, insurance and repair contexts and prices outside $4,500–$100,000 are excluded; a conservative lower threshold ensures the $4,000 subtraction never produces zero. Each URL contributes a median, then IQR and median-relative filters remove outliers. The central 50% of retained asking-price signals forms the retail range; wholesale subtracts $4,000. Too few signals means **insufficient data**, not a made-up value. These are unverified snippets, not official MMR or sold comparables; trim/mileage may differ. `valuation()` is the future authorized dealer provider seam; no live Manheim integration is present.

The verdict is a transparent heuristic: model-level recalls, complaint volume, sample condition grade and the illustrative retail markup inform Buy / Inspect first / Negotiate. Risk = min(100, recalls × 5 + min(complaints // 10, 45) + 20 if sample grade < 3). Missing safety data means no score and Inspect first. Buy is conditional on inspection, not a recommendation or guarantee. No accident, title, owner, odometer, service-history or theft checks are provided.

After decoding, independent lookups run concurrently with 22-second HTTP timeouts (research: 65 seconds). Failed sections carry error notes without failing the report. Reports are cached by VIN **and language** for 24 hours in gitignored `cache.sqlite`; set `VIN_CACHE_PATH` for another local location. Cache includes partial reports until expiry; delete the cache while the server is stopped to refresh early. No PII is requested. VIN/model queries are sent to the named data providers. Disable access logs as shown to avoid recording VIN URLs. Fonts and the editorial hero photo load from third parties; no service worker.

## Development

`python -m pytest -q` runs offline mocked-provider tests, including valid/invalid format, empty results, failures, pricing provenance, language routing, cache expiry and report aggregation. For optional browser checks, install Playwright separately (`pip install playwright && python -m playwright install chromium`). No credentials belong in tests, files or git.
