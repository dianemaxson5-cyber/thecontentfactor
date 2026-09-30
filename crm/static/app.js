// Small helpers for the email editor, menus and confirm prompts. No framework needed.
(function () {
  const csrf = document.querySelector('meta[name="csrf"]')?.content || "";

  // Mobile menu
  document.querySelector(".menu-toggle")?.addEventListener("click", () =>
    document.querySelector(".side").classList.toggle("open"));

  // Confirm before destructive or irreversible actions: <form data-confirm="...">
  document.addEventListener("submit", (e) => {
    const submitter = e.submitter;
    const msg = submitter?.dataset.confirm || e.target.dataset.confirm;
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // Insert merge fields at the cursor of the last focused subject/body field
  let lastField = document.querySelector("textarea.body");
  document.querySelectorAll("input[name=subject], textarea.body").forEach((el) =>
    el.addEventListener("focus", () => (lastField = el)));

  function insert(text) {
    if (!lastField) return;
    const start = lastField.selectionStart ?? lastField.value.length;
    const end = lastField.selectionEnd ?? start;
    lastField.value = lastField.value.slice(0, start) + text + lastField.value.slice(end);
    lastField.focus();
    lastField.selectionStart = lastField.selectionEnd = start + text.length;
    lastField.dispatchEvent(new Event("input"));
  }
  document.querySelectorAll("[data-insert]").forEach((btn) =>
    btn.addEventListener("click", () => insert(btn.dataset.insert)));
  document.querySelectorAll("select[data-insert-select]").forEach((sel) =>
    sel.addEventListener("change", () => {
      if (sel.value) insert(sel.value);
      sel.value = "";
    }));

  // Load a saved template into the editor
  document.querySelectorAll("select[data-template-loader]").forEach((sel) =>
    sel.addEventListener("change", () => {
      const opt = sel.selectedOptions[0];
      if (!opt || !opt.value) return;
      const form = sel.closest("form");
      form.querySelector("input[name=subject]").value = opt.dataset.subject;
      form.querySelector("textarea.body").value = opt.dataset.body;
      form.querySelector("textarea.body").dispatchEvent(new Event("input"));
    }));

  // Live preview
  function post(url, form) {
    const data = new FormData();
    data.append("csrf", csrf);
    data.append("subject", form.querySelector("input[name=subject]")?.value || "");
    data.append("body", form.querySelector("textarea.body")?.value || "");
    const who = form.querySelector("[data-preview-contact]");
    const checked = form.querySelector("input[name=contact_ids]:checked");
    data.append("contact_id", (who && who.value) || (checked && checked.value) || "");
    return fetch(url, { method: "POST", body: data }).then((r) => r.json());
  }

  document.querySelectorAll("form[data-editor]").forEach((form) => {
    const frame = form.querySelector(".preview-frame");
    const subj = form.querySelector(".preview-subject");
    let timer;
    const refresh = () => {
      if (!frame) return;
      post("/preview", form).then((res) => {
        frame.srcdoc = res.html;
        if (subj) subj.textContent = res.subject ? "Subject: " + res.subject : "";
      });
    };
    form.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(refresh, 400); });
    form.addEventListener("change", () => { clearTimeout(timer); timer = setTimeout(refresh, 100); });
    refresh();

    form.querySelector("[data-send-test]")?.addEventListener("click", (e) => {
      e.preventDefault();
      post("/send-test", form).then((res) => window.alert(res.message));
    });
  });

  // Recipient search filter on compose
  const filter = document.querySelector("[data-recipient-filter]");
  filter?.addEventListener("input", () => {
    const q = filter.value.toLowerCase();
    document.querySelectorAll(".recipients label").forEach((l) =>
      (l.style.display = l.textContent.toLowerCase().includes(q) ? "" : "none"));
  });

  // Select-all on contact list
  document.querySelector("[data-select-all]")?.addEventListener("change", (e) =>
    document.querySelectorAll("input[name=ids]").forEach((c) => (c.checked = e.target.checked)));

  // Campaign audience counter
  const counts = window.AUDIENCE_COUNTS;
  if (counts) {
    const stage = document.querySelector("select[name=segment_stage]");
    const tag = document.querySelector("select[name=segment_tag]");
    const out = document.querySelector("[data-audience-count]");
    const update = () => {
      const n = counts[stage.value + "|" + tag.value] ?? 0;
      out.textContent = n + (n === 1 ? " subscribed contact" : " subscribed contacts");
      const btn = document.querySelector("button[value=send]");
      if (btn) btn.dataset.confirm = "Send this campaign to " + n + " contact(s) now? This can't be undone.";
    };
    stage.addEventListener("change", update);
    tag.addEventListener("change", update);
    update();
  }

  // Copy buttons
  document.querySelectorAll("[data-copy]").forEach((btn) =>
    btn.addEventListener("click", () => {
      navigator.clipboard.writeText(btn.dataset.copy).then(() => {
        const old = btn.textContent;
        btn.textContent = "Copied";
        setTimeout(() => (btn.textContent = old), 1200);
      });
    }));
})();
