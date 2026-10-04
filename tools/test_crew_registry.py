"""crew registry, transport contract and the "add a bot with zero code edits" property.

* the six shipped roles load, with scoped capabilities (Nabar and Cody cannot write);
* a bad bot file skips THAT bot with a reason and costs the rest of the crew nothing;
* there is no delete anywhere on the surface, and nothing in the crew can approve;
* the Transport contract is what callers depend on — a second transport (Hermes
  Desktop, later) drops in without touching the callers;
* the crew never imports lane_router (a coupling is a Nabar flag, not a detail);
* adding a bot is data only: yaml + prompt, and not one source file changes.
"""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

import pytest

from core import vault_bridge
from core.tool_result import ToolResult
from crew import capabilities as caps
from crew import runtime
from crew.registry import load_specs
from crew.runtime import InProcessTransport
from crew.transport import (
    ERROR_TYPES,
    BotSpec,
    HealthStatus,
    Transport,
    Turn,
    fail,
)
from tools.crew_testkit import ScriptedLLM, assert_crew_failure_shape, call, final

REPO = Path(__file__).resolve().parent.parent
CREW_PY = sorted((REPO / "crew").glob("*.py"))


def write_bot(directory: Path, bot_id: str, **overrides) -> Path:
    cfg = {
        "id": bot_id,
        "role": "Tester",
        "description": f"{bot_id} for tests",
        "prompt": f"prompts/{bot_id}.md",
        "capabilities": "[vault.read]",
        "model_tier": "fast",
        "brands": '["*"]',
        "write_zones": "[inbox]",
        "handoffs_to": "[]",
        **overrides,
    }
    (directory / "prompts").mkdir(parents=True, exist_ok=True)
    if cfg["prompt"] == f"prompts/{bot_id}.md":
        (directory / "prompts" / f"{bot_id}.md").write_text(f"I am {bot_id}.", encoding="utf-8")
    path = directory / f"{bot_id}.yaml"
    path.write_text("\n".join(f"{k}: {v}" for k, v in cfg.items() if v is not None) + "\n", "utf-8")
    return path


# ── the shipped crew ────────────────────────────────────────────────────────
class TestShippedCrew:
    @pytest.fixture(scope="class")
    def report(self):
        return load_specs()

    def test_all_six_roles_load_and_nothing_is_skipped(self, report):
        assert sorted(report.specs) == ["cody", "drew", "laura", "maya", "nabar", "tom"]
        assert report.skipped == []

    def test_every_bot_names_a_real_role_and_prompt(self, report):
        roles = {b.id: b.role.lower() for b in report.specs.values()}
        assert "coordinator" in roles["laura"] and "research" in roles["drew"]
        assert "social" in roles["maya"] and "creative" in roles["tom"]
        assert "cod" in roles["cody"] and "review" in roles["nabar"]
        assert all(len(b.prompt) > 200 for b in report.specs.values())

    def test_laura_alone_coordinates(self, report):
        assert set(report.specs["laura"].handoffs_to) == {"drew", "maya", "tom", "cody", "nabar"}
        for bot_id, spec in report.specs.items():
            if bot_id != "laura":
                assert spec.handoffs_to == () and "crew.handoff" not in spec.capabilities

    def test_nabar_cannot_write_anything(self, report):
        nabar = report.specs["nabar"]
        assert nabar.write_zones == ()
        assert set(nabar.capabilities) == {"vault.read", "vault.search", "code.check"}

    def test_cody_cannot_write_the_vault_or_the_repo(self, report):
        cody = set(report.specs["cody"].capabilities)
        assert cody == {"code.propose", "code.check", "vault.read", "vault.search"}

    def test_only_drew_may_propose_replacing_a_note(self, report):
        holders = [b.id for b in report.specs.values() if "vault.replace_staged" in b.capabilities]
        assert holders == ["drew"]

    def test_every_default_zone_is_the_unbriefed_inbox(self, report):
        for spec in report.specs.values():
            assert set(spec.write_zones) <= {"inbox"}

    def test_every_capability_a_bot_lists_exists(self, report):
        for spec in report.specs.values():
            assert set(spec.capabilities) <= set(caps.CAPABILITIES)


# ── registry validation: a bad file costs only itself ───────────────────────
BAD_FILES = {
    "id_mismatch": {"id": "someone_else"},
    "bad_id_chars": {"id": "Bad-Id!"},
    "missing_role": {"role": None},
    "unknown_capability": {"capabilities": "[vault.read, vault.delete]"},
    "duplicate_capability": {"capabilities": "[vault.read, vault.read]"},
    "bad_tier": {"model_tier": "ultra"},
    "bad_brands": {"brands": '["Delta Cuisine!"]'},
    "bad_zones": {"write_zones": "[everywhere]"},
    "bad_handoff": {"handoffs_to": '["NOT AN ID"]'},
    "missing_prompt_file": {"prompt": "prompts/nope.md"},
    "prompt_traversal": {"prompt": "../../outside.md"},
    "capabilities_not_a_list": {"capabilities": "vault.read"},
}


class TestRegistryValidation:
    @pytest.mark.parametrize("case", sorted(BAD_FILES))
    def test_bad_file_is_skipped_with_a_reason_and_the_rest_still_load(self, tmp_path, case):
        crew = tmp_path / "crew"
        write_bot(crew, "good")
        write_bot(crew, "broken", **BAD_FILES[case])
        (tmp_path / "outside.md").write_text("secret", encoding="utf-8")
        report = load_specs(crew)
        assert list(report.specs) == ["good"]
        assert [name for name, _ in report.skipped] == ["broken.yaml"]
        assert report.skipped[0][1].strip()

    def test_unknown_capability_skips_the_whole_bot_not_just_the_name(self, tmp_path):
        write_bot(tmp_path, "half", capabilities="[vault.read, vault.teleport]")
        assert load_specs(tmp_path).specs == {}

    def test_yaml_syntax_error_and_non_mapping_are_skipped(self, tmp_path):
        write_bot(tmp_path, "good")
        (tmp_path / "garbled.yaml").write_text("id: [unclosed\n  - : :", encoding="utf-8")
        (tmp_path / "listy.yaml").write_text("- just\n- a list\n", encoding="utf-8")
        report = load_specs(tmp_path)
        assert list(report.specs) == ["good"] and len(report.skipped) == 2

    def test_empty_or_missing_directory_degrades_to_an_empty_crew(self, tmp_path):
        assert load_specs(tmp_path).specs == {}
        assert load_specs(tmp_path / "does-not-exist").specs == {}

    def test_oversized_prompt_is_refused(self, tmp_path):
        write_bot(tmp_path, "big")
        (tmp_path / "prompts" / "big.md").write_text("x" * 30_000, encoding="utf-8")
        assert load_specs(tmp_path).specs == {}

    def test_defaults_are_the_safe_ones(self, tmp_path):
        write_bot(tmp_path, "plain", model_tier=None, brands=None, write_zones=None)
        spec = load_specs(tmp_path).specs["plain"]
        assert spec.model_tier == "fast" and spec.brands == ("*",)
        assert spec.write_zones == ("inbox",)


# ── nothing deletes, nothing approves ───────────────────────────────────────
class TestSurfaceHasNoDeleteAndNoApproval:
    BANNED = re.compile(r"delet|remov|erase|wipe|trash|confirm|approv|reject", re.IGNORECASE)

    def test_no_capability_name_deletes_or_approves(self):
        assert not [n for n in caps.CAPABILITIES if self.BANNED.search(n)]

    def test_bridge_actions_do_not_delete_or_approve(self):
        assert not [a for a in vault_bridge.ACTIONS if self.BANNED.search(a)]

    def test_tool_declaration_enums_carry_no_delete_or_approve(self):
        from app.tool_declarations import TOOL_DECLARATIONS

        seen = 0
        for tool in TOOL_DECLARATIONS:
            if tool["name"] in ("vault_bridge", "crew"):
                seen += 1
                enum = tool["parameters"]["properties"]["action"]["enum"]
                assert not [a for a in enum if self.BANNED.search(a)], tool["name"]
        assert seen == 2

    @pytest.mark.parametrize(
        "path", [*CREW_PY, REPO / "core" / "vault_bridge.py"], ids=lambda p: p.name
    )
    def test_no_crew_code_can_consume_the_confirmation_slot(self, path):
        """Arming the shared store (`.store(...)`) is allowed; consuming it is approving,
        and approving is the user's. Judged on the AST — real calls on a store object, a
        use of the `allow_sole` back door, or a reference to the approval check — so a
        docstring that merely names them, or a plain dict's `.clear()`, is not a hit."""
        consuming = {"confirm", "cancel", "clear", "expire_all"}  # peek/get only READ
        offences = []
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in consuming
                and "store" in ast.unparse(node.func.value).lower()
            ):
                offences.append(f"{ast.unparse(node.func)}()")
            if isinstance(node, ast.keyword) and node.arg == "allow_sole":
                offences.append("allow_sole=")
            if isinstance(node, ast.Name) and node.id == "_user_approved_pending":
                offences.append("_user_approved_pending")
        assert offences == [], f"{path.name}: {offences}"

    def test_the_ast_check_actually_catches_a_consuming_call(self):
        """Non-vacuity: prove the check above can fail."""
        bad = "from core.pending_confirmation import get_shared_store\nget_shared_store().confirm('x')"
        hits = [
            n
            for n in ast.walk(ast.parse(bad))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "confirm"
            and "store" in ast.unparse(n.func.value).lower()
        ]
        assert hits


# ── the transport contract ──────────────────────────────────────────────────
class TestTransportContract:
    def test_contract_is_exactly_the_four_methods(self):
        assert Transport.__abstractmethods__ == {"dispatch", "register", "capabilities", "health"}

    def test_an_incomplete_transport_cannot_be_instantiated(self):
        class Half(Transport):
            def register(self, bot_id, spec): ...

        with pytest.raises(TypeError):
            Half()

    def test_in_process_transport_implements_it(self):
        assert isinstance(InProcessTransport(llm=lambda *a: {}), Transport)

    def test_error_shape_is_closed(self):
        with pytest.raises(ValueError):
            fail("SomethingMadeUp", "nope")
        res = fail("UnknownBot", "No bot named 'x'.")
        assert_crew_failure_shape(res)
        assert res.error == res.message == res.data["detail"]

    def test_every_declared_error_type_builds_a_valid_failure(self):
        for error_type in sorted(ERROR_TYPES):
            assert_crew_failure_shape(fail(error_type, "something specific"))

    def test_dispatch_to_an_unknown_bot_is_a_shaped_failure_not_an_exception(self):
        t = InProcessTransport(llm=lambda *a: {})
        res = t.dispatch("ghost", Turn(task="hi"))
        assert_crew_failure_shape(res)
        assert res.error_type == "UnknownBot"

    def test_capabilities_of_an_unknown_bot_is_empty(self):
        assert InProcessTransport(llm=lambda *a: {}).capabilities("ghost") == []

    def test_health_makes_no_model_call(self):
        def boom(*a):
            raise AssertionError("health() must not call the model")

        t = InProcessTransport(llm=boom)
        assert t.health() == HealthStatus(False, "in_process", "no bots registered", 0)
        t.register("a", BotSpec("a", "R", "d", "p", ()))
        assert t.health().ok and t.health().bots == 1

    def test_reregistering_replaces_the_spec(self):
        t = InProcessTransport(llm=lambda *a: {})
        t.register("a", BotSpec("a", "R", "d", "p", ("vault.read",)))
        t.register("a", BotSpec("a", "R", "d", "p", ("vault.search",)))
        assert t.capabilities("a") == ["vault.search"]

    def test_a_crashing_model_is_a_shaped_failure_never_an_exception(self):
        def explode(*a):
            raise RuntimeError("model fell over")

        t = InProcessTransport(llm=explode)
        t.register("a", BotSpec("a", "R", "d", "p", ()))
        res = t.dispatch("a", Turn(task="x"))
        assert_crew_failure_shape(res)
        assert res.error_type == "BotError"


# Era's own in-process CM-vs-assistant router (formerly hermes_*; "Hermes" now means the
# separate Hermes Agent app, which a crew transport may legitimately talk to).
_ERA_ROUTER_MODULES = {"lane_router", "lane_dispatch"}


class TestNoLaneRouterCoupling:
    @pytest.mark.parametrize("path", CREW_PY, ids=lambda p: p.name)
    def test_crew_source_never_imports_the_router(self, path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
                imported += [f"{node.module}.{a.name}" for a in node.names]
        assert not [m for m in imported if _ERA_ROUTER_MODULES & set(m.split("."))], path.name

    def test_importing_the_crew_does_not_load_the_router(self):
        import subprocess
        import sys

        code = (
            "import sys; import crew.runtime, crew.capabilities, crew.registry, crew.nabar;"
            "sys.exit(1 if any(m in ('lane_router', 'lane_dispatch') for m in sys.modules) else 0)"
        )
        proc = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), capture_output=True)
        assert proc.returncode == 0, proc.stderr.decode()[-400:]


class FakeDesktopTransport(Transport):
    """What a Hermes Desktop transport has to look like: the contract and nothing else."""

    name = "fake_desktop"

    def __init__(self):
        self.seen: list[tuple[str, str]] = []

    def register(self, bot_id, spec):
        pass

    def capabilities(self, bot_id):
        return ["vault.read"]

    def dispatch(self, bot_id, turn):
        self.seen.append((bot_id, turn.task))
        return ToolResult.ok(
            message=f"{bot_id} (remote) handled: {turn.task}", data={"bot": bot_id}
        )

    def health(self):
        return HealthStatus(True, self.name, "remote up", 1)


class TestAnotherTransportDropsIn:
    @pytest.fixture(autouse=True)
    def restore(self):
        yield
        runtime.reset_transport(None)

    def test_the_crew_tool_runs_unchanged_over_a_different_transport(self):
        fake = FakeDesktopTransport()
        runtime.reset_transport(fake, load_specs().specs)
        asked = runtime.run_tool({"action": "ask", "bot": "drew", "task": "find the cadence"})
        assert asked.success and "remote" in asked.message
        assert fake.seen == [("drew", "find the cadence")]
        listed = runtime.run_tool({"action": "list"})
        assert listed.success and "drew (Research)" in listed.message
        assert "remote up" in listed.message
        assert runtime.run_tool({"action": "status"}).success

    def test_malformed_requests_are_shaped_failures(self):
        runtime.reset_transport(FakeDesktopTransport(), {})
        for params in ({"action": "ask"}, {"action": "ask", "task": "  "}, {"action": "dance"}):
            res = runtime.run_tool(params)
            assert_crew_failure_shape(res)
            assert res.error_type == "BadRequest"
        assert runtime.run_tool(None).error_type == "BadRequest"


# ── adding a bot is data only ───────────────────────────────────────────────
def source_fingerprint() -> str:
    h = hashlib.sha256()
    for root in ("core", "app", "crew", "actions"):
        for path in sorted((REPO / root).rglob("*.py")):
            h.update(path.relative_to(REPO).as_posix().encode())
            h.update(path.read_bytes())
    return h.hexdigest()


class TestAddingABot:
    def test_a_new_bot_needs_yaml_and_a_prompt_and_not_one_source_file(self, tmp_path):
        before = source_fingerprint()

        crew_dir = tmp_path / "crew"
        for name in ("laura", "drew"):
            for rel in (f"{name}.yaml", f"prompts/{name}.md"):
                dst = crew_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes((REPO / "config" / "crew" / rel).read_bytes())
        # The ONLY things added for the new bot:
        write_bot(crew_dir, "ines", role="Accounting", capabilities="[vault.search, vault.read]")
        # ...and Laura is told she may route to her, by editing Laura's yaml (data).
        laura = crew_dir / "laura.yaml"
        laura.write_text(
            laura.read_text(encoding="utf-8").replace(
                "[drew, maya, tom, cody, nabar]", "[drew, ines]"
            ),
            encoding="utf-8",
        )

        report = load_specs(crew_dir)
        assert sorted(report.specs) == ["drew", "ines", "laura"] and report.skipped == []

        llm = ScriptedLLM(
            {
                "laura": [
                    call("crew.handoff", to="ines", task="what do we owe the stone supplier?"),
                    final("Ines says nothing is on the books."),
                ],
                "ines": [call("vault.search", query="supplier"), final("No supplier notes found.")],
            }
        )
        t = InProcessTransport(llm=llm)
        for bot_id, spec in report.specs.items():
            t.register(bot_id, spec)
        res = t.dispatch("laura", Turn(task="ask Ines about the supplier"))

        assert res.success and "Ines says" in res.message
        # Ines takes two steps (search, then answer), then Laura finishes.
        assert llm.bots_called() == ["laura", "ines", "ines", "laura"]
        assert source_fingerprint() == before  # zero edits under core/ app/ crew/ actions/

    def test_the_new_bot_gets_only_what_its_yaml_grants(self, tmp_path):
        write_bot(tmp_path, "ines", capabilities="[vault.read]")
        t = InProcessTransport(
            llm=ScriptedLLM({"ines": [call("vault.create", title="x", content="y"), final("done")]})
        )
        t.register("ines", load_specs(tmp_path).specs["ines"])
        res = t.dispatch("ines", Turn(task="write something"))
        assert res.success and res.data["effects"] == []  # the create was denied, not run

    def test_laura_cannot_route_to_a_bot_her_yaml_does_not_name(self, tmp_path):
        write_bot(tmp_path, "laura", capabilities="[crew.handoff]", handoffs_to="[drew]")
        write_bot(tmp_path, "ines", capabilities="[vault.read]")
        llm = ScriptedLLM(
            {
                "laura": [call("crew.handoff", to="ines", task="x"), final("could not route")],
                "ines": [final("I should never run")],
            }
        )
        t = InProcessTransport(llm=llm)
        for spec in load_specs(tmp_path).specs.values():
            t.register(spec.id, spec)
        t.dispatch("laura", Turn(task="go"))
        assert "ines" not in llm.bots_called()
