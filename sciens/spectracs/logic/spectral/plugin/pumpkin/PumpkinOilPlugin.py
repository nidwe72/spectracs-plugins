import colorsys

from sciens.spectracs.plugin_sdk import (
    SpectralPlugin, SpectralWorkflowPhaseType, SpectralWorkflowStep, SpectraContainer,
    MeanOp, TransmissionOp, AbsorptionOp, VerdictOp, EvaluationColorUtil,
    EvaluationResult, ColorSwatchView, VerdictView, LabelView, SpectrumPlotView, MetadataField, CaptureView,
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
        phase.setHint("measurement complete")  # coach line once BOTH steps are captured (Edwin)
        phase.addToSteps(self.__measurementStep(REFERENCE, "Isopropanol (reference)",
                                                "Insert isopropanol and capture"))
        phase.addToSteps(self.__measurementStep(SAMPLE, "+ pumpkin oil (sample)",
                                                "select oil-tab and capture oil-dilution"))

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
        phase.setHint("You can view the measurement results here.")  # SPEC_acquisition_guidance: plugin-authored

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
        result.addItem(VerdictView(roast.value, hueDegrees=hue))

        step = SpectralWorkflowStep()
        step.setLabel("Result")
        step.setEvaluationResult(result)
        phase = workflow.getPhase(SpectralWorkflowPhaseType.EVALUATION)
        phase.setHint("The measurement has been evaluated.")  # SPEC_acquisition_guidance: plugin-authored
        phase.addToSteps(step)

    def metadata(self, workflow):
        # Plugin-declared metadata form (SPEC_workflow_persistence.md §2.3). Only `title` shows as a
        # column in the Home workflows table.
        return [
            MetadataField("title", "Title", MetadataField.TEXT, showInWorkflowsTable=True, order=0),
            MetadataField("temperature", "Roasting temperature (°C)", MetadataField.NUMBER, order=1),
            MetadataField("dateOfRoasting", "Date of roasting", MetadataField.DATE, order=2),
        ]

    # publishing: inherited pass -> 0 steps -> auto-skipped (D1)

    def __measurementStep(self, role, label, prompt):
        step = SpectralWorkflowStep()
        step.setRole(role)
        step.setLabel(label)
        step.setFrames(self.FRAMES)
        step.setMandatory(True)
        # P6: plugin-driven acquisition wording — the host reads captureLabel for the Measure button.
        # SPEC_acquisition_guidance.md P4: the per-step `prompt` is now role-specific — the host surfaces it as
        # the coach line + drives the amber next-action cue from whichever step is still uncaptured.
        step.setView(CaptureView(prompt=prompt,
                                 captureLabel="Capture " + label, geometry="transmission"))
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
