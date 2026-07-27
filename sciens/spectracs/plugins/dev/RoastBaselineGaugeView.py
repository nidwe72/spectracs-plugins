from sciens.spectracs.plugin_sdk import VerdictGaugeView, GaugeRender, GaugeColorUtil


class RoastBaselineGaugeView(VerdictGaugeView):
    # SPEC_capture_quality.md §16.10.9 — the SECOND Roast Ampel, driven by the LINEAR-BASELINE pigment ratio
    # instead of the raw Soret/Q. Same preset shape as RoastGaugeView (§8.3a): the plugin owns the colours /
    # labels / gradient and injects them, and caches verdictLabel / swatchColor via GaugeColorUtil so a
    # saved-runs table reads them without maths or a re-run.
    #
    # It exists ALONGSIDE RoastGaugeView, not instead of it. The two run on different scales and must never be
    # compared by number — only by verdict. Keeping both visible is the whole point: the tab-vs-tab comparison
    # is how the recalibration will eventually be judged (Edwin's "eureka" convention, §7b).
    #
    # ─ SCALE, and how provisional it is ────────────────────────────────────────────────────────────────────
    # Anchored on 2026-07-27, ALL 15 runs of that day (spectracs-references/tmp/20260727B + C), §16.10.7:
    #
    #     green (n=9)  12.113   range [10.565 .. 14.209]
    #     brown (n=6)   9.361   range [ 7.714 .. 10.002]
    #
    # Ranked by this metric all 9 green sort above all 6 brown, gap 0.563 — where the raw Soret/Q ratio
    # INTERLEAVES the two classes and no threshold separates them (3 of 15 wrong at best). Cohen's d 1.18 -> 2.48.
    #
    # ⚠ THE THRESHOLD IS PROVISIONAL AND MUST NOT BE READ AS CALIBRATED (§16.10.7 / §16.10.9):
    #   - n = 15, ONE day, ONE pair of oils, ±2.7 % margin at the nearest run
    #   - separation is demonstrated at FIXED DILUTION only; dilution invariance is UNRESOLVED (§16.10.8),
    #     because seating noise alone produces a 1.34x metric spread — as large as a 2.19x dilution change
    #   - the band edges and the two quiet windows were chosen AFTER seeing the tilt problem, so they are
    #     fitted, not independent
    # Fresh-data validation (both classes, n >= 15, one optical configuration) still gates any promotion of
    # this gauge from "second opinion" to headline.
    __THRESHOLD = 10.3      # midpoint of worst-green 10.565 / best-brown 10.002, rounded

    _GOOD = {"text": "#FFFFFF", "bg": "#2f3b1f"}      # white on dark-green chip — matches RoastGaugeView
    _BROWN = {"text": "#FFFFFF", "bg": "#3b241f"}     # white on dark-brown chip
    # Same ramp shape as RoastGaugeView, stretched onto this scale: fresh green at the top, a muted-olive pivot
    # AT the threshold, then brown kicking in aggressively just below it and deepening to the right edge.
    _ANCHORS = [(15.0, "#9B9E57"), (10.3, "#8B8952"), (9.5, "#6E4A22"), (7.0, "#442C0E")]
    _THRESHOLDS = [__THRESHOLD]
    _BAND_LEFT = 15.0       # headroom past the highest green observed (14.209)
    _BAND_RIGHT = 7.0       # headroom past the lowest brown observed (7.714)

    def __init__(self, value, render, caption="Verdict · linear baseline"):
        classes = [{"label": "good — green", "colors": self._GOOD},
                   {"label": "probably too brown", "colors": self._BROWN}]
        util = GaugeColorUtil()
        index = util.classify(value, self._THRESHOLDS, self._BAND_LEFT, self._BAND_RIGHT)
        super().__init__(
            value, render=render, caption=caption,
            bandLeft=self._BAND_LEFT, bandRight=self._BAND_RIGHT, gradientAnchors=self._ANCHORS,
            thresholds=self._THRESHOLDS, classes=classes, valueColor="#FFFFFF",
            verdictLabel=classes[index]["label"],
            swatchColor=util.gradientColorAt(value, self._ANCHORS))
