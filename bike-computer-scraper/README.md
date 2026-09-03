# Bike Computer Scraper

Finds used **Garmin Edge 530 or better** bike computers on
[Kleinanzeigen.de](https://www.kleinanzeigen.de) under a price ceiling
(default **€180**), filters out the noise, and ranks what is left by
value for money.

## Why it is not just a search URL

A raw Kleinanzeigen search for `garmin edge 530` is mostly *not* bike computers.
It returns €8 handlebar mounts, €5 screen protectors, silicone cases, charging
cables, "Suche" (wanted) ads from other buyers, and units sold `defekt / für
Bastler`. This tool exists to throw those away and rank the real offers.

| Rejected as | Example |
|---|---|
| accessory only | *Lenkerhalterung für Garmin Edge 530 830 1030* — €8 |
| wanted ad | *Suche Garmin Edge 530 oder 830* |
| defective | *Garmin Edge 830 defekt Bastler* — €45 |
| over budget | *Garmin Edge 1040 Solar* — €320 |
| too old a model | *Garmin Edge 25* |
| implausibly cheap | anything under `--min-price` (accessory or scam) |

Pickup-only ads are **kept but penalised**, not dropped — some of the best-value
listings are local collection only. Use `--no-pickup` to exclude them outright.

## Ranking

Score is driven by capability-per-euro, then adjusted:

- **Model tier** — 1050 > 1040 > 1030 Plus > 1030 / 850 > 840 > 830 / 550 > 540 > 530
- **Headroom under budget** — cheaper is better, proportionally
- **Bonuses** — like-new condition, OVP, warranty/receipt, bundled HR strap or
  speed/cadence sensor, ships rather than pickup-only, price negotiable (VB)
- **Penalty** — pickup only

Every result prints the reasons behind its score, so you can sanity-check the
ranking rather than trust it blindly.

## Install

```bash
pip install -r requirements.txt
playwright install chromium
```

## Use

```bash
# Default: Edge 530/830/1030/540/840, up to €180, 2 pages each
python bike_scraper.py

# Tighter budget, more results, all categories
python bike_scraper.py --max-price 140 --top 20 --category ""

# Only ads that ship
python bike_scraper.py --no-pickup

# Just one model, deeper
python bike_scraper.py --queries "garmin edge 830" --pages 5
```

Writes `results.json` and a readable `results.html` digest alongside the console
table.

### When bot protection wins

Kleinanzeigen serves a bot check to automation. Two escape hatches:

```bash
# 1. Watch the browser and clear the check by hand
python bike_scraper.py --no-headless

# 2. Skip automation entirely: save the search page from your own browser
#    (Ctrl+S, "Webpage, HTML only") into saved/, then parse it
python bike_scraper.py --from-file 'saved/*.html'
```

`--from-file` runs the identical parser and ranking. It is the reliable path if
you only want to do this occasionally.

If Chromium refuses to launch, point at one you already have:

```bash
CHROMIUM_PATH=/usr/bin/chromium python bike_scraper.py
```

### Optional email digest

Off by default. Credentials are read from the environment only — never stored
in this repo:

```bash
export SMTP_USER="you@gmail.com"
export SMTP_PASS="your-16-char-app-password"   # Gmail App Password, not your login
python bike_scraper.py --email --to you@gmail.com
```

Run it on a schedule with cron for a daily digest:

```cron
0 8 * * *  cd /path/to/bike-computer-scraper && /usr/bin/python3 bike_scraper.py --email --to you@gmail.com
```

## Tests

```bash
python3 tests/test_scraper.py     # 36 assertions, no test runner needed
```

The fixture in `tests/fixtures/` encodes the ad shapes that actually cause
trouble, so filter and scoring regressions get caught immediately.

## Known limitations

- **The CSS selectors have not been checked against the live site.** They were
  written against Kleinanzeigen's documented `article.aditem` markup and are
  verified against the fixture, but the site renames its classes periodically.
  If a run returns 0 results or empty titles, dump a page and inspect it:
  ```bash
  python bike_scraper.py --dump-html dump/ --pages 1
  ```
  Then update the selector lists in `parse_listings()`. Each field already tries
  several selectors in order, so usually only one entry needs adding.
- **Search coverage is keyword-based.** An ad titled *"Fahrradcomputer Garmin,
  neuwertig"* without a model number will be missed. Widen `--queries` if you
  want more recall at the cost of more noise.
- Kleinanzeigen's Terms of Service restrict automated access. This tool is
  written for occasional personal use — sequential requests, a delay between
  them, no parallelism. Keep it that way, or use `--from-file`.
