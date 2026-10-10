// Live sentence under the criteria dates: "Looking for a place available by ...".
(function () {
  var DAY = 86400000;
  var MONTH = 30.4375 * DAY;

  function value(id) {
    var el = document.getElementById(id);
    return el ? el.value : "";
  }

  function flex(id, fallback) {
    var n = parseInt(value(id), 10);
    return isNaN(n) || n < 0 ? fallback : n;
  }

  function sentence() {
    var moveIn = value("move_in");
    if (!moveIn) return "";
    var start = new Date(moveIn + "T00:00:00Z");
    if (isNaN(start)) return "";
    var by = new Date(start.getTime() + flex("move_in_flex_days", 7) * DAY);
    var text = "Looking for a place available by " +
      by.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
    var moveOut = value("move_out");
    var end = moveOut ? new Date(moveOut + "T00:00:00Z") : null;
    if (end && !isNaN(end) && end > start) {
      var out = flex("move_out_flex_days", 90);
      var inFlex = flex("move_in_flex_days", 7);
      var low = Math.floor(Math.max(end - start - (out + inFlex) * DAY, 0) / MONTH);
      var high = Math.ceil((end - start + (out + inFlex) * DAY) / MONTH);
      text += " for roughly " + (low >= 1 && low < high ? low + "\u2013" + high : high) + " months";
    }
    return text + ".";
  }

  function update() {
    var el = document.getElementById("dates-summary");
    if (el) el.textContent = sentence();
  }

  document.addEventListener("input", update);
  document.addEventListener("htmx:afterSwap", update);
  document.addEventListener("DOMContentLoaded", update);
})();
