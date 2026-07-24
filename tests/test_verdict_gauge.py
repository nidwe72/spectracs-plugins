"""
SPEC_roast_ampel.md §8 — the plugin-driven Verdict gauge (Roast Ampel).

Covers the Qt-free stack: GaugeColorUtil maths (G0), VerdictGaugeView round-trip + factory (G1), the
RoastGaugeView cache (G6), and the DevSpectralPlugin wiring — gauge first in "Evaluation (new)" (G6) and the
verdict badge on the PUBLISHING "Send to LIMS" step (G7).

Run from the spectracsPy repo root:
    PYTHONPATH=".:../spectracsPy-core:../spectracsPy-model:../spectracsPy-base:../spectracsPy-server:../spectracs-plugins" \
        ./venv/bin/python -m pytest ../spectracs-plugins/tests/test_verdict_gauge.py -q
"""
import unittest

from sciens.spectracs.plugin_sdk import (
    SpectralWorkflow, SpectralWorkflowPhaseType, SpectraContainer,
    VerdictGaugeView, GaugeRender, GaugeColorUtil,
    REFERENCE, SAMPLE,
)
from sciens.spectracs.model.spectral.SpectralWorkflowPhase import SpectralWorkflowPhase
from sciens.spectracs.model.spectral.plugin.view.ViewModelFactory import ViewModelFactory
from sciens.spectracs.logic.spectral.synthesis.LedReferenceSynthesisLogicModule import LedReferenceSynthesisLogicModule
from sciens.spectracs.logic.spectral.synthesis.LedReferenceSynthesisLogicModuleParameters import LedReferenceSynthesisLogicModuleParameters
from sciens.spectracs.logic.spectral.synthesis.OilSampleSynthesisLogicModule import OilSampleSynthesisLogicModule
from sciens.spectracs.logic.spectral.synthesis.OilSampleSynthesisLogicModuleParameters import OilSampleSynthesisLogicModuleParameters
from sciens.spectracs.logic.spectral.synthesis.PlaygroundDemoOils import PLAYGROUND_DEMO_OILS

from sciens.spectracs.plugins.dev.DevSpectralPlugin import DevSpectralPlugin
from sciens.spectracs.plugins.dev.RoastGaugeView import RoastGaugeView

# A fixed 3-anchor descending set for exercising GaugeColorUtil directly (NOT the plugin's actual roast anchors,
# which start at 4.5 with an extra brown anchor — read those off the view in the view-model tests instead).
SAMPLE_ANCHORS = [(4.0, "#9B9E57"), (2.8, "#8B8952"), (2.0, "#6E5A34")]

PHASE_ORDER = [
    SpectralWorkflowPhaseType.ACQUISITION,
    SpectralWorkflowPhaseType.PROCESSING,
    SpectralWorkflowPhaseType.EVALUATION,
    SpectralWorkflowPhaseType.METADATA,
    SpectralWorkflowPhaseType.PUBLISHING,
]


class GaugeColorUtilTest(unittest.TestCase):
    def setUp(self):
        self.util = GaugeColorUtil()

    def test_classify_is_orientation_aware_descending_band(self):
        # band descends 4.0 -> 2.0, threshold 2.8; value on the boundary stays in the LEFT (good) class
        self.assertEqual(self.util.classify(3.69, [2.8], 4.0, 2.0), 0)
        self.assertEqual(self.util.classify(2.62, [2.8], 4.0, 2.0), 1)
        self.assertEqual(self.util.classify(2.80, [2.8], 4.0, 2.0), 0)
        self.assertEqual(self.util.classify(4.03, [2.8], 4.0, 2.0), 0)

    def test_classify_supports_three_classes(self):
        # N thresholds -> N+1 classes (the amber middle class would be a data-only change)
        self.assertEqual(self.util.classify(3.5, [2.8, 2.6], 4.0, 2.0), 0)
        self.assertEqual(self.util.classify(2.7, [2.8, 2.6], 4.0, 2.0), 1)
        self.assertEqual(self.util.classify(2.4, [2.8, 2.6], 4.0, 2.0), 2)

    def test_gradient_is_exact_at_anchors_and_clamps_beyond(self):
        self.assertEqual(self.util.gradientColorAt(4.0, SAMPLE_ANCHORS), "#9b9e57")
        self.assertEqual(self.util.gradientColorAt(2.8, SAMPLE_ANCHORS), "#8b8952")
        self.assertEqual(self.util.gradientColorAt(2.0, SAMPLE_ANCHORS), "#6e5a34")
        self.assertEqual(self.util.gradientColorAt(4.5, SAMPLE_ANCHORS), "#9b9e57")   # clamp to left/green
        self.assertEqual(self.util.gradientColorAt(1.5, SAMPLE_ANCHORS), "#6e5a34")   # clamp to right/brown

    def test_position_is_linear_and_clamped(self):
        self.assertAlmostEqual(self.util.positionOf(3.69, 4.0, 2.0), 0.155, places=3)
        self.assertEqual(self.util.positionOf(4.03, 4.0, 2.0), 0.0)   # RD#5: marker clamps at the edge
        self.assertEqual(self.util.positionOf(1.0, 4.0, 2.0), 1.0)


class VerdictGaugeViewTest(unittest.TestCase):
    def __view(self):
        return RoastGaugeView(3.69, GaugeRender.BAND | GaugeRender.LABEL | GaugeRender.SWATCH)

    def test_roast_preset_caches_verdict_and_swatch(self):
        view = self.__view()
        self.assertEqual(view.verdictLabel, "good — green")
        # swatch colour is the gradient at the value on the plugin's OWN anchors (read off the view)
        self.assertEqual(view.swatchColor, GaugeColorUtil().gradientColorAt(view.value, view.gradientAnchors))
        self.assertEqual(view.bandLeft, 4.5)
        self.assertEqual(view.bandRight, 2.0)

    def test_brown_value_caches_brown_verdict(self):
        self.assertEqual(RoastGaugeView(2.45, GaugeRender.LABEL).verdictLabel, "probably too brown")

    def test_round_trips_through_the_view_model_factory(self):
        view = self.__view()
        view.setShownInReport(True)
        rebuilt = ViewModelFactory.fromJson(view.toJson())
        self.assertIsInstance(rebuilt, VerdictGaugeView)
        self.assertEqual(rebuilt.toJson(), view.toJson())
        self.assertEqual(rebuilt.toJson()["type"], "gauge")
        self.assertEqual(rebuilt.toJson()["render"], ["label", "band", "swatch"])
        self.assertTrue(rebuilt.isShownInReport)

    def test_render_flags_are_additive(self):
        self.assertEqual(RoastGaugeView(3.0, GaugeRender.LABEL | GaugeRender.SWATCH).render.toNames(),
                         ["label", "swatch"])


class DevPluginGaugeWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference = LedReferenceSynthesisLogicModule().synthesize(
            LedReferenceSynthesisLogicModuleParameters()).getSpectrum()
        parameters = OilSampleSynthesisLogicModuleParameters()
        parameters.setReference(cls.reference)
        parameters.setTargetHue(PLAYGROUND_DEMO_OILS[0].targetHue)
        cls.sample = OilSampleSynthesisLogicModule().synthesize(parameters).getSpectrum()

    def __runPlugin(self):
        plugin = DevSpectralPlugin()
        workflow = SpectralWorkflow()
        for phaseType in PHASE_ORDER:
            phase = SpectralWorkflowPhase()
            phase.setType(phaseType)
            workflow.addToPhases(phase)
        plugin.acquisition(workflow)
        captured = {REFERENCE: self.reference, SAMPLE: self.sample}
        acquisition = workflow.getPhase(SpectralWorkflowPhaseType.ACQUISITION)
        for step in acquisition.getSteps().values():
            container = SpectraContainer()
            container.addToSpectra(captured[step.getRole()], step.getRole())
            step.setContainer(container)
        plugin.processing(workflow)
        plugin.evaluation(workflow)
        plugin.publishing(workflow)
        return workflow

    def __step(self, workflow, phaseType, label):
        phase = workflow.getPhase(phaseType)
        return next(s for s in phase.getSteps().values() if s.getLabel() == label)

    def test_gauge_is_first_item_of_evaluation_new(self):
        workflow = self.__runPlugin()
        items = self.__step(workflow, SpectralWorkflowPhaseType.EVALUATION, "Evaluation (new)") \
            .getEvaluationResult().getItems()
        self.assertIsInstance(items[0], VerdictGaugeView)
        self.assertEqual(items[0].render, GaugeRender.BAND | GaugeRender.LABEL | GaugeRender.SWATCH)
        self.assertIsNotNone(items[0].verdictLabel)

    def test_publishing_step_carries_the_verdict_badge(self):
        workflow = self.__runPlugin()
        step = self.__step(workflow, SpectralWorkflowPhaseType.PUBLISHING, "Send to LIMS")
        badgeItems = step.getEvaluationResult().getItems()
        self.assertEqual(len(badgeItems), 1)
        self.assertIsInstance(badgeItems[0], VerdictGaugeView)
        self.assertEqual(badgeItems[0].render, GaugeRender.LABEL | GaugeRender.SWATCH)

    def test_badge_value_matches_the_evaluation_gauge(self):
        workflow = self.__runPlugin()
        evalGauge = self.__step(workflow, SpectralWorkflowPhaseType.EVALUATION, "Evaluation (new)") \
            .getEvaluationResult().getItems()[0]
        badge = self.__step(workflow, SpectralWorkflowPhaseType.PUBLISHING, "Send to LIMS") \
            .getEvaluationResult().getItems()[0]
        self.assertAlmostEqual(evalGauge.value, badge.value, places=9)


if __name__ == "__main__":
    unittest.main()
