"""Checkpoint 2.2 — grounded action contract.

The gate under test is deliberately pure: given an intent, an observation and
a locator's provenance, `validate_action_grounding` must return one
deterministic verdict, with no model call, no I/O and no site knowledge. These
tests pin that, and then check it live — proving that an action which cannot
be grounded genuinely does not reach the browser.
"""

import asyncio
import gc
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automation_engine as ae

from tests.test_contract_observation import discovery, rec


def obs_with(records, **kw):
    return ae.PageObservation(url="https://x.test/p", discovery=discovery(records, **kw))


def intent_for(name, record, *, operation=None, observation_id=1,
               provenance=ae.RESOLVE_GROUNDED, required_input=None, **kw):
    return ae.ActionIntent(
        operation=operation or ae.operation_for_record(record),
        target_name=name,
        target_agent_id=record.get("agent_id") if record else None,
        observation_id=observation_id,
        provenance=provenance,
        record=record or {},
        required_input=required_input,
        **kw,
    )


# ===========================================================================
# Operation derivation: mechanical, not asked of the model
# ===========================================================================

class TestOperationDerivation(unittest.TestCase):

    def test_plain_button_is_a_click(self):
        self.assertEqual(
            ae.operation_for_record(rec("Next", role="button")), ae.OP_CLICK)

    def test_submit_button_is_a_submit(self):
        r = rec("Go", role="button")
        r["type"] = "submit"
        self.assertEqual(ae.operation_for_record(r), ae.OP_SUBMIT)

    def test_link_is_a_navigation(self):
        r = rec("Home", role="link")
        r["tag"] = "a"
        self.assertEqual(ae.operation_for_record(r), ae.OP_NAVIGATE)

    def test_textbox_is_a_fill(self):
        r = rec("Email", role="textbox")
        r["tag"] = "input"
        self.assertEqual(ae.operation_for_record(r), ae.OP_FILL)

    def test_select_is_a_select_not_a_click(self):
        r = rec("Size", role="combobox")
        r["tag"] = "select"
        self.assertEqual(ae.operation_for_record(r), ae.OP_SELECT)

    def test_checkbox_is_a_toggle(self):
        r = rec("Gift", role="checkbox")
        r["tag"] = "input"
        r["type"] = "checkbox"
        self.assertEqual(ae.operation_for_record(r), ae.OP_TOGGLE)

    def test_unknown_record_derives_nothing_rather_than_guessing(self):
        self.assertIsNone(ae.operation_for_record(None))


# ===========================================================================
# Intent self-description
# ===========================================================================

class TestActionIntent(unittest.TestCase):

    def test_intent_carries_the_observation_it_was_issued_against(self):
        i = intent_for("Next", rec("Next", agent_id="1"))
        self.assertEqual(i.observation_id, 1)
        self.assertIn("observation=1", i.describe())

    def test_intent_records_how_the_target_was_found(self):
        i = intent_for("Next", rec("Next", agent_id="1"), provenance=ae.RESOLVE_TEXT_GUESS)
        self.assertIn("resolution=text_guess", i.describe())

    def test_expected_effect_is_absent_rather_than_invented(self):
        """A system that must not fabricate success cannot invent an outcome
        and then report the match."""
        i = intent_for("Next", rec("Next", agent_id="1"))
        self.assertIsNone(i.expected_effect)

    def test_sensitive_input_is_not_echoed_in_the_description(self):
        r = rec("Password", agent_id="1", role="textbox", sensitive=True)
        i = intent_for("Password", r, operation=ae.OP_FILL,
                       required_input="hunter2")
        self.assertNotIn("hunter2", i.describe())
        self.assertIn("<redacted>", i.describe())

    def test_unclassified_action_defaults_to_consequential(self):
        """Failing to classify must fail safe, not open."""
        i = intent_for("Next", rec("Next", agent_id="1"))
        self.assertTrue(i.consequential)
        self.assertTrue(i.requires_confirmation)


# ===========================================================================
# The grounding gate
# ===========================================================================

class TestGroundingGate(unittest.TestCase):

    def test_grounded_actionable_action_is_allowed(self):
        obs = obs_with([rec("Next", agent_id="1")])
        self.assertEqual(
            ae.validate_action_grounding(intent_for("Next", obs.discovery["by_label"]["Next"]), obs),
            ae.GROUND_OK)

    # -- targets that were never observed ---------------------------------

    def test_target_not_in_the_observation_is_rejected(self):
        obs = obs_with([rec("Next", agent_id="1")])
        code = ae.validate_action_grounding(intent_for("Place Order", None), obs)
        self.assertEqual(code, ae.GROUND_TARGET_NOT_OBSERVED)

    def test_missing_target_rejection_explains_itself(self):
        msg = ae.grounding_message(ae.GROUND_TARGET_NOT_OBSERVED)
        self.assertIn("not present in the latest observation", msg)

    # -- duplicate controls -----------------------------------------------

    def test_duplicate_label_is_rejected_as_ambiguous(self):
        obs = obs_with([rec("Add", agent_id="1"), rec("Add", agent_id="2")])
        first = obs.discovery["by_label"]["Add"]
        code = ae.validate_action_grounding(intent_for("Add", first), obs)
        self.assertEqual(code, ae.GROUND_TARGET_AMBIGUOUS)

    def test_ambiguity_can_be_overridden_only_explicitly(self):
        obs = obs_with([rec("Add", agent_id="1"), rec("Add", agent_id="2")])
        first = obs.discovery["by_label"]["Add"]
        i = intent_for("Add", first)
        self.assertEqual(
            ae.validate_action_grounding(i, obs, allow_ambiguous=True),
            ae.GROUND_OK,
            "disambiguation must be an explicit decision, never a default")

    # -- duplicates are addressable, not merely refused -------------------

    def test_each_occurrence_is_offered_as_its_own_option(self):
        obs = obs_with([rec("Add", agent_id="1"), rec("Add", agent_id="2"),
                        rec("Add", agent_id="3")])
        self.assertIn("Add #2", obs.options)
        self.assertIn("Add #3", obs.options,
                      "every occurrence must be individually selectable")

    def test_indexed_option_is_actionable_while_bare_label_is_ambiguous(self):
        obs = obs_with([rec("Add", agent_id="1"), rec("Add", agent_id="2")])
        self.assertEqual(obs.status_of("Add"), ae.OBS_AMBIGUOUS)
        self.assertEqual(obs.status_of("Add #2"), ae.OBS_ACTIONABLE)

    def test_indexed_option_resolves_to_its_own_record(self):
        obs = obs_with([rec("Add", agent_id="1"), rec("Add", agent_id="2")])
        self.assertEqual(obs.record_for("Add")["agent_id"], "1")
        self.assertEqual(obs.record_for("Add #2")["agent_id"], "2",
                         "each option must address its own control")

    def test_indexed_option_passes_the_gate(self):
        obs = obs_with([rec("Add", agent_id="1"), rec("Add", agent_id="2")])
        i = intent_for("Add #2", obs.record_for("Add #2"))
        self.assertEqual(ae.validate_action_grounding(i, obs), ae.GROUND_OK)

    def test_indexed_option_reports_its_own_disabled_state(self):
        obs = obs_with([rec("Add", agent_id="1"), rec("Add", agent_id="2"),
                        rec("Add", agent_id="3", disabled=True)])
        self.assertEqual(obs.status_of("Add #3"), ae.OBS_DISABLED,
                         "status must come from that occurrence, not the label")
        self.assertEqual(obs.status_of("Add #2"), ae.OBS_ACTIONABLE,
                         "one disabled occurrence must not disable them all")

    def test_mechanically_proven_safety_reaches_every_occurrence(self):
        """Numbering a control must not become a way to shed its safety tag."""
        obs = obs_with([rec("Docs", agent_id="1"), rec("Docs", agent_id="2")],
                       mechanical_safety_tags={"Docs": ae.SAFETY_EXTERNAL_SITE})
        tags = obs.discovery["mechanical_safety_tags"]
        self.assertEqual(tags.get("Docs"), ae.SAFETY_EXTERNAL_SITE)
        self.assertEqual(tags.get("Docs #2"), ae.SAFETY_EXTERNAL_SITE)

    def test_option_list_explains_the_numbering(self):
        obs = obs_with([rec("Add", agent_id="1"), rec("Add", agent_id="2")])
        block = obs.render_options_block()
        self.assertIn("Add #2", block)
        self.assertIn("2 controls", block)

    # -- disabled / hidden / obscured -------------------------------------

    def test_disabled_target_is_rejected(self):
        obs = obs_with([rec("Pay", agent_id="1", disabled=True)])
        code = ae.validate_action_grounding(
            intent_for("Pay", obs.discovery["by_label"]["Pay"]), obs)
        self.assertEqual(code, ae.GROUND_TARGET_DISABLED)

    def test_hidden_target_is_rejected(self):
        hidden = [{"role": "button", "name": "Finish", "tag": "button",
                   "disabled": False}]
        obs = obs_with([], hidden=hidden, hidden_count=1)
        code = ae.validate_action_grounding(intent_for("Finish", None), obs)
        self.assertEqual(code, ae.GROUND_TARGET_HIDDEN)

    def test_obscured_target_is_rejected(self):
        obs = obs_with([rec("Buy", agent_id="1", occluded_by="Sidebar")])
        code = ae.validate_action_grounding(
            intent_for("Buy", obs.discovery["by_label"]["Buy"]), obs)
        self.assertEqual(code, ae.GROUND_TARGET_OBSCURED)

    # -- stale references ---------------------------------------------------

    def test_reference_from_an_earlier_observation_is_rejected(self):
        """Agent ids restart each extraction, so a stale id would otherwise
        re-bind to whichever control now holds that number."""
        obs = obs_with([rec("Next", agent_id="1")])
        stale = intent_for("Next", obs.discovery["by_label"]["Next"],
                           observation_id=99)
        self.assertEqual(ae.validate_action_grounding(stale, obs),
                         ae.GROUND_STALE_OBSERVATION)

    def test_missing_observation_rejects_rather_than_assumes(self):
        i = intent_for("Next", rec("Next", agent_id="1"))
        self.assertEqual(ae.validate_action_grounding(i, None),
                         ae.GROUND_NO_OBSERVATION)

    # -- ungrounded resolution --------------------------------------------

    def test_text_guessed_resolution_is_rejected(self):
        obs = obs_with([rec("Next", agent_id="1")])
        i = intent_for("Next", obs.discovery["by_label"]["Next"],
                       provenance=ae.RESOLVE_TEXT_GUESS)
        self.assertEqual(ae.validate_action_grounding(i, obs),
                         ae.GROUND_UNGROUNDED_RESOLUTION)

    def test_unresolved_locator_is_rejected(self):
        obs = obs_with([rec("Next", agent_id="1")])
        i = intent_for("Next", obs.discovery["by_label"]["Next"],
                       provenance=ae.RESOLVE_UNRESOLVED)
        self.assertEqual(ae.validate_action_grounding(i, obs),
                         ae.GROUND_UNGROUNDED_RESOLUTION)

    def test_only_the_grounded_resolution_is_accepted(self):
        obs = obs_with([rec("Next", agent_id="1")])
        rec_next = obs.discovery["by_label"]["Next"]
        for prov in (ae.RESOLVE_HINTED_ID, ae.RESOLVE_HINTED_VALUE,
                     ae.RESOLVE_TEXT_GUESS, ae.RESOLVE_UNRESOLVED):
            with self.subTest(provenance=prov):
                i = intent_for("Next", rec_next, provenance=prov)
                self.assertEqual(ae.validate_action_grounding(i, obs),
                                 ae.GROUND_UNGROUNDED_RESOLUTION)

    # -- required input -----------------------------------------------------

    def test_fill_without_a_value_is_rejected(self):
        obs = obs_with([rec("Email", agent_id="1", role="textbox")])
        i = intent_for("Email", obs.discovery["by_label"]["Email"],
                       required_input=None)
        self.assertEqual(ae.validate_action_grounding(i, obs),
                         ae.GROUND_MISSING_INPUT)

    def test_fill_with_a_value_passes_grounding(self):
        obs = obs_with([rec("Email", agent_id="1", role="textbox")])
        i = intent_for("Email", obs.discovery["by_label"]["Email"],
                       required_input="a@b.test")
        self.assertEqual(ae.validate_action_grounding(i, obs), ae.GROUND_OK)

    def test_click_needs_no_input(self):
        obs = obs_with([rec("Next", agent_id="1")])
        i = intent_for("Next", obs.discovery["by_label"]["Next"],
                       required_input=None)
        self.assertEqual(ae.validate_action_grounding(i, obs), ae.GROUND_OK)

    # -- determinism -------------------------------------------------------

    def test_gate_is_deterministic(self):
        obs = obs_with([rec("Add", agent_id="1"), rec("Add", agent_id="2")])
        i = intent_for("Add", obs.discovery["by_label"]["Add"])
        codes = {ae.validate_action_grounding(i, obs) for _ in range(20)}
        self.assertEqual(len(codes), 1,
                         "the same inputs must always give the same verdict")

    def test_every_rejection_code_has_a_human_explanation(self):
        for code in (ae.GROUND_NO_OBSERVATION, ae.GROUND_STALE_OBSERVATION,
                     ae.GROUND_TARGET_NOT_OBSERVED, ae.GROUND_TARGET_AMBIGUOUS,
                     ae.GROUND_TARGET_DISABLED, ae.GROUND_TARGET_OBSCURED,
                     ae.GROUND_TARGET_HIDDEN, ae.GROUND_UNGROUNDED_RESOLUTION,
                     ae.GROUND_MISSING_INPUT, ae.GROUND_AWAITING_CONFIRMATION,
                     ae.GROUND_POLICY_BLOCKED):
            with self.subTest(code=code):
                self.assertNotEqual(ae.grounding_message(code), "rejected")


# ===========================================================================
# Live: an ungrounded action must never reach the executor
# ===========================================================================

def _browser():
    try:
        from playwright.async_api import async_playwright
    except Exception:
        return None, "playwright is not installed"
    return async_playwright, None


FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fixtures", "observation", "index.html",
)


class TestLiveGrounding(unittest.IsolatedAsyncioTestCase):

    @classmethod
    def setUpClass(cls):
        pw, err = _browser()
        if pw is None or not os.path.exists(FIXTURE):
            raise unittest.SkipTest(err or f"fixture missing: {FIXTURE}")
        cls._pw = pw
        cls._loop = asyncio.new_event_loop()
        cls._pw_ctx = cls._loop.run_until_complete(pw().start())
        cls._browser_obj = cls._loop.run_until_complete(
            cls._pw_ctx.chromium.launch()
        )
        cls._page = cls._loop.run_until_complete(cls._browser_obj.new_page())
        cls._loop.run_until_complete(
            cls._page.goto("file:///" + FIXTURE.replace("\\", "/"))
        )

    @classmethod
    def tearDownClass(cls):
        loop = getattr(cls, "_loop", None)
        if loop is None:
            return
        try:
            loop.run_until_complete(cls._browser_obj.close())
            loop.run_until_complete(cls._pw_ctx.stop())
            loop.run_until_complete(asyncio.sleep(0))
        except Exception:
            pass
        finally:
            try:
                loop.close()
            except Exception:
                pass
            gc.collect()

    def observe(self):
        loop = self._loop
        d = loop.run_until_complete(
            ae.extract_page_elements(self._page, "button, a, input, select"))
        return ae.PageObservation(url=self._page.url, discovery=d), d

    def test_observed_element_resolves_grounded(self):
        obs, d = self.observe()
        loop = self._loop
        _, provenance = loop.run_until_complete(
            ae.build_locator_for_edge(self._page, "Add", d["hints"],
                                      agent_ids={n: r["agent_id"]
                                                 for n, r in d["by_label"].items()}))
        self.assertEqual(provenance, ae.RESOLVE_GROUNDED)

    def test_element_removed_since_observation_resolves_only_by_guess(self):
        """After the control disappears, its id no longer grounds anything."""
        obs, d = self.observe()
        ids = {n: r["agent_id"] for n, r in d["by_label"].items()}
        self._loop.run_until_complete(
            self._page.evaluate("() => document.getElementById('add-a').remove()"))
        _, provenance = self._loop.run_until_complete(
            ae.build_locator_for_edge(self._page, "Add", d["hints"],
                                      agent_ids=ids))
        self.assertNotEqual(provenance, ae.RESOLVE_GROUNDED,
                            "a stale id must never claim to be grounded")
        # Re-observing is how ambiguity is actually resolved: with only one
        # "Add" left the label genuinely identifies one control, so it becomes
        # actionable again and grounds afresh under a new id.
        # Re-observing is how ambiguity is actually resolved: with only one
        # "Add" left the label genuinely identifies one control, so it becomes
        # actionable again and grounds afresh under a new id.
        obs2, d2 = self.observe()
        self.assertEqual(obs2.status_of("Add"), ae.OBS_ACTIONABLE)
        i2 = ae.ActionIntent(operation=ae.OP_CLICK, target_name="Add",
                             observation_id=obs2.observation_id,
                             provenance=ae.RESOLVE_GROUNDED,
                             record=d2["by_label"]["Add"])
        self.assertEqual(ae.validate_action_grounding(i2, obs2), ae.GROUND_OK)

    def test_never_seen_target_is_rejected_before_any_lookup(self):
        obs, _ = self.observe()
        code = ae.validate_action_grounding(
            ae.ActionIntent(operation=ae.OP_CLICK, target_name="Delete Account",
                           observation_id=obs.observation_id,
                           provenance=ae.RESOLVE_GROUNDED),
            obs)
        self.assertEqual(code, ae.GROUND_TARGET_NOT_OBSERVED)

    def test_disabled_control_is_observed_then_rejected(self):
        obs, d = self.observe()
        self.assertEqual(obs.status_of("Pay"), ae.OBS_DISABLED)
        i = ae.ActionIntent(operation=ae.OP_CLICK, target_name="Pay",
                            observation_id=obs.observation_id,
                            provenance=ae.RESOLVE_GROUNDED,
                            record=d["by_label"]["Pay"])
        self.assertEqual(ae.validate_action_grounding(i, obs),
                         ae.GROUND_TARGET_DISABLED)


class TestSelectValueResolution(unittest.TestCase):
    """A select whose value the model did not supply.

    The gate refusing an input action with no value is correct — the agent must
    not invent what the user wanted. But a user who wrote "select the Editor
    role" HAS supplied the value, and asking again is a defect. The defect was
    two-layered: a select's available options were never extracted at all, so no
    value could be known; and nothing consulted the user's own words.

    These are offline tests. Neither `goal_texts_from_config` nor
    `resolve_goal_stated_option` existed before this repair, so every test here
    fails against the original behaviour.
    """

    CONFIG = {
        "ai_context": ("Open the confirm dialog and dismiss it, then select "
                       "the Editor role."),
        "test_goal": {
            "objective": "Report the selected role on the page",
            "final_evidence": ["Editor"],
        },
    }

    # --- the defect: options were never observable -----------------------
    def test_extraction_collects_select_options(self):
        """A select's choice set must reach the observation.

        Without this the model is asked for a value it cannot know, which is
        what produced `missing_required_input` on a goal that named the value.
        """
        self.assertIn("options: tag === 'select'", ae.DOM_EXTRACT_JS,
                      "element records must carry a select's options")
        self.assertIn("el.options || []", ae.DOM_EXTRACT_JS,
                      "options must be read from the control's real option list")

    # --- goal text collection --------------------------------------------
    def test_goal_texts_include_every_user_authored_string(self):
        texts = ae.goal_texts_from_config(self.CONFIG)
        self.assertIn("select the Editor role", texts[0],
                      "ai_context is user-authored goal text")
        self.assertIn("Report the selected role on the page", texts)
        self.assertIn("Editor", texts,
                      "final_evidence is user-authored goal text")

    def test_goal_texts_tolerate_absent_or_malformed_config(self):
        self.assertEqual(ae.goal_texts_from_config(None), [])
        self.assertEqual(ae.goal_texts_from_config({}), [])
        self.assertEqual(ae.goal_texts_from_config({"test_goal": "junk"}), [])

    # --- the resolution rule ----------------------------------------------
    def test_resolves_the_single_option_the_goal_names(self):
        texts = ae.goal_texts_from_config(self.CONFIG)
        self.assertEqual(
            ae.resolve_goal_stated_option(
                ["Administrator", "Viewer", "Editor"], texts),
            "Editor")

    def test_refuses_when_no_option_is_named(self):
        texts = ae.goal_texts_from_config(self.CONFIG)
        self.assertIsNone(
            ae.resolve_goal_stated_option(["Administrator", "Viewer"], texts),
            "an unnamed option must never be invented")

    def test_refuses_when_the_goal_names_two_options(self):
        """Ambiguity must fall back to asking, not to picking one."""
        self.assertIsNone(ae.resolve_goal_stated_option(
            ["Administrator", "Viewer", "Editor"],
            ["select the Editor or Viewer role"]))

    def test_refuses_without_any_goal_text(self):
        self.assertIsNone(
            ae.resolve_goal_stated_option(["Editor"], []))

    def test_refuses_when_the_control_has_no_options(self):
        self.assertIsNone(ae.resolve_goal_stated_option([], ["Editor"]))
        self.assertIsNone(ae.resolve_goal_stated_option(None, ["Editor"]))

    def test_matching_is_whole_word_not_substring(self):
        """A short option must not match inside a longer word.

        'Editor' must not be satisfied by 'Editorial', and a choice too short
        to be identifiable from running text must not be resolved at all.
        """
        self.assertIsNone(ae.resolve_goal_stated_option(
            ["Editor"], ["open the Editorial page"]))
        self.assertIsNone(ae.resolve_goal_stated_option(
            ["In", "Out of stock"], ["show what is In stock"]),
            "a two-character option is not identifiable from prose; the run "
            "must ask rather than guess")

    def test_matching_ignores_case_and_surrounding_punctuation(self):
        self.assertEqual(
            ae.resolve_goal_stated_option(["Administrator", "Editor"],
                                         ["then select the editor role."]),
            "Editor")

    # --- generalization: different layout, wording and option set ----------
    def test_generalises_to_a_different_control_and_wording(self):
        """Same capability, unrelated site: a shipping-speed select whose goal
        is written only in `test_goal.evidence`, with different option labels
        and none of the original words."""
        config = {
            "test_goal": {
                "objective": "Choose a delivery speed for the order",
                "evidence": {"text_contains": ["Express"],
                             "steps_min": 1},
            },
        }
        texts = ae.goal_texts_from_config(config)
        self.assertEqual(
            ae.resolve_goal_stated_option(
                ["Standard", "Express", "Overnight"], texts),
            "Express")

    def test_generalises_when_only_objective_states_the_choice(self):
        config = {"test_goal": {"objective": "Set the priority to Urgent"}}
        self.assertEqual(
            ae.resolve_goal_stated_option(["Low", "Normal", "Urgent"],
                                         ae.goal_texts_from_config(config)),
            "Urgent")

    def test_duplicate_option_texts_do_not_create_false_ambiguity(self):
        """The same option listed twice is one choice, not two."""
        self.assertEqual(
            ae.resolve_goal_stated_option(["Editor", "editor", "EDITOR"],
                                         ["select the Editor role"]),
            "Editor")

    def test_blank_options_are_ignored(self):
        self.assertEqual(
            ae.resolve_goal_stated_option(["", "  ", "Editor"],
                                         ["select the Editor role"]),
            "Editor")


if __name__ == "__main__":
    unittest.main(verbosity=2)

