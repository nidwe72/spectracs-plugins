from sciens.spectracs.plugin_sdk import (
    SpectralPlugin, SpectralWorkflowPhaseType, SpectralWorkflowStep, SpectraContainer,
    MeanOp, TransmissionOp, AbsorptionOp, SpectrumPlotView,
    EvaluationResult, LabelView, MetricFieldView, MetricFieldViewStyle, SpectrumFeatureUtil,
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

    # --- Pumpkin peak-ratio bands (HARD-CODED here for now — SPEC_pumpkin_peak_ratio_eval.md §7, Edwin #1).
    # The bench was meant generic (just absorption); it takes on these pumpkin-specifics for now. When the
    # pumpkin plugin becomes a 2nd consumer these promote to a shared feature-config (constants, not logic).
    # Also read by DevMeasurementBenchViewModule for the band-marked plot (P2).
    BLUE_BAND = (450.0, 490.0)        # browning window
    BLUE_PEAK = (450.0, 465.0)        # reference blue-peak search (the gate)
    GREEN_BAND = (510.0, 540.0)       # clarity / anchor
    Q_SEARCH = (565.0, 590.0)         # Q-band local-max search
    Q_BASELINE = (555.0, 600.0)       # Q-band baseline anchors
    GATE_FRACTION = 0.25              # keep λ where reference >= 25% of its blue peak (trims cyan dip)
    VALUE_CEILING = 1.5               # drop saturated-Soret λ (A > 1.5)
    __EPS = 1e-3

    def evaluation(self, workflow):
        # Compose the GENERIC ops (SpectrumFeatureUtil) with the pumpkin constants above → render-only
        # metrics (no calibrated verdict yet — SPEC_pumpkin_peak_ratio_eval.md §4/§10 P1). Provisional.
        absorption = self.__findRole(workflow, ABSORPTION)
        reference = self.__findRole(workflow, REFERENCE)   # the meaned "Spectra" step carries REFERENCE
        if absorption is None or reference is None:
            return  # no absorption/reference yet -> 0 steps -> phase auto-skipped
        step = SpectralWorkflowStep()
        step.setLabel("Evaluation")
        step.setEvaluationResult(self.__peakRatioResult(absorption, reference))
        workflow.getPhase(SpectralWorkflowPhaseType.EVALUATION).addToSteps(step)

    def __peakRatioResult(self, absorption, reference) -> EvaluationResult:
        util = SpectrumFeatureUtil()

        peak = util.peakInRange(absorption, *self.Q_SEARCH)                 # D_Q: local-max minus baseline
        qLambda = peak[0] if peak is not None else 575.0
        baseline = util.linearBaseline(absorption, qLambda, self.Q_BASELINE[0], self.Q_BASELINE[1])
        dQ = (peak[1] - baseline) if (peak is not None and baseline is not None) else None

        aGreen = util.bandMean(absorption, *self.GREEN_BAND)               # clarity / anchor
        aBlue, _blueKept = util.referenceGatedBand(                        # browning (reference-gated)
            absorption, reference, self.BLUE_BAND[0], self.BLUE_BAND[1],
            self.GATE_FRACTION, self.VALUE_CEILING, self.BLUE_PEAK[0], self.BLUE_PEAK[1])

        # Composition-level guards (the plugin's job, not the generic op's).
        confidence = []
        if dQ is None:
            confidence.append("Q-band baseline gap")
        if aBlue is None:
            confidence.append("blue window empty/saturated")
        if aGreen is None or aGreen < self.__EPS:
            confidence.append("green anchor ~0")

        def ratio(numerator, denominator):
            if numerator is None or denominator is None:
                return None
            return numerator / max(denominator, self.__EPS)               # near-zero denom floor

        gGreen = ratio(dQ, aGreen)
        gBlue = ratio(dQ, aBlue)
        browning = ratio(aBlue, aGreen)

        def fmt(value):
            return "—" if value is None else ("%.3f" % value)

        # G3 — metrics as Spectrometer-setup-style rows: gray label chip + read-only value field, with the
        # meaning as a click/hover tooltip on the label (SPEC §17 / peak-ratio §6).
        # Ratios cancel path·concentration (Beer-Lambert A=ε·c·l) → intrinsic to the oil regardless of how
        # strongly it is diluted; the absolute absorptions do not. Mark the ratios with a bold-label style so
        # the reader sees which numbers survive dilution (SPEC_bench_small_screen_refinements.md S5).
        dilutionInvariant = MetricFieldViewStyle.builder().labelBold(True).build()
        result = EvaluationResult()
        result.addItem(LabelView("Pumpkin-oil peak-ratio — PROVISIONAL (uncalibrated: no good/bad "
                                 "thresholds yet)"))
        result.addItem(MetricFieldView("Greenness G", fmt(gGreen),
            "D_Q ÷ A_green — headline quality index; higher = greener / fresher oil.",
            style=dilutionInvariant))
        result.addItem(MetricFieldView("Pigment D_Q", "%s @ %.0f nm" % (fmt(dQ), qLambda),
            "depth of the green-pigment Q-band — how much intact green pigment is present."))
        result.addItem(MetricFieldView("Browning A_blue", fmt(aBlue),
            "blue-region absorption — rises with roasting / Maillard browning."))
        result.addItem(MetricFieldView("Clarity A_green", fmt(aGreen),
            "green-window floor — rises with turbidity / darkening (sediment, heavy roast)."))
        result.addItem(MetricFieldView("Browning ratio", fmt(browning),
            "A_blue ÷ A_green — the roast axis, isolated from pigment; higher = more browned.",
            style=dilutionInvariant))
        result.addItem(MetricFieldView("G' (alt.)", fmt(gBlue),
            "D_Q ÷ A_blue — browning-sensitive denominator (fragile on this rig).",
            style=dilutionInvariant))
        if confidence:
            result.addItem(LabelView("⚠ low confidence: " + ", ".join(confidence)))
        return result

    def __findRole(self, workflow, role):
        # The meaned REFERENCE lives in the PROCESSING "Spectra" step; ABSORPTION in the absorption step.
        phase = workflow.getPhase(SpectralWorkflowPhaseType.PROCESSING)
        for step in phase.getSteps().values():
            container = step.getContainer()
            if container is not None and role in container.getSpectra():
                return container.getSpectra()[role]
        return None

    # metadata / publishing: inherited (return [] / pass) -> 0 steps -> auto-skipped

    def __measurementStep(self, role, label):
        step = SpectralWorkflowStep()
        step.setRole(role)
        step.setLabel(label)
        step.setFrames(self.FRAMES)
        step.setMandatory(True)
        return step
