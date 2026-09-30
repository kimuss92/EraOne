"""era_offers.py — turn a brand's standing promotions on and off by voice or text.

A promotion lives in `brands.yaml` under `brands.<key>.offers[]`. An offer with
`active: true` is one the community-management agent may put into promo content
today, without ever asking you whether it's still running. When a promo ends you
say so once — *"Era, the -10% discount is no longer valid"* — and this module flips
`active: false`, so no future content can use it.

Why this module exists rather than living in `era_dispatch.py`:
`era_dispatch` reads `brands.yaml` with `yaml.safe_load`, which throws away every
comment in the file. `brands.yaml` is the routing seatbelt and its comments record
*why* each account id is what it is — rewriting it with safe_load would silently
delete them. So, exactly like `era_scheduler.set_schedule`, the write path here
goes through ruamel's round-trip loader, which preserves comments and formatting.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from ruamel.yaml import YAML

BRANDS_FILE = Path(__file__).with_name("brands.yaml")

_yaml = YAML()
_yaml.preserve_quotes = True  # edits stay minimal; brands.yaml's comments survive

# Serialize writes within this process. era_scheduler holds its own lock over the
# same file; both are short critical sections around a load->mutate->dump, and a
# human saying "the promo is over" is not racing a cron tick for the same bytes.
_edit_lock = threading.Lock()


def _load():
    with open(BRANDS_FILE, encoding="utf-8") as f:
        return _yaml.load(f)


def _norm(text) -> str:
    """Fold an offer reference to a comparable core: '-10 %' / '10 percent' /
    'remise_10' all collapse toward '10'. Lets a spoken label match a yaml id."""
    t = str(text or "").lower().strip()
    t = t.replace("percent", "%").replace("pour cent", "%").replace("pourcent", "%")
    return re.sub(r"[^a-z0-9%]+", "", t)


def _matches(offer, ref: str) -> bool:
    """True when `ref` plausibly names this offer — by id, by label, or by the
    bare number ('-10%' names remise_10). Never matches on an empty ref."""
    if not ref:
        return False
    n = _norm(ref)
    if not n:
        return False
    for field in ("id", "label", "description"):
        val = _norm(offer.get(field))
        if val and (n == val or n in val or val in n):
            return True
    # Bare-number fallback: "10" / "-10%" -> remise_10, but only when the ref is
    # essentially just that number, so "10" can't match an unrelated "10 ans" offer.
    ref_digits = re.sub(r"\D", "", n)
    if ref_digits and re.fullmatch(r"-?%?\d+%?", n):
        for field in ("id", "label"):
            if ref_digits and ref_digits in re.sub(r"\D", "", _norm(offer.get(field))):
                return True
    return False


def list_offers(brand_key: str | None = None) -> list[dict]:
    """Every configured offer, optionally scoped to one brand. Each row carries its
    brand so a caller can report 'Delta Cuisine: -10% (active)' without a re-lookup."""
    cfg = _load()
    out = []
    for key, brand in (cfg.get("brands") or {}).items():
        if brand_key and key != brand_key:
            continue
        for offer in brand.get("offers") or []:
            out.append(
                {
                    "brand": key,
                    "id": offer.get("id"),
                    "label": offer.get("label"),
                    "description": offer.get("description"),
                    "active": bool(offer.get("active")),
                    "usable_in": list(offer.get("usable_in") or []),
                }
            )
    return out


def set_offer(brand_key: str, offer_ref: str, *, active: bool) -> dict:
    """Flip ONE offer's `active` flag in brands.yaml, keeping comments intact.

    `offer_ref` may be the yaml id ("remise_10") or how you say it ("-10%").
    Raises KeyError when the brand is unknown, the brand has no offers, the ref
    matches nothing, or the ref is ambiguous across several offers — an ambiguous
    match must never silently pick one, because turning off the wrong promotion
    would let a dead discount keep shipping in live content."""
    with _edit_lock:
        cfg = _load()
        brands = cfg.get("brands") or {}
        if brand_key not in brands:
            raise KeyError(f'Unknown brand "{brand_key}".')
        offers = brands[brand_key].get("offers")
        if not offers:
            raise KeyError(f'"{brand_key}" has no offers configured.')

        hits = [o for o in offers if _matches(o, offer_ref)]
        if not hits:
            known = ", ".join(str(o.get("label") or o.get("id")) for o in offers)
            raise KeyError(f'No offer matching "{offer_ref}" for {brand_key}. Known: {known}.')
        if len(hits) > 1:
            names = ", ".join(str(o.get("label") or o.get("id")) for o in hits)
            raise KeyError(f'"{offer_ref}" is ambiguous for {brand_key} — it matches: {names}.')

        offer = hits[0]
        offer["active"] = bool(active)
        with open(BRANDS_FILE, "w", encoding="utf-8") as f:
            _yaml.dump(cfg, f)

    return {
        "brand": brand_key,
        "id": offer.get("id"),
        "label": offer.get("label"),
        "active": bool(offer.get("active")),
    }
