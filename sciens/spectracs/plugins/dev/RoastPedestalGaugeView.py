from sciens.spectracs.plugin_sdk import VerdictGaugeView, GaugeRender, GaugeColorUtil


class RoastPedestalGaugeView(VerdictGaugeView):
    # SPEC_capture_quality.md §16.20 — the FIRST and primary Roast Ampel: the pigment index computed with the
    # 620-630 far anchor AND the pedestal residual put back. Same preset shape as its two siblings (§8.3a):
    # the plugin owns colours / labels / gradient and injects them, and GaugeColorUtil caches verdictLabel and
    # swatchColor so a saved-runs table reads them without maths or a re-run.
    #
    # It sits ABOVE RoastFar620GaugeView and the raw metric row. All three run on DIFFERENT SCALES and must
    # never be compared by number — only by verdict. That is the whole reason three are shown.
    #
    # ─ WHAT THIS METRIC IS ──────────────────────────────────────────────────────────────────────────────────
    #   M = B_Soret / (B_Q - r_Q)   with both bands taken above the 520-540 / 620-630 fitted line, and
    #                               r_Q = PB_R_Q = -0.0184 (that anchor's OWN residual, §16.20.2).
    #
    # ─ SCALE, measured on the POST-REBUILD archive (§16.20.4, 28 runs) ──────────────────────────────────────
    #   green (Steirerkraft B+C, n=12)   12.331 +/- 0.465   all-green runs span 11.610 .. 13.337
    #   brown (S-Budget series D, n=6)    8.590 +/- 0.156   all-brown runs span  8.421 ..  8.770
    #   Cohen's d 9.46;  EMPTY CORRIDOR 8.770 .. 11.610 (width 2.840 = 76 % of the class gap)
    #
    # ─ THE THRESHOLD: 10.6, RETAINED (Edwin 2026-08-03) ─────────────────────────────────────────────────────
    # 10.6 is inherited from RoastBaselineGaugeView's 600-630 scale, where §16.10.17d chose it as a POLICY
    # (a false GREEN ships bad oil; a false BROWN costs a re-check). It happens to land inside this metric's
    # corridor too, so it classifies all 28 archived runs correctly.
    #
    # ⚠ IT IS INHERITED, NOT DERIVED ON THIS SCALE, and the balance is not what it was:
    #        T = 10.6 on 600-630 (shipped)   green +4.83 sigma   brown  +9.88 sigma
    #        T = 10.6 HERE                   green +3.72 sigma   brown +12.90 sigma
    #        T = 10.2 (this corridor's midpoint)  green +4.60 sigma   brown +10.27 sigma
    # The green margin is a quarter thinner than the derived midpoint would give, and §16.10.17d's reason for
    # pushing the line up (buying brown detection power when brown sat at ~9.9 sigma) has expired here: brown
    # clears by more than 10 sigma even at the midpoint. Edwin's call was to keep 10.6 so the number does not
    # move twice; 10.2 is the derived alternative if the green side is ever felt to be tight.
    #
    # ⚠ AND THE WHOLE SCALE IS PROVISIONAL: three oils, one rig state, and r_Q itself is an instrument
    # constant that does NOT survive a rebuild (§16.19). Re-derive on any mechanical change.
    __THRESHOLD = 10.6

    _GOOD = {"text": "#FFFFFF", "bg": "#2f3b1f"}      # white on dark-green chip — matches its siblings
    _BROWN = {"text": "#FFFFFF", "bg": "#3b241f"}     # white on dark-brown chip
    # Same ramp shape as the siblings, stretched onto this scale: fresh green at the top, a muted-olive pivot
    # AT the threshold, brown kicking in just below it and deepening to the right edge.
    _ANCHORS = [(14.0, "#9B9E57"), (10.6, "#8B8952"), (9.9, "#6E4A22"), (7.5, "#442C0E")]
    _THRESHOLDS = [__THRESHOLD]
    _BAND_LEFT = 14.0       # headroom past the highest green observed (13.337)
    _BAND_RIGHT = 7.5       # headroom past the lowest brown observed (8.421)

    def __init__(self, value, render, caption="Verdict · baseline + pedestal"):
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
