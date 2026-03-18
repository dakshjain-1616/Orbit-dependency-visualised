"""
Orbis — Repository Architecture Visualizer
Streams SSE progress events while cloning + analysing a GitHub repo.
"""

import json
import os
import shutil
import logging
import tempfile
import asyncio
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
import git
from anthropic import AsyncAnthropic

from neo_parser import analyze_codebase

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("neo.api")

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Orbis",
    description="3D architectural dependency visualizer for GitHub repositories",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
DEMO_FILE  = Path(__file__).parent / "architecture_analysis.json"


# ── Helpers ───────────────────────────────────────────────────────────────────
def _sse(event_type: str, **kwargs) -> str:
    """Format a single Server-Sent Event line."""
    return f"data: {json.dumps({'type': event_type, **kwargs})}\n\n"


# ── Request model ─────────────────────────────────────────────────────────────
class AnalyzeRequest(BaseModel):
    repo_url: str
    branch: Optional[str] = None

    @field_validator("repo_url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if not (v.startswith("https://github.com/") or v.startswith("git@github.com:")):
            raise ValueError("Only GitHub URLs are supported (https://github.com/…)")
        return v


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def serve_index():
    index = STATIC_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="Frontend not built")
    return FileResponse(str(index))


@app.post("/analyze")
async def analyze_repo(req: AnalyzeRequest):
    """
    Stream SSE events while cloning + analysing the repository.

    Event types emitted:
      { "type": "progress", "step": "…", "pct": 0-100 }
      { "type": "complete", "data": { …NEO spec… } }
      { "type": "error",    "message": "…" }
    """
    async def generate():
        tmp_dir: Optional[str] = None
        try:
            # ── Clone ──────────────────────────────────────────────────────
            yield _sse("progress", step="Connecting to GitHub…", pct=5)

            tmp_dir = tempfile.mkdtemp(prefix="neo_clone_")
            clone_kwargs: dict = {"to_path": tmp_dir, "depth": 1, "single_branch": True}
            if req.branch:
                clone_kwargs["branch"] = req.branch

            loop = asyncio.get_event_loop()
            logger.info("Cloning %s …", req.repo_url)

            try:
                await loop.run_in_executor(
                    None,
                    lambda: git.Repo.clone_from(req.repo_url, **clone_kwargs),
                )
            except git.exc.GitCommandError as exc:
                err = exc.stderr.strip() if exc.stderr else str(exc)
                yield _sse("error", message=f"Clone failed — {err}. Check the URL is correct and the repo is public.")
                return

            logger.info("Clone complete → %s", tmp_dir)
            yield _sse("progress", step="Parsing AST & building symbol table…", pct=38)

            # ── Analyse ────────────────────────────────────────────────────
            try:
                result = await loop.run_in_executor(
                    None,
                    lambda: analyze_codebase(tmp_dir),
                )
            except ValueError as exc:
                msg = str(exc)
                if "No supported" in msg or "No Python files" in msg:
                    yield _sse("error", message="No supported source files found. Orbis supports Python, JavaScript/TypeScript, Go, Rust, and Java repositories.")
                else:
                    yield _sse("error", message=f"Analysis failed: {msg}")
                return
            except Exception as exc:
                logger.exception("analyze_codebase failed")
                yield _sse("error", message=f"Analysis failed: {exc}")
                return

            yield _sse("progress", step="Computing architectural insights…", pct=85)
            await asyncio.sleep(0.05)
            yield _sse("progress", step="Serialising graph data…", pct=96)
            await asyncio.sleep(0.05)

            logger.info(
                "Analysis complete: %d nodes, %d edges",
                len(result.get("nodes", [])),
                len(result.get("edges", [])),
            )
            yield _sse("complete", data=result)

        except Exception as exc:
            logger.exception("Unexpected error in SSE generator")
            yield _sse("error", message=str(exc))
        finally:
            if tmp_dir and os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)
                logger.info("Cleaned up %s", tmp_dir)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/demo")
async def demo():
    """Return the built-in demo analysis (requests library)."""
    if not DEMO_FILE.exists():
        raise HTTPException(status_code=404, detail="Demo data not available on this server")
    return JSONResponse(content=json.loads(DEMO_FILE.read_text()))


@app.get("/health")
async def health():
    return {"status": "ok", "service": "Orbis", "version": "2.0.0"}


# ── AI Chat ───────────────────────────────────────────────────────────────────

_anthropic: Optional[AsyncAnthropic] = None

def _get_anthropic() -> AsyncAnthropic:
    global _anthropic
    if _anthropic is None:
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise ValueError("ANTHROPIC_API_KEY environment variable is not set")
        _anthropic = AsyncAnthropic(api_key=key)
    return _anthropic


def _build_chat_system(ctx: dict) -> str:
    """Compress graph data into a concise system prompt for Claude."""
    nodes   = ctx.get("nodes", [])
    edges   = ctx.get("edges", [])
    insights = ctx.get("insights", [])

    # Nodes — sorted by LOC, up to 80
    node_lines = []
    for n in sorted(nodes, key=lambda x: x.get("lines_of_code", 0), reverse=True)[:80]:
        deps = ", ".join((n.get("internal_dependencies") or [])[:4])
        ext  = ", ".join((n.get("external_dependencies") or [])[:3])
        line = (
            f"  {n['id']} [{n.get('language','?')}/{n.get('type','module')}]"
            f" {n.get('lines_of_code',0)} LOC"
            + (f" → {deps}" if deps else "")
            + (f" (ext: {ext})" if ext else "")
        )
        node_lines.append(line)

    # Edges — up to 120, flag circulars
    edge_lines = []
    for e in (edges or [])[:120]:
        suffix = " ⚠️circular" if e.get("type") == "circular" else ""
        edge_lines.append(f"  {e.get('source',e.get('from','?'))} → {e.get('target',e.get('to','?'))}{suffix}")

    # Insights
    ins_lines = [
        f"  [{i.get('severity','?').upper()}] {i.get('title','')}: {i.get('description','')}"
        for i in (insights or [])
    ]

    langs = ctx.get("languages", {})
    lang_str = ", ".join(f"{k} ({v} files)" for k, v in langs.items()) or "unknown"

    return (
        "You are an expert software architect assistant. "
        "A GitHub repository has been analyzed into a dependency graph. "
        "Answer questions about its structure, design, and quality concisely and helpfully. "
        "Reference specific module names from the data when relevant. "
        "Keep answers focused and practical.\n\n"
        f"Architecture: {ctx.get('architecture_type', 'unknown')}\n"
        f"Languages: {lang_str}\n"
        f"Summary: {ctx.get('summary', '')}\n\n"
        f"Modules ({len(nodes)} total, top by LOC):\n"
        + ("\n".join(node_lines) or "  (none)") + "\n\n"
        f"Dependencies ({len(edges)} total):\n"
        + ("\n".join(edge_lines) or "  (none)") + "\n\n"
        "Insights:\n"
        + ("\n".join(ins_lines) or "  (none)")
    )


class ChatRequest(BaseModel):
    question: str
    context: dict[str, Any]


@app.post("/chat")
async def chat(req: ChatRequest):
    """Stream an AI answer about the analyzed codebase via SSE."""
    async def generate():
        try:
            client = _get_anthropic()
        except ValueError as exc:
            yield _sse("error", message=str(exc))
            return

        system = _build_chat_system(req.context)
        try:
            async with client.messages.stream(
                model="claude-opus-4-6",
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": req.question}],
            ) as stream:
                async for text in stream.text_stream:
                    yield _sse("token", text=text)
            yield _sse("done")
        except Exception as exc:
            logger.exception("Chat error")
            yield _sse("error", message=str(exc))

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# ── Static assets (CSS, fonts, etc. if any) ───────────────────────────────────
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=False)
