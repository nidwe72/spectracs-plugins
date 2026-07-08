from sciens.spectracs.plugin_sdk import (
    SpectralPlugin, SpectralWorkflowPhaseType, SpectralWorkflowStep, SpectraContainer,
    MeanOp, TransmissionOp, AbsorptionOp, SpectrumPlotView,
    REFERENCE, SAMPLE, TRANSMISSION, ABSORPTION,
)


class DevSpectralPlugin(SpectralPlugin):
    # Generic "Swiss-knife" plugin for the master dev measurement bench (SPEC_dev_measure_bench.md P1).
    # Same real pipeline as an end-user plugin — ACQUISITION declares REFERENCE+SAMPLE, PROCESSING runs
    # mean -> transmission -> absorption — but WITHOUT any use-case evaluation/verdict. Standalone; not
    # subclassed or shared by any other plugin. Injected transiently (no session codeRef).
    title = "Measurement bench (dev)"

    FRAMES = 20  # default burst; the bench view may override step.frames from its frame-count dropdown (D2)

    def acquisition(self, workflow):
        phase = workflow.getPhase(SpectralWorkflowPhaseType.ACQUISITION)
        phase.addToSteps(self.__measurementStep(REFERENCE, "Reference"))
        phase.addToSteps(self.__measurementStep(SAMPLE, "Sample"))

    def processing(self, workflow):
        acquisition = workflow.getPhase(SpectralWorkflowPhaseType.ACQUISITION)
        captured = SpectraContainer()
        for step in acquisition.getSteps().values():
            role = step.getRole()
            if role is None or step.getContainer() is None:
                continue
            captured.addToSpectra(step.getContainer().getSpectra()[role], role)

        meaned = MeanOp().apply(captured)              # {reference: mean, sample: mean}
        transmission = TransmissionOp().apply(meaned)  # {transmission}
        absorption = AbsorptionOp().apply(meaned)      # {absorption}

        phase = workflow.getPhase(SpectralWorkflowPhaseType.PROCESSING)

        # Spectra: reference + sample overlaid — carries the meaned container (both roles). No single-curve
        # SpectrumPlotView (the bench view overlays both roles via SpectrumPlotWidget.addTrace — N3).
        spectraStep = SpectralWorkflowStep()
        spectraStep.setLabel("Spectra")
        spectraStep.setContainer(meaned)
        phase.addToSteps(spectraStep)

        transmissionStep = SpectralWorkflowStep()
        transmissionStep.setLabel("Transmission")
        transmissionStep.setContainer(transmission)
        transmissionStep.setView(SpectrumPlotView(transmission.getSpectra()[TRANSMISSION], "T(λ) = S/R"))
        phase.addToSteps(transmissionStep)

        absorptionStep = SpectralWorkflowStep()
        absorptionStep.setLabel("Absorption")
        absorptionStep.setContainer(absorption)
        absorptionStep.setView(SpectrumPlotView(absorption.getSpectra()[ABSORPTION], "A(λ) = −log10(S/R)"))
        phase.addToSteps(absorptionStep)

    def evaluation(self, workflow):
        pass  # generic bench has no verdict -> 0 steps -> auto-skipped

    # metadata / publishing: inherited (return [] / pass) -> 0 steps -> auto-skipped

    def __measurementStep(self, role, label):
        step = SpectralWorkflowStep()
        step.setRole(role)
        step.setLabel(label)
        step.setFrames(self.FRAMES)
        step.setMandatory(True)
        return step
