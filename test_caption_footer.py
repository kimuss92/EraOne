"""
test_caption_footer.py — proves the bilingual caption-footer selector in
era_dispatch picks the footer whose language MATCHES the description body.

Ceram Quartz posts in FR or AR (Tunisian dialect). A French body must get the
French footer; an Arabic body must get the exact Arabic footer from brands.yaml.
A misread here would append the wrong-language contact block to a live post.
"""

import era_dispatch as ed

# Sentinels unique to each real footer so we assert on the RIGHT one.
_FR_MARK = "showroom à La Soukra"
_AR_MARK = "ابعثلنا ميساج على الصفحة"


def _cq_brand():
    return ed.load_brands()["brands"]["ceram_quartz"]


def test_has_arabic_detects_script():
    assert ed._has_arabic("زين مطبخك") is True
    assert ed._has_arabic("Sublimez votre cuisine") is False
    assert ed._has_arabic("") is False
    # A mostly-French caption with one Arabic word still counts as Arabic-bearing.
    assert ed._has_arabic("Promo سعر") is True


def test_french_description_gets_french_footer():
    cq = _cq_brand()
    desc = "Sublimez votre cuisine avec le quartz Ceram. Devis gratuit au showroom."
    footer = ed.select_caption_footer(cq, desc)
    assert _FR_MARK in footer
    assert _AR_MARK not in footer


def test_arabic_description_gets_arabic_footer():
    cq = _cq_brand()
    desc = "زين مطبخك مع رخام سيرام كوارتز. تعال للشوروم وخذ فكرة على الأسعار."
    footer = ed.select_caption_footer(cq, desc)
    assert _AR_MARK in footer
    assert _FR_MARK not in footer


def test_arabic_footer_is_verbatim_from_brands_yaml():
    cq = _cq_brand()
    # The selector must return the configured block unchanged, not a paraphrase.
    assert (
        ed.select_caption_footer(cq, "عربي").strip() == (cq.get("caption_footer_ar") or "").strip()
    )


def test_falls_back_to_french_when_no_arabic_footer_configured():
    # A brand with only a French footer (e.g. Delta Cuisine) returns it for any language.
    fr_only = {"caption_footer": "FR only footer"}
    assert ed.select_caption_footer(fr_only, "عربي") == "FR only footer"
    assert ed.select_caption_footer(fr_only, "français") == "FR only footer"
