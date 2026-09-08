"""End-to-end tests for the manager + delegate flow.

These run without any API credentials. Both delegate agents are stood in for
by stubs registered through the same registry the real ones use: a stub
classifier for the per-file `decide()` call, and a stub scheme proposer for
the one corpus-level `propose()` call that decides this run's vocabulary. That
is deliberate — if stubs can be swapped in here, the "add a dedupe agent
later" claim in the brief is real rather than aspirational, and it is the same
seam for both agents.

The categories themselves are no longer a fixed list, so what is asserted here
is the *shape* of the flow rather than a vocabulary: the proposer sees exactly
the rule-unplaceable files in exactly one call, the classifier is restricted
to what the proposer proposed, and every name that becomes a folder is
sanitised before it reaches the filesystem.

    .venv/bin/python -m unittest discover -s tests -t .
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

# The state dir must be redirected BEFORE organiser modules read it, so that
# tests never write to the real ~/.file-organiser-agent. mkdtemp keeps it
# unique per process, so concurrent runs never share a plan store.
_TEST_STATE = tempfile.mkdtemp(prefix="organiser-state-")
os.environ["FILE_ORGANISER_STATE_DIR"] = _TEST_STATE

from organiser import delegates, undo_log  # noqa: E402
from organiser.categories import (  # noqa: E402
    FALLBACK_CATEGORY,
    MAX_CATEGORY_LENGTH,
    dedupe_categories,
    is_safe_category,
    normalise_category,
)
from organiser.classifier import ClassifierAgent  # noqa: E402
from organiser.delegates import (  # noqa: E402
    DelegateDecision,
    DelegateRequest,
    DelegateUnavailable,
    ProposedCategory,
    SchemeProposal,
    SchemeRequest,
)
from organiser.manager import FirstRunConfirmationRequired, Manager  # noqa: E402
from organiser.paths import unique_destination  # noqa: E402
from organiser.config import (  # noqa: E402
    DEFAULT_MODELS,
    ConfigError,
    config_path,
    load_settings,
)
from organiser.plan import PlanStore  # noqa: E402
from organiser.providers import ProviderError  # noqa: E402
from organiser.rules import classify_by_rules  # noqa: E402

# What the stub proposer proposes unless a test asks for something else.
# Deliberately not names the rule table could have produced, so "the
# classifier was given the proposal" is distinguishable from "the classifier
# was given some hardcoded list".
STUB_SCHEME = [
    ("Tenancy", "Leases and correspondence with a landlord"),
    ("Meeting Notes", "Minutes and agendas"),
]
STUB_SCHEME_NAMES = [name for name, _ in STUB_SCHEME]
STUB_REASON = "two clusters: tenancy paperwork and meeting records"


class StubClassifier:
    """Stands in for the real classifier. Deterministic, no network."""

    name = "stub"

    def __init__(self, available=True, detail="ready", raises=False, forced_category=None):
        self._available = available
        self._detail = detail
        self._raises = raises
        self._forced = forced_category
        self.seen: list[DelegateRequest] = []

    def available(self):
        return self._available, self._detail

    def decide(self, request: DelegateRequest) -> DelegateDecision:
        if self._raises:
            raise DelegateUnavailable("simulated outage")
        self.seen.append(request)
        # The vocabulary is proposed per run now, so the stub answers out of
        # the list it was handed rather than out of a fixed table.
        first = (request.allowed_categories or [FALLBACK_CATEGORY])[0]
        if self._forced is not None:
            return DelegateDecision(self._forced, "high", "forced by the test", self.name)
        # lease_agreement -> confident; everything else -> low confidence
        if "lease" in request.filename:
            return DelegateDecision(first, "high", "tenancy contract text", self.name)
        if request.snippet_is_binary:
            return DelegateDecision(FALLBACK_CATEGORY, "low", "binary, name gives nothing", self.name)
        return DelegateDecision(first, "medium", "reads like meeting notes", self.name)


class StubSchemeProposer:
    """Stands in for the scheme proposer. One call per run, no network."""

    name = "scheme-stub"

    def __init__(
        self,
        categories=STUB_SCHEME,
        available=True,
        detail="ready",
        raises=False,
        reason=STUB_REASON,
    ):
        self._categories = list(categories)
        self._available = available
        self._detail = detail
        self._raises = raises
        self._reason = reason
        self.calls: list[SchemeRequest] = []

    def available(self):
        return self._available, self._detail

    def propose(self, request: SchemeRequest) -> SchemeProposal:
        self.calls.append(request)
        if self._raises:
            raise DelegateUnavailable("simulated scheme outage")
        return SchemeProposal(
            categories=[
                ProposedCategory(name=name, description=description)
                for name, description in self._categories
            ],
            reason=self._reason,
            proposed_by=self.name,
        )


def register_stubs(classifier=None, proposer=None):
    """Put both stub agents in the registry and return them."""
    classifier = classifier if classifier is not None else StubClassifier()
    proposer = proposer if proposer is not None else StubSchemeProposer()
    delegates.register(classifier)
    delegates.register(proposer)
    return classifier, proposer


def stub_manager(**kwargs) -> Manager:
    return Manager(delegate_name="stub", scheme_name="scheme-stub", **kwargs)


def build_sandbox() -> Path:
    root = Path(tempfile.mkdtemp(prefix="organiser-sandbox-"))
    (root / "notes.md").write_text("hello")
    (root / "photo.jpg").write_bytes(b"\xff\xd8\xff fake jpeg")
    (root / "budget.csv").write_text("a,b,c")
    (root / "Screenshot 2026-08-19.png").write_bytes(b"\x89PNG fake")
    # rules can't place these -> delegation candidates
    (root / "lease_agreement").write_text("Dear Sir, the tenancy agreement for 12 Wattle St.")
    (root / "minutes.dat").write_text("MEETING MINUTES\nAttendees: A, B\n")
    (root / "blob.xyz").write_bytes(bytes(range(256)) * 4)
    # must be left alone
    (root / ".hidden").write_text("secret")
    (root / "subfolder").mkdir()
    (root / "subfolder" / "deep.txt").write_text("untouched")
    os.symlink(root / "notes.md", root / "link_to_notes")
    # pre-existing collision
    (root / "Documents").mkdir()
    (root / "Documents" / "notes.md").write_text("older file, must survive")
    # A folder the rule table would never produce: proves the proposer is
    # offered the user's own existing naming for reuse.
    (root / "Tax Returns").mkdir()
    # A hidden folder is not part of anyone's scheme.
    (root / ".cache").mkdir()
    return root


class RulePassTests(unittest.TestCase):
    def test_extension_matches(self):
        self.assertEqual(classify_by_rules("photo.jpg").category, "Images")
        self.assertEqual(classify_by_rules("budget.csv").category, "Spreadsheets")
        self.assertEqual(classify_by_rules("script.py").category, "Code")

    def test_filename_rule_beats_extension(self):
        # .png would be Images, but the screenshot name rule wins
        self.assertEqual(classify_by_rules("Screenshot 2026-08-19.png").category, "Screenshots")

    def test_unplaceable_returns_none(self):
        self.assertIsNone(classify_by_rules("lease_agreement"))
        self.assertIsNone(classify_by_rules("blob.xyz"))


class UniqueDestinationTests(unittest.TestCase):
    def test_appends_suffix_and_does_not_stack(self):
        root = Path(tempfile.mkdtemp())
        (root / "a.txt").write_text("x")
        first = unique_destination(root / "a.txt")
        self.assertEqual(first.name, "a_1.txt")
        # a claimed-but-not-yet-created destination is still respected
        second = unique_destination(root / "a.txt", reserved={first})
        self.assertEqual(second.name, "a_2.txt")


class CategoryNamingTests(unittest.TestCase):
    """`normalise_category` is the only thing standing between a model's
    output and a real directory name, so it is tested on its own."""

    def test_path_traversal_cannot_survive(self):
        result = normalise_category("../../etc")
        self.assertEqual(result, "etc")
        self.assertNotIn("..", result)
        self.assertNotIn("/", result)

    def test_separators_are_flattened(self):
        self.assertEqual(normalise_category("Invoices/2024"), "Invoices 2024")
        self.assertEqual(normalise_category(r"Invoices\2024"), "Invoices 2024")
        for raw in ("Invoices/2024", "a/b/c", "~/Documents"):
            self.assertNotIn("/", normalise_category(raw) or "")

    def test_reserved_names_are_rejected(self):
        for raw in (".", "..", "con", "CON", "prn", "aux", "nul"):
            self.assertIsNone(normalise_category(raw), raw)

    def test_empty_and_whitespace_are_rejected(self):
        for raw in ("", "   ", "\t\n", "___", "...", None):
            self.assertIsNone(normalise_category(raw), repr(raw))

    def test_control_characters_are_stripped(self):
        self.assertEqual(normalise_category("Inv\x00oi\nces"), "Invoices")
        self.assertEqual(normalise_category("Tax\x07 Returns"), "Tax Returns")

    def test_accents_degrade_to_ascii_without_losing_the_letter(self):
        self.assertEqual(normalise_category("Facturación"), "Facturacion")
        self.assertEqual(normalise_category("Résumés & Références"), "Resumes & References")

    def test_length_is_capped(self):
        long_words = "Very long category name that keeps going and going past the cap"
        capped = normalise_category(long_words)
        self.assertLessEqual(len(capped), MAX_CATEGORY_LENGTH)
        # cut on a word boundary, so the folder still reads
        self.assertTrue(long_words.startswith(capped))
        # a single unbroken word is still cut hard
        self.assertEqual(len(normalise_category("A" * 60)), MAX_CATEGORY_LENGTH)

    def test_is_safe_category_agrees_with_normalise(self):
        self.assertTrue(is_safe_category("Tax Returns"))
        self.assertFalse(is_safe_category("Invoices/2024"))
        self.assertFalse(is_safe_category(".."))

    def test_dedupe_collapses_case_insensitive_duplicates(self):
        self.assertEqual(
            dedupe_categories(["Invoices", "invoices", "INVOICES", "Receipts"]),
            ["Invoices", "Receipts"],  # first spelling wins, order kept
        )
        # unusable names are dropped rather than repaired
        self.assertEqual(dedupe_categories(["..", "", "Tenancy"]), ["Tenancy"])


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.root = build_sandbox()
        self.stub, self.proposer = register_stubs()
        self.manager = stub_manager()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_scan_moves_nothing(self):
        before = sorted(p.name for p in self.root.iterdir())
        self.manager.scan(str(self.root))
        after = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(before, after)

    def test_skips_dotfiles_symlinks_and_folders(self):
        plan = self.manager.scan(str(self.root))
        names = {i.filename for i in plan.items}
        self.assertNotIn(".hidden", names)
        self.assertNotIn("subfolder", names)
        self.assertNotIn("link_to_notes", names)
        self.assertNotIn("deep.txt", names)

    def test_rules_handle_the_easy_cases_without_delegating(self):
        plan = self.manager.scan(str(self.root))
        by_name = {i.filename: i for i in plan.items}
        self.assertEqual(by_name["photo.jpg"].decided_by, "rules")
        self.assertEqual(by_name["photo.jpg"].confidence, "high")
        self.assertFalse(by_name["photo.jpg"].needs_review)
        delegated_names = {r.filename for r in self.stub.seen}
        self.assertNotIn("photo.jpg", delegated_names)

    def test_only_unmatched_files_are_delegated(self):
        self.manager.scan(str(self.root))
        self.assertEqual(
            {r.filename for r in self.stub.seen},
            {"lease_agreement", "minutes.dat", "blob.xyz"},
        )

    def test_delegate_gets_snippet_and_allowed_categories(self):
        self.manager.scan(str(self.root))
        seen = {r.filename: r for r in self.stub.seen}
        self.assertIn("tenancy agreement", seen["lease_agreement"].snippet)
        self.assertFalse(seen["lease_agreement"].snippet_is_binary)
        self.assertTrue(seen["blob.xyz"].snippet_is_binary)
        self.assertIsNone(seen["blob.xyz"].snippet)
        # The vocabulary is whatever the proposer proposed this run, with the
        # fallback appended by the manager — never a fixed list.
        self.assertEqual(
            seen["blob.xyz"].allowed_categories,
            STUB_SCHEME_NAMES + [FALLBACK_CATEGORY],
        )

    def test_low_confidence_delegations_are_flagged_for_review(self):
        plan = self.manager.scan(str(self.root))
        by_name = {i.filename: i for i in plan.items}
        self.assertTrue(by_name["blob.xyz"].needs_review)      # low
        self.assertTrue(by_name["minutes.dat"].needs_review)   # medium
        self.assertFalse(by_name["lease_agreement"].needs_review)  # high
        self.assertEqual(
            {i.filename for i in plan.review_items},
            {"blob.xyz", "minutes.dat"},
        )

    def test_collision_gets_a_suffix_in_the_plan(self):
        plan = self.manager.scan(str(self.root))
        notes = next(i for i in plan.items if i.filename == "notes.md")
        self.assertTrue(notes.destination.endswith("Documents/notes_1.md"))


class SchemeProposalTests(unittest.TestCase):
    """The corpus-level pass: one call, the whole unplaceable set, and a
    vocabulary that then constrains every per-file decision."""

    def setUp(self):
        self.root = build_sandbox()
        self.stub, self.proposer = register_stubs()
        self.manager = stub_manager()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_proposer_is_called_exactly_once_with_every_unplaceable_file(self):
        self.manager.scan(str(self.root))
        # One call for the whole folder is the entire point: naming a category
        # per file in isolation is what produces Invoices/Bills/Receipts.
        self.assertEqual(len(self.proposer.calls), 1)
        self.assertEqual(
            {f.filename for f in self.proposer.calls[0].files},
            {"lease_agreement", "minutes.dat", "blob.xyz"},
        )

    def test_proposer_sees_the_same_snippets_the_manager_read(self):
        self.manager.scan(str(self.root))
        files = {f.filename: f for f in self.proposer.calls[0].files}
        self.assertIn("tenancy agreement", files["lease_agreement"].snippet)
        self.assertTrue(files["blob.xyz"].snippet_is_binary)
        self.assertIsNone(files["blob.xyz"].snippet)
        self.assertGreater(files["minutes.dat"].size_bytes, 0)

    def test_existing_categories_offer_rule_categories_and_real_folders(self):
        self.manager.scan(str(self.root))
        existing = self.proposer.calls[0].existing_categories
        # categories the rule pass is filling this run
        for name in ("Documents", "Images", "Spreadsheets", "Screenshots"):
            self.assertIn(name, existing, name)
        # a folder that already exists in the target, which the rule table
        # could never have produced — this is the reuse hint
        self.assertIn("Tax Returns", existing)
        # hidden folders are nobody's category
        self.assertNotIn(".cache", existing)

    def test_plan_records_the_proposal_and_its_reason(self):
        plan = self.manager.scan(str(self.root))
        self.assertEqual(plan.scheme_reason, STUB_REASON)
        self.assertEqual([c["name"] for c in plan.proposed_categories], STUB_SCHEME_NAMES)
        self.assertEqual(
            plan.proposed_categories[0]["description"], STUB_SCHEME[0][1]
        )
        self.assertTrue(plan.proposed_categories[0]["is_new"])

    def test_proposal_survives_a_plan_store_round_trip(self):
        plan = self.manager.scan(str(self.root))
        reloaded = PlanStore().load(plan.plan_id)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.scheme_reason, plan.scheme_reason)
        self.assertEqual(reloaded.proposed_categories, plan.proposed_categories)

    def test_nothing_to_delegate_means_no_proposal_call(self):
        rules_only = Path(tempfile.mkdtemp(prefix="organiser-rules-only-"))
        try:
            (rules_only / "a.jpg").write_bytes(b"x")
            (rules_only / "b.csv").write_text("a,b")
            plan = stub_manager().scan(str(rules_only))
            self.assertEqual(self.proposer.calls, [])
            self.assertEqual(plan.proposed_categories, [])
            self.assertTrue(all(i.action == "move" for i in plan.items))
        finally:
            shutil.rmtree(rules_only, ignore_errors=True)


class ClassifierVocabularyTests(unittest.TestCase):
    """The classifier is handed a vocabulary and is not allowed to leave it.

    This talks to the real ClassifierAgent through a fake *provider*, because
    the coercion is the agent's own defence in depth. Injecting at the provider
    seam also means these tests are provider-agnostic: they would pass
    unchanged against OpenAI or a local endpoint.
    """

    @staticmethod
    def _agent(category, confidence="high", reason="because", error=None):
        class FakeProvider:
            name = "fake"

            def available(self):
                return True, "fake provider"

            def complete_json(self, **kwargs):
                if error is not None:
                    raise ProviderError(error)
                return SimpleNamespace(
                    category=category, confidence=confidence, reason=reason
                )

        return ClassifierAgent(provider=FakeProvider())

    @staticmethod
    def _request(allowed):
        return DelegateRequest(
            filename="blob.xyz",
            snippet=None,
            snippet_is_binary=True,
            size_bytes=42,
            allowed_categories=allowed,
        )

    def test_a_category_inside_the_list_is_kept(self):
        agent = self._agent("Tenancy")
        decision = agent.decide(self._request(["Tenancy", FALLBACK_CATEGORY]))
        self.assertEqual(decision.category, "Tenancy")
        self.assertEqual(decision.confidence, "high")

    def test_a_category_outside_the_list_becomes_the_fallback(self):
        agent = self._agent("Invented Folder")
        decision = agent.decide(self._request(["Tenancy", FALLBACK_CATEGORY]))
        # never a new folder: coining categories is the proposer's job
        self.assertEqual(decision.category, FALLBACK_CATEGORY)
        self.assertEqual(decision.confidence, "low")

    def test_an_unknown_confidence_level_becomes_low(self):
        agent = self._agent("Tenancy", confidence="very sure")
        decision = agent.decide(self._request(["Tenancy", FALLBACK_CATEGORY]))
        self.assertEqual(decision.confidence, "low")

    def test_a_refusal_is_unavailability_not_a_guess(self):
        agent = self._agent("Tenancy", error="the model declined this request")
        with self.assertRaises(DelegateUnavailable):
            agent.decide(self._request(["Tenancy", FALLBACK_CATEGORY]))

    def test_a_provider_error_never_becomes_a_category(self):
        agent = self._agent("Tenancy", error="rate limited")
        with self.assertRaises(DelegateUnavailable):
            agent.decide(self._request(["Tenancy", FALLBACK_CATEGORY]))


class GracefulDegradationTests(unittest.TestCase):
    def setUp(self):
        self.root = build_sandbox()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _assert_unplaceable_left_alone(self, plan):
        by_name = {i.filename: i for i in plan.items}
        for name in ("lease_agreement", "minutes.dat", "blob.xyz"):
            self.assertEqual(by_name[name].action, "skip", name)
            self.assertIsNone(by_name[name].category, name)
            self.assertIsNone(by_name[name].destination, name)
        # the rule pass still did its job
        self.assertEqual(by_name["photo.jpg"].action, "move")
        self.assertEqual(by_name["photo.jpg"].category, "Images")
        return by_name

    # --- the classifier ----------------------------------------------------

    def test_unavailable_delegate_leaves_files_alone(self):
        register_stubs(classifier=StubClassifier(available=False, detail="no API key"))
        plan = stub_manager().scan(str(self.root))
        self._assert_unplaceable_left_alone(plan)

    def test_mid_run_outage_does_not_guess(self):
        register_stubs(classifier=StubClassifier(raises=True))
        plan = stub_manager().scan(str(self.root))
        by_name = {i.filename: i for i in plan.items}
        for name in ("lease_agreement", "minutes.dat", "blob.xyz"):
            self.assertEqual(by_name[name].action, "skip", name)
            self.assertIsNone(by_name[name].category, name)

    def test_missing_delegate_is_not_fatal(self):
        register_stubs()
        plan = Manager(delegate_name="does-not-exist", scheme_name="scheme-stub").scan(
            str(self.root)
        )
        self.assertTrue(any(i.action == "move" for i in plan.items))
        self.assertFalse(plan.classifier_available)

    # --- the scheme proposer -----------------------------------------------

    def test_unavailable_proposer_leaves_unplaceable_files_alone(self):
        classifier, proposer = register_stubs(
            proposer=StubSchemeProposer(available=False, detail="no API key")
        )
        plan = stub_manager().scan(str(self.root))
        self._assert_unplaceable_left_alone(plan)
        # no vocabulary means nothing to classify against, so we never ask
        self.assertEqual(proposer.calls, [])
        self.assertEqual(classifier.seen, [])
        self.assertEqual(plan.proposed_categories, [])

    def test_proposer_outage_does_not_invent_a_category(self):
        classifier, proposer = register_stubs(proposer=StubSchemeProposer(raises=True))
        plan = stub_manager().scan(str(self.root))
        self._assert_unplaceable_left_alone(plan)
        self.assertEqual(len(proposer.calls), 1)  # tried once, then gave up
        self.assertEqual(classifier.seen, [])
        self.assertEqual(plan.proposed_categories, [])

    def test_empty_scheme_leaves_unplaceable_files_alone(self):
        classifier, _ = register_stubs(proposer=StubSchemeProposer(categories=[]))
        plan = stub_manager().scan(str(self.root))
        self._assert_unplaceable_left_alone(plan)
        self.assertEqual(classifier.seen, [])
        self.assertEqual(plan.proposed_categories, [])

    def test_a_scheme_of_only_unusable_names_leaves_files_alone(self):
        classifier, _ = register_stubs(
            proposer=StubSchemeProposer(categories=[("..", "traversal"), ("", "empty")])
        )
        plan = stub_manager().scan(str(self.root))
        self._assert_unplaceable_left_alone(plan)
        self.assertEqual(classifier.seen, [])

    def test_missing_proposer_is_not_fatal(self):
        classifier, _ = register_stubs()
        plan = Manager(delegate_name="stub", scheme_name="nope").scan(str(self.root))
        self._assert_unplaceable_left_alone(plan)
        self.assertEqual(classifier.seen, [])
        # the classifier itself is fine; only the vocabulary is missing
        self.assertTrue(plan.classifier_available)

    def test_a_skipped_file_says_why(self):
        register_stubs(proposer=StubSchemeProposer(available=False, detail="no API key"))
        plan = stub_manager().scan(str(self.root))
        lease = next(i for i in plan.items if i.filename == "lease_agreement")
        self.assertIn("no API key", lease.reason)


class HostileCategoryNameTests(unittest.TestCase):
    """A proposed category name is model output that becomes a directory.
    These prove it cannot become a directory anywhere but inside the target.
    """

    def setUp(self):
        # An isolated parent so "did anything appear outside the target?" is
        # a question with an exact answer.
        self.parent = Path(tempfile.mkdtemp(prefix="organiser-escape-"))
        self.root = self.parent / "target"
        self.root.mkdir()
        (self.root / "photo.jpg").write_bytes(b"\xff\xd8\xff fake jpeg")
        (self.root / "lease_agreement").write_text("tenancy agreement text")

    def tearDown(self):
        shutil.rmtree(self.parent, ignore_errors=True)

    def test_a_traversing_proposal_is_sanitised_before_the_classifier_sees_it(self):
        classifier, _ = register_stubs(
            proposer=StubSchemeProposer(categories=[("../../escaped", "hostile")])
        )
        plan = stub_manager().scan(str(self.root))
        allowed = classifier.seen[0].allowed_categories
        for name in allowed:
            self.assertNotIn("..", name)
            self.assertNotIn("/", name)
        self.assertEqual(allowed, ["escaped", FALLBACK_CATEGORY])
        self.assertEqual([c["name"] for c in plan.proposed_categories], ["escaped"])

    def test_a_hostile_category_cannot_escape_the_target_on_apply(self):
        # Belt and braces: the classifier itself hands back the raw traversal
        # string, so this exercises the manager's own last-chance sanitising.
        register_stubs(
            classifier=StubClassifier(forced_category="../../escaped"),
            proposer=StubSchemeProposer(categories=[("../../escaped", "hostile")]),
        )
        manager = stub_manager()
        plan = manager.scan(str(self.root))
        result = manager.apply(plan.plan_id, confirm_first_apply=True)

        self.assertEqual(result.failed, [])
        target = self.root.resolve()
        for moved in result.moved:
            self.assertTrue(
                Path(moved["destination"]).resolve().is_relative_to(target),
                moved["destination"],
            )
        # nothing at all was created beside the target folder
        self.assertEqual([p.name for p in self.parent.iterdir()], ["target"])
        self.assertFalse((self.parent / "escaped").exists())
        # The raw traversal string is not in the vocabulary the manager handed
        # out, so it is rejected as out-of-scheme before the sanitised
        # "escaped" folder is ever reached — the file goes to the fallback.
        self.assertTrue((self.root / FALLBACK_CATEGORY / "lease_agreement").is_file())
        self.assertFalse((self.root / "escaped").exists())

    def test_a_category_outside_the_proposed_scheme_is_refused(self):
        # The manager, not the delegate, owns the vocabulary. A delegate that
        # answers outside the list it was given cannot widen the scheme by
        # inventing a folder — the file is filed under the fallback and
        # flagged for review instead.
        register_stubs(
            classifier=StubClassifier(forced_category=".."),
            proposer=StubSchemeProposer(categories=[("Tenancy", "leases")]),
        )
        plan = stub_manager().scan(str(self.root))
        lease = next(i for i in plan.items if i.filename == "lease_agreement")
        self.assertEqual(lease.action, "move")
        self.assertEqual(lease.category, FALLBACK_CATEGORY)
        self.assertEqual(lease.confidence, "low")
        self.assertTrue(lease.needs_review)
        self.assertIn("not one of this run's categories", lease.reason)
        self.assertNotIn("Tenancy", str(lease.destination))

    def test_an_invented_category_cannot_create_a_folder(self):
        register_stubs(
            classifier=StubClassifier(forced_category="Totally Invented"),
            proposer=StubSchemeProposer(categories=[("Tenancy", "leases")]),
        )
        manager = stub_manager()
        plan = manager.scan(str(self.root))
        manager.apply(plan.plan_id, confirm_first_apply=True)
        self.assertFalse((self.root / "Totally Invented").exists())
        self.assertTrue((self.root / FALLBACK_CATEGORY / "lease_agreement").is_file())


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.root = build_sandbox()
        self.stub, self.proposer = register_stubs()
        self.manager = stub_manager()
        self.plan = self.manager.scan(str(self.root))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_first_apply_on_a_new_folder_requires_confirmation(self):
        with self.assertRaises(FirstRunConfirmationRequired):
            self.manager.apply(self.plan.plan_id)
        # and nothing moved
        self.assertTrue((self.root / "photo.jpg").exists())

    def test_confirmed_apply_moves_files(self):
        result = self.manager.apply(self.plan.plan_id, confirm_first_apply=True)
        self.assertGreater(len(result.moved), 0)
        self.assertEqual(result.failed, [])
        self.assertTrue((self.root / "Images" / "photo.jpg").exists())
        self.assertTrue((self.root / "Screenshots" / "Screenshot 2026-08-19.png").exists())
        self.assertFalse((self.root / "photo.jpg").exists())

    def test_delegated_files_land_in_the_proposed_categories(self):
        self.manager.apply(self.plan.plan_id, confirm_first_apply=True)
        # the proposal's first name, chosen by the stub classifier
        self.assertTrue((self.root / STUB_SCHEME_NAMES[0] / "lease_agreement").is_file())
        # nothing invented a folder outside proposal + fallback + rule names
        allowed = set(STUB_SCHEME_NAMES) | {FALLBACK_CATEGORY}
        allowed |= {"Images", "Screenshots", "Spreadsheets", "Documents"}
        allowed |= {"subfolder", "Tax Returns", ".cache"}
        created = {p.name for p in self.root.iterdir() if p.is_dir()}
        self.assertEqual(created - allowed, set())

    def test_apply_never_overwrites(self):
        original = (self.root / "Documents" / "notes.md").read_text()
        self.manager.apply(self.plan.plan_id, confirm_first_apply=True)
        self.assertEqual((self.root / "Documents" / "notes.md").read_text(), original)
        self.assertTrue((self.root / "Documents" / "notes_1.md").exists())

    def test_apply_leaves_subfolders_dotfiles_and_symlinks_alone(self):
        self.manager.apply(self.plan.plan_id, confirm_first_apply=True)
        self.assertTrue((self.root / "subfolder" / "deep.txt").exists())
        self.assertTrue((self.root / ".hidden").exists())
        self.assertTrue((self.root / "link_to_notes").is_symlink())

    def test_partial_approval_only_moves_approved_items(self):
        photo = next(i for i in self.plan.items if i.filename == "photo.jpg")
        result = self.manager.apply(
            self.plan.plan_id, approved_ids=[photo.item_id], confirm_first_apply=True
        )
        self.assertEqual([m["filename"] for m in result.moved], ["photo.jpg"])
        self.assertTrue((self.root / "budget.csv").exists())  # not approved, untouched

    def test_every_move_is_logged(self):
        result = self.manager.apply(self.plan.plan_id, confirm_first_apply=True)
        entries = undo_log.read_log(limit=500, plan_id=self.plan.plan_id)
        self.assertEqual(len(entries), len(result.moved))
        entry = entries[0]
        for key in ("timestamp", "source", "destination", "category", "decided_by", "confidence"):
            self.assertIn(key, entry)
        # the log is enough to reverse the move by hand
        self.assertTrue(Path(entry["destination"]).exists())

    def test_nothing_is_ever_deleted(self):
        # Compare real files by basename. Symlinks are excluded on purpose:
        # moving a file leaves any symlink pointing at it dangling, which is
        # unavoidable and is not a deletion — the link itself is untouched
        # (asserted separately in test_apply_leaves_subfolders_dotfiles_...).
        def real_files():
            return sorted(
                p.name for p in self.root.rglob("*") if p.is_file() and not p.is_symlink()
            )

        before = real_files()
        self.manager.apply(self.plan.plan_id, confirm_first_apply=True)
        after = real_files()
        # notes.md collides with an existing Documents/notes.md and is renamed,
        # so compare counts plus the survival of every original name.
        self.assertEqual(len(before), len(after))
        self.assertEqual(
            len([n for n in after if n.startswith("notes")]),
            len([n for n in before if n.startswith("notes")]),
        )

    def test_second_apply_on_same_folder_needs_no_confirmation(self):
        self.manager.apply(self.plan.plan_id, confirm_first_apply=True)
        second = self.manager.scan(str(self.root))
        self.assertFalse(second.is_first_run)
        self.manager.apply(second.plan_id)  # must not raise


class RulesToggleTests(unittest.TestCase):
    """`use_rules=False` sends every file to the agents instead of the table."""

    def setUp(self):
        self.root = build_sandbox()
        self.classifier, self.proposer = register_stubs()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_rules_on_is_the_default(self):
        plan = stub_manager().scan(str(self.root))
        self.assertTrue(plan.used_rules)
        self.assertGreater(plan.summary()["by_agent"].get("rules", 0), 0)

    def test_rules_off_sends_everything_to_the_agents(self):
        plan = stub_manager().scan(str(self.root), use_rules=False)
        self.assertFalse(plan.used_rules)
        # not one decision came from the table
        self.assertEqual(plan.summary()["by_agent"].get("rules", 0), 0)
        self.assertTrue(all(i.decided_by != "rules" for i in plan.items))

    def test_rules_off_shows_the_whole_folder_to_the_proposer(self):
        plan = stub_manager().scan(str(self.root), use_rules=False)
        seen = {f.filename for f in self.proposer.calls[0].files}
        planned = {i.filename for i in plan.items}
        self.assertEqual(seen, planned)
        # still exactly one corpus-level call, however many files there are
        self.assertEqual(len(self.proposer.calls), 1)

    def test_rules_off_still_skips_dotfiles_symlinks_and_folders(self):
        # The toggle widens what gets classified, never what gets touched.
        plan = stub_manager().scan(str(self.root), use_rules=False)
        names = {i.filename for i in plan.items}
        for excluded in (".hidden", "subfolder", "link_to_notes", "deep.txt"):
            self.assertNotIn(excluded, names)

    def test_rules_off_with_no_agent_organises_nothing(self):
        # The degenerate case worth warning about: with rules on, the easy
        # files would still have been placed.
        register_stubs(classifier=StubClassifier(available=False, detail="no key"))
        plan = stub_manager().scan(str(self.root), use_rules=False)
        self.assertEqual(plan.moves, [])
        self.assertTrue(all(i.action == "skip" for i in plan.items))

    def test_used_rules_survives_a_store_round_trip(self):
        plan = stub_manager().scan(str(self.root), use_rules=False)
        reloaded = PlanStore().load(plan.plan_id)
        self.assertFalse(reloaded.used_rules)
        self.assertIs(plan.to_dict()["used_rules"], False)

    def test_rules_off_still_applies_safely(self):
        manager = stub_manager()
        plan = manager.scan(str(self.root), use_rules=False)
        result = manager.apply(plan.plan_id, confirm_first_apply=True)
        self.assertEqual(result.failed, [])
        # the pre-existing file is untouched and nothing escaped the target
        self.assertEqual((self.root / "Documents" / "notes.md").read_text(),
                         "older file, must survive")
        for moved in result.moved:
            self.assertTrue(
                Path(moved["destination"]).resolve().is_relative_to(self.root.resolve())
            )


class ConfigTests(unittest.TestCase):
    """Provider/model resolution: env var beats config file beats default."""

    # Every env var config.py looks at, cleared so the host machine's own
    # environment can never change the outcome of these tests.
    ENV_KEYS = [
        "FILE_ORGANISER_PROVIDER",
        "FILE_ORGANISER_BASE_URL",
        "FILE_ORGANISER_SCHEME_MODEL",
        "FILE_ORGANISER_CLASSIFIER_MODEL",
        "FILE_ORGANISER_SCHEME_EFFORT",
        "FILE_ORGANISER_CLASSIFIER_EFFORT",
    ]

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in self.ENV_KEYS}
        if config_path().exists():
            config_path().unlink()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if config_path().exists():
            config_path().unlink()

    @staticmethod
    def _write(data: dict):
        config_path().write_text(json.dumps(data))

    def test_defaults_with_no_config_file(self):
        settings = load_settings()
        self.assertEqual(settings.provider, "anthropic")
        self.assertEqual(settings.classifier.model, DEFAULT_MODELS["anthropic"])
        self.assertEqual(settings.sources["provider"], "default")

    def test_config_file_is_read(self):
        self._write({"provider": "openai", "classifier": {"model": "some-model"}})
        settings = load_settings()
        self.assertEqual(settings.provider, "openai")
        self.assertEqual(settings.classifier.model, "some-model")
        self.assertEqual(settings.sources["classifier.model"], "config file")

    def test_environment_beats_the_config_file(self):
        self._write({"provider": "openai", "classifier": {"model": "some-model"}})
        os.environ["FILE_ORGANISER_CLASSIFIER_MODEL"] = "override-model"
        settings = load_settings()
        self.assertEqual(settings.classifier.model, "override-model")
        self.assertEqual(settings.sources["classifier.model"], "environment")

    def test_switching_provider_drops_the_files_model_names(self):
        # Otherwise an OpenAI model id would be sent to Anthropic and fail
        # with a confusing 404.
        self._write({"provider": "openai", "classifier": {"model": "an-openai-model"}})
        os.environ["FILE_ORGANISER_PROVIDER"] = "anthropic"
        settings = load_settings()
        self.assertEqual(settings.provider, "anthropic")
        self.assertEqual(settings.classifier.model, DEFAULT_MODELS["anthropic"])

    def test_an_explicit_model_override_still_wins_when_switching_provider(self):
        self._write({"provider": "openai", "classifier": {"model": "an-openai-model"}})
        os.environ["FILE_ORGANISER_PROVIDER"] = "anthropic"
        os.environ["FILE_ORGANISER_CLASSIFIER_MODEL"] = "chosen-explicitly"
        self.assertEqual(load_settings().classifier.model, "chosen-explicitly")

    def test_per_agent_models_are_independent(self):
        self._write(
            {
                "provider": "openai",
                "scheme": {"model": "big-model"},
                "classifier": {"model": "small-model"},
            }
        )
        settings = load_settings()
        self.assertEqual(settings.scheme.model, "big-model")
        self.assertEqual(settings.classifier.model, "small-model")

    def test_non_anthropic_providers_have_no_default_model(self):
        self._write({"provider": "openai"})
        self.assertIsNone(load_settings().classifier.model)

    def test_compatible_without_a_base_url_is_an_error(self):
        self._write({"provider": "compatible", "classifier": {"model": "llama"}})
        with self.assertRaises(ConfigError):
            load_settings()

    def test_compatible_with_a_base_url_resolves(self):
        self._write(
            {
                "provider": "compatible",
                "base_url": "http://localhost:11434/v1",
                "classifier": {"model": "llama"},
            }
        )
        settings = load_settings()
        self.assertEqual(settings.base_url, "http://localhost:11434/v1")

    def test_an_unknown_provider_is_an_error(self):
        self._write({"provider": "not-a-provider"})
        with self.assertRaises(ConfigError):
            load_settings()

    def test_malformed_json_is_an_error_not_a_crash(self):
        config_path().write_text("{not json")
        with self.assertRaises(ConfigError):
            load_settings()

    def test_a_missing_model_makes_the_agent_unavailable_not_a_guess(self):
        self._write({"provider": "openai"})
        ok, detail = ClassifierAgent().available()
        self.assertFalse(ok)
        self.assertIn("no model configured", detail)

    def test_config_never_carries_an_api_key(self):
        # Keys belong in the provider's env var; the state dir must not hold
        # a secret. Guard against anyone "helpfully" adding one later.
        self._write({"provider": "openai", "classifier": {"model": "m"}})
        self.assertNotIn("api_key", load_settings().to_dict())


if __name__ == "__main__":
    unittest.main()
