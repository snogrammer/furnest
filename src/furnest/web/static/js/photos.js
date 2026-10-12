// Listing photos that fail to load (a blocked CDN, a 502 from /photo) get a
// "Photo unavailable" placeholder instead of collapsing to nothing (#212).
(function () {
  var WRAPPERS = ".card-photo, .gallery li";

  function mark(img) {
    var wrapper = img.closest(WRAPPERS);
    if (wrapper) wrapper.classList.add("is-broken");
  }

  // ``error`` doesn't bubble, so listen in the capture phase. This also catches
  // photos in results htmx swaps in later.
  document.addEventListener("error", function (event) {
    var target = event.target;
    if (target instanceof HTMLImageElement) mark(target);
  }, true);

  // Photos that already failed before this script ran.
  document.querySelectorAll(".card-photo img, .gallery img").forEach(function (img) {
    if (img.complete && img.naturalWidth === 0 && img.getAttribute("src")) mark(img);
  });
})();
