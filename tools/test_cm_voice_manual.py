"""Every example phrase in docs/CM_VOICE_MANUAL.md must route the way the manual says, and Era's
community pack must carry the rules she needs to help with CM orders."""

from pathlib import Path

import pytest

from app.cm_intents import _infer_cm_task_type, ads_mode, is_cm_order
from app.text_intents import _model_intent
from core import prompt_packs

ROOT = Path(__file__).resolve().parent.parent
MANUAL = ROOT / "docs" / "CM_VOICE_MANUAL.md"

# (phrase, lane, ads mode or None): these are the manual's "Say this" examples.
ORDERS = [
    ("Plan Delta Cuisine's week", "weekly", None),
    ("Plan this week for Ceram Quartz", "weekly", None),
    ("Approve the plan", "approve", None),
    ("Approve reel 1 and 3", "approve", None),
    ("Change reel 1's texture", "revise", None),
    ("Change the caption of reel 2", "revise", None),
    ("Publish reel 1 now", "publish", None),
    ("Prepare the prompts for a Ceram Quartz Paletino reel", "package", None),
    ("Urgent: promo reel for Delta Cuisine now", "rush", None),
    ("Build a campaign for the new Rosieres pack", "campaign", None),
    ("Make the ad creative for the Delta promo", "adcreative", None),
    ("Run a Meta ad campaign for Delta Cuisine on the last reel, 5 euros a day for 10 days", "ads", "build"),
    ("Boost the last reel for Delta Cuisine", "ads", "build"),
    ("Sponsor the last reel", "ads", "build"),
    ("Prepare a Meta ad for Delta Cuisine to get messages on WhatsApp", "ads", "build"),
    ("Lance une campagne Meta pour Delta Cuisine", "ads", "build"),
    ("How are my campaigns doing?", "ads", "report"),
    ("Report on my Meta ads for Delta Cuisine", "ads", "report"),
    ("Pause the Delta Cuisine campaign", "ads", "manage"),
    ("Pause the Delta Cuisine dressing reel", "ads", "manage"),
    ("Boost budget on the Taj Mahal messages campaign", "ads", "manage"),
    ("Rename the active Delta campaigns to the naming standard", "ads", "manage"),
    ("Pause ad 52596011234040", "ads", "manage"),
    ("Activate ad 52596011234040", "ads", "manage"),
    ("Move the remaining budget to the Interactions campaign", "ads", "manage"),
    ("Optimize the Interactions campaign for Delta Cuisine", "ads", "manage"),
]

BRAINS = [
    ("Use Claude", "era.reason"),
    ("Use OmniRoute", "brain:omniroute"),
    ("Use the local model", "era.classify"),
    ("Back to automatic routing", "auto"),
    ("Which brain are you using", "brain:status"),
]


@pytest.mark.parametrize("phrase,lane,mode", ORDERS)
def test_every_manual_order_routes_to_its_lane(phrase, lane, mode):
    assert _infer_cm_task_type(phrase) == lane, phrase
    assert is_cm_order(phrase) or lane in ("weekly", "rush"), phrase
    if mode:
        assert ads_mode(phrase) == mode, phrase


@pytest.mark.parametrize("phrase,token", BRAINS)
def test_every_manual_brain_order_is_understood(phrase, token):
    assert _model_intent(phrase) == token, phrase


def test_the_manual_exists_and_quotes_every_verified_phrase():
    text = MANUAL.read_text(encoding="utf-8")
    for phrase, *_ in ORDERS + BRAINS:
        assert phrase in text, phrase
    for needle in ("shazam", "Gate 1", "Gate 2", "Publish", "welcome template", "Démarrer des conversations"):
        assert needle in text, needle


def test_eras_community_pack_teaches_the_meta_ads_rules():
    pack = prompt_packs.load("community")
    for needle in (
        "Interactions",
        "OUTCOME_ENGAGEMENT",
        "Démarrer des conversations 29/11/2025",
        "Send WhatsApp message",
        "never NO_BUTTON",
        "draft",
        "Publish in Ads Manager",
        "use OmniRoute",
        "ask Tom",
        "Era_CM_Voice_Manual",
    ):
        assert needle in pack, needle
    # the old rules she already had stay
    assert "manage_schedule" in pack and "manage_offer" in pack
    assert len(pack) < 7000  # it rides on a tool result: keep it lean
