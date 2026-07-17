"""
T2 — the plugin-boundary test (SPEC_project_structure.md, the S5 subsection).

Exercises PumpkinOilPlugin's hooks DIRECTLY, feeding spectra synthesized in spectracsPy-core, WITHOUT the
app-side SpectralWorkflowEngine or the camera/QImage capture path. It imports `plugin_sdk` + `-core`
synthesis only — no app module — so it runs in THIS repo's CI with `spectracsPy` off the PYTHONPATH.
(The T1 full `engine.runAll` end-to-end, test_pumpkin_workflow_end_to_end.py, stays in the app: the engine
is genuinely host — qGray/session/acquisition.)

The plugin's contract is three hooks over a workflow:
  acquisition(workflow)  -> declares REFERENCE + SAMPLE steps; the HOST fills each step's container.
  processing(workflow)   -> reads the captured spectra, computes mean -> transmission -> absorption.
  evaluation(workflow)   -> turns transmission into a colour/hue verdict (an EvaluationResult).
This test emulates only the one host step in the middle (fill the declared steps with synthesized spectra),
so it is a faithful boundary mirror of T1 without the engine.

Run (app-absent PYTHONPATH — note spectracsPy is NOT on it):
  PYTHONPATH=.:../spectracsPy-core:../spectracsPy-model:../spectracsPy-base \
    ../spectracsPy/venv/bin/python -m pytest tests/test_pumpkin_plugin_boundary.py -q
"""
import unittest

from sciens.spectracs.plugin_sdk import (
    SpectralWorkflow, SpectralWorkflowPhaseType, SpectraContainer, VerdictView, REFERENCE, SAMPLE,
)
# NOTE: SpectralWorkflowPhase is NOT re-exported by the plugin_sdk facade (only SpectralWorkflow /
# ...PhaseType / ...Step are). A pure-SDK author therefore cannot build a test workflow from the facade
# alone — a small, additive testability gap worth closing later (§1c: be conservative about the facade,
# but exporting it is additive+free). It lives in -model (not the app), so importing it here is fine.
from sciens.spectracs.model.spectral.SpectralWorkflowPhase import SpectralWorkflowPhase
from sciens.spectracs.logic.spectral.synthesis.LedReferenceSynthesisLogicModule import LedReferenceSynthesisLogicModule
from sciens.spectracs.logic.spectral.synthesis.LedReferenceSynthesisLogicModuleParameters import LedReferenceSynthesisLogicModuleParameters
from sciens.spectracs.logic.spectral.synthesis.OilSampleSynthesisLogicModule import OilSampleSynthesisLogicModule
from sciens.spectracs.logic.spectral.synthesis.OilSampleSynthesisLogicModuleParameters import OilSampleSynthesisLogicModuleParameters
from sciens.spectracs.logic.spectral.synthesis.PlaygroundDemoOils import PLAYGROUND_DEMO_OILS

from sciens.spectracs.plugins.pumpkin.PumpkinOilPlugin import PumpkinOilPlugin


# Mirrors SpectralWorkflowEngine.PHASE_ORDER — the engine is app, so the boundary test builds the phases itself.
PHASE_ORDER = [
    SpectralWorkflowPhaseType.ACQUISITION,
    SpectralWorkflowPhaseType.PROCESSING,
    SpectralWorkflowPhaseType.EVALUATION,
    SpectralWorkflowPhaseType.METADATA,
    SpectralWorkflowPhaseType.PUBLISHING,
]


class PumpkinPluginBoundaryTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # The reference LED spectrum, synthesized in -core (no calibration/profile needed: T2 never encodes
        # to an image, so the ROI/calibration round-trip T1 relies on is not in play here).
        cls.reference = LedReferenceSynthesisLogicModule().synthesize(
            LedReferenceSynthesisLogicModuleParameters()).getSpectrum()

    def __buildWorkflow(self):
        workflow = SpectralWorkflow()
        for phaseType in PHASE_ORDER:
            phase = SpectralWorkflowPhase()
            phase.setType(phaseType)
            workflow.addToPhases(phase)
        return workflow

    def __synthesizeSample(self, targetHue):
        parameters = OilSampleSynthesisLogicModuleParameters()
        parameters.setReference(self.reference)
        parameters.setTargetHue(targetHue)
        return OilSampleSynthesisLogicModule().synthesize(parameters).getSpectrum()

    def __runPlugin(self, sample):
        plugin = PumpkinOilPlugin()
        workflow = self.__buildWorkflow()

        # 1. the plugin declares its acquisition steps.
        plugin.acquisition(workflow)

        # 2. the HOST's one job, emulated: fill each declared step's container with a "captured" spectrum
        #    keyed by the step's role. Here "captured" = the synthesized ref/sample (no camera round-trip).
        captured = {REFERENCE: self.reference, SAMPLE: sample}
        acquisition = workflow.getPhase(SpectralWorkflowPhaseType.ACQUISITION)
        for step in acquisition.getSteps().values():
            role = step.getRole()
            container = SpectraContainer()
            container.addToSpectra(captured[role], role)
            step.setContainer(container)

        # 3. + 4. the plugin computes mean -> T -> A, then the colour/hue verdict.
        plugin.processing(workflow)
        plugin.evaluation(workflow)
        return workflow

    def __verdict(self, workflow):
        phase = workflow.getPhase(SpectralWorkflowPhaseType.EVALUATION)
        step = list(phase.getSteps().values())[0]
        result = step.getEvaluationResult()
        return [item for item in result.getItems() if isinstance(item, VerdictView)][0].roastState

    def test_each_demo_oil_produces_the_expected_verdict(self):
        # Same assertion as T1, driven through the plugin hooks directly instead of engine.runAll.
        for demoOil in PLAYGROUND_DEMO_OILS:
            workflow = self.__runPlugin(self.__synthesizeSample(demoOil.targetHue))
            self.assertEqual(self.__verdict(workflow), demoOil.roastState.value, demoOil.label)

    def test_evaluation_yields_a_verdict_and_two_swatches(self):
        # The plugin's EvaluationResult shape: measured + target ColorSwatchView, a hue LabelView, a VerdictView.
        from sciens.spectracs.plugin_sdk import ColorSwatchView, LabelView
        workflow = self.__runPlugin(self.__synthesizeSample(PLAYGROUND_DEMO_OILS[0].targetHue))
        phase = workflow.getPhase(SpectralWorkflowPhaseType.EVALUATION)
        items = list(phase.getSteps().values())[0].getEvaluationResult().getItems()
        self.assertEqual(sum(isinstance(i, ColorSwatchView) for i in items), 2)
        self.assertEqual(sum(isinstance(i, VerdictView) for i in items), 1)
        self.assertTrue(any(isinstance(i, LabelView) for i in items))


if __name__ == "__main__":
    unittest.main()
