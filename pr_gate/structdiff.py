
"""
structdiff.py: diff estructural multilenguaje entre dos revisiones de git.

Construye los árboles con tree-sitter (https://tree-sitter.github.io), el
parser que usan GitHub, Neovim, Zed, difftastic o Semgrep. No usa LLM:
produce hechos estructurales (símbolos añadidos, eliminados, renombrados,
movidos, cambios de firma, de llamadas, de decoradores, etc.) y marca qué
cambios merecen una llamada al LLM para explicar la motivación.

Lenguajes: Python, JavaScript/JSX, TypeScript/TSX, Go y Java. Añadir otro
es agregar una entrada a SPECS (ver el final de la sección de lenguajes).

Uso, desde el CLI:
    jcton structdiff main HEAD
    jcton structdiff main HEAD --json
    jcton structdiff main HEAD --llm-context
    jcton structdiff --files viejo.ts nuevo.ts
"""
from __future__ import annotations

import difflib
import functools
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from tree_sitter_language_pack import get_parser

# Cambios de cuerpo con más líneas que esto se marcan para el LLM.
LLM_LINES_THRESHOLD = 15
# Cuerpos más cortos no se usan para detectar renombres o movimientos
# (evita falsos positivos con funciones triviales).
MIN_RENAME_LINES = 3

KIND_ES = {
    "function": "función",
    "async function": "función async",
    "method": "método",
    "async method": "método async",
    "constructor": "constructor",
    "class": "clase",
    "interface": "interfaz",
    "type": "tipo",
    "constant": "constante",
    "attribute": "atributo",
}

IDENT = re.compile(r"[A-Za-z_$][\w$]*")
COMMENT_TYPES = {"comment", "line_comment", "block_comment"}


# ------------------------------------------------------------- lenguajes

@dataclass(frozen=True)
class Spec:
    """Qué nodos de la gramática de tree-sitter significan qué cosa."""
    functions: frozenset = frozenset()       # definen funciones o métodos
    classes: frozenset = frozenset()         # contenedores con miembros
    values: dict = field(default_factory=dict)  # nodo -> tipo de declarador hijo (None = el propio nodo)
    types: dict = field(default_factory=dict)   # alias, enums, structs: igual formato que values
    imports: frozenset = frozenset()
    import_items: frozenset = frozenset()    # Go: cada import_spec de un bloque import (...) por separado
    calls: dict = field(default_factory=dict)   # nodo de llamada -> campos que forman el destino
    wrappers: frozenset = frozenset()        # se atraviesan sin crear símbolo (export, decorated, if...)
    decorators: frozenset = frozenset()      # decoradores o anotaciones
    function_values: frozenset = frozenset() # `const f = () => ...` cuenta como función
    return_fields: tuple = ()
    base_fields: tuple = ()                  # campos o tipos de hijo con la herencia
    receiver_field: str | None = None        # Go: func (t *T) M()
    py_docstring: bool = False


_JS = dict(
    functions=frozenset({"function_declaration", "generator_function_declaration", "method_definition"}),
    classes=frozenset({"class_declaration"}),
    values={"lexical_declaration": "variable_declarator",
            "variable_declaration": "variable_declarator",
            "field_definition": None},
    imports=frozenset({"import_statement"}),
    calls={"call_expression": ("function",), "new_expression": ("constructor",)},
    wrappers=frozenset({"export_statement"}),
    decorators=frozenset({"decorator"}),
    function_values=frozenset({"arrow_function", "function_expression", "function", "generator_function"}),
    return_fields=("return_type",),
    base_fields=("class_heritage",),
)
_TS = dict(
    _JS,
    functions=_JS["functions"] | {"method_signature", "abstract_method_signature", "function_signature"},
    classes=_JS["classes"] | {"abstract_class_declaration", "interface_declaration"},
    values={**_JS["values"], "public_field_definition": None, "property_signature": None},
    types={"type_alias_declaration": None, "enum_declaration": None},
    wrappers=_JS["wrappers"] | {"ambient_declaration"},
    base_fields=("class_heritage", "extends_type_clause"),
)

SPECS = {
    "python": Spec(
        functions=frozenset({"function_definition"}),
        classes=frozenset({"class_definition"}),
        values={"assignment": None},
        imports=frozenset({"import_statement", "import_from_statement", "future_import_statement"}),
        calls={"call": ("function",)},
        wrappers=frozenset({"decorated_definition", "expression_statement", "if_statement",
                            "try_statement", "block", "else_clause", "elif_clause",
                            "except_clause", "finally_clause"}),
        decorators=frozenset({"decorator"}),
        return_fields=("return_type",),
        base_fields=("superclasses",),
        py_docstring=True,
    ),
    "javascript": Spec(**_JS),
    "typescript": Spec(**_TS),
    "tsx": Spec(**_TS),
    "go": Spec(
        functions=frozenset({"function_declaration", "method_declaration"}),
        values={"const_declaration": "const_spec", "var_declaration": "var_spec"},
        types={"type_declaration": "type_spec"},
        imports=frozenset({"import_declaration"}),
        import_items=frozenset({"import_spec"}),
        calls={"call_expression": ("function",)},
        return_fields=("result",),
        receiver_field="receiver",
    ),
    "java": Spec(
        functions=frozenset({"method_declaration", "constructor_declaration"}),
        classes=frozenset({"class_declaration", "interface_declaration",
                           "record_declaration", "enum_declaration"}),
        values={"field_declaration": "variable_declarator",
                "constant_declaration": "variable_declarator"},
        imports=frozenset({"import_declaration"}),
        calls={"method_invocation": ("object", "name"), "object_creation_expression": ("type",)},
        wrappers=frozenset({"enum_body_declarations"}),
        decorators=frozenset({"marker_annotation", "annotation"}),
        return_fields=("type",),
        base_fields=("superclass", "interfaces"),
    ),
}

EXT_LANG = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
    ".go": "go", ".java": "java",
}


def lang_for(path: str | None) -> str | None:
    return EXT_LANG.get(os.path.splitext(path or "")[1].lower())


@functools.lru_cache(maxsize=None)
def _parser(lang):
    return get_parser(lang)


# ------------------------------------------------------------- extracción

@dataclass
class Symbol:
    qualname: str
    kind: str
    signature: str = ""
    decorators: list = field(default_factory=list)
    bases: list = field(default_factory=list)
    docstring: str | None = None
    body_hash: str = ""          # hash del árbol sin comentarios ni espacios
    sig_key: str = ""            # hash estructural de la firma (ignora espacios)
    source: str = ""
    calls: list = field(default_factory=list)
    lineno: int = 0

    @property
    def n_lines(self) -> int:
        return self.source.count("\n") + 1 if self.source else 0


@dataclass
class ModuleInfo:
    symbols: dict
    imports: list
    error: str | None = None
    warning: str | None = None


def _norm(text: str) -> str:
    return " ".join(text.split())


def _clean_doc(text: str) -> str:
    text = re.sub(r'^[rRuUbBfF]*("""|\'\'\'|"|\')', "", text.strip())
    text = re.sub(r'("""|\'\'\'|"|\')$', "", text)
    lines = [re.sub(r"^\s*(/\*\*?|\*/|\*|//+|#)\s?", "", l).rstrip("*/ ").strip()
             for l in text.splitlines()]
    return "\n".join(l for l in lines if l).strip()


def extract(source: str, lang: str) -> ModuleInfo:
    spec = SPECS[lang]
    src = source.encode("utf-8")
    tree = _parser(lang).parse(src)
    symbols: dict[str, Symbol] = {}
    imports: set[str] = set()

    def T(n) -> str:
        return src[n.start_byte:n.end_byte].decode("utf-8", "replace")

    def h(nodes) -> str:
        """Hash de la estructura: tipos de nodo + texto de las hojas, sin
        comentarios. Ignora espacios, indentación y saltos de línea."""
        acc = hashlib.sha1()
        stack = list(reversed([n for n in nodes if n is not None]))
        while stack:
            n = stack.pop()
            if n.type in COMMENT_TYPES:
                continue
            acc.update(n.type.encode() + b"\x00")
            if n.child_count == 0:
                acc.update(src[n.start_byte:n.end_byte] + b"\x01")
            else:
                stack.extend(reversed(n.children))
        return acc.hexdigest()[:12]

    def calls_in(node) -> list:
        out, stack = set(), [node] if node is not None else []
        while stack:
            n = stack.pop()
            fields = spec.calls.get(n.type)
            if fields:
                parts = [n.child_by_field_name(f) for f in fields]
                name = ".".join(_norm(T(p)) for p in parts if p is not None)
                if name:
                    out.add(name if len(name) <= 60 else name[:59] + "…")
            stack.extend(n.named_children)
        return sorted(out)

    def py_doc_node(body):
        if body is None or not body.named_children:
            return None
        first = body.named_children[0]
        if first.type == "string":
            return first
        if first.type == "expression_statement" and first.named_children \
                and first.named_children[0].type == "string":
            return first
        return None

    def doc_and_body(node, anchor):
        """(docstring, nodos del cuerpo sin docstring)."""
        body = node.child_by_field_name("body")
        children = list(body.children) if body is not None else []
        if spec.py_docstring:
            d = py_doc_node(body)
            if d is not None:
                children = [c for c in children if c != d]
                return _clean_doc(T(d)), children
            return None, children
        # Otros lenguajes: comentarios inmediatamente anteriores (JSDoc, Javadoc, Go doc).
        com, prev = [], anchor.prev_named_sibling
        while prev is not None and prev.type in COMMENT_TYPES:
            com.insert(0, T(prev))
            prev = prev.prev_named_sibling
        return (_clean_doc("\n".join(com)) or None) if com else None, children

    def own_decorators(node) -> list:
        out = []
        for c in node.children:
            if c.type in spec.decorators:
                out.append(_norm(T(c)).lstrip("@"))
            elif c.type == "modifiers":  # Java
                out += [_norm(T(a)).lstrip("@") for a in c.named_children if a.type in spec.decorators]
        return out

    def add_function(fn, prefix, in_class, decos, anchor, name=None, src_node=None):
        if name is None:
            n = fn.child_by_field_name("name")
            if n is None:
                return
            name = T(n)
        kind = "method" if in_class else "function"
        if spec.receiver_field and fn.child_by_field_name(spec.receiver_field) is not None:
            recv = fn.child_by_field_name(spec.receiver_field)
            m = re.search(r"\*?\s*([A-Za-z_]\w*)\s*(\[[^\]]*\])?\s*\)?\s*$", T(recv))
            prefix, kind = (m.group(1) + "." if m else prefix), "method"
        if "constructor" in fn.type:
            kind = "constructor"
        elif any(c.type == "async" for c in fn.children):
            kind = "async " + kind
        params = fn.child_by_field_name("parameters")
        sig = f"{_norm(T(params))}" if params is not None else "()"
        for rf in spec.return_fields:
            r = fn.child_by_field_name(rf)
            if r is not None:
                sig += f" -> {_norm(T(r)).lstrip(':').strip()}"
                break
        ret = next((fn.child_by_field_name(rf) for rf in spec.return_fields
                    if fn.child_by_field_name(rf) is not None), None)
        doc, body_nodes = doc_and_body(fn, anchor)
        q = prefix + name
        node_for_src = src_node or fn
        symbols[q] = Symbol(q, kind, sig, decos + own_decorators(fn), [], doc,
                            h(body_nodes), h([params, ret]), T(node_for_src),
                            calls_in(fn.child_by_field_name("body")),
                            node_for_src.start_point[0] + 1)

    def add_class(c, prefix, decos, anchor):
        n = c.child_by_field_name("name")
        if n is None:
            return
        q = prefix + T(n)
        bases = []
        for b in spec.base_fields:
            bn = c.child_by_field_name(b) or next((x for x in c.named_children if x.type == b), None)
            if bn is not None:
                bases.append(_norm(T(bn)).strip("()"))
        doc, body_nodes = doc_and_body(c, anchor)
        kind = "interface" if "interface" in c.type else "class"
        symbols[q] = Symbol(q, kind, "", decos + own_decorators(c), bases, doc,
                            h(body_nodes), "", T(c), [], c.start_point[0] + 1)
        body = c.child_by_field_name("body")
        if body is not None:
            walk(body, q + ".", True, [], None)

    def add_values(node, prefix, in_class, decos, anchor, table, kind):
        child_type = table[node.type]
        decls = [node] if child_type is None else [x for x in node.named_children if x.type == child_type]
        for d in decls:
            nn = (d.child_by_field_name("name") or d.child_by_field_name("left")
                  or d.child_by_field_name("property"))
            if nn is None or not IDENT.fullmatch(T(nn)):
                continue
            val = d.child_by_field_name("value") or d.child_by_field_name("right")
            if val is not None and val.type in spec.function_values:
                add_function(val, prefix, in_class, decos, anchor, name=T(nn), src_node=d)
                continue
            if kind is None:
                kind = "attribute" if in_class else "constant"
            q = prefix + T(nn)
            symbols[q] = Symbol(q, kind, _norm(T(d)), decos, body_hash=h([val or d]),
                                sig_key=h([d]), source=T(d), lineno=d.start_point[0] + 1)

    def walk(node, prefix, in_class, pending, anchor):
        for ch in node.named_children:
            t = ch.type
            if t in spec.decorators:
                pending.append(_norm(T(ch)).lstrip("@"))
                continue
            if t in COMMENT_TYPES:
                continue
            anc = anchor or ch
            if t in spec.wrappers:
                walk(ch, prefix, in_class, pending, anc)
                continue
            decos = pending[:]
            pending.clear()
            if t in spec.functions:
                add_function(ch, prefix, in_class, decos, anc)
            elif t in spec.classes:
                add_class(ch, prefix, decos, anc)
            elif t in spec.values:
                add_values(ch, prefix, in_class, decos, anc, spec.values, None)
            elif t in spec.types:
                add_values(ch, prefix, in_class, decos, anc, spec.types, "type")
            elif t in spec.imports and not in_class:
                items = []
                stack = [ch]
                while stack and spec.import_items:
                    n = stack.pop()
                    if n.type in spec.import_items:
                        items.append(n)
                    else:
                        stack.extend(n.named_children)
                imports.update(_norm(T(n)) for n in (items or [ch]))

    walk(tree.root_node, "", False, [], None)
    warning = None
    if tree.root_node.has_error:
        warning = "el árbol tiene errores de sintaxis; el análisis puede ser parcial"
        if not symbols and source.strip():
            return ModuleInfo({}, [], error="no se pudo analizar (errores de sintaxis)")
    return ModuleInfo(symbols, sorted(imports), warning=warning)


# ------------------------------------------------------------ comparación

@dataclass
class Change:
    status: str            # added, removed, modified, cosmetic, renamed, moved
    qualname: str
    kind: str
    details: list = field(default_factory=list)
    old_qualname: str | None = None
    from_path: str | None = None
    lines_changed: int = 0
    needs_llm: bool = False
    body_hash: str = ""
    n_lines: int = 0
    new_source: str = ""   # solo se llena cuando needs_llm, para el contexto del LLM


def _family(kind: str) -> str:
    if kind in ("class", "interface"):
        return "class"
    if kind in ("constant", "attribute", "type"):
        return "value"
    return "func"


def _renameable(s: Symbol) -> bool:
    return _family(s.kind) != "value" and s.n_lines >= MIN_RENAME_LINES


def _lines_changed(a: str, b: str) -> int:
    sm = difflib.SequenceMatcher(None, a.splitlines(), b.splitlines(), autojunk=False)
    return sum(max(i2 - i1, j2 - j1)
               for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal")


def _set_diff(old, new):
    o, n = set(old), set(new)
    return sorted(n - o), sorted(o - n)


def _short(text: str, n: int = 70) -> str:
    text = _norm(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def _codes(items):
    return ", ".join(f"`{i}`" for i in items)


def compare_symbol(a: Symbol, b: Symbol) -> Change | None:
    if a.source == b.source and a.decorators == b.decorators:
        return None
    d = []
    fam = _family(b.kind)
    if a.kind != b.kind:
        d.append(f"tipo: {KIND_ES.get(a.kind, a.kind)} → {KIND_ES.get(b.kind, b.kind)}")
    if a.sig_key != b.sig_key:
        label = "valor" if fam == "value" else "firma"
        d.append(f"{label}: `{_short(a.signature)}` → `{_short(b.signature)}`")
    add, rem = _set_diff(a.decorators, b.decorators)
    if add:
        d.append("decoradores nuevos: " + _codes("@" + x for x in add))
    if rem:
        d.append("decoradores quitados: " + _codes("@" + x for x in rem))
    if a.bases != b.bases:
        d.append(f"herencia: `{', '.join(a.bases) or '-'}` → `{', '.join(b.bases) or '-'}`")
    if a.docstring != b.docstring:
        d.append("documentación actualizada")

    lines = 0
    body_changed = fam == "func" and a.body_hash != b.body_hash
    if body_changed:
        lines = _lines_changed(a.source, b.source)
        d.append(f"cuerpo modificado (~{lines} líneas)")
        add, rem = _set_diff(a.calls, b.calls)
        if add:
            d.append("ahora llama a " + _codes(add))
        if rem:
            d.append("deja de llamar a " + _codes(rem))

    if not d:
        # Misma estructura. En una clase significa que cambiaron sus miembros
        # (se reportan aparte); en lo demás, solo formato o comentarios.
        if fam == "class":
            return None
        return Change("cosmetic", b.qualname, b.kind, ["solo formato o comentarios"])

    needs = body_changed and lines >= LLM_LINES_THRESHOLD
    return Change("modified", b.qualname, b.kind, d, lines_changed=lines,
                  needs_llm=needs, new_source=b.source if needs else "")


def _describe_new(s: Symbol, children, pool) -> list:
    d = []
    if s.docstring:
        d.append(_short(s.docstring.splitlines()[0], 100).rstrip("."))
    if s.bases:
        d.append(f"hereda de {_codes(s.bases)}")
    if s.decorators:
        d.append(_codes("@" + x for x in s.decorators))
    methods = [c.split(".")[-1] for c in children if _family(pool[c].kind) == "func"]
    if methods:
        d.append(f"métodos: {_codes(methods)}")
    return d


def compare_modules(old: ModuleInfo | None, new: ModuleInfo | None):
    os_ = old.symbols if old else {}
    ns = new.symbols if new else {}

    changes: list[Change] = []
    removed = {q: os_[q] for q in os_.keys() - ns.keys()}
    added = {q: ns[q] for q in ns.keys() - os_.keys()}

    for q in sorted(os_.keys() & ns.keys()):
        c = compare_symbol(os_[q], ns[q])
        if c:
            changes.append(c)

    def _renamed(oq, q):
        so, sn = removed.pop(oq), added.pop(q)
        c = compare_symbol(so, sn)
        d = list(c.details) if c and c.status == "modified" else []
        changes.append(Change("renamed", q, sn.kind, d, old_qualname=oq,
                              lines_changed=c.lines_changed if c else 0,
                              needs_llm=bool(c and c.needs_llm),
                              new_source=c.new_source if c else ""))

    def _pair_members(oq, q):
        for om in [k for k in removed if k.startswith(oq + ".")]:
            nm = q + om[len(oq):]
            if nm in added:
                c = compare_symbol(removed.pop(om), added.pop(nm))
                if c:
                    changes.append(c)

    # Renombres de clases: por similitud del conjunto de miembros (Jaccard),
    # así un renombre sigue detectándose aunque además cambie algún método.
    def _members(q, pool):
        return {k[len(q) + 1:] for k in pool
                if k.startswith(q + ".") and "." not in k[len(q) + 1:]}

    for q in sorted(k for k, s in added.items() if _family(s.kind) == "class"):
        mn = _members(q, added)
        best, score = None, 0.0
        for oq in (k for k, s in removed.items() if _family(s.kind) == "class"):
            mo = _members(oq, removed)
            if mn or mo:
                j = len(mn & mo) / len(mn | mo)
                if j > score:
                    best, score = oq, j
        if best and score >= 0.5:
            _renamed(best, q)
            _pair_members(best, q)

    # Renombres de funciones: mismo cuerpo normalizado, otro nombre.
    by_hash: dict = {}
    for q, s in removed.items():
        if _renameable(s):
            by_hash.setdefault((s.body_hash, _family(s.kind)), []).append(q)
    for q in sorted(added, key=lambda x: (x.count("."), x)):
        if q not in added:
            continue
        s = added[q]
        candidates = [c for c in by_hash.get((s.body_hash, _family(s.kind)), []) if c in removed]
        if _renameable(s) and candidates:
            _renamed(candidates[0], q)
            if _family(s.kind) == "class":
                _pair_members(candidates[0], q)

    # Altas y bajas. Los miembros de una clase (o tipo Go) nueva o eliminada
    # se resumen en la línea del contenedor en vez de listarse uno a uno.
    for bucket, status in ((added, "added"), (removed, "removed")):
        parents = {q for q, s in bucket.items() if _family(s.kind) == "class" or s.kind == "type"}
        for q in sorted(bucket):
            s = bucket[q]
            if any(q.startswith(p + ".") for p in parents):
                continue
            children = sorted(k for k in bucket if k.startswith(q + "."))
            d = _describe_new(s, children, bucket) if status == "added" else []
            changes.append(Change(status, q, s.kind, d, body_hash=s.body_hash, n_lines=s.n_lines))

    ia, ir = _set_diff(old.imports if old else [], new.imports if new else [])
    order = {"modified": 0, "renamed": 1, "moved": 2, "added": 3, "removed": 4, "cosmetic": 5}
    changes.sort(key=lambda c: (order[c.status], -c.lines_changed, c.qualname))
    return changes, ia, ir


# -------------------------------------------------------------------- git

@dataclass
class FileReport:
    path: str
    status: str              # A, M, D, R, C, T
    old_path: str | None
    added: int
    deleted: int
    lang: str | None = None
    changes: list = field(default_factory=list)
    imports_added: list = field(default_factory=list)
    imports_removed: list = field(default_factory=list)
    error: str | None = None
    warning: str | None = None

    @property
    def needs_llm(self) -> bool:
        return any(c.needs_llm for c in self.changes)

    @property
    def cosmetic_only(self) -> bool:
        return (self.lang is not None and self.status == "M" and bool(self.changes)
                and all(c.status == "cosmetic" for c in self.changes)
                and not self.imports_added and not self.imports_removed)


def _git(args, cwd) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True)
    if r.returncode != 0:
        sys.exit(f"git {' '.join(args)} falló:\n{r.stderr.decode()}")
    return r.stdout.decode("utf-8", "replace")


def _show(rev, path, cwd) -> str | None:
    r = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=cwd, capture_output=True)
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None


def changed_files(base, head, cwd):
    mb = _git(["merge-base", base, head], cwd).strip()
    parts = _git(["diff", "--name-status", "-z", "-M", mb, head], cwd).split("\0")
    files, i = [], 0
    while i < len(parts) and parts[i]:
        st = parts[i]
        if st[0] in "RC":
            files.append((st[0], parts[i + 1], parts[i + 2]))
            i += 3
        else:
            files.append((st[0], parts[i + 1], parts[i + 1]))
            i += 2

    raw = _git(["diff", "--numstat", "-z", "-M", "-w", mb, head], cwd).split("\0")
    stats, i = {}, 0
    while i < len(raw) and raw[i]:
        a, d, p = raw[i].split("\t")
        if p == "":
            p, i = raw[i + 2], i + 3
        else:
            i += 1
        stats[p] = (int(a) if a != "-" else 0, int(d) if d != "-" else 0)
    return mb, [(st, old, new, *stats.get(new, (0, 0))) for st, old, new in files]


def detect_moves(reports):
    """Empareja símbolos eliminados en un archivo con añadidos en otro (mismo cuerpo)."""
    pool = {}
    for r in reports:
        for c in r.changes:
            if c.status == "removed" and c.n_lines >= MIN_RENAME_LINES and _family(c.kind) != "value":
                pool.setdefault((c.body_hash, _family(c.kind)), []).append((r, c))
    for r in reports:
        for c in r.changes:
            if c.status != "added" or c.n_lines < MIN_RENAME_LINES:
                continue
            cands = [(rr, cc) for rr, cc in pool.get((c.body_hash, _family(c.kind)), [])
                     if rr is not r and cc in rr.changes]
            if cands:
                rr, cc = cands[0]
                rr.changes.remove(cc)
                c.status, c.from_path, c.old_qualname = "moved", rr.path, cc.qualname
                c.details = []


def compare_sources(src_old, src_new, lang, rep: FileReport):
    mo = extract(src_old, lang) if src_old is not None else None
    mn = extract(src_new, lang) if src_new is not None else None
    rep.error = (mo and mo.error) or (mn and mn.error)
    rep.warning = (mn and mn.warning) or (mo and mo.warning)
    if not rep.error:
        rep.changes, rep.imports_added, rep.imports_removed = compare_modules(mo, mn)


def compare_files(old_path: str, new_path: str, lang: str) -> FileReport:
    report = FileReport(new_path, "M", old_path, 0, 0, lang=lang)
    compare_sources(_read(old_path), _read(new_path), lang, report)
    return report


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def analyze(base, head, cwd="."):
    mb, files = changed_files(base, head, cwd)
    reports = []
    for st, old, new, a, d in files:
        lang = lang_for(new) or lang_for(old)
        rep = FileReport(new, st, old if old != new else None, a, d, lang=lang)
        reports.append(rep)
        if lang is None:
            continue
        src_old = _show(mb, old, cwd) if st not in "AC" else None
        src_new = _show(head, new, cwd) if st != "D" else None
        compare_sources(src_old, src_new, lang, rep)
    detect_moves(reports)
    return reports


# ----------------------------------------------------------------- salida

def render_change(c: Change) -> str:
    k = KIND_ES.get(c.kind, c.kind)
    extra = (": " + "; ".join(c.details)) if c.details else ""
    tag = " **[LLM]**" if c.needs_llm else ""
    if c.status == "added":
        return f"- **+** {k} `{c.qualname}`{extra}"
    if c.status == "removed":
        return f"- **-** {k} `{c.qualname}`"
    if c.status == "renamed":
        return f"- **↻** {k} `{c.old_qualname}` → `{c.qualname}`{extra}{tag}"
    if c.status == "moved":
        return f"- **⇢** {k} `{c.qualname}` movido desde `{c.from_path}` (`{c.old_qualname}`)"
    if c.status == "cosmetic":
        return f"- **·** `{c.qualname}`: solo formato o comentarios"
    return f"- **~** {k} `{c.qualname}`{extra}{tag}"


def render_file(r: FileReport) -> str:
    title = f"`{r.old_path}` → `{r.path}`" if r.old_path else f"`{r.path}`"
    out = [f"### {title} (+{r.added} -{r.deleted}){' [LLM]' if r.needs_llm else ''}"]
    if r.lang is None:
        out.append("_Lenguaje no soportado, sin análisis estructural._")
    elif r.error:
        out.append(f"_No se pudo analizar: {r.error}_")
    elif r.cosmetic_only:
        out.append("_Solo cambios de formato o comentarios. Se puede saltar._")
    else:
        if r.warning:
            out.append(f"_Aviso: {r.warning}_")
        out += [render_change(c) for c in r.changes if c.status != "cosmetic"]
        n_cos = sum(c.status == "cosmetic" for c in r.changes)
        if n_cos:
            out.append(f"- _{n_cos} símbolo(s) con cambios solo de formato_")
        if r.imports_added:
            out.append("- imports nuevos: " + _codes(r.imports_added))
        if r.imports_removed:
            out.append("- imports quitados: " + _codes(r.imports_removed))
        if len(out) == 1 + bool(r.warning):
            out.append("_Sin cambios estructurales detectados._")
    return "\n".join(out)


GROUPS = [("A", "Nuevos"), ("M", "Modificados"), ("R", "Renombrados"),
          ("C", "Copiados"), ("D", "Eliminados"), ("T", "Cambio de tipo")]


def render_markdown(reports, base, head) -> str:
    n_llm = sum(r.needs_llm for r in reports)
    n_skip = sum(r.cosmetic_only for r in reports)
    counts = ", ".join(f"{sum(r.status == s for r in reports)} {name.lower()}"
                       for s, name in GROUPS if any(r.status == s for r in reports))
    out = [f"# Diff estructural `{base}`..`{head}`", "",
           f"{len(reports)} archivos ({counts}). "
           f"{n_llm} requieren LLM, {n_skip} son solo formato.", ""]
    for st, name in GROUPS:
        group = sorted((r for r in reports if r.status == st),
                       key=lambda r: -(r.added + r.deleted))
        if group:
            out.append(f"## {name}\n")
            out += [render_file(r) + "\n" for r in group]
    return "\n".join(out)


def llm_context(r: FileReport) -> str:
    """Contexto compacto para pedir la motivación: resumen estructural más
    solo el código de los cambios grandes, no el diff completo."""
    lines = [f"Archivo: {r.path} ({r.lang}, estado {r.status}, +{r.added} -{r.deleted})",
             "Cambios estructurales:"]
    lines += [render_change(c) for c in r.changes if c.status != "cosmetic"]
    for c in r.changes:
        if c.needs_llm:
            lines += [f"\nCódigo nuevo de `{c.qualname}`:", f"```{r.lang}", c.new_source, "```"]
    return "\n".join(lines)


def to_json(reports) -> str:
    data = []
    for r in reports:
        d = asdict(r)
        d["needs_llm"], d["cosmetic_only"] = r.needs_llm, r.cosmetic_only
        for c in d["changes"]:
            for k in ("body_hash", "n_lines", "new_source"):
                c.pop(k, None)
        data.append(d)
    return json.dumps(data, ensure_ascii=False, indent=2)