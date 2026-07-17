# spectracs-plugins

**The plugins tier.** Each `SpectralPlugin` lives here, isolated from the app: it imports the
`sciens.spectracs.plugin_sdk` façade (from `spectracsPy-core`) and **nothing app-side** — no Qt, no camera, no
session, no server. Open this repo alone in an IDE and you see the SDK, not the application.

Extracted from `spectracsPy` on 2026-07-17. Design + rationale:
[`spectracsPy/docs/SPEC_project_structure.md`](../spectracsPy/docs/SPEC_project_structure.md) (phase **S5**).

## What's in here

```
sciens/spectracs/plugins/
    pumpkin/PumpkinOilPlugin.py     Pumpkin-seed-oil colour QM (the shipping plugin)
    dev/DevSpectralPlugin.py        the bench's generic driver (ref/sample -> T/A, no verdict)
tests/
    test_pumpkin_plugin_boundary.py T2 — drives the hooks directly on -core-synthesized spectra
```

`codeRef` (the plugin identity M3 signs) is the dotted path, e.g.
`sciens.spectracs.plugins.pumpkin.PumpkinOilPlugin.PumpkinOilPlugin`.

## The isolation is dev-time; at runtime it is one merged tree

`android/spike/stage_app_src.sh` rsyncs every sibling's `sciens/` into one namespace-merged tree, and the desktop
run recipe puts them all on `PYTHONPATH`. So a plugin *loads by plain import* (until M3-B3's DB loader replaces
that) and *can* physically reach app code at runtime — the "app absent" property is a **PyCharm/CI discipline**, not
a sandbox. Enforced mechanically:

```bash
# MUST print nothing — a plugin source file may import only the plugin_sdk façade
grep -rE 'from sciens\.spectracs\.' sciens | grep -v 'plugin_sdk'
```

## Tests

Two tiers (SPEC_project_structure.md, the S5 subsection):

- **T1** — the full `engine.runAll` end-to-end — stays in **`spectracsPy`** (`test_pumpkin_workflow_end_to_end.py`):
  the engine is genuinely host (qGray / session / camera-acquisition), so that test cannot leave the app.
- **T2** — the plugin-boundary test — lives **here**. It synthesizes reference + sample spectra in `-core`, calls
  the plugin's three hooks directly, and asserts the verdict + `EvaluationResult` shape. It imports `plugin_sdk` +
  `-core` + `-model` only — **no app** — which is the point.

Run T2 (app-absent PYTHONPATH — note `spectracsPy` is not on it):

```bash
PYTHONPATH=.:../spectracsPy-core:../spectracsPy-model:../spectracsPy-base \
  ../spectracsPy/venv/bin/python -m pytest tests/test_pumpkin_plugin_boundary.py -q
```

## Dependencies

Namespace-merged via `PYTHONPATH` (PEP 420), not pip-installed; everything shares `spectracsPy/venv`. The runtime
need is `spectracsPy-core`'s dependency set (numpy · scipy · colour-science · rgbxy · spectres · matplotlib · Pillow
· pypdf) plus `spectracsPy-model` + `spectracsPy-base`. See [`requirements.txt`](requirements.txt).
