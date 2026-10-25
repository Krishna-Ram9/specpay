"""Measure a flat-pattern ASCII DXF using only the standard library.

The file is untrusted input: size and entity counts are capped, and only computed numbers
(never text from the file) are passed on to the AI.
Measured: LINE, ARC, CIRCLE, LWPOLYLINE/POLYLINE (with bulges) and SPLINE control points.
Holes are CIRCLE entities. Coordinates are reported from the bottom-left corner of the bounding box.
"""
import hashlib
import math
import re

MAX_CHARS = 2_000_000
MAX_ENTITIES = 50_000
UNITS = {1: ("inch", 25.4), 2: ("foot", 304.8), 4: ("mm", 1.0), 5: ("cm", 10.0), 6: ("m", 1000.0)}  # $INSUNITS


class DXFError(ValueError):
    pass


def _entities(text: str):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    pairs = []
    for i in range(0, len(lines) - 1, 2):
        try:
            pairs.append((int(lines[i].strip()), lines[i + 1].strip()))
        except ValueError:
            raise DXFError("This does not look like an ASCII DXF file (binary DXF is not supported)")
    units, section, ents, cur = None, None, [], None
    for n, (code, val) in enumerate(pairs):
        if code == 0 and val == "SECTION" and n + 1 < len(pairs):
            section = pairs[n + 1][1]
        elif code == 0 and val == "ENDSEC":
            section = None
        elif section == "HEADER" and code == 9 and val == "$INSUNITS" and n + 1 < len(pairs):
            try:
                units = int(pairs[n + 1][1])
            except ValueError:
                pass
        elif section == "ENTITIES":
            if code == 0:
                cur = {"type": val, "d": []}
                ents.append(cur)
                if len(ents) > MAX_ENTITIES:
                    raise DXFError("The DXF has too many entities for this demo")
            elif cur is not None:
                cur["d"].append((code, val))
    return units, ents


def _f(d, code, default=0.0):
    for c, v in d:
        if c == code:
            try:
                return float(v)
            except ValueError:
                return default
    return default


def _arc_pts(cx, cy, r, a0, a1):
    """Extreme points of a counter-clockwise arc from angle a0 to a1 (degrees)."""
    a0, a1 = a0 % 360, a1 % 360
    sweep = (a1 - a0) % 360 or 360
    angles = [a0, a1] + [q for q in (0, 90, 180, 270) if (q - a0) % 360 < sweep]
    return [(cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))) for a in angles]


def _bulge_pts(p1, p2, b):
    if abs(b) < 1e-9:
        return []
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    d = math.hypot(dx, dy)
    if d < 1e-9:
        return []
    off = (d / 2) * (1 - b * b) / (2 * b)  # signed distance from the chord midpoint along the left normal
    cx, cy = (p1[0] + p2[0]) / 2 - dy / d * off, (p1[1] + p2[1]) / 2 + dx / d * off
    r = math.hypot(p1[0] - cx, p1[1] - cy)
    a1 = math.degrees(math.atan2(p1[1] - cy, p1[0] - cx))
    a2 = math.degrees(math.atan2(p2[1] - cy, p2[0] - cx))
    return _arc_pts(cx, cy, r, a1, a2) if b > 0 else _arc_pts(cx, cy, r, a2, a1)


def _poly_pts(verts, closed):
    pts = [(x, y) for x, y, _ in verts]
    segs = list(zip(verts, verts[1:])) + ([(verts[-1], verts[0])] if closed and len(verts) > 2 else [])
    for (x1, y1, b), (x2, y2, _) in segs:
        pts += _bulge_pts((x1, y1), (x2, y2), b)
    return pts


def analyze(text: str, filename: str = "") -> dict:
    """Return measured facts about a flat-pattern DXF, or raise DXFError."""
    if len(text) > MAX_CHARS:
        raise DXFError("The DXF file is larger than 2 MB")
    units_code, ents = _entities(text)
    pts, holes, counts, skipped = [], [], {}, 0
    layers, lines, polys = {}, [], []
    i = 0
    while i < len(ents):
        e, d, t = ents[i], ents[i]["d"], ents[i]["type"]
        i += 1
        if t == "LINE":
            pts += [(_f(d, 10), _f(d, 20)), (_f(d, 11), _f(d, 21))]
            lines.append((_f(d, 10), _f(d, 20), _f(d, 11), _f(d, 21)))
        elif t == "CIRCLE":
            x, y, r = _f(d, 10), _f(d, 20), _f(d, 40)
            if r > 0:
                pts += [(x - r, y - r), (x + r, y + r)]
                holes.append((2 * r, x, y))
        elif t == "ARC":
            pts += _arc_pts(_f(d, 10), _f(d, 20), _f(d, 40), _f(d, 50), _f(d, 51))
        elif t == "LWPOLYLINE":
            verts = []
            for c, v in d:
                try:
                    if c == 10:
                        verts.append([float(v), 0.0, 0.0])
                    elif c == 20 and verts:
                        verts[-1][1] = float(v)
                    elif c == 42 and verts:
                        verts[-1][2] = float(v)
                except ValueError:
                    pass
            pp = _poly_pts([tuple(v) for v in verts], int(_f(d, 70)) & 1)
            pts += pp
            polys.append((bool(int(_f(d, 70)) & 1), len(verts), pp))
        elif t == "POLYLINE":
            verts = []
            while i < len(ents) and ents[i]["type"] == "VERTEX":
                vd = ents[i]["d"]
                verts.append((_f(vd, 10), _f(vd, 20), _f(vd, 42)))
                i += 1
            if i < len(ents) and ents[i]["type"] == "SEQEND":
                i += 1
            pp = _poly_pts(verts, int(_f(d, 70)) & 1)
            pts += pp
            polys.append((bool(int(_f(d, 70)) & 1), len(verts), pp))
        elif t == "SPLINE":
            xs = [float(v) for c, v in d if c == 10 and re.fullmatch(r"-?[\d.eE+-]+", v)]
            ys = [float(v) for c, v in d if c == 20 and re.fullmatch(r"-?[\d.eE+-]+", v)]
            pts += list(zip(xs, ys))
        else:
            skipped += 1
            continue
        counts[t] = counts.get(t, 0) + 1
        lay = re.sub(r"[^A-Za-z0-9_ -]", "_", next((v for c, v in d if c == 8), "0"))[:32] or "0"
        layers[lay] = layers.get(lay, 0) + 1
    if not pts:
        raise DXFError("No measurable geometry (lines, arcs, circles, polylines) was found in the DXF")
    name, k = UNITS.get(units_code, ("mm", 1.0))
    minx, miny = min(p[0] for p in pts), min(p[1] for p in pts)
    w, h = (max(p[0] for p in pts) - minx) * k, (max(p[1] for p in pts) - miny) * k
    hl = sorted(({"diameter": round(dia * k, 2), "x": round((x - minx) * k, 2), "y": round((y - miny) * k, 2)}
                 for dia, x, y in holes), key=lambda q: (q["y"], q["x"]))[:50]
    pf = []
    for closed, n, pp in polys[:20]:
        if pp:
            xs_, ys_ = [a for a, _ in pp], [b for _, b in pp]
            pf.append({"closed": closed, "vertices": n,
                       "width": round((max(xs_) - min(xs_)) * k, 2), "height": round((max(ys_) - min(ys_)) * k, 2)})
    axis = []
    for x1, y1, x2, y2 in lines:
        if abs(y1 - y2) < 1e-4 and abs(x1 - x2) > 1e-4:
            axis.append({"dir": "horizontal", "at": round((y1 - miny) * k, 2),
                         "from": round((min(x1, x2) - minx) * k, 2), "to": round((max(x1, x2) - minx) * k, 2)})
        elif abs(x1 - x2) < 1e-4 and abs(y1 - y2) > 1e-4:
            axis.append({"dir": "vertical", "at": round((x1 - minx) * k, 2),
                         "from": round((min(y1, y2) - miny) * k, 2), "to": round((max(y1, y2) - miny) * k, 2)})
    groups = {}
    for q in hl:
        groups[q["diameter"]] = groups.get(q["diameter"], 0) + 1
    summary = (f"{len(hl)} circular hole(s): " + ", ".join(f"{n} x dia {dia:.2f}" for dia, n in sorted(groups.items()))
               ) if hl else "no circular holes found"
    return {
        "file_name": re.sub(r"[^A-Za-z0-9._ -]", "_", filename or "upload.dxf")[:80],
        "sha256": hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(),
        "units": "mm", "source_units": "mm" if units_code in (None, 0) else name,
        "units_assumed": units_code in (None, 0),
        "width": round(w, 2), "height": round(h, 2),
        "entity_counts": counts, "unmeasured_entities": skipped,
        "holes": hl, "hole_summary": summary,
        "layers": dict(list(layers.items())[:20]), "axis_lines": axis[:30], "polylines": pf,
    }


def describe(f: dict) -> str:
    """Plain-text facts for the AI prompt (numbers only; nothing from inside the file's text)."""
    ents = ", ".join(f"{n} x {t}" for t, n in sorted(f["entity_counts"].items())) or "none"
    lays = ", ".join(f"{k} ({v})" for k, v in f.get("layers", {}).items()) or "none"
    pls = "; ".join(f"{'closed' if p['closed'] else 'open'} polyline, {p['vertices']} vertices, extent {p['width']:.2f} x {p['height']:.2f} mm"
                    for p in f.get("polylines", [])) or "none"
    axis = "\n".join(f"  {a['dir']} line at {'y' if a['dir'] == 'horizontal' else 'x'}={a['at']:.2f}, "
                     f"from {a['from']:.2f} to {a['to']:.2f} (length {a['to'] - a['from']:.2f} mm)"
                     for a in f.get("axis_lines", []))
    units = "not declared in the file (assumed mm)" if f["units_assumed"] else f"declared in the file as {f.get('source_units', 'mm')}"
    extra = (f"Units: {units}\n"
             f"Entities: {sum(f['entity_counts'].values())} measured ({ents}); "
             f"{f['unmeasured_entities']} other entities not measured\n"
             f"Layers (entities per layer): {lays}\nPolylines: {pls}\nAxis-aligned lines:\n{axis or '  none'}")
    hole_lines = "\n".join(f"  hole dia {q['diameter']:.2f} mm at x={q['x']:.2f}, y={q['y']:.2f}" for q in f["holes"])
    return (f"Uploaded file name: {f['file_name']}\n"
            f"Overall size of all geometry: {f['width']:.2f} x {f['height']:.2f} mm"
            f"{' (units not stated in the file, assumed mm)' if f['units_assumed'] else ''}\n"
            f"{f['hole_summary']} (positions measured from the bottom-left corner of the overall bounding box)\n"
            f"{hole_lines}\n{extra}").strip()
