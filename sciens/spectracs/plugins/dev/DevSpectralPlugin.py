from sciens.spectracs.plugin_sdk import (
    SpectralPlugin, SpectralWorkflowPhaseType, SpectralWorkflowStep, SpectraContainer,
    MeanOp, TransmissionOp, AbsorptionOp, BaselineOffsetOp, MedianFilterOp,
    SpectrumPlotView, CaptureView, SpectrumCaptureView, TabGroupView, ReportView, SeriesPlotView, TableView,
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
import numpy

from sciens.spectracs.plugin_sdk import (
    FrameRing, MonitorEngine, MonitorPolicy, MonitorDecision, MonitorOutcome, MonitorMode,
)
from sciens.spectracs.plugins.dev.RoastPedestalGaugeView import RoastPedestalGaugeView
from sciens.spectracs.plugins.dev.RoastFar620GaugeView import RoastFar620GaugeView
from sciens.spectracs.plugins.dev.RoastQPercentGaugeView import RoastQPercentGaugeView


class ClearingEvaluator:
    """The settling algorithm of SPEC_settled_measurement.md §14 — the PLUGIN's own, not the SDK's.

    ⛔⛔ IT LIVES IN THIS FILE ON PURPOSE (§21/M1). `PluginPublishUtil.lintSelfContained()` rejects a
    plugin source that imports app or sibling code, so a published plugin is ONE module. A plain class
    beside the plugin class is fine (the "more than one plugin class" check only counts SpectralPlugin
    subclasses); a sibling module would make the plugin unpublishable.

    ⭐ COMPOSED, NOT INHERITED (§10.1a-bis): this object is handed to a `MonitorEngine` the plugin
    assembles. The engine owns the ring, the reduce, the centre stamp, the promotion and the caps; this
    class owns what any of it MEANS. It knows the bands; the engine never will.
    """

    version = "clearing-1.0"
    valueKey = "qPercent"

    # ⭐ Every constant here is the PLUGIN's. None of them appear in the SDK (§10.2).
    THETA_PER_MINUTE = 0.0017      # §14.3 — §2.1's "0.005 per 3-minute sample", re-expressed as a RATE
    # ⭐⭐ THE COMPARISON SPAN IS IN SECONDS, NOT IN WINDOWS — and that correction came from replaying the
    # real 2026-08-14 curve (§14.2b/§14.3 rule 3). "j = 2 windows" is only the right answer when a window
    # is ~35 s long: on the archive's 3.3-minute samples the same j doubles a span that was already
    # correct, and the gate fires two samples late. What the noise budget actually asks for is a span of
    # ~70 s or more, so the evaluator walks back to the first decision row at least this far away and
    # works identically at any cadence, on live frames and on a replayed CSV alike.
    GATE_SPAN_SECONDS = 70.0
    GATE_CONSECUTIVE = 2           # k_gate — two consecutive flat comparisons (TEST A)
    TREND_ROWS = 5                 # m — the re-clouding trend baseline (TEST B)
    MATERIAL_FALL = 0.010          # how far A_valley must have fallen BELOW ITS MAXIMUM to call it "was clearing"

    def __init__(self, plugin, reference, mode=None):
        self.__plugin = plugin
        self.__reference = reference
        self.mode = mode or MonitorMode.PRODUCT
        # ⚠ `plugin` is only needed by evaluate() (the metric). decide() is pure arithmetic over rows, so
        # a test may drive the ALGORITHM with no plugin, no camera and no spectra — which is exactly what
        # §11.9b's "replay the 2026-08-14 CSV rows" does.
        self.columns = list(plugin.MONITOR_COLUMNS) if plugin is not None else []
        self.__consecutiveFlat = 0
        self.__gateIndex = None        # index into the decision rows where the gate fired
        self.__branch = None
        self.__reclouded = False

    # --- the two questions the engine asks --------------------------------------------------------

    def evaluate(self, spectrum):
        container = SpectraContainer()
        container.addToSpectra(self.__reference, REFERENCE)
        container.addToSpectra(spectrum, SAMPLE)
        return self.__plugin.monitorMetrics(container)     # ⭐ the plugin's OWN public metric (§10.3)

    def decide(self, rows):
        decisions = [row for row in rows if row.isDecisionRow and not row.provisional]
        if not decisions:
            return MonitorDecision.carryOn()
        latest = decisions[-1]

        # ⛔ §3.1 / §12.3: below the Soret floor the MEASUREMENT is broken — abort at once rather than
        # burning 25 minutes of lamp on a fill that cannot produce a number.
        if not latest.values:
            if len(decisions) >= 2 and not any(row.values for row in decisions[-2:]):
                return MonitorDecision(stop=True, outcome=MonitorOutcome.MEASUREMENT_BROKEN,
                                       note="A_Soret below the floor — no numbers at all (§3.1)")
            return MonitorDecision.carryOn()

        # Already read? A DIAGNOSTIC run keeps observing (§11.9c) — the engine's latch (§14.6) is what
        # makes that safe, so there is nothing more to decide here.
        if self.__gateIndex is not None:
            return self.__afterGate(decisions)

        note = None
        if self.__isReclouding(decisions):
            # ⭐ TEST B (§14.5): a SUSTAINED SIGNED trend, on a LONG baseline where noise averages down.
            # A rise resets the counter AND the clearing clock — the jar cooled below its cloud point on
            # the way in, which is a diagnosable condition, not a glitch.
            self.__consecutiveFlat = 0
            self.__reclouded = True
            note = "re-clouding — the gate was reset (§14.5 TEST B)"
        elif self.__isFlat(decisions):
            # ⭐ TEST A (§14.5): flatness is a MAGNITUDE question on a SHORT baseline. ⛔ The first draft
            # asked one comparison to answer both questions, and on an already-clear fill (true rate 0,
            # measured rate zero-mean noise) a signed test rejected half of all comparisons at random.
            self.__consecutiveFlat += 1
        else:
            self.__consecutiveFlat = 0

        if self.__consecutiveFlat >= self.GATE_CONSECUTIVE and self.__hasFallenSinceMaximum(decisions):
            return self.__fireGate(decisions)
        return MonitorDecision.carryOn(note)

    # --- the gate ---------------------------------------------------------------------------------

    @classmethod
    def rateAt(cls, times, values, index):
        """d(A_valley)/dt in per-MINUTE units, across NON-OVERLAPPING windows ≥ GATE_SPAN_SECONDS apart.

        ⭐ §14.3 rules 1+3. Walking back by TIME rather than by a window count is what makes the criterion
        cadence-independent: the threshold is a rate, so the only thing that must be held is the span it
        is measured over — long enough for the noise budget of §14.2b, and never shorter.

        ⭐ PUBLIC AND SHARED: the gate decides with it AND the Settling plot draws with it, so the picture
        and the decision cannot disagree. A plot that showed a differently-computed rate would be a
        diagnostic that lies about the thing it is diagnosing."""
        for older in range(index - 1, -1, -1):
            minutes = (times[index] - times[older]) / 60.0
            if minutes <= 0:
                return None                 # ⚠ a non-monotonic clock (§25/X3) — refuse rather than invent
            if times[index] - times[older] >= cls.GATE_SPAN_SECONDS:
                if values[index] is None or values[older] is None:
                    return None
                return (values[index] - values[older]) / minutes
        return None                         # not enough history yet to measure a rate at all

    @classmethod
    def ratesOver(cls, times, values):
        return [cls.rateAt(times, values, index) for index in range(len(times))]

    def __rate(self, decisions, index):
        return self.rateAt([row.t for row in decisions],
                           [row.get("valley") for row in decisions], index)

    def __isFlat(self, decisions):
        rate = self.__rate(decisions, len(decisions) - 1)
        return rate is not None and abs(rate) < self.THETA_PER_MINUTE

    def __isReclouding(self, decisions):
        window = [row for row in decisions[-self.TREND_ROWS:] if row.values]
        if len(window) < self.TREND_ROWS:
            return False
        times = [row.t / 60.0 for row in window]
        values = [row.get("valley") for row in window]
        slope, standardError = self.__slopeWithError(times, values)
        if slope is None:
            return False
        return slope > self.THETA_PER_MINUTE and slope > 2.0 * standardError

    def __hasFallenSinceMaximum(self, decisions):
        # ⚠ §14.5: never settle at the TOP of a re-clouding dip. A fill that is flat from the first row
        # satisfies this trivially (its maximum IS the first row and no fall is required).
        valleys = [row.get("valley") for row in decisions if row.values]
        if len(valleys) < 2:
            return True
        maximumIndex = valleys.index(max(valleys))
        return maximumIndex < len(valleys) - 1

    def __fireGate(self, decisions):
        self.__gateIndex = len(decisions) - 1
        valleys = [row.get("valley") for row in decisions if row.values]
        fall = max(valleys) - decisions[-1].get("valley")
        # ⭐ §9.6: ONE algorithm — what the gate SAW picks the read, not what the operator claims.
        if fall >= self.MATERIAL_FALL:
            self.__branch = "was-clearing"
            # The vertex needs the row AFTER the gate as well, so the read waits exactly one more
            # decision row (§14.4) — never the ten further minutes a rise-confirmation would cost.
            return MonitorDecision.carryOn("gate fired (was clearing) — waiting one row for the vertex")
        self.__branch = "arrived-clear"
        return MonitorDecision(promote=True, stop=self.mode == MonitorMode.PRODUCT,
                               outcome=MonitorOutcome.SETTLED_IMMEDIATE, branch=self.__branch,
                               readAs="FIRST_SETTLED_WINDOW", note="settled — the fill arrived clear")

    def __afterGate(self, decisions):
        if self.__branch != "was-clearing":
            return MonitorDecision.carryOn()
        # ⭐ The vertex is read around the Q% MINIMUM, not around the gate row (§2.2: "the minimum, read as
        # a parabola vertex through its three neighbours"). Those are different rows — on the 2026-08-14
        # curve the minimum sits at t = 16.7 while the gate confirms it at 19.9 — and fitting around the
        # gate row instead would fit a rising ramp, whose parabola has no minimum at all.
        usable = [row for row in decisions if row.values]
        if len(usable) < 3:
            return MonitorDecision.carryOn()
        minimumIndex = min(range(len(usable)), key=lambda index: usable[index].get("qPercent"))
        if minimumIndex == len(usable) - 1:
            # The minimum is still the newest row: it may yet fall further, so wait for its right-hand
            # neighbour rather than declaring a minimum that has no other side.
            return MonitorDecision.carryOn()
        window = usable[max(0, minimumIndex - 1):minimumIndex + 2]
        vertex = self.__vertex(window)
        return MonitorDecision(promote=True, stop=self.mode == MonitorMode.PRODUCT,
                               outcome=MonitorOutcome.SETTLED_AFTER_CLEARING, branch=self.__branch,
                               readAs="VERTEX", answer=vertex, promoteRow=usable[minimumIndex],
                               note="settled — read as a parabola vertex")

    def __vertex(self, window):
        """The Q% minimum as a PARABOLA VERTEX through three decision rows (§2.2).

        ⚠ The minimum of n noisy samples is biased LOW by ~0.9 sd because it SELECTS the most negative
        excursion; a vertex through three points AVERAGES instead. ⚠ Guards carried over from the
        prototype (§25/X8): fewer than three usable points, or an upward-opening fit that is not a
        minimum, fall back to the raw row — and np.polyfit RAISES on identical x rather than returning nan.
        """
        usable = [row for row in window if row.values]
        if len(usable) < 3:
            return window[-1].get("qPercent")
        times = [row.t for row in usable]
        values = [row.get("qPercent") for row in usable]
        if len(set(times)) < 3:
            return usable[-1].get("qPercent")
        try:
            a, b, c = numpy.polyfit(times, values, 2)
        except Exception:
            return usable[-1].get("qPercent")
        if not numpy.isfinite(a) or a <= 0:
            return usable[-1].get("qPercent")
        at = -b / (2 * a)
        return float(a * at * at + b * at + c)

    @staticmethod
    def __slopeWithError(times, values):
        n = len(times)
        meanT = sum(times) / n
        meanV = sum(values) / n
        denominator = sum((t - meanT) ** 2 for t in times)
        if denominator <= 0:
            return None, None
        slope = sum((t - meanT) * (v - meanV) for t, v in zip(times, values)) / denominator
        intercept = meanV - slope * meanT
        residuals = [v - (slope * t + intercept) for t, v in zip(times, values)]
        if n <= 2:
            return slope, 0.0
        variance = sum(residual ** 2 for residual in residuals) / (n - 2)
        return slope, (variance / denominator) ** 0.5

    # --- what the operator sees (§13.3) -----------------------------------------------------------

    def coach(self, rows):
        decisions = [row for row in rows if row.isDecisionRow and row.values]
        if not decisions:
            return {"state": "starting …", "progress": ("INDETERMINATE", None), "fields": []}
        latest = decisions[-1]
        rate = self.__rate(decisions, len(decisions) - 1)
        # ⛔ §17/U1: NO provisional Q% is shown. A number displayed while it is still moving is a number
        # somebody writes down — and the settled value may differ by more than the gauge's own boundaries.
        fields = [("turbidity", "%.4f%s" % (latest.get("valley"),
                                            "" if rate is None else "  %+.4f/min" % rate))]
        if self.__reclouded and self.__gateIndex is None:
            return {"state": "re-clouded — warming again …", "progress": ("INDETERMINATE", None),
                    "fields": fields, "severity": "WARN"}
        if self.__gateIndex is not None:
            fields.append(("Q%", "%.1f" % latest.get("qPercent")))
            return {"state": "settled — measuring", "progress": ("INDETERMINATE", None), "fields": fields}
        return {"state": "clearing …", "progress": ("INDETERMINATE", None), "fields": fields}


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

    # --- monitored acquisition (SPEC_settled_measurement.md §10.1a-bis) --------------------------------

    MONITOR_WINDOW_FRAMES = 50     # ⭐ W. §14.2b: bigger is always better at fixed wall-clock, so W_gate = W
    MONITOR_RETENTION_FRAMES = 60  # R = W + margin. ⛔ NOT sized by the run length — the winner is promoted out
    MONITOR_MAX_SECONDS = 1500.0   # 25 min (§12.2), chosen against the 17-min beam-clearing of 2026-08-14

    def createMonitor(self, reference=None, mode=None, frames=None):
        """ASSEMBLE the monitor: an SDK engine + an SDK ring + ⭐ THIS PLUGIN'S OWN evaluator.

        ⭐⭐ COMPOSITION, NOT INHERITANCE (§10.1a-bis). Nothing calls INTO this plugin during a run: the
        host is handed one object and only pushes frames into it. The engine holds a collaborator it was
        given — it does not know the word "plugin", and it names no wavelength.

        ⚠ `reference` is the already-captured blank, held FIXED for the whole run: every row is
        `S_window` against that one `R`. Without it there is nothing to compute absorbance against, so a
        missing reference means no monitor (the host then falls back to a plain burst).
        """
        if reference is None:
            return None
        policy = MonitorPolicy(windowFrames=frames or self.MONITOR_WINDOW_FRAMES,
                               retentionFrames=self.MONITOR_RETENTION_FRAMES,
                               maxSeconds=self.MONITOR_MAX_SECONDS)
        evaluator = ClearingEvaluator(self, reference, mode)
        return MonitorEngine(evaluator, FrameRing(policy.windowFrames, policy.retentionFrames), policy,
                             evaluatorId="dev-clearing", evaluatorVersion=evaluator.version)

    def settlingStep(self, record):
        """The Settling step-tab: the run's own history, from the run's own record (§18).

        ⛔ NO RECORD -> NO STEP. A plain-burst capture has no trajectory, and an empty graph is worse than
        a missing tab — the same convention as "a hook that creates no steps is auto-skipped".

        ⭐ Built from the GENERIC MonitorRecord (§15.2), so nothing here needs the host to understand
        `Q%`: the plugin knows its own column keys and hands over plain numbers under its own labels.
        ⚠ Two panels, ⛔ never one shared y-axis: `Q%` sits near 13 while `A_valley` runs 0.95 -> 0.026,
        and the gate panel is LOG because a 40x fall puts the settling tail — the part being judged — in
        the bottom 3 % of a linear panel (§18.7).
        """
        rows = (record or {}).get("rows") or []
        if not rows:
            return None
        answer = record.get("answer") or {}
        minutes = [row["t"] / 60.0 for row in rows]

        view = SeriesPlotView(title="Settling", xLabel="minutes since insertion")
        outcome = record.get("outcome", "")
        view.addHeaderField("outcome", outcome)
        if answer.get("value") is not None:
            view.addHeaderField("Q%", "%.2f" % answer["value"])
            view.addHeaderField("read", "%s · %s" % (answer.get("readAs", "?"), answer.get("branch", "?")))
            view.addHeaderField("at", "%.2f min" % (answer["t"] / 60.0))
            low, high = self.V_VERDICT_BAND
            # ⛔ §18.7: the 12-22 domain is a HEADER CHIP, not an axis level — drawn as levels it forces a
            # 10-unit axis around a 0.5-unit trajectory and flattens the trace into a line.
            view.addHeaderField("domain", "✓ in domain" if low <= answer["value"] <= high
                                else "⛔ outside %g–%g — no verdict" % (low, high))
        if record.get("clearingSeconds") is not None:
            view.addHeaderField("clearing", "%.2f min" % (record["clearingSeconds"] / 60.0))

        view.addPanel("qPercent", "Q%", scale="linear")
        view.addSeries("qPercent", minutes, [row.get("qPercent") for row in rows], "Q%", "#e08000")
        if answer.get("value") is not None:
            view.addPoint("qPercent", answer["t"] / 60.0, answer["value"], "the answer", "#2ECC71")

        # ⛔⛔ THE θ LINE DOES NOT BELONG ON THIS PANEL, and putting it there was a CATEGORY ERROR (rig
        # screenshot, 2026-08-17): θ = 0.0017 is a RATE, in absorbance per MINUTE; this panel's y-axis is
        # ABSORBANCE. Drawing it at y = 0.0017 asserts an equivalence that does not exist — and it forced
        # the axis to span from 0.0017 up past the data, which is what made the panel unreadable.
        # ⇒ ⭐ the criterion gets its OWN panel, in its own units, where the convergence is actually visible.
        valleys = [row.get("valley") for row in rows]
        view.addPanel("valley", "A_valley 500–560 nm", scale=self.__panelScale(valleys))
        view.addSeries("valley", minutes, valleys, "A_valley", "#4aa3df")

        # ⭐ THE GATE PANEL PROPER: |d A_valley / dt| against θ — the one plot on which "has it settled?"
        # can be read directly. Computed with the SAME ≥70 s span rule the evaluator gates on (§14.3), so
        # the picture and the decision cannot disagree.
        rates = ClearingEvaluator.ratesOver([row["t"] for row in rows], valleys)
        rateMinutes = [minute for minute, rate in zip(minutes, rates) if rate is not None]
        rateValues = [abs(rate) for rate in rates if rate is not None]
        if rateValues:
            view.addPanel("rate", "|Δ A_valley / Δt|  (the gate)", scale=self.__panelScale(rateValues))
            view.addSeries("rate", rateMinutes, rateValues, "rate", "#c8a05a")
            view.addLevel("rate", ClearingEvaluator.THETA_PER_MINUTE,
                          "settled below %g /min" % ClearingEvaluator.THETA_PER_MINUTE)

        for panelKey in ("valley", "rate"):
            if not any(panel["key"] == panelKey for panel in view.panels):
                continue
            if record.get("clearingSeconds") is not None:
                view.addMarker(panelKey, record["clearingSeconds"] / 60.0, "gate fired")
            for note in record.get("notes") or []:
                if "re-clouding" in note:
                    view.addMarker(panelKey, float(note.split("s ")[0]) / 60.0, "re-clouded")

        # ⭐ §18.7: the footer is what makes a saved run re-analysable in a year. A graph without it is a
        # picture, not a record.
        policy = record.get("policy") or {}
        view.addFooterField("policy", "W %s · cadence %s · cap %.0f s"
                            % (policy.get("windowFrames"), policy.get("evaluateEveryNFrames"),
                               policy.get("maxSeconds", 0)))
        view.addFooterField("evaluator", "%s %s" % (record.get("evaluatorId"), record.get("evaluatorVersion")))
        if record.get("distinctFraction") is not None:
            view.addFooterField("distinct frames", "%.1f %%" % (100.0 * record["distinctFraction"]))

        # ⭐⭐ OVERVIEW IS A SUMMARY, NOT A CHART (Edwin, at the rig 2026-08-17 — this REVERSES §18.8's
        # "the first tab must answer alone with both curves"). Three stacked panels in a fixed-height step
        # left every one of them short, and the numbers that actually answer "what did I measure and why
        # can I trust it" are TEXT. ⇒ Overview carries the answer, the read, the guards and the audit
        # line as an aligned metric grid; every curve gets its own full-height tab beside it.
        # ⚠ The cost is stated where it was argued: correlating the gate with the Q% trace now takes two
        # tabs. Edwin has seen both and chosen this.
        group = TabGroupView().addTab("Overview", self.__settlingSummary(record, answer))

        # ⭐ ONE TAB PER GRAPH, at full height. ⚠ shownInReport stays FALSE on them: tabs flatten to
        # sections on paper (§18.8), and the report takes the summary, not three separate pages.
        for panel in view.panels:
            single = SeriesPlotView(title=panel["label"], xLabel=view.xLabel)
            single.panels = [panel]                    # the SAME panel dict — one construction, two homes
            group.addTab(self.__SETTLING_TAB_LABELS.get(panel["key"], panel["key"]), single)

        health = self.__settlingHealthTable(record)
        if health is not None:
            # ⭐ Conditional, and entirely plugin-side: the tab appears only when it has something to say,
            # so a miller's PDF never carries a page of empty diagnostics (§18.8).
            group.addTab("Health", health)
        decisions = self.__settlingDecisionsTable(record)
        if decisions is not None:
            group.addTab("Decisions", decisions)

        step = SpectralWorkflowStep()
        step.setLabel("Settling")
        step.setView(group)
        return step

    def __settlingSummary(self, record, answer):
        """The Overview tab: what was measured, how it was read, and under which rules — as TEXT.

        ⭐ Rendered through the ordinary metric grid (`MetricFieldView`), so it lines up exactly like the
        EVALUATION rows the operator already reads, with tooltips carrying the why. ⛔ No chart here: the
        curves have their own tabs, at a height where they can be read.
        """
        items = [LabelView("Settling — how this measurement was chosen")]
        outcome = record.get("outcome", "?")
        items.append(MetricFieldView(
            "Outcome", outcome,
            "SETTLED_IMMEDIATE = the fill arrived clear · SETTLED_AFTER_CLEARING = it cleared in the beam "
            "and the Q% minimum was read as a parabola vertex · NEVER_SETTLED / CANCELLED / "
            "MEASUREMENT_BROKEN = ⛔ no value at all, and the curve tabs show why.",
            style=MetricFieldViewStyle.builder().labelBold(True).build()))

        if answer.get("value") is not None:
            low, high = self.V_VERDICT_BAND
            inDomain = low <= answer["value"] <= high
            items.append(MetricFieldView(
                "Q%", "%.2f" % answer["value"],
                "The answer. LATCHED at the moment it was read — later rows join the trajectory but can "
                "never replace it, or a noise dip late in the photodamage ramp would steal the value.",
                style=MetricFieldViewStyle.builder().labelBold(True).build()))
            items.append(MetricFieldView(
                "Verdict domain", "✓ inside %g–%g" % (low, high) if inDomain
                else "⛔ outside %g–%g — value stands, NO verdict" % (low, high),
                "§3.1a: outside this band the metric was never scored, so the number is reported but no "
                "verdict is drawn."))
            items.append(MetricFieldView(
                "Read as", "%s · %s" % (answer.get("readAs", "?"), answer.get("branch", "?")),
                "FIRST_SETTLED_WINDOW = the fill was flat from the start, so the first settled window IS "
                "the answer. VERTEX = it was clearing, so the Q% minimum was read as a parabola through "
                "its three neighbours — the raw minimum of noisy samples is biased low."))
            items.append(MetricFieldView("Read at", "%.2f min" % (answer["t"] / 60.0),
                                         "Time of the winning window's CENTRE, measured from the first frame."))
        else:
            items.append(MetricFieldView(
                "Value", "— none —",
                "⛔ A run without a value never reports one. The curve tabs show how far it got; a fill "
                "that has been in the beam has also banked light dose, so a FRESH fill reads truer than "
                "re-measuring this one."))

        if record.get("clearingSeconds") is not None:
            items.append(MetricFieldView(
                "Clearing time", "%.2f min" % (record["clearingSeconds"] / 60.0),
                "When the gate confirmed the fill had stopped clearing. ⭐ Logged with every measurement "
                "because it is a σ_fill component, not a diagnostic curiosity."))
        rows = record.get("rows") or []
        if rows:
            items.append(MetricFieldView("Decision rows", "%d" % len(rows),
                                         "Windows the gate was actually evaluated on."))
            accepted = [row.get("nAccepted") for row in rows if row.get("nAccepted") is not None]
            if accepted:
                items.append(MetricFieldView(
                    "Frames accepted", "%d–%d of %s" % (min(accepted), max(accepted),
                                                        (record.get("policy") or {}).get("windowFrames", "?")),
                    "⚠ A dip WHILE THE FILL CLEARS is expected, not a fault: the C1 frame rejection was "
                    "built for an auto-exposure ramp, and inside a rolling window a clearing sample looks "
                    "like one."))
        for note in record.get("notes") or []:
            items.append(MetricFieldView("Note", note, "Events the evaluator recorded during the run."))

        # ⭐ The audit line — without it a saved run is a picture, not a record (§18.7).
        policy = record.get("policy") or {}
        items.append(MetricFieldView(
            "Policy", "W %s · cadence %s · cap %.0f s" % (policy.get("windowFrames"),
                                                          policy.get("evaluateEveryNFrames"),
                                                          policy.get("maxSeconds", 0)),
            "The rules this run was made under. ⛔ Two runs made under different rules must never be "
            "compared silently."))
        items.append(MetricFieldView("Evaluator", "%s %s" % (record.get("evaluatorId"),
                                                             record.get("evaluatorVersion")),
                                     "Which algorithm, and which version of it, produced the answer."))
        if record.get("distinctFraction") is not None:
            items.append(MetricFieldView(
                "Distinct frames", "%.1f %%" % (100.0 * record["distinctFraction"]),
                "Fraction of camera frames that were NOT a repeat of their predecessor. Duplicates "
                "inflate the noise (a window of W behaves like W × this), so a run whose duplicate rate "
                "drifted is a run whose noise budget drifted with it."))
        for item in items:
            item.setShownInReport(True)
        return items

    # Short tab labels for the per-graph tabs — the panel labels themselves carry units and are too long
    # for a tab bar that already holds Overview / Health / Decisions.
    __SETTLING_TAB_LABELS = {"qPercent": "Q%", "valley": "Turbidity", "rate": "Rate"}

    @staticmethod
    def __panelScale(values):
        """log ONLY when the data actually spans a decade (rig screenshot, 2026-08-17).

        ⛔ A log axis on a nearly FLAT series is worse than useless: pyqtgraph fills the axis with minor
        decade ticks (0.01 · 0.02 · 0.03 · 0.04 · 0.06 …) that overlap into an unreadable smear, around a
        line that never moves. ⭐ Log earns its place on a CLEARING curve (A_valley falls 40×, and on a
        linear axis the settling tail the gate judges would sit in the bottom 3 %) — and nowhere else."""
        usable = [value for value in values if value is not None and value > 0]
        if len(usable) < 2:
            return "linear"
        return "log" if (max(usable) / min(usable)) >= 10.0 else "linear"

    def __settlingHealthTable(self, record):
        """`A_Soret`, DN and `nAccepted` per row — ⭐ shown ONLY when one of them says something.

        ⚠ `nAccepted` DIPPING DURING FAST CLEARING IS CORRECT, not a fault (§23/V2): C1 was built to
        reject the coherent dim group an auto-exposure ramp leaves behind, and inside a rolling window
        during clearing that "ramp" is the measurement. ⇒ the caption says so, rather than leaving a
        reader to read a real dip as a broken capture."""
        rows = record.get("rows") or []
        window = record.get("policy", {}).get("windowFrames")
        dipped = any(row.get("nAccepted") is not None and window and row["nAccepted"] < window for row in rows)
        lowSoret = any(row.get("soret") is not None and row["soret"] < self.V_SORET_FLOOR * 1.5 for row in rows)
        if not (dipped or lowSoret):
            return None
        table = (TableView(title="Capture health",
                           caption="⚠ nAccepted dipping while the fill clears is EXPECTED: the C1 frame "
                                   "rejection was built for an auto-exposure ramp, and inside a rolling "
                                   "window a clearing sample looks like one. It is not a fault.")
                 .addColumn("t", "t", "s", "%.1f").addColumn("soret", "A_Soret", None, "%.4f")
                 .addColumn("n", "frames", None, "%d").addColumn("nAccepted", "accepted", None, "%d"))
        for row in rows:
            table.addRow(row)
        return table

    def __settlingDecisionsTable(self, record):
        """The numeric decision rows — ⚠ DIAGNOSTIC content, so it is withheld from short product runs.

        ⭐ It answers "why exactly THERE?" numerically, which the plots can only answer approximately."""
        rows = [row for row in (record.get("rows") or []) if row.get("isDecisionRow", True)]
        # ⚠ Relaxed from 8 to 2 after the rig (2026-08-17): a settled 1.7-minute run has ~3 decision rows,
        # and on the MASTER bench three rows of "here is exactly what the gate compared" is precisely what
        # the operator wants to see. Two is the floor because one row is not a trajectory.
        if len(rows) < 2:
            return None
        table = (TableView(title="Decision rows",
                           caption="Every row the gate was evaluated on (§14.4).")
                 .addColumn("t", "t", "s", "%.1f").addColumn("valley", "A_valley", None, "%.4f")
                 .addColumn("qPercent", "Q%", None, "%.3f").addColumn("nAccepted", "accepted", None, "%d"))
        for row in rows:
            table.addRow(row)
        return table

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
        # ⭐ THE SETTLING STEP IS DECLARED HERE **REPORT-ONLY** (SPEC_settled_measurement.md §27.11). The
        # operator reads it under Sample, where the measurement happened — ⛔ so no host draws a tab for
        # it here. But the report is assembled from the WORKFLOW's flagged views, and a `Q%` that was
        # CHOSEN deserves to carry the curve it was chosen from onto the paper.
        # ⚠ Declared FIRST, before any measurement maths: a run that produced no value leaves the SAMPLE
        # step uncaptured and the ops below return early — the diagnostic must survive the failure of the
        # measurement it documents.
        phase = workflow.getPhase(SpectralWorkflowPhaseType.PROCESSING)
        settling = self.settlingStep(
            workflow.getMonitorRecord() if hasattr(workflow, "getMonitorRecord") else None)
        if settling is not None:
            phase.addToSteps(settling.setReportOnly(True))

        acquisition = workflow.getPhase(SpectralWorkflowPhaseType.ACQUISITION)
        captured = SpectraContainer()
        for step in acquisition.getSteps().values():
            role = step.getRole()
            if role is None or step.getContainer() is None:
                continue
            captured.addToSpectra(step.getContainer().getSpectra()[role], role)
        if REFERENCE not in captured.getSpectra() or SAMPLE not in captured.getSpectra():
            # ⛔ A monitored run that produced no value deliberately leaves the step uncaptured (§12.1:
            # "a cancelled capture is not a capture"). Return with ONLY the settling tab rather than
            # raising a KeyError three ops down — the operator gets the curve that explains it.
            return

        meaned = MeanOp().apply(captured)              # {reference: mean, sample: mean}
        transmission = TransmissionOp().apply(meaned)  # {transmission}
        absorption = AbsorptionOp().apply(meaned)      # {absorption}

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

    # --- ⭐⭐ `V` / `Q%` — SPEC_metric_research.md §10, SPEC_v_metric_integration.md §3. -------------------
    #
    #   V  = (A_valley − A_Q) / A_Soret        Q% = −100·V = 100·(A_Q − A_valley)/A_Soret
    #        on the DE-SPIKED RAW absorbance, ⛔ NO BASELINE ANYWHERE
    #
    # ⛔⛔ THESE ARE NOT THE PB WINDOWS AND MUST NOT BE ALIASED TO THEM. `V`'s Q band starts at 565, not the
    # 560 of PB_Q_BAND, and its valley is 500–560, not GREEN_BAND's 510–540. §10.1's edge test found the Q
    # window is the SENSITIVE one (572–578 destroys the separation outright). Reusing PB_Q_BAND here would
    # render plausibly, disagree with `diagnostics/box_metrics.py`, and NOTHING WOULD ERROR —
    # `tests/test_v_metric_windows.py` is what catches it.
    #
    # ⛔ FROZEN 2026-08-14 — pre-registration. Changing them invalidates ROADMAP PRIO 2c / σ_fill, which is
    # the test of whether `V` survives data it was not tuned on. Do not re-tune.
    #
    # ⭐ WHY THIS CONSTRUCTION. The numerator is a DIFFERENCE, so any additive offset (stray light, scattering,
    # seating) cancels — both bands carry it equally. The denominator is a LEVEL, so multiplicative scale
    # (concentration, exposure) cancels. Same immunity the fitted chord provides, obtained arithmetically —
    # and because nothing is fitted, NO ANCHOR CAN CONTAMINATE IT. That is not academic: §16.31.3a measures
    # the shipped chord's far foot sitting ON the Qy band, giving every fill its own baseline slope.
    V_SORET_BAND = (448.0, 460.0)     # same window as PB_SORET_BAND — deliberately declared separately
    V_VALLEY_BAND = (500.0, 560.0)    # the rising flank between the bands (NOT a basin — §6.2)
    V_Q_BAND = (565.0, 580.0)         # the Q band — ⛔ 565, not 560
    # §10.3 — the shipped line, in V×100 with V's sign. ⭐ THE ONE SIGNED SOURCE: the gauge negates this to
    # get its +18.6, so the two repositories cannot drift apart. Corridor midpoint is −18.665; −18.6 is kept
    # on the STRICT side per §16.10.17d (a false GREEN is the harder error to make) and matches the one
    # decimal displayed. No archived run lies between the two.
    V_THRESHOLD = -18.6
    # §3.1 — below this Soret level there is NO VERDICT AT ALL and no numbers either. The archive minimum is
    # 0.334, so this only fires on a broken capture. ⚠ It withholds rather than clamps: a clamped pill is a
    # lie with a number. ⭐ Verified on real data: it fires on all 27 runs of the 20260806A NULL SERIES.
    V_SORET_FLOOR = 0.15
    # §3.1a — the DOMAIN of the verdict, and a SECOND guard on the same withhold-don't-clamp principle.
    # A gauge CLAMPS a value past its band edge (GaugeColorUtil, RD#5) — so without this, `Q%` = 39.90 draws
    # a confident "probably too brown" and `Q%` = -28.34 draws "good — green", on samples the metric has no
    # opinion about at all. ⛔ A NEGATIVE Q% means A_Q sits BELOW the valley: there is no Q band, so it is
    # not an oil-shaped spectrum. Measured over the whole archive: 38 of 143 reports fall outside this band,
    # 34 of them the loose pre-rebuild one-offs.
    #
    # ⚠ THE BAND, NOT THE SCORED CORRIDOR (12.70..20.82). Thirteen archived runs sit in between — e.g.
    # 20260731A/004 at 20.94 and 20260808B/001 at 21.08 — and those ARE real brown-oil measurements a hair
    # past the corpus. They keep their verdict. The band is the gauge's own declared scale; past it, the
    # metric was never scored at all.
    #
    # ⭐ IT WITHHOLDS THE PILL ONLY — the numbers and the plot survive. Unlike a sub-floor Soret, an
    # out-of-band Q% is a PERFECTLY GOOD MEASUREMENT of a sample outside the metric's domain: the band means
    # are real, the bars sit where they belong, and blanking them would destroy evidence. It is the VERDICT
    # that has no basis, and only the verdict is withheld.
    # ⚠ Must equal RoastQPercentGaugeView's own band — `test_v_metric_windows` asserts it.
    V_VERDICT_BAND = (12.0, 22.0)

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
    # SPEC_v_metric_integration.md §6.4 — CONSTRUCTION on the V tab (the crosshair and the zero datum), as
    # distinct from a MEASURED bar. Cool against the warm gold/yellow so "this is a reference line, not a
    # reading" survives at a glance. Both are drawn dashed for the same reason the fitted baseline is.
    __V_CONSTRUCTION = "#8fb8d8"

    # --- SPEC_soret_448_trim.md §13 — THE DN GUARD, now plugin-owned data rather than a renderer constant.
    # §16.23.8 states it two-sided and this plugin is where the numbers belong: the guards are the REJECTION
    # edges (below 16 too concentrated, above ~60 too dilute) and the target pair is where a fill should
    # actually land. Both are declared on the Reference-vs-Sample plot AND handed to the live capture preview
    # via CaptureView, so the operator, the screen and the PDF quote one set of numbers.
    # ⚠ Read in DISPLAY DN (axis="dn"), which is where the operator judges dilution — not gamma-decoded again.
    DN_GUARD_LOW = 16.0               # below: quantization-limited, the bin is not a measurement (§17.6/11)
    # --- REVISED 2026-08-12, SPEC_capture_quality.md §16.23.10 -----------------------------------------------
    # ⭐ TARGET 20-40 -> 20-50, and the 16/60 REJECTION EDGES ARE NO LONGER DRAWN.
    #
    # ⛔ Why the old pair had to go: §16.23.8 justified 20-40 from the A = 1/ln10 = 0.434 optimum via
    # "R ≈ 88 ⇒ S ≈ 32 DN". That arithmetic only closes in LINEAR units, and these thresholds are ENCODED
    # (§16.23.10b, settled on `20260804A`). Converted properly the 0.434 optimum lands at ≈ 120 DN — past even
    # the old "too dilute" line. The band was never in the space its own derivation assumed.
    #
    # ⚠ 20-50 is EDWIN'S WORKING WINDOW (§16.23.10e), not a derived one:
    #   - fits the oil under test: BillaClever at 2 capillaries / 8 mL predicts 39.9 DN
    #   - catches all 6 runs of `20260804A` (74-98 DN), the session §16.24.7 calls over-dilute
    #   - ⛔ BUT the 8 archive runs whose `A_Q` is already correct span 48.6-66.6 DN — 7 of 8 read
    #     "too dilute" against it
    # ⛔ A fixed DN band CANNOT carry a dilution verdict across oils: guard-at-correct-dilution tracks the
    # oil's own band ratio at r = -0.985, ~46 DN of drift over the observed range (§16.23.10d). This pair is a
    # GROSS-ERROR ENVELOPE. The real criterion is `A_Q` ∈ 0.19-0.23 and it lives in EVALUATION.
    #
    # ⭐ 16 and 60 are no longer drawn: across 34 archive runs the minimum ever observed was 37.6 DN, so the
    # rejection edges only added ink to the one plot the operator actually reads. The 16 CHECK survives in the
    # host's CAPTURE-LOWDN log line, where it is the one thing that would catch a genuinely broken capture.
    DN_TARGET_LOW = 20.0
    DN_TARGET_HIGH = 50.0
    # The window `min(S)` is taken over — the metric's own Soret window (§16.23.10f). ⚠ COUPLED: retrim
    # PB_SORET_BAND and this guard moves with it, silently. Accepted by Edwin; recorded so it is not
    # rediscovered as a bug.
    DN_GUARD_BAND = PB_SORET_BAND
    __GUARD_COLOR = "#c87a3c"
    __TARGET_COLOR = "#6b7f5a"
    # The measured reading is painted green inside the target pair, red outside (§16.23.10f).
    __MEASURED_INSIDE_COLOR = "#2ECC71"
    __MEASURED_OUTSIDE_COLOR = "#E74C3C"

    def dnLevels(self):
        # The target pair, built ONCE and handed to both consumers: the PROCESSING Reference-vs-Sample plot and
        # the SAMPLE capture step's live preview. Dotted = the window a fill should land in. The dashed
        # rejection edges are gone (see DN_TARGET_LOW above) — what the operator needs at the bench is "am I in
        # the window?", and the edges were never reached.
        return [(self.DN_TARGET_LOW, None, None, None, self.__TARGET_COLOR, "dotted", None),
                (self.DN_TARGET_HIGH, None, None, "20–50 DN target (provisional)", self.__TARGET_COLOR,
                 "dotted", None)]

    def dnGuardColors(self):
        return {"inside": self.__MEASURED_INSIDE_COLOR, "outside": self.__MEASURED_OUTSIDE_COLOR}

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
    WAVELENGTH_MIN_NM = 400.0
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
        # ⚠ RENAMED 2026-08-14 (SPEC_v_metric_integration.md §6): this tab was "Absorption (bands)" and the
        # NAME now belongs to the V plot below. A saved run from before the cut-over therefore shows the CHORD
        # picture under the new name — the plot TITLES differ ("A(λ) — PB bands" vs "A(λ) — V bands"), so the
        # picture self-identifies even where the tab label does not. Same "compare verdicts, never numbers
        # across a cut-over" rule §16 applies to the 448 trim.
        newSpectrumStep.setLabel("Absorption (bands, baseline)")
        newSpectrumStep.setView(self.__bandPlot(despikedAbsorption, newQLambda))

        # ⭐⭐ THE `V` PICTURE (SPEC_v_metric_integration.md §6) — one curve, no baseline, three bars, the
        # valley crosshair and a zero datum, so the metric's numerator and denominator are both distances on
        # screen. It is flagged into the PDF ADDITIVELY: the chord plot keeps its own flag and the report
        # grows by a page rather than swapping one picture for another (Edwin).
        vSpectrumStep = SpectralWorkflowStep()
        vSpectrumStep.setLabel("Absorption (bands)")
        vSpectrumStep.setView(self.__vBandPlot(despikedAbsorption))

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
        # ⚠ 2026-08-14 (SPEC_v_metric_integration.md §1): the V plot takes the "Absorption (bands)" name and
        # the primary slot; the chord plot follows it as "(bands, baseline)". Newest-first, as with the rows.
        # Metrics, Absorption (bands), Absorption (bands, baseline), Report, Metrics (dev), Absorption (dev).
        phase.addToSteps(newStep)          # "Metrics"
        phase.addToSteps(vSpectrumStep)    # "Absorption (bands)"        — the V picture
        phase.addToSteps(newSpectrumStep)  # "Absorption (bands, baseline)" — the chord picture
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

    def __vBandPlot(self, despikedAbsorption):
        # SPEC_v_metric_integration.md §6 — the `V` picture. A DIFFERENT picture from __bandPlot, which is why
        # it is a different tab: ONE curve, NO fitted baseline, NO subtracted curve, because `V` subtracts
        # nothing. Everything it needs is already a view-model primitive.
        #
        # ⭐⭐ THE POINT: THE PICTURE IS THE ARITHMETIC. With the crosshair and the zero datum drawn, both
        # halves of the formula are DISTANCES ON SCREEN —
        #     the gap from ④ the valley crosshair up to ③ the Q bar  IS the NUMERATOR
        #     the gap from ⑤ zero              up to ① the Soret bar IS the DENOMINATOR
        # Same rule the chord tab was built on (SPEC_soret_448_trim.md §12.3/§25.1). ⛔ Without ⑤ the
        # denominator has nothing to be measured against and the plot tells half the story.
        #
        # ⚠ ⑤ IS A RANGED BAR, NOT A FULL-WIDTH LINE, and that is deliberate. An unranged level renders as a
        # pg.InfiniteLine on screen and ax.axhline on paper, and whether either participates in the view's
        # AUTO-RANGE is not something this plot is willing to assume. A ranged level is a plain plot() call on
        # both paths, so it is in-range by construction — and 448–460 is exactly where the denominator is
        # read anyway. `tests/test_v_zero_datum_is_ranged.py` pins it.
        #
        # ⚠ NO λmax MARKER, unlike the chord tab. That tab marks it because D_Q measures a PEAK HEIGHT; `V`
        # is window means only, and a peak marker would advertise a quantity this metric does not use.
        util = SpectrumFeatureUtil()
        view = SpectrumPlotView(title="A(λ) — V bands (despiked)")
        view.setLegend(LegendPosition.NORTH_EAST, padding=34.0)
        view.addTrace(despikedAbsorption, "A(λ) despiked", self.__RAW_COLOR)

        # ⚠ The §3.1 guard: with no verdict there are no annotations either — every bar, the crosshair and the
        # zero datum each assert a number we just declined to report. The bands stay: a WINDOW is a constant
        # of the method, not a measurement.
        vTerms = self.__vTerms(despikedAbsorption)
        for band, caption in ((self.V_SORET_BAND, "S"), (self.V_VALLEY_BAND, "valley"),
                              (self.V_Q_BAND, "Q")):
            view.addBand(*band, caption)
        if vTerms is None:
            return view.setShownInReport(True)
        soret, valley, qBand, _ = vTerms

        # ⭐ GOLD, and that is a semantic choice: on the chord tab __ANCHOR_BAR means "measured on the RAW /
        # despiked curve", which is exactly what all three of these are. Cyan (__METRIC_BAR) would have meant
        # "measured on the subtracted curve" — and this tab HAS no subtracted curve, so it would have been a
        # false statement two tabs apart. The colour keeps meaning one thing across both plots.
        for number, band, label, mean in ((1, self.V_SORET_BAND, "Soret band mean", soret),
                                          (2, self.V_VALLEY_BAND, "valley band mean", valley),
                                          (3, self.V_Q_BAND, "Q band mean", qBand)):
            view.addLevel(mean, band[0], band[1], label=label, color=self.__ANCHOR_BAR, number=number)

        # ⭐ THE CROSSHAIR (§6.2). Horizontal arm at A_valley — exactly the number V uses — and the vertical
        # arm at the λ where the curve ATTAINS it, so the cross-point sits ON the curve and both arms are
        # true statements at once. Measured across 58 runs: 522.2 ± 1.5 nm.
        # ⛔ NOT at the window's minimum: that sits at the LEFT edge (~509 nm) and is 23 % BELOW A_valley, so
        # a cross drawn there would render fine and be silently false (§6.2).
        view.addLevel(valley, label="valley level  A(λ) = A_valley", color=self.__V_CONSTRUCTION,
                      style="dashed", number=4)
        crossing = util.levelCrossing(despikedAbsorption, *self.V_VALLEY_BAND, valley)
        if crossing is not None:
            view.addMarker(crossing, "A_valley")
        view.addLevel(0.0, self.V_SORET_BAND[0], self.V_SORET_BAND[1], label="zero",
                      color=self.__V_CONSTRUCTION, style="dashed", number=5)
        return view.setShownInReport(True)

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

        # ⭐⭐ `V` / `Q%` (SPEC_v_metric_integration.md §3). Computed on the SAME despiked curve, on its OWN
        # frozen windows, with NO baseline. `__vTerms` returns None when the §3.1 guard trips — ONE condition,
        # three consumers (no gauge, "—" rows, no annotations on the V plot).
        vTerms = self.__vTerms(despikedAbsorption)
        # ⚠ TWO guards, and they are not the same one (§3.1 / §3.1a). `vTerms` is None only when the
        # measurement is broken; `hasVerdict` is False whenever the SAMPLE is outside the metric's domain,
        # in which case the rows and the plot still report what was measured.
        qPercent = vTerms[3] if self.__vHasVerdict(vTerms) else None

        dilutionInvariant = MetricFieldViewStyle.builder().labelBold(True).build()
        result = EvaluationResult()
        # ⭐ `Q%` SITS ABOVE §16.20's LADDER — it is NOT a rung of it. The ladder's rungs differ from each
        # other by HOW MUCH CORRECTION was applied (pedestal vs plain baseline), which is why each adjacent
        # pair isolates exactly one change; those two stay adjacent below and that property is untouched.
        # `Q%` is a DIFFERENT METRIC on a different construction — raw curve, no chord anywhere — so it heads
        # the tab as the thing this project intends to ship, not as "one more step of correction".
        # ⚠ ALL THREE RUN ON DIFFERENT SCALES: compare verdicts, never numbers.
        # ⚠ And this one is ONE SESSION OLD (PRIO 2c / σ_fill is its out-of-sample test) — which is exactly
        # why it is NOT wired to the PUBLISHING badge (SPEC_v_metric_integration.md §9).
        if qPercent is not None:
            result.addItem(RoastQPercentGaugeView(
                qPercent, render=GaugeRender.BAND | GaugeRender.LABEL | GaugeRender.SWATCH))
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
        # ⭐ The `V` rows head the metric block (SPEC_v_metric_integration.md §5) — the eye lands on colour,
        # then on the metric this project intends to ship, then on the older ladder below it.
        self.__addVMetrics(result, vTerms)
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

    # --- the PUBLIC metric API (SPEC_settled_measurement.md §10.3) -------------------------------------

    MONITOR_COLUMNS = [
        {"key": "qPercent", "label": "Q%", "unit": ""},
        {"key": "soret", "label": "A_Soret 448-460", "unit": "A"},
        {"key": "valley", "label": "A_valley 500-560", "unit": "A"},
        {"key": "qBand", "label": "A_Q 565-580", "unit": "A"},
    ]

    def monitorMetrics(self, container):
        """{REFERENCE, SAMPLE} -> {"qPercent", "soret", "valley", "qBand"} — or {} below the §3.1 floor.

        ⭐⭐ THE DRY KEYSTONE (SPEC_settled_measurement.md §10.3). `diagnostics/clearing_time_course.py`
        used to reach in through NAME MANGLING (`plugin._DevSpectralPlugin__vTerms(...)`) because there was
        no API — the DRY instinct fighting the absence of one. This is that API.

        ⚠ NOTE WHO CALLS IT, because it is the whole point of §10.1a-bis: this plugin's OWN evaluator
        calls it, and a diagnostic script may call it directly. ⛔ The SDK never does — it does not know
        the method exists. The plugin is not asked for `Q%` by the machinery; it computes `Q%` for itself,
        inside an object it built, and emits a row the machinery merely carries.

        ⚠ Returns {} rather than raising when the Soret floor is not met: the caller's row legitimately
        carries no values at all (§25/X3), and a clamped number would be a lie with a number attached.
        """
        absorption = AbsorptionOp().apply(container).getSpectra().get(ABSORPTION)
        terms = self.__vTerms(self.__despikedAbsorption(absorption))
        if terms is None:
            return {}
        soret, valley, qBand, qPercent = terms
        return {"qPercent": qPercent, "soret": soret, "valley": valley, "qBand": qBand}

    def __vTerms(self, despikedAbsorption):
        """(A_Soret, A_valley, A_Q, Q%) on the de-spiked RAW absorbance — or None if there is no verdict.

        SPEC_v_metric_integration.md §3. ⭐ ONE guard, evaluated ONCE and passed down, because it has three
        consumers: the gauge, the metric rows and the V plot's annotations. Returning None rather than a
        clamped value is the point — §3.1: a clamped pill is a lie with a number attached.

        ⚠ This stays the internal form (it returns a TUPLE, which the three consumers unpack); the public
        `monitorMetrics()` above is the named-dict face of the SAME computation. ⛔ There is exactly ONE
        definition of the metric and it is here — a second copy "for a while" is the §10.1a failure in
        miniature (SPEC_settled_measurement.md §21/M4).
        """
        if despikedAbsorption is None:
            return None
        util = SpectrumFeatureUtil()
        soret = util.bandMean(despikedAbsorption, *self.V_SORET_BAND)
        valley = util.bandMean(despikedAbsorption, *self.V_VALLEY_BAND)
        qBand = util.bandMean(despikedAbsorption, *self.V_Q_BAND)
        if soret is None or valley is None or qBand is None or soret < self.V_SORET_FLOOR:
            return None
        return soret, valley, qBand, 100.0 * (qBand - valley) / soret

    def __vHasVerdict(self, vTerms):
        """Whether `Q%` may carry a VERDICT — §3.1a, the second guard.

        ⚠ Deliberately separate from `__vTerms`. A sub-floor Soret means the MEASUREMENT is broken, so
        nothing is reported. An out-of-band `Q%` means the measurement is fine and the SAMPLE is outside
        this metric's domain — the numbers stay, the pill goes. Conflating the two would blank evidence.
        """
        if vTerms is None:
            return False
        low, high = self.V_VERDICT_BAND
        return low <= vTerms[3] <= high

    def __addVMetrics(self, result, vTerms):
        # SPEC_v_metric_integration.md §5 — the FIVE `V` rows, at the head of the metric block.
        soret, valley, qBand, qPercent = (None, None, None, None) if vTerms is None else vTerms
        text = lambda value, digits: "—" if value is None else ("%.*f" % (digits, value))
        dilutionInvariant = MetricFieldViewStyle.builder().labelBold(True).build()
        # ⚠ ONE DECIMAL, and that is a measured choice: the within-fill sd of this quantity is 0.70 and the
        # refill floor 0.21 (§10.5), so a second decimal would be theatre.
        result.addItem(MetricFieldView("Q%", text(qPercent, 1),
            "100 × (A_Q − A_valley) ÷ A_Soret on the DE-SPIKED RAW absorbance — the Q band's height above the "
            "valley as a percentage of the Soret flank, with NO baseline anywhere. HIGHER = BROWNER; the line "
            "is 18.6. ⭐ Best metric on record (class gap 5.05 σ vs M448's 3.80; separates under BOTH contested "
            "labellings; 17/18 fills ordered right; a ±40 % dose change costs 0.12). ⛔ A LAMP SWAP moves it "
            "4.84 — more than the whole green/brown span — so a chart cannot cross a lamp change; half "
            "concentration moves it 2.19. ⚠ NOT yet tested on data it was not tuned on (PRIO 2c / σ_fill). "
            "⛔ Outside 12–22 the value is still reported but NO VERDICT is drawn: the metric was never "
            "scored there, and a negative Q% means there is no Q band above the valley at all.",
            style=dilutionInvariant))
        # ⚠ TWO decimals here, deliberately: this row's entire job is to be diffed against
        # `diagnostics/box_metrics.py`, which prints two. It is the audit trail for the frozen definition.
        result.addItem(MetricFieldView("V ×100 (frozen def.)", text(None if qPercent is None else -qPercent, 2),
            "(A_valley − A_Q) ÷ A_Soret × 100 — the FROZEN form the spec, the diagnostics and PRIO 2c's "
            "pre-registration all speak (SPEC_metric_research.md §10.1). Q% is exactly −1 × this. Shown so a "
            "window silently drifting out of sync with the frozen definition is visible on screen."))
        result.addItem(MetricFieldView("A_Soret · 448–460 nm", text(soret, 3),
            "V's denominator: mean absorbance over the Soret flank on the de-spiked RAW curve. ⭐ It never "
            "drops below 0.334 on the archive and never gets within 7.5 σ of zero — which is why V divides by "
            "it and M448's B_Q (6 σ from zero) is the fragile one."))
        result.addItem(MetricFieldView("A_valley · 500–560 nm", text(valley, 3),
            "The window between the two bands. ⚠ NOT a basin: its minimum sits at the LEFT edge (~509 nm) and "
            "the curve rises from there toward the Q band, so this is a slope average, 23 % above the true "
            "minimum. ⛔ Which is why 'the valley is the pigment's own zero' does not hold — see W in §10.2."))
        result.addItem(MetricFieldView("A_Q · 565–580 nm", text(qBand, 3),
            "V's Q band. ⛔ 565, NOT the 560 of the 'Q · 560–580 nm' row below — these are different windows "
            "and the edge test found this the sensitive one (572–578 destroys the separation)."))

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
        # SPEC_capture_quality.md §16.23.10f: alongside the drawn target pair, the SAMPLE step declares WHERE
        # the low-DN statistic is evaluated (`guardBandNm`), WHAT counts as acceptable (`guardTargetDn`) and
        # HOW the measured reading is painted (`guardColors`). Declaring the target separately from `levels` is
        # deliberate — `levels` is what gets DRAWN, `guardTargetDn` is the RULE, and both are built from the
        # same two constants so they cannot drift.
        captureView = CaptureView(prompt=prompt,
                                  captureLabel="Capture " + label.lower(), geometry="transmission",
                                  wavelengthMinNm=self.WAVELENGTH_MIN_NM, wavelengthMaxNm=self.WAVELENGTH_MAX_NM,
                                  croppedPreview=True,  # Change A: cropped-ROI live preview (permanent, phase X)
                                  levels=(self.dnLevels() if role == SAMPLE else []))
        if role == SAMPLE:
            captureView.setGuardBand(self.DN_GUARD_BAND[0], self.DN_GUARD_BAND[1],
                                     targetDn=(self.DN_TARGET_LOW, self.DN_TARGET_HIGH),
                                     colors=self.dnGuardColors())
        step.setView(captureView)
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
