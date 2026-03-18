"""
Orbis Multi-Language AST Parser
Supports: Python, JavaScript, TypeScript (+ TSX), Go, Rust, Java
Produces the NEO OUTPUT SPEC JSON consumed by the Orbis visualizer.
"""

import os
import re
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from tree_sitter import Language, Parser as TSParser, Node
import tree_sitter_python      as _tspy
import tree_sitter_javascript  as _tsjs
import tree_sitter_typescript  as _tsts
import tree_sitter_go          as _tsgo
import tree_sitter_rust        as _tsrs
import tree_sitter_java        as _tsjava

logger = logging.getLogger(__name__)

# ── Language objects ──────────────────────────────────────────────────────────
_LANGS: dict[str, Language] = {
    "python":     Language(_tspy.language()),
    "javascript": Language(_tsjs.language()),
    "typescript": Language(_tsts.language_typescript()),
    "tsx":        Language(_tsts.language_tsx()),
    "go":         Language(_tsgo.language()),
    "rust":       Language(_tsrs.language()),
    "java":       Language(_tsjava.language()),
}

EXT_LANG: dict[str, str] = {
    ".py":   "python",
    ".js":   "javascript",  ".mjs": "javascript",
    ".cjs":  "javascript",  ".jsx": "javascript",
    ".ts":   "typescript",  ".tsx": "tsx",
    ".go":   "go",
    ".rs":   "rust",
    ".java": "java",
}

# ── Skip dirs ─────────────────────────────────────────────────────────────────
SKIP_DIRS = {
    "venv", ".venv", "env", ".env", "__pycache__", ".git",
    "node_modules", "dist", "build", ".tox", "site-packages",
    ".eggs", ".mypy_cache", ".pytest_cache", "htmlcov",
    "vendor", "target",   # Go / Rust build dirs
    ".gradle", ".mvn",    # Java
    "docs", "doc", "examples", "example",
}

# ── Go stdlib top-level packages ──────────────────────────────────────────────
_GO_STDLIB = {
    "archive","bufio","builtin","bytes","compress","container","context",
    "crypto","database","debug","encoding","errors","expvar","flag","fmt",
    "go","hash","html","http","image","index","io","log","math","mime",
    "net","os","path","plugin","reflect","regexp","runtime","sort",
    "strconv","strings","sync","syscall","testing","text","time","unicode",
    "unsafe","internal",
}

# ── Java stdlib prefixes ──────────────────────────────────────────────────────
_JAVA_STDLIB_PFX = ("java.", "javax.", "sun.", "com.sun.", "org.w3c.", "org.xml.")

# ── Rust stdlib crates ────────────────────────────────────────────────────────
_RUST_STDLIB = {"std", "core", "alloc", "proc_macro", "test"}


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════

def _txt(node: Optional[Node]) -> str:
    if node is None:
        return ""
    return (node.text or b"").decode("utf-8", errors="replace")


def _count_lines(src: bytes) -> int:
    return src.count(b"\n") + (1 if src else 0)


def _module_id(filepath: str, root: str) -> str:
    """Stable module ID: path relative to repo root, no extension."""
    rel = os.path.relpath(filepath, root)
    parts = Path(rel).parts
    stem  = Path(rel).stem
    return str(Path(*parts[:-1], stem)) if len(parts) > 1 else stem


def _layer(filepath: str, root: str) -> str:
    rel   = os.path.relpath(filepath, root)
    parts = Path(rel).parts
    top   = parts[0] if parts else "root"
    if top in ("test", "tests", "__tests__", "spec", "specs", "test_*"):
        return "layer_tests"
    if any(p in ("test", "tests", "__tests__", "spec") for p in parts):
        return "layer_tests"
    if top in ("src", "lib", "pkg"):
        return f"layer_{parts[1]}" if len(parts) > 2 else "layer_src"
    if top in ("cmd", "internal", "api", "core", "domain",
               "service", "handler", "model", "middleware", "util",
               "utils", "config", "common", "shared"):
        return f"layer_{top}"
    return f"layer_{top}" if top != rel else "layer_root"


def _node_type_from_path(filepath: str, lang: str) -> str:
    name = Path(filepath).stem.lower()
    if any(t in name for t in ("test", "spec", "mock", "stub", "fixture")):
        return "test"
    if any(t in name for t in ("model", "schema", "entity", "domain", "dto")):
        return "model"
    if any(t in name for t in ("service", "svc")):
        return "service"
    if any(t in name for t in ("controller", "handler", "route", "view", "endpoint")):
        return "controller"
    if any(t in name for t in ("util", "helper", "common", "shared", "tools")):
        return "utility"
    if any(t in name for t in ("middleware", "interceptor", "filter", "hook")):
        return "middleware"
    if any(t in name for t in ("config", "setting", "const", "constant", "env")):
        return "config"
    # Go / Rust entry points
    if lang in ("go", "rust") and name in ("main", "lib", "mod"):
        return "module"
    return "module"


def _complexity(loc: int, fns: int, classes: int) -> str:
    score = loc + fns * 15 + classes * 40
    if score < 200:  return "low"
    if score < 600:  return "medium"
    if score < 1500: return "high"
    return "very high"


# ═════════════════════════════════════════════════════════════════════════════
# Language-specific extractors
# ═════════════════════════════════════════════════════════════════════════════

# ── Python ────────────────────────────────────────────────────────────────────

def _py_extract(tree_root: Node, filepath: str, repo_root: str,
                all_module_ids: set[str]) -> dict:
    """Parse Python imports and exported symbols."""
    internal, external, symbols = [], [], []
    funcs = classes = 0

    def resolve_relative(level: int, module_str: str) -> Optional[str]:
        """Turn relative import into an absolute module ID."""
        parts = Path(os.path.relpath(filepath, repo_root)).parts
        # go up 'level' directories
        base = list(parts[:-1])
        for _ in range(level - 1):
            if base:
                base.pop()
        if module_str:
            base += module_str.split(".")
        return "/".join(base) if base else None

    def visit(node: Node, depth: int = 0):
        nonlocal funcs, classes

        if node.type == "import_statement":
            for child in node.children:
                if child.type in ("dotted_name", "aliased_import"):
                    name_node = child.child_by_field_name("name") or child
                    name = _txt(name_node).split(".")[0]
                    _classify_py(name, internal, external, all_module_ids)

        elif node.type == "import_from_statement":
            # Determine the source module
            level = 0
            source_mod = ""
            for child in node.children:
                if child.type == "relative_import":
                    pfx = child.child_by_field_name("import_prefix")
                    level = len(_txt(pfx)) if pfx else 0
                    mod_node = child.child_by_field_name("dotted_name") or \
                               child.child_by_field_name("module_name")
                    source_mod = _txt(mod_node) if mod_node else ""
                elif child.type == "dotted_name" and source_mod == "" and level == 0:
                    source_mod = _txt(child)

            if level > 0:
                resolved = resolve_relative(level, source_mod)
                if resolved:
                    dep = resolved
                    if dep in all_module_ids:
                        if dep not in internal:
                            internal.append(dep)
                    else:
                        # try shorter path matches
                        matched = next((m for m in all_module_ids
                                        if m.endswith(dep) or dep.endswith(m)), None)
                        if matched and matched not in internal:
                            internal.append(matched)
            elif source_mod:
                _classify_py(source_mod.split(".")[0], internal, external, all_module_ids)

        elif node.type in ("function_definition", "decorated_definition") and depth <= 1:
            funcs += 1
            n = node.child_by_field_name("name")
            if n and not _txt(n).startswith("_"):
                symbols.append(_txt(n))

        elif node.type == "class_definition" and depth <= 1:
            classes += 1
            n = node.child_by_field_name("name")
            if n:
                symbols.append(_txt(n))

        for child in node.children:
            visit(child, depth + 1)

    visit(tree_root)
    return dict(internal=list(dict.fromkeys(internal)),
                external=list(dict.fromkeys(external)),
                symbols=symbols, funcs=funcs, classes=classes)


_PY_STDLIB = {
    "os","sys","re","json","math","random","logging","typing","dataclasses",
    "enum","datetime","pathlib","collections","itertools","functools","abc",
    "io","time","threading","subprocess","shutil","tempfile","hashlib",
    "base64","urllib","http","socket","struct","copy","inspect","importlib",
    "contextlib","warnings","traceback","unittest","argparse","configparser",
    "csv","sqlite3","statistics","string","textwrap","pprint","operator",
    "weakref","gc","platform","signal","queue","multiprocessing","concurrent",
    "asyncio","ssl","email","html","xml","zipfile","tarfile","gzip","bz2",
    "lzma","pickle","shelve","array","bisect","heapq","decimal","fractions",
    "uuid","secrets","hmac","codecs","locale","calendar","types","builtins",
    "ast","dis","tokenize","token","site","cmath","numbers","fnmatch",
    "glob","difflib","linecache","pdb","cProfile","profile","pstats",
    "doctest","unittest","io","abc","ctypes","curses","readline","rlcompleter",
    "sched","select","selectors","shlex","smtplib","ftplib","imaplib",
    "poplib","xmlrpc","wsgiref","webbrowser","turtle","tkinter","tty","pty",
    "mimetypes","mailbox","netrc","ipaddress","textwrap","unicodedata",
    "struct","codecs","encodings","keyword","py_compile","compileall",
}

def _classify_py(top: str, internal: list, external: list, all_ids: set[str]):
    if top in _PY_STDLIB:
        return
    # Check if any module ID starts with top
    matches = [m for m in all_ids if m.split("/")[0] == top or m == top]
    if matches:
        for m in matches:
            if m not in internal:
                internal.append(m)
    else:
        if top not in external:
            external.append(top)


# ── JavaScript / TypeScript ───────────────────────────────────────────────────

def _js_extract(tree_root: Node, filepath: str, repo_root: str,
                all_module_ids: set[str]) -> dict:
    internal, external, symbols = [], [], []
    funcs = classes = 0

    def resolve_js_path(source: str) -> Optional[str]:
        """Turn a relative JS import path into a module ID."""
        if not (source.startswith("./") or source.startswith("../")):
            return None
        base_dir = os.path.dirname(filepath)
        resolved = os.path.normpath(os.path.join(base_dir, source))
        # Strip extension if present
        resolved = re.sub(r'\.(js|jsx|ts|tsx|mjs|cjs)$', '', resolved)
        mid = _module_id(resolved, repo_root)
        return mid

    def visit(node: Node, depth: int = 0):
        nonlocal funcs, classes

        if node.type == "import_statement":
            src = node.child_by_field_name("source")
            if src:
                raw = _txt(src).strip("'\"` ")
                _classify_js(raw, filepath, repo_root, all_module_ids, internal, external)

        elif node.type == "export_statement":
            decl = node.children[1] if len(node.children) > 1 else None
            if decl:
                if decl.type in ("function_declaration", "generator_function_declaration"):
                    funcs += 1
                    n = decl.child_by_field_name("name")
                    if n: symbols.append(_txt(n))
                elif decl.type == "class_declaration":
                    classes += 1
                    n = decl.child_by_field_name("name")
                    if n: symbols.append(_txt(n))
                elif decl.type in ("lexical_declaration", "variable_declaration"):
                    for vd in decl.children:
                        if vd.type == "variable_declarator":
                            n = vd.child_by_field_name("name")
                            if n: symbols.append(_txt(n))
                elif decl.type in ("interface_declaration", "type_alias_declaration",
                                   "enum_declaration"):
                    n = decl.child_by_field_name("name")
                    if n: symbols.append(_txt(n))
            for child in node.children:
                visit(child, depth + 1)

        elif node.type in ("function_declaration", "method_definition",
                           "function", "arrow_function") and depth <= 2:
            funcs += 1

        elif node.type == "class_declaration" and depth <= 2:
            classes += 1

        # require() calls
        elif node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn and _txt(fn) == "require":
                args = node.child_by_field_name("arguments")
                if args:
                    for arg in args.children:
                        if arg.type == "string":
                            raw = _txt(arg).strip("'\"` ")
                            _classify_js(raw, filepath, repo_root,
                                         all_module_ids, internal, external)

        for child in node.children:
            visit(child, depth + 1)

    visit(tree_root)
    return dict(internal=list(dict.fromkeys(internal)),
                external=list(dict.fromkeys(external)),
                symbols=symbols, funcs=funcs, classes=classes)


def _classify_js(raw: str, filepath: str, repo_root: str, all_ids: set[str],
                 internal: list, external: list):
    if raw.startswith(("./", "../")):
        base_dir = os.path.dirname(filepath)
        resolved = os.path.normpath(os.path.join(base_dir, raw))
        resolved = re.sub(r'\.(js|jsx|ts|tsx|mjs|cjs)$', '', resolved)
        mid = _module_id(resolved, repo_root)
        # Try exact match or index file
        for candidate in (mid, mid + "/index"):
            if candidate in all_ids and candidate not in internal:
                internal.append(candidate)
                return
        if mid not in internal:
            internal.append(mid)
    elif raw and not raw.startswith(("node:", "bun:")):
        pkg = raw.split("/")[0].lstrip("@")
        if not raw.startswith("@"):
            pkg = raw.split("/")[0]
        else:
            parts = raw.split("/")
            pkg = parts[0] + "/" + parts[1] if len(parts) > 1 else parts[0]
        if pkg not in external:
            external.append(pkg)


# ── Go ────────────────────────────────────────────────────────────────────────

def _go_extract(tree_root: Node, filepath: str, repo_root: str,
                all_module_ids: set[str], go_module_name: str) -> dict:
    internal, external, symbols = [], [], []
    funcs = classes = 0

    def classify_go_import(path: str):
        clean = path.strip('"')
        top = clean.split("/")[0]
        if top in _GO_STDLIB:
            return  # stdlib, skip
        if go_module_name and clean.startswith(go_module_name):
            sub = clean[len(go_module_name):].lstrip("/")
            mid = sub if sub else _module_id(filepath, repo_root)
            if mid not in internal:
                internal.append(mid)
        else:
            pkg = clean.split("/")[-1]
            if pkg not in external:
                external.append(pkg)

    def visit(node: Node, depth: int = 0):
        nonlocal funcs, classes

        if node.type == "import_declaration":
            for child in node.children:
                if child.type == "import_spec":
                    path_node = child.child_by_field_name("path")
                    if path_node:
                        classify_go_import(_txt(path_node))
                elif child.type == "import_spec_list":
                    for spec in child.children:
                        if spec.type == "import_spec":
                            path_node = spec.child_by_field_name("path")
                            if path_node:
                                classify_go_import(_txt(path_node))

        elif node.type == "function_declaration":
            funcs += 1
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _txt(name_node)
                if name[0].isupper():  # exported
                    symbols.append(name)

        elif node.type in ("type_declaration",):
            for spec in node.children:
                if spec.type == "type_spec":
                    n = spec.child_by_field_name("name")
                    if n:
                        classes += 1
                        name = _txt(n)
                        if name[0].isupper():
                            symbols.append(name)

        elif node.type == "method_declaration":
            funcs += 1

        for child in node.children:
            visit(child, depth + 1)

    visit(tree_root)
    return dict(internal=list(dict.fromkeys(internal)),
                external=list(dict.fromkeys(external)),
                symbols=symbols, funcs=funcs, classes=classes)


def _detect_go_module(repo_root: str) -> str:
    """Read go.mod to find the module name."""
    go_mod = Path(repo_root) / "go.mod"
    if go_mod.exists():
        for line in go_mod.read_text().splitlines():
            m = re.match(r'^module\s+(\S+)', line)
            if m:
                return m.group(1)
    return ""


# ── Rust ──────────────────────────────────────────────────────────────────────

def _rust_extract(tree_root: Node, filepath: str, repo_root: str,
                  all_module_ids: set[str]) -> dict:
    internal, external, symbols = [], [], []
    funcs = classes = 0

    def use_path(node: Node) -> str:
        """Recursively extract the top-level crate/path from a use argument."""
        if node.type == "identifier":
            return _txt(node)
        if node.type in ("crate", "super", "self"):
            return _txt(node)
        path = node.child_by_field_name("path")
        if path:
            return use_path(path)
        return _txt(node).split("::")[0]

    def visit(node: Node, depth: int = 0):
        nonlocal funcs, classes

        if node.type == "use_declaration":
            arg = node.child_by_field_name("argument")
            if arg:
                top = use_path(arg).split("::")[0]
                if top in ("crate", "super", "self"):
                    if top not in internal:
                        internal.append(top)
                elif top in _RUST_STDLIB:
                    pass  # stdlib
                else:
                    if top not in external:
                        external.append(top)

        elif node.type == "function_item":
            funcs += 1
            vis = any(c.type == "visibility_modifier" for c in node.children)
            n = node.child_by_field_name("name")
            if n and vis:
                symbols.append(_txt(n))

        elif node.type in ("struct_item", "enum_item", "trait_item", "impl_item"):
            n = node.child_by_field_name("name")
            if n:
                classes += 1
                vis = any(c.type == "visibility_modifier" for c in node.children)
                if vis:
                    symbols.append(_txt(n))

        for child in node.children:
            visit(child, depth + 1)

    visit(tree_root)
    return dict(internal=list(dict.fromkeys(internal)),
                external=list(dict.fromkeys(external)),
                symbols=symbols, funcs=funcs, classes=classes)


# ── Java ──────────────────────────────────────────────────────────────────────

def _java_extract(tree_root: Node, filepath: str, repo_root: str,
                  all_module_ids: set[str], java_packages: set[str]) -> dict:
    internal, external, symbols = [], [], []
    funcs = classes = 0

    def full_name(node: Node) -> str:
        """Flatten a (possibly scoped) identifier into dotted string."""
        if node.type == "identifier":
            return _txt(node)
        name = node.child_by_field_name("name")
        path = node.child_by_field_name("scope") or node.child_by_field_name("path") \
               or (node.children[0] if node.children else None)
        if path and name:
            return full_name(path) + "." + _txt(name)
        return _txt(node)

    def visit(node: Node, depth: int = 0):
        nonlocal funcs, classes

        if node.type == "import_declaration":
            for child in node.children:
                if child.type == "scoped_identifier":
                    qname = full_name(child)
                    top2 = ".".join(qname.split(".")[:2])
                    if qname.startswith(_JAVA_STDLIB_PFX):
                        pass  # stdlib
                    elif any(qname.startswith(p) for p in java_packages):
                        # same project package
                        pkg_path = qname.replace(".", "/")
                        # find matching module
                        match = next((m for m in all_module_ids
                                      if m.endswith(qname.split(".")[-1])
                                      or pkg_path in m), None)
                        dep = match or pkg_path
                        if dep not in internal:
                            internal.append(dep)
                    else:
                        # top-level package as external
                        pkg = qname.split(".")[0]
                        if pkg not in external:
                            external.append(pkg)

        elif node.type in ("method_declaration", "constructor_declaration"):
            funcs += 1
            mods = node.children[0] if node.children else None
            if mods and mods.type == "modifiers":
                if any(_txt(c) == "public" for c in mods.children):
                    n = node.child_by_field_name("name")
                    if n:
                        symbols.append(_txt(n))

        elif node.type in ("class_declaration", "interface_declaration",
                           "enum_declaration", "annotation_type_declaration"):
            classes += 1
            n = node.child_by_field_name("name")
            if n:
                symbols.append(_txt(n))
            for child in node.children:
                visit(child, depth + 1)
            return

        for child in node.children:
            visit(child, depth + 1)

    visit(tree_root)
    return dict(internal=list(dict.fromkeys(internal)),
                external=list(dict.fromkeys(external)),
                symbols=symbols, funcs=funcs, classes=classes)


def _detect_java_packages(repo_root: str) -> set[str]:
    """Collect all package prefixes declared in the repo."""
    pkgs: set[str] = set()
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".java"):
                try:
                    src = Path(os.path.join(root, f)).read_text(errors="replace")
                    m = re.search(r'^\s*package\s+([\w.]+)\s*;', src, re.MULTILINE)
                    if m:
                        parts = m.group(1).split(".")
                        # Register top 2 components as a prefix
                        pkgs.add(".".join(parts[:2]))
                except Exception:
                    pass
    return pkgs


# ═════════════════════════════════════════════════════════════════════════════
# File collection
# ═════════════════════════════════════════════════════════════════════════════

def _collect_files(root: str) -> list[tuple[str, str]]:
    """Return list of (filepath, lang) for all supported source files."""
    result: list[tuple[str, str]] = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for f in sorted(files):
            ext = Path(f).suffix.lower()
            lang = EXT_LANG.get(ext)
            if lang:
                result.append((os.path.join(dirpath, f), lang))
    return result


# ═════════════════════════════════════════════════════════════════════════════
# Architecture & insight detection (language-agnostic)
# ═════════════════════════════════════════════════════════════════════════════

def _detect_architecture(nodes: list[dict], edges: list[dict]) -> str:
    layers = {n.get("layer", "") for n in nodes}
    node_types = {n.get("type", "") for n in nodes}

    has_controller = any("controller" in l or "handler" in l or "route" in l
                         or "view" in l for l in layers) or "controller" in node_types
    has_model = any("model" in l or "entity" in l or "domain" in l
                    for l in layers) or "model" in node_types
    has_service = any("service" in l or "svc" in l for l in layers) or "service" in node_types
    has_middleware = "middleware" in node_types or any("middleware" in l for l in layers)

    if has_controller and has_model and has_service:
        return "MVC"
    if has_middleware and has_service:
        return "Layered"
    if any("api" in l or "handler" in l for l in layers):
        return "REST API"
    if any("cmd" in l for l in layers):
        return "CLI"
    return "Modular"


def _detect_insights(nodes: list[dict], edges: list[dict]) -> list[dict]:
    insights: list[dict] = []
    adj:  dict[str, set[str]] = {n["id"]: set() for n in nodes}
    radj: dict[str, set[str]] = {n["id"]: set() for n in nodes}

    for e in edges:
        s, t = e.get("from", ""), e.get("to", "")
        if s in adj and t in adj:
            adj[s].add(t)
            radj[t].add(s)

    # ── High coupling ────────────────────────────────────────────────────────
    for n in nodes:
        out = len(adj.get(n["id"], set()))
        if out >= 6:
            sev = "critical" if out >= 9 else "high"
            insights.append({
                "type": "high_coupling",
                "severity": sev,
                "title": f"High Coupling: '{n['label']}' has {out} outgoing dependencies",
                "description": f"Module '{n['label']}' imports {out} other modules, making it "
                               "a high-coupling hub. Changes to any dependency will ripple here.",
                "affected_nodes": [n["id"]],
                "recommendation": "Consider the Facade or Mediator pattern to reduce direct "
                                  "coupling. Introduce an abstraction layer.",
            })

    # ── High fan-in ──────────────────────────────────────────────────────────
    for n in nodes:
        fan = len(radj.get(n["id"], set()))
        if fan >= 8:
            insights.append({
                "type": "high_fan_in",
                "severity": "info",
                "title": f"Core Module: '{n['label']}' is imported by {fan} modules",
                "description": f"'{n['label']}' has the highest in-degree ({fan}), "
                               "making it a central dependency.",
                "affected_nodes": [n["id"]],
                "recommendation": "Ensure this module has a stable, well-documented API "
                                  "to minimize downstream breakage.",
            })

    # ── Circular deps ────────────────────────────────────────────────────────
    visited: set[str] = set()

    def find_cycle(start: str) -> Optional[list[str]]:
        stack, path = [start], [start]
        seen: set[str] = {start}
        while stack:
            node_id = stack[-1]
            moved = False
            for nb in adj.get(node_id, set()):
                if nb == start and len(path) > 1:
                    return path + [start]
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
                    path.append(nb)
                    moved = True
                    break
            if not moved:
                stack.pop()
                path.pop()
        return None

    reported: set[str] = set()
    for n in nodes:
        if n["id"] in visited:
            continue
        cycle = find_cycle(n["id"])
        if cycle:
            key = frozenset(cycle)
            if key not in reported:
                reported.add(key)
                chain = " → ".join(cycle)
                insights.append({
                    "type": "circular_dependency",
                    "severity": "critical",
                    "title": f"Circular Dependency: {chain}",
                    "description": f"A circular import chain exists: {chain}. "
                                   "This can cause import errors at runtime and makes "
                                   "the codebase hard to test in isolation.",
                    "affected_nodes": list(dict.fromkeys(cycle)),
                    "recommendation": "Extract shared types/interfaces into a separate module. "
                                      "Break the cycle at its weakest link.",
                })
        visited.add(n["id"])

    # ── Isolated modules ─────────────────────────────────────────────────────
    connected = {e["from"] for e in edges} | {e["to"] for e in edges}
    isolated = [n for n in nodes if n["id"] not in connected]
    if isolated:
        names = ", ".join(n["label"] for n in isolated[:5])
        ellipsis = "…" if len(isolated) > 5 else ""
        insights.append({
            "type": "isolated_module",
            "severity": "medium",
            "title": f"Isolated Modules ({len(isolated)}): {names}{ellipsis}",
            "description": "These modules have no detected import relationships: "
                           + ", ".join(n["id"] for n in isolated) + ".",
            "affected_nodes": [n["id"] for n in isolated],
            "recommendation": "Verify these are intentionally standalone or remove dead code.",
        })

    # ── Large modules ────────────────────────────────────────────────────────
    for n in nodes:
        loc = n.get("lines_of_code", 0)
        if loc > 500:
            insights.append({
                "type": "large_module",
                "severity": "medium",
                "title": f"Large Module: {n['label']} ({loc:,} lines)",
                "description": f"'{n['label']}' has {loc:,} lines of code, exceeding the "
                               "500-line threshold. Large files often bundle multiple responsibilities.",
                "affected_nodes": [n["id"]],
                "recommendation": "Consider splitting into smaller, single-responsibility modules.",
            })

    return insights


def _build_summary(nodes: list[dict], edges: list[dict],
                   arch: str, lang_counts: dict[str, int]) -> str:
    total_loc = sum(n.get("lines_of_code", 0) for n in nodes)
    lang_str  = ", ".join(f"{n} ({c} files)" for n, c in
                          sorted(lang_counts.items(), key=lambda x: -x[1]))
    circular  = sum(1 for e in edges if e.get("type") == "circular")
    high_coup = [n["label"] for n in nodes if len([e for e in edges if e["from"] == n["id"]]) >= 6]

    parts = [
        f"Codebase contains {len(nodes)} modules totalling {total_loc:,} lines of code.",
        f"Languages detected: {lang_str}.",
        f"Architecture pattern: {arch}.",
        f"{len(edges)} dependency edges detected across {len(nodes)} nodes.",
    ]
    if circular:
        parts.append(f"⚠️ {circular} circular dependencies detected — see insights.")
    if high_coup:
        parts.append(f"High-coupling nodes: {', '.join(high_coup[:3])}.")
    return " ".join(parts)


# ═════════════════════════════════════════════════════════════════════════════
# Main entry point
# ═════════════════════════════════════════════════════════════════════════════

def analyze_codebase(root_path: str) -> dict[str, Any]:
    root    = str(Path(root_path).resolve())
    files   = _collect_files(root)

    if not files:
        raise ValueError(
            "No supported source files found. "
            "Orbis supports Python, JavaScript, TypeScript, Go, Rust, and Java."
        )

    # Count files per language
    lang_counts: dict[str, int] = {}
    for _, lang in files:
        lang_counts[lang] = lang_counts.get(lang, 0) + 1

    # Pre-compute: all module IDs, Go module name, Java packages
    all_module_ids = {_module_id(fp, root) for fp, _ in files}
    go_module      = _detect_go_module(root) if "go" in lang_counts else ""
    java_packages  = _detect_java_packages(root) if "java" in lang_counts else set()

    # Build per-language parsers
    parsers: dict[str, TSParser] = {lang: TSParser(_LANGS[lang]) for lang in set(l for _, l in files)}

    nodes: list[dict] = []
    edges: list[dict] = []

    for filepath, lang in files:
        mid   = _module_id(filepath, root)
        label = Path(filepath).name
        layer = _layer(filepath, root)
        ntype = _node_type_from_path(filepath, lang)

        try:
            src = Path(filepath).read_bytes()
        except Exception:
            continue

        loc  = _count_lines(src)
        tree = parsers[lang].parse(src)
        root_node = tree.root_node

        try:
            if lang == "python":
                info = _py_extract(root_node, filepath, root, all_module_ids)
            elif lang in ("javascript", "typescript", "tsx"):
                info = _js_extract(root_node, filepath, root, all_module_ids)
            elif lang == "go":
                info = _go_extract(root_node, filepath, root, all_module_ids, go_module)
            elif lang == "rust":
                info = _rust_extract(root_node, filepath, root, all_module_ids)
            elif lang == "java":
                info = _java_extract(root_node, filepath, root, all_module_ids, java_packages)
            else:
                info = dict(internal=[], external=[], symbols=[], funcs=0, classes=0)
        except Exception as exc:
            logger.debug("Extraction error %s: %s", filepath, exc)
            info = dict(internal=[], external=[], symbols=[], funcs=0, classes=0)

        cx = _complexity(loc, info["funcs"], info["classes"])

        nodes.append({
            "id":                   mid,
            "label":                label,
            "type":                 ntype,
            "path":                 filepath,
            "layer":                layer,
            "language":             lang,
            "lines_of_code":        loc,
            "complexity":           cx,
            "exported_symbols":     info["symbols"],
            "internal_dependencies":info["internal"],
            "external_dependencies":info["external"],
            "metrics": {
                "lines_of_code":    loc,
                "classes":          info["classes"],
                "functions_total":  info["funcs"],
                "stdlib_imports":   0,
            },
        })

        for dep in info["internal"]:
            edges.append({"from": mid, "to": dep, "type": "import",
                          "label": f"imports {dep.split('/')[-1]}"})

    # Mark circular edges
    adj: dict[str, set[str]] = {}
    for e in edges:
        adj.setdefault(e["from"], set()).add(e["to"])

    circular_pairs: set[tuple[str, str]] = set()
    for e in edges:
        if e["to"] in adj.get(e["from"], set()) and e["from"] in adj.get(e["to"], set()):
            circular_pairs.add((min(e["from"], e["to"]), max(e["from"], e["to"])))

    for e in edges:
        pair = (min(e["from"], e["to"]), max(e["from"], e["to"]))
        if pair in circular_pairs:
            e["type"] = "circular"

    arch     = _detect_architecture(nodes, edges)
    insights = _detect_insights(nodes, edges)
    summary  = _build_summary(nodes, edges, arch, lang_counts)

    # Dependency graph stats
    node_ids    = {n["id"] for n in nodes}
    connected   = {e["from"] for e in edges} | {e["to"] for e in edges}
    isolated    = [n["id"] for n in nodes if n["id"] not in connected]
    high_coup_n = sorted(
        [{"node": n["id"], "out_degree": len(adj.get(n["id"], set()))} for n in nodes],
        key=lambda x: -x["out_degree"]
    )[:5]

    return {
        "schema_version":    "2.0",
        "generated_at":      datetime.now(timezone.utc).isoformat(),
        "codebase_root":     root,
        "architecture_type": arch,
        "languages":         lang_counts,
        "summary":           summary,
        "nodes":             nodes,
        "edges":             edges,
        "insights":          insights,
        "dependency_graph_stats": {
            "total_nodes":       len(nodes),
            "total_edges":       len(edges),
            "dag_verified":      len(circular_pairs) == 0,
            "isolated_nodes":    isolated,
            "high_coupling_nodes": high_coup_n,
        },
    }
