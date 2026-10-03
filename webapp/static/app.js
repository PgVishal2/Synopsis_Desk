// Tender Synopsis - small progressive enhancements (the app works without JS except live progress)
(function () {
  const $ = (s, r = document) => r.querySelector(s);

  // ---- upload: drag & drop + file list
  const drop = $("#drop"), input = $("#files"), list = $("#file-list");
  if (drop && input) {
    let picked = new DataTransfer();
    const render = () => {
      list.innerHTML = "";
      [...picked.files].forEach((f, i) => {
        const li = document.createElement("li");
        li.innerHTML = `<span class="n"></span><span class="small muted">${(f.size / 1048576).toFixed(1)} MB</span>
          <button type="button" aria-label="Remove">×</button>`;
        li.querySelector(".n").textContent = f.name;
        li.querySelector("button").onclick = (e) => {
          e.preventDefault(); e.stopPropagation();
          const next = new DataTransfer();
          [...picked.files].forEach((g, j) => j !== i && next.items.add(g));
          picked = next; input.files = picked.files; render();
        };
        list.appendChild(li);
      });
    };
    const add = (files) => { [...files].forEach(f => picked.items.add(f)); input.files = picked.files; render(); };
    input.addEventListener("change", () => { const fs = [...input.files]; picked = new DataTransfer(); add(fs); });
    ["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("over"); }));
    drop.addEventListener("drop", e => add(e.dataTransfer.files));
    $("#upload-form").addEventListener("submit", () => {
      const b = $("#go"); b.disabled = true; b.lastChild.textContent = " Uploading…";
    });
  }

  // ---- job progress polling
  const prog = $("#progress");
  if (prog) {
    const steps = ["read", "write", "check", "done"];
    const stepOf = (msg) => {
      const m = (msg || "").toLowerCase();
      if (m.includes("checking figures")) return 2;
      if (m.includes("writing")) return 1;
      if (m.includes("finished")) return 3;
      return 0;
    };
    const paint = (i) => document.querySelectorAll("#steps li").forEach((li, k) => {
      li.classList.toggle("done", k < i); li.classList.toggle("now", k === i);
      if (k < i) li.querySelector(".dot").textContent = "✓";
    });
    paint(stepOf($("#live-msg").textContent));
    const tick = async () => {
      try {
        const r = await fetch(`/api/jobs/${prog.dataset.job}`, { headers: { "Accept": "application/json" } });
        if (r.status === 401) { location.href = "/login"; return; }
        const j = await r.json();
        $("#live-msg").textContent = j.step || "";
        if (j.status === "done" || j.status === "failed") { paint(3); location.reload(); return; }
        paint(stepOf(j.step));
      } catch (e) { /* network blip: try again */ }
      setTimeout(tick, 2500);
    };
    setTimeout(tick, 1500);
  }

  // ---- edit form: add/remove rows of the top table
  const kft = $("#kft"), addRow = $("#add-row");
  if (kft && addRow) {
    kft.addEventListener("click", e => { if (e.target.matches("[data-remove-row]")) e.target.closest(".kft-row").remove(); });
    addRow.addEventListener("click", () => {
      const d = document.createElement("div"); d.className = "kft-row";
      d.innerHTML = `<input type="text" name="kft_label" aria-label="Label"><textarea name="kft_value" rows="2" aria-label="Value"></textarea>
        <button type="button" class="btn ghost small" data-remove-row>Remove</button>`;
      kft.appendChild(d); d.querySelector("input").focus();
    });
  }

  // ---- confirm destructive forms
  document.querySelectorAll("form[data-confirm]").forEach(f =>
    f.addEventListener("submit", e => { if (!confirm(f.dataset.confirm)) e.preventDefault(); }));

  // ---- settings: test AI
  const t = $("#test-ai");
  if (t) t.addEventListener("click", async () => {
    t.disabled = true; t.textContent = "Testing…";
    const out = $("#test-out"); out.innerHTML = "";
    try {
      const r = await fetch(t.dataset.url, { method: "POST", headers: { "X-CSRF-Token": window.CSRF } });
      const j = await r.json();
      j.lines.forEach(line => {
        const li = document.createElement("li");
        li.className = line.startsWith("✅") ? "info" : "error"; li.textContent = line; out.appendChild(li);
      });
    } catch (e) { out.innerHTML = '<li class="error">Could not reach the server.</li>'; }
    t.disabled = false; t.textContent = "Test now";
  });
})();
