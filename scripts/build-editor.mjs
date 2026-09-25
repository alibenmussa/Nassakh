// Bundle the chapter editor (static/src/editor/index.js: TipTap on the Phase 4 schema) into one plain script,
//   static/dist/editor.js   an IIFE exposing window.NassakhEditor, minified, no source map (committed like app.css)
// Run with `npm run build:editor`. Node is build-time only (PHASE5_SPEC §1); the page loads the file as it is.
import { build } from "esbuild";
import { statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const entry = join(root, "static/src/editor/index.js");
const outfile = join(root, "static/dist/editor.js");

try {
  await build({
    entryPoints: [entry],
    outfile,
    bundle: true,
    format: "iife",
    globalName: "NassakhEditor",
    platform: "browser",
    target: ["es2020", "safari15", "chrome100", "firefox100"],
    minify: true,
    sourcemap: false,
    legalComments: "none",
    logLevel: "warning",
    banner: { js: "/* Nassakh chapter editor bundle: built by scripts/build-editor.mjs from static/src/editor/, do not edit. */" },
  });
} catch (error) {
  console.error(error && error.message ? error.message : error);
  process.exit(1);
}
const size = statSync(outfile).size;
console.log(`built ${outfile.slice(root.length + 1)} (${Math.round(size / 1024)} KB)`);
