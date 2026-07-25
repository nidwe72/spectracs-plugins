from sciens.spectracs.plugin_sdk import VerdictGaugeView, GaugeRender, GaugeColorUtil


class RoastGaugeView(VerdictGaugeView):
    # SPEC_roast_ampel.md §8.3a — the Roast Ampel as a thin preset of the generic gauge. The PLUGIN owns the
    # colours / labels / gradient and injects them via super().__init__ (constructor injection, D8-accessor).
    # It also computes the cached verdictLabel / swatchColor via GaugeColorUtil — the model must not (core->model
    # already, RD#12) — so a saved-runs table reads them without maths or a re-run (§8.11).
    #
    # Band 6.0 -> 3.0. RECALIBRATED 2026-07-25 (Edwin) for the FRESH 2026 oils: the 2023 oils measured so far had
    # AGED, reading low; fresh oils read higher, so the whole scale shifts up (was band 5.0->2.0, threshold 2.8).
    # One threshold at 4.4 -> two classes; a second brown line + warn class later would be a data-only change
    # (RD#5: a value past an edge clamps only the marker). Band right (3.0) is my pick — Edwin gave the green
    # start (6.0) and the brown threshold (4.4); 3.0 shifts the old brown edge up by the same +1.0 as the start.

    # pill: WHITE text on a dark chip (Edwin 2026-07-24). The ZONES bar reuses the SAME chip colour (`bg`) so a
    # zone matches its verdict badge — the renderer falls back to `bg` for the zone fill.
    _GOOD = {"text": "#FFFFFF", "bg": "#2f3b1f"}      # white on dark-green chip
    _BROWN = {"text": "#FFFFFF", "bg": "#3b241f"}     # white on dark-brown chip
    # fresh green (6.0) -> muted-olive pivot at the 4.4 threshold -> brown kicks in AGGRESSIVELY: a warm brown by
    # 3.8, deepening to a dark brown at 3.0. The 3.8 anchor makes the brown clearly visible right below 4.4.
    _ANCHORS = [(6.0, "#9B9E57"), (4.4, "#8B8952"), (3.8, "#6E4A22"), (3.0, "#442C0E")]
    _THRESHOLDS = [4.4]
    _BAND_LEFT = 6.0        # headroom at the green (high-ratio) end so the marker isn't pinned to the left edge
    _BAND_RIGHT = 3.0

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
