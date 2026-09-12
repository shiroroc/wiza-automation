"""Browser-side probe: read the Wiza panel wherever it is hiding.

The extension can render into (a) a chrome-extension:// iframe, (b) a shadow
root attached somewhere in the LinkedIn DOM, or (c) plain injected nodes. These
JS snippets handle all three by walking childNodes AND shadowRoots, so we never
depend on a brittle single CSS path.

Clickable elements are handed back with an opaque token; JS_CLICK turns a token
back into the live element. The registry resets on every probe, so always probe
immediately before you click.
"""

# Returns: {url, roots: [{text, mailtos, tels, buttons: [{token,text,disabled,visible}]}]}
JS_PROBE = r"""
(cfg) => {
  const reg = (window.__wizaAuto = { n: 0, map: {} });
  const token = (el) => { const t = "t" + (++reg.n); reg.map[t] = el; return t; };

  const collect = (root) => {
    const parts = [], mailtos = [], tels = [], buttons = [];
    const seen = new Set();
    // A CLOSED Chrome side panel keeps its document alive as a target but has
    // no layout at all - every rect comes back 0x0. Without noticing that, we
    // happily read stale text from a panel nobody can see, and never manage to
    // click anything. So track whether ANYTHING here has real size.
    let laidOut = false;

    const visit = (node) => {
      if (!node || seen.has(node)) return;
      seen.add(node);
      const nt = node.nodeType;

      if (nt === 3) {                       // text node
        const t = (node.textContent || "").trim();
        if (t) parts.push(t);
        return;
      }
      if (nt !== 1 && nt !== 9 && nt !== 11) return;   // element, document, fragment

      if (nt === 1) {
        const tag = node.tagName.toLowerCase();
        if (tag === "script" || tag === "style" || tag === "noscript") return;

        const href = node.getAttribute("href") || "";
        const lower = href.toLowerCase();
        if (lower.startsWith("mailto:")) {
          try { mailtos.push(decodeURIComponent(href.slice(7).split("?")[0])); } catch (e) {}
        }
        if (lower.startsWith("tel:")) {
          try { tels.push(decodeURIComponent(href.slice(4))); } catch (e) {}
        }

        // Data often lives in attributes (copy-to-clipboard buttons, inputs).
        for (const a of ["value", "placeholder", "aria-label", "title",
                         "data-email", "data-phone", "data-clipboard-text"]) {
          const v = node.getAttribute ? node.getAttribute(a) : null;
          if (v) parts.push(v);
        }
        if (tag === "input" || tag === "textarea") { if (node.value) parts.push(node.value); }

        const role = (node.getAttribute("role") || "").toLowerCase();
        let clickable = (tag === "button" || tag === "a" || role === "button" ||
                         node.onclick != null ||
                         node.getAttribute("tabindex") !== null);

        // React attaches its handlers at the root, so node.onclick is null on
        // everything it renders - a <span>Reload</span> looks inert here even
        // though it is the button. cursor:pointer is the reliable tell.
        // Restricted to small text nodes so this stays cheap.
        if (!clickable && (tag === "span" || tag === "div" || tag === "li" ||
                           tag === "p" || tag === "label")) {
          const own = (node.textContent || "").trim();
          if (own && own.length <= 60 && node.children.length <= 2) {
            try {
              if (getComputedStyle(node).cursor === "pointer") clickable = true;
            } catch (e) {}
          }
        }

        // Only LEAF elements that actually carry text count as proof of
        // layout. A closed side panel can still report a sized <body>, so
        // testing containers would call a shut panel "open".
        if (!laidOut && node.children.length === 0 &&
            (node.textContent || "").trim()) {
          try {
            const r0 = node.getBoundingClientRect();
            if (r0.width > 0 && r0.height > 0) laidOut = true;
          } catch (e) {}
        }

        if (clickable) {
          let rect = { width: 0, height: 0 };
          try { rect = node.getBoundingClientRect(); } catch (e) {}
          const label = ((node.innerText || node.textContent || "") + " " +
                         (node.getAttribute("aria-label") || ""))
                        .replace(/\s+/g, " ").trim().slice(0, 140);
          buttons.push({
            token: token(node),
            tag: tag,
            text: label,
            disabled: !!node.disabled || node.getAttribute("aria-disabled") === "true",
            visible: rect.width > 0 && rect.height > 0
          });
        }

        if (node.shadowRoot) visit(node.shadowRoot);
      }

      for (const c of node.childNodes || []) visit(c);
    };

    visit(root);
    return { text: parts.join("\n"), mailtos: mailtos, tels: tels,
             buttons: buttons, laidOut: laidOut };
  };

  // --- decide which subtree(s) to read -------------------------------------
  let roots = [];

  if (cfg.panelSelector) {
    const el = document.querySelector(cfg.panelSelector);
    if (el) roots = [el];
  }

  if (!roots.length && cfg.rootHints && cfg.rootHints.length) {
    const hits = [];
    const seen = new Set();
    const scan = (node) => {
      if (!node || seen.has(node)) return;
      seen.add(node);
      if (node.nodeType === 1) {
        const cls = typeof node.className === "string" ? node.className : "";
        const sig = (node.id + " " + cls + " " + node.tagName).toLowerCase();
        // Outermost match wins: record it and stop descending.
        if (cfg.rootHints.some((h) => sig.indexOf(h) !== -1)) { hits.push(node); return; }
        if (node.shadowRoot) scan(node.shadowRoot);
      }
      for (const c of node.childNodes || []) scan(c);
    };
    scan(document.documentElement);
    roots = hits;
  }

  // Inside an extension iframe the whole document IS the panel.
  if (!roots.length && cfg.wholeDocument && document.body) roots = [document.body];

  return { url: location.href, roots: roots.map(collect) };
}
"""

JS_CLICK = r"""
(token) => {
  const reg = window.__wizaAuto;
  if (!reg || !reg.map[token]) return "stale";
  const el = reg.map[token];
  try { el.scrollIntoView({ block: "center", inline: "center" }); } catch (e) {}
  try {
    el.click();                       // fires a real MouseEvent; React handlers see it
    return "ok";
  } catch (e) {
    return "error: " + e.message;
  }
}
"""
