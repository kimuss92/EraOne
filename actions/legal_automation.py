"""Legal automation scaffold — EXPLICIT DEFINITION ONLY — NOT autonomous legal practice.
Service defined: AUTO-NDA GENERATOR (small-business / freelancer contracts).
Safety: requires human confirmation before output; includes DISCLAIMER; no filing/submission.
Revenue: proposal only (~$1K/mo requires 20-30 clients at $30-50/mo; NOT GUARANTEED).
Not for harm: generates template text, not legal advice; user must review.
"""

DISCLAIMER = (
    "DISCLAIMER: This tool generates document templates for informational purposes only. "
    "It does not constitute legal advice, create an attorney-client relationship, or "
    "substitute for licensed legal counsel in your jurisdiction. Always review with a lawyer."
)


def generate_nda(client_name: str, service_summary: str) -> dict:
    """Produce a NON-DISCLOSURE AGREEMENT draft — requires human confirmation."""
    draft = (
        f"NON-DISCLOSURE AGREEMENT (DRAFT)\n"
        f"Parties: {client_name}\n"
        f"Purpose: {service_summary}\n"
        f"Confidential Information: All proprietary, technical, and business information.\n"
        f"Term: 2 years from execution.\n"
        f"[DISCLAIMER — NOT LEGAL ADVICE]\n"
    )
    return {
        "draft": draft,
        "disclaimer": DISCLAIMER,
        "requires_human_review": True,
        "autonomous_action_permitted": False,
        "revenue_projection": "Not guaranteed; requires defined pricing + clients.",
    }
