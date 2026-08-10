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
    # ⚠⚠ THE SCALE MOVED ON 2026-08-10 — the Soret window was trimmed 440-460 -> 448-460 (S1,
    # `SPEC_metric_research.md` §7.13, shipped per `SPEC_soret_448_trim.md`). B_Soret is the numerator, so
    # every number below is ~x0.66 of what this docstring used to say. ⛔ A value read before that date is on
    # the OLD scale and must not be compared with one read after it. The verdict is comparable; the number is
    # not. (Saved runs cache their verdict label at capture time — §18 S2.)
    #
    # ─ SCALE, RE-DERIVED on §16.20.4's own corpus, 448-460 window (`diagnostics/soret_448_thresholds.py`) ────
    #   green (Steirerkraft B+C, n=12)    8.369 +/- 0.352   all-green runs span 7.831 .. 9.166
    #   brown (S-Budget series D, n=6)    5.593 +/- 0.129   all-brown runs span 5.409 .. 5.748
    #   Cohen's d 9.24 (was 9.46 on 440-460 — unchanged within noise)
    #   EMPTY CORRIDOR 5.748 .. 7.831 (width 2.083 = 75 % of the class gap)
    #
    # ─ THE THRESHOLD: 6.8, DERIVED (2026-08-10) ─────────────────────────────────────────────────────────────
    # The corridor midpoint, rounded: (5.748 + 7.831) / 2 = 6.79. ⭐ This is the first time this gauge's line
    # has been DERIVED on its own scale — 10.6 was inherited from RoastBaselineGaugeView's 600-630 scale
    # (§16.10.17d) and never re-fitted here.
    #
    #        T = 10.6 on the old 440-460 scale   green +3.72 sigma   brown +12.90 sigma
    #        T = 6.8  HERE                       green +4.46 sigma   brown  +9.37 sigma
    #
    # ⇒ The line is BETTER BALANCED than the one it replaces: the green margin gains ~20 % while brown still
    # clears by more than 9 sigma. §16.10.17d's policy (a false GREEN ships bad oil, a false BROWN costs a
    # re-check) is preserved — brown keeps twice green's margin — without a hand-applied nudge.
    #
    # ⚠ WHY NOT JUST RESCALE 10.6 BY THE WINDOW FACTOR: the measured M448/M440 ratio is CLASS-DEPENDENT
    # (0.642 on the brown fill, 0.672 on the greens, §16.27), which is exactly why the trim improves d. One
    # multiplier would have sat above every observed factor and pushed the line toward brown.
    #
    # ─ WHAT CHANGED VERDICT (the honest part) ───────────────────────────────────────────────────────────────
    # ⭐ 0 of the 18 derivation-corpus runs change class. Outside it, 2 of 31 do: `Spar ggA` runs 1 and 3 move
    # brown -> green while run 2 stays brown — i.e. that fill now STRADDLES the line. That is §16.27.6's
    # finding restated ("the metric produces a graded scale where T assumes a binary one"), not a regression:
    # T = 10.6 rejected both supermarket ggA oils outright, and this line rejects one of them and splits the
    # other. ⛔ It is still a threshold question the validation study owns (ROADMAP PRIO 3).
    #
    # ⚠ The one green-corpus-adjacent run that reads brown, `Steirerkraft half-strength` #6, read brown on the
    # OLD scale too (10.385 against T = 10.6). It is the deliberate half-dilution fill and it fails identically
    # on both scales — not a regression.
    #
    # ⚠ FRAME COUNT: the corpus was captured at 150 frames/burst; the app now captures 60 (§11). Averaging
    # changes VARIANCE, not the expectation, so the corridor stands — but the sigma margins above are computed
    # on 150-frame spreads and are very slightly optimistic for a 60-frame run.
    #
    # ⚠ AND THE WHOLE SCALE IS PROVISIONAL: three oils, one rig state, and r_Q itself is an instrument
    # constant that does NOT survive a rebuild (§16.19). Re-derive on any mechanical change.
    __THRESHOLD = 6.8

    _GOOD = {"text": "#FFFFFF", "bg": "#2f3b1f"}      # white on dark-green chip — matches its siblings
    _BROWN = {"text": "#FFFFFF", "bg": "#3b241f"}     # white on dark-brown chip
    # Same ramp shape as the siblings, carried onto the trimmed scale: fresh green at the top, a muted-olive
    # pivot AT the threshold, brown kicking in just below it and deepening to the right edge.
    _ANCHORS = [(9.6, "#9B9E57"), (6.8, "#8B8952"), (6.3, "#6E4A22"), (4.8, "#442C0E")]
    _THRESHOLDS = [__THRESHOLD]
    _BAND_LEFT = 9.6        # headroom past the highest green observed (9.166)
    _BAND_RIGHT = 4.8       # headroom past the lowest brown observed (5.409)

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
