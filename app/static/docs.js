"use strict";
window.addEventListener("DOMContentLoaded", () => {
  SwaggerUIBundle({
    url: "/openapi.json",
    dom_id: "#swagger-ui",
    deepLinking: true,
    validatorUrl: null,
    supportedSubmitMethods: ["get", "post", "put", "patch", "delete"],
    requestInterceptor(request) {
      request.credentials = "same-origin";
      const entry = document.cookie.split("; ").find(value => value.startsWith("arm_csrf="));
      if (entry && !["GET", "HEAD", "OPTIONS"].includes((request.method || "GET").toUpperCase())) {
        request.headers["X-CSRF-Token"] = decodeURIComponent(entry.slice("arm_csrf=".length));
      }
      return request;
    }
  });
});
