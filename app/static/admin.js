/* Makes the console act rather than navigate.
 *
 * Every control is a real <form> that works with this file absent - the server
 * answers a redirect when nothing asks for JSON. What this adds is the part
 * that makes it feel like an instrument: the page stays put, the row updates,
 * and a confirmation is a dialog written here rather than the browser's, which
 * announces the hostname and looks like a phishing prompt on the one screen
 * that changes billing.
 */
(function () {
  "use strict";

  var veil = document.getElementById("veil");
  var pending = null;

  function toast(text, bad) {
    var el = document.getElementById("toast");
    if (!el) return;
    el.textContent = text;
    el.style.background = bad ? "#B3411A" : "";
    el.classList.add("on");
    clearTimeout(el._t);
    el._t = setTimeout(function () {
      el.classList.remove("on");
    }, 3200);
  }

  function ask(question, detail, onYes) {
    if (!veil) return onYes();
    veil.querySelector("h3").textContent = question;
    veil.querySelector("p").textContent = detail || "";
    pending = onYes;
    veil.classList.add("on");
    veil.querySelector(".go").focus();
  }

  function close() {
    if (veil) veil.classList.remove("on");
    pending = null;
  }

  if (veil) {
    veil.querySelector(".go").addEventListener("click", function () {
      var go = pending;
      close();
      if (go) go();
    });
    veil.querySelector(".no").addEventListener("click", close);
    veil.addEventListener("click", function (e) {
      if (e.target === veil) close();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") close();
    });
  }

  /* Refresh only the parts that carry numbers, so pressing a button does not
     cost a page load and lose your scroll position. */
  function refresh() {
    var here = new URL(window.location.href);
    fetch(here.pathname + here.search, { headers: { "X-Partial": "1" } })
      .then(function (r) {
        return r.text();
      })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        ["figs", "needs", "wsp", "cap", "state"].forEach(function (id) {
          var fresh = doc.getElementById(id);
          var here = document.getElementById(id);
          if (fresh && here) here.innerHTML = fresh.innerHTML;
        });
        wire();
      })
      .catch(function () {
        /* A refresh that fails leaves the page as it was, which is honest
           enough: the action itself already reported its own outcome. */
      });
  }

  function send(form) {
    var data = new FormData(form);
    data.append("ajax", "1");
    form.classList.add("busy");

    fetch(form.action, { method: "POST", body: data })
      .then(function (r) {
        return r.ok ? r.json() : { ok: false, message: "That did not work" };
      })
      .then(function (out) {
        toast(out.message || "Done", !out.ok);
        form.reset();
        refresh();
      })
      .catch(function () {
        toast("That did not work", true);
      })
      .finally(function () {
        form.classList.remove("busy");
      });
  }

  function wire() {
    document.querySelectorAll("form[action^='/admin/']").forEach(function (form) {
      if (form._wired) return;
      form._wired = true;
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        var q = form.getAttribute("data-ask");
        if (q) {
          ask(q, form.getAttribute("data-detail"), function () {
            send(form);
          });
        } else {
          send(form);
        }
      });
    });
  }

  wire();
})();
