// Copy runtime assets out of node_modules so the app needs no CDN and no Node at runtime.
//   static/vendor/alpine.min.js               Alpine.js (deferred script in base.html)
//   static/fonts/ibm-plex-sans-arabic-*.woff2 IBM Plex Sans Arabic, arabic + latin subsets, 400/500/600
import { copyFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const modules = join(root, "node_modules");

const copies = [
  [join(modules, "alpinejs/dist/cdn.min.js"), join(root, "static/vendor/alpine.min.js")],
];

const fontDir = join(modules, "@fontsource/ibm-plex-sans-arabic/files");
for (const subset of ["arabic", "latin"]) {
  for (const weight of [400, 500, 600]) {
    const name = `ibm-plex-sans-arabic-${subset}-${weight}-normal.woff2`;
    copies.push([join(fontDir, name), join(root, "static/fonts", name)]);
  }
}

let failed = false;
for (const [from, to] of copies) {
  if (!existsSync(from)) {
    console.error(`missing ${from} — run npm install`);
    failed = true;
    continue;
  }
  mkdirSync(dirname(to), { recursive: true });
  copyFileSync(from, to);
  console.log(`vendored ${to.slice(root.length + 1)}`);
}
process.exit(failed ? 1 : 0);
