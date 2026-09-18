/*
 * In-page perception. Returns an accessibility-shaped projection of one frame.
 *
 * Why compute this in-page instead of reading Chrome's own AX tree: on the
 * legacy markup this system targets, the browser's AX tree leaves most controls
 * *unnamed* -- a text box whose label is a sibling <td> has no accessible name
 * at all. The recovery of that label from layout is the whole job, and it has
 * to happen where the layout is. The output shape is deliberately the shape a
 * macOS AX / Windows UIA tree already has, so a desktop driver is a producer
 * swap rather than a redesign.
 */
(function (opts) {
  const MAX_TEXT = 120;
  const norm = (s) => (s || "").replace(/ /g, " ").replace(/\s+/g, " ").trim();
  const vis = (el) => {
    const s = getComputedStyle(el);
    if (s.display === "none" || s.visibility === "hidden" || s.opacity === "0") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  // --- role mapping: tag/type -> AX role -----------------------------------
  function roleOf(el) {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === "input") {
      const t = (el.type || "text").toLowerCase();
      if (t === "submit" || t === "button" || t === "reset" || t === "image") return "button";
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      if (t === "password") return "textbox";  // AX calls this textbox; sensitivity is our concern
      return "textbox";
    }
    if (tag === "textarea") return "textbox";
    if (tag === "select") return el.multiple ? "listbox" : "combobox";
    if (tag === "button") return "button";
    if (tag === "a") return el.hasAttribute("href") ? "link" : "generic";
    if (tag === "summary") return "button";
    if (tag === "th") return "columnheader";
    if (tag === "td") return "cell";
    return "generic";
  }

  // --- accessible name: the ARIA subset browsers actually agree on ---------
  function accName(el) {
    const al = el.getAttribute("aria-label");
    if (norm(al)) return norm(al);
    const lb = el.getAttribute("aria-labelledby");
    if (lb) {
      const parts = lb.split(/\s+/).map((id) => {
        const n = document.getElementById(id);
        return n ? norm(n.innerText || n.textContent) : "";
      }).filter(Boolean);
      if (parts.length) return norm(parts.join(" "));
    }
    if (el.labels && el.labels.length) {
      const t = norm(el.labels[0].innerText || el.labels[0].textContent);
      if (t) return t;
    }
    const tag = el.tagName.toLowerCase();
    if (tag === "input") {
      const ty = (el.type || "").toLowerCase();
      if (ty === "submit" || ty === "button" || ty === "reset") return norm(el.value);
      if (ty === "image") return norm(el.alt);
    }
    if (tag === "button" || tag === "a" || tag === "summary") {
      const t = norm(el.innerText || el.textContent);
      if (t) return t;
      const img = el.querySelector("img[alt]");
      if (img) return norm(img.alt);
    }
    if (norm(el.getAttribute("title"))) return norm(el.getAttribute("title"));
    return "";
  }

  // --- legacy label recovery: where old apps actually put the label --------
  // Ordered by how much we trust it; the first hit wins.
  function inferLabel(el) {
    // 1. the cell immediately left of ours in a layout table
    const td = el.closest("td, th");
    if (td) {
      let prev = td.previousElementSibling;
      while (prev && !norm(prev.innerText)) prev = prev.previousElementSibling;
      if (prev) {
        const t = norm(prev.innerText);
        if (t && t.length <= 60) return t;
      }
      // 2. same cell, text before the control (e.g. "Amount: [input]")
      const own = norm(td.innerText);
      const mine = norm(el.value || el.innerText || "");
      if (own && own !== mine && own.length <= 60) {
        const stripped = norm(own.replace(mine, ""));
        if (stripped) return stripped.replace(/[:*]\s*$/, "");
      }
    }
    // 3. preceding text in the same parent
    let p = el.previousSibling;
    while (p) {
      const t = norm(p.textContent);
      if (t) return t.replace(/[:*]\s*$/, "").slice(0, 60);
      p = p.previousSibling;
    }
    // 4. placeholder as a weak last resort
    if (norm(el.placeholder)) return norm(el.placeholder);
    // 5. the generated control name, de-camelised: ctl00$MainContent$txtMemberId
    const nm = el.getAttribute("name") || "";
    if (nm) {
      const leaf = nm.split(/[$:.]/).pop() || "";
      const words = leaf.replace(/^(txt|ddl|btn|chk|lst|rad|lbl)/i, "")
                        .replace(/([a-z])([A-Z])/g, "$1 $2").trim();
      if (words) return words;
    }
    return "";
  }

  // --- table context, so a value can be addressed by row + column ----------
  function tableCtx(el) {
    const cell = el.closest("td, th");
    if (!cell) return null;
    const row = cell.closest("tr");
    if (!row) return null;
    const table = row.closest("table");
    const cells = Array.from(row.children);
    const colIndex = cells.indexOf(cell);
    let colHeader = "";
    if (table) {
      // A column header only means something in a grid. Layout tables -- which
      // is most of a legacy app -- have no headers, and inventing one produces
      // anchors that look precise and are not.
      const rows = Array.from(table.rows);
      let headerRow = rows.find((r) =>
        Array.from(r.children).some((c) => c.tagName === "TH"));
      if (!headerRow && rows.length >= 2) {
        const first = rows[0];
        const sameShape = first !== row && first.children.length === cells.length;
        const looksLikeHeadings = first.children.length >= 2 &&
          Array.from(first.children).every((c) => {
            const t = norm(c.innerText);
            return t.length > 0 && t.length < 30;
          });
        if (sameShape && looksLikeHeadings) headerRow = first;
      }
      if (headerRow && headerRow !== row && headerRow.children[colIndex]) {
        colHeader = norm(headerRow.children[colIndex].innerText);
      }
    }
    return {
      row_text: cells.map((c) => norm(c.innerText)).join(" | "),
      col_index: String(colIndex),
      col_header: colHeader,
    };
  }

  function cssPath(el) {
    const parts = [];
    let cur = el;
    for (let d = 0; cur && cur.nodeType === 1 && d < 4; d++) {
      let seg = cur.tagName.toLowerCase();
      if (cur.id) { parts.unshift("#" + CSS.escape(cur.id)); break; }
      const sibs = cur.parentElement ? Array.from(cur.parentElement.children).filter(
        (c) => c.tagName === cur.tagName) : [];
      if (sibs.length > 1) seg += ":nth-of-type(" + (sibs.indexOf(cur) + 1) + ")";
      parts.unshift(seg);
      cur = cur.parentElement;
    }
    return parts.join(" > ");
  }

  // --- collect -------------------------------------------------------------
  const INTERACTIVE = 'input:not([type=hidden]), select, textarea, button, a[href], ' +
                      '[role=button], [role=link], [role=textbox], [onclick], summary';
  const controls = [];
  let n = 0;
  document.querySelectorAll("[data-lh-h]").forEach((e) => e.removeAttribute("data-lh-h"));

  for (const el of document.querySelectorAll(INTERACTIVE)) {
    if (!vis(el)) continue;
    const h = opts.prefix + "e" + (++n);
    el.setAttribute("data-lh-h", h);
    const r = el.getBoundingClientRect();
    const name = accName(el);
    controls.push({
      handle: h,
      role: roleOf(el),
      name: name,
      inferred_label: name ? "" : inferLabel(el),
      // A <select> reports its option's *value* attribute ("SAV"), not the text
      // a person reads ("SAVINGS"). Surfacing the attribute makes a correctly
      // selected control look wrong to anyone reasoning about the screen -- the
      // model re-selected it in a loop rather than moving on. Report what is
      // displayed; the value attribute is an implementation detail.
      value: el.tagName === "SELECT"
        ? norm((el.selectedOptions && el.selectedOptions[0]
                ? el.selectedOptions[0].text : el.value) || "")
        : ((el.value !== undefined && el.type !== "password") ? norm(String(el.value)) : ""),
      text: norm((el.innerText || "").slice(0, MAX_TEXT)),
      enabled: !el.disabled,
      editable: /^(INPUT|TEXTAREA)$/.test(el.tagName) && !el.readOnly && !el.disabled,
      focused: document.activeElement === el,
      box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      attrs: Object.assign({
        tag: el.tagName.toLowerCase(),
        type: (el.type || ""),
        name: el.getAttribute("name") || "",
        id: el.id || "",
        href: el.getAttribute("href") || "",
        options: el.tagName === "SELECT"
          ? Array.from(el.options).map((o) => norm(o.text)).filter(Boolean).join("|") : "",
        css: cssPath(el),
      }, tableCtx(el) || {}),
    });
  }

  // Readable value-bearing nodes: table cells and short leaf text blocks.
  // These are extraction targets, not action targets.
  const readables = [];
  let m = 0;
  const seen = new Set();
  for (const el of document.querySelectorAll("td, th, li, dd, span, b, strong, div")) {
    if (!vis(el)) continue;
    if (el.querySelector(INTERACTIVE)) continue;
    if (el.children.length > 2) continue;
    const t = norm(el.innerText);
    if (!t || t.length > MAX_TEXT) continue;
    const ctx = tableCtx(el);
    const key = t + "|" + (ctx ? ctx.row_text : "") + "|" + (ctx ? ctx.col_index : "");
    if (seen.has(key)) continue;
    seen.add(key);
    const h = opts.prefix + "t" + (++m);
    el.setAttribute("data-lh-h", h);
    const r = el.getBoundingClientRect();
    readables.push({
      handle: h, role: roleOf(el) === "generic" ? "text" : roleOf(el),
      name: "", inferred_label: "", value: "", text: t,
      enabled: true, editable: false, focused: false,
      box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      attrs: Object.assign({ tag: el.tagName.toLowerCase(), css: cssPath(el) }, ctx || {}),
    });
  }

  // Visible prose, for the agent's situational awareness.
  const bodyText = norm(document.body ? document.body.innerText : "");
  const lines = (document.body ? document.body.innerText : "")
    .split("\n").map(norm).filter((s) => s.length > 1).slice(0, 60);

  return {
    url: location.href,
    title: document.title,
    controls: controls,
    readables: readables,
    lines: lines,
    body_text: bodyText.slice(0, 8000),
  };
})
