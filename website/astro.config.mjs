import react from "@astrojs/react";
import starlight from "@astrojs/starlight";
import { defineConfig } from "astro/config";

import { pyApiSidebar } from "./py-api-sidebar.mjs";

/** GitHub Project Pages: set to `/repo-name/` (trailing slash). Root site: `'/'`. */
// Served under /py/ on graphrefly.dev via Cloudflare Worker proxy.
// GitHub Pages URL: graphrefly.github.io/graphrefly-py/py/...
const base = process.env.ASTRO_BASE_PATH ?? "/py/";

export default defineConfig({
	site: process.env.ASTRO_SITE_URL ?? "https://example.invalid",
	base,
	vite: {
		ssr: { noExternal: ["pyodide"] },
		optimizeDeps: { exclude: ["pyodide"] },
	},
	integrations: [
		starlight({
			title: "GraphReFly",
			description: "Reactive harness layer for agent workflows. Describe automations in plain language, trace every decision, enforce policies, persist checkpoints. Zero dependencies.",
			components: {
				Header: "./src/components/Header.astro",
				MobileMenuFooter: "./src/components/MobileMenuFooter.astro",
				Sidebar: "./src/components/Sidebar.astro",
				SiteTitle: "./src/components/SiteTitle.astro",
			},
			customCss: ["./src/styles/custom.css"],
			head: [
				{ tag: "link", attrs: { rel: "preconnect", href: "https://fonts.googleapis.com" } },
				{
					tag: "link",
					attrs: { rel: "preconnect", href: "https://fonts.gstatic.com", crossorigin: "" },
				},
			],
			social: [
				{
					icon: "github",
					label: "graphrefly-py",
					href: "https://github.com/graphrefly/graphrefly-py",
				},
			],
			sidebar: [
				{
					label: "Protocol",
					items: [{ label: "Specification", link: "/spec" }],
				},
				...pyApiSidebar,
				{
					label: "Labs",
					items: [{ label: "Python (Pyodide)", link: "/lab/python" }],
				},
			],
		}),
		react(),
	],
});
