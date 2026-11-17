# LOOM module explorer

An interactive hierarchy view of the LOOM RTL — modules, submodules, their ports,
what each one does, and which section of the spec defines it. Two ways to use it:

**1. Open it in the repo (no build step, no dependencies)**

```bash
xdg-open viewer/index.html          # or just open the file in any browser
```

Everything is self-contained: `index.html` + `style.css` + `viewer.js` +
`loom.diagram.js`. No npm install, no bundler, works from `file://`.

**2. View it online**

<https://rajesh963.github.io/loom-protocol-emulator/modules/> — deployed by the
`gds` workflow together with the Tiny Tapeout 3D GDS viewer.

## What you can do

- click a block for details (instance name, what it does, spec section, badges)
- **double-click** a block with the `modules` badge (or click it in the sidebar)
  to open its internal diagram — e.g. `u_core → thread0 → event queue`
- breadcrumbs + `Esc` to go back; deep links work: `index.html#/u_core/thread0`
- drag & drop any SiliconPulse `diagram.json` to load a different hierarchy
- `3D tilt` gives a perspective view; `fit` re-fits the window

## Data / regeneration

`viewer/gen_diagram.py` generates both `loom.diagram.json` and its
`loom.diagram.js` wrapper (the JS copy is what makes `file://` work, because
browsers block `fetch()` on `file://`). The hierarchy, instance names and
parameters mirror the RTL; approximate cell counts come from
`tools/area_synth.sh`; `specRef` fields point into `docs/architecture.md`,
`docs/isa.md` and `flow/README.md`.

```bash
python3 viewer/gen_diagram.py
```

Edit `gen_diagram.py` (not the generated files) when the RTL hierarchy changes.

## SiliconPulse compatible — how to view LOOM in a siliconpulse-3d fork

`viewer/loom.diagram.json` uses the **SiliconPulse `diagram.v1`** schema and has
been validated against
[`public/examples/diagram.schema.json`](https://github.com/rajesh963/siliconpulse-3d/blob/main/public/examples/diagram.schema.json)
(every nested diagram, including `internal` modules, passes).

**Runtime import (no code changes):**

```bash
git clone https://github.com/<you>/siliconpulse-3d && cd siliconpulse-3d
npm install
npm run dev                       # http://localhost:3000
# grab the diagram (or download it from the repo's viewer/ folder)
curl -LO https://raw.githubusercontent.com/rajesh963/loom-protocol-emulator/master/viewer/loom.diagram.json
```

Then in the app: **Architecture → Load JSON / IP-XACT / CSV → choose
`loom.diagram.json` → Validate → Load diagram**. Click a block that shows
internals (e.g. `u_core`) and use **Open internal module** to walk
`u_core → thread0 → event queue`; use the app's breadcrumb to go back. The
upload is stored in `localStorage` for the current SoC preset (switching presets
clears it) and **Export JSON** downloads it again.

**Serve it as a static example in the fork (optional):**

```bash
cp loom.diagram.json siliconpulse-3d/public/examples/     # then fetch it from the UI
```

**Add it as a permanent preset (optional, requires a catalog entry):**
copy the JSON to `src/data/diagrams/loom.ts` as
`export const LOOM_DIAGRAM: ArchitectureDiagram = {...}`, register it in
`DIAGRAM_REGISTRY` in `src/data/diagrams/index.ts`, and point a SoC catalog
entry at it. The runtime import above is the documented "bring your own SoC"
path and needs no code.

**Zero-install alternative:** open our own `viewer/index.html` — same JSON, no
Node toolchain, works from `file://`.
