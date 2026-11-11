"""
recap_html.py: un solo archivo HTML, sin red, para revisar un cambio.

Todo el texto del diff y de las notas se escapa: el diff es dato no confiable.
El HTML no carga fuentes, scripts ni estilos externos.
"""
from __future__ import annotations

import difflib
import html
import re
from datetime import datetime

from pr_gate.recap import FileFacts
from pr_gate.recap import Hunk
from pr_gate.recap import RecapFacts
from pr_gate.recap_notes import RecapNotes
from pr_gate.structdiff import KIND_ES

STATUS_ES = {"A": "nuevo", "M": "modificado", "D": "eliminado", "R": "renombrado",
             "C": "copiado", "T": "cambio de tipo"}
LEVEL_ORDER = {"alto": 0, "medio": 1, "bajo": 2}
MAX_INLINE_PAIR = 400   # líneas más largas no llevan resaltado por palabra
_TOKEN = re.compile(r"\w+|\s+|[^\w\s]")


def render_recap(facts: RecapFacts, notes: RecapNotes, now: datetime | None = None) -> str:
    now = now or datetime.now()
    title = notes.title or f"Recap {facts.base}..{facts.head}"
    body = "\n".join([
        _topbar(title, facts),
        '<div class="layout">',
        _sidebar(facts),
        '<main id="main">',
        _overview(facts, notes, now),
        _agent_sections(facts, notes),
        _risk_map(facts),
        _files(facts, notes),
        _commits(facts),
        "</main>",
        "</div>",
    ])
    return _PAGE.format(
        title=_e(title),
        storage_key=_e(f"jcton-recap:{facts.head_sha}"),
        css=_CSS,
        body=body,
        js=_JS,
    )


# ---------------------------------------------------------------- partes

def _topbar(title: str, facts: RecapFacts) -> str:
    return f"""
<header class="topbar">
  <div class="tb-title"><span class="logo">jcton</span><h1>{_e(title)}</h1></div>
  <div class="tb-controls">
    <span class="progress" id="progress" aria-live="polite">0 / {len(facts.files)} revisados</span>
    <div class="seg" role="group" aria-label="Vista del diff">
      <button type="button" data-view="unified" class="on">Unificado</button>
      <button type="button" data-view="split">Lado a lado</button>
    </div>
    <label class="chk"><input type="checkbox" id="hide-low"> Ocultar riesgo bajo</label>
    <button type="button" class="ghost" id="expand-all">Expandir todo</button>
  </div>
</header>"""


def _sidebar(facts: RecapFacts) -> str:
    items = []
    for f in facts.files:
        items.append(
            f'<li data-level="{f.risk.level}"><a href="#{_anchor(f.path)}">'
            f'<span class="dot {f.risk.level}" title="riesgo {f.risk.level}"></span>'
            f'<span class="sb-path">{_e(_short_path(f.path))}</span>'
            f'<span class="sb-num"><span class="add">+{f.report.added}</span> '
            f'<span class="del">-{f.report.deleted}</span></span>'
            f'<span class="sb-done" data-done-for="{_e(f.path)}">✓</span></a></li>'
        )
    return f"""
<nav class="sidebar" aria-label="Archivos">
  <div class="sb-head">Archivos <span class="muted">por riesgo</span></div>
  <ul>{''.join(items)}</ul>
</nav>"""


def _overview(facts: RecapFacts, notes: RecapNotes, now: datetime) -> str:
    counts = {"alto": 0, "medio": 0, "bajo": 0}
    for f in facts.files:
        counts[f.risk.level] += 1
    stats = "".join([
        _stat(str(len(facts.files)), "archivos"),
        _stat(f'<span class="add">+{facts.added}</span> <span class="del">-{facts.deleted}</span>', "líneas", raw=True),
        _stat(str(len(facts.commits)), "commits"),
        _stat(f'<span class="cnt alto">{counts["alto"]}</span> · <span class="cnt medio">{counts["medio"]}</span>'
              f' · <span class="cnt bajo">{counts["bajo"]}</span>', "riesgo alto · medio · bajo", raw=True),
    ])
    warnings = "".join(f'<li>{_e(w)}</li>' for w in facts.warnings)
    warn_block = f'<div class="callout warn"><strong>Señales automáticas</strong><ul>{warnings}</ul></div>' if warnings else ""
    author = f" · notas de {_e(notes.author)}" if notes.author else ""
    return f"""
<section class="overview">
  <p class="meta"><code>{_e(facts.base)}</code> → <code>{_e(facts.head)}</code>
    · base común <code>{_e(facts.merge_base[:10])}</code> · head <code>{_e(facts.head_sha[:10])}</code>
    · generado {_e(now.strftime('%Y-%m-%d %H:%M'))}{author}</p>
  <div class="stats">{stats}</div>
  {warn_block}
</section>"""


def _stat(value: str, label: str, raw: bool = False) -> str:
    shown = value if raw else _e(value)
    return f'<div class="stat"><div class="stat-v">{shown}</div><div class="stat-l">{_e(label)}</div></div>'


def _agent_sections(facts: RecapFacts, notes: RecapNotes) -> str:
    if notes.empty:
        return """
<section class="card agent">
  <h2>Narrativa del agente</h2>
  <p class="muted">No se pasaron notas (<code>--notes</code>). El resto de la página son hechos
  calculados por Python a partir de git y tree-sitter.</p>
</section>"""
    parts = ['<section class="card agent">',
             '<h2>Narrativa del agente <span class="tag">escrito por un modelo · verifícalo</span></h2>']
    if notes.problems:
        items = "".join(f"<li>{_e(p)}</li>" for p in notes.problems)
        parts.append(f'<div class="callout bad"><strong>Problemas en las notas</strong><ul>{items}</ul></div>')
    if notes.summary:
        parts.append(f'<h3>Qué cambia</h3>{_prose(notes.summary)}')
    if notes.intent:
        parts.append(f'<h3>Por qué</h3>{_prose(notes.intent)}')
    if notes.risks:
        parts.append("<h3>Riesgos que ve el agente</h3>" + _risk_notes(notes))
    if notes.review_order:
        parts.append("<h3>Orden de lectura sugerido</h3>" + _review_order(notes))
    columns = [
        ("Verificado", notes.verified, "ok"),
        ("Sin verificar", notes.not_verified, "warn"),
        ("Preguntas para el autor", notes.questions, "q"),
    ]
    blocks = []
    for label, items, cls in columns:
        if items:
            lis = "".join(f"<li>{_inline(i)}</li>" for i in items)
            blocks.append(f'<div class="col {cls}"><h3>{label}</h3><ul>{lis}</ul></div>')
    if blocks:
        parts.append(f'<div class="cols">{"".join(blocks)}</div>')
    parts.append("</section>")
    return "\n".join(parts)


def _risk_notes(notes: RecapNotes) -> str:
    rows = []
    for r in sorted(notes.risks, key=lambda r: LEVEL_ORDER[r.level]):
        where = ""
        if r.path:
            label = _e(r.path) + (f":{r.line}" if r.line else "")
            if r.known:
                target = _anchor(r.path) + (f"-L{r.line}" if r.line else "")
                where = f'<a class="path" href="#{target}">{label}</a>'
            else:
                where = f'<span class="path missing" title="No está en el diff">{label} ⚠</span>'
        detail = _prose(r.detail) if r.detail else ""
        rows.append(f'<li class="risk-note"><span class="lv {r.level}">{r.level}</span>'
                    f'<div><strong>{_inline(r.title)}</strong> {where}{detail}</div></li>')
    return f'<ul class="risk-notes">{"".join(rows)}</ul>'


def _review_order(notes: RecapNotes) -> str:
    rows = []
    for item in notes.review_order:
        if item.known:
            link = f'<a class="path" href="#{_anchor(item.path)}">{_e(item.path)}</a>'
        else:
            link = f'<span class="path missing" title="No está en el diff">{_e(item.path)} ⚠</span>'
        why = f' <span class="muted">{_inline(item.why)}</span>' if item.why else ""
        rows.append(f"<li>{link}{why}</li>")
    return f'<ol class="order">{"".join(rows)}</ol>'


def _risk_map(facts: RecapFacts) -> str:
    top = max((f.risk.score for f in facts.files), default=0) or 1
    rows = []
    for f in facts.files:
        width = max(2, round(100 * f.risk.score / top)) if f.risk.score else 0
        signals = "".join(f'<span class="chip">{_e(s)}</span>' for s in f.risk.signals) or '<span class="muted">sin señales</span>'
        rows.append(
            f'<tr data-level="{f.risk.level}"><td><a class="path" href="#{_anchor(f.path)}">{_e(f.path)}</a></td>'
            f'<td class="num">{f.risk.score:g}</td>'
            f'<td class="barcell"><div class="bar {f.risk.level}" style="width:{width}%"></div></td>'
            f'<td>{signals}</td></tr>'
        )
    return f"""
<section class="card">
  <h2>Mapa de riesgo <span class="tag calc">calculado por Python</span></h2>
  <p class="muted small">Puntaje determinista por archivo: rutas sensibles, tamaño, símbolos eliminados,
  firmas cambiadas y cuerpos reescritos. Sirve para ordenar la lectura, no para aprobar.</p>
  <div class="tablewrap"><table class="riskmap">
    <thead><tr><th>Archivo</th><th class="num">Puntaje</th><th></th><th>Señales</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table></div>
</section>"""


def _files(facts: RecapFacts, notes: RecapNotes) -> str:
    return '<section class="files"><h2>Archivos</h2>' + "".join(
        _file(f, notes.file_notes.get(f.path, "")) for f in facts.files
    ) + "</section>"


def _file(f: FileFacts, note: str) -> str:
    r = f.report
    rename = f'<span class="muted">{_e(r.old_path)} → </span>' if r.old_path else ""
    flags = []
    if f.generated:
        flags.append('<span class="chip">generado</span>')
    if r.cosmetic_only:
        flags.append('<span class="chip">solo formato</span>')
    if f.is_test:
        flags.append('<span class="chip test">test</span>')
    open_attr = " open" if f.risk.level != "bajo" and not f.generated else ""
    note_html = f'<div class="callout agent-note"><span class="tag">agente</span>{_prose(note)}</div>' if note else ""
    return f"""
<details class="file" id="{_anchor(f.path)}" data-level="{f.risk.level}"{open_attr}>
  <summary>
    <span class="dot {f.risk.level}"></span>
    <span class="f-path">{rename}{_e(f.path)}</span>
    <span class="f-status">{_e(STATUS_ES.get(r.status, r.status))}</span>
    {''.join(flags)}
    <span class="f-num"><span class="add">+{r.added}</span> <span class="del">-{r.deleted}</span></span>
    <label class="done" onclick="event.stopPropagation()"><input type="checkbox" data-done="{_e(f.path)}"> revisado</label>
  </summary>
  <div class="f-body">
    {note_html}
    {_structure(f)}
    {_diff(f)}
  </div>
</details>"""


def _structure(f: FileFacts) -> str:
    r = f.report
    if r.lang is None:
        return ""
    if r.error:
        return f'<p class="muted small">Sin análisis estructural: {_e(r.error)}</p>'
    items = [_change(c) for c in r.changes if c.status != "cosmetic"]
    if r.imports_added:
        items.append('<li><span class="sym add">+</span> imports: ' + ", ".join(f"<code>{_e(i)}</code>" for i in r.imports_added) + "</li>")
    if r.imports_removed:
        items.append('<li><span class="sym del">−</span> imports: ' + ", ".join(f"<code>{_e(i)}</code>" for i in r.imports_removed) + "</li>")
    if not items:
        return ""
    return f'<div class="structure"><div class="s-head">Cambios estructurales <span class="tag calc">tree-sitter</span></div><ul>{"".join(items)}</ul></div>'


def _change(c) -> str:
    kind = KIND_ES.get(c.kind, c.kind)
    details = "; ".join(c.details)
    detail = f' <span class="muted">{_inline(details)}</span>' if details else ""
    big = ' <span class="chip warnchip">reescrito a fondo</span>' if c.needs_llm else ""
    symbol = {"added": ("add", "+"), "removed": ("del", "−"), "renamed": ("mod", "↻"),
              "moved": ("mod", "⇢"), "modified": ("mod", "~")}.get(c.status, ("mod", "·"))
    name = f"<code>{_e(c.qualname)}</code>"
    if c.status == "renamed":
        name = f"<code>{_e(c.old_qualname)}</code> → <code>{_e(c.qualname)}</code>"
    if c.status == "moved":
        name = f"<code>{_e(c.qualname)}</code> desde <code>{_e(c.from_path)}</code>"
    return f'<li><span class="sym {symbol[0]}">{symbol[1]}</span> {_e(kind)} {name}{detail}{big}</li>'


# ------------------------------------------------------------------ diff

def _diff(f: FileFacts) -> str:
    d = f.diff
    if f.generated:
        return '<p class="muted small">Archivo generado: el diff se omite.</p>'
    if d.binary:
        return '<p class="muted small">Archivo binario.</p>'
    if not d.hunks:
        return '<p class="muted small">Sin líneas de diff (cambio de modo o renombre puro).</p>'
    unified = "".join(_unified_hunk(h, _anchor(f.path)) for h in d.hunks)
    split = "".join(_split_hunk(h) for h in d.hunks)
    cut = f'<p class="muted small">… {d.truncated} líneas más omitidas. Míralas con <code>git diff</code>.</p>' if d.truncated else ""
    return f"""
<div class="diff">
  <table class="code unified"><colgroup><col class="ln"><col class="ln"><col class="sg"><col></colgroup><tbody>{unified}</tbody></table>
  <table class="code split"><colgroup><col class="ln"><col class="half"><col class="ln"><col class="half"></colgroup><tbody>{split}</tbody></table>
  {cut}
</div>"""


def _blocks(hunk: Hunk):
    """Agrupa el hunk en contexto y bloques (borradas, añadidas) consecutivos."""
    i, lines = 0, hunk.lines
    while i < len(lines):
        if lines[i].kind in "+-":
            removed, added = [], []
            while i < len(lines) and lines[i].kind == "-":
                removed.append(lines[i])
                i += 1
            while i < len(lines) and lines[i].kind == "+":
                added.append(lines[i])
                i += 1
            yield "change", removed, added
        else:
            yield "context", [lines[i]], []
            i += 1


def _unified_hunk(hunk: Hunk, anchor: str) -> str:
    rows = [f'<tr class="hunk"><td colspan="4">{_e(hunk.header)}</td></tr>']
    for kind, removed, added in _blocks(hunk):
        if kind == "context":
            line = removed[0]
            if line.kind == "\\":
                rows.append(f'<tr class="meta"><td></td><td></td><td></td><td>{_e(line.text)}</td></tr>')
                continue
            rows.append(_urow("ctx", line.old_no, line.new_no, " ", _e(line.text), anchor))
            continue
        old_html, new_html = _paired(removed, added)
        for line, text in zip(removed, old_html):
            rows.append(_urow("del", line.old_no, None, "−", text))
        for line, text in zip(added, new_html):
            rows.append(_urow("add", None, line.new_no, "+", text, anchor))
    return "".join(rows)


def _urow(cls: str, old_no, new_no, sign: str, text: str, anchor: str = "") -> str:
    ident = f' id="{anchor}-L{new_no}"' if anchor and new_no else ""
    return (f'<tr class="{cls}"{ident}><td class="ln">{old_no or ""}</td><td class="ln">{new_no or ""}</td>'
            f'<td class="sg">{sign}</td><td class="tx">{text}</td></tr>')


def _split_hunk(hunk: Hunk) -> str:
    rows = [f'<tr class="hunk"><td colspan="4">{_e(hunk.header)}</td></tr>']
    for kind, removed, added in _blocks(hunk):
        if kind == "context":
            line = removed[0]
            if line.kind == "\\":
                continue
            text = _e(line.text)
            rows.append(f'<tr class="ctx"><td class="ln">{line.old_no}</td><td class="tx">{text}</td>'
                        f'<td class="ln">{line.new_no}</td><td class="tx">{text}</td></tr>')
            continue
        old_html, new_html = _paired(removed, added)
        for i in range(max(len(removed), len(added))):
            left = (f'<td class="ln">{removed[i].old_no}</td><td class="tx del">{old_html[i]}</td>'
                    if i < len(removed) else '<td class="ln empty"></td><td class="tx empty"></td>')
            right = (f'<td class="ln">{added[i].new_no}</td><td class="tx add">{new_html[i]}</td>'
                     if i < len(added) else '<td class="ln empty"></td><td class="tx empty"></td>')
            rows.append(f'<tr class="pair">{left}{right}</tr>')
    return "".join(rows)


def _paired(removed: list, added: list) -> tuple[list[str], list[str]]:
    """Escapa cada línea y, cuando una borrada y una añadida se emparejan,
    resalta solo las palabras que cambiaron."""
    old = [_e(line.text) for line in removed]
    new = [_e(line.text) for line in added]
    for i in range(min(len(removed), len(added))):
        a, b = removed[i].text, added[i].text
        if len(a) > MAX_INLINE_PAIR or len(b) > MAX_INLINE_PAIR:
            continue
        old[i], new[i] = _word_diff(a, b)
    return old, new


def _word_diff(a: str, b: str) -> tuple[str, str]:
    ta, tb = _TOKEN.findall(a), _TOKEN.findall(b)
    matcher = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    if matcher.ratio() < 0.4:
        return _e(a), _e(b)
    out_a, out_b = [], []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        sa, sb = _e("".join(ta[i1:i2])), _e("".join(tb[j1:j2]))
        if op == "equal":
            out_a.append(sa)
            out_b.append(sb)
            continue
        if sa:
            out_a.append(f"<mark>{sa}</mark>")
        if sb:
            out_b.append(f"<mark>{sb}</mark>")
    return "".join(out_a), "".join(out_b)


def _commits(facts: RecapFacts) -> str:
    if not facts.commits:
        return ""
    rows = "".join(f"<li><code>{_e(c.sha)}</code> {_e(c.subject)} <span class=\"muted\">· {_e(c.author)}</span></li>"
                   for c in facts.commits)
    return f'<section class="card"><h2>Commits</h2><ul class="commits">{rows}</ul></section>'


# ---------------------------------------------------------------- texto

def _e(value) -> str:
    return html.escape(str(value or ""), quote=True)


def _inline(text: str) -> str:
    """Escapa y luego permite solo `código` y **negrita**."""
    escaped = _e(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)


def _prose(text: str) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return "".join(f"<p>{_inline(p).replace(chr(10), '<br>')}</p>" for p in paragraphs)


def _anchor(path: str) -> str:
    return "f-" + re.sub(r"[^A-Za-z0-9_-]", "-", path)


def _short_path(path: str) -> str:
    parts = path.split("/")
    if len(parts) <= 3:
        return path
    return "…/" + "/".join(parts[-2:])


# ------------------------------------------------------------- plantilla

_PAGE = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:">
<meta name="referrer" content="no-referrer">
<title>{title}</title>
<style>{css}</style>
</head>
<body data-storage-key="{storage_key}">
{body}
<script>{js}</script>
</body>
</html>
"""

_CSS = """
:root{
  --bg:#f7f7f5;--panel:#ffffff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e4e3de;--soft:#f0efea;
  --accent:#3a5bd9;--add-bg:#e7f6ea;--add-mark:#b7e4c0;--add-ink:#1f7a35;
  --del-bg:#fbeceb;--del-mark:#f3bdb8;--del-ink:#b3261e;
  --hi:#c0392b;--mid:#c27c0e;--lo:#3f8f5b;--hunk:#eef1fb;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
  --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  color-scheme:light;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#141413;--panel:#1c1c1a;--ink:#ecebe6;--muted:#9a9a93;--line:#2e2e2b;--soft:#242422;
  --accent:#8ea2ff;--add-bg:#15291b;--add-mark:#245b33;--add-ink:#7fd394;
  --del-bg:#2e1716;--del-mark:#6a2924;--del-ink:#f2958c;
  --hi:#f07167;--mid:#e0a83a;--lo:#6cc08b;--hunk:#1d2236;color-scheme:dark;
}}
*{box-sizing:border-box}
html{scroll-padding-top:72px}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 var(--sans)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
code{font:12.5px var(--mono);background:var(--soft);padding:1px 4px;border-radius:4px}
h1{font-size:15px;margin:0;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
h2{font-size:17px;margin:0 0 12px;font-weight:650;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
h3{font-size:13px;margin:18px 0 6px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
p{margin:6px 0}
.muted{color:var(--muted)}.small{font-size:12.5px}
.add{color:var(--add-ink)}.del{color:var(--del-ink)}
.topbar{position:sticky;top:0;z-index:5;display:flex;gap:16px;align-items:center;justify-content:space-between;
  padding:10px 20px;background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(8px);
  border-bottom:1px solid var(--line);flex-wrap:wrap}
.tb-title{display:flex;gap:10px;align-items:center;min-width:0;flex:1}
.logo{font:600 12px var(--mono);padding:3px 7px;border-radius:5px;background:var(--ink);color:var(--bg)}
.tb-controls{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.progress{font:12.5px var(--mono);color:var(--muted)}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:7px;overflow:hidden}
.seg button,.ghost{font:inherit;font-size:12.5px;border:0;background:var(--panel);color:var(--ink);padding:5px 10px;cursor:pointer}
.seg button.on{background:var(--ink);color:var(--bg)}
.ghost{border:1px solid var(--line);border-radius:7px}
.chk{font-size:12.5px;display:flex;gap:5px;align-items:center;cursor:pointer}
.layout{display:grid;grid-template-columns:260px minmax(0,1fr);gap:24px;max-width:1500px;margin:0 auto;padding:20px}
.sidebar{position:sticky;top:64px;align-self:start;max-height:calc(100vh - 84px);overflow:auto}
.sb-head{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;margin-bottom:8px}
.sidebar ul{list-style:none;margin:0;padding:0}
.sidebar li a{display:grid;grid-template-columns:10px 1fr auto 12px;gap:8px;align-items:center;padding:5px 6px;
  border-radius:6px;color:var(--ink);font-size:12.5px}
.sidebar li a:hover{background:var(--soft);text-decoration:none}
.sb-path{font-family:var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sb-num{font:11px var(--mono)}
.sb-done{visibility:hidden;color:var(--lo);font-size:12px}.sb-done.on{visibility:visible}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;flex:none}
.dot.alto{background:var(--hi)}.dot.medio{background:var(--mid)}.dot.bajo{background:var(--lo)}
main{min-width:0;display:flex;flex-direction:column;gap:20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px 20px}
.overview .meta{color:var(--muted);font-size:12.5px;margin:0 0 12px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.stat-v{font:600 20px var(--mono)}.stat-l{font-size:12px;color:var(--muted)}
.lv{font:600 11px var(--mono);text-transform:uppercase;padding:1px 6px;border-radius:4px;color:#fff}
.cnt.alto{color:var(--hi)}.cnt.medio{color:var(--mid)}.cnt.bajo{color:var(--lo)}
.lv.alto{background:var(--hi)}.lv.medio{background:var(--mid)}.lv.bajo{background:var(--lo)}
.tag{font:500 11px var(--sans);padding:2px 7px;border-radius:999px;background:var(--soft);color:var(--muted);border:1px solid var(--line)}
.tag.calc{color:var(--accent)}
.callout{border-radius:8px;padding:10px 14px;margin:12px 0;border:1px solid var(--line);background:var(--soft)}
.callout ul{margin:6px 0 0;padding-left:18px}
.callout.warn{border-color:color-mix(in srgb,var(--mid) 45%,var(--line));background:color-mix(in srgb,var(--mid) 9%,var(--panel))}
.callout.bad{border-color:color-mix(in srgb,var(--hi) 45%,var(--line));background:color-mix(in srgb,var(--hi) 9%,var(--panel))}
.agent-note{margin-top:0}.agent-note .tag{float:right}
.risk-notes{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:10px}
.risk-note{display:grid;grid-template-columns:62px 1fr;gap:10px;align-items:start}
.risk-note .lv{text-align:center;margin-top:2px}
.path{font:12.5px var(--mono)}.path.missing{color:var(--hi)}
.order{padding-left:20px;margin:0}.order li{margin:4px 0}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}
.col ul{padding-left:18px;margin:0}.col li{margin:4px 0}
.col.ok h3{color:var(--lo)}.col.warn h3{color:var(--mid)}.col.q h3{color:var(--accent)}
.tablewrap{overflow-x:auto}
table.riskmap{width:100%;border-collapse:collapse;font-size:13px}
.riskmap th{text-align:left;font-weight:600;color:var(--muted);font-size:12px;border-bottom:1px solid var(--line);padding:6px 8px}
.riskmap td{border-bottom:1px solid var(--line);padding:7px 8px;vertical-align:top}
.riskmap .num{text-align:right;font-family:var(--mono)}
.barcell{width:18%;min-width:80px}
.bar{height:8px;border-radius:4px;margin-top:6px}
.bar.alto{background:var(--hi)}.bar.medio{background:var(--mid)}.bar.bajo{background:var(--lo)}
.chip{display:inline-block;font-size:11.5px;padding:1px 7px;margin:1px 4px 1px 0;border-radius:999px;background:var(--soft);border:1px solid var(--line);color:var(--muted)}
.chip.test{color:var(--accent)}.warnchip{color:var(--mid)}
.files h2{margin-bottom:10px}
details.file{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin-bottom:12px;overflow:hidden}
details.file>summary{list-style:none;display:flex;gap:10px;align-items:center;padding:10px 14px;cursor:pointer;flex-wrap:wrap}
details.file>summary::-webkit-details-marker{display:none}
details.file>summary::before{content:"▸";color:var(--muted);transition:transform .15s}
details.file[open]>summary::before{transform:rotate(90deg)}
details.file[open]>summary{border-bottom:1px solid var(--line)}
details.file.reviewed>summary{opacity:.6}
.f-path{font:600 13px var(--mono);word-break:break-all;flex:1;min-width:200px}
.f-status{font-size:12px;color:var(--muted)}
.f-num{font:12px var(--mono)}
.done{font-size:12px;display:flex;gap:4px;align-items:center;color:var(--muted);cursor:pointer}
.f-body{padding:12px 14px}
.structure{border:1px solid var(--line);border-radius:8px;padding:8px 12px;margin-bottom:12px}
.s-head{font-size:12px;font-weight:600;margin-bottom:4px;display:flex;gap:8px;align-items:center}
.structure ul{list-style:none;margin:0;padding:0;font-size:13px}
.structure li{padding:2px 0}
.sym{display:inline-block;width:16px;text-align:center;font-family:var(--mono);font-weight:700}
.sym.add{color:var(--add-ink)}.sym.del{color:var(--del-ink)}.sym.mod{color:var(--mid)}
.diff{overflow-x:auto;border:1px solid var(--line);border-radius:8px}
table.code{width:100%;border-collapse:collapse;font:12.5px/1.45 var(--mono);table-layout:fixed}
table.code col.ln{width:48px}table.code col.sg{width:18px}
table.code td{padding:0 8px;vertical-align:top}
table.code td.tx{white-space:pre-wrap;word-break:break-word}
table.code td.ln{color:var(--muted);text-align:right;user-select:none;font-size:11.5px;border-right:1px solid var(--line)}
table.code td.sg{color:var(--muted);user-select:none;text-align:center;padding:0}
table.code tr.hunk td{background:var(--hunk);color:var(--muted);padding:3px 10px;font-size:11.5px}
table.code tr.meta td{color:var(--muted);font-style:italic}
.unified tr.add td{background:var(--add-bg)}.unified tr.del td{background:var(--del-bg)}
.unified tr.add td.sg{color:var(--add-ink)}.unified tr.del td.sg{color:var(--del-ink)}
.split td.tx.add{background:var(--add-bg)}.split td.tx.del{background:var(--del-bg)}
.split td.empty{background:var(--soft)}
.split td.tx+td.ln{border-left:1px solid var(--line)}
.unified tr.add mark,.split td.add mark{background:var(--add-mark);color:inherit;border-radius:2px}
.unified tr.del mark,.split td.del mark{background:var(--del-mark);color:inherit;border-radius:2px}
table.split{display:none}
body.view-split table.unified{display:none}body.view-split table.split{display:table}
body.hide-low [data-level="bajo"]{display:none}
.unified tr:target td{box-shadow:inset 0 2px 0 var(--accent),inset 0 -2px 0 var(--accent)}
.commits{list-style:none;margin:0;padding:0}.commits li{padding:3px 0;font-size:13px}
@media (max-width:900px){
  .layout{grid-template-columns:1fr;padding:16px}
  .sidebar{position:static;max-height:none}
  .topbar{padding:10px 16px;position:static}html{scroll-padding-top:8px}
  body.view-split table.split{font-size:11px}
}
@media print{.topbar,.sidebar{display:none}.layout{display:block}details.file{break-inside:avoid}}
"""

_JS = r"""
(function(){
  var KEY = document.body.getAttribute('data-storage-key');
  function load(){ try { return JSON.parse(localStorage.getItem(KEY) || '{}'); } catch(e){ return {}; } }
  function save(s){ try { localStorage.setItem(KEY, JSON.stringify(s)); } catch(e){} }
  var state = load();
  var boxes = Array.prototype.slice.call(document.querySelectorAll('input[data-done]'));
  function paint(){
    var n = 0;
    boxes.forEach(function(b){
      var p = b.getAttribute('data-done'), on = !!(state.done && state.done[p]);
      b.checked = on; if (on) n++;
      var d = b.closest('details'); if (d) d.classList.toggle('reviewed', on);
      document.querySelectorAll('.sb-done').forEach(function(s){ if (s.getAttribute('data-done-for') === p) s.classList.toggle('on', on); });
    });
    document.getElementById('progress').textContent = n + ' / ' + boxes.length + ' revisados';
  }
  boxes.forEach(function(b){
    b.addEventListener('change', function(){
      state.done = state.done || {};
      state.done[b.getAttribute('data-done')] = b.checked;
      if (b.checked) { var d = b.closest('details'); if (d) d.open = false; }
      save(state); paint();
    });
  });
  function setView(v){
    document.body.classList.toggle('view-split', v === 'split');
    document.querySelectorAll('.seg button').forEach(function(x){ x.classList.toggle('on', x.getAttribute('data-view') === v); });
    state.view = v; save(state);
  }
  document.querySelectorAll('.seg button').forEach(function(x){
    x.addEventListener('click', function(){ setView(x.getAttribute('data-view')); });
  });
  var hide = document.getElementById('hide-low');
  hide.addEventListener('change', function(){ document.body.classList.toggle('hide-low', hide.checked); state.hideLow = hide.checked; save(state); });
  var all = document.getElementById('expand-all'), expanded = false;
  all.addEventListener('click', function(){
    expanded = !expanded;
    document.querySelectorAll('details.file').forEach(function(d){ d.open = expanded; });
    all.textContent = expanded ? 'Colapsar todo' : 'Expandir todo';
  });
  function reveal(id){
    var t = document.getElementById(id), fallback = false;
    if (!t) { t = document.getElementById(id.replace(/-L\d+$/, '')); fallback = true; }
    if (!t) return;
    var d = t.tagName === 'DETAILS' ? t : t.closest('details'); if (d) d.open = true;
    if (t.tagName === 'TR' && document.body.classList.contains('view-split')) setView('unified');
    if (fallback) setTimeout(function(){ t.scrollIntoView(); }, 0);
  }
  document.querySelectorAll('a[href^="#f-"]').forEach(function(a){
    a.addEventListener('click', function(){ reveal(a.getAttribute('href').slice(1)); });
  });
  if (location.hash) reveal(location.hash.slice(1));
  if (state.view) setView(state.view);
  if (state.hideLow) { hide.checked = true; document.body.classList.add('hide-low'); }
  paint();
})();
"""
