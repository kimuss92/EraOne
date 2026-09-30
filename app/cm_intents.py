"""CM (community-management) task-type inference (extracted from main.py).

Maps a typed/spoken order to a dispatch task_type via regex intent matching.
Pure; re-imported by main.py.
"""

import re

# ── CM task-type inference: planning vs publishing ─────────────
# Hermes only classifies "cm" vs "assistant" — it carries NO plan-vs-publish signal.
# The live typed path used to hardcode task_type="weekly", so a typed "publish the
# approved reel" dispatched as a weekly PLAN (wrong brief) AND missed the publish
# Composio allowlist in era_dispatch (post silently denied headless). This maps a
# publish/post/schedule-a-post order to the "publish" task_type so the dispatch runs
# the publish flow with Composio pre-approved. See [[era-headless-publish-perms]].
_PUBLISH_INTENT = re.compile(
    r"\b(publish|publishing|publie|publier|poste[rz]?)\b"
    r"|\bpost\s+(it|this|these|them|the|now)\b"
    r"|\bschedule\s+(the\s+)?(post|reel|carousel|carrousel|story)\b",
    re.IGNORECASE,
)

# A correction to ONE already-planned item ("change reel 1's texture", "rewrite the
# carousel description"). Must name a change verb AND an asset noun, so "change the
# plan" or a bare "change it" falls through to weekly rather than half-editing a run.
_REVISE_INTENT = re.compile(
    r"\b(change|changer|modif(?:y|ie[rz]?)|replace|remplace[rz]?|rewrite|réécri[rs]|"
    r"redo|refais|fix|corrige[rz]?|swap)\b"
    r".{0,40}?\b(reel|réel|carousel|carrousel|post|caption|description|texture|"
    r"material|matériau|hook|visual|asset|slide)\b",
    re.IGNORECASE | re.DOTALL,
)

# A sudden, time-sensitive campaign that must jump ahead of the weekly cadence.
_RUSH_INTENT = re.compile(
    r"\b(urgent|urgence|rush|asap|right now|tout de suite|immediately|"
    r"quick(?:ly)?|vite|rapide(?:ment)?)\b",
    re.IGNORECASE,
)

_CAMPAIGN_INTENT = re.compile(r"\b(campaign|campagne)\b", re.IGNORECASE)

# Meta Ads (paid) orders — checked BEFORE the organic _CAMPAIGN_INTENT so a paid
# order ("run a Meta ad campaign") lands in the ads lane, not the organic weekly
# lane's "campaign" reading. "budget" and "boost" alone are enough (an order that
# names spend is unambiguous even without the word "ads"); "campaign"/"campagne"
# alone is NOT enough here — that's the organic lane's word too, and only becomes
# an ads order when paired with one of these ad-specific terms.
_ADS_INTENT = re.compile(
    r"\b(meta\s*ads?|facebook\s*ads?|paid\s*(?:ad|campaign)|sponsor(?:ed|ing|isé[e]?)?|"
    r"publicit[ée]|boost(?:ed|ing)?\s+(?:this\s+)?post|ad\s*budget|budget\s*publicitaire)\b",
    re.IGNORECASE,
)

# Meta Ads OPERATIONS on campaigns that already exist — reading results and
# changing them (added 2026-09-19). _ADS_INTENT above only knows how to START a
# campaign; without these, "how are my campaigns doing" or "add $2 to the budget"
# fell through to the organic lanes and got a content-planning brief.
#
# Two separate patterns because the two orders carry different risk:
#   REPORT is read-only, so a false positive costs nothing — it can be wide, and
#     may use a bare "ads".
#   MANAGE changes spend/state, so it is deliberately tight: the target must be a
#     campaign / ad set / "Meta|Facebook|paid ads" (never a bare "ad", or "stop
#     the ad" on a video would start editing a live campaign), and every manage
#     order is still confirmation-gated inside the brief (era_dispatch).
# "review" is left out of REPORT on purpose: "review the campaign plan" is an
# organic Gate-1 order and must stay there.
_ADS_TARGET = r"(?:campaigns?|campagnes?|ad\s*sets?|adsets?|(?:meta|facebook|paid)\s*ads?)"
_ADS_REPORT_INTENT = re.compile(
    r"\b(?:results?|performance|report|reports|analy[sz]e|analysis|r[ée]sultats?|rapport|"
    r"bilan|analyse|check|status|monitor|statut|suivi)\b.{0,30}?\b(?:ads?|campaigns?|campagnes?|"
    r"publicit[ée]s?|ad\s*sets?)\b"
    r"|\b(?:ads?|campaigns?|campagnes?|publicit[ée]s?|ad\s*sets?)\b.{0,30}?\b(?:results?|"
    r"performance|report|doing|work(?:s|ed|ing)?|r[ée]sultats?|rapport|bilan|status)\b"
    r"|\bhow\s+(?:is|are|did|do)\b.{0,40}?\b(?:ads?|campaigns?|campagnes?)\b"
    # Added 2026-09-25 with the in-house analyst: diagnosis / audit / benchmark /
    # fatigue / competitor questions are analysis, and fell through to BUILD —
    # which asks for a budget. REPORT is read-only, so it may be wide.
    r"|\b(?:why|pourquoi|audit|diagnos\w*|benchmarks?|compare|comparer|insights?)\b.{0,50}?"
    r"\b(?:ads?|campaigns?|campagnes?|publicit[ée]s?|ad\s*sets?|cost|co[uû]ts?|cpm|cpc|ctr|"
    r"spend|d[ée]penses?)\b"
    r"|\b(?:competitors?|concurrents?|ad\s*library)\b.{0,40}?\b(?:ads?|publicit\w*|running)\b"
    r"|\b(?:ads?|publicit[ée]s?)\b.{0,40}?\b(?:competitors?|concurrents?)\b"
    r"|\b(?:ads?|creatives?|campaigns?)\b.{0,30}?\b(?:tired|fatigued?|fatigue|dead|saturated)\b",
    re.IGNORECASE | re.DOTALL,
)
_ADS_MANAGE_INTENT = re.compile(
    r"\b(?:extend|prolong|prolonge[rz]?|pause|pauser|resume|reprendre|relance[rz]?|scale|"
    r"duplicate|duplique[rz]?|stop|arr[êe]te[rz]?)\b.{0,30}?\b" + _ADS_TARGET + r"\b"
    r"|\b(?:add|more|increase|raise|reduce|lower|cut|extra|augmente[rz]?|ajoute[rz]?|"
    r"diminue[rz]?|baisse[rz]?)\b.{0,25}?\bbudget\b"
    r"|\b(?:extend|prolong|prolonge[rz]?)\b.{0,25}?\b(?:days?|jours?|weeks?|semaines?|end\s*date|"
    r"date\s*de\s*fin|time|duration)\b"
    r"|\b(?:expand|widen|broaden|elargi[rs]?|[ée]largi[rs]?)\b.{0,25}?\b(?:audience|targeting|"
    r"ciblage|reach)\b"
    r"|\b(?:change|edit|swap|replace|modifie[rz]?)\b.{0,25}?\b(?:audience|targeting|ciblage)\b"
    # Added 2026-09-25 — "switch placements to manual, remove Facebook Feed" and
    # "pause ad 52579562852040" fell through to BUILD/REPORT and changed nothing.
    # A bare "ad" is still never a target; an ad named by its numeric id is.
    r"|\b(?:pause|pauser|resume|reprendre|stop|arr[êe]te[rz]?|activate|active[rz]?)\b"
    r".{0,15}?\bads?\s+(?:id\s+)?\d{6,}"
    r"|\b(?:remove|exclude|drop|turn\s+off|disable|switch|change|edit|set|keep|retire[rz]?|"
    r"enl[èe]ve[rz]?|exclu(?:re|ez)|d[ée]sactive[rz]?)\b.{0,40}?\b(?:placements?|emplacements?|"
    r"(?:facebook|instagram)\s+(?:feed|reels|stories)|audience\s*network)\b"
    r"|\b(?:optimi[sz]e|optimise[rz]?|improve|am[ée]liore[rz]?|lower|reduce|r[ée]dui(?:re|sez))\b"
    r".{0,40}?\b(?:cost\s*per|co[uû]t\s*par|cpr|cpc|cpm)\b"
    r"|\b(?:optimi[sz]e|optimise[rz]?)\b.{0,30}?\b" + _ADS_TARGET + r"\b"
    r"|\b(?:reallocate|re-allocate|move|shift|transfer|r[ée]allou\w*|d[ée]place[rz]?|"
    r"transf[èe]re[rz]?)\b.{0,40}?\bbudget\b"
    r"|\b(?:change|set|restrict|limit|narrow|exclude|remove)\b.{0,25}?\b(?:ages?|[âa]ges?|"
    r"genders?|locations?|regions?|cities)\b",
    re.IGNORECASE | re.DOTALL,
)


def ads_mode(text: str) -> str:
    """What kind of ads order is this: "manage", "report" or "build".

    manage wins over report because a mixed order ("check the results and add
    budget if it's winning") must reach the brief that is allowed to act — it
    still reports first, and its confirmation rules still apply. Anything that
    names neither is a build (start a campaign), which is the lane's original job.
    """
    t = text or ""
    if _ADS_MANAGE_INTENT.search(t):
        return "manage"
    if _ADS_REPORT_INTENT.search(t):
        return "report"
    return "build"


# Ad CREATIVE (make the picture) as opposed to ads (spend the money). Deliberately
# narrow: every alternative must pair an ad/banner noun with something concrete, so
# a bare "ad" never triggers it. Checked LATE — see _infer_cm_task_type's docstring
# for why it sits below revise/approve/publish rather than next to _ADS_INTENT.
_ADCREATIVE_INTENT = re.compile(
    r"\bad\s*(?:creative|visual|banner|mockup|format|size|ratio)s?\b"
    r"|\bcr[ée]a(?:tion)?s?\s*(?:pub|publicitaires?)\b"
    r"|\bvisuels?\s*publicitaires?\b"
    r"|\bbanni[èe]re?s?\b|\bbanners?\b"
    r"|\b(?:resize|redimensionne[rz]?)\b.{0,30}?"
    r"\b(?:stor(?:y|ies)|placements?|formats?|ratios?|reels?|feed)\b",
    re.IGNORECASE | re.DOTALL,
)

# An approval, at whichever gate the named item currently sits (the CM reads plan.md
# to tell which). Deliberately the VERB only: "approve"/"approuve"/"valide" match,
# while the ADJECTIVE "approved" does not — otherwise "publish the approved posts"
# would read as an approval order and route away from the publish lane.
_APPROVE_INTENT = re.compile(
    r"\b(approve|approuve|approuver|approuvez|valide|valider|validez)\b",
    re.IGNORECASE,
)


def _infer_cm_task_type(text: str) -> str:
    """Map a CM utterance to a dispatch lane.

    Order matters — the most specific intent wins:
      ads      → a Meta Ads / paid-spend order ("boost this post", "run a Meta ad
                 campaign"). Checked FIRST, ahead of revise/approve/campaign: ads
                 vocabulary ("sponsored", "ad budget", "Facebook ads") is specific
                 enough that it never false-positives on organic orders, and an
                 ads-flavored "approve"/"change" means something entirely different
                 (activate a paused campaign; edit ad copy) from the organic Gate-2
                 flow those words trigger everywhere else — it needs its own lane,
                 not a fallthrough into revise/approve's organic brief.
      revise   → a correction to a named item ("change reel 1's texture"). Checked
                 next because a mixed order ("approve 2 and 3, change reel 1") must
                 land in the lane that can do both; the revise brief covers approvals.
      approve  → an approval ("approve the plan", "approve reel 1"). Checked before
                 publish so "approve and publish reel 1" approves the asset at Gate 2
                 rather than sweeping every already-approved file.
      publish  → a clear publish/post/schedule-a-post order.
      adcreative → make ad creative (the picture), not run ads (the money). Checked
                 LATE on purpose, below all four lanes above, because each of them
                 beats it on a mixed order: "change the ad visual's texture" is a
                 correction and must stay in revise (that lane is also where
                 record_feedback captures the lesson — routing it here would lose
                 the preference silently), and "publish the ad creative" is a
                 publish. It sits ABOVE rush/campaign so "urgent ad creative" and
                 "ad creative for the summer campaign" both land here, which is
                 right — those are creative production, not an organic slate.
                 Ads still wins outright: an order naming spend must reach the lane
                 that asks about budget, never the one that silently generates.
      rush     → a sudden time-sensitive campaign (always available, even mid-week).
      campaign → an on-demand campaign build.
      weekly   → everything else (plan the week / produce).

    Conservative on purpose: a bare noun "post" (e.g. "plan this week's posts") does
    NOT match publish — only post + it/this/now — so planning orders never get
    mis-dispatched as a live publish. Likewise revise needs a change verb AND an
    asset noun, so it can never swallow a plain planning order."""
    t = text or ""
    if _ADS_INTENT.search(t) or _ADS_MANAGE_INTENT.search(t) or _ADS_REPORT_INTENT.search(t):
        return "ads"
    if _REVISE_INTENT.search(t):
        return "revise"
    if _APPROVE_INTENT.search(t):
        return "approve"
    if _PUBLISH_INTENT.search(t):
        return "publish"
    if _ADCREATIVE_INTENT.search(t):
        return "adcreative"
    if _RUSH_INTENT.search(t):
        return "rush"
    if _CAMPAIGN_INTENT.search(t):
        return "campaign"
    return "weekly"


# Intents that positively identify a message as a community-management order.
#
# _RUSH_INTENT is deliberately EXCLUDED: it matches bare urgency words
# ("quickly", "asap"), which qualify a CM order but are not evidence of one.
# Including it would classify "explain this quickly" as brand work.
_CM_ORDER_INTENTS = (
    _PUBLISH_INTENT,
    _REVISE_INTENT,
    _APPROVE_INTENT,
    _CAMPAIGN_INTENT,
    _ADS_INTENT,
    _ADS_MANAGE_INTENT,
    _ADS_REPORT_INTENT,
    _ADCREATIVE_INTENT,
)


def is_cm_order(text: str) -> bool:
    """True when the message is a community-management order.

    NOT the same question as _infer_cm_task_type, and conflating them is a real
    trap: that function decides WHICH CM lane a message belongs to once
    something has already established that it IS one, and it falls back to
    "weekly" for everything unmatched. So it returns a truthy lane for literally
    any string, including "why is it slow" — using it as an is-this-CM test
    classifies the entire world as brand work.
    """
    body = (text or "").strip()
    if not body:
        return False
    return any(pattern.search(body) for pattern in _CM_ORDER_INTENTS)
