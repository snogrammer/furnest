// Starts Swagger UI on /api/docs. A file, not an inline script, because of the CSP.
"use strict";

document.addEventListener("DOMContentLoaded", function () {
  const root = document.getElementById("swagger");
  // eslint-disable-next-line no-undef
  SwaggerUIBundle({
    url: root.dataset.specUrl,
    domNode: root,
    deepLinking: true,
    // The schema needs the session cookie; "Try it out" needs the Bearer token
    // entered under "Authorize", since /api/v1 doesn't accept the cookie.
    withCredentials: true,
  });
});
