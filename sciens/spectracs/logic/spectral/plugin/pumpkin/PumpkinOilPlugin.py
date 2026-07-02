import colorsys

from sciens.spectracs.plugin_sdk import (
    SpectralPlugin, SpectralWorkflowPhaseType, SpectralWorkflowStep, SpectraContainer,
    MeanOp, TransmissionOp, AbsorptionOp, VerdictOp, EvaluationColorUtil,
    EvaluationResult, ColorSwatchView, VerdictView, LabelView, SpectrumPlotView,
    REFERENCE, SAMPLE, TRANSMISSION, ABSORPTION,
)


class PumpkinOilPlugin(SpectralPlugin):
    # Pumpkin-seed-oil colour QM (SPEC_pumpkin_integration.md C.2). One class, five hooks; imports only
    # plugin_sdk (Qt-free). ACQUISITION declares REFERENCE+SAMPLE (host fills from the virtual device);
    # PROCESSING computes mean -> transmission -> absorption; EVALUATION turns T into a colour/hue verdict.
    title = "Pumpkin-seed-oil colour QM"

    PERFECT_HUE = 60.0  # the "perfect green" target hue (degrees) — verdict bands live in VerdictOp (47/66)
    FRAMES = 5          # >=5 to satisfy the reused capture-preview gate (D14/N)

    def acquisition(self, workflow):
        phase = workflow.getPhase(SpectralWorkflowPhaseType.ACQUISITION)
        phase.addToSteps(self.__measurementStep(REFERENCE, "Isopropanol (reference)"))
        phase.addToSteps(self.__measurementStep(SAMPLE, "+ pumpkin oil (sample)"))

    def processing(self, workflow):
        acquisition = workflow.getPhase(SpectralWorkflowPhaseType.ACQUISITION)
        captured = SpectraContainer()
        for step in acquisition.getSteps().values():
            role = step.getRole()
            captured.addToSpectra(step.getContainer().getSpectra()[role], role)

        meaned = MeanOp().apply(captured)              # {reference: mean, sample: mean}
        transmission = TransmissionOp().apply(meaned)  # {transmission}
        absorption = AbsorptionOp().apply(meaned)      # {absorption}

        phase = workflow.getPhase(SpectralWorkflowPhaseType.PROCESSING)

        absorptionStep = SpectralWorkflowStep()
        absorptionStep.setLabel("Absorption")
        absorptionStep.setContainer(absorption)
        absorptionStep.setPersist(True)
        absorptionStep.setView(SpectrumPlotView(absorption.getSpectra()[ABSORPTION], "A(λ) = −log10(S/R)"))
        phase.addToSteps(absorptionStep)

        transmissionStep = SpectralWorkflowStep()  # headless carrier (no view) — feeds EVALUATION
        transmissionStep.setContainer(transmission)
        phase.addToSteps(transmissionStep)

    def evaluation(self, workflow):
        transmission = self.__findTransmission(workflow)
        rgb, hue = EvaluationColorUtil().spectrumToRgbAndHue(transmission)
        roast = VerdictOp().verdict(hue)

        result = EvaluationResult()
        result.addItem(ColorSwatchView(rgb, "measured"))
        result.addItem(ColorSwatchView(self.__targetRgb(), "target"))
        result.addItem(LabelView("hue %.0f°" % hue))
        result.addItem(VerdictView(roast.value))

        step = SpectralWorkflowStep()
        step.setLabel("Result")
        step.setEvaluationResult(result)
        workflow.getPhase(SpectralWorkflowPhaseType.EVALUATION).addToSteps(step)

    # metadata / publishing: inherited pass -> 0 steps -> auto-skipped (D1)

    def __measurementStep(self, role, label):
        step = SpectralWorkflowStep()
        step.setRole(role)
        step.setLabel(label)
        step.setFrames(self.FRAMES)
        step.setMandatory(True)
        return step

    def __findTransmission(self, workflow):
        phase = workflow.getPhase(SpectralWorkflowPhaseType.PROCESSING)
        for step in phase.getSteps().values():
            container = step.getContainer()
            if container is not None and TRANSMISSION in container.getSpectra():
                return container.getSpectra()[TRANSMISSION]
        return None

    def __targetRgb(self):
        r, g, b = colorsys.hls_to_rgb(self.PERFECT_HUE / 360.0, 0.20, 0.85)
        return (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))
