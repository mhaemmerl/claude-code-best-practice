#!/usr/bin/env python3
"""Regression tests for the Kleinanzeigen bike-computer scraper.

Plain asserts, no test-runner dependency:  python3 tests/test_scraper.py

The fixture encodes the ad shapes that actually cause trouble on Kleinanzeigen:
mounts and screen protectors that name the model, "Suche" (wanted) ads, broken
units, and over-budget listings.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bike_scraper import (  # noqa: E402
    classify, detect_model, parse_listings, parse_price, score, search_url, slugify,
)

FIXTURE = Path(__file__).parent / "fixtures" / "kleinanzeigen-search.html"
MAX_PRICE, MIN_PRICE = 180, 40

passed = failed = 0


def check(label: str, actual, expected) -> None:
    global passed, failed
    if actual == expected:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}\n         expected: {expected!r}\n         actual:   {actual!r}")


print("parse_price")
check("plain", parse_price("139 €"), (139, False))
check("negotiable", parse_price("139 € VB"), (139, True))
check("thousands", parse_price("1.030 €"), (1030, False))
check("free", parse_price("Zu verschenken"), (0, False))
check("empty", parse_price(""), (None, False))

print("\ndetect_model")
check("plain 530", detect_model("garmin edge 530 gebraucht"), ("530", 3))
check("plus beats base", detect_model("edge 1030 plus ovp"), ("1030 plus", 8))
check("highest of several", detect_model("passt für edge 530 830 1030"), ("1030", 7))
check("too old", detect_model("garmin edge 25 gps"), (None, 0))

print("\nsearch_url")
check("umlauts", slugify("Fahrradcomputer für Straße"), "fahrradcomputer-fuer-strasse")
check("price filter",
      search_url("garmin edge 530", max_price=180),
      "https://www.kleinanzeigen.de/s-preis::180/garmin-edge-530/k0c217")
check("paging",
      search_url("garmin edge 830", max_price=180, page=2),
      "https://www.kleinanzeigen.de/s-seite:2/preis::180/garmin-edge-830/k0c217")
check("all categories",
      search_url("garmin edge 530", category=""),
      "https://www.kleinanzeigen.de/s/garmin-edge-530/k0")

print("\nparsing the fixture")
listings = parse_listings(FIXTURE.read_text(encoding="utf-8"))
check("all ads found", len(listings), 10)
check("title", listings[0].title,
      "Garmin Edge 530 GPS Fahrradcomputer sehr guter Zustand")
check("price", (listings[0].price, listings[0].negotiable), (139, True))
check("location", listings[0].location, "80331 München")
check("absolute url", listings[0].url.startswith("https://www.kleinanzeigen.de/s-anzeige/"), True)
check("ad id", listings[0].ad_id, "2801001")

print("\nclassification (the part that matters)")
verdicts = {}
for ad in listings:
    keep, reason = classify(ad, MAX_PRICE, MIN_PRICE, exclude_pickup_only=False)
    verdicts[ad.ad_id] = "keep" if keep else reason

check("genuine 530 kept", verdicts["2801001"], "keep")
check("mount rejected", verdicts["2801002"], "accessory only (mount/case/cable)")
check("wanted ad rejected", verdicts["2801003"], "wanted ad (Suche), not an offer")
check("defective rejected", verdicts["2801004"], "defective / for parts")
check("1030 Plus kept", verdicts["2801005"], "keep")
check("over budget rejected", verdicts["2801006"], "over budget (320 > 180 EUR)")
check("screen protector rejected", verdicts["2801007"], "accessory only (mount/case/cable)")
check("830 kept", verdicts["2801008"], "keep")
check("Edge 25 rejected", verdicts["2801009"], "no Edge 530-or-better model identified")
check("pickup-only kept by default", verdicts["2801010"], "keep")

kept = [a for a in listings if verdicts[a.ad_id] == "keep"]
check("survivor count", len(kept), 4)

_, hard = classify(listings[9], MAX_PRICE, MIN_PRICE, exclude_pickup_only=True)
check("--no-pickup excludes it", hard, "pickup only")

print("\nscoring")
for ad in kept:
    score(ad, MAX_PRICE)
ranked = sorted(kept, key=lambda a: a.score, reverse=True)
check("1030 Plus ranks first", ranked[0].model, "1030 plus")
check("pickup-only ranks last", ranked[-1].ad_id, "2801010")
check("pickup penalty noted", "pickup only — no shipping" in ranked[-1].reasons, True)
check("sensors noted", "heart-rate strap included" in
      [r for a in kept if a.ad_id == "2801001" for r in a.reasons], True)
check("all scores positive", all(a.score > 0 for a in kept), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
