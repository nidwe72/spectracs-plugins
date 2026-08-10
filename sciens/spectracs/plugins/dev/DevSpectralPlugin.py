from sciens.spectracs.plugin_sdk import (
    SpectralPlugin, SpectralWorkflowPhaseType, SpectralWorkflowStep, SpectraContainer,
    MeanOp, TransmissionOp, AbsorptionOp, BaselineOffsetOp, MedianFilterOp,
    SpectrumPlotView, CaptureView, SpectrumCaptureView, TabGroupView, ReportView,
    LimsPublishView, GaugeRender,
    EvaluationResult, LabelView, MetricFieldView, MetricFieldViewStyle, SpectrumFeatureUtil,
    EvaluationColorUtil, MetadataField,
    NavigationMode, NavigationPolicy, WorkflowPolicy, LegendPosition,
    REFERENCE, SAMPLE, TRANSMISSION, ABSORPTION,
)
# ⚠ RoastGaugeView (T = 4.4, raw Soret/Q) and RoastBaselineGaugeView (T = 10.6, 600-630 anchor) are the two
# ampeln §16.20 RETIRED, and their imports are gone from here (SPEC_soret_448_trim.md §5, D-deadgauges). They
# were imported but never instantiated, which made two stale thresholds look current. The classes survive —
# tests build them, and RoastBaselineGaugeView still documents the 600-630 scale — but nothing renders them,
# and their thresholds were deliberately NOT re-derived for the 448 window: maintaining a scale nobody reads
# is how a wrong number gets quoted years later.
from sciens.spectracs.plugins.dev.RoastPedestalGaugeView import RoastPedestalGaugeView
from sciens.spectracs.plugins.dev.RoastFar620GaugeView import RoastFar620GaugeView


class DevSpectralPlugin(SpectralPlugin):
    # Generic "Swiss-knife" plugin for the master dev measurement bench (SPEC_dev_measure_bench.md P1).
    # Same real pipeline as an end-user plugin — ACQUISITION declares REFERENCE+SAMPLE, PROCESSING runs
    # mean -> transmission -> absorption — but WITHOUT any use-case evaluation/verdict. Standalone; not
    # subclassed or shared by any other plugin. Injected transiently (no session codeRef).
    title = "Measurement bench (dev)"

    # SPEC_simplified_plugin_navigation.md (M3, phase X) — the plugin drives the end-user-mirroring "should-be"
    # presentation PERMANENTLY: auto-advance nav + per-step (Reference/Sample) chevrons + the cropped-ROI capture
    # preview + the METADATA form. The temporary SIMPLIFIED_NAVIGATION regression toggle was removed 2026-07-25
    # after the as-is path was rig-verified (Edwin); there is no as-is branch any more.

    # Burst per capture. 150 -> 60 (Edwin 2026-08-10, SPEC_soret_448_trim.md §11). Frame averaging attacks only
    # TEMPORAL noise, as 1/sqrt(N), so this costs sqrt(150/60) = 1.58x on that one term — and §16.26.1 measured
    # the WHOLE instrument floor at 0.42 % of M (null run 003, nothing moved between the two bursts) against a
    # careful-reseat rms of 4.47 % and an archive CV of 3-5 %. ⇒ worst case 0.42 -> 0.66 %, still ~7x below the
    # term that actually limits the measurement; and it is an over-estimate, because 003's 0.42 % also contains
    # lamp/AE drift that more frames never fix.
    # ⭐ What it buys: ~5 s -> ~2 s per burst, i.e. ~2.5x less time IN THE BEAM — §16.22.1a measured the sample
    # at <=40 °C in-beam with 3-5x faster degradation there, and §16.11.16 made "measure within the hour" a
    # verdict rule. It also makes §16.11.17's decay-rate run (0/1/2/4/24 h in one evening) affordable.
    # ⚠ Does NOT invalidate a threshold derived on the 150-frame archive: averaging changes VARIANCE, not the
    # expectation, so there is no bias — only slightly wider class spreads (see the gauge docstrings).
    FRAMES = 60
    # CapturePanel seeds its frame count from this declared value; the frame-count dropdown (when shown) overrides.

    def declaredEvalBands(self):
        # Every wavelength band this plugin's evaluation reads — the capture window (§9 M1) must cover them all,
        # else clamping would starve an eval band. The host / assertion reads this generically.
        # ⚠ The LEGACY 600-630 anchor is deliberately NOT declared: nothing in this plugin computes on it
        # any more (§16.20), and this method's contract is the bands the evaluation actually READS.
        # ⚠ PB_BASELINE_WINDOWS' upper edge is 630 nm. It USED to sit hard against WAVELENGTH_MAX_NM; since the
        # window was widened to 700 nm (2026-08-09) it no longer does, but 630 is still the reddest declared
        # band, so any future narrowing of the ROI below 630 starves it.
        return ([self.BLUE_BAND, self.BLUE_PEAK, self.GREEN_BAND, self.Q_SEARCH, self.Q_BASELINE,
                 self.PB_SORET_BAND, self.PB_Q_BAND]
                + list(self.PB_BASELINE_WINDOWS))

    def __assertWindowCoversBands(self):
        lo, hi = self.WAVELENGTH_MIN_NM, self.WAVELENGTH_MAX_NM
        for bandLo, bandHi in self.declaredEvalBands():
            if bandLo < lo or bandHi > hi:
                raise ValueError(
                    "SPEC_capture_quality.md §9 (M1): capture window [%g, %g] nm does not cover declared eval "
                    "band [%g, %g] — clamping would starve it." % (lo, hi, bandLo, bandHi))

    def policy(self):
        # Cross-cutting flow policy (M3): AUTO_ADVANCE + per-step Reference/Sample chevrons (the should-be preset).
        return WorkflowPolicy(navigation=NavigationPolicy(
            NavigationMode.AUTO_ADVANCE, stepChevronPhases={SpectralWorkflowPhaseType.ACQUISITION}))

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

        # Change G (SPEC_simplified_plugin_navigation.md §4.7-G): the Reference/Sample RASTER inspection views are
        # now PLUGIN-DECLARED (they were host-injected bench dev-chrome). C1/T1 (§7b): the full-frame + cropped
        # rasters are grouped into SUB-TABS via an explicit TabGroupView. The plugin declares the shells + role;
        # the HOST fills the captured-frame pixels it alone owns (masked full-frame / cropped-to-ROI per the flag),
        # traversing into the group. §7b renames "raster" → "image".
        rasterSteps = {}
        for role, label in ((REFERENCE, "Reference image"), (SAMPLE, "Sample Image")):
            rasterStep = SpectralWorkflowStep()
            rasterStep.setLabel(label)
            rasterStep.setRole(role)
            rasters = EvaluationResult()
            rasters.addItem(TabGroupView()
                            .addTab("Full frame", SpectrumCaptureView(
                                caption="Region outside the ROI blacked out", cropped=False))
                            .addTab("Cropped ROI", SpectrumCaptureView(
                                caption="Cropped to the ROI", cropped=True)))
            rasterStep.setEvaluationResult(rasters)
            rasterSteps[role] = rasterStep

        # Spectra: reference + sample overlaid. P5: the overlay is now DECLARED as a multi-trace
        # SpectrumPlotView (was host-drawn via SpectrumPlotWidget.addTrace) — the host renders it generically.
        # M2 (Edwin): the PROCESSING Reference-vs-Sample overlay is also rendered in the PDF report.
        spectraStep = SpectralWorkflowStep()
        spectraStep.setLabel("Spectra")
        spectraStep.setContainer(meaned)
        # axis="dn": these are RAW capture spectra. On a linear axis the dim-but-healthy range collapses into
        # the bottom 4% and a fine 60 DN band reads as "nothing" — which caused a mis-dilution on 2026-07-27
        # (SPEC_capture_quality.md §16.7.2e). Transmission/absorbance below stay unitless: no axis flag.
        # SPEC_soret_448_trim.md §13: the DN guard is DECLARED here, not hard-coded in the renderers. Two
        # dashed rejection edges (16 / 60) and a faint dotted target pair (20 / 40) — because the question at
        # the bench is "am I in the window?", not "have I cleared the edge?": a fill at 17 DN is legal and bad.
        spectraView = SpectrumPlotView(title="Reference vs Sample", axis="dn") \
            .addTrace(meaned.getSpectra()[REFERENCE], "Reference", "c") \
            .addTrace(meaned.getSpectra()[SAMPLE], "Sample", "y")
        for level in self.dnLevels():
            spectraView.addLevel(*level)
        spectraStep.setView(spectraView.setShownInReport(True))

        transmissionStep = SpectralWorkflowStep()
        transmissionStep.setLabel("Transmission")
        transmissionStep.setContainer(transmission)
        transmissionStep.setView(SpectrumPlotView(transmission.getSpectra()[TRANSMISSION], "T(λ) = S/R"))

        absorptionStep = SpectralWorkflowStep()
        absorptionStep.setLabel("Absorption")
        absorptionStep.setContainer(absorption)
        # M2 (SPEC_bench_pdf_export.md §3): flag the PROCESSING absorption curve into the PDF report. Canonical
        # example — it is shown HERE (PROCESSING) and appears in the report, but never in the EVALUATION GUI.
        absorptionStep.setView(SpectrumPlotView(absorption.getSpectra()[ABSORPTION], "A(λ) = −log10(S/R)")
                               .setShownInReport(True))

        # SPEC_capability_proof.md §7.0.1/§8.2: the processing ladder made VISIBLE — three COLOURED traces, raw
        # (grey) -> de-spiked (orange, narrow instrument spikes removed) -> baseline-corrected (green, + flat-offset).
        # SpectrumPlotView carries no linestyle, so colour distinguishes them; the hex/name colours are valid in
        # BOTH renderers (pyqtgraph mkPen rejects matplotlib greys like "0.6").
        absorptionRaw = absorption.getSpectra()[ABSORPTION]
        despiked = self.__despikedAbsorption(absorptionRaw)
        ladderStep = SpectralWorkflowStep()
        ladderStep.setLabel("Absorption (dev)")   # §7b rename (was "Absorption (raw / despiked / baseline-corrected)")
        ladderStep.setView(SpectrumPlotView(title="A(λ) — raw → despiked → baseline-corrected (flat-offset)")
                           .addTrace(absorptionRaw, "A raw", "#888888")
                           .addTrace(despiked, "A despiked", "#e08000")
                           .addTrace(self.__baselineCorrectedAbsorption(despiked), "A despiked + baseline", "g")
                           .setShownInReport(True))

        # §7b step order: Spectra, Absorption, Transmission, Reference image, Sample Image, Absorption (dev).
        # Default-selected step = Spectra (it is first ⇒ automatic).
        phase.addToSteps(spectraStep)
        phase.addToSteps(absorptionStep)
        phase.addToSteps(transmissionStep)
        phase.addToSteps(rasterSteps[REFERENCE])
        phase.addToSteps(rasterSteps[SAMPLE])
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
    # ⚠⚠ `PB_SORET_BAND_LEGACY_440` MUST KEEP MEANING 440-460 FOREVER — the same rule, and the same reason, as
    # `PB_BASELINE_WINDOWS_LEGACY_600` below. Every §16 number published before 2026-08-10 was measured on this
    # window, and the archive-reproducing diagnostics (`settling_sweep.py` above all, which derives the
    # reference metric every comparison table is built on) read it. Repointing it would silently redefine them.
    PB_SORET_BAND_LEGACY_440 = (440.0, 460.0)

    # --- THE TRIM: 440-460 -> 448-460 (SPEC_metric_research.md §7.13 S1, adopted Edwin 2026-08-04; shipped
    # 2026-08-10, SPEC_soret_448_trim.md). `DOC_pedestal_correction.md` §7 established that the 440-447 bins
    # read 2.0-2.6 DN against a reference near 88 — "they are not measurements" — and they sat inside this
    # window, so a third of the numerator was fed by bins the project had already written off.
    #
    # Measured effect of dropping them (§7.13.2/§7.13.3, whole archive): class d 6.91 -> 7.37, within-green
    # d 1.21 -> 1.34, dilution spread 10.3 % -> 8.8 %, and the pedestal anomaly's significance t 4.43 -> 2.92.
    # Post-rebuild it holds up: §16.28.2a transfers `M448 + pedestal` across a LAMP SWAP at 3 % (against 51 %
    # for the raw ratio), and §16.27.5's Cohen's d is better on M448 for all three oil pairs.
    #
    # ⚠ What it is NOT: §16.28.3 measured M448 WORSE on run-to-run repeatability in 8 of 10 fills (~20 %
    # relative). ⇒ it is specifically an ILLUMINATION-robustness device — better on discrimination and on
    # lamp/exposure transfer, not on re-seating noise. Ship it for what it is.
    #
    # ⚠ `B_Soret` falls ~x0.65-0.67, so BOTH gauge scales moved with it — see their docstrings. The factor is
    # class-dependent (0.642 brown .. 0.672 green, §16.27), which is exactly WHY the trim improves d, and also
    # why the thresholds were re-derived on the archive rather than rescaled by a single number.
    PB_SORET_BAND = (448.0, 460.0)    # Soret right-hand slope = green-pigment blue absorption
    PB_Q_BAND = (560.0, 580.0)        # green-pigment Q-band

    # --- SPEC_capture_quality.md §16.10.2/§16.10.9: the two anchor windows the linear baseline is fitted
    # through. A re-seating tilt enters absorbance as an offset AND a slope; fitting a straight line across
    # these two windows and subtracting it removes both, where a flat-offset subtraction removes only the
    # offset and SNV only offset+scale. Both lie inside the 440–630 capture clamp.
    #
    # ⚠ THE FAR WINDOW IS NOT "OIL-QUIET" — it MEASURES. (Was documented as featureless until 2026-07-31;
    # SPEC_capability_proof.md §2.1a, SPEC_capture_quality.md §16.12.12/§16.12.13.) Expanding the correction
    # at the two band centroids, the shipped metric is really THREE-REGION:
    #
    #            A_Soret − 1.941·A_near + 0.941·A_far
    #   S/Q  =  ──────────────────────────────────────      (reproduces this code to within 0.5 %)
    #            A_Q     − 0.529·A_near − 0.471·A_far
    #
    # A_far enters the NUMERATOR positively and the DENOMINATOR negatively — both RAISE the ratio. And
    # 600–630 carries real green pigment: the rise across it is green 0.0535 vs brown 0.0159 (5.1 σ, 37 runs)
    # under an identical lamp. So it is a third pigment band, not a correction anchor.
    #
    # ⚠ CORRECTED 2026-08-04. This comment said "the flank toward the true CHLOROPHYLL Q max near 665 nm,
    # which sits OUTSIDE our capture clamp". Both halves were wrong, and they contradicted the block
    # directly below. The pigment is PROTOchlorophyll — a porphyrin, not a chlorin — and its Qy sits at
    # ~623-626 nm (`KB_spectroscopy_physics.md` §4.1, sourced from Fruhwirth & Hermetter 2007). The band
    # is therefore INSIDE the window, not a distant flank of something unreachable. The 5.1 σ measurement
    # and every consequence below stand; the attribution gets stronger, because the rise is the band
    # itself rather than its tail. `LAB_DIARY_capability_proof.md` §2 recorded this correction on
    # 2026-07-31 and stated the comment had been fixed — it had not.
    #
    # It is also LOAD-BEARING: sweeping the far edge in, Cohen's d falls 2.88 → 0.94 and the classes overlap
    # outright at 600–610. Do NOT "clean up" these windows on the assumption that they are signal-free — the
    # discrimination goes with them. Changing them is a metric redesign, not a tidy-up, and it is gated on
    # post-rig-rebuild data with a real brown series (§16.12.15 item 2).
    PB_BASELINE_WINDOWS_LEGACY_600 = ((520.0, 540.0), (600.0, 630.0))

    # --- SPEC_capture_quality.md §16.20 — the 620-630 FAR ANCHOR (Edwin 2026-08-02/03) -------------------
    # Same near anchor, far anchor moved from 600-630 to 620-630. Two things follow from the paragraph above:
    # the far window MEASURES rather than corrects, and protochlorophyll's Qy sits at ~623-626 nm
    # (`KB_spectroscopy_physics.md` §4.1). 620-630 is therefore CENTRED on the pigment band instead of
    # straddling it, and it starts clear of the 607 nm lamp emission line, whose excess collapses between
    # 609 and 610 nm (§16.20.6).
    #
    # ⚠ `PB_BASELINE_WINDOWS_LEGACY_600` ABOVE MUST KEEP MEANING 600-630 FOREVER. Ten diagnostics read it,
    # and one is load-bearing for the entire §16 evidence base: `settling_sweep.py` derives the reference
    # metric that every comparison table is built on, under the keys `A_Soret linear` / `A_Q linear` /
    # `S/Q linear base`. Repointing it would silently redefine every historical number in the specs.
    #
    # ⚠ HISTORICAL: 630 nm WAS exactly WAVELENGTH_MAX_NM and the grid stopped at 629.8, so on every run captured
    # before 2026-08-09 this anchor was really 620-629.8, sitting hard against the capture edge. The window now
    # reaches 636 nm, so the anchor is fully interior and reads the full 620-630 for the first time — expect a
    # small step in the far-anchor level against the pre-widening archive. `declaredEvalBands()` covers it; any
    # future narrowing of the ROI below 630 starves it silently.
    PB_BASELINE_WINDOWS = ((520.0, 540.0), (620.0, 630.0))

    # The pedestal residual REFITTED ON THE 620-630 ANCHOR (§16.20.2; Kiendler run-level straight-line fit,
    # `diagnostics/pedestal_correction.py`). NOT the 600-630 anchor's -0.0246: move the anchor and the
    # residual moves with it, so pairing these bands with that constant would be a category error.
    #
    # ⚠⚠ THIS IS AN INSTRUMENT CONSTANT LIVING IN A DISTRIBUTED PLUGIN, AND THAT IS THE WRONG HOME FOR IT.
    # It was fitted on ONE oil on THIS rig, and §16.19 shows it does not survive a mechanical rebuild. Every
    # instrument loading this plugin therefore inherits one instrument's number. Acceptable for the DEV bench
    # tool it is used in; NOT acceptable for anything an end user runs. §16.17 designs the calibration that
    # should own it (per-instrument, stored on the instrument record, re-measured after any rig change), and
    # §16.17.8 blocker 6 records what must be settled first.
    PB_R_Q = -0.0184

    # --- SPEC_soret_448_trim.md §12/§14 — the EVALUATION plot's own palette. Anchors are drawn in a different
    # shade from measurement bands so a reader can tell "this window feeds the LINE" from "this window feeds
    # the RATIO" without reading the captions. RGBA for the shading (pyqtgraph brush + matplotlib alpha both
    # accept a hex string), plain hex for the bars.
    # ⚠ Every colour here must read on BOTH grounds: the app draws on a dark plot, the PDF on white paper, and
    # M2's contract is that the preview IS the PDF. Mid-tones only — a near-white bar is invisible on paper and
    # a near-black one is invisible on screen. (First cut used #e8e8e8 for the bars; it vanished in the report.)
    __ANCHOR_SHADE = "#5a6a7a55"      # baseline anchors: cool, recessive
    __RAW_COLOR = "#e8e337"           # A(λ) despiked — the measurement
    __CORRECTED_COLOR = "#35d3d3"     # A(λ) − baseline — what the metric is read on
    __BASELINE_COLOR = "#d08a2c"      # the fitted 520-540 / 620-630 line (dashed)
    # ⭐ A BAR WEARS THE COLOUR OF THE CURVE IT WAS MEASURED ON (SPEC_soret_448_trim.md §25.1). That one rule
    # is what makes two overlaid curves safe: ownership is readable without a caption.
    __METRIC_BAR = __CORRECTED_COLOR  # ① Soret and ② Q — measured on the SUBTRACTED curve
    __ANCHOR_BAR = "#c9a227"          # ③ red and ④ quiet anchor — measured on the RAW curve

    # --- SPEC_soret_448_trim.md §13 — THE DN GUARD, now plugin-owned data rather than a renderer constant.
    # §16.23.8 states it two-sided and this plugin is where the numbers belong: the guards are the REJECTION
    # edges (below 16 too concentrated, above ~60 too dilute) and the target pair is where a fill should
    # actually land. Both are declared on the Reference-vs-Sample plot AND handed to the live capture preview
    # via CaptureView, so the operator, the screen and the PDF quote one set of numbers.
    # ⚠ Read in DISPLAY DN (axis="dn"), which is where the operator judges dilution — not gamma-decoded again.
    DN_GUARD_LOW = 16.0               # below: quantization-limited, the bin is not a measurement (§17.6/11)
    DN_GUARD_HIGH = 60.0              # above: too dilute — the band stops carrying absorbance
    DN_TARGET_LOW = 20.0              # the window a fill should land in (§16.23.8)
    DN_TARGET_HIGH = 40.0
    __GUARD_COLOR = "#c87a3c"
    __TARGET_COLOR = "#6b7f5a"

    def dnLevels(self):
        # The four DN lines, built ONCE and handed to both consumers: the PROCESSING Reference-vs-Sample plot
        # and the SAMPLE capture step's live preview. Two weights on purpose — dashed = the REJECTION edges,
        # dotted = the target window — because the question at the bench is "am I in the window?", not "have I
        # cleared the edge?": §16.23.8 makes a fill at 17 DN legal and bad.
        return [(self.DN_GUARD_LOW, None, None, "16 DN — too concentrated", self.__GUARD_COLOR, "dashed", None),
                (self.DN_GUARD_HIGH, None, None, "60 DN — too dilute", self.__GUARD_COLOR, "dashed", None),
                (self.DN_TARGET_LOW, None, None, None, self.__TARGET_COLOR, "dotted", None),
                (self.DN_TARGET_HIGH, None, None, "20–40 DN target", self.__TARGET_COLOR, "dotted", None)]

    # Hue-normalized colour chips: a fixed, CALM S/L so only HUE varies between oils (SPEC_capability_proof.md §5,
    # "C scheme" — Edwin 2026-07-22). Lowered from the original vivid 80/50 to a darker, less "popping" pair.
    __NORM_SATURATION = 38.0
    __NORM_LIGHTNESS = 34.0

    # SPEC_capture_quality.md §9 (M1) / SPEC_capability_proof.md §7.0.2: the host HARD-CLAMPS the captured ROI to
    # this window (via CaptureView.wavelengthMin/MaxNm) so the dead margins never enter the stored spectrum
    # (they'd only feed the S/R floor-guard garbage). Must ⊇ every declaredEvalBand() below — asserted in
    # acquisition() — including the PB blue band at 440.
    #
    # 440–630 → 700 → 636 nm (Edwin 2026-08-09, settled in three steps on one evening). The 630 edge was the
    # old lamp's useful limit, and the 620–630 far anchor was the reddest thing it could light. 700 was tried
    # first, to reach a 660 nm LED; runs 20260808A/B measured that the camera's IR-cut kills everything past
    # ~650 (130× down at 660, at the dark floor by 665), so 650–700 can only ever be floor noise on this
    # camera. ⭐ 636 is EDWIN'S CALL, and the reason is PRESENTATION, not physics: past ~636 the A(λ) curve
    # wobbles as the lamp dies, and the ~630 peak stops reading as a peak on the plot.
    #
    # ⚠ WHAT THIS COSTS — read before deriving anything from the red flank:
    #  * the protochlorophyll Qy band measured at 627–630 nm, half-maxima 622.3 / 636.4 (run A, 248 σ). 636
    #    therefore clamps 0.4 nm BLUE of the red half-maximum: the stored band is TRUNCATED, and on the Yuji
    #    (run B) it is truncated hard — there A(λ) is still at 83 % of its peak at 640 nm and does not fall
    #    below half until past 644. ⇒ NEVER derive a band width, FWHM or red-flank shape from a spectrum
    #    captured under this clamp; the fall-off at the right edge is the WINDOW, not the pigment.
    #  * 641–645 nm was the red baseline anchor that made run A's amplitude measurable (ΔA = 0.060). It is
    #    outside the window now, so that measurement cannot be repeated without widening this constant again.
    #
    # ⭐ The band itself is REAL and none of the above is in doubt: it survived a lamp swap. The Sansi V2 and
    # the Yuji put their own sharp edges 3 nm apart (614.0 vs 610.9 nm) while the band stayed at 627.3 / 629.7,
    # and the two references cross-correlate at 0.00 nm in the Q region, so the rig did not move. Lamp
    # structure moves with the lamp; this does not.
    #
    # Nothing SHIPPED needs the lost stretch: the reddest declaredEvalBand() edge is 630.0 (PB_BASELINE_WINDOWS'
    # far anchor), 6 nm inside this clamp. The loss is to future characterisation of the band, not to the
    # current metrics. If the plot cosmetics are what matter, the cleaner fix is a display-only x-range on
    # SpectrumPlotView (model field + toJson/fromJson + both renderers) — then the data stays and only the
    # drawing is clipped.
    #
    # ⚠ The per-pixel nm mapping does not change (the ROI just spans a different column count), so samples
    # inside the existing bands are the same pixels — PB_R_Q and the gauge thresholds keep their meaning. But
    # the colour chips do NOT: EvaluationColorUtil integrates the whole curve, so the COLOUR row is not
    # comparable across a window change. The ratio metrics are.
    WAVELENGTH_MIN_NM = 440.0
    WAVELENGTH_MAX_NM = 636.0

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
        metricsStep.setLabel("Metrics (dev)")   # §7b rename (was "Metrics" — the legacy peak-ratio metrics)
        metricsResult = self.__peakRatioResult(absorption, reference, transmission)
        # M2: flag the evaluation metrics into the PDF report (the verdict numbers a reader wants on paper).
        for item in metricsResult.getItems():
            item.setShownInReport(True)
        metricsStep.setEvaluationResult(metricsResult)
        # Q-peak dashed marker (restored from the pre-P4 host-drawn plot): the local-max λ in the Q search band.
        peak = SpectrumFeatureUtil().peakInRange(absorption, *self.Q_SEARCH)
        qLambda = peak[0] if peak is not None else 575.0
        # M2 (Edwin): the EVALUATION band-marked absorption spectrum is also rendered in the PDF report.
        spectrumStep = SpectralWorkflowStep()
        spectrumStep.setLabel("Absorption (bands, dev)")   # §7b rename (was "Spectrum" — legacy band plot)
        # F1/B4 (SPEC_soret_448_trim.md §8.2): Q_BASELINE is now marked too. D_Q — and therefore this tab's
        # headline "Greenness G" — is a peak height above the CHORD drawn across 555-600, and marking only
        # where the peak is SEARCHED while hiding what it is measured AGAINST was the tab's own defect B4.
        spectrumStep.setView(SpectrumPlotView(absorption, title="A(λ) — bands")
                             .addBand(*self.BLUE_BAND, "blue")
                             .addBand(*self.GREEN_BAND, "clarity")
                             .addBand(*self.Q_SEARCH, "Q search")
                             .addBand(*self.Q_BASELINE, "D_Q chord", self.__ANCHOR_SHADE)
                             .addMarker(qLambda, "Q").setShownInReport(True))

        # V3 (SPEC_capability_proof.md §2.1): a SECOND, forward-looking evaluation view — the PB literature bands
        # (440-460 Soret / 560-580 Q) read as plain band MEANS on the DESPIKED absorbance, the pigment ratio, and
        # the 10 colour chips DUPLICATED here at the calm C-scheme S/L. The legacy "Metrics" tab is left fully
        # intact so the old-band numbers stay directly comparable — a tab-vs-tab "eureka" comparison (Edwin).
        despikedAbsorption = self.__despikedAbsorption(absorption)
        newStep = SpectralWorkflowStep()
        newStep.setLabel("Metrics")   # §7b rename (was "Evaluation (new)" — the PB literature-band metrics, now primary)
        newResult = self.__newEvaluationResult(despikedAbsorption, transmission, absorption)
        for item in newResult.getItems():
            item.setShownInReport(True)
        newStep.setEvaluationResult(newResult)

        # A SECOND version of the A(λ) spectrum with the NEW bands marked (Edwin): the PB Soret + Q windows plus
        # the shared 510-540 clarity floor shaded, Q local-max marked — on the same despiked curve as the metrics.
        newPeak = SpectrumFeatureUtil().peakInRange(despikedAbsorption, *self.PB_Q_BAND)
        newQLambda = newPeak[0] if newPeak is not None else 570.0
        newSpectrumStep = SpectralWorkflowStep()
        newSpectrumStep.setLabel("Absorption (bands)")   # §7b rename (was "Spectrum (new)" — the PB-band A(λ) plot)
        newSpectrumStep.setView(self.__bandPlot(despikedAbsorption, newQLambda))

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

        # §7b step order: the NEW/PB-band views are primary; the legacy peak-ratio views become "(dev)".
        # Metrics, Absorption (bands), Report, Metrics (dev), Absorption (bands, dev).
        phase.addToSteps(newStep)          # "Metrics"
        phase.addToSteps(newSpectrumStep)  # "Absorption (bands)"
        phase.addToSteps(reportStep)       # "Report"
        phase.addToSteps(metricsStep)      # "Metrics (dev)"
        phase.addToSteps(spectrumStep)     # "Absorption (bands, dev)"

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
        pedestalRatio = self.__pedestalRatio(workflow)
        if pedestalRatio is not None:
            # SPEC_roast_ampel.md §8.4 Option B — the LIMS headline: a big verdict pill + a coarse green|red zone
            # bar, NO fine band and NO number (D-lims-number), so it reads as a stable verdict at a glance.
            # ⚠ SWITCHED 2026-08-03 (§16.20). This badge used to be driven by RoastGaugeView on the RAW Soret/Q
            # ratio — the one metric of the three that cannot separate the classes at all (d = 1.20, classes
            # overlap), on the threshold T = 4.4 that sits below the entire brown class. It was therefore
            # reporting "good — green" for brown oil on the ONE screen an end user actually sees. It now uses
            # the same primary metric as the first EVALUATION gauge.
            badge = EvaluationResult()
            badge.addItem(RoastPedestalGaugeView(pedestalRatio, render=GaugeRender.LABEL | GaugeRender.ZONES))
            step.setEvaluationResult(badge)
        step.setView(LimsPublishView(
            title="Send to LIMS",
            sampleTypeName="Pumpkin Oil", sampleTypeCode="OIL",
            analyses=[{"name": "Spectracs Measurement", "key": "SpectracsMeasurement",
                       "group": "Spectroscopy"}],
            backend="senaite", configKey="SENAITE"))
        phase.addToSteps(step)

    def __bandPlot(self, despikedAbsorption, qLambda):
        # SPEC_soret_448_trim.md §12.3/§12.4/§14 — the EVALUATION picture, declared entirely through the
        # view-model (no renderer knows anything about pumpkin oil).
        #
        # ⭐ THE IDENTITY THIS IS BUILT ON:  mean(curve over band) - mean(fitted line over band) = B_band.
        # So a BAR at the band mean of the PLOTTED curve, with the fitted baseline drawn beneath it, makes the
        # vertical gap between them the very number the gauges divide. The two `· baseline` metric rows stop
        # being numbers a reader must trust and become a distance on screen.
        #
        # ⚠ The bars MUST be fed the mean of the plotted (despiked) curve — NOT the baselined mean. Passing
        # far620Soret here would draw the bar below where the curve actually is and nothing would error; the
        # picture would silently stop being true. `tests/test_band_bar_identity.py` is what catches it.
        #
        # ⚠ The 510-540 clarity band is deliberately NOT shaded here (§14): a single grey block at 510-540 was
        # being read as the 520-540 baseline anchor and was wrong by 10 nm on the left edge (defect B2). The
        # `Clarity · 510-540 nm` metric row is unaffected — only the shading is gone.
        util = SpectrumFeatureUtil()
        near, far = self.PB_BASELINE_WINDOWS
        fit = util.fittedBaseline(despikedAbsorption, self.PB_BASELINE_WINDOWS)
        corrected = util.linearBaselineCorrected(despikedAbsorption, self.PB_BASELINE_WINDOWS)
        # ⚠ The measured curve is declared as a TRACE, not as the view's primary spectrum: only a trace can
        # carry a label, and the legend names every curve by text in its own colour. A primary with no label
        # would have left the yellow curve as the one unnamed thing on the plot.
        view = SpectrumPlotView(title="A(λ) — PB bands (despiked)")
        view.setLegend(LegendPosition.NORTH_EAST, padding=34.0)
        view.addTrace(despikedAbsorption, "A(λ) despiked", self.__RAW_COLOR)

        # ⭐ THE TWO CURVES. The subtracted one is what the verdict actually reads, so it is drawn rather than
        # left to be imagined — and the two are 6..46 px apart on a bench-sized plot (measured, §24.1), so the
        # overlay is legible. The fitted baseline is dashed: it reads as CONSTRUCTION, not as a measurement.
        # ⭐ It rises to the red on real oil, and that is not scattering (which falls with λ): it is §16.12.12's
        # finding that the far anchor MEASURES, sitting on protochlorophyll's Qy band.
        if corrected is not None:
            view.addTrace(corrected, "A(λ) − baseline", self.__CORRECTED_COLOR)
        if fit is not None:
            view.addTrace(fit, "fitted baseline (520–540 / 620–630)", self.__BASELINE_COLOR, style="dashed")

        # ⭐⭐ THE RULE (Edwin, §25.1): A BAR SITS ON THE CURVE THAT GIVES IT MEANING.
        #   ① Soret and ② Q  -> the SUBTRACTED curve: their heights ARE B_Soret and B_Q, the two numbers the
        #                       verdict divides (M = B_Soret / (B_Q − r_Q)).
        #   ③ red and ④ quiet anchor -> the RAW curve: the anchors DEFINE the fitted line, and their bars
        #                       landing on it is the plot's own running proof that the fit is anchored.
        # ⚠ Feeding a bar the other curve would render plausibly and be WRONG — that is what
        # `test_band_bar_identity` and the plugin-boundary test pin down.
        for number, band, label, source, color in (
                (1, self.PB_SORET_BAND, "Soret band mean", corrected, self.__METRIC_BAR),
                (2, self.PB_Q_BAND, "Q-band mean", corrected, self.__METRIC_BAR),
                (3, far, "red-anchor mean", despikedAbsorption, self.__ANCHOR_BAR),
                (4, near, "quiet-anchor mean", despikedAbsorption, self.__ANCHOR_BAR)):
            mean = util.bandMean(source, *band) if source is not None else None
            if mean is not None:
                view.addLevel(mean, band[0], band[1], label=label, color=color, number=number)

        # The four windows the metrics on this tab read, shaded. Captions stay (Edwin) — the anchors' captions
        # are legible again now that they no longer inherit the recessive shading colour (§25.3).
        for band, caption in ((self.PB_SORET_BAND, "S"), (near, "quiet anchor"),
                              (self.PB_Q_BAND, "Q"), (far, "red anchor")):
            view.addBand(*band, caption, self.__ANCHOR_SHADE if band in (near, far) else None)
        # ⚠ The marker is labelled "λmax", NOT "Q": it sits inside the Q band, so two captions reading "Q" a
        # few nm apart is what the plot showed before numbering (§22.1's duplicate-caption defect). This one
        # names what the line actually is — the local maximum's wavelength.
        return view.addMarker(qLambda, "λmax").setShownInReport(True)

    def __pedestalRatio(self, workflow):
        # SPEC_roast_ampel.md §8.6 / SPEC_capture_quality.md §16.20 — the PRIMARY pigment index: the 620-630
        # baseline with that anchor's pedestal residual put back, i.e. the same value the first EVALUATION
        # gauge shows. Recomputed here (publishing() gets the workflow) so the phase hooks stay independent —
        # cheap, deterministic, no cross-phase stashing.
        absorption = self.__findRole(workflow, ABSORPTION)
        if absorption is None:
            return None
        despiked = self.__despikedAbsorption(absorption)
        util = SpectrumFeatureUtil()
        far620 = util.linearBaselineCorrected(despiked, self.PB_BASELINE_WINDOWS)
        if far620 is None:
            return None
        soret = util.bandMean(far620, *self.PB_SORET_BAND)
        qBand = util.bandMean(far620, *self.PB_Q_BAND)
        if soret is None or qBand is None:
            return None
        return soret / max(qBand - self.PB_R_Q, self.__EPS)

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

        # SPEC_capture_quality.md §16.20 — the SAME construction on the 620-630 far anchor, which is centred on
        # protochlorophyll's Qy band instead of straddling it and starts clear of the 607 nm lamp line. Two
        # ratios come off it: the plain one, and the one with that anchor's own pedestal residual put back.
        # ⚠ The three verdicts live on THREE DIFFERENT SCALES. Only their verdicts are comparable.
        far620 = util.linearBaselineCorrected(despikedAbsorption, self.PB_BASELINE_WINDOWS)
        far620Soret = util.bandMean(far620, *self.PB_SORET_BAND) if far620 is not None else None
        far620Q = util.bandMean(far620, *self.PB_Q_BAND) if far620 is not None else None
        far620Ratio = ratio(far620Soret, far620Q)
        pedestalRatio = (None if far620Soret is None or far620Q is None
                         else far620Soret / max(far620Q - self.PB_R_Q, self.__EPS))

        dilutionInvariant = MetricFieldViewStyle.builder().labelBold(True).build()
        result = EvaluationResult()
        # SPEC_roast_ampel.md §8.5 — the Roast Ampel gauge is the FIRST item of this tab (gradient band + marker +
        # verdict pill + value-on-swatch), driven by the same Soret/Q pigment ratio the metric row below shows.
        pigmentRatio = ratio(soret, qBand)
        # §16.20 — THREE verdicts, in decreasing order of how much correction has been applied, so the reader
        # can see what each step of the construction buys:
        #   1  620-630 anchor + pedestal correction   <- the primary
        #   2  620-630 anchor, no correction
        #   3  raw Soret/Q                            <- VALUE ONLY, deliberately NO gauge (see below)
        # Each pair of adjacent rows isolates exactly one change. The NUMBERS are not comparable between them.
        if pedestalRatio is not None:
            result.addItem(RoastPedestalGaugeView(
                pedestalRatio, render=GaugeRender.BAND | GaugeRender.LABEL | GaugeRender.SWATCH))
        if far620Ratio is not None:
            result.addItem(RoastFar620GaugeView(
                far620Ratio, render=GaugeRender.BAND | GaugeRender.LABEL | GaugeRender.SWATCH))
        # Full 10-variant colour set, DUPLICATED from the legacy tab (identical builder → identical chips), at the
        # calm C-scheme S/L. Placed first so the eye lands on colour, then the numbers. (Header labels removed
        # 2026-07-24, Edwin — the gauge row + the metric rows are self-explanatory.)
        colourChips = self.__colourChips(transmission, rawAbsorption, despikedAbsorption,
                                         self.__baselineCorrectedAbsorption(despikedAbsorption))
        if colourChips:
            for chip in colourChips:
                result.addItem(chip)
        result.addItem(MetricFieldView("Soret · 448–460 nm", fmt(soret),
            "mean absorbance over the 448–460 nm Soret right-hand slope (green-pigment blue absorption). "
            "The window starts at 448, not 440: the 440–447 bins read 2.0–2.6 DN against a reference near 88 "
            "and are not measurements — dropping them improved class separation, the within-green task and "
            "dilution spread at once (SPEC_metric_research.md §7.13)."))
        result.addItem(MetricFieldView("Q · 560–580 nm", fmt(qBand),
            "mean absorbance over the 560–580 nm green-pigment Q-band."))
        result.addItem(MetricFieldView("Clarity · 510–540 nm", fmt(clarity),
            "mean absorbance over the 510–540 nm clarity floor (turbidity / darkening); the shared denominator."))
        result.addItem(MetricFieldView("Pigment ratio", fmt(ratio(soret, qBand)),
            "Soret ÷ Q — the two green-pigment bands; dilution-invariant (both scale with concentration). Primary "
            "discriminator candidate. ⚠ the Q band is weak on this rig (§13/F7) — watch its run-to-run stability.",
            style=dilutionInvariant))
        result.addItem(MetricFieldView("Pigment ratio · clarity", fmt(ratio(soret, clarity)),
            "Soret ÷ clarity-floor on the NEW bands (448–460 / 510–540) — the stable-denominator safety net for "
            "the pigment ratio; dilution-invariant.", style=dilutionInvariant))
        # §16.10.9 — the linear-baseline rows. The two band means are emitted beside the ratio so a reader can
        # see WHAT the correction did to each band, not just its effect on the quotient.
        result.addItem(MetricFieldView("Soret · baseline", fmt(far620Soret),
            "mean 448–460 absorbance measured above the fitted 520–540/620–630 baseline, not above zero. "
            "On the Absorption (bands) plot this is the vertical GAP between the S bar and the dashed line."))
        result.addItem(MetricFieldView("Q · baseline", fmt(far620Q),
            "mean 560–580 absorbance measured above the same fitted baseline."))
        # §16.20 — THE THIRD VERDICT, deliberately a VALUE WITH NO GAUGE. On post-rebuild data the raw ratio
        # does NOT separate the classes: green 5.387 ± 0.510 against brown 4.842 ± 0.290, Cohen's d 1.20, and
        # the classes OVERLAP outright (lowest green run 4.863 < highest brown run 5.340). No threshold
        # classifies all 28 archived runs, so any pill drawn here would be a guess wearing a verdict's clothes.
        # ⚠ This also retired the shipped T = 4.4, which sat BELOW the entire brown class (minimum 4.622) and
        # therefore called every run of the brown S-Budget oil "good — green".
        result.addItem(MetricFieldView("Verdict · raw Soret/Q  (no verdict)", fmt(pigmentRatio),
            "Soret ÷ Q with NO baseline at all — shown for continuity with the older reports and as the "
            "uncorrected end of the three-verdict ladder. ⚠ NO verdict is drawn because this quantity cannot "
            "carry one: on post-rebuild data the green and brown classes OVERLAP (Cohen's d 1.20 against 9.46 "
            "and 10.35 for the two gauges above), so no threshold separates them. Read the gauges, not this.",
            style=dilutionInvariant))
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
        # colours use the sRGB converter (full gamut, no Philips-Hue clamp) with a RELATIVE ceiling so a T→0
        # spike can't dominate (SPEC_capture_quality.md §17.6/7 — an absolute cap would have to be
        # re-tuned when gamma linearization moves the absorbance scale, and this plugin ships sealed); transmission uses rgbxy (verdict-compatible). Each returns measured (h,s,l) deg/%. Three intrinsic
        # processing rungs — raw, de-spiked, de-spiked+baseline — shown hue-normalized (fixed S/L) so only HUE moves;
        # plus a natural (measured S/L) chip. Only the ABSORBED colours get the correction rungs (an additive b is a
        # chromaticity SHIFT for absorbed, invariant for perceived). Order: intrinsic → intrinsic-perceived (+180°
        # complement into the green-yellow-brown family) → perceived (transmission).
        util = EvaluationColorUtil()

        def hsl(spectrum, converter, ceiling=None):
            return util.spectrumToHsl(spectrum, converter=converter, ceiling=ceiling) if spectrum is not None else None

        hslAbsorb = hsl(absorption, "srgb", util.RELATIVE)
        hslDespiked = hsl(despikedAbsorption, "srgb", util.RELATIVE)
        hslBaseline = hsl(despikedBaselineAbsorption, "srgb", util.RELATIVE)
        hslPerceive = hsl(transmission, "rgbxy")

        # Intrinsic-perceived = the COLORIMETRIC complement of the absorbed colour (SPEC_capability_proof.md option
        # (b), 2026-07-22): reflect the absorbed chromaticity through the D65 white point — NOT a +180° HSL hue flip
        # (which lands ~34° off the true perceived hue; the white-point complement is ~4° on K/L/M/N). One per
        # absorbance rung, so the processed twins stay meaningful.
        def complement(spectrum):
            return util.complementViaWhitePoint(spectrum, ceiling=util.RELATIVE) if spectrum is not None else None
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
        # SPEC_soret_448_trim.md §25.4: the SAME four DN lines the Spectra plot and the PDF draw are handed to
        # the LIVE preview — that is where the dosing decision is actually made, so it is the one plot that
        # must not carry a stale private constant.
        # ⭐ SAMPLE ONLY (Edwin 2026-08-10). §16.23.8 states the guard on min(S) after the SAMPLE capture; the
        # reference is a solvent blank whose level is set by auto-exposure and judged against R ≈ 88. Drawing
        # 16/60 DN on the reference asserted a rule that does not apply there — and invited the operator to
        # "fix" a reference that was never wrong.
        step.setView(CaptureView(prompt=prompt,
                                 captureLabel="Capture " + label.lower(), geometry="transmission",
                                 wavelengthMinNm=self.WAVELENGTH_MIN_NM, wavelengthMaxNm=self.WAVELENGTH_MAX_NM,
                                 croppedPreview=True,  # Change A: cropped-ROI live preview (permanent, phase X)
                                 levels=(self.dnLevels() if role == SAMPLE else [])))
        # M2 (SPEC_bench_pdf_export.md §5b): declare that this role's captured frame belongs in the PDF report
        # (cropped to the ROI). The plugin declares presence + flag; the HOST fills `.image` with the hardware
        # pixels after capture, embeds it as a named attachment, and draws it on the page. Alongside it, declare
        # the role's extracted SPECTRUM for the report (Edwin) — same host-fill pattern: the plugin flags an
        # empty SpectrumPlotView, the host sets its `.spectrum` from the captured spectrum after acquisition.
        # §7b (Edwin 2026-07-25): the report wants BOTH frames — the full frame with the ROI rectangle painted
        # (roiOverlay, "the camera saw a sane image; here's where the ROI landed") AND the cropped-to-ROI frame.
        # Separate shownInReport items (the report has no tabs — it stacks them); NOT a TabGroupView.
        captureResult = EvaluationResult()
        captureResult.addItem(SpectrumCaptureView(caption=label + " — full frame (ROI marked)",
                                                  roiOverlay=True).setShownInReport(True))
        captureResult.addItem(SpectrumCaptureView(caption=label + " — captured frame (ROI)",
                                                  cropped=True).setShownInReport(True))
        captureResult.addItem(SpectrumPlotView(title=label + " — spectrum").setShownInReport(True))
        step.setEvaluationResult(captureResult)
        return step
