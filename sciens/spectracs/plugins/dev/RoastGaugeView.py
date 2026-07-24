from sciens.spectracs.plugin_sdk import VerdictGaugeView, GaugeRender, GaugeColorUtil


class RoastGaugeView(VerdictGaugeView):
    # SPEC_roast_ampel.md §8.3a — the Roast Ampel as a thin preset of the generic gauge. The PLUGIN owns the
    # colours / labels / gradient and injects them via super().__init__ (constructor injection, D8-accessor).
    # It also computes the cached verdictLabel / swatchColor via GaugeColorUtil — the model must not (core->model
    # already, RD#12) — so a saved-runs table reads them without maths or a re-run (§8.11).
    #
    # Band 4.0 -> 2.0 (wide, anticipates very green/brown oils; a value past an edge clamps only the marker,
    # RD#5). One threshold at 2.8 -> two classes; [2.8, 2.6] + a warn class later would be a data-only change.

    # pill: WHITE text on a dark chip (Edwin 2026-07-24). The ZONES bar reuses the SAME chip colour (`bg`) so a
    # zone matches its verdict badge — the renderer falls back to `bg` for the zone fill.
    _GOOD = {"text": "#FFFFFF", "bg": "#2f3b1f"}      # white on dark-green chip
    _BROWN = {"text": "#FFFFFF", "bg": "#3b241f"}     # white on dark-brown chip
    # fresh-olive (4.5) -> muted-olive pivot (2.8) -> brown kicks in AGGRESSIVELY: a warm brown already by 2.4,
    # deepening to a dark brown at 2.0. The extra 2.4 anchor makes the brown clearly visible right below 2.8
    # (Edwin 2026-07-24 — the old subtle 2.8->2.0 ramp read too olive on-screen).
    _ANCHORS = [(4.5, "#9B9E57"), (2.8, "#8B8952"), (2.4, "#6E4A22"), (2.0, "#442C0E")]
    _THRESHOLDS = [2.8]
    _BAND_LEFT = 5.0        # headroom above the greenest oils (~4.5) so the marker isn't pinned to the left edge
    _BAND_RIGHT = 2.0

    def __init__(self, value, render, caption="Verdict"):
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
