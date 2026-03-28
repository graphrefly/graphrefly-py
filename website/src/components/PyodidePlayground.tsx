import { loadPyodide } from "pyodide";
import { useCallback, useState } from "react";

const INDEX_URL = "https://cdn.jsdelivr.net/pyodide/v0.27.7/full/";

const SAMPLE = `
# Toy CQRS-style trace (stdlib only)
commands: list[str] = []

def handle(cmd: str) -> None:
    commands.append(cmd)

handle("open_session")
handle("submit_query")
{"commands": commands, "n": len(commands)}
`.trim();

export default function PyodidePlayground() {
	const [output, setOutput] = useState("");
	const [busy, setBusy] = useState(false);

	const run = useCallback(async () => {
		setBusy(true);
		setOutput("Loading Pyodide…");
		try {
			const pyodide = await loadPyodide({ indexURL: INDEX_URL });
			setOutput("Running…");
			const result = await pyodide.runPythonAsync(SAMPLE);
			setOutput(String(result));
		} catch (e) {
			setOutput(e instanceof Error ? e.message : String(e));
		} finally {
			setBusy(false);
		}
	}, []);

	return (
		<div className="gr-pyodide">
			<p className="gr-pyodide-lead">
				Loads the Pyodide runtime from jsDelivr (first run downloads WASM; works on static GitHub
				Pages).
			</p>
			<button type="button" className="gr-pyodide-btn" onClick={run} disabled={busy}>
				{busy ? "Running…" : "Run sample"}
			</button>
			<pre className="gr-pyodide-out">{output || "—"}</pre>
		</div>
	);
}
