#!/usr/bin/env python3
"""Find used Garmin Edge bike computers (530 or better) on Kleinanzeigen.de.

Fetching and parsing are deliberately decoupled:

  * the default path drives a real Chromium via Playwright (Kleinanzeigen
    blocks plain HTTP clients), then parses the resulting HTML.
  * ``--from-file`` parses HTML you saved from your own browser. Same parser,
    no automation involved. This is the escape hatch when bot protection wins,
    and it is what makes the parser testable offline.

Usage:
    python bike_scraper.py                       # live search, defaults below
    python bike_scraper.py --max-price 180
    python bike_scraper.py --from-file saved/*.html
    python bike_scraper.py --email               # also mail the digest (see README)
"""

from __future__ import annotations

import argparse
import dataclasses
import glob
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

BASE = "https://www.kleinanzeigen.de"

# Every bike computer at or above Garmin Edge 530 capability, mapped onto one
# shared tier scale so brands can be compared directly. Tier is desirability,
# not price. Anything below 530 class (Edge 130/25, Bryton 420, Sigma ROX 4,
# base Lezyne) is deliberately absent: it fails the "530 or better" bar.
# Key = regex-ish match phrase, value = (tier, display name).
MODEL_TIERS: dict[str, tuple[int, str]] = {
    # Garmin Edge
    "1050": (10, "Garmin Edge 1050"),
    "1040": (9, "Garmin Edge 1040"),
    "1030 plus": (8, "Garmin Edge 1030 Plus"),
    "1030": (7, "Garmin Edge 1030"),
    "850": (7, "Garmin Edge 850"),
    "840": (6, "Garmin Edge 840"),
    "830": (5, "Garmin Edge 830"),
    "550": (5, "Garmin Edge 550"),
    "540": (4, "Garmin Edge 540"),
    "530": (3, "Garmin Edge 530"),
    "explore 2": (4, "Garmin Edge Explore 2"),
    # Wahoo - ROAM is the 830/1030-class device, BOLT the 530-class one.
    "elemnt roam": (6, "Wahoo ELEMNT ROAM"),
    "elemnt bolt": (4, "Wahoo ELEMNT BOLT"),
    "elemnt": (4, "Wahoo ELEMNT"),
    # Hammerhead
    "karoo 3": (9, "Hammerhead Karoo 3"),
    "karoo 2": (7, "Hammerhead Karoo 2"),
    "karoo": (6, "Hammerhead Karoo"),
    # Sigma - ROX 12 is the mapping flagship; 11.1 sits at 530 level.
    "rox 12": (5, "Sigma ROX 12"),
    "rox 11.1": (3, "Sigma ROX 11.1"),
    "rox 11": (3, "Sigma ROX 11"),
    # Bryton - only the mapping models clear the bar.
    "rider 750": (5, "Bryton Rider 750"),
    "rider 860": (6, "Bryton Rider 860"),
    # Lezyne
    "mega xl": (3, "Lezyne Mega XL"),
}

# Ads that match the search words but are not a bike computer. This is the
# single most valuable part of the tool: a raw "Garmin Edge 530" search on
# Kleinanzeigen is mostly mounts, cases and screen protectors.
ACCESSORY_PATTERNS = [
    r"\bhalterung\b", r"\bhalter\b", r"\bmount\b", r"\blenkerhalterung\b",
    r"\bschutzfolie\b", r"\bdisplayschutz\b", r"\bpanzerglas\b",
    r"\bh(ü|ue)lle\b", r"\bcase\b", r"\bsilikon\b", r"\btasche\b",
    r"\bakku\b", r"\bbatterie\b", r"\bkabel\b", r"\bladekabel\b",
    r"\bersatzteil\b", r"\bzubeh(ö|oe)r\s+nur\b", r"\bnur\s+halterung\b",
]

# Ads for broken units, or people *looking to buy* rather than sell.
DEFECT_PATTERNS = [r"\bdefekt\b", r"\bbastler\b", r"\bersatzteiltr(ä|ae)ger\b",
                   r"\bnicht\s+funktionsf(ä|ae)hig\b", r"\bkaputt\b"]
WANTED_PATTERNS = [r"^\s*suche\b", r"\bsuche\s+garmin\b", r"\bkaufgesuch\b",
                   r"\bbiete\s+nicht\b", r"\btausche\b"]

# Signals that an ad is worth more than its bare price suggests.
BONUS_PATTERNS = {
    r"\b(wie\s+neu|neuwertig|sehr\s+gut(er)?\s+zustand)\b": 6,
    r"\bovp\b|\boriginalverpackung\b|\bkarton\b": 3,
    r"\bgarantie\b|\brechnung\b": 4,
    r"\bbundle\b|\bset\b|\bmit\s+zubeh(ö|oe)r\b": 3,
    r"\bherzfrequenz|\bhf-?gurt\b|\bbrustgurt\b": 3,
    r"\btrittfrequenz\b|\bcadence\b|\bspeed\s*sensor\b|\bgeschwindigkeitssensor\b": 3,
    r"\bversand\b|\bverschicke\b|\bporto\b": 2,
}


@dataclass
class Listing:
    """One classified ad, after parsing and scoring."""

    ad_id: str
    title: str
    url: str
    price: int | None          # EUR, None when the ad states no price
    negotiable: bool           # "VB" / Verhandlungsbasis
    location: str
    posted: str
    description: str
    shipping: str
    model: str | None = None
    tier: int = 0
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def haystack(self) -> str:
        return f"{self.title}\n{self.description}\n{self.shipping}".lower()


# --------------------------------------------------------------------------- #
# URL building
# --------------------------------------------------------------------------- #

def slugify(query: str) -> str:
    slug = query.lower().strip()
    slug = (slug.replace("ä", "ae").replace("ö", "oe")
                .replace("ü", "ue").replace("ß", "ss"))
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return slug or "garmin-edge"


def search_url(query: str, max_price: int | None = None, min_price: int | None = None,
               page: int = 1, category: str = "c217") -> str:
    """Build a Kleinanzeigen search URL.

    Kleinanzeigen stacks filter modifiers after the leading ``s-``:
        /s-seite:2/preis:20:180/garmin-edge-530/k0c217

    ``category`` defaults to c217 (Fahrrad-Zubehör), which already removes a lot
    of unrelated noise. Pass ``category=""`` to search every category.
    """
    mods: list[str] = []
    if page > 1:
        mods.append(f"seite:{page}")
    if max_price is not None or min_price is not None:
        mods.append(f"preis:{min_price or ''}:{max_price or ''}")
    # The first modifier hyphenates onto the "s"; the rest are slash-separated:
    #   /s-seite:2/preis::180/garmin-edge-530/k0c217
    prefix = "s-" + "/".join(mods) if mods else "s"
    return f"{BASE}/{prefix}/{slugify(query)}/k0{category}"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

PRICE_RE = re.compile(r"(\d[\d.\s]*)\s*€")


def parse_price(raw: str) -> tuple[int | None, bool]:
    """'139 € VB' -> (139, True). 'Zu verschenken' -> (0, False)."""
    text = (raw or "").strip()
    if not text:
        return None, False
    low = text.lower()
    if "verschenken" in low:
        return 0, False
    negotiable = "vb" in low or "verhandel" in low
    m = PRICE_RE.search(text)
    if not m:
        return None, negotiable
    digits = re.sub(r"[^\d]", "", m.group(1))
    return (int(digits) if digits else None), negotiable


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)) if node else ""


def _first(node, selectors: Sequence[str]) -> str:
    """Try several selectors and return the first non-empty match.

    Kleinanzeigen renames its BEM-ish classes now and then; a fallback chain
    keeps the parser alive across those changes instead of silently yielding
    empty fields.
    """
    for sel in selectors:
        found = node.select_one(sel)
        if found and _text(found):
            return _text(found)
    return ""


def parse_listings(markup: str) -> list[Listing]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "lxml")
    cards = soup.select("article.aditem[data-adid]") or soup.select("article[data-adid]")
    listings: list[Listing] = []

    for card in cards:
        ad_id = card.get("data-adid", "")
        href = card.get("data-href") or ""
        if not href:
            link = card.select_one("a[href*='/s-anzeige/']")
            href = link.get("href", "") if link else ""
        url = href if href.startswith("http") else f"{BASE}{href}"

        title = _first(card, [
            "h2 a", ".text-module-begin a", ".ellipsis", "h2",
        ])
        price_raw = _first(card, [
            ".aditem-main--middle--price-shipping--price",
            ".aditem-main--middle--price", "[class*='price']",
        ])
        shipping = _first(card, [
            ".aditem-main--middle--price-shipping--shipping", "[class*='shipping']",
        ])
        location = _first(card, [".aditem-main--top--left", "[class*='top--left']"])
        posted = _first(card, [".aditem-main--top--right", "[class*='top--right']"])
        description = _first(card, [
            ".aditem-main--middle--description", "[class*='description']", "p",
        ])

        price, negotiable = parse_price(price_raw)
        if not title:
            continue
        listings.append(Listing(
            ad_id=ad_id, title=title, url=url, price=price, negotiable=negotiable,
            location=location, posted=posted, description=description,
            shipping=shipping,
        ))
    return listings


# --------------------------------------------------------------------------- #
# Filtering and scoring
# --------------------------------------------------------------------------- #

def detect_model(text: str) -> tuple[str | None, int]:
    """Return the highest-tier Edge model mentioned. '1030 Plus' beats '1030'."""
    low = re.sub(r"\s+", " ", text.lower())
    best: tuple[str | None, int] = (None, 0)
    for phrase, (tier, display) in MODEL_TIERS.items():
        # "1030 plus" must be allowed to beat "1030", so keep scanning and take
        # the highest tier rather than returning on first match.
        pattern = r"\b" + re.escape(phrase).replace(r"\ ", r"\s*") + r"\b"
        if re.search(pattern, low) and tier > best[1]:
            best = (display, tier)
    return best


def any_match(patterns: Iterable[str], text: str) -> bool:
    return any(re.search(p, text) for p in patterns)


def classify(listing: Listing, max_price: int, min_price: int,
             exclude_pickup_only: bool) -> tuple[bool, str]:
    """Decide whether an ad survives. Returns (keep, reason_if_dropped)."""
    hay = listing.haystack

    if any_match(WANTED_PATTERNS, hay):
        return False, "wanted ad (Suche), not an offer"
    if any_match(DEFECT_PATTERNS, hay):
        return False, "defective / for parts"

    model, tier = detect_model(hay)
    if model is None:
        return False, "no model at Edge 530 level or above"
    listing.model, listing.tier = model, tier

    # Accessory ads usually name the model too ("Halterung für Edge 530"), so
    # the model check alone is not enough.
    if any_match(ACCESSORY_PATTERNS, hay):
        return False, "accessory only (mount/case/cable)"

    if listing.price is None:
        return False, "no price stated"
    if listing.price > max_price:
        return False, f"over budget ({listing.price} > {max_price} EUR)"
    if listing.price < min_price:
        # A €15 "Edge 830" is an accessory or a scam, not a bargain.
        return False, f"implausibly cheap ({listing.price} < {min_price} EUR)"

    # Pickup-only is a soft signal, not a disqualifier: some of the best-value
    # ads are local-collection only. It costs points in score(), not survival.
    if exclude_pickup_only and is_pickup_only(listing):
        return False, "pickup only"

    return True, ""


def is_pickup_only(listing: Listing) -> bool:
    return "nur abholung" in listing.haystack


def score(listing: Listing, max_price: int) -> None:
    """Value-for-money score. Deliberately readable rather than clever."""
    reasons: list[str] = []

    # Capability per euro is the backbone of the ranking.
    price = max(listing.price or max_price, 1)
    value = (listing.tier * 100.0) / price
    total = value * 10.0
    reasons.append(f"{listing.model} (tier {listing.tier}) at {price} EUR")

    # Headroom under budget is worth something on its own.
    headroom = (max_price - price) / max_price
    total += headroom * 8.0
    if headroom > 0.3:
        reasons.append(f"{int(headroom * 100)}% under budget")

    for pattern, points in BONUS_PATTERNS.items():
        if re.search(pattern, listing.haystack):
            total += points
            reasons.append(_bonus_label(pattern))

    if listing.negotiable:
        total += 2
        reasons.append("price negotiable (VB)")

    if is_pickup_only(listing):
        total -= 6
        reasons.append("pickup only \u2014 no shipping")

    listing.score = round(total, 1)
    listing.reasons = reasons


def _bonus_label(pattern: str) -> str:
    labels = {
        "zustand": "described as like-new",
        "ovp": "original packaging",
        "garantie": "warranty or receipt",
        "bundle": "bundle with extras",
        "herzfrequenz": "heart-rate strap included",
        "trittfrequenz": "speed/cadence sensor included",
        "versand": "ships (no pickup needed)",
    }
    for key, label in labels.items():
        if key in pattern:
            return label
    return "bonus"


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def fetch_pages(queries: Sequence[str], max_price: int, pages: int,
                category: str, headless: bool, delay: float,
                dump_dir: Path | None) -> list[str]:
    """Drive a real browser over the search result pages.

    Kleinanzeigen serves a bot-check page to plain HTTP clients, so a real
    browser engine is not optional here. Requests stay slow and sequential on
    purpose: this is a personal search, not a crawl.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright is not installed. Run: pip install -r requirements.txt "
                 "&& playwright install chromium")

    pages_html: list[str] = []
    with sync_playwright() as p:
        # CHROMIUM_PATH lets you point at a Chromium you already have instead of
        # letting Playwright manage its own download - useful when the bundled
        # browser revision does not match the installed Playwright version.
        launch_kwargs: dict = {"headless": headless}
        if os.environ.get("CHROMIUM_PATH"):
            launch_kwargs["executable_path"] = os.environ["CHROMIUM_PATH"]
        try:
            browser = p.chromium.launch(**launch_kwargs)
        except Exception as exc:  # noqa: BLE001 - turn a wall of stack into advice
            sys.exit(
                f"Could not start Chromium: {exc}\n\n"
                "Fix it with one of:\n"
                "  1. playwright install chromium\n"
                "  2. CHROMIUM_PATH=/path/to/chromium python bike_scraper.py ...\n"
                "  3. Skip the browser entirely: save the search page from your own\n"
                "     browser and run  python bike_scraper.py --from-file saved/*.html"
            )
        context = browser.new_context(
            locale="de-DE",
            timezone_id="Europe/Berlin",
            viewport={"width": 1366, "height": 900},
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
        )
        page = context.new_page()
        for query in queries:
            for n in range(1, pages + 1):
                url = search_url(query, max_price=max_price, page=n, category=category)
                print(f"  fetching {url}", file=sys.stderr)
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    page.wait_for_selector("article[data-adid]", timeout=10_000)
                except Exception as exc:  # noqa: BLE001 - report and continue
                    detail = str(exc).splitlines()[0][:140]
                    print(f"  ! {type(exc).__name__}: {detail}", file=sys.stderr)
                    if "ERR_" in detail or "NS_ERROR" in detail or "Timeout" in detail:
                        print("    (network blocked or unreachable - if you are behind a\n"
                              "     restrictive proxy, use --from-file with saved pages)",
                              file=sys.stderr)
                # A failed navigation leaves the page unusable, so reading its
                # content throws too. Skip this page rather than lose the run.
                try:
                    markup = page.content()
                except Exception:  # noqa: BLE001
                    continue
                if _looks_like_botwall(markup):
                    print("  ! bot check hit. Re-run with --no-headless, or save the "
                          "page from your own browser and use --from-file.",
                          file=sys.stderr)
                if dump_dir:
                    dump_dir.mkdir(parents=True, exist_ok=True)
                    (dump_dir / f"{slugify(query)}-p{n}.html").write_text(
                        markup, encoding="utf-8")
                pages_html.append(markup)
                time.sleep(delay)
        browser.close()
    return pages_html


def _looks_like_botwall(markup: str) -> bool:
    low = markup[:5000].lower()
    return any(s in low for s in ("captcha", "bot-schutz", "access denied",
                                  "unusual traffic", "zugriff verweigert"))


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def render_console(results: list[Listing], dropped: dict[str, int], max_price: int) -> str:
    if not results:
        return ("No matching listings.\n"
                f"Dropped: {json.dumps(dropped, ensure_ascii=False)}")
    lines = [f"Top {len(results)} used bike computers (Edge 530 class or better) under {max_price} EUR", ""]
    for i, r in enumerate(results, 1):
        price = f"{r.price} EUR" + (" VB" if r.negotiable else "")
        lines.append(f"{i:2}. {(r.model or '?'):<22} {price:<12} score {r.score:>6}  {r.location}")
        lines.append(f"    {r.title[:88]}")
        lines.append(f"    {', '.join(r.reasons[:4])}")
        lines.append(f"    {r.url}")
        lines.append("")
    lines.append(f"Filtered out: {json.dumps(dropped, ensure_ascii=False)}")
    return "\n".join(lines)


def render_html(results: list[Listing], max_price: int) -> str:
    rows = []
    for i, r in enumerate(results, 1):
        price = f"{r.price}&nbsp;€" + (" VB" if r.negotiable else "")
        rows.append(f"""
      <tr>
        <td class="rank">{i}</td>
        <td><a href="{html.escape(r.url)}">{html.escape(r.title)}</a>
            <div class="meta">{html.escape(r.location)} &middot; {html.escape(r.posted)}</div>
            <div class="why">{html.escape(', '.join(r.reasons[:4]))}</div></td>
        <td class="model">{html.escape(r.model or '?')}</td>
        <td class="price">{price}</td>
      </tr>""")
    return f"""<!doctype html>
<html lang="de"><meta charset="utf-8">
<title>Garmin Edge unter {max_price} €</title>
<style>
  body {{ font: 15px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 52rem; color: #1a1a1a; }}
  table {{ border-collapse: collapse; width: 100%; }}
  td, th {{ border-bottom: 1px solid #e5e5e5; padding: .7rem .5rem; vertical-align: top; }}
  .rank {{ color: #999; width: 2rem; }}
  .price {{ font-weight: 600; white-space: nowrap; text-align: right; }}
  .model {{ white-space: nowrap; color: #444; }}
  .meta {{ color: #777; font-size: .85em; }}
  .why {{ color: #2b6b3f; font-size: .85em; }}
  a {{ color: #12508c; }}
</style>
<h1>Gebrauchte Garmin Edge (530+) unter {max_price} €</h1>
<p>{len(results)} Treffer, sortiert nach Preis-Leistung.</p>
<table><tbody>{''.join(rows)}</tbody></table>
</html>"""


def send_email(subject: str, body_html: str, to_addr: str) -> None:
    """Optional SMTP digest. Credentials come from the environment only.

    Set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS (a Gmail app password, not
    your account password). Nothing is ever written to disk or the repo.
    """
    import smtplib
    from email.message import EmailMessage

    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    if not user or not password:
        sys.exit("--email needs SMTP_USER and SMTP_PASS in the environment. See README.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.set_content("This digest is HTML. Open it in an HTML-capable client.")
    msg.add_alternative(body_html, subtype="html")

    with smtplib.SMTP(host, port) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(msg)
    print(f"Emailed digest to {to_addr}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

DEFAULT_QUERIES = [
    # Garmin
    "garmin edge 530", "garmin edge 830", "garmin edge 1030",
    "garmin edge 540", "garmin edge 840", "garmin edge explore 2",
    # Wahoo
    "wahoo elemnt bolt", "wahoo elemnt roam",
    # Others at 530 class or above
    "hammerhead karoo", "sigma rox 12", "bryton rider 750",
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-price", type=int, default=180, help="EUR ceiling (default: 180)")
    ap.add_argument("--min-price", type=int, default=40,
                    help="EUR floor; below this an ad is an accessory or a scam (default: 40)")
    ap.add_argument("--queries", nargs="*", default=DEFAULT_QUERIES)
    ap.add_argument("--pages", type=int, default=2, help="result pages per query")
    ap.add_argument("--category", default="c217",
                    help="Kleinanzeigen category suffix; '' searches everything")
    ap.add_argument("--top", type=int, default=10, help="how many results to report")
    ap.add_argument("--from-file", nargs="*", metavar="GLOB",
                    help="parse saved HTML instead of fetching")
    ap.add_argument("--no-headless", dest="headless", action="store_false",
                    help="show the browser (helps when a bot check appears)")
    ap.add_argument("--delay", type=float, default=2.5, help="seconds between requests")
    ap.add_argument("--no-pickup", dest="exclude_pickup_only", action="store_true",
                    help="drop pickup-only ads entirely (default: keep but penalise)")
    ap.add_argument("--dump-html", metavar="DIR", help="save fetched pages for debugging")
    ap.add_argument("--out", default="results", help="output basename (default: results)")
    ap.add_argument("--email", action="store_true", help="also send the digest by SMTP")
    ap.add_argument("--to", default=os.environ.get("DIGEST_TO", ""),
                    help="recipient for --email")
    args = ap.parse_args(argv)

    if args.from_file:
        paths = [Path(p) for pattern in args.from_file for p in sorted(glob.glob(pattern))]
        if not paths:
            sys.exit("--from-file matched no files")
        print(f"Parsing {len(paths)} saved page(s)", file=sys.stderr)
        pages_html = [p.read_text(encoding="utf-8", errors="replace") for p in paths]
    else:
        print(f"Searching Kleinanzeigen for {len(args.queries)} queries "
              f"up to {args.max_price} EUR", file=sys.stderr)
        pages_html = fetch_pages(
            args.queries, args.max_price, args.pages, args.category,
            args.headless, args.delay,
            Path(args.dump_html) if args.dump_html else None,
        )

    seen: dict[str, Listing] = {}
    dropped: dict[str, int] = {}
    for markup in pages_html:
        for listing in parse_listings(markup):
            key = listing.ad_id or listing.url
            if key in seen:
                continue
            keep, reason = classify(listing, args.max_price, args.min_price,
                                    args.exclude_pickup_only)
            if not keep:
                dropped[reason] = dropped.get(reason, 0) + 1
                continue
            score(listing, args.max_price)
            seen[key] = listing

    results = sorted(seen.values(), key=lambda x: x.score, reverse=True)[: args.top]

    print(render_console(results, dropped, args.max_price))

    Path(f"{args.out}.json").write_text(
        json.dumps([dataclasses.asdict(r) for r in results], indent=2, ensure_ascii=False),
        encoding="utf-8")
    page_html = render_html(results, args.max_price)
    Path(f"{args.out}.html").write_text(page_html, encoding="utf-8")
    print(f"\nWrote {args.out}.json and {args.out}.html", file=sys.stderr)

    if args.email:
        if not args.to:
            sys.exit("--email needs --to or DIGEST_TO")
        send_email(f"{len(results)} Garmin Edge deals under {args.max_price} EUR",
                   page_html, args.to)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
