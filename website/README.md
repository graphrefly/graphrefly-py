# GraphReFly docs site (Astro + Starlight)

Same stack as **graphrefly-ts** (`website/`): Starlight, React islands, and a **Pyodide** lab page. This copy lives in the Python repo so the WASM / browser Python work tracks **graphrefly-py**.

## Commands

```bash
pnpm install
pnpm sync-docs    # copies ../docs/*.md into src/content/docs/ with frontmatter
pnpm dev          # predev runs sync-docs
pnpm build        # prebuild runs sync-docs
pnpm preview
```

## GitHub Pages

```bash
ASTRO_SITE_URL=https://your-org.github.io ASTRO_BASE_PATH=/graphrefly-py/ pnpm build
```

Deploy the `dist/` output (e.g. `peaceiris/actions-gh-pages` with `publish_dir: website/dist`).

The **graphrefly-ts** repo also keeps its own `website/` for the TypeScript project; the two sites are independent deployments.
