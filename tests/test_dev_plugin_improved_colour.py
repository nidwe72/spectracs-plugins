"""
Entry 0 (SPEC_capability_proof.md §7.0.1 / §8.1, E4/E5) — the DevSpectralPlugin improved-colour skeleton.

Drives the plugin's hooks DIRECTLY (acquisition -> processing -> evaluation), the same boundary style as
test_pumpkin_plugin_boundary, and asserts the EVALUATION colour block now carries the two "· improved"
absorbance twins (7 chips total) and that PROCESSING declares the "Absorption (improved)" overlay tab.

Run from the spectracsPy repo root:
    PYTHONPATH=".:../spectracsPy-core:../spectracsPy-model:../spectracsPy-base:../spectracsPy-server:../spectracs-plugins" \
        ./venv/bin/python -m pytest ../spectracs-plugins/tests/test_dev_plugin_improved_colour.py -q
"""
import unittest

from sciens.spectracs.plugin_sdk import (
    SpectralWorkflow, SpectralWorkflowPhaseType, SpectraContainer, MetricFieldView, REFERENCE, SAMPLE,
)
from sciens.spectracs.model.spectral.SpectralWorkflowPhase import SpectralWorkflowPhase
from sciens.spectracs.logic.spectral.synthesis.LedReferenceSynthesisLogicModule import LedReferenceSynthesisLogicModule
from sciens.spectracs.logic.spectral.synthesis.LedReferenceSynthesisLogicModuleParameters import LedReferenceSynthesisLogicModuleParameters
from sciens.spectracs.logic.spectral.synthesis.OilSampleSynthesisLogicModule import OilSampleSynthesisLogicModule
from sciens.spectracs.logic.spectral.synthesis.OilSampleSynthesisLogicModuleParameters import OilSampleSynthesisLogicModuleParameters
from sciens.spectracs.logic.spectral.synthesis.PlaygroundDemoOils import PLAYGROUND_DEMO_OILS

from sciens.spectracs.plugins.dev.DevSpectralPlugin import DevSpectralPlugin

PHASE_ORDER = [
    SpectralWorkflowPhaseType.ACQUISITION,
    SpectralWorkflowPhaseType.PROCESSING,
    SpectralWorkflowPhaseType.EVALUATION,
    SpectralWorkflowPhaseType.METADATA,
    SpectralWorkflowPhaseType.PUBLISHING,
]


class DevPluginImprovedColourTest(unittest.TestCase):

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
        return workflow

    def __metricItems(self, workflow):
        phase = workflow.getPhase(SpectralWorkflowPhaseType.EVALUATION)
        metricsStep = next(s for s in phase.getSteps().values() if s.getLabel() == "Metrics")
        return metricsStep.getEvaluationResult().getItems()

    def test_evaluation_colour_chips_are_the_ten_variant_set(self):
        items = self.__metricItems(self.__runPlugin())
        # A colour chip is a MetricFieldView carrying a swatch colour; the plain metric rows (Greenness, ...) don't.
        chips = [i for i in items if isinstance(i, MetricFieldView) and i.color is not None]
        labels = [c.label for c in chips]
        self.assertEqual(labels, [
            "Intrinsic", "Intrinsic · hue-norm", "Intrinsic · despiked", "Intrinsic · despiked + baseline",
            "Intrinsic-perceived", "Intrinsic-perceived · hue-norm", "Intrinsic-perceived · despiked",
            "Intrinsic-perceived · despiked + baseline",
            "Perceived", "Perceived · hue-norm",
        ])

    def test_every_metric_has_a_paired_despiked_twin(self):
        items = self.__metricItems(self.__runPlugin())
        # metric rows are MetricFieldViews WITHOUT a swatch colour (chips have one)
        metricLabels = [i.label for i in items if isinstance(i, MetricFieldView) and i.color is None]
        for base in ("Greenness G", "Pigment D_Q", "Browning A_blue", "Clarity A_green",
                     "Browning ratio", "G' (alt.)"):
            self.assertIn(base, metricLabels, base)
            self.assertIn(base + " · despiked", metricLabels, base + " twin")
            # the twin sits directly under its raw row
            self.assertEqual(metricLabels.index(base + " · despiked"), metricLabels.index(base) + 1)
        # metrics no longer carry a flat-offset "· improved" twin (that is colour-only now)
        self.assertFalse(any(label.endswith(" · improved") for label in metricLabels))

    def test_processing_declares_the_three_trace_absorption_ladder(self):
        workflow = self.__runPlugin()
        processing = workflow.getPhase(SpectralWorkflowPhaseType.PROCESSING)
        step = next((s for s in processing.getSteps().values()
                     if "despiked" in s.getLabel()), None)
        self.assertIsNotNone(step, "the raw/despiked/improved ladder tab")
        # raw + despiked + improved
        self.assertEqual(len(step.getView().allTraces()), 3)
