// The enable switch on the Sources page: submit its form as soon as it is flipped.
// A file, not an inline script, because of the CSP. Without JavaScript the form keeps
// its Apply button; with it, the "js" class on <html> hides the button.
"use strict";

document.documentElement.classList.add("js");

document.addEventListener("change", function (event) {
  const form = event.target.closest("form[data-autosubmit]");
  if (!form || event.target.name !== "enabled") {
    return;
  }
  form.requestSubmit();
});
