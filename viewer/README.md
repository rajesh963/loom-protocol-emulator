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

## SiliconPulse compatible

The JSON uses the **SiliconPulse `diagram.v1`** schema (the same one used by
[siliconpulse-3d](https://github.com/rajesh963/siliconpulse-3d)), so
`viewer/loom.diagram.json` can also be loaded into that application via
**Architecture → Load JSON** for the full spec/IP-XACT workspace experience.
