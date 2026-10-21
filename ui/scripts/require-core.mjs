// `npm run dev` needs the core to proxy to, named explicitly: see vite.config.ts.
const target = process.env.LOKKI_CORE;
if (!target) {
  console.error("set LOKKI_CORE to the core's URL, e.g. LOKKI_CORE=http://127.0.0.1:8382 npm run dev");
  process.exit(2);
}

try {
  if (new URL(target).protocol !== "http:") throw new Error("LOKKI_CORE must use http:");
} catch {
  console.error("LOKKI_CORE must be a valid absolute http URL, e.g. http://127.0.0.1:8382");
  process.exit(2);
}
