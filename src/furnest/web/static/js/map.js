// The dashboard's Map view (#214): pins for the filtered properties, clustered,
// the saved search outlines, and Geoman tools to draw a new one.
// The map container carries its settings in data-* attributes and the pins come
// from its points URL. Popups are built from DOM nodes and textContent, never HTML.
// A drawn shape fills the save form's hidden field; the form posts it as GeoJSON.
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

  function savedOutlines(el) {
    var outlines;
    try {
      outlines = JSON.parse(el.dataset.outlines || "[]");
    } catch (e) {
      return [];
    }
    return outlines.map(function (outline) {
      var layer = L.geoJSON(
        { type: "Polygon", coordinates: [outline.polygon] },
        { pmIgnore: true, interactive: false, style: function () { return { className: "outline-saved" }; } }
      );
      layer.bindTooltip(outline.name, { sticky: true });
      return layer;
    });
  }

  function drawingTools(entry) {
    if (!entry.map.pm) return; // Geoman missing: the map still shows pins.
    var box = entry.el.parentNode;
    var form = box.querySelector("[data-outline-form]");
    var field = box.querySelector("[data-outline-polygon]");
    var hint = box.querySelector("[data-outline-hint]");
    if (!form || !field) return;
    entry.map.pm.setGlobalOptions({ pathOptions: { className: "outline-draft" } });
    entry.map.pm.addControls({
      position: "topleft",
      drawPolygon: true,
      drawRectangle: true,
      editMode: true,
      removalMode: true,
      drawMarker: false,
      drawCircleMarker: false,
      drawPolyline: false,
      drawCircle: false,
      drawText: false,
      dragMode: false,
      cutPolygon: false,
      rotateMode: false,
    });
    var draft = null;

    function update() {
      field.value = draft ? JSON.stringify(draft.toGeoJSON().geometry) : "";
      form.hidden = !draft;
      if (hint) hint.hidden = !!draft;
      if (draft && !form.elements.name.value) form.elements.name.focus();
    }

    entry.map.on("pm:create", function (event) {
      // One shape at a time: drawing again replaces the unsaved one.
      if (draft) entry.map.removeLayer(draft);
      draft = event.layer;
      draft.on("pm:edit", update);
      update();
    });
    entry.map.on("pm:remove", function (event) {
      if (event.layer === draft) {
        draft = null;
        update();
      }
    });
  }

  function draw(entry, data) {
    // pmIgnore: Geoman's edit and removal modes must not touch the pins.
    var cluster = L.markerClusterGroup({ showCoverageOnHover: false, pmIgnore: true });
    var bounds = [];
    data.points.forEach(function (point) {
      var icon = L.divIcon({ className: "pin pin-" + point.status, iconSize: [16, 16] });
      var marker = L.marker([point.lat, point.lng], { icon: icon, title: point.title, pmIgnore: true });
      marker.bindPopup(function () { return popup(point); }, { maxWidth: 240 });
      cluster.addLayer(marker);
      bounds.push([point.lat, point.lng]);
    });
    entry.map.addLayer(cluster);
    var all = L.latLngBounds(bounds);
    entry.outlines.forEach(function (layer) { all.extend(layer.getBounds()); });
    if (all.isValid()) entry.map.fitBounds(all, { padding: [24, 24], maxZoom: 15 });
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
    var entry = { el: el, map: map, outlines: savedOutlines(el) };
    entry.outlines.forEach(function (layer) { layer.addTo(map); });
    drawingTools(entry);
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
