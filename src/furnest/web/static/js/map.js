// The dashboard's Map view (#214): pins for the filtered properties, clustered.
// The map container carries its settings in data-* attributes and the pins come
// from its points URL. Popups are built from DOM nodes and textContent, never HTML.
(function () {
  var current = null; // { el, map }
  var FALLBACK_CENTER = [39.5, -98.35]; // the contiguous US, when nothing has a pin
  var FALLBACK_ZOOM = 4;

  function popup(point) {
    var box = document.createElement("div");
    box.className = "map-popup";
    if (point.photo) {
      var img = document.createElement("img");
      img.src = point.photo;
      img.alt = "";
      img.loading = "lazy";
      img.decoding = "async";
      img.referrerPolicy = "no-referrer";
      img.addEventListener("error", function () { img.remove(); });
      box.appendChild(img);
    }
    var title = document.createElement("a");
    title.className = "map-popup-title";
    title.href = point.url;
    title.textContent = point.title;
    box.appendChild(title);
    var facts = document.createElement("p");
    var status = document.createElement("span");
    status.className = "status status-" + point.status;
    status.textContent = point.status_label;
    facts.appendChild(status);
    if (point.rent) facts.appendChild(document.createTextNode(" " + point.rent));
    box.appendChild(facts);
    return box;
  }

  function note(el, data) {
    var target = el.parentNode.querySelector("[data-map-note]");
    if (!target) return;
    var parts = [];
    if (data.truncated) parts.push("Showing the newest " + data.points.length + " properties.");
    if (data.unlocated) {
      parts.push(data.unlocated + (data.unlocated === 1 ? " property" : " properties") +
        " not shown (no location).");
    }
    target.textContent = parts.join(" ");
  }

  function draw(entry, data) {
    var cluster = L.markerClusterGroup({ showCoverageOnHover: false });
    var bounds = [];
    data.points.forEach(function (point) {
      var icon = L.divIcon({ className: "pin pin-" + point.status, iconSize: [16, 16] });
      var marker = L.marker([point.lat, point.lng], { icon: icon, title: point.title });
      marker.bindPopup(function () { return popup(point); }, { maxWidth: 240 });
      cluster.addLayer(marker);
      bounds.push([point.lat, point.lng]);
    });
    entry.map.addLayer(cluster);
    if (bounds.length) entry.map.fitBounds(bounds, { padding: [24, 24], maxZoom: 15 });
    note(entry.el, data);
  }

  function init() {
    if (current && !document.body.contains(current.el)) {
      current.map.remove();
      current = null;
    }
    var el = document.getElementById("map");
    if (!el || (current && current.el === el) || typeof L === "undefined") return;
    // A page restored from htmx's history cache still holds the old map's nodes.
    while (el.firstChild) el.removeChild(el.firstChild);
    var map = L.map(el).setView(FALLBACK_CENTER, FALLBACK_ZOOM);
    L.tileLayer(el.dataset.tileUrl, {
      attribution: el.dataset.tileAttribution,
      maxZoom: parseInt(el.dataset.maxZoom, 10) || 19,
      // The page's Referrer-Policy is same-origin; OSM's tile servers need a Referer.
      referrerPolicy: "strict-origin-when-cross-origin",
    }).addTo(map);
    var entry = { el: el, map: map };
    current = entry;
    fetch(el.dataset.pointsUrl, { headers: { Accept: "application/json" } })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        if (current === entry) draw(entry, data);
      })
      .catch(function () {
        var target = el.parentNode.querySelector("[data-map-note]");
        if (target && current === entry) target.textContent = "Couldn't load the pins. Reload to try again.";
      });
  }

  document.addEventListener("htmx:afterSwap", init);
  document.addEventListener("htmx:historyRestore", init);
  document.addEventListener("DOMContentLoaded", init);
})();
