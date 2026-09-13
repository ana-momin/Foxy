/* The announcement preview.
 *
 * Slack's mrkdwn is not markdown: single asterisks are bold, underscores are
 * italic, and links are <url|label>. Guessing at that from a plain textarea is
 * how a message goes out to every channel with a stray asterisk in it, so this
 * renders the same shapes the server will send.
 */
(function () {
  "use strict";

  var form = document.getElementById("annc");
  if (!form) return;

  var TONES = {
    news: ["📢", "Announcement"],
    update: ["✨", "What's new"],
    "heads-up": ["⚠️", "Heads up"],
    thanks: ["👋", "From Foxy"],
    gift: ["🎉", "Good news"]
  };

  function esc(s) {
    return s.replace(/[&<>]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c];
    });
  }

  function mrkdwn(s) {
    return esc(s)
      .replace(/&lt;(https?:[^|&]+)\|([^&]+)&gt;/g, '<a href="$1">$2</a>')
      .replace(/\*([^*\n]+)\*/g, "<b>$1</b>")
      .replace(/_([^_\n]+)_/g, "<em>$1</em>");
  }

  function draw() {
    var tone = TONES[form.tone.value] || TONES.news;
    var head = document.getElementById("p-head");
    var text = document.getElementById("p-text");
    var btn = document.getElementById("p-btn");

    head.textContent = tone[0] + "  " + (form.title.value.trim() || tone[1]);

    var body = form.message.value;
    text.innerHTML = body.trim()
      ? mrkdwn(body)
      : '<span style="opacity:.45">Your message appears here.</span>';

    var label = form.link_label.value.trim();
    var url = form.link_url.value.trim();
    btn.hidden = !(label && url);
    if (!btn.hidden) {
      btn.textContent = label;
      btn.href = url;
    }
  }

  form.addEventListener("input", draw);
  form.addEventListener("change", draw);
  draw();
})();
