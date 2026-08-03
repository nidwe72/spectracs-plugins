from sciens.spectracs.plugin_sdk import VerdictGaugeView, GaugeRender, GaugeColorUtil


class RoastFar620GaugeView(VerdictGaugeView):
    # SPEC_capture_quality.md §16.20 — the SECOND Roast Ampel: the pigment index computed with the 620-630 far
    # anchor and NO pedestal correction. Same preset shape as its siblings (§8.3a).
    #
    # It sits between RoastPedestalGaugeView (the same anchor WITH the correction) and the raw metric row, so
    # the two halves of the change can be read apart: this gauge against the one above it isolates what the
    # pedestal correction does; this gauge against the raw row isolates what the baseline does. All three run
    # on DIFFERENT SCALES — compare verdicts, never numbers.
    #
    # ─ WHAT THIS METRIC IS ──────────────────────────────────────────────────────────────────────────────────
    #   M = B_Soret / B_Q   with both bands taken above the 520-540 / 620-630 fitted line. Nothing added back.
    #
    # ─ SCALE, measured on the POST-REBUILD archive (§16.20.4, 28 runs) ──────────────────────────────────────
    #   green (Steirerkraft B+C, n=12)   15.559 +/- 0.615   all-green runs span 14.671 .. 18.356
    #   brown (S-Budget series D, n=6)   10.160 +/- 0.197   all-brown runs span  9.957 .. 10.408
    #   Cohen's d 10.35 — the BEST discrimination of the three, and of any window tested (§16.20.4)
    #   EMPTY CORRIDOR 10.408 .. 14.671 (width 4.263 = 79 % of the class gap)
    #
    # ─ THE THRESHOLD: 12.5, DERIVED (§16.20.4) ──────────────────────────────────────────────────────────────
    # The midpoint of the empty corridor, rounded: (10.408 + 14.671) / 2 = 12.54. Margins that follow:
    #        green +4.91 sigma      brown +12.07 sigma
    # This is a DERIVED line, not an inherited one — there was no predecessor on this scale to inherit from.
    # It keeps §16.10.17d's policy shape (brown clears by more than green, so a false GREEN stays the harder
    # error to make) without needing a policy nudge, because the corridor is wide enough on its own.
    #
    # ⚠ PROVISIONAL for the same reasons as its siblings: three oils, one rig state, and the anchor itself is
    # a 2026-08 proposal that §16.20.3 has NOT cleared for the end-user verdict. Bench instrument only.
    __THRESHOLD = 12.5

    _GOOD = {"text": "#FFFFFF", "bg": "#2f3b1f"}      # white on dark-green chip — matches its siblings
    _BROWN = {"text": "#FFFFFF", "bg": "#3b241f"}     # white on dark-brown chip
    _ANCHORS = [(19.0, "#9B9E57"), (12.5, "#8B8952"), (11.5, "#6E4A22"), (9.0, "#442C0E")]
    _THRESHOLDS = [__THRESHOLD]
    _BAND_LEFT = 19.0       # headroom past the highest green observed (18.356)
    _BAND_RIGHT = 9.0       # headroom past the lowest brown observed (9.957)

    def __init__(self, value, render, caption="Verdict · baseline"):
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
