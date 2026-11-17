/* LOOM module explorer — vanilla JS, no build step.
 *
 * Data model: SiliconPulse "diagram.v1" (see viewer/gen_diagram.py):
 *   { title, subtitle, source, width, height, groups[], blocks[], wires[], notes[] }
 *   block.internal -> another diagram (recursive).
 *
 * Endpoints: window.LOOM_DIAGRAM (committed loom.diagram.js) or drag & drop a JSON.
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const wrap = $("stage-wrap"), stage = $("stage"), wiresEl = $("wires");
  const SVGNS = "http://www.w3.org/2000/svg";

  const state = {
    root: window.LOOM_DIAGRAM || null,
    stack: [],          // [{diagram, blockId}]
    selected: null,
    tilt: false,
    scale: 1,
  };

  // ---------------------------------------------------------------- helpers
  const cur = () => (state.stack.length ? state.stack[state.stack.length - 1].diagram : state.root);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

  function internalsOf(d) {
    return (d.blocks || []).filter((b) => b.internal);
  }

  // ---------------------------------------------------------------- render
  function render() {
    const d = cur();
    if (!d) {
      $("details").innerHTML = '<div class="empty">No diagram loaded. Drag a SiliconPulse <code>diagram.json</code> here.</div>';
      return;
    }
    $("title").textContent = d.title || "LOOM — module explorer";
    $("subtitle").textContent = d.subtitle || "";
    renderCrumbs();
    $("legend").innerHTML = d.source ? `<span>source: <b style="color:#cfe3f5">${esc(d.source)}</b></span>` : "";
    renderTree();
    renderStage(d);
    fitStage(d);
    $("details").innerHTML = '<div class="empty">Select a block for details' +
      (state.stack.length ? "" : " · drag &amp; drop a diagram.json to load another hierarchy") +
      "</div>";
    state.selected = null;
    syncHash();
  }

  function renderCrumbs() {
    const c = $("crumbs");
    c.innerHTML = "";
    const mk = (label, idx) => {
      const b = document.createElement("button");
      b.textContent = label;
      b.onclick = () => { state.stack.length = idx; render(); };
      return b;
    };
    c.appendChild(mk("top", 0));
    state.stack.forEach((fr, i) => {
      const sep = document.createElement("span"); sep.textContent = "/"; c.appendChild(sep);
      c.appendChild(mk(fr.label || fr.blockId, i + 1));
    });
  }

  function renderTree() {
    const root = state.root, tree = $("tree");
    tree.innerHTML = "";
    const addDiagram = (ul, d, depth) => {
      (d.blocks || []).forEach((b) => {
        const li = document.createElement("li");
        const btn = document.createElement("button");
        btn.className = "node" + (state.selected === b.id ? " active" : "");
        btn.style.paddingLeft = (8 + depth * 2) + "px";
        const dot = document.createElement("span");
        dot.className = "dot"; dot.style.background = b.color || "#64748b";
        btn.appendChild(dot);
        const lbl = document.createElement("span");
        lbl.textContent = b.label;
        btn.appendChild(lbl);
        if (b.internal) {
          const tag = document.createElement("span");
          tag.className = "tag"; tag.textContent = "modules";
          btn.appendChild(tag);
        }
        btn.onclick = () => {
          select(b, d);
          if (b.internal) openInternal(b, d);
        };
        li.appendChild(btn);
        if (b.internal) {
          const sub = document.createElement("ul");
          sub.className = "children";
          addDiagram(sub, b.internal, depth + 1);
          li.appendChild(sub);
        }
        ul.appendChild(li);
      });
    };
    addDiagram(tree, root, 0);
  }

  function renderStage(d) {
    stage.style.width = (d.width || 1200) + "px";
    stage.style.height = (d.height || 800) + "px";
    // clear everything except the wire layer
    [...stage.children].forEach((el) => { if (el !== wiresEl) el.remove(); });
    wiresEl.setAttribute("viewBox", `0 0 ${d.width || 1200} ${d.height || 800}`);
    wiresEl.innerHTML = "";

    (d.groups || []).forEach((g) => {
      const el = document.createElement("div");
      el.className = "group";
      el.style.cssText = `left:${g.x}px;top:${g.y}px;width:${g.w}px;height:${g.h}px;` +
        `border-color:${hexA(g.color, .55)};`;
      const lab = document.createElement("div");
      lab.className = "g-label"; lab.textContent = g.label;
      el.appendChild(lab);
      stage.appendChild(el);
    });

    const byId = {};
    (d.blocks || []).forEach((b) => {
      byId[b.id] = b;
      const el = document.createElement("div");
      el.className = "block";
      el.dataset.id = b.id;
      el.style.cssText = `left:${b.x}px;top:${b.y}px;width:${b.w}px;height:${b.h}px;` +
        `border-color:${hexA(b.color, .5)};`;
      const bar = document.createElement("div");
      bar.className = "b-bar"; bar.style.background = b.color || "#64748b";
      el.appendChild(bar);
      const lab = document.createElement("div");
      lab.className = "b-label"; lab.textContent = b.label;
      el.appendChild(lab);
      if (b.sublabel) {
        const sub = document.createElement("div");
        sub.className = "b-sub"; sub.textContent = b.sublabel;
        el.appendChild(sub);
      }
      if (b.badges && b.badges.length) {
        const bs = document.createElement("div");
        bs.className = "b-badges";
        b.badges.slice(0, 4).forEach((t) => {
          const s = document.createElement("span");
          s.className = "badge"; s.textContent = t;
          bs.appendChild(s);
        });
        el.appendChild(bs);
      }
      if (b.internal) {
        const n = document.createElement("div");
        n.className = "b-nested"; n.textContent = "modules";
        el.appendChild(n);
      }
      el.addEventListener("click", (ev) => { ev.stopPropagation(); select(b, d); });
      el.addEventListener("dblclick", (ev) => {
        ev.stopPropagation();
        if (b.internal) openInternal(b, d);
      });
      stage.appendChild(el);
    });

    (d.wires || []).forEach((w, i) => {
      const a = byId[w.from], b = byId[w.to];
      if (!a || !b) return;
      drawWire(w, a, b, i);
    });
  }

  function hexA(hex, a) {
    if (!hex || hex[0] !== "#" || hex.length < 7) return `rgba(120,140,170,${a})`;
    const r = parseInt(hex.slice(1, 3), 16), g = parseInt(hex.slice(3, 5), 16), bl = parseInt(hex.slice(5, 7), 16);
    return `rgba(${r},${g},${bl},${a})`;
  }

  function drawWire(w, a, b, i) {
    const ax = a.x + a.w / 2, ay = a.y + a.h / 2;
    const bx = b.x + b.w / 2, by = b.y + b.h / 2;
    const dx = bx - ax, dy = by - ay;
    const cx = ax + dx * 0.5, cy = ay + dy * 0.5 - Math.min(70, Math.abs(dx) * 0.12 + Math.abs(dy) * 0.12);
    const path = document.createElementNS(SVGNS, "path");
    path.setAttribute("d", `M ${ax} ${ay} Q ${cx} ${cy} ${bx} ${by}`);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", hexA(a.color || "#22d3ee", 0.45));
    path.setAttribute("stroke-width", "1.4");
    path.setAttribute("stroke-dasharray", "5 5");
    path.setAttribute("data-i", i);
    const title = document.createElementNS(SVGNS, "title");
    title.textContent = `${w.busType || "wire"} · ${w.width || "?"} bit · ${w.clock || "clk"}`;
    path.appendChild(title);
    wiresEl.appendChild(path);
  }

  // ---------------------------------------------------------------- actions
  function select(b, d) {
    state.selected = b.id;
    document.querySelectorAll(".block").forEach((el) =>
      el.classList.toggle("selected", el.dataset.id === b.id));
    document.querySelectorAll(".tree .node").forEach((el) => el.classList.remove("active"));
    const det = $("details");
    det.innerHTML =
      `<h3>${esc(b.label)}${b.sublabel ? ` <span style="color:#8b9bb4;font-weight:400;font-size:12px">— ${esc(b.sublabel)}</span>` : ""}</h3>` +
      `<div class="meta">` +
      (b.kind ? `<span>kind: <b>${esc(b.kind)}</b></span>` : "") +
      (b.specRef ? `<span>spec: <b>${esc(b.specRef)}</b></span>` : "") +
      (b.internal ? `<span><b>has internal diagram</b> (double-click / “modules”)</span>` : "") +
      (b.badges || []).map((t) => `<span><b>${esc(t)}</b></span>`).join("") +
      `</div>` +
      (b.details ? `<p>${esc(b.details)}</p>` : "") +
      ((d.notes || []).length ? `<p style="color:#8b9bb4;font-size:12px">${esc(d.notes[0])}</p>` : "");
  }

  function openInternal(b) {
    state.stack.push({ diagram: b.internal, blockId: b.id, label: b.label });
    render();
  }

  function back() {
    if (state.stack.length) { state.stack.pop(); render(); }
  }

  // -- deep links: index.html#/u_core/thread0 --------------------------------
  function hashIds() {
    return (location.hash || "").replace(/^#\/?/, "").split("/").filter(Boolean);
  }

  function openPath(ids) {
    state.stack.length = 0;
    let diagram = state.root;
    for (const id of ids) {
      const b = (diagram.blocks || []).find((x) => x.id === id);
      if (!b || !b.internal) break;
      state.stack.push({ diagram: b.internal, blockId: b.id, label: b.label });
      diagram = b.internal;
    }
    render();
  }

  function syncHash() {
    const want = "#/" + state.stack.map((f) => f.blockId).join("/");
    if (location.hash !== want) {
      try { history.replaceState(null, "", want); } catch (e) { location.hash = want; }
    }
  }

  function fitStage(d) {
    const w = d.width || 1200, h = d.height || 800;
    // clientWidth/Height can be 0 before first layout: fall back to the known
    // chrome sizes (sidebar 320 px, header ~56 px, details panel ~150 px).
    const wrapW = wrap.clientWidth || Math.max(320, window.innerWidth - 320);
    const wrapH = wrap.clientHeight || Math.max(240, window.innerHeight - 210);
    state.scale = Math.min(Math.max(200, wrapW - 24) / w,
                           Math.max(200, wrapH - 24) / h, 1.15);
    applyTransform(w, h, wrapW, wrapH);
    requestAnimationFrame(() => applyTransform(w, h)); // settle once layout is known
  }

  function applyTransform(w, h, wrapW, wrapH) {
    const d = cur() || {};
    w = w || d.width || 1200; h = h || d.height || 800;
    const s = state.scale;
    const cw = wrapW || wrap.clientWidth || Math.max(320, window.innerWidth - 320);
    const ch = wrapH || wrap.clientHeight || Math.max(240, window.innerHeight - 210);
    // scale about the stage centre, then translate the centre onto the wrapper centre
    const dx = (cw - w) / 2;
    const dy = (ch - h) / 2;
    stage.style.transformOrigin = "50% 50%";
    stage.style.transform = state.tilt
      ? `translate(${dx}px, ${dy}px) scale(${s}) perspective(1800px) rotateX(38deg)`
      : `translate(${dx}px, ${dy}px) scale(${s})`;
  }

  // ---------------------------------------------------------------- wire-up
  $("btn-back").onclick = back;
  $("btn-root").onclick = () => { state.stack.length = 0; render(); };
  $("btn-tilt").onclick = (e) => {
    state.tilt = !state.tilt;
    document.body.classList.toggle("flat", !state.tilt);
    e.currentTarget.setAttribute("aria-pressed", String(state.tilt));
    applyTransform();
  };
  $("btn-flat").onclick = () => { const d = cur(); if (d) fitStage(d); };
  window.addEventListener("resize", () => { const d = cur(); if (d) fitStage(d); });
  if (window.ResizeObserver) new ResizeObserver(() => { const d = cur(); if (d) fitStage(d); }).observe(wrap);
  window.addEventListener("keydown", (e) => { if (e.key === "Escape" || e.key === "Backspace") back(); });
  window.addEventListener("hashchange", () => {
    if (!state.root) return;
    const ids = hashIds();
    const curIds = state.stack.map((f) => f.blockId);
    if (ids.join("/") !== curIds.join("/")) openPath(ids);
  });

  // drag & drop another SiliconPulse diagram
  document.addEventListener("dragover", (e) => e.preventDefault());
  document.addEventListener("drop", async (e) => {
    e.preventDefault();
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (!f) return;
    try {
      const d = JSON.parse(await f.text());
      if (!d.blocks) throw new Error("no blocks[] in JSON");
      state.root = d; state.stack.length = 0; render();
    } catch (err) {
      $("details").innerHTML = `<div class="empty">Could not load ${esc(f.name)}: ${esc(err.message)}</div>`;
    }
  });

  // boot: use the committed JS object; try JSON as a fallback (http server case)
  if (!state.root) {
    fetch("loom.diagram.json").then((r) => r.json()).then((d) => {
      state.root = d; render();
    }).catch(() => render());
  } else {
    document.body.classList.remove("flat");
    const bootIds = hashIds();      // captured before render() rewrites the hash
    render();
    if (bootIds.length) openPath(bootIds);
  }
})();
