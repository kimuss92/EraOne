# File approval reliability — October 18, 2026

Next establishment check: AUTH-2 / AUTH-3. The prior live rename used the desired
destination in name while omitting new_name; approval was consumed and execution
failed. It was not a clean cancellation. Keep that history and the protected
PendingConfirmationStore/HUD resolution semantics.

Plan before implementation:
- Share pure rename argument validation across live and background dispatch, and
  direct file handling. Invalid/incomplete calls return corrective tool errors
  before arming or consuming approval; never infer a destination from name.
- Treat new_name as a filename in the existing parent, preventing a rename from
  becoming a path move; retain collision refusal and safe-root checks.
- Clarify the existing schema's two supported source forms and destination field;
  regenerate the Anthropic golden after editing tool_declarations.py.
- Show source and destination in existing confirmation/HUD description paths,
  preserving their clipping and no-new-narration policies. No frontend build.
- Test disposable filesystem outcomes through existing approval machinery:
  missing arguments, valid approval, cancellation, mismatch, replay, collision,
  traversal and HUD resolution. Synthetic approvals belong only to isolated
  tests; never approve a live pending action on the owner's behalf.
- Run affected/protected suites, Ruff, manifest and golden checks, then document
  software evidence separately from the still-owed owner live approval test.

Verification and remaining acceptance will be recorded after the repair.
