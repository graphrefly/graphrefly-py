#!/usr/bin/env node

import { writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const websiteRoot = path.resolve(__dirname, "..");
const distRoot = path.join(websiteRoot, "dist");
const customDomain = process.env.PY_DOCS_CUSTOM_DOMAIN ?? "py.graphrefly.dev";

if (customDomain.trim().length > 0) {
	writeFileSync(path.join(distRoot, "CNAME"), `${customDomain.trim()}\n`);
}

writeFileSync(
	path.join(distRoot, "artifact-manifest.json"),
	`${JSON.stringify(
		{
			package: "graphrefly",
			framework: "astro-starlight",
			route: process.env.ASTRO_BASE_PATH ?? "/",
			source: "website/src/content/docs",
			apiGenerator: "griffe",
		},
		null,
		2,
	)}\n`,
);

console.log("prepared Starlight graphrefly Python docs artifact");
