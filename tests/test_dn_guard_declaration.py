"""SPEC_capture_quality.md §16.23.10h — what the dev plugin DECLARES for the low-DN guard.

The plugin owns the measurement constants; the host owns the maths. These lock the declaration down so a
future edit cannot silently move the verdict — in particular, `levels` (what is DRAWN) and `guardTargetDn`
(the RULE) must stay built from the same two constants.
"""
import unittest

from sciens.spectracs.plugin_sdk.roles import REFERENCE, SAMPLE
from sciens.spectracs.plugins.dev.DevSpectralPlugin import DevSpectralPlugin


class DnGuardDeclarationTest(unittest.TestCase):

    def setUp(self):
        self.plugin = DevSpectralPlugin()

    def test_the_target_window_is_20_to_50(self):
        # §16.23.10e — Edwin's call, 2026-08-12. Was 20-40, whose justification (§16.23.8's A = 0.434 optimum)
        # was linear arithmetic applied to encoded thresholds and does not survive (§16.23.10b).
        self.assertEqual(self.plugin.DN_TARGET_LOW, 20.0)
        self.assertEqual(self.plugin.DN_TARGET_HIGH, 50.0)

    def test_the_guard_band_is_the_metric_window(self):
        # ⚠ §16.23.10f, the accepted coupling: retrim PB_SORET_BAND and the guard moves with it.
        self.assertEqual(self.plugin.DN_GUARD_BAND, self.plugin.PB_SORET_BAND)
        self.assertEqual(self.plugin.DN_GUARD_BAND, (448.0, 460.0))

    def test_the_rejection_edges_are_no_longer_drawn(self):
        # §16.23.10f: across 34 archive runs the minimum observed was 37.6 DN, so 16/60 only added ink. The 16
        # CHECK survives in the host's log line — this asserts only that the LINES are gone.
        values = [level[0] for level in self.plugin.dnLevels()]
        self.assertEqual(sorted(values), [20.0, 50.0])
        self.assertNotIn(16.0, values)
        self.assertNotIn(60.0, values)

    def test_the_drawn_levels_and_the_rule_cannot_drift(self):
        # ⭐ The reason `guardTargetDn` is declared rather than inferred from `levels`: adding a decorative
        # level later must not move the verdict, and the pair must still agree today.
        values = sorted(level[0] for level in self.plugin.dnLevels())
        self.assertEqual((values[0], values[-1]), (self.plugin.DN_TARGET_LOW, self.plugin.DN_TARGET_HIGH))

    def test_the_target_is_labelled_provisional(self):
        # §16.23.10e — it fits the oil under test and would call 7 of 8 correctly-dosed archive runs too
        # dilute. The operator must see that on the plot, not only in the spec.
        labels = [level[3] for level in self.plugin.dnLevels() if level[3]]
        self.assertTrue(any("provisional" in label.lower() for label in labels),
                        "the drawn caption must carry the provisional status: %r" % (labels,))

    def test_the_measured_reading_has_an_inside_and_an_outside_colour(self):
        colors = self.plugin.dnGuardColors()
        self.assertIn("inside", colors)
        self.assertIn("outside", colors)
        self.assertNotEqual(colors["inside"], colors["outside"])


class DnGuardCaptureViewTest(unittest.TestCase):
    """The declaration as it reaches the host, per role."""

    def setUp(self):
        from sciens.spectracs.plugin_sdk import SpectralWorkflow, SpectralWorkflowPhaseType
        from sciens.spectracs.model.spectral.SpectralWorkflowPhase import SpectralWorkflowPhase
        workflow = SpectralWorkflow()
        phase = SpectralWorkflowPhase()
        phase.setType(SpectralWorkflowPhaseType.ACQUISITION)
        workflow.addToPhases(phase)
        DevSpectralPlugin().acquisition(workflow)
        self.views = {step.getRole(): step.getView() for step in phase.getSteps().values()}

    def test_the_sample_declares_the_full_guard(self):
        view = self.views[SAMPLE]
        self.assertEqual(view.guardBandNm, (448.0, 460.0))
        self.assertEqual(view.guardTargetDn, (20.0, 50.0))
        self.assertIsNotNone(view.guardColors)
        self.assertTrue(view.levels)

    def test_the_reference_declares_none_of_it(self):
        # ⭐ §16.23.8 / §16.23.10f: the guard is stated on min(S) AFTER THE SAMPLE capture. The reference is a
        # solvent blank whose level is set by auto-exposure — drawing a dosing rule on it invited the operator
        # to "fix" a reference that was never wrong.
        view = self.views[REFERENCE]
        self.assertIsNone(view.guardBandNm)
        self.assertIsNone(view.guardTargetDn)
        self.assertEqual(view.levels, [])


if __name__ == "__main__":
    unittest.main()
