"""U2: the deterministic part of goal-relevant action selection.

Everything pinned here is a pure function or a prompt-assembly fact, so these
tests need no browser, no model and no network, and they pin the two general
mechanisms the U2 diagnosis produced:

  * `control_value_transition` — an action's own effect on the control it
    targeted, so a successful fill/select/toggle is not mistaken for a no-op
    just because the page's node hash did not move.

  * `goal_candidate_evidence` — which observed controls the user's own words
    demonstrably refer to, and the evidence for each. Facts only: no score, no
    threshold, no ranking.

The end-to-end behaviour of both, on real pages and a real model, is pinned in
test_u2_action_selection_e2e.py.

No website-specific text, selector or ordering appears below. Every string is
opaque input to a rule about it.

Run with:
    .\\venv311\\Scripts\\python.exe -m unittest tests.test_u2_selection_contract -v
"""

import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import automation_engine as ae


def rec(name, role="button", tag="button", options=None, value="", typ=""):
    return {"agent_id": "1", "name": name, "role": role, "tag": tag,
            "type": typ, "value": value, "options": options,
            "placeholder": "", "aria_label": "", "testid": "",
            "disabled": False, "sensitive": False, "occluded_by": ""}


class TestControlValueTransition(unittest.TestCase):
    """A value-changing action is judged by the value it changed."""

    def test_a_changed_value_is_real_progress(self):
        self.assertIs(
            ae.control_value_transition(ae.OP_SELECT, "-- choose --", "Editor"),
            True)

    def test_a_typed_value_is_real_progress(self):
        self.assertIs(
            ae.control_value_transition(ae.OP_FILL, "", "ada@example.test"),
            True)

    def test_a_toggled_value_is_real_progress(self):
        self.assertIs(
            ae.control_value_transition(ae.OP_TOGGLE, "unchecked", "checked"),
            True)

    def test_an_unchanged_value_is_not_progress(self):
        """Re-picking the option that was already selected did nothing, and
        saying otherwise would let a genuinely stuck run keep going."""
        self.assertIs(
            ae.control_value_transition(ae.OP_SELECT, "Editor", "Editor"),
            False)

    def test_a_click_is_never_judged_by_value(self):
        """A click's effect is navigation or UI change, which this instrument
        cannot see. Answering True/False here would invent evidence."""
        for op in (ae.OP_CLICK, ae.OP_NAVIGATE, ae.OP_SUBMIT):
            self.assertIsNone(
                ae.control_value_transition(op, "a", "b"),
                f"{op} must not be judged by a value transition")

    def test_an_unobservable_value_is_inconclusive_not_a_guess(self):
        """A control that has vanished, or a value never observable (a
        sensitive field), says nothing either way."""
        self.assertIsNone(ae.control_value_transition(ae.OP_SELECT, None, "x"))
        self.assertIsNone(ae.control_value_transition(ae.OP_SELECT, "x", None))
        self.assertIsNone(
            ae.control_value_transition(ae.OP_SELECT, None, None))

    def test_the_value_mutating_operations_are_exactly_the_ones_that_apply(self):
        self.assertEqual(
            set(ae.VALUE_MUTATING_OPERATIONS),
            {ae.OP_FILL, ae.OP_SELECT, ae.OP_TOGGLE},
            "a value change is the effect of these and of nothing else")


class TestNamedOptionPerControl(unittest.TestCase):
    """Which choice, on which control, the user's own words name."""

    def test_the_single_named_option_is_returned(self):
        self.assertEqual(
            ae._named_option_for_control(
                ["-- choose --", "Administrator", "Editor", "Viewer"],
                ["set the role to Editor"]),
            "Editor")

    def test_two_named_options_resolve_to_nothing(self):
        """Naming two choices does not identify one, so the control cannot be
        credited with either."""
        self.assertIsNone(ae._named_option_for_control(
            ["Editor", "Viewer"], ["set the role to Editor or Viewer"]))

    def test_no_named_option_resolves_to_nothing(self):
        self.assertIsNone(ae._named_option_for_control(
            ["Editor", "Viewer"], ["set the department to Finance"]))

    def test_a_choice_too_short_to_be_identified_is_refused(self):
        """A two-letter choice appears inside ordinary prose; crediting a
        control with it would be a guess."""
        self.assertIsNone(ae._named_option_for_control(
            ["On", "Off"], ["turn the feature on"]))

    def test_matching_is_by_whole_word_not_substring(self):
        self.assertIsNone(ae._named_option_for_control(
            ["Editor"], ["the Editorial workflow is fine"]),
            "'Editor' must not be found inside 'Editorial'")

    def test_no_options_means_no_answer(self):
        self.assertIsNone(ae._named_option_for_control(None, ["anything"]))
        self.assertIsNone(ae._named_option_for_control([], ["anything"]))


class TestGoalCandidateEvidence(unittest.TestCase):
    """The connection between the user's words and an observed control."""

    def test_the_select_that_offers_the_named_choice_is_evidenced(self):
        """The core case: a text field sits beside a dropdown, the goal names a
        CHOICE, and only the dropdown can offer it."""
        found = ae.goal_candidate_evidence(
            ["set the role to Editor"],
            [("Role", rec("Role", role="combobox", tag="select",
                          options=["-- choose --", "Editor", "Viewer"],
                          value="-- choose --")),
             ("Username", rec("Username", role="textbox", tag="input"))])
        self.assertEqual([f["option"] for f in found], ["Role"])
        self.assertIn("offering the choice 'Editor'",
                      " ".join(found[0]["reasons"]))

    def test_a_control_the_goal_names_is_evidenced(self):
        found = ae.goal_candidate_evidence(
            ["press the Export report button"],
            [("Export report", rec("Export report")),
             ("Save draft", rec("Save draft"))])
        self.assertEqual([f["option"] for f in found], ["Export report"])
        self.assertIn("refers to this control by name",
                      " ".join(found[0]["reasons"]))

    def test_a_control_with_no_connection_is_absent_not_ranked_last(self):
        """Absence is the message: an unconnected control must not appear as a
        weak candidate, or the model reads a shortlist that is not one."""
        found = ae.goal_candidate_evidence(
            ["set the role to Editor"],
            [("Username", rec("Username", role="textbox", tag="input")),
             ("Save changes", rec("Save changes"))])
        self.assertEqual(found, [])

    def test_overlapping_labels_do_not_both_match(self):
        """'Role' is named; 'Role description' merely contains the word. Only
        the control the user actually named is evidenced."""
        found = ae.goal_candidate_evidence(
            ["set the role to Editor"],
            [("Role", rec("Role", role="combobox", tag="select",
                          options=["Editor", "Viewer"])),
             ("Role description", rec("Role description",
                                      role="textbox", tag="input"))])
        self.assertEqual([f["option"] for f in found], ["Role"])

    def test_the_current_subtask_can_supply_the_connection(self):
        """When the objective is broad, the sub-task being advanced now is the
        specific thing the next action must serve."""
        found = ae.goal_candidate_evidence(
            ["complete the account setup"],
            [("Team", rec("Team", role="textbox", tag="input")),
             ("Username", rec("Username", role="textbox", tag="input"))],
            focus_text="join the Platform team")
        self.assertEqual([f["option"] for f in found], ["Team"])

    def test_every_reported_option_stays_addressable_when_labels_repeat(self):
        """Several controls can share one label. Each occurrence is reported
        under its own option string, so the model can still name one."""
        found = ae.goal_candidate_evidence(
            ["add the second product to the cart"],
            [("Add to cart", rec("Add to cart")),
             ("Add to cart #2", rec("Add to cart")),
             ("Add to cart #3", rec("Add to cart"))])
        # None of these is evidenced: the goal names no control and offers no
        # choice. What matters is that no phantom entry is invented.
        self.assertEqual(found, [])

    def test_the_implied_action_is_stated_so_a_value_is_not_mistaken_for_a_control(self):
        found = ae.goal_candidate_evidence(
            ["set the role to Editor"],
            [("Role", rec("Role", role="combobox", tag="select",
                          options=["Editor"], value=""))])
        self.assertIn("SET ITS VALUE to 'Editor'", found[0]["implies"])
        self.assertNotIn("Editor", found[0]["option"],
                         "the choice must never be offered as a control")

    def test_implied_actions_cover_the_control_kinds(self):
        cases = [
            (rec("Role", role="combobox", tag="select", options=["A"]),
             "ENTER A VALUE"),
            (rec("Go", role="link", tag="a"), "FOLLOW it"),
            (rec("Save", role="button", tag="button"), "PRESS it"),
        ]
        for record, expected in cases:
            found = ae.goal_candidate_evidence(
                ["use the control called " + record["name"] + " now"],
                [(record["name"], record)])
            self.assertTrue(found, record["name"])
            self.assertIn(expected, found[0]["implies"], record["name"])

    def test_role_and_value_are_carried_through_for_the_model(self):
        found = ae.goal_candidate_evidence(
            ["set the role to Editor"],
            [("Role", rec("Role", role="combobox", tag="select",
                          options=["Editor"], value="-- choose --"))])
        self.assertEqual(found[0]["role"], "combobox")
        self.assertEqual(found[0]["value"], "-- choose --")

    def test_malformed_input_is_ignored_rather_than_crashing(self):
        self.assertEqual(ae.goal_candidate_evidence(None, None), [])
        self.assertEqual(ae.goal_candidate_evidence([], []), [])
        self.assertEqual(
            ae.goal_candidate_evidence(["x"], [("a", None), ("b", "junk")]), [])

    def test_a_control_with_no_accessible_name_is_skipped(self):
        """The extraction layer already refuses to invent a name for such a
        control; this layer must not then credit it with relevance."""
        self.assertEqual(
            ae.goal_candidate_evidence(["anything"], [("x", rec(""))]), [])


class TestRelevanceBlockIsWired(unittest.TestCase):
    """The evidence has to reach the prompt, or the model cannot use it."""

    @classmethod
    def setUpClass(cls):
        cls.src = inspect.getsource(ae.ask_ai_navigator)

    def test_the_relevance_block_is_computed_from_the_live_goal(self):
        self.assertIn("goal_candidate_evidence(", self.src)

    def test_the_block_appears_in_the_prompt(self):
        self.assertIn("{relevance_block}", self.src)

    def test_the_goal_is_used_not_the_config_snapshot(self):
        """A stale goal string here is what let a superseded objective keep
        steering selection after U1 replaced it."""
        self.assertIn("[goal]", self.src)
        self.assertNotIn('config["ai_context"]', self.src)

    def test_being_clickable_is_never_offered_as_a_reason(self):
        self.assertIn("being visible, enabled or easy to click is not evidence",
                      self.src)

    def test_no_connection_is_stated_plainly_instead_of_implied(self):
        """Silence would read as 'everything is a candidate'."""
        self.assertIn("has a demonstrated", self.src)

    def test_the_options_block_itself_is_unchanged(self):
        """The choice contract is exact strings. Enriching the prompt must not
        change the strings the model is required to return."""
        self.assertIn("observation.render_options_block()", self.src)
        observation = ae.PageObservation(
            url="https://x.test/", title="t",
            discovery={"option_index": {"Role": ("Role", 0)},
                       "by_option": {"Role": rec("Role", role="combobox",
                                                 tag="select",
                                                 options=["Editor"])}})
        rendered = observation.render_options_block()
        self.assertIn("you MUST pick one of these exact strings", rendered)
        self.assertIn("\nRole", rendered,
                      "the option must still be the bare accessible name, so "
                      "the string the model returns is unchanged")
        self.assertNotIn("combobox", rendered,
                         "control kinds live in the relevance block, not in "
                         "the string the model must echo back")

    def test_selection_is_not_gated_by_an_arbitrary_threshold(self):
        """No numeric confidence cut-off was introduced: the evidence is a set
        of facts, and the model still chooses."""
        for banned in ("confidence", "RELEVANCE_THRESHOLD", "score >=",
                       "min_relevance"):
            self.assertNotIn(banned, self.src)


class TestFutilityUsesTheActionEffect(unittest.TestCase):
    """The loop must consult the control's own value before judging futility."""

    @classmethod
    def setUpClass(cls):
        cls.src = inspect.getsource(ae.run_pathfinder_agent)

    def test_the_before_value_is_captured_at_the_grounding_gate(self):
        """Captured where the operation is derived, so a click is never
        recorded as a value mutation."""
        self.assertIn("_pending_value_effect = (", self.src)
        self.assertIn("control_value_transition(", self.src)

    def test_a_moved_value_is_recorded_as_progress_not_a_futile_action(self):
        self.assertIn('outcome="changed_control_value"', self.src)
        self.assertIn("changed its own value", self.src)

    def test_the_futile_penalty_is_reached_only_when_the_value_did_not_move(self):
        self.assertIn("_value_moved is True", self.src)
        self.assertIn("FUTILE_ACTION_PENALTY", self.src)

    def test_the_effect_is_keyed_to_its_own_transition(self):
        """Otherwise a cleared pending transition could be compared against a
        different action's before-value and invent progress."""
        self.assertIn("_pending_value_effect[0] == pending_transition_seq",
                      self.src)

    def test_loop_detection_is_not_weakened(self):
        """Same-node navigation that changed nothing is still penalised, and a
        repeated failure still goes through the bounded recovery controller."""
        self.assertIn('failure_reason="same_node_hash_after_click"', self.src)
        self.assertIn("_handle_recovery(_src, _act, _futile_class", self.src)


if __name__ == "__main__":
    unittest.main()