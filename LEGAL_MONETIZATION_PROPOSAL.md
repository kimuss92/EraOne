# Proposal: Autonomous Legal Assistant Module (EraOne extension)

Status: PROPOSAL ONLY — NOT BUILT. Requires user approval + legal review.

Concept: Add an `actions/legal_automation.py` module for a SPECIFIC, LEGAL service
(e.g., automatic NDA generation, contract review checklist, or small-claims filing
prep). It would use the existing Gemini + web_search pipeline to fetch templates
and produce formatted documents.

Revenue claim ($1K/mo): NOT GUARANTEED. To validate:
- Define exact service (which legal task?).
- Confirm compliance (not practicing law without license; must include disclaimer).
- Build + test with pilot users.
- This is a business model, not a code feature.

Safety: No autonomous action on legal filings without human confirmation.
No confidential data processing without encryption at rest (current config does
not enforce; must add before handling docs).

Next step: User picks (a) specific service, (b) skips, or (c) defines deliverable.
