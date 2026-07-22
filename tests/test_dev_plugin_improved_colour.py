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
    SpectralWorkflow, SpectralWorkflowPhaseType, SpectraContainer, MetricFieldView, EvaluationColorUtil,
    REFERENCE, SAMPLE, ABSORPTION,
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
        # V3 rename (Edwin 2026-07-22): "Browning A_blue" -> "Soret A_blue", "Browning ratio" ->
        # "Pigment ratio · legacy" (de-browned; §11 found the direction inverted).
        for base in ("Greenness G", "Pigment D_Q", "Soret A_blue", "Clarity A_green",
                     "Pigment ratio · legacy", "G' (alt.)"):
            self.assertIn(base, metricLabels, base)
            self.assertIn(base + " · despiked", metricLabels, base + " twin")
            # the twin sits directly under its raw row
            self.assertEqual(metricLabels.index(base + " · despiked"), metricLabels.index(base) + 1)
        # metrics no longer carry a flat-offset "· improved" twin (that is colour-only now)
        self.assertFalse(any(label.endswith(" · improved") for label in metricLabels))
        # no "Browning" wording survives in the legacy tab
        self.assertFalse(any("Browning" in label for label in metricLabels))

    def test_processing_declares_the_three_trace_absorption_ladder(self):
        workflow = self.__runPlugin()
        processing = workflow.getPhase(SpectralWorkflowPhaseType.PROCESSING)
        step = next((s for s in processing.getSteps().values()
                     if "despiked" in s.getLabel()), None)
        self.assertIsNotNone(step, "the raw/despiked/improved ladder tab")
        # raw + despiked + improved
        self.assertEqual(len(step.getView().allTraces()), 3)

    # --- V3 (SPEC_capability_proof.md §2.1, Edwin 2026-07-22): the "Evaluation (new)" tab + second plot ---

    def __evalStep(self, workflow, label):
        phase = workflow.getPhase(SpectralWorkflowPhaseType.EVALUATION)
        return next((s for s in phase.getSteps().values() if s.getLabel() == label), None)

    def test_new_evaluation_tab_carries_pb_band_means_and_pigment_ratios(self):
        workflow = self.__runPlugin()
        step = self.__evalStep(workflow, "Evaluation (new)")
        self.assertIsNotNone(step, "the Evaluation (new) step")
        labels = [i.label for i in step.getEvaluationResult().getItems()
                  if isinstance(i, MetricFieldView) and i.color is None]
        for expected in ("Soret · 440–460 nm", "Q · 560–580 nm", "Clarity · 510–540 nm",
                         "Pigment ratio", "Pigment ratio · clarity"):
            self.assertIn(expected, labels, expected)

    def test_new_evaluation_tab_duplicates_the_ten_colour_chips(self):
        workflow = self.__runPlugin()
        step = self.__evalStep(workflow, "Evaluation (new)")
        chips = [i for i in step.getEvaluationResult().getItems()
                 if isinstance(i, MetricFieldView) and i.color is not None]
        self.assertEqual(len(chips), 10, "the full 10-variant colour set, duplicated")

    def test_second_band_marked_spectrum_uses_the_pb_bands(self):
        workflow = self.__runPlugin()
        step = self.__evalStep(workflow, "Spectrum (new)")
        self.assertIsNotNone(step, "the Spectrum (new) step")
        bands = step.getView().bands  # list of (lowNm, highNm[, label])
        windows = {(round(b[0]), round(b[1])) for b in bands}
        self.assertEqual(windows, {(440, 460), (510, 540), (560, 580)})

    def test_intrinsic_perceived_chip_is_the_white_point_complement(self):
        # SPEC_capability_proof.md option (b): the intrinsic-perceived hue must be the white-point complement of the
        # absorbed colour, NOT the retired +180° HSL flip. Prove the plugin routes through complementViaWhitePoint.
        workflow = self.__runPlugin()
        proc = workflow.getPhase(SpectralWorkflowPhaseType.PROCESSING)
        absorption = next(s for s in proc.getSteps().values()
                          if s.getLabel() == "Absorption").getContainer().getSpectra()[ABSORPTION]
        util = EvaluationColorUtil()
        expected = round(util.complementViaWhitePoint(absorption, ceiling=3.0)[0])
        absHue = util.spectrumToHsl(absorption, converter="srgb", ceiling=3.0)[0]
        flip = round((absHue + 180.0) % 360.0)
        step = self.__evalStep(workflow, "Evaluation (new)")
        chip = next(i for i in step.getEvaluationResult().getItems()
                    if isinstance(i, MetricFieldView) and i.label == "Intrinsic-perceived · hue-norm")
        shown = int(chip.value.split("°")[0].replace("H", "").strip())
        self.assertEqual(shown, expected, "chip hue must be the white-point complement")
        self.assertNotEqual(shown, flip, "must NOT be the old +180° flip")

    def test_normalized_chips_use_the_calm_c_scheme_saturation_lightness(self):
        workflow = self.__runPlugin()
        step = self.__evalStep(workflow, "Evaluation (new)")
        normChips = [i for i in step.getEvaluationResult().getItems()
                     if isinstance(i, MetricFieldView) and i.color is not None
                     and i.value is not None and "hue-norm" in i.label]
        self.assertTrue(normChips, "at least one hue-normalized chip")
        for chip in normChips:
            self.assertIn("S 38%", chip.value, chip.label)
            self.assertIn("L 34%", chip.value, chip.label)
