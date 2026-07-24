from sciens.spectracs.plugin_sdk import (
    SpectralPlugin, SpectralWorkflowPhaseType, SpectralWorkflowStep, SpectraContainer,
    MeanOp, TransmissionOp, AbsorptionOp, BaselineOffsetOp, MedianFilterOp,
    SpectrumPlotView, CaptureView, SpectrumCaptureView, ReportView,
    LimsPublishView, GaugeRender,
    EvaluationResult, LabelView, MetricFieldView, MetricFieldViewStyle, SpectrumFeatureUtil,
    EvaluationColorUtil, MetadataField,
    NavigationMode, NavigationPolicy, WorkflowPolicy,
    REFERENCE, SAMPLE, TRANSMISSION, ABSORPTION,
)
from sciens.spectracs.plugins.dev.RoastGaugeView import RoastGaugeView


class DevSpectralPlugin(SpectralPlugin):
    # Generic "Swiss-knife" plugin for the master dev measurement bench (SPEC_dev_measure_bench.md P1).
    # Same real pipeline as an end-user plugin — ACQUISITION declares REFERENCE+SAMPLE, PROCESSING runs
    # mean -> transmission -> absorption — but WITHOUT any use-case evaluation/verdict. Standalone; not
    # subclassed or shared by any other plugin. Injected transiently (no session codeRef).
    title = "Measurement bench (dev)"

    # SPEC_simplified_plugin_navigation.md (M3) — the "should-be" end-user-mirroring presentation, driven ENTIRELY
    # by the plugin. TEMPORARY test toggle: flip to False to run the old step-through navigation for a regression
    # check; removed once verified. Selects, together: auto-advance nav + per-step (Reference/Sample) chevrons +
    # the cropped-ROI capture preview. The METADATA phase is declared unconditionally (it is a permanent addition).
    SIMPLIFIED_NAVIGATION = True

    FRAMES = 150  # burst per capture (Edwin 2026-07-15): the bench averages 150 frames for a cleaner spectrum.
    # CapturePanel seeds its frame count from this declared value; the frame-count dropdown (when shown) overrides.

    def declaredEvalBands(self):
        # Every wavelength band this plugin's evaluation reads — the capture window (§9 M1) must cover them all,
        # else clamping would starve an eval band. The host / assertion reads this generically.
        return [self.BLUE_BAND, self.BLUE_PEAK, self.GREEN_BAND, self.Q_SEARCH, self.Q_BASELINE,
                self.PB_SORET_BAND, self.PB_Q_BAND]

    def __assertWindowCoversBands(self):
        lo, hi = self.WAVELENGTH_MIN_NM, self.WAVELENGTH_MAX_NM
        for bandLo, bandHi in self.declaredEvalBands():
            if bandLo < lo or bandHi > hi:
                raise ValueError(
                    "SPEC_capture_quality.md §9 (M1): capture window [%g, %g] nm does not cover declared eval "
                    "band [%g, %g] — clamping would starve it." % (lo, hi, bandLo, bandHi))

    def policy(self):
        # Cross-cutting flow policy (M3). should-be = AUTO_ADVANCE + Reference/Sample chevrons; else today's STEP.
        if self.SIMPLIFIED_NAVIGATION:
            return WorkflowPolicy(navigation=NavigationPolicy(
                NavigationMode.AUTO_ADVANCE, stepChevronPhases={SpectralWorkflowPhaseType.ACQUISITION}))
        return WorkflowPolicy.default()

    def metadata(self, workflow):
        # Change C — the METADATA form, taken over from PumpkinOilPlugin. The user lands here after measuring
        # (the auto-advance jump halts on the required form), then Next -> PUBLISHING.
        return [
            MetadataField("title", "Title", MetadataField.TEXT, showInWorkflowsTable=True, order=0),
            MetadataField("temperature", "Roasting temperature (°C)", MetadataField.NUMBER, order=1),
            MetadataField("dateOfRoasting", "Date of roasting", MetadataField.DATE, order=2),
        ]

    def acquisition(self, workflow):
        self.__assertWindowCoversBands()  # fail loud at build time if the clamp window can't feed the eval bands
        phase = workflow.getPhase(SpectralWorkflowPhaseType.ACQUISITION)
        phase.setHint("measurement complete")  # coach line once BOTH steps are captured (Edwin)
        phase.addToSteps(self.__measurementStep(REFERENCE, "Reference", "Insert isopropanol and capture"))
        phase.addToSteps(self.__measurementStep(SAMPLE, "Sample", "Insert the oil dilution and capture"))

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
        phase.setHint("You can view the measurement results here.")  # SPEC_acquisition_guidance: plugin-authored

        # Spectra: reference + sample overlaid. P5: the overlay is now DECLARED as a multi-trace
        # SpectrumPlotView (was host-drawn via SpectrumPlotWidget.addTrace) — the host renders it generically.
        # M2 (Edwin): the PROCESSING Reference-vs-Sample overlay is also rendered in the PDF report.
        spectraStep = SpectralWorkflowStep()
        spectraStep.setLabel("Spectra")
        spectraStep.setContainer(meaned)
        spectraStep.setView(SpectrumPlotView(title="Reference vs Sample")
                            .addTrace(meaned.getSpectra()[REFERENCE], "Reference", "c")
                            .addTrace(meaned.getSpectra()[SAMPLE], "Sample", "y")
                            .setShownInReport(True))
        phase.addToSteps(spectraStep)

        transmissionStep = SpectralWorkflowStep()
        transmissionStep.setLabel("Transmission")
        transmissionStep.setContainer(transmission)
        transmissionStep.setView(SpectrumPlotView(transmission.getSpectra()[TRANSMISSION], "T(λ) = S/R"))
        phase.addToSteps(transmissionStep)

        absorptionStep = SpectralWorkflowStep()
        absorptionStep.setLabel("Absorption")
        absorptionStep.setContainer(absorption)
        # M2 (SPEC_bench_pdf_export.md §3): flag the PROCESSING absorption curve into the PDF report. Canonical
        # example — it is shown HERE (PROCESSING) and appears in the report, but never in the EVALUATION GUI.
        absorptionStep.setView(SpectrumPlotView(absorption.getSpectra()[ABSORPTION], "A(λ) = −log10(S/R)")
                               .setShownInReport(True))
        phase.addToSteps(absorptionStep)

        # SPEC_capability_proof.md §7.0.1/§8.2: the processing ladder made VISIBLE — three COLOURED traces, raw
        # (grey) -> de-spiked (orange, narrow instrument spikes removed) -> baseline-corrected (green, + flat-offset).
        # SpectrumPlotView carries no linestyle, so colour distinguishes them; the hex/name colours are valid in
        # BOTH renderers (pyqtgraph mkPen rejects matplotlib greys like "0.6").
        absorptionRaw = absorption.getSpectra()[ABSORPTION]
        despiked = self.__despikedAbsorption(absorptionRaw)
        ladderStep = SpectralWorkflowStep()
        ladderStep.setLabel("Absorption (raw / despiked / baseline-corrected)")
        ladderStep.setView(SpectrumPlotView(title="A(λ) — raw → despiked → baseline-corrected (flat-offset)")
                           .addTrace(absorptionRaw, "A raw", "#888888")
                           .addTrace(despiked, "A despiked", "#e08000")
                           .addTrace(self.__baselineCorrectedAbsorption(despiked), "A despiked + baseline", "g")
                           .setShownInReport(True))
        phase.addToSteps(ladderStep)

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

    # --- PB literature bands (SPEC_capability_proof.md §2.1 / SPEC_pumpkin_peak_ratio_eval.md §1b.1, V3, Edwin
    # 2026-07-22). The "Evaluation (new)" tab reads these NEW, literature-anchored windows as plain band MEANS
    # (NOT the legacy peak-ratio machinery): the 440-460 Soret right-hand slope and the 560-580 Q-band, with the
    # shared 510-540 clarity floor (= GREEN_BAND) as the stable-denominator comparison. Both new windows sit
    # inside the 440-630 capture clamp, so no window change (they are added to declaredEvalBands below).
    PB_SORET_BAND = (440.0, 460.0)    # Soret right-hand slope = green-pigment blue absorption
    PB_Q_BAND = (560.0, 580.0)        # green-pigment Q-band

    # Hue-normalized colour chips: a fixed, CALM S/L so only HUE varies between oils (SPEC_capability_proof.md §5,
    # "C scheme" — Edwin 2026-07-22). Lowered from the original vivid 80/50 to a darker, less "popping" pair.
    __NORM_SATURATION = 38.0
    __NORM_LIGHTNESS = 34.0

    # SPEC_capture_quality.md §9 (M1) / SPEC_capability_proof.md §7.0.2: the CFL lamp illuminates usefully only
    # ~440–630 nm. The host HARD-CLAMPS the captured ROI to this window (via CaptureView.wavelengthMin/MaxNm) so
    # the dead margins never enter the stored spectrum (they'd only feed the S/R floor-guard garbage). Must ⊇
    # every declaredEvalBand() below — asserted in acquisition() — including the incoming PB blue band at 440.
    WAVELENGTH_MIN_NM = 440.0
    WAVELENGTH_MAX_NM = 630.0

    def evaluation(self, workflow):
        # Compose the GENERIC ops (SpectrumFeatureUtil) with the pumpkin constants above → render-only
        # metrics (no calibrated verdict yet — SPEC_pumpkin_peak_ratio_eval.md §4/§10 P1). Provisional.
        absorption = self.__findRole(workflow, ABSORPTION)
        reference = self.__findRole(workflow, REFERENCE)   # the meaned "Spectra" step carries REFERENCE
        transmission = self.__findRole(workflow, TRANSMISSION)  # feeds the perceived-colour swatch row
        if absorption is None or reference is None:
            return  # no absorption/reference yet -> 0 steps -> phase auto-skipped
        # P4: EVALUATION declares TWO plugin steps — Metrics (the EvaluationResult) and Spectrum (the band-marked
        # absorption plot). The host renders each as a step-tab; the band shading (was host-drawn in the bench's
        # __absorptionBandsPlot) is now the plugin's declared SpectrumPlotView bands.
        phase = workflow.getPhase(SpectralWorkflowPhaseType.EVALUATION)
        phase.setHint("The measurement has been evaluated.")  # SPEC_acquisition_guidance: plugin-authored
        metricsStep = SpectralWorkflowStep()
        metricsStep.setLabel("Metrics")
        metricsResult = self.__peakRatioResult(absorption, reference, transmission)
        # M2: flag the evaluation metrics into the PDF report (the verdict numbers a reader wants on paper).
        for item in metricsResult.getItems():
            item.setShownInReport(True)
        metricsStep.setEvaluationResult(metricsResult)
        phase.addToSteps(metricsStep)
        # Q-peak dashed marker (restored from the pre-P4 host-drawn plot): the local-max λ in the Q search band.
        peak = SpectrumFeatureUtil().peakInRange(absorption, *self.Q_SEARCH)
        qLambda = peak[0] if peak is not None else 575.0
        # M2 (Edwin): the EVALUATION band-marked absorption spectrum is also rendered in the PDF report.
        spectrumStep = SpectralWorkflowStep()
        spectrumStep.setLabel("Spectrum")
        spectrumStep.setView(SpectrumPlotView(absorption, title="A(λ) — bands")
                             .addBand(*self.BLUE_BAND).addBand(*self.GREEN_BAND).addBand(*self.Q_SEARCH)
                             .addMarker(qLambda, "Q").setShownInReport(True))
        phase.addToSteps(spectrumStep)

        # V3 (SPEC_capability_proof.md §2.1): a SECOND, forward-looking evaluation view — the PB literature bands
        # (440-460 Soret / 560-580 Q) read as plain band MEANS on the DESPIKED absorbance, the pigment ratio, and
        # the 10 colour chips DUPLICATED here at the calm C-scheme S/L. The legacy "Metrics" tab is left fully
        # intact so the old-band numbers stay directly comparable — a tab-vs-tab "eureka" comparison (Edwin).
        despikedAbsorption = self.__despikedAbsorption(absorption)
        newStep = SpectralWorkflowStep()
        newStep.setLabel("Evaluation (new)")
        newResult = self.__newEvaluationResult(despikedAbsorption, transmission, absorption)
        for item in newResult.getItems():
            item.setShownInReport(True)
        newStep.setEvaluationResult(newResult)
        phase.addToSteps(newStep)

        # A SECOND version of the A(λ) spectrum with the NEW bands marked (Edwin): the PB Soret + Q windows plus
        # the shared 510-540 clarity floor shaded, Q local-max marked — on the same despiked curve as the metrics.
        newPeak = SpectrumFeatureUtil().peakInRange(despikedAbsorption, *self.PB_Q_BAND)
        newQLambda = newPeak[0] if newPeak is not None else 570.0
        newSpectrumStep = SpectralWorkflowStep()
        newSpectrumStep.setLabel("Spectrum (new)")
        newSpectrumStep.setView(SpectrumPlotView(despikedAbsorption, title="A(λ) — PB bands (despiked)")
                                .addBand(*self.PB_SORET_BAND).addBand(*self.GREEN_BAND).addBand(*self.PB_Q_BAND)
                                .addMarker(newQLambda, "Q").setShownInReport(True))
        phase.addToSteps(newSpectrumStep)

        # M2 (SPEC_bench_pdf_export.md §1): declare a Report step. Its ReportView surfaces as a tab in EVALUATION
        # (beside Metrics | Spectrum) whose body the host renders with matplotlib (a preview that IS the PDF) +
        # a Save action. The body is NOT listed here — it is the isShownInReport-flagged content across phases
        # (the acquisition captures, the PROCESSING absorption, the metrics above). No ReportView → no tab.
        username = getattr(workflow, "username", None)
        reportStep = SpectralWorkflowStep()
        reportStep.setLabel("Report")
        reportStep.setView(ReportView(title="Measurement bench report",
                                      subtitle=("Operator: %s" % username) if username else self.title,
                                      embedMetadata=True))
        phase.addToSteps(reportStep)

    def publishing(self, workflow):
        # L6 (SPEC_lims_integration.md §3): declare a PUBLISHING "Send to LIMS" step. Its LimsPublishView
        # carries only the plugin-owned facts — the target LIMS + this sample's type + analyses. The host
        # renders a Publish button; on click it builds the M2 PDF and calls the server publish RPC (the client
        # never talks to the LIMS). M1 = data upload → a single generic analysis; the per-metric analyses are a
        # later LIMS-side concern.
        phase = workflow.getPhase(SpectralWorkflowPhaseType.PUBLISHING)
        phase.setHint("Send the result to the laboratory if you want.")  # SPEC_acquisition_guidance: plugin-authored
        step = SpectralWorkflowStep()
        step.setLabel("Send to LIMS")
        # SPEC_roast_ampel.md §8.6 — the end-user headline: show the verdict badge on the publish step, above the
        # publish button. The badge is one more view-model in the step's item list (LABEL + SWATCH render).
        pigmentRatio = self.__pigmentRatio(workflow)
        if pigmentRatio is not None:
            # SPEC_roast_ampel.md §8.4 Option B — the LIMS headline: a big verdict pill + a coarse green|red zone
            # bar, NO fine band and NO number (D-lims-number), so it reads as a stable verdict at a glance.
            badge = EvaluationResult()
            badge.addItem(RoastGaugeView(pigmentRatio, render=GaugeRender.LABEL | GaugeRender.ZONES))
            step.setEvaluationResult(badge)
        step.setView(LimsPublishView(
            title="Send to LIMS",
            sampleTypeName="Pumpkin Oil", sampleTypeCode="OIL",
            analyses=[{"name": "Spectracs Measurement", "key": "SpectracsMeasurement",
                       "group": "Spectroscopy"}],
            backend="senaite", configKey="SENAITE"))
        phase.addToSteps(step)

    def __pigmentRatio(self, workflow):
        # SPEC_roast_ampel.md §8.6 — the Soret/Q pigment ratio on the despiked absorbance, the same value the
        # Evaluation (new) gauge shows. Recomputed here (publishing() gets the workflow) so the phase hooks stay
        # independent — cheap, deterministic, no cross-phase stashing.
        absorption = self.__findRole(workflow, ABSORPTION)
        if absorption is None:
            return None
        despiked = self.__despikedAbsorption(absorption)
        util = SpectrumFeatureUtil()
        soret = util.bandMean(despiked, *self.PB_SORET_BAND)
        qBand = util.bandMean(despiked, *self.PB_Q_BAND)
        if soret is None or qBand is None:
            return None
        return soret / max(qBand, self.__EPS)

    def __computeMetrics(self, absorption, reference):
        # Pure peak-ratio computation on ONE absorbance spectrum. Called twice by __peakRatioResult — once on the
        # raw absorbance, once on the flat-offset + light-SG "improved" absorbance — so every metric gets a paired
        # raw / improved readout with identical machinery (UC1, SPEC_capability_proof.md §7.0.1). Returns None if
        # there is no absorbance yet.
        if absorption is None:
            return None
        util = SpectrumFeatureUtil()
        peak = util.peakInRange(absorption, *self.Q_SEARCH)                 # D_Q: local-max minus a LOCAL baseline
        qLambda = peak[0] if peak is not None else 575.0
        # linearBaseline draws a straight line between the two Q_BASELINE anchors and reads it at qLambda; D_Q is the
        # peak height ABOVE that local line. A flat offset b lifts peak and line equally, so D_Q is already b-immune
        # (which is why its improved twin barely moves) — SPEC_capability_proof.md §7.0.1.
        baseline = util.linearBaseline(absorption, qLambda, self.Q_BASELINE[0], self.Q_BASELINE[1])
        dQ = (peak[1] - baseline) if (peak is not None and baseline is not None) else None

        aGreen = util.bandMean(absorption, *self.GREEN_BAND)               # clarity / anchor (absolute → carries b)
        aBlue, _blueKept = util.referenceGatedBand(                        # browning (reference-gated; absolute → b)
            absorption, reference, self.BLUE_BAND[0], self.BLUE_BAND[1],
            self.GATE_FRACTION, self.VALUE_CEILING, self.BLUE_PEAK[0], self.BLUE_PEAK[1])

        def ratio(numerator, denominator):
            if numerator is None or denominator is None:
                return None
            return numerator / max(denominator, self.__EPS)               # near-zero denom floor

        return {"dQ": dQ, "qLambda": qLambda, "aGreen": aGreen, "aBlue": aBlue,
                "gGreen": ratio(dQ, aGreen), "gBlue": ratio(dQ, aBlue), "browning": ratio(aBlue, aGreen)}

    def __peakRatioResult(self, absorption, reference, transmission=None) -> EvaluationResult:
        # The processing ladder, once (single source of truth): raw → de-spike → (colour only) flat-offset + SG.
        # METRICS use raw + DE-SPIKED (the flat-offset degrades the small band means — oilH, SPEC §7.0.1); COLOUR
        # uses raw + IMPROVED (de-spike + flat-offset + light SG). De-spiking removes the narrow instrument spikes
        # (blue-pump edge, registration) that are not oil.
        despiked = self.__despikedAbsorption(absorption)
        despikedBaseline = self.__baselineCorrectedAbsorption(despiked)
        raw = self.__computeMetrics(absorption, reference)
        despikedMetrics = self.__computeMetrics(despiked, reference)

        # Composition-level guards (the plugin's job, not the generic op's) — read from the raw (primary) set.
        confidence = []
        if raw is not None:
            if raw["dQ"] is None:
                confidence.append("Q-band baseline gap")
            if raw["aBlue"] is None:
                confidence.append("blue window empty/saturated")
            if raw["aGreen"] is None or raw["aGreen"] < self.__EPS:
                confidence.append("green anchor ~0")

        def fmt(value):
            return "—" if value is None else ("%.3f" % value)

        def scalar(metrics, key):
            return fmt(metrics[key]) if metrics is not None else "—"

        def dQtext(metrics):
            if metrics is None or metrics["dQ"] is None:
                return "— @ — nm"
            return "%s @ %.0f nm" % (fmt(metrics["dQ"]), metrics["qLambda"])

        # G3 — metrics as Spectrometer-setup-style rows: gray label chip + read-only value field, with the
        # meaning as a click/hover tooltip on the label (SPEC §17 / peak-ratio §6).
        # Ratios cancel path·concentration (Beer-Lambert A=ε·c·l) → intrinsic to the oil regardless of how
        # strongly it is diluted; the absolute absorptions do not. Mark the ratios with a bold-label style so
        # the reader sees which numbers survive dilution (SPEC_bench_small_screen_refinements.md S5).
        dilutionInvariant = MetricFieldViewStyle.builder().labelBold(True).build()
        result = EvaluationResult()
        result.addItem(LabelView("Pumpkin-oil peak-ratio — PROVISIONAL (uncalibrated: no good/bad "
                                 "thresholds yet)"))
        # Colour chips (SPEC_color_retrieval.md + capability_proof §7.0.1/§8.2): the 10-variant set — intrinsic
        # (absorbance) then intrinsic-perceived (+180° complement) then perceived (transmission). Each intrinsic
        # family: natural, hue-norm, · despiked, · despiked + baseline; perceived: natural + hue-norm. The processed
        # rungs are hue-normalized so only HUE moves. Each aligns in the shared metric grid.
        colourChips = self.__colourChips(transmission, absorption, despiked, despikedBaseline)
        if colourChips:
            result.addItem(LabelView("Colour — processed variants (despiked, baseline) are hue-normalized"))
            for chip in colourChips:
                result.addItem(chip)
        # Every metric renders as a raw row + a "· despiked" twin (recomputed on the DE-SPIKED absorbance — narrow
        # instrument spikes removed), the same twin convention as the colour chips (UC1). Flat-offset is NOT applied
        # to the metrics (it degrades the small band means — oilH). D_Q barely moves (it already uses a local
        # baseline, see __computeMetrics); A_blue drops where the ~473 blue-pump spike used to inflate it.
        self.__pairMetric(result, "Greenness G", scalar(raw, "gGreen"), scalar(despikedMetrics, "gGreen"),
            "D_Q ÷ A_green — headline quality index; higher = greener / fresher oil.", dilutionInvariant)
        self.__pairMetric(result, "Pigment D_Q", dQtext(raw), dQtext(despikedMetrics),
            "depth of the green-pigment Q-band — how much intact green pigment is present.")
        self.__pairMetric(result, "Soret A_blue", scalar(raw, "aBlue"), scalar(despikedMetrics, "aBlue"),
            "blue Soret-region absorption (450–490, legacy band) — tracks the green-pigment Soret band, "
            "not browning (renamed from 'Browning A_blue'; §11 found the direction inverted).")
        self.__pairMetric(result, "Clarity A_green", scalar(raw, "aGreen"), scalar(despikedMetrics, "aGreen"),
            "green-window floor — rises with turbidity / darkening (sediment, heavy roast).")
        self.__pairMetric(result, "Pigment ratio · legacy", scalar(raw, "browning"), scalar(despikedMetrics, "browning"),
            "A_blue ÷ A_green-clarity on the LEGACY bands (450–490 / 510–540) — the §11 discriminator, kept for "
            "continuity; higher = more intact pigment (renamed from 'Browning ratio', which read inverted).",
            dilutionInvariant)
        self.__pairMetric(result, "G' (alt.)", scalar(raw, "gBlue"), scalar(despikedMetrics, "gBlue"),
            "D_Q ÷ A_blue — browning-sensitive denominator (fragile on this rig).", dilutionInvariant)
        if confidence:
            result.addItem(LabelView("⚠ low confidence: " + ", ".join(confidence)))
        return result

    def __newEvaluationResult(self, despikedAbsorption, transmission, rawAbsorption) -> EvaluationResult:
        # V3 (SPEC_capability_proof.md §2.1) — the "Evaluation (new)" tab. The PB literature bands read as plain
        # band MEANS (not the legacy peak-ratio machinery) on the DESPIKED absorbance. MEAN, not integral (SPEC §9,
        # Edwin 2026-07-22): the two 20-nm bands make the Soret/Q ratio identical either way, and means keep the
        # same unit + cross-tab comparability as the legacy A_blue/A_green — an integral would inject a bandwidth
        # factor into the unequal-width Soret/clarity comparison. Emits: the three band means, the pigment ratio
        # (Soret/Q = primary), a Soret/clarity safety net (stable denominator), and the 10 colour chips duplicated.
        util = SpectrumFeatureUtil()
        soret = util.bandMean(despikedAbsorption, *self.PB_SORET_BAND) if despikedAbsorption is not None else None
        qBand = util.bandMean(despikedAbsorption, *self.PB_Q_BAND) if despikedAbsorption is not None else None
        clarity = util.bandMean(despikedAbsorption, *self.GREEN_BAND) if despikedAbsorption is not None else None

        def fmt(value):
            return "—" if value is None else ("%.3f" % value)

        def ratio(numerator, denominator):
            if numerator is None or denominator is None:
                return None
            return numerator / max(denominator, self.__EPS)

        dilutionInvariant = MetricFieldViewStyle.builder().labelBold(True).build()
        result = EvaluationResult()
        # SPEC_roast_ampel.md §8.5 — the Roast Ampel gauge is the FIRST item of this tab (gradient band + marker +
        # verdict pill + value-on-swatch), driven by the same Soret/Q pigment ratio the metric row below shows.
        pigmentRatio = ratio(soret, qBand)
        if pigmentRatio is not None:
            result.addItem(RoastGaugeView(pigmentRatio,
                                          render=GaugeRender.BAND | GaugeRender.LABEL | GaugeRender.SWATCH))
        # Full 10-variant colour set, DUPLICATED from the legacy tab (identical builder → identical chips), at the
        # calm C-scheme S/L. Placed first so the eye lands on colour, then the numbers. (Header labels removed
        # 2026-07-24, Edwin — the gauge row + the metric rows are self-explanatory.)
        colourChips = self.__colourChips(transmission, rawAbsorption, despikedAbsorption,
                                         self.__baselineCorrectedAbsorption(despikedAbsorption))
        if colourChips:
            for chip in colourChips:
                result.addItem(chip)
        result.addItem(MetricFieldView("Soret · 440–460 nm", fmt(soret),
            "mean absorbance over the 440–460 nm Soret right-hand slope (green-pigment blue absorption)."))
        result.addItem(MetricFieldView("Q · 560–580 nm", fmt(qBand),
            "mean absorbance over the 560–580 nm green-pigment Q-band."))
        result.addItem(MetricFieldView("Clarity · 510–540 nm", fmt(clarity),
            "mean absorbance over the 510–540 nm clarity floor (turbidity / darkening); the shared denominator."))
        result.addItem(MetricFieldView("Pigment ratio", fmt(ratio(soret, qBand)),
            "Soret ÷ Q — the two green-pigment bands; dilution-invariant (both scale with concentration). Primary "
            "discriminator candidate. ⚠ the Q band is weak on this rig (§13/F7) — watch its run-to-run stability.",
            style=dilutionInvariant))
        result.addItem(MetricFieldView("Pigment ratio · clarity", fmt(ratio(soret, clarity)),
            "Soret ÷ clarity-floor on the NEW bands (440–460 / 510–540) — the stable-denominator safety net for "
            "the pigment ratio; dilution-invariant.", style=dilutionInvariant))
        return result

    def __pairMetric(self, result, label, rawText, despikedText, tooltip, style=None):
        # A metric row plus its "· despiked" twin (median de-spiked absorbance), mirroring the colour-chip twin
        # convention so the whole EVALUATION reads consistently (UC1).
        result.addItem(MetricFieldView(label, rawText, tooltip, style=style))
        result.addItem(MetricFieldView(label + " · despiked", despikedText,
            tooltip + "  [median de-spiked — removes narrow instrument spikes]", style=style))

    def __despikedAbsorption(self, absorption):
        # De-spike (median, small kernel): removes narrow INSTRUMENT spikes — the lamp blue-pump edge (~473 nm) and
        # the registration artifact (~607 nm) — while leaving the broad oil bands intact (SPEC_capability_proof.md
        # §7.0.1). Non-destructive; the raw absorbance stays intact for the raw rows/chip/plot that share it.
        if absorption is None:
            return None
        container = SpectraContainer()
        container.addToSpectra(absorption, ABSORPTION)
        return MedianFilterOp(kernelSize=7).apply(container).getSpectra()[ABSORPTION]

    def __baselineCorrectedAbsorption(self, despikedAbsorption):
        # Colour-only baseline correction: flat-offset (deep-red anchor-mean floor) on the ALREADY de-spiked
        # absorbance. Removes the additive b that SHIFTS the ABSORBED chromaticity (SPEC §7.0.1). COLOUR-ONLY — it
        # degrades the small band-mean metrics (oilH), so metrics use raw + de-spiked. NO smoothing (a near-no-op
        # for chromaticity — colour is a spectral integral; Edwin). Non-destructive (the op deep-copies).
        if despikedAbsorption is None:
            return None
        container = SpectraContainer()
        container.addToSpectra(despikedAbsorption, ABSORPTION)
        return BaselineOffsetOp().apply(container).getSpectra()[ABSORPTION]

    def __colourChips(self, transmission, absorption, despikedAbsorption=None, despikedBaselineAbsorption=None):
        # SPEC_color_retrieval.md §1 + capability_proof §7.0.1/§8.2 — the 10-variant colour set. Absorbance-derived
        # colours use the sRGB converter (full gamut, no Philips-Hue clamp) with a ceiling so a T→0 spike can't
        # dominate; transmission uses rgbxy (verdict-compatible). Each returns measured (h,s,l) deg/%. Three intrinsic
        # processing rungs — raw, de-spiked, de-spiked+baseline — shown hue-normalized (fixed S/L) so only HUE moves;
        # plus a natural (measured S/L) chip. Only the ABSORBED colours get the correction rungs (an additive b is a
        # chromaticity SHIFT for absorbed, invariant for perceived). Order: intrinsic → intrinsic-perceived (+180°
        # complement into the green-yellow-brown family) → perceived (transmission).
        util = EvaluationColorUtil()

        def hsl(spectrum, converter, ceiling=None):
            return util.spectrumToHsl(spectrum, converter=converter, ceiling=ceiling) if spectrum is not None else None

        hslAbsorb = hsl(absorption, "srgb", 3.0)
        hslDespiked = hsl(despikedAbsorption, "srgb", 3.0)
        hslBaseline = hsl(despikedBaselineAbsorption, "srgb", 3.0)
        hslPerceive = hsl(transmission, "rgbxy")

        # Intrinsic-perceived = the COLORIMETRIC complement of the absorbed colour (SPEC_capability_proof.md option
        # (b), 2026-07-22): reflect the absorbed chromaticity through the D65 white point — NOT a +180° HSL hue flip
        # (which lands ~34° off the true perceived hue; the white-point complement is ~4° on K/L/M/N). One per
        # absorbance rung, so the processed twins stay meaningful.
        def complement(spectrum):
            return util.complementViaWhitePoint(spectrum, ceiling=3.0) if spectrum is not None else None
        hslIntrinsicPerceived = complement(absorption)
        hslIPDespiked = complement(despikedAbsorption)
        hslIPBaseline = complement(despikedBaselineAbsorption)
        chips = [
            # intrinsic (absorbance)
            self.__chip(util, "Intrinsic", hslAbsorb, normalized=False,
                tooltip="colorAbsorbed — literal CIE colour of the absorbance at measured S/L (dilution-invariant hue; reads blue-violet)."),
            self.__chip(util, "Intrinsic · hue-norm", hslAbsorb, normalized=True,
                tooltip="colorAbsorbed hue at fixed S/L (hue-normalized)."),
            self.__chip(util, "Intrinsic · despiked", hslDespiked, normalized=True,
                tooltip="colorAbsorbed hue after median de-spike (narrow instrument spikes removed), hue-normalized."),
            self.__chip(util, "Intrinsic · despiked + baseline", hslBaseline, normalized=True,
                tooltip="colorAbsorbed hue after de-spike then flat-offset baseline (additive b removed), hue-normalized."),
            # intrinsic-perceived (colorimetric complement — reflect absorbed chromaticity through D65 white)
            self.__chip(util, "Intrinsic-perceived", hslIntrinsicPerceived, normalized=False,
                tooltip="colorIntrinsicPerceived — the perceived-family colour, as the colorimetric complement of the absorbed colour (absorbed chromaticity reflected through the D65 white point). Dilution-invariant; ~4° from the true perceived hue (vs ~34° for the old +180° flip)."),
            self.__chip(util, "Intrinsic-perceived · hue-norm", hslIntrinsicPerceived, normalized=True,
                tooltip="colorIntrinsicPerceived at fixed S/L (hue-normalized)."),
            self.__chip(util, "Intrinsic-perceived · despiked", hslIPDespiked, normalized=True,
                tooltip="colorIntrinsicPerceived after median de-spike, hue-normalized."),
            self.__chip(util, "Intrinsic-perceived · despiked + baseline", hslIPBaseline, normalized=True,
                tooltip="colorIntrinsicPerceived after de-spike then flat-offset baseline, hue-normalized."),
            # perceived (transmission)
            self.__chip(util, "Perceived", hslPerceive, normalized=False,
                tooltip="colorPerceived — what the oil looks like at this dilution (moves with concentration)."),
            self.__chip(util, "Perceived · hue-norm", hslPerceive, normalized=True,
                tooltip="colorPerceived hue at fixed S/L (hue-normalized)."),
        ]
        return [chip for chip in chips if chip is not None]

    def __chip(self, util, label, hsl, normalized, tooltip):
        # Build one colour chip (a MetricFieldView carrying swatch + HSL text). F13: skip when the source spectrum
        # is missing. F10: a near-grey source has a meaningless hue → grey chip + "achromatic", never a fake colour.
        # The intrinsic-perceived complement is now computed upstream (EvaluationColorUtil.complementViaWhitePoint),
        # so the chip no longer applies a hue offset of its own.
        if hsl is None:
            return None
        hue, saturation, lightness = hsl
        if util.chroma(saturation, lightness) < EvaluationColorUtil.ACHROMATIC_CHROMA:
            return MetricFieldView(label, value="achromatic / undefined", tooltip=tooltip, color=(128, 128, 128))
        hue = hue % 360.0
        if normalized:
            rgb = util.rgbFromHsl(hue, self.__NORM_SATURATION, self.__NORM_LIGHTNESS)
            text = "H %.0f° · S %.0f%% · L %.0f%%" % (hue, self.__NORM_SATURATION, self.__NORM_LIGHTNESS)
        else:
            rgb = util.rgbFromHsl(hue, saturation, lightness)
            text = "H %.0f° · S %.0f%% · L %.0f%%" % (hue, saturation, lightness)
        return MetricFieldView(label, value=text, tooltip=tooltip, color=rgb)

    def __findRole(self, workflow, role):
        # The meaned REFERENCE lives in the PROCESSING "Spectra" step; ABSORPTION in the absorption step.
        phase = workflow.getPhase(SpectralWorkflowPhaseType.PROCESSING)
        for step in phase.getSteps().values():
            container = step.getContainer()
            if container is not None and role in container.getSpectra():
                return container.getSpectra()[role]
        return None

    # metadata / publishing: inherited (return [] / pass) -> 0 steps -> auto-skipped

    def __measurementStep(self, role, label, prompt):
        step = SpectralWorkflowStep()
        step.setRole(role)
        step.setLabel(label)
        step.setFrames(self.FRAMES)
        step.setMandatory(True)
        # P6: declare the acquisition capture SHELL (prompt/label/geometry). The host owns the camera; the bench
        # currently still drives capture through its own panel (TODO P6 full capture-path migration). The
        # frame-count + exposure/auto-exposure controls stay HIDDEN (the default) — auto-exposure runs under the
        # hood; the plugin can opt them in via setShowFramesControl/setShowExposureControls when needed.
        # SPEC_acquisition_guidance.md P4: `prompt` is now role-specific (Reference vs Sample).
        step.setView(CaptureView(prompt=prompt,
                                 captureLabel="Capture " + label.lower(), geometry="transmission",
                                 wavelengthMinNm=self.WAVELENGTH_MIN_NM, wavelengthMaxNm=self.WAVELENGTH_MAX_NM,
                                 croppedPreview=self.SIMPLIFIED_NAVIGATION))  # Change A: cropped-ROI live preview
        # M2 (SPEC_bench_pdf_export.md §5b): declare that this role's captured frame belongs in the PDF report
        # (cropped to the ROI). The plugin declares presence + flag; the HOST fills `.image` with the hardware
        # pixels after capture, embeds it as a named attachment, and draws it on the page. Alongside it, declare
        # the role's extracted SPECTRUM for the report (Edwin) — same host-fill pattern: the plugin flags an
        # empty SpectrumPlotView, the host sets its `.spectrum` from the captured spectrum after acquisition.
        captureResult = EvaluationResult()
        captureResult.addItem(SpectrumCaptureView(caption=label + " — captured frame (ROI)",
                                                  cropped=True).setShownInReport(True))
        captureResult.addItem(SpectrumPlotView(title=label + " — spectrum").setShownInReport(True))
        step.setEvaluationResult(captureResult)
        return step
