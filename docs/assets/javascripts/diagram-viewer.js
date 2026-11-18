(() => {
  function element(tag, className) {
    const node = document.createElement(tag);
    node.className = className;
    return node;
  }

  function button(label, action) {
    const control = document.createElement("button");
    control.type = "button";
    control.textContent = label;
    control.addEventListener("click", action);
    return control;
  }

  const minZoom = 50;
  const maxZoom = 400;
  const zoomStep = 25;
  let zoom = 100;
  let activeDiagram;

  const dialog = element("dialog", "astra-diagram-dialog md-typeset");
  dialog.setAttribute("aria-label", "Expanded diagram");
  const toolbar = element("div", "astra-diagram-toolbar");
  const viewport = element("div", "astra-diagram-viewport");
  viewport.tabIndex = 0;
  viewport.setAttribute("role", "region");
  viewport.setAttribute("aria-label", "Scrollable diagram");
  const stage = element("div", "astra-diagram-stage");
  viewport.append(stage);
  dialog.append(toolbar, viewport);
  document.body.append(dialog);

  function fitWidth() {
    if (dialog.open) stage.style.width = `${viewport.clientWidth}px`;
  }

  function setZoom(nextZoom) {
    const ratio = nextZoom / zoom;
    const centerX = viewport.clientWidth / 2;
    const centerY = viewport.clientHeight / 2;
    const left = (viewport.scrollLeft + centerX) * ratio - centerX;
    const top = (viewport.scrollTop + centerY) * ratio - centerY;
    zoom = nextZoom;
    stage.style.setProperty("--astra-diagram-zoom", zoom / 100);
    viewport.scrollTo(left, top);
    reset.textContent = `${zoom}%`;
    reset.setAttribute("aria-label", `Reset zoom, currently ${zoom}%`);
    zoomOut.disabled = zoom === minZoom;
    zoomIn.disabled = zoom === maxZoom;
  }

  const zoomOut = button("Zoom out", () => setZoom(zoom - zoomStep));
  const reset = button("100%", () => setZoom(100));
  reset.title = "Reset zoom";
  const zoomIn = button("Zoom in", () => setZoom(zoom + zoomStep));
  const close = button("Close", () => dialog.close());
  close.autofocus = true;
  toolbar.append(zoomOut, reset, zoomIn, close);

  new ResizeObserver(fitWidth).observe(viewport);

  dialog.addEventListener("close", () => {
    const { diagram, original, opener } = activeDiagram;
    original.append(diagram);
    activeDiagram = undefined;
    if (opener.isConnected) opener.focus({ preventScroll: true });
  });

  const enhanced = new WeakSet();

  function addControls(content) {
    // Material swaps each Mermaid fence for a rendered host with a closed shadow root.
    for (const diagram of content.querySelectorAll("div.mermaid")) {
      if (enhanced.has(diagram)) continue;
      enhanced.add(diagram);

      const original = element("div", "astra-diagram");
      const actions = element("div", "astra-diagram-toolbar");
      const opener = button("Expand diagram", () => {
        if (activeDiagram) return;
        activeDiagram = { diagram, original, opener };
        stage.append(diagram);
        dialog.showModal();
        fitWidth();
        setZoom(100);
        viewport.scrollTo(0, 0);
      });
      opener.setAttribute("aria-haspopup", "dialog");
      diagram.replaceWith(original);
      actions.append(opener);
      original.append(actions, diagram);
    }
  }

  let observer;
  // Material emits again after instant navigation replaces the page content.
  document$.subscribe(() => {
    observer?.disconnect();
    if (dialog.open) dialog.close();
    const content = document.querySelector(".md-content__inner");
    if (!content) return;
    addControls(content);
    observer = new MutationObserver(() => addControls(content));
    observer.observe(content, { childList: true, subtree: true });
  });
})();
