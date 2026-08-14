from sciens.spectracs.plugin_sdk import VerdictGaugeView, GaugeRender, GaugeColorUtil


class RoastQPercentGaugeView(VerdictGaugeView):
    # SPEC_v_metric_integration.md §4 — the FOURTH Roast Ampel, and the first one built on `V`
    # (`SPEC_metric_research.md` §10). Same thin-preset shape as its three siblings (SPEC_roast_ampel.md
    # §8.3a): the PLUGIN owns colours / labels / gradient and injects them via super().__init__, and the
    # cached verdictLabel / swatchColor are computed here so a saved-runs table reads them without maths.
    #
    # ─ WHAT THIS METRIC IS ──────────────────────────────────────────────────────────────────────────────────
    #   Q% = 100 * (A_Q - A_valley) / A_Soret = -100 * V      on the DE-SPIKED RAW absorbance
    #        A_Soret 448-460 · A_valley 500-560 · A_Q 565-580          ⛔ NO BASELINE ANYWHERE
    #
    # ⭐ Read as the Q band's height above the valley as a PERCENTAGE of the Soret flank — the absorbance
    # units cancel, so nothing is invented. HIGHER = BROWNER: it reads as a roast index.
    #
    # ⭐⭐ THREE CLASSES, NOT TWO — AND THAT IS A REQUIREMENT, NOT A REFINEMENT. §10.3 says outright that "a
    # fill whose runs straddle the line has no verdict and the gauge must say so rather than average its way
    # to one". Measured at T = 18.6, THREE archived fills straddle it run-to-run:
    #     Steirerkraft half-strength   16.49 17.63 18.38 | 19.35 19.44 19.79      (3.3 units across ONE fill)
    #     Steirerkraft aged 24 h       15.97 18.10 | 19.34
    #     Spar Steirisches g.g.A.      18.06 18.34 | 18.81
    # ⇒ a two-class gauge shows a DIFFERENT VERDICT for two consecutive captures of the same jar. That is the
    # §16.20 defect class: a gauge drawing a confident verdict it cannot support.
    #
    # ─ THE EDGES ARE MEASURED, NOT CHOSEN ───────────────────────────────────────────────────────────────────
    # 18.6 +/- 0.70, where 0.70 is the measured WITHIN-FILL sd — the run-to-run scatter of the very quantity
    # on this axis. And the zone is free: the corpus's empty corridor runs 17.14 .. 20.19, so [17.9, 19.3]
    # sits ENTIRELY inside dead space and ⭐ no corpus run changes class.
    #
    # ⚠ WHAT IT DOES NOT FIX (§4.1a). The zone NARROWS the flip-flop; it does not abolish it. Spar Steirisches
    # is fully absorbed (18.06 / 18.34 / 18.81 — all borderline), but the other two straddlers span more than
    # the zone is wide and still yield both a green and a brown run (half-strength: 16.49 .. 19.79, 3.3 units
    # in ONE fill). ⚠ Both of those are §10.4's own known weaknesses of `V` — a half-concentration prep and a
    # 24 h-aged fill — so the gauge is reporting a real instability, not inventing one.
    #
    # ─ SCALE, on §16.20.4's own 18-run corpus, native sampling (§10.1a) ──────────────────────────────────────
    #   green (Steirerkraft B+C, n=12)   15.940 +/- 1.167   all-green runs span 12.70 .. 17.14
    #   brown (S-Budget series D, n=6)   20.443 +/- 0.260   all-brown runs span 20.19 .. 20.82
    #   empty corridor 17.140 .. 20.191 (3.051 wide)        Cohen's d 5.33
    #
    # ⚠ T = 18.6 is the SHIPPED line; the corridor midpoint is 18.665. 18.6 is kept on the STRICT side per
    # §16.10.17d (a false GREEN is the harder error to make) and matches the one decimal displayed. No
    # archived run lies between the two. ⛔ The signed source of truth is DevSpectralPlugin.V_THRESHOLD.
    #
    # ⚠⚠ PROVISIONAL, and more so than its siblings: `V` is ONE SESSION old and has NOT been tested on data it
    # was not tuned on — ROADMAP PRIO 2c / sigma_fill is that test. Both Spar g.g.A. oils read GREEN here,
    # contradicting §16.30.1a's relabel, and the threshold corpus deliberately excludes the boundary products,
    # so a pill drawn on a Spar oil is an EXTRAPOLATION. Bench instrument only — this gauge is deliberately
    # NOT wired to the PUBLISHING badge (§9, unblocked by sigma_fill).
    #
    # ⚠ A LAMP SWAP moves this metric 4.84 units — more than the whole green/brown span. A chart cannot cross
    # a lamp change, and SPEC_lamp_rebuild.md's rebuild will reset the scale.

    _GOOD = {"text": "#FFFFFF", "bg": "#2f3b1f"}       # white on dark-green chip — matches its siblings
    _BORDERLINE = {"text": "#FFFFFF", "bg": "#6b5320"}  # white on a neutral amber chip — "come back with more data"
    _BROWN = {"text": "#FFFFFF", "bg": "#3b241f"}      # white on dark-brown chip
    # ⚠ ASCENDING band — green is the LOW end here, the reverse of every sibling. GaugeColorUtil is
    # orientation-aware ("band may descend"), so the gradient/marker/classify maths needs no change; only the
    # anchors are listed low-to-high. A value exactly ON a boundary stays in the LEFT (greener) class, which
    # matches §10.3's `V > T_V` convention.
    _ANCHORS = [(12.0, "#9B9E57"), (17.9, "#8B8952"), (19.3, "#6E4A22"), (22.0, "#442C0E")]
    _THRESHOLDS = [17.9, 19.3]
    _BAND_LEFT = 12.0       # headroom past the lowest run on record (12.70)
    _BAND_RIGHT = 22.0      # headroom past the highest run on record (20.82)

    def __init__(self, value, render, caption="Verdict · Q%"):
        classes = [{"label": "good — green", "colors": self._GOOD},
                   {"label": "borderline — re-measure", "colors": self._BORDERLINE},
                   {"label": "probably too brown", "colors": self._BROWN}]
        util = GaugeColorUtil()
        index = util.classify(value, self._THRESHOLDS, self._BAND_LEFT, self._BAND_RIGHT)
        super().__init__(
            value, render=render, caption=caption,
            bandLeft=self._BAND_LEFT, bandRight=self._BAND_RIGHT, gradientAnchors=self._ANCHORS,
            thresholds=self._THRESHOLDS, classes=classes, valueColor="#FFFFFF",
            verdictLabel=classes[index]["label"],
            swatchColor=util.gradientColorAt(value, self._ANCHORS))
