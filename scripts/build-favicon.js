// Rasterizes web/app/icon.svg into web/app/favicon.ico.
//
// Browsers request /favicon.ico unconditionally, so shipping only an SVG icon
// leaves a 404 in the network log. Next serves app/favicon.ico at that path.
//
// sharp renders the SVG but cannot write ICO, so we wrap the PNG frames in an
// ICO container by hand — ICO has allowed embedded PNG since Vista and every
// browser we care about reads it.
//
// Run from the repo root:  node scripts/build-favicon.js

const fs = require("fs");
const path = require("path");
const sharp = require(path.join(__dirname, "..", "web", "node_modules", "sharp"));

const APP = path.join(__dirname, "..", "web", "app");
const SRC = path.join(APP, "icon.svg");
const OUT = path.join(APP, "favicon.ico");

// At 16px the 2px stroke of the full-size mark renders under a pixel wide and
// turns to mush, so the smallest frame gets a chunkier, tighter-cropped variant.
const SMALL_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#7c5cff" />
      <stop offset="1" stop-color="#5234c7" />
    </linearGradient>
  </defs>
  <rect width="32" height="32" rx="6" fill="url(#g)" />
  <g transform="translate(3 3) scale(1.0833)"
     fill="none" stroke="#fff" stroke-width="2.6"
     stroke-linecap="round" stroke-linejoin="round">
    <path d="M12 2 2 7l10 5 10-5-10-5Z" />
    <path d="m2 17 10 5 10-5" />
    <path d="m2 12 10 5 10-5" />
  </g>
</svg>`;

const SIZES = [16, 32, 48];

async function main() {
  const full = fs.readFileSync(SRC);

  const frames = await Promise.all(
    SIZES.map((size) =>
      sharp(size === 16 ? Buffer.from(SMALL_SVG) : full, { density: 384 })
        .resize(size, size)
        .png({ compressionLevel: 9 })
        .toBuffer()
        .then((data) => ({ size, data }))
    )
  );

  // ICONDIR: reserved(2) + type(2, 1=icon) + image count(2).
  const header = Buffer.alloc(6);
  header.writeUInt16LE(0, 0);
  header.writeUInt16LE(1, 2);
  header.writeUInt16LE(frames.length, 4);

  const ENTRY = 16;
  let offset = header.length + ENTRY * frames.length;

  const entries = frames.map(({ size, data }) => {
    const e = Buffer.alloc(ENTRY);
    e.writeUInt8(size, 0); // width  (0 would mean 256)
    e.writeUInt8(size, 1); // height
    e.writeUInt8(0, 2); // palette size, 0 = truecolour
    e.writeUInt8(0, 3); // reserved
    e.writeUInt16LE(1, 4); // colour planes
    e.writeUInt16LE(32, 6); // bits per pixel
    e.writeUInt32LE(data.length, 8);
    e.writeUInt32LE(offset, 12);
    offset += data.length;
    return e;
  });

  fs.writeFileSync(
    OUT,
    Buffer.concat([header, ...entries, ...frames.map((f) => f.data)])
  );

  console.log(
    `wrote ${path.relative(process.cwd(), OUT)} ` +
      `(${SIZES.join("/")}px, ${fs.statSync(OUT).size} bytes)`
  );
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
