"use strict";

(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const readJSON = (id, fallback) => {
    try { return JSON.parse($(id).textContent); } catch { return fallback; }
  };
  const kindles = readJSON("#kindles-data", []);
  const smtpOk = readJSON("#smtp-data", false);

  // ---- avisos ----------------------------------------------------------------
  let toastTimer;
  function toast(message, isError = false) {
    const el = $("#toast");
    el.textContent = message;
    el.classList.toggle("error", isError);
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), isError ? 7000 : 4500);
  }

  // ---- búsqueda en vivo ------------------------------------------------------
  const form = $("#search-form");
  const results = $("#results");
  if (form && results) {
    let timer, controller;
    form.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const params = new URLSearchParams(new FormData(form));
        for (const [k, v] of [...params]) if (!v) params.delete(k);
        controller?.abort();
        controller = new AbortController();
        try {
          const res = await fetch(`/?${params}&partial=1`, { signal: controller.signal });
          if (!res.ok) return;
          results.innerHTML = await res.text();
          history.replaceState(null, "", params.toString() ? `/?${params}` : "/");
          $(".hero")?.classList.toggle("compact", params.toString() !== "");
        } catch (err) {
          if (err.name !== "AbortError") console.error(err);
        }
      }, 250);
    });
  }

  // ---- trabajos (descarga / envío) ------------------------------------------
  async function api(url, options = {}) {
    let res;
    try {
      res = await fetch(url, { headers: { "Content-Type": "application/json" }, ...options });
    } catch {
      // Sesión de Cloudflare Access caducada (redirige a otro dominio) o sin conexión.
      location.reload();
      throw new Error("Sin conexión");
    }
    const json = (res.headers.get("content-type") || "").includes("application/json");
    // Si la sesión de Cloudflare Access caduca, la respuesta es su página de login.
    if (!json || res.redirected) { location.reload(); throw new Error("Sesión caducada"); }
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Algo ha fallado");
    return data;
  }

  function setBusy(button, label, progress) {
    button.classList.add("busy");
    button.disabled = true;
    button.style.setProperty("--progress", `${Math.round((progress || 0) * 100)}%`);
    const span = button.querySelector("span");
    if (span) span.textContent = label;
  }

  function reset(button) {
    button.classList.remove("busy");
    button.disabled = false;
    const span = button.querySelector("span");
    if (span && button.dataset.label) span.textContent = button.dataset.label;
  }

  async function follow(jobId, button) {
    for (;;) {
      await new Promise((r) => setTimeout(r, 1000));
      const job = await api(`/api/trabajos/${jobId}`);
      if (job.state === "done" || job.state === "error") return job;
      const label = job.state === "sending" ? "Enviando…"
        : job.peers ? `${Math.round(job.progress * 100)} %` : "Buscando…";
      setBusy(button, label, job.progress);
    }
  }

  async function runJob(button, start, onDone) {
    const span = button.querySelector("span");
    button.dataset.label ??= span ? span.textContent : "";
    setBusy(button, "Preparando…", 0);
    try {
      const { job_id } = await start();
      const job = await follow(job_id, button);
      if (job.state === "error") throw new Error(job.message);
      onDone(job);
    } catch (err) {
      reset(button);
      toast(err.message, true);
    }
  }

  function sendToKindle(button, kindle) {
    const bookId = button.dataset.kindle;
    try { localStorage.setItem("kindle", String(kindle.id)); } catch {}
    runJob(
      button,
      () => api(`/api/libros/${bookId}/kindle`, {
        method: "POST",
        body: JSON.stringify({ kindle_id: kindle.id }),
      }),
      (job) => {
        reset(button);
        button.classList.add("done");
        const span = button.querySelector("span");
        if (span) span.textContent = "Enviado ✓";
        toast(job.message);
      },
    );
  }

  // ---- selector de Kindle ----------------------------------------------------
  let picker;
  function closePicker() { picker?.remove(); picker = null; }

  function openPicker(button) {
    closePicker();
    let last = null;
    try { last = localStorage.getItem("kindle"); } catch {}
    const ordered = [...kindles].sort((a, b) => (String(b.id) === last) - (String(a.id) === last));
    picker = document.createElement("div");
    picker.className = "picker";
    picker.setAttribute("role", "menu");
    for (const k of ordered) {
      const item = document.createElement("button");
      item.type = "button";
      item.setAttribute("role", "menuitem");
      item.innerHTML = "<strong></strong><small></small>";
      item.querySelector("strong").textContent = k.name;
      item.querySelector("small").textContent = k.email;
      item.addEventListener("click", () => { closePicker(); sendToKindle(button, k); });
      picker.append(item);
    }
    document.body.append(picker);
    const rect = button.getBoundingClientRect();
    const left = Math.min(rect.left + scrollX, scrollX + document.documentElement.clientWidth - picker.offsetWidth - 16);
    picker.style.left = `${Math.max(16, left)}px`;
    picker.style.top = `${rect.bottom + scrollY + 6}px`;
    picker.querySelector("button")?.focus();
  }

  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePicker(); });

  document.addEventListener("click", (event) => {
    if (picker && !picker.contains(event.target)) closePicker();

    const kindleBtn = event.target.closest("[data-kindle]");
    if (kindleBtn && !kindleBtn.disabled) {
      event.preventDefault();
      event.stopPropagation();
      if (!smtpOk) return toast("Falta configurar el correo (SMTP) en el fichero .env", true);
      if (!kindles.length) {
        toast("Primero añade tu Kindle en Ajustes", true);
        setTimeout(() => { location.href = "/ajustes"; }, 1200);
        return;
      }
      if (kindles.length === 1) return sendToKindle(kindleBtn, kindles[0]);
      return openPicker(kindleBtn);
    }

    const dl = event.target.closest("[data-download]");
    if (dl) {
      event.preventDefault();
      if (dl.classList.contains("busy")) return;
      runJob(
        dl,
        () => api(`/api/archivos/${dl.dataset.download}/preparar`, { method: "POST" }),
        (job) => {
          reset(dl);
          location.href = `/descargar/${dl.dataset.download}?job=${job.id}`;
        },
      );
    }
  });

  document.addEventListener("submit", (event) => {
    const message = event.target.dataset.confirm;
    if (message && !confirm(message)) event.preventDefault();
  });
})();
