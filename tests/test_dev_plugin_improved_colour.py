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
        metricsStep = next(s for s in phase.getSteps().values() if s.getLabel() == "Metrics (dev)")
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
                     if s.getLabel() == "Absorption (dev)"), None)
        self.assertIsNotNone(step, "the raw/despiked/improved ladder tab")
        # raw + despiked + improved
        self.assertEqual(len(step.getView().allTraces()), 3)

    # --- V3 (SPEC_capability_proof.md §2.1, Edwin 2026-07-22): the "Metrics" tab + second plot ---

    # ⚠ RENAMED 2026-08-14 (SPEC_v_metric_integration.md §6): the chord picture these four cases assert
    # about moved from "Absorption (bands)" to "Absorption (bands, baseline)", and the old NAME now belongs
    # to the `V` plot — a different picture (one curve, no fitted baseline). Looking the step up by the old
    # label would silently start asserting the chord tab's properties against the V tab.

    def __evalStep(self, workflow, label):
        phase = workflow.getPhase(SpectralWorkflowPhaseType.EVALUATION)
        return next((s for s in phase.getSteps().values() if s.getLabel() == label), None)

    def test_new_evaluation_tab_carries_pb_band_means_and_pigment_ratios(self):
        workflow = self.__runPlugin()
        step = self.__evalStep(workflow, "Metrics")
        self.assertIsNotNone(step, "the Evaluation (new) step")
        labels = [i.label for i in step.getEvaluationResult().getItems()
                  if isinstance(i, MetricFieldView) and i.color is None]
        for expected in ("Soret · 448–460 nm", "Q · 560–580 nm", "Clarity · 510–540 nm",
                         "Pigment ratio", "Pigment ratio · clarity"):
            self.assertIn(expected, labels, expected)

    def test_new_evaluation_tab_duplicates_the_ten_colour_chips(self):
        workflow = self.__runPlugin()
        step = self.__evalStep(workflow, "Metrics")
        chips = [i for i in step.getEvaluationResult().getItems()
                 if isinstance(i, MetricFieldView) and i.color is not None]
        self.assertEqual(len(chips), 10, "the full 10-variant colour set, duplicated")

    def test_second_band_marked_spectrum_uses_the_pb_bands(self):
        # SPEC_soret_448_trim.md §14 — the plot must shade EXACTLY the windows this tab's metrics read: the
        # trimmed Soret, the Q band, and BOTH baseline anchors. ⚠ 510-540 (clarity) is deliberately absent:
        # one grey block at 510-540 was being read as the 520-540 anchor and was wrong by 10 nm (defect B2).
        # The clarity metric ROW is unaffected — asserted above.
        workflow = self.__runPlugin()
        step = self.__evalStep(workflow, "Absorption (bands, baseline)")
        self.assertIsNotNone(step, "the Spectrum (new) step")
        bands = step.getView().bands  # list of (lowNm, highNm, label, color)
        windows = {(round(b[0]), round(b[1])) for b in bands}
        self.assertEqual(windows, {(448, 460), (520, 540), (560, 580), (620, 630)})
        self.assertEqual({b[2] for b in bands}, {"S", "quiet anchor", "Q", "red anchor"})

    def test_the_band_plot_overlays_the_subtracted_curve_and_the_fitted_baseline(self):
        # SPEC_soret_448_trim.md §25.1 — three curves: the measurement, what the verdict reads, and the
        # construction that connects them (dashed, so it reads as construction).
        workflow = self.__runPlugin()
        view = self.__evalStep(workflow, "Absorption (bands, baseline)").getView()
        labels = [trace[1] for trace in view.allTraces()]
        self.assertIn("A(λ) − baseline", labels)
        dashed = [trace for trace in view.allTraces() if trace[3] == "dashed"]
        self.assertEqual(len(dashed), 1, "the fitted baseline, dashed so it reads as construction")

    def test_each_bar_is_measured_on_the_curve_that_gives_it_meaning(self):
        # ⭐⭐ THE RULE (Edwin, §25.1) and the one failure that would render plausibly while being wrong:
        #   S / Q  -> the SUBTRACTED curve (their heights ARE B_Soret and B_Q, the numbers M divides)
        #   anchors -> the RAW curve (they DEFINE the fitted line; their bars landing on it is the proof)
        # Swap either source and nothing errors — the picture just stops being true.
        from sciens.spectracs.plugin_sdk import SpectrumFeatureUtil
        workflow = self.__runPlugin()
        view = self.__evalStep(workflow, "Absorption (bands, baseline)").getView()
        util, plugin = SpectrumFeatureUtil(), DevSpectralPlugin()
        raw = view.allTraces()[0][0]   # the measured curve is a labelled TRACE now, not the primary
        corrected = util.linearBaselineCorrected(raw, plugin.PB_BASELINE_WINDOWS)
        onCorrected = {1: plugin.PB_SORET_BAND, 2: plugin.PB_Q_BAND}
        bars = {level[6]: level for level in view.levels if level[6] is not None}
        self.assertEqual(sorted(bars), [1, 2, 3, 4], "four numbered bars")
        for number, level in bars.items():
            source = corrected if number in onCorrected else raw
            self.assertAlmostEqual(level[0], util.bandMean(source, level[1], level[2]), places=9,
                                   msg="bar %d is measured on the wrong curve" % number)
        # ⚠ and the anchors' bars must land ON the fitted line — i.e. ~0 once the baseline is removed.
        # RELATIVE to the numerator, deliberately: the fit is an equal-weight LSQ through every anchor point,
        # not a two-point chord, so the residual depends on how curved the curve is INSIDE the window (§18
        # duck #1). On real archive data it is 0.0004 — 0.05 % of B_Soret; on this synthetic stand-in, whose
        # anchor windows are far more curved, it is ~2 %. An absolute tolerance would encode the fixture.
        soret = util.bandMean(corrected, *plugin.PB_SORET_BAND)
        for number in (3, 4):
            level = bars[number]
            self.assertLess(abs(util.bandMean(corrected, level[1], level[2])), 0.05 * abs(soret),
                            msg="anchor %d drifted off the fitted line" % number)

    def test_the_band_plot_declares_a_numbered_legend_in_the_north_east(self):
        from sciens.spectracs.plugin_sdk import LegendPosition
        workflow = self.__runPlugin()
        view = self.__evalStep(workflow, "Absorption (bands, baseline)").getView()
        self.assertEqual(view.legendPosition, LegendPosition.NORTH_EAST)
        self.assertGreater(view.legendPadding, 0, "a MAGNITUDE — the renderer owns the sign")
        rows = view.legendRows()
        # numbered bars first, ascending; then the curves, which carry no number and are named by colour
        self.assertEqual([row[0] for row in rows[:4]], [1, 2, 3, 4])
        self.assertEqual([row[1] for row in rows[:4]],
                         ["Soret band mean", "Q-band mean", "red-anchor mean", "quiet-anchor mean"])
        self.assertTrue(all(row[0] is None and row[2] for row in rows[4:]), "curves: no badge, own colour")

    def test_the_dn_guard_is_declared_on_the_SAMPLE_step_only(self):
        # SPEC_soret_448_trim.md §25.4 — §16.23.8 states the guard on min(S) after the SAMPLE capture; the
        # reference is a solvent blank judged against R ~ 88, so the dosing rule never applied to it.
        #
        # ⭐ UPDATED 2026-08-12 (SPEC_capture_quality.md §16.23.10): the four lines {16, 60, 20, 40} became the
        # single target pair {20, 50}. The 16/60 REJECTION EDGES are no longer drawn — across 34 archive runs
        # the minimum ever observed was 37.6 DN, so they only added ink to the plot the operator actually
        # reads; the 16 CHECK moved to the host's CAPTURE-LOWDN log line. The target moved 40 -> 50 because
        # §16.23.8's justification for 20-40 (the A = 0.434 optimum via "R ≈ 88 ⇒ S ≈ 32 DN") is LINEAR
        # arithmetic applied to ENCODED thresholds and does not survive (§16.23.10b).
        from sciens.spectracs.plugin_sdk import REFERENCE as REF, SAMPLE as SMP
        workflow = self.__runPlugin()
        processing = workflow.getPhase(SpectralWorkflowPhaseType.PROCESSING)
        step = next(s for s in processing.getSteps().values() if s.getLabel() == "Spectra")
        self.assertEqual({level[0] for level in step.getView().levels}, {20.0, 50.0})
        byRole = {s.getRole(): s.getView()
                  for s in workflow.getPhase(SpectralWorkflowPhaseType.ACQUISITION).getSteps().values()}
        self.assertEqual([level[0] for level in byRole[SMP].levels], [20.0, 50.0])
        self.assertEqual(byRole[REF].levels, [], "the reference declares no DN guard")
        # the captions travel WITH the values — one source of truth for the preview and the report
        self.assertIn("20–50 DN target (provisional)", [level[3] for level in byRole[SMP].levels])
        # ⚠ and the window/rule/colours ride along on the SAMPLE step only (§16.23.10f)
        self.assertEqual(byRole[SMP].guardBandNm, (448.0, 460.0))
        self.assertEqual(byRole[SMP].guardTargetDn, (20.0, 50.0))
        self.assertIsNone(byRole[REF].guardBandNm)

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
        step = self.__evalStep(workflow, "Metrics")
        chip = next(i for i in step.getEvaluationResult().getItems()
                    if isinstance(i, MetricFieldView) and i.label == "Intrinsic-perceived · hue-norm")
        shown = int(chip.value.split("°")[0].replace("H", "").strip())
        self.assertEqual(shown, expected, "chip hue must be the white-point complement")
        self.assertNotEqual(shown, flip, "must NOT be the old +180° flip")

    def test_normalized_chips_use_the_calm_c_scheme_saturation_lightness(self):
        workflow = self.__runPlugin()
        step = self.__evalStep(workflow, "Metrics")
        normChips = [i for i in step.getEvaluationResult().getItems()
                     if isinstance(i, MetricFieldView) and i.color is not None
                     and i.value is not None and "hue-norm" in i.label]
        self.assertTrue(normChips, "at least one hue-normalized chip")
        for chip in normChips:
            self.assertIn("S 38%", chip.value, chip.label)
            self.assertIn("L 34%", chip.value, chip.label)
