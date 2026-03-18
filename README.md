# Orbis — Repository Architecture Visualizer

> 🤖 This project was autonomously built using **[NEO](https://heyneo.so)** — your autonomous AI Agent.

**Orbis turns any GitHub repository into an interactive 3D dependency graph in seconds.**

Paste a repo URL → Orbis clones it, parses the ASTs, detects architecture patterns, and renders the entire codebase as a navigable 3D graph. Click any module to inspect its dependencies, metrics, and exported symbols. Ask the built-in AI assistant questions like *"which module should I refactor first?"* or *"why are there circular dependencies here?"* and get answers grounded in the actual code structure.

## Features

- **3D force-directed graph** — nodes sized by lines of code, colored by type, with animated directional particles on edges
- **Multi-language AST parsing** — Python, JavaScript/TypeScript, Go, Rust, and Java via tree-sitter
- **AI chat assistant** — ask Claude questions about the analyzed codebase ("Which modules have circular dependencies?", "Where should I add feature X?")
- **Architectural insights** — auto-detected issues (god modules, high coupling, circular deps) with severity ratings
- **Focus Mode** — dim unconnected nodes to trace dependency paths
- **Shareable URLs** — `?repo=https://github.com/...` auto-triggers analysis on load
- **Recent history** — last 5 repos stored locally for quick re-analysis
- **Demo mode** — load a pre-analyzed snapshot without a GitHub clone

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | FastAPI + Server-Sent Events (SSE) |
| AST Parsing | tree-sitter (Python, JS/TS, Go, Rust, Java) |
| AI | Claude Opus 4.6 via Anthropic API |
| 3D Graph | [3d-force-graph](https://github.com/vasturiano/3d-force-graph) + Three.js |
| Frontend | Vanilla JS SPA — no build step |

## Quick Start

### 1. Clone & install

```bash
#clone this repo 
cd orbis
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Set up environment

```bash
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY for the AI chat feature
```

Get an API key at [console.anthropic.com](https://console.anthropic.com).

### 3. Run

```bash
uvicorn main:app --host 0.0.0.0 --port 8001
```

Open [http://localhost:8001](http://localhost:8001).

### Docker

```bash
docker build -t orbis .
docker run -p 8001:8001 -e ANTHROPIC_API_KEY=sk-ant-... orbis
```

## Usage

1. Enter a public GitHub repository URL (e.g. `https://github.com/expressjs/express`)
2. Optionally specify a branch
3. Click **Analyze** — Orbis clones the repo, parses ASTs, and builds the graph (~5–30s)
4. Explore the 3D graph:
   - **Click** a node to open its detail drawer
   - **Scroll** to zoom, **drag** to rotate
   - Use **Focus Mode** to highlight a node's direct connections
   - Use **layer filter chips** to show/hide architectural layers
5. Ask the **AI assistant** questions about the codebase in the chat panel

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `R` | Reset camera |
| `P` | Pause/resume rotation |
| `F` | Toggle Focus Mode |
| `/` | Focus search box |
| `Esc` | Close drawer / exit Focus Mode |

## Architecture

```
main.py           FastAPI backend — SSE streaming for /analyze, /chat
neo_parser.py     Multi-language AST parser (tree-sitter)
static/
  index.html      Single-page frontend (3d-force-graph + Three.js)
save_analysis.py  Utility: pre-generate demo data from a repo
```

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Frontend SPA |
| `POST` | `/analyze` | Stream SSE events while cloning + analyzing a repo |
| `POST` | `/chat` | Stream AI answers about an analyzed codebase |
| `GET` | `/demo` | Return pre-built demo analysis (psf/requests) |
| `GET` | `/health` | Health check |

### Output Schema

`/analyze` emits SSE events and completes with a `complete` event containing:

```json
{
  "schema_version": "2.0",
  "architecture_type": "MVC",
  "languages": { "python": 42 },
  "summary": "Codebase contains 42 modules...",
  "nodes": [{
    "id": "requests/auth",
    "label": "auth.py",
    "type": "utility",
    "language": "python",
    "lines_of_code": 315,
    "complexity": "medium",
    "exported_symbols": ["AuthBase", "HTTPBasicAuth"],
    "internal_dependencies": ["requests/compat"],
    "external_dependencies": [],
    "metrics": { "functions_total": 12, "classes": 4 }
  }],
  "edges": [{ "from": "requests/api", "to": "requests/auth", "type": "import" }],
  "insights": [{
    "type": "high_coupling",
    "severity": "high",
    "title": "High fan-in on requests/models",
    "description": "14 modules import this file directly.",
    "affected_nodes": ["requests/models"],
    "recommendation": "Consider splitting into smaller focused modules."
  }]
}
```

## Supported Languages

| Language | File Extensions |
|----------|----------------|
| Python | `.py` |
| JavaScript | `.js`, `.mjs`, `.cjs`, `.jsx` |
| TypeScript | `.ts`, `.tsx` |
| Go | `.go` |
| Rust | `.rs` |
| Java | `.java` |

## AI Chat

The chat assistant uses **Claude Opus 4.6** and receives the full architectural graph as context — node list, dependencies, insights, and summary. It can answer questions like:

- "What does the `auth` module depend on?"
- "Why are there circular dependencies between X and Y?"
- "Which module should I refactor first?"
- "Where would I add a caching layer?"

Requires `ANTHROPIC_API_KEY` in your environment. The feature gracefully degrades (shows an error message) if the key is missing.

## Development

```bash
# Run with auto-reload
uvicorn main:app --reload --port 8001

# Re-generate demo data
python save_analysis.py
```

## License

MIT — see [LICENSE](LICENSE).
