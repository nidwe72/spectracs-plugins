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
    # ⚠⚠ THE SCALE MOVED ON 2026-08-10 — the Soret window was trimmed 440-460 -> 448-460 (`SPEC_soret_448_trim.md`).
    # B_Soret is the numerator, so every number below is ~x0.68 of what this docstring used to say. ⛔ A value
    # read before that date is on the OLD scale; compare verdicts, never numbers, across the cut-over.
    #
    # ─ SCALE, RE-DERIVED on §16.20.4's own corpus, 448-460 window (`diagnostics/soret_448_thresholds.py`) ────
    #   green (Steirerkraft B+C, n=12)   10.560 +/- 0.453   all-green runs span 9.895 .. 11.475
    #   brown (S-Budget series D, n=6)    6.615 +/- 0.151   all-brown runs span 6.436 ..  6.796
    #   Cohen's d 10.25 (was 10.35) — still the best discrimination of the three
    #   EMPTY CORRIDOR 6.796 .. 9.895 (width 3.098 = 78 % of the class gap)
    #
    # ─ THE THRESHOLD: 8.3, DERIVED ──────────────────────────────────────────────────────────────────────────
    # The midpoint of the empty corridor, rounded: (6.796 + 9.895) / 2 = 8.35. Margins that follow:
    #        green +4.99 sigma      brown +11.16 sigma      (was +4.91 / +12.07 at T = 12.5 on 440-460)
    # Derived on its own scale, exactly as its predecessor was — the construction is unchanged, only the
    # numerator window moved. It keeps §16.10.17d's policy shape (brown clears by more than green, so a false
    # GREEN stays the harder error to make) without needing a policy nudge.
    #
    # ─ WHAT CHANGED VERDICT ─────────────────────────────────────────────────────────────────────────────────
    # ⭐ 0 of the 18 derivation-corpus runs change class. Outside it, ONE does: `Steirerkraft A aged 24 h` run 1
    # moves green -> brown. ⭐ That is a CORRECTION, not a regression — §16.11.16 established that fill as a
    # genuinely browner oil (Qy -17 % / 572 nm +14 % per unit Soret, demetallation) whose 3 runs the old scale
    # misclassified as green. The trimmed window catches one of the three. The other two still read green here
    # and brown on the pedestal gauge, so "measure within the hour" remains a VERDICT rule, not a fixed defect.
    #
    # ⚠ FRAME COUNT: the corpus is 150-frame; the app now captures 60 (§11). Averaging changes variance, not
    # expectation, so the corridor stands and the sigma margins are marginally optimistic.
    #
    # ⚠ PROVISIONAL for the same reasons as its siblings: three oils, one rig state, and the anchor itself is
    # a 2026-08 proposal that §16.20.3 has NOT cleared for the end-user verdict. Bench instrument only.
    __THRESHOLD = 8.3

    _GOOD = {"text": "#FFFFFF", "bg": "#2f3b1f"}      # white on dark-green chip — matches its siblings
    _BROWN = {"text": "#FFFFFF", "bg": "#3b241f"}     # white on dark-brown chip
    _ANCHORS = [(12.0, "#9B9E57"), (8.3, "#8B8952"), (7.6, "#6E4A22"), (6.0, "#442C0E")]
    _THRESHOLDS = [__THRESHOLD]
    _BAND_LEFT = 12.0       # headroom past the highest green observed (11.475)
    _BAND_RIGHT = 6.0       # headroom past the lowest brown observed (6.436)

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
