"""Safe analysis of files a freelancer attaches to a submission.

Nothing is ever executed, unpacked to disk or trusted. Computed facts go to the AI as trusted evidence;
short text excerpts go separately, labelled as untrusted data. Standard library only (PDF text needs
the optional `pypdf` package and degrades gracefully without it).
"""
import ast
import base64
import binascii
import csv
import io
import json
import re
import zipfile

import dxf_tools

MAX_FILES, MAX_BYTES, MAX_TOTAL, EXCERPT = 5, 2_000_000, 6_000_000, 6000
SUPPORTED = ".dxf .step .stp .py .zip .pdf .csv .md .txt .json"
_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[Ee][-+]?\d+)?"


class FileError(ValueError):
    pass


def _clean(s, n=60) -> str:
    return re.sub(r"[^A-Za-z0-9_.\- ]", "_", str(s))[:n]


def _dec(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _dxf(name, raw):
    try:
        f = dxf_tools.analyze(_dec(raw), name)
    except dxf_tools.DXFError as exc:
        raise FileError(str(exc))
    return {**f, "summary": f"{f['width']} x {f['height']} mm; {f['hole_summary']}", "details": dxf_tools.describe(f)}, None


def _protocol(schema: str):
    """Name the STEP application protocol from the schema string (AUTOMOTIVE_DESIGN is AP214)."""
    u = schema.upper()
    if u.startswith("AUTOMOTIVE_DESIGN") or re.search(r"\b214\b", u):
        return "AP214"
    if u.startswith("CONFIG_CONTROL_DESIGN") or u.startswith("AP203"):
        return "AP203"
    if "AP242" in u or "MANAGED_MODEL_BASED_3D_ENGINEERING" in u:
        return "AP242"
    return None


def _extent(points, k):
    return [round((max(p[a] for p in points) - min(p[a] for p in points)) * k, 2) for a in range(3)] if points else None


def _step(name, raw):
    t = _dec(raw)
    if not t.lstrip().startswith("ISO-10303-21"):
        raise FileError("this does not look like a STEP file (ISO-10303-21)")
    up = t.upper()
    sch = re.search(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']{0,60})'", t)
    if re.search(r"\.MILLI\.\s*,\s*\.METRE\.", up):
        k, units = 1.0, "mm (declared)"
    elif "'INCH'" in up:
        k, units = 25.4, "inch (declared, converted to mm)"
    elif re.search(r"SI_UNIT\s*\(\s*\$\s*,\s*\.METRE\.", up):
        k, units = 1000.0, "metre (declared, converted to mm)"
    else:
        k, units = 1.0, "not declared (assumed mm)"
    pts = {}
    for i, c in re.findall(r"#(\d+)\s*=\s*CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(([^)]*)\)\s*\)", up):
        v = [float(x) for x in re.findall(_NUM, c)[:3]]
        if len(v) == 3:
            pts[int(i)] = v
    verts = [int(v) for v in re.findall(r"VERTEX_POINT\s*\(\s*'[^']*'\s*,\s*#(\d+)\s*\)", up)]
    use = [pts[v] for v in verts if v in pts] or list(pts.values())
    ext = _extent(use, k)
    ext_all = _extent(list(pts.values()), k)
    groups = {}
    for r in re.findall(r"CYLINDRICAL_SURFACE\s*\(\s*'[^']*'\s*,\s*#\d+\s*,\s*(" + _NUM + r")\s*\)", up)[:500]:
        d = round(float(r) * k * 2, 2)
        groups[d] = groups.get(d, 0) + 1
    solids = len(re.findall(r"MANIFOLD_SOLID_BREP|BREP_WITH_VOIDS", up))
    faces = len(re.findall(r"ADVANCED_FACE", up))
    proto = _protocol(sch.group(1)) if sch else None
    desc = re.search(r"FILE_DESCRIPTION\s*\(\s*\(\s*'([^']{0,80})'", t)
    fn = re.search(r"FILE_NAME\s*\(\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*\([^)]*\)\s*,\s*\([^)]*\)\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)", t)
    head = ((f"; application protocol {proto}" if proto else "")
            + (f"; description in the file header: {_clean(desc.group(1), 60)}" if desc else "")
            + (f"; exporting system named in the file header: {_clean(fn.group(4), 60)}" if fn and fn.group(4).strip() else ""))
    inch = units.startswith("inch")
    inn = lambda v: f" ({v / 25.4:.3f} in)" if inch else ""
    lines = [f"STEP file: schema {_clean(sch.group(1)) if sch else 'unknown'}{head}; units {units}; solids: {solids}; faces: {faces}"]
    if ext:
        same = ext_all is None or all(abs(x - y) < 0.01 for x, y in zip(ext, ext_all))
        fmt = lambda e: f"{e[0]} x {e[1]} x {e[2]} mm" + (f" ({e[0] / 25.4:.3f} x {e[1] / 25.4:.3f} x {e[2] / 25.4:.3f} in)" if inch else "")
        if same:
            lines.append(f"Bounding box of the model: {fmt(ext)}; smallest extent: {min(ext)} mm "
                         "(this equals the thickness only for plate-like or sheet-metal parts)")
        else:
            lines.append(f"Bounding box of the model's vertices: {fmt(ext)} (a lower bound: curved edges can extend past their end vertices). "
                         f"Bounding box including every reference point in the file: {fmt(ext_all)} (an upper-end estimate: reference points "
                         "can lie outside the part). The part has curved geometry, so its true overall size is approximate, roughly between "
                         "these two boxes. Do not treat the smallest extent as a thickness.")
    if groups:
        lines.append("Cylindrical surfaces by diameter: " + ", ".join(f"{n} x dia {d:.2f} mm{inn(d)}" for d, n in sorted(groups.items()))
                     + " (a round hole is often modelled as two half-cylinders, so counts can be double the hole count)")
    if not ext:
        box = "no geometry found"
    elif ext_all is None or all(abs(x - y) < 0.01 for x, y in zip(ext, ext_all)):
        box = f"bbox {ext[0]} x {ext[1]} x {ext[2]} mm"
    else:
        box = f"approx size {ext[0]} x {ext[1]} x {ext[2]} to {ext_all[0]} x {ext_all[1]} x {ext_all[2]} mm (curved part)"
    return {"summary": f"STEP: {solids} solid(s), {faces} faces, {box}", "details": "\n".join(lines)}, None


def _py(name, raw):
    t = _dec(raw)
    try:
        tree = ast.parse(t)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        line = getattr(exc, "lineno", "?")
        return ({"summary": f"Python: SYNTAX ERROR (line {line})",
                 "details": f"Python file: SYNTAX ERROR at line {line}; the file does not parse. It was not executed."}, t[:EXCERPT])
    nodes = list(ast.walk(tree))
    funcs = sorted({n.name for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))})
    classes = sorted({n.name for n in nodes if isinstance(n, ast.ClassDef)})
    imps = {a.name.split(".")[0] for n in nodes if isinstance(n, ast.Import) for a in n.names}
    imps |= {n.module.split(".")[0] for n in nodes if isinstance(n, ast.ImportFrom) and n.module}
    tests = [f for f in funcs if f.startswith("test")]
    asserts = sum(isinstance(n, ast.Assert) for n in nodes)
    asserts += sum(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr.startswith("assert") for n in nodes)
    lst = lambda xs: ", ".join(_clean(x, 40) for x in xs[:30]) or "none"
    d = (f"Python file (static analysis only; the code was NOT executed, so it is unknown whether it runs or tests pass): "
         f"syntax valid; {t.count(chr(10)) + 1} lines; functions: {lst(funcs)}; classes: {lst(classes)}; "
         f"imports: {lst(sorted(imps))}; test functions: {lst(tests)}; assertions (assert statements and assertEqual-style calls): {asserts}")
    return {"summary": f"Python: syntax valid, {len(funcs)} functions, {len(tests)} test functions, {asserts} asserts", "details": d}, t[:EXCERPT]


def _zip(name, raw):
    try:
        infos = zipfile.ZipFile(io.BytesIO(raw)).infolist()
    except (zipfile.BadZipFile, ValueError):
        raise FileError("not a valid ZIP archive")
    files = [i for i in infos if not i.is_dir()]
    names = ", ".join(re.sub(r"[^A-Za-z0-9_./\- ]", "_", i.filename)[:80] for i in files[:40])
    return ({"summary": f"ZIP: {len(files)} file(s)",
             "details": f"ZIP archive (listing only, nothing was extracted or opened): {len(files)} file(s), "
                        f"{sum(i.file_size for i in files)} bytes uncompressed. File names: {names or 'none'}"}, None)


def _pdf(name, raw):
    if not raw.startswith(b"%PDF"):
        raise FileError("not a PDF file")
    try:
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(raw))
        n = len(r.pages)
        text = "\n".join((p.extract_text() or "") for p in r.pages[:5])
        mb = r.pages[0].mediabox
        w, h = float(mb.width) * 25.4 / 72, float(mb.height) * 25.4 / 72
    except Exception:  # missing pypdf, encrypted or damaged file: fall back to "unverified"
        return ({"summary": "PDF (text not extractable here)",
                 "details": f"PDF file, {len(raw)} bytes. Its text could NOT be extracted on this server, so its contents are unverified."}, None)
    note = "" if text.strip() else " (no text layer: scan or vector-only drawing, so its contents are unverified)"
    return ({"summary": f"PDF: {n} page(s), {len(text)} characters of text",
             "details": f"PDF file: {n} page(s); first page {w:.0f} x {h:.0f} mm; {len(text)} characters of text from the first 5 pages{note}"},
            text[:EXCERPT] if text.strip() else None)


def _csv(name, raw):
    t = _dec(raw)
    try:
        rows = [r for r in list(csv.reader(io.StringIO(t)))[:100001] if r]
    except csv.Error:
        raise FileError("not a readable CSV file")
    hdr = [_clean(c, 40) for c in rows[0][:30]] if rows else []
    n = max(len(rows) - 1, 0)
    return ({"summary": f"CSV: {len(hdr)} columns, {n} rows",
             "details": f"CSV file: header columns: {', '.join(hdr) or 'none'}; {n} data rows"}, "\n".join(t.splitlines()[:25])[:EXCERPT])


def _plain(name, raw):
    t = _dec(raw)
    if name.lower().endswith(".json"):
        try:
            j = json.loads(t)
        except (ValueError, RecursionError):
            return {"summary": "JSON: NOT valid", "details": "JSON file: NOT valid JSON"}, t[:EXCERPT]
        keys = ", ".join(_clean(k, 40) for k in list(j)[:20]) if isinstance(j, dict) else type(j).__name__
        return {"summary": "JSON: valid", "details": f"JSON file: valid; top level: {keys}"}, t[:EXCERPT]
    heads = [_clean(h, 80) for h in re.findall(r"^#{1,6}\s+(.{1,80})$", t, re.M)[:30]]
    return ({"summary": f"Text: {len(t)} characters, {len(heads)} headings",
             "details": f"Text file: {t.count(chr(10)) + 1} lines, {len(t)} characters; headings: {'; '.join(heads) or 'none'}"}, t[:EXCERPT])


ANALYZERS = {".dxf": _dxf, ".step": _step, ".stp": _step, ".py": _py, ".zip": _zip,
             ".pdf": _pdf, ".csv": _csv, ".md": _plain, ".txt": _plain, ".json": _plain}


def analyze_all(items):
    """items: [(file name, base64 data)] -> (facts to store, trusted facts text, untrusted excerpts text)."""
    if len(items) > MAX_FILES:
        raise FileError(f"Attach at most {MAX_FILES} files")
    facts, trusted, excerpts, total = [], [], [], 0
    for name, data in items:
        label = _clean(name, 40)
        ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in ANALYZERS:
            raise FileError(f"{label}: this file type is not supported (use {SUPPORTED})")
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise FileError(f"{label}: the file could not be read")
        total += len(raw)
        if len(raw) > MAX_BYTES or total > MAX_TOTAL:
            raise FileError("Files are too large (2 MB each, 6 MB in total)")
        try:
            f, excerpt = ANALYZERS[ext](name, raw)
        except FileError as exc:
            raise FileError(f"{label}: {exc}")
        details = f.pop("details")
        f.update({"kind": ext.lstrip("."), "file_name": _clean(name, 80), "size": len(raw)})
        facts.append(f)
        trusted.append(f"--- File {len(facts)}: {f['file_name']} ({f['kind']}, {len(raw)} bytes) ---\n{details}")
        if excerpt:
            excerpts.append(f"--- {f['file_name']} ---\n{excerpt}")
    return facts, "\n\n".join(trusted), "\n\n".join(excerpts)
