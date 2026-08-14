"""SPEC_v_metric_integration.md §8 — `V` / `Q%` in the DEV plugin.

Covers the four things that can go wrong SILENTLY, i.e. where a mistake renders plausibly and errors
nowhere:

  T1  the FROZEN windows — the one-character difference between `V`'s Q band (565–580) and the
      plugin's existing PB_Q_BAND (560–580), plus the sign convention of the single threshold source
  T2a a hermetic golden — a synthetic spectrum whose band means are known, so a re-tuned window or a
      flipped sign is caught without any data file
  T5  the THREE-class gauge — the borderline zone exists because §10.3 requires it, and the band runs
      the other way round from every sibling
  T6  the zero datum is a RANGED bar (§6.4) — as a full-width line its participation in autoscale is
      not guaranteed on either renderer
  T7  the gauge survives toJson/fromJson — ViewModelFactory SILENTLY DROPS unknown types, so this is
      what proves the preset never becomes one

⛔ The REAL-DATA reconciliation is deliberately NOT here: this suite is hermetic and
`spectracs-references/tmp/` is uncommitted scratch. It lives in `diagnostics/box_terms.py` TABLE 6,
which checks the plugin against `box_metrics.py` on all 58 archived runs (§8, T2b).

Run from the spectracsPy repo root:
    PYTHONPATH=".:../spectracsPy-core:../spectracsPy-model:../spectracsPy-base:../spectracsPy-server:../spectracs-plugins" \
        ./venv/bin/python -m pytest ../spectracs-plugins/tests/test_v_metric.py -q
"""
import unittest

from sciens.spectracs.model.spectral.Spectrum import Spectrum
from sciens.spectracs.model.spectral.plugin.view.ViewModelFactory import ViewModelFactory
from sciens.spectracs.plugin_sdk import GaugeRender

from sciens.spectracs.plugins.dev.DevSpectralPlugin import DevSpectralPlugin
from sciens.spectracs.plugins.dev.RoastQPercentGaugeView import RoastQPercentGaugeView

RENDER = GaugeRender.BAND | GaugeRender.LABEL | GaugeRender.SWATCH


def _flatBandSpectrum(soret, valley, qBand):
    """A spectrum that is CONSTANT inside each of V's three windows, so its band means are exactly the
    three arguments — whatever the sampling convention. That is what makes the golden hermetic: it
    tests the windows and the formula without also testing the quadrature."""
    values = {}
    nm = 440.0
    while nm <= 630.0:
        key = round(nm, 2)
        if 448.0 <= key <= 460.0:
            values[key] = soret
        elif 500.0 <= key <= 560.0:
            values[key] = valley
        elif 565.0 <= key <= 580.0:
            values[key] = qBand
        else:
            values[key] = 0.02          # outside every window — must not reach any band mean
        nm += 0.2
    spectrum = Spectrum()
    spectrum.setValuesByNanometers(values)
    return spectrum


class VMetricWindowsTest(unittest.TestCase):
    # T1 — the frozen constants (§10.1 / §3).

    def setUp(self):
        self.plugin = DevSpectralPlugin()

    def testWindowsAreTheFrozenOnes(self):
        self.assertEqual((448.0, 460.0), self.plugin.V_SORET_BAND)
        self.assertEqual((500.0, 560.0), self.plugin.V_VALLEY_BAND)
        self.assertEqual((565.0, 580.0), self.plugin.V_Q_BAND)

    def testVWindowsAreNotAliasedToThePbOnes(self):
        # ⛔ THE trap of §3: PB_Q_BAND starts at 560 and GREEN_BAND is 510–540. Reusing either would
        # disagree with box_metrics.py and error nowhere.
        self.assertNotEqual(self.plugin.PB_Q_BAND, self.plugin.V_Q_BAND)
        self.assertNotEqual(self.plugin.GREEN_BAND, self.plugin.V_VALLEY_BAND)
        self.assertEqual(565.0, self.plugin.V_Q_BAND[0])

    def testThresholdIsTheSingleSignedSource(self):
        # Q4 — T lives once, in V units with V's sign; the gauge negates it.
        self.assertEqual(-18.6, self.plugin.V_THRESHOLD)
        self.assertAlmostEqual(-self.plugin.V_THRESHOLD,
                               (RoastQPercentGaugeView._THRESHOLDS[0]
                                + RoastQPercentGaugeView._THRESHOLDS[1]) / 2.0, places=6)


class VGoldenTest(unittest.TestCase):
    # T2a — the hermetic golden.

    def setUp(self):
        self.plugin = DevSpectralPlugin()

    def testKnownBandMeansGiveTheKnownQPercent(self):
        # (0.250 − 0.100) / 0.600 = 0.25 ⇒ Q% = 25.0, V ×100 = −25.0
        terms = self.plugin._DevSpectralPlugin__vTerms(
            _flatBandSpectrum(soret=0.600, valley=0.100, qBand=0.250))
        soret, valley, qBand, qPercent = terms
        self.assertAlmostEqual(0.600, soret, places=9)
        self.assertAlmostEqual(0.100, valley, places=9)
        self.assertAlmostEqual(0.250, qBand, places=9)
        self.assertAlmostEqual(25.0, qPercent, places=9)

    def testGreenerOilReadsLower(self):
        # The sign convention, pinned: less Q above the valley = greener = SMALLER Q%.
        greener = self.plugin._DevSpectralPlugin__vTerms(
            _flatBandSpectrum(0.600, 0.100, 0.200))[3]
        browner = self.plugin._DevSpectralPlugin__vTerms(
            _flatBandSpectrum(0.600, 0.100, 0.260))[3]
        self.assertLess(greener, browner)

    def testTheGuardWithholdsRatherThanClamps(self):
        # §3.1 — below the Soret floor there is NO verdict at all, and it is ONE condition (None here
        # is what removes the gauge, blanks the rows AND strips the plot's annotations).
        self.assertIsNone(self.plugin._DevSpectralPlugin__vTerms(
            _flatBandSpectrum(soret=0.10, valley=0.02, qBand=0.05)))
        self.assertIsNone(self.plugin._DevSpectralPlugin__vTerms(None))


class VDomainGuardTest(unittest.TestCase):
    """§3.1a — the SECOND guard: out of domain withholds the PILL but keeps the numbers.

    ⛔ Without it a gauge CLAMPS (GaugeColorUtil, RD#5): the archive contains a report reading
    Q% = 39.90 that would be stamped 'probably too brown', and one reading −28.34 — no Q band above
    the valley at all, i.e. not an oil-shaped spectrum — that would be stamped 'good — green'.
    """

    def setUp(self):
        self.plugin = DevSpectralPlugin()

    def __hasVerdict(self, qBand):
        terms = self.plugin._DevSpectralPlugin__vTerms(_flatBandSpectrum(0.600, 0.100, qBand))
        return self.plugin._DevSpectralPlugin__vHasVerdict(terms)

    def testTheBandMatchesTheGaugesOwn(self):
        # ⚠ Two constants, one meaning — the guard and the gauge scale must not drift apart.
        self.assertEqual((RoastQPercentGaugeView._BAND_LEFT, RoastQPercentGaugeView._BAND_RIGHT),
                         self.plugin.V_VERDICT_BAND)

    def testInsideTheBandKeepsItsVerdict(self):
        self.assertTrue(self.__hasVerdict(0.200))       # Q% = 16.7, an ordinary green oil
        self.assertTrue(self.__hasVerdict(0.223))       # Q% = 20.5, an ordinary brown oil

    def testJustPastTheScoredCorpusStillKeepsItsVerdict(self):
        # ⭐ The band, NOT the scored corridor (12.70..20.82). Thirteen archived runs sit in between —
        # 20260731A/004 at 20.94, 20260808B/001 at 21.08 — and they are real brown-oil measurements.
        self.assertTrue(self.__hasVerdict(0.2258))      # Q% ~= 20.97
        self.assertTrue(self.__hasVerdict(0.2265))      # Q% ~= 21.08

    def testOutsideTheBandLosesTheVerdict(self):
        self.assertFalse(self.__hasVerdict(0.340))      # Q% ~= 40.0, the archive's worst
        self.assertFalse(self.__hasVerdict(0.160))      # Q% ~= 10.0, below the scale

    def testANegativeQPercentNeverCarriesAVerdict(self):
        # ⛔ A_Q BELOW A_valley: there is no Q band, so this is not an oil-shaped spectrum. Clamping it
        # to "good — green" was the single worst case the archive dry-run turned up (−28.34).
        terms = self.plugin._DevSpectralPlugin__vTerms(_flatBandSpectrum(0.600, 0.250, 0.100))
        self.assertLess(terms[3], 0.0)
        self.assertFalse(self.plugin._DevSpectralPlugin__vHasVerdict(terms))

    def testTheNumbersAndThePlotSURVIVEBeingOutOfDomain(self):
        # ⭐ THE DISTINCTION THAT MAKES THIS A SECOND GUARD AND NOT AN EXTENSION OF THE FIRST. An
        # out-of-band Q% is a perfectly good measurement of a sample outside the metric's domain: the
        # band means are real and the bars belong where they are. Only the VERDICT has no basis.
        spectrum = _flatBandSpectrum(0.600, 0.100, 0.340)      # Q% ~= 40
        terms = self.plugin._DevSpectralPlugin__vTerms(spectrum)
        self.assertIsNotNone(terms)                            # ⛔ NOT None — the numbers stand
        self.assertAlmostEqual(40.0, terms[3], places=9)
        view = self.plugin._DevSpectralPlugin__vBandPlot(spectrum)
        self.assertEqual([1, 2, 3, 4, 5], sorted(level[6] for level in view.levels))
        self.assertEqual(1, len(view.markers))


class QPercentGaugeTest(unittest.TestCase):
    # T5 — three classes, ascending band.

    def _label(self, value):
        return RoastQPercentGaugeView(value, render=RENDER).verdictLabel

    def testTheThreeClasses(self):
        self.assertEqual("good — green", self._label(15.9))
        self.assertEqual("borderline — re-measure", self._label(18.6))
        self.assertEqual("probably too brown", self._label(20.3))

    def testBoundariesStayInTheGreenerClass(self):
        # GaugeColorUtil: a value exactly ON a boundary stays LEFT — and left is greener here because
        # the band ASCENDS, the reverse of every sibling gauge.
        self.assertEqual("good — green", self._label(17.9))
        self.assertEqual("borderline — re-measure", self._label(19.3))

    def testValuesPastTheEndsClampInsteadOfInverting(self):
        self.assertEqual("good — green", self._label(5.0))
        self.assertEqual("probably too brown", self._label(25.0))

    def testTheCorpusKeepsItsClasses(self):
        # §4.1 — the borderline zone is paid for out of the empty corridor (17.14 .. 20.19), so no run
        # of the 18-run threshold corpus may land in it. These are the corpus extremes.
        for greenRun in (12.70, 15.94, 17.14):
            self.assertEqual("good — green", self._label(greenRun))
        for brownRun in (20.19, 20.44, 20.82):
            self.assertEqual("probably too brown", self._label(brownRun))

    def testTheBorderlineZoneCatchesWhatItCanAndNoMore(self):
        # ⚠ §4.2 — the honest claim, pinned so nobody over-promises it later. Spar Steirisches, whose
        # three runs sit within 0.75 of each other, is FULLY absorbed: no confident verdict at all.
        for run in (18.06, 18.34, 18.81):
            self.assertEqual("borderline — re-measure", self._label(run))
        # ⛔ But the other two straddlers span more than the zone is wide, so they STILL produce both a
        # green and a brown run. The zone narrows the flip-flop; it does not abolish it — and both of
        # these fills are known-abnormal (half concentration, 24 h aged), i.e. §10.4's own weaknesses.
        self.assertEqual("good — green", self._label(16.49))          # half-strength, run 1
        self.assertEqual("probably too brown", self._label(19.79))    # half-strength, run 6
        self.assertEqual("good — green", self._label(15.97))          # aged 24 h, run 1
        self.assertEqual("probably too brown", self._label(19.34))    # aged 24 h, run 3


class QPercentGaugeRoundTripTest(unittest.TestCase):
    # T7 — ViewModelFactory silently drops unknown "type" tags; prove the preset never becomes one.

    def testThreeClassesSurviveSerialization(self):
        original = RoastQPercentGaugeView(18.9, render=RENDER)
        restored = ViewModelFactory.fromJson(original.toJson())
        self.assertIsNotNone(restored)
        self.assertEqual("gauge", original.toJson()["type"])
        self.assertEqual(3, len(restored.classes))
        self.assertEqual([17.9, 19.3], list(restored.thresholds))
        self.assertEqual(original.verdictLabel, restored.verdictLabel)
        self.assertEqual(original.swatchColor, restored.swatchColor)
        # ⚠ A saved run therefore reloads with the thresholds it was SAVED with — historical fidelity,
        # and the reason the threshold is data in every stored measurement, not code (§8).
        self.assertEqual(12.0, restored.bandLeft)
        self.assertEqual(22.0, restored.bandRight)


class VBandPlotTest(unittest.TestCase):
    # T6 (+ the §6.3 legend and the §6.2 crosshair, on a synthetic curve).

    def setUp(self):
        self.plugin = DevSpectralPlugin()
        self.view = self.plugin._DevSpectralPlugin__vBandPlot(
            _flatBandSpectrum(soret=0.600, valley=0.100, qBand=0.250))

    def testZeroDatumIsARangedBarNotAFullWidthLine(self):
        # §6.4 — as an unranged level it renders via pg.InfiniteLine / ax.axhline, whose participation
        # in autoscale is not guaranteed; ranged, it is a plot() call and in-range by construction.
        zero = [level for level in self.view.levels if level[6] == 5][0]
        self.assertEqual(0.0, zero[0])
        self.assertEqual(448.0, zero[1])
        self.assertEqual(460.0, zero[2])

    def testTheCrosshairArmIsFullWidth(self):
        # The valley level, by contrast, IS full-width — it has to reach the Q band for the numerator
        # to read as a vertical gap.
        crosshair = [level for level in self.view.levels if level[6] == 4][0]
        self.assertAlmostEqual(0.100, crosshair[0], places=9)
        self.assertIsNone(crosshair[1])
        self.assertIsNone(crosshair[2])

    def testEveryAnnotationIsNumberedSoItGetsALegendRow(self):
        # §6.3 — legendRows() only emits rows for NUMBERED levels and LABELLED traces. An unnumbered
        # level is an unexplained line on the plot.
        self.assertEqual([1, 2, 3, 4, 5], sorted(level[6] for level in self.view.levels))
        self.assertEqual(6, len(self.view.legendRows()))   # 5 badges + the named trace

    def testNoPeakMarkerIsAdvertised(self):
        # §6 — `V` is window means only; a λmax marker would advertise a quantity it does not use.
        # The only marker is the crosshair's vertical arm.
        self.assertEqual(["A_valley"], [marker[1] for marker in self.view.markers])

    def testTheGuardStripsEveryAnnotationButKeepsTheWindows(self):
        # §3.1's third consumer: no verdict ⇒ no bars, no crosshair, no zero — but the bands stay,
        # because a WINDOW is a constant of the method, not a measurement.
        blank = self.plugin._DevSpectralPlugin__vBandPlot(
            _flatBandSpectrum(soret=0.10, valley=0.02, qBand=0.05))
        self.assertEqual([], blank.levels)
        self.assertEqual([], blank.markers)
        self.assertEqual(3, len(blank.bands))


if __name__ == "__main__":
    unittest.main()
