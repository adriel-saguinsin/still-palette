/* Still Palette */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const el = {
    landing: $("landing"),
    editor: $("editor"),
    dropzone: $("dropzone"),
    browse: $("browse"),
    fileInput: $("file-input"),
    landingError: $("landing-error"),
    stageError: $("stage-error"),
    overlay: $("drop-overlay"),
    print: $("print"),
    shimmer: $("shimmer"),
    qualityDot: $("quality-dot"),
    qualityText: $("quality-text"),
    printWrap: $("print-wrap"),
    overlay2: $("bar-overlay"),
    ovTools: $("ov-tools"),
    fileName: $("file-name"),
    fileDims: $("file-dims"),
    changeImage: $("change-image"),
    borderColor: $("border-color"),
    borderHex: $("border-hex"),
    borderWidth: $("border-width"),
    borderWidthOut: $("border-width-out"),
    colourCount: $("colour-count"),
    colourCountOut: $("colour-count-out"),
    reverse: $("reverse"),
    segCustom: $("seg-custom"),
    shuffle: $("shuffle"),
    resetPalette: $("reset-palette"),
    pickHint: $("pick-hint"),
    paletteNote: $("palette-note"),
    showHex: $("show-hex"),
    signature: $("signature"),
    signatureFields: $("signature-fields"),
    signatureText: $("signature-text"),
    signatureDate: $("signature-date"),
  };

  // `slots` is the canonical palette; the server returns the display ordering.
  const state = {
    token: null,
    slots: [],
    base: [],
    pool: [],
    ordered: [],
    order: "dominance",
    reverse: false,
    layout: null,
    renderId: null,
    armed: null, // slot index awaiting an eyedropper click
    rerollCursor: 0,
  };

  const DRAFT_DELAY = 120;
  const FINAL_DELAY = 420;

  // ---------------------------------------------------------------- helpers

  async function postJSON(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Something went wrong.");
    return data;
  }

  function showError(node, message) {
    node.textContent = message;
    node.hidden = false;
    clearTimeout(node._t);
    node._t = setTimeout(() => (node.hidden = true), 5000);
  }

  function readOptions(draft) {
    return {
      borderHex: el.borderHex.value,
      borderPct: parseFloat(el.borderWidth.value),
      showHex: el.showHex.checked,
      signature: el.signature.checked,
      signatureText: el.signature.checked ? el.signatureText.value : "",
      signatureDate: el.signature.checked ? formatDate(el.signatureDate.value) : "",
      draft: draft,
    };
  }

  function formatDate(iso) {
    if (!iso) return "";
    const [y, m, d] = iso.split("-").map(Number);
    if (!y || !m || !d) return iso;
    const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    return `${d} ${months[m - 1]} ${y}`;
  }

  // ------------------------------------------------------------- rendering

  let draftTimer = null;
  let finalTimer = null;
  let seq = 0;
  let applied = 0;

  function scheduleRender() {
    if (!state.token || !state.slots.length) return;
    clearTimeout(draftTimer);
    clearTimeout(finalTimer);
    setQuality(false);
    draftTimer = setTimeout(() => doRender(true), DRAFT_DELAY);
    finalTimer = setTimeout(() => doRender(false), FINAL_DELAY);
  }

  function setQuality(full) {
    el.qualityDot.classList.toggle("pending", !full);
    el.qualityText.textContent = full ? "Full quality" : "Refining…";
    el.shimmer.hidden = full;
    el.print.classList.toggle("loading", !full);
  }

  async function doRender(draft) {
    const mine = ++seq;
    try {
      const data = await postJSON("/api/render", {
        token: state.token,
        colors: state.slots,
        order: state.order,
        reverse: state.reverse,
        options: readOptions(draft),
      });
      // A slow draft must never land on top of an already-applied final.
      if (mine < applied) return;
      applied = mine;

      state.ordered = data.ordered;
      state.layout = data.layout;
      state.renderId = data.renderId;
      buildOverlay();

      el.print.onload = () => {
        // A new print can be a different shape, so the overlay has to be
        // re-measured before it means anything.
        syncOverlayBox();
        // The print now shows the new order, so the painted preview can go.
        releasePreview();
        if (!draft) setQuality(true);
      };
      el.print.src = data.url;
      if (draft) setQuality(false);
    } catch (err) {
      if (mine < applied) return;
      // Never leave the preview pinned over a print that will not arrive.
      releasePreview();
      showError(el.stageError, err.message);
      setQuality(true);
    }
  }

  // -------------------------------------------------------------- palette

  async function loadPalette(n) {
    const data = await postJSON("/api/extract", { token: state.token, n: n });
    state.base = data.base.map((s) => ({
      hex: s.hex,
      weight: s.weight,
      source: "extracted",
    }));
    state.pool = data.pool;
    state.slots = state.base.map((s) => ({ ...s }));
    state.rerollCursor = 0;
    setOrder("dominance", { silent: true });
    el.segCustom.disabled = true;

    if (data.short) {
      el.paletteNote.textContent = `only ${data.base.length} distinct`;
      el.paletteNote.hidden = false;
    } else {
      el.paletteNote.hidden = true;
    }
  }

  function availableAlternates() {
    const used = new Set(state.slots.map((s) => s.hex));
    return state.pool.filter((c) => !used.has(c.hex));
  }

  function reroll(index) {
    const candidates = availableAlternates();
    if (!candidates.length) {
      showError(el.stageError, "No other distinct colours left in this image.");
      return;
    }
    const pick = candidates[state.rerollCursor % candidates.length];
    state.rerollCursor += 1;
    // A re-rolled colour keeps its cluster weight, so it still sorts sensibly
    // under dominance ordering.
    state.slots[index] = { hex: pick.hex, weight: pick.weight, source: "rerolled" };
    scheduleRender();
  }

  function shuffleAll() {
    const wanted = state.slots.length;
    // Alternates first so a shuffle visibly moves, then the extracted colours,
    // deduped -- cycling a short candidate list would otherwise deal the same
    // colour into several slots at once.
    const seen = new Set();
    const supply = [];
    for (const c of state.pool.concat(state.base)) {
      if (!seen.has(c.hex)) {
        seen.add(c.hex);
        supply.push(c);
      }
    }
    if (supply.length < 2) {
      showError(el.stageError, "This image has no alternate colours to swap in.");
      return;
    }

    const next = [];
    const used = new Set();
    let cursor = state.rerollCursor;
    while (next.length < wanted && used.size < supply.length) {
      const pick = supply[cursor % supply.length];
      cursor += 1;
      if (used.has(pick.hex)) continue;
      used.add(pick.hex);
      next.push({ hex: pick.hex, weight: pick.weight, source: "rerolled" });
    }
    state.slots = next;
    state.rerollCursor = cursor;
    scheduleRender();
  }

  function resetPalette() {
    state.slots = state.base.map((s) => ({ ...s }));
    state.rerollCursor = 0;
    disarm();
    setOrder("dominance");
  }

  function setOrder(mode, opts) {
    state.order = mode;
    document.querySelectorAll(".seg").forEach((b) => {
      b.setAttribute("aria-pressed", String(b.dataset.order === mode));
    });
    if (mode !== "custom") el.segCustom.disabled = true;
    if (!(opts && opts.silent)) scheduleRender();
  }

  // ------------------------------------------- interactive swatches on print

  // The swatches are laid directly over the printed bar rather than mirrored in
  // the side panel, so reordering happens on the thing you are actually looking
  // at. Positions come from the render layout as percentages, which means they
  // stay aligned however the browser scales the image down.

  const ICON_REROLL =
    '<svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/></svg>';
  const ICON_PICK =
    '<svg viewBox="0 0 24 24"><path d="M15 4l5 5"/><path d="M17.5 6.5L9 15l-3 1 1-3 8.5-8.5a2.1 2.1 0 0 1 3 3z"/></svg>';

  let hovered = null; // display index the toolbar is anchored to

  // While a drag is live the overlay stops being a set of invisible hit-regions
  // and becomes the preview itself: the swatches are painted with their colours
  // and the printed bar behind them is covered. The printed bar cannot move, so
  // without this only a bare outline slides around while the real colours stay
  // put -- which reads as a misaligned box rather than a rearrangement.
  let previewHeld = false;

  function paintSwatches(on) {
    swatchNodes().forEach((n) => {
      n.style.background = on ? n.dataset.hex : "";
    });
  }

  function releasePreview() {
    if (!previewHeld) return;
    previewHeld = false;
    el.overlay2.classList.remove("holding");
    paintSwatches(false);
  }

  // Pin the overlay to the print's real box. Everything inside is positioned in
  // percentages, so if this box is even slightly wider than the image the error
  // grows across the bar and the last swatch ends up off the print entirely.
  function syncOverlayBox() {
    if (!el.print.naturalWidth) return;
    const wrap = el.printWrap.getBoundingClientRect();
    const img = el.print.getBoundingClientRect();
    el.overlay2.style.left = img.left - wrap.left + "px";
    el.overlay2.style.top = img.top - wrap.top + "px";
    el.overlay2.style.width = img.width + "px";
    el.overlay2.style.height = img.height + "px";
  }

  function buildOverlay() {
    el.overlay2.innerHTML = "";
    const layout = state.layout;
    if (!layout || !layout.swatches || !layout.swatches.length) return;
    syncOverlayBox();
    const { width: W, height: H } = layout;
    const pct = (v, total) => (v / total) * 100 + "%";

    // Covers the printed bar, gutters included, in the frame colour.
    const first = layout.swatches[0];
    const last = layout.swatches[layout.swatches.length - 1];
    const mask = document.createElement("div");
    mask.className = "bar-mask";
    mask.style.left = pct(first.x, W);
    mask.style.top = pct(layout.barY, H);
    mask.style.width = pct(last.x + last.w - first.x, W);
    mask.style.height = pct(layout.barH, H);
    mask.style.background = el.borderHex.value;
    el.overlay2.appendChild(mask);

    layout.swatches.forEach((rect, i) => {
      const node = document.createElement("div");
      node.className = "ov-swatch";
      node.dataset.index = String(i);
      const hex = (state.ordered[i] || {}).hex || "";
      node.dataset.hex = hex;
      node.title = hex;
      node.style.left = pct(rect.x, W);
      node.style.top = pct(rect.y, H);
      node.style.width = pct(rect.w, W);
      node.style.height = pct(rect.h, H);
      if (state.armedDisplay === i) node.classList.add("armed");
      el.overlay2.appendChild(node);
    });

    // Keep the painted preview up until the re-rendered print arrives, or the
    // bar would flick back to the old order for a frame after the drop.
    if (previewHeld) paintSwatches(true);
    if (hovered !== null && hovered >= layout.swatches.length) hideTools();
  }

  function swatchNodes() {
    return [...el.overlay2.querySelectorAll(".ov-swatch")];
  }

  // --- floating toolbar -----------------------------------------------------

  function showTools(index) {
    const node = swatchNodes()[index];
    if (!node || drag) return;
    hovered = index;
    const wrap = el.printWrap.getBoundingClientRect();
    const r = node.getBoundingClientRect();
    el.ovTools.hidden = false;
    el.ovTools.style.left = r.left - wrap.left + r.width / 2 + "px";
    el.ovTools.style.top = r.top - wrap.top - 6 + "px";
  }

  function hideTools() {
    hovered = null;
    el.ovTools.hidden = true;
  }

  // Every overlay binding lives in here so that a failure to wire the palette
  // editor cannot stop the upload listeners further down from registering. A
  // stale server serving cached HTML against newer JavaScript used to throw
  // here, which killed the whole script -- leaving no drop handler, so dropped
  // files simply opened in a new tab, and no click handler to open the picker.


  // Leaving for the toolbar itself must not dismiss it, or the buttons would be
  // unreachable -- the toolbar sits flush above the swatch so the pointer can
  // travel there without crossing dead space.


  // Maps a position in the displayed order back to its canonical slot.
  function canonicalIndex(displayIndex) {
    const target = state.ordered[displayIndex];
    if (!target) return -1;
    const seen = state.ordered
      .slice(0, displayIndex)
      .filter((s) => s.hex === target.hex).length;
    let hits = 0;
    for (let i = 0; i < state.slots.length; i++) {
      if (state.slots[i].hex === target.hex) {
        if (hits === seen) return i;
        hits += 1;
      }
    }
    return -1;
  }

  // --- drag to reorder, directly on the print -------------------------------

  let drag = null;



  function targetFor(x) {
    // Nearest centre, rather than "which swatch is the pointer inside". The
    // gutters between swatches belong to no swatch, so a hit test loses the
    // target every time the pointer crosses one and the preview snaps back.
    const rects = drag.rects;
    let best = 0;
    let bestDist = Infinity;
    for (let i = 0; i < rects.length; i++) {
      const dist = Math.abs(x - (rects[i].left + rects[i].width / 2));
      if (dist < bestDist) {
        bestDist = dist;
        best = i;
      }
    }
    return best;
  }

  function applyPreview(dx) {
    const { rects, from, target } = drag;
    const gutter = gutterPx();

    // Resting position of every swatch in the previewed order, accumulated
    // from real widths. Shifting each displaced swatch by one swatch width is
    // close but drifts, because widths differ by a pixel or two where the bar
    // absorbs its rounding remainder.
    const order = rects.map((_, i) => i);
    order.splice(target, 0, order.splice(from, 1)[0]);
    const left = new Array(rects.length);
    let x = rects[0].left;
    for (const idx of order) {
      left[idx] = x;
      x += rects[idx].width + gutter;
    }

    swatchNodes().forEach((n, i) => {
      if (i === from) return;
      n.style.transform = `translateX(${left[i] - rects[i].left}px)`;
    });
    // The dragged one tracks the pointer instead of snapping to its slot.
    drag.node.style.transform = `translateX(${dx}px)`;
  }

  function gutterPx() {
    // Gutter between swatches in *displayed* pixels.
    const rects = drag ? drag.rects : [];
    if (rects.length < 2) return 0;
    return Math.max(0, rects[1].left - rects[0].right);
  }

  function endDrag() {
    if (!drag) return;
    const { from, target, active, node } = drag;
    drag = null;

    node.classList.remove("dragging");
    el.overlay2.classList.remove("dragging");
    swatchNodes().forEach((n) => {
      n.classList.remove("sliding");
      n.style.transform = "";
    });
    if (!active || target === from) {
      paintSwatches(false);
      return;
    }
    // Hold the painted preview until the new print loads, so the bar does not
    // flick back to the old order in the gap before the render returns.
    previewHeld = true;
    el.overlay2.classList.add("holding");

    // What you dragged is what you get: the arrangement on screen becomes the
    // canonical order. Reverse is cleared so the result is not silently flipped.
    const next = state.ordered.map((s) => ({
      hex: s.hex,
      weight: s.weight,
      source: s.source,
    }));
    next.splice(target, 0, next.splice(from, 1)[0]);
    state.slots = next;
    // Paint the new order straight away rather than waiting for the render.
    state.ordered = next;
    buildOverlay();
    state.reverse = false;
    el.reverse.checked = false;
    el.segCustom.disabled = false;
    setOrder("custom");
  }


  // Bindings are isolated here so a page whose HTML predates this script
  // cannot throw partway through and leave the rest of the app unwired.
  function wireOverlay() {
    el.ovTools.querySelector('[data-act="reroll"]').innerHTML = ICON_REROLL;
    el.ovTools.querySelector('[data-act="pick"]').innerHTML = ICON_PICK;

    // The print resizes with the window, and the panel and hint text below it
    // can reflow independently, so re-measure rather than assume.
    window.addEventListener("resize", () => {
      syncOverlayBox();
      hideTools();
    });
    if (window.ResizeObserver) {
      new ResizeObserver(syncOverlayBox).observe(el.print);
    }

    el.overlay2.addEventListener("pointerover", (e) => {
      const node = e.target.closest(".ov-swatch");
      if (!node) return;
      // Re-measure before anything becomes visible. The overlay is transparent
      // until you touch it, so a layout change that slipped past the resize
      // listener can only ever show up from here on -- measuring now makes
      // alignment independent of whether those events actually arrived.
      if (!drag) syncOverlayBox();
      showTools(Number(node.dataset.index));
    });

    el.printWrap.addEventListener("pointerleave", hideTools);

    el.ovTools.addEventListener("click", (e) => {
      const tool = e.target.closest(".tool");
      if (!tool || hovered === null) return;
      const display = hovered;
      const index = canonicalIndex(display);
      if (index < 0) return;
      if (tool.dataset.act === "reroll") {
        disarm();
        reroll(index);
      } else {
        arm(display, index);
      }
    });

    el.overlay2.addEventListener("pointerdown", (e) => {
      const node = e.target.closest(".ov-swatch");
      if (!node || state.armed !== null) return;
      e.preventDefault();
      // Measure before capturing rects, so a drag can never start from a stale
      // overlay box and carry the error through every offset it computes.
      syncOverlayBox();
      drag = {
        from: Number(node.dataset.index),
        target: Number(node.dataset.index),
        node: node,
        rects: swatchNodes().map((n) => n.getBoundingClientRect()),
        startX: e.clientX,
        active: false,
        pointerId: e.pointerId,
      };
      node.setPointerCapture(e.pointerId);
    });

    el.overlay2.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const dx = e.clientX - drag.startX;
      if (!drag.active) {
        if (Math.abs(dx) < 4) return; // a click, not a drag
        drag.active = true;
        hideTools();
        el.overlay2.classList.add("dragging");
        paintSwatches(true);
        swatchNodes().forEach((n) => n.classList.add("sliding"));
        drag.node.classList.add("dragging");
        drag.node.classList.remove("sliding");
      }
      drag.target = targetFor(e.clientX);
      applyPreview(dx);
    });

    el.overlay2.addEventListener("pointerup", endDrag);
    el.overlay2.addEventListener("pointercancel", endDrag);
  }

  // ----------------------------------------------------------- eyedropper

  function arm(display, index) {
    state.armed = index;
    state.armedDisplay = display;
    el.pickHint.hidden = false;
    el.print.classList.add("picking");
    hideTools();
    swatchNodes().forEach((n, i) => n.classList.toggle("armed", i === display));
    // The overlay would otherwise swallow the click meant for the photo.
    el.overlay2.style.pointerEvents = "none";
  }

  function disarm() {
    state.armed = null;
    state.armedDisplay = null;
    el.pickHint.hidden = true;
    el.print.classList.remove("picking");
    el.overlay2.style.pointerEvents = "";
    swatchNodes().forEach((n) => n.classList.remove("armed"));
  }

  el.print.addEventListener("click", async (e) => {
    if (state.armed === null || !state.renderId) return;
    const rect = el.print.getBoundingClientRect();
    const x = ((e.clientX - rect.left) / rect.width) * el.print.naturalWidth;
    const y = ((e.clientY - rect.top) / rect.height) * el.print.naturalHeight;
    const index = state.armed;
    try {
      const data = await postJSON("/api/sample", {
        token: state.token,
        renderId: state.renderId,
        x: x,
        y: y,
      });
      // Two identical swatches render as one indistinguishable block, so a
      // duplicate is refused. The slot stays armed for another try.
      const clash = state.slots.some((s, i) => i !== index && s.hex === data.hex);
      if (clash) {
        showError(el.stageError, `${data.hex} is already on the bar — try another spot.`);
        return;
      }
      // A picked colour has no cluster share; weight stays null so dominance
      // ordering places it last rather than inventing a share for it.
      state.slots[index] = { hex: data.hex, weight: null, source: "picked" };
      disarm();
      scheduleRender();
    } catch (err) {
      showError(el.stageError, err.message);
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.armed !== null) disarm();
  });

  // --------------------------------------------------------------- upload

  async function handleFile(file) {
    if (!file) return;
    const form = new FormData();
    form.append("image", file);
    const target = el.editor.hidden ? el.landingError : el.stageError;
    try {
      const res = await fetch("/api/upload", { method: "POST", body: form });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || "Upload failed.");

      state.token = data.token;
      el.fileName.textContent = data.name;
      el.fileDims.textContent = `${data.width} × ${data.height}`;
      el.landing.hidden = true;
      el.editor.hidden = false;
      disarm();
      await loadPalette(Number(el.colourCount.value));
      scheduleRender();
    } catch (err) {
      showError(target, err.message);
    }
  }

  el.browse.addEventListener("click", (e) => {
    e.stopPropagation();
    el.fileInput.click();
  });
  el.dropzone.addEventListener("click", () => el.fileInput.click());
  el.dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      el.fileInput.click();
    }
  });
  el.changeImage.addEventListener("click", () => el.fileInput.click());
  el.fileInput.addEventListener("change", (e) => {
    handleFile(e.target.files[0]);
    e.target.value = "";
  });

  // Dragging the print must never re-enter the app as a new upload: a browser
  // hands a dragged <img> to the drop target as a file, so a "is it a file?"
  // check cannot tell it apart from a real one.
  //
  // The fix is to stop the drag ever starting -- draggable="false" on the
  // element plus this cancellation -- rather than to filter it out at drop
  // time. An earlier version tracked a "drag came from inside" flag, but
  // cancelling a dragstart means no dragend follows to lower it again, so the
  // flag stuck on and silently killed every later file drop.
  document.addEventListener("dragstart", (e) => {
    if (el.printWrap.contains(e.target)) e.preventDefault();
  });

  const isFileDrag = (e) =>
    e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");

  let dragDepth = 0;

  function clearDropUI() {
    dragDepth = 0;
    el.overlay.hidden = true;
    el.dropzone.classList.remove("over");
  }

  window.addEventListener("dragenter", (e) => {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    dragDepth += 1;
    if (!el.editor.hidden) el.overlay.hidden = false;
    else el.dropzone.classList.add("over");
  });

  // preventDefault here is what stops the browser navigating away to the
  // dropped file. It has to happen for *every* file drag, including ones we
  // then decline to handle -- otherwise declining one silently replaces the
  // whole app with the raw image.
  window.addEventListener("dragover", (e) => {
    if (isFileDrag(e)) e.preventDefault();
  });

  window.addEventListener("dragleave", (e) => {
    if (!isFileDrag(e)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) clearDropUI();
  });

  window.addEventListener("drop", (e) => {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    clearDropUI();
    handleFile(e.dataTransfer.files[0]);
  });

  window.addEventListener("paste", (e) => {
    const item = Array.from(e.clipboardData?.items || []).find((i) =>
      i.type.startsWith("image/")
    );
    if (item) handleFile(item.getAsFile());
  });

  // -------------------------------------------------------------- controls

  function syncBorder(hex, from) {
    const value = hex.toUpperCase();
    if (!/^#[0-9A-F]{6}$/.test(value)) {
      el.borderHex.classList.add("invalid");
      return;
    }
    el.borderHex.classList.remove("invalid");
    if (from !== "hex") el.borderHex.value = value;
    if (from !== "picker") el.borderColor.value = value;
    document.querySelectorAll(".preset").forEach((p) => {
      p.setAttribute("aria-pressed", String(p.dataset.hex === value));
    });
    scheduleRender();
  }

  el.borderColor.addEventListener("input", () =>
    syncBorder(el.borderColor.value, "picker")
  );
  el.borderHex.addEventListener("input", () => {
    let v = el.borderHex.value.trim();
    if (v && !v.startsWith("#")) v = "#" + v;
    syncBorder(v, "hex");
  });
  document.querySelectorAll(".preset").forEach((p) => {
    p.addEventListener("click", () => syncBorder(p.dataset.hex, "preset"));
  });

  el.borderWidth.addEventListener("input", () => {
    el.borderWidthOut.textContent = `${el.borderWidth.value}%`;
    scheduleRender();
  });

  el.colourCount.addEventListener("input", () => {
    el.colourCountOut.textContent = el.colourCount.value;
  });
  el.colourCount.addEventListener("change", async () => {
    if (!state.token) return;
    // A different cluster count invalidates every edit, so the palette resets
    // rather than carrying over colours that no longer belong to it.
    disarm();
    try {
      await loadPalette(Number(el.colourCount.value));
      scheduleRender();
    } catch (err) {
      showError(el.stageError, err.message);
    }
  });

  document.querySelectorAll(".seg").forEach((b) => {
    b.addEventListener("click", () => {
      if (b.disabled) return;
      setOrder(b.dataset.order);
    });
  });

  el.reverse.addEventListener("change", () => {
    state.reverse = el.reverse.checked;
    scheduleRender();
  });

  el.shuffle.addEventListener("click", () => {
    disarm();
    shuffleAll();
  });
  el.resetPalette.addEventListener("click", resetPalette);

  el.showHex.addEventListener("change", scheduleRender);
  el.signature.addEventListener("change", () => {
    el.signatureFields.hidden = !el.signature.checked;
    scheduleRender();
  });
  el.signatureText.addEventListener("input", scheduleRender);
  el.signatureDate.addEventListener("change", scheduleRender);

  // ----------------------------------------------------------------- init

  el.signatureDate.value = document.body.dataset.today || "";
  syncBorder("#FFFFFF", "init");
  setOrder("dominance", { silent: true });

  // Wired last, and defensively: uploading must work even if the palette
  // editor cannot be set up.
  const missing = Object.keys(el).filter((k) => !el[k]);
  if (missing.length) {
    // In practice this means the served HTML is older than this script -- an
    // earlier instance still holding the port and serving cached templates.
    console.error(
      "Still Palette: the page is missing " + missing.join(", ") +
      ". The HTML looks older than this script; restart the server."
    );
    const warn = document.createElement("p");
    warn.className = "error";
    warn.style.margin = "12px";
    warn.textContent =
      "This page is out of date with the app. Stop any other running copy of " +
      "Still Palette, restart it, then reload.";
    document.body.prepend(warn);
  } else {
    wireOverlay();
  }
})();
