// Copy buttons: <button data-copy-target="ID"> copies the value of the field with
// that id. A file, not an inline script, because of the CSP. Without the Clipboard
// API (an insecure origin), the field's text is selected for a manual copy instead.
"use strict";

document.addEventListener("click", function (event) {
  const button = event.target.closest("[data-copy-target]");
  if (!button) {
    return;
  }
  const field = document.getElementById(button.dataset.copyTarget);
  if (!field) {
    return;
  }
  const label = button.textContent;
  const done = function (text) {
    button.textContent = text;
    setTimeout(function () {
      button.textContent = label;
    }, 1500);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(field.value).then(
      function () {
        done("Copied");
      },
      function () {
        field.select();
      },
    );
  } else {
    field.select();
    done("Selected: press Ctrl/⌘+C");
  }
});
