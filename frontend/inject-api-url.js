// Build script: copies index.html and injects the API base URL from
// the VITE_API_BASE_URL environment variable (set in Vercel dashboard).
const fs = require("fs");
const path = require("path");

const src = path.join(__dirname, "..", "app", "frontend", "index.html");
const dist = path.join(__dirname, "dist");
const dest = path.join(dist, "index.html");

fs.mkdirSync(dist, { recursive: true });

let html = fs.readFileSync(src, "utf-8");

const apiBase = process.env.VITE_API_BASE_URL;
if (apiBase) {
  // Inject a <meta> tag right after <head> so the frontend picks it up
  html = html.replace("<head>", `<head>\n<meta name="api-base-url" content="${apiBase}">`);
  console.log(`Injected API base URL: ${apiBase}`);
} else {
  console.warn("VITE_API_BASE_URL not set — frontend will use same-origin (local dev mode)");
}

fs.writeFileSync(dest, html, "utf-8");
console.log(`Built frontend → ${dest}`);
