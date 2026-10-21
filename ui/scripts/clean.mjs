// The build's own files, and nothing else: static/.gitkeep is tracked and stays.
import { rmSync } from "node:fs";
import { resolve } from "node:path";
const out = resolve(import.meta.dirname, "../../core/lokki_core/static");
for (const name of ["index.html", "assets"]) rmSync(resolve(out, name), { recursive: true, force: true });
