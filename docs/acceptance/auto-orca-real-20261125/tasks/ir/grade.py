"""Independent grader for the frozen formaldehyde Opt/Freq task (stdlib only)."""
from itertools import combinations
import json
import math
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
LIMIT = json.loads((HERE / "rubric.json").read_text(encoding="utf-8"))["thresholds"]
CRITERIA = (
    "conditions_and_provenance", "optimization_and_final_geometry",
    "minimum_and_mode_counts", "carbonyl_mode", "wavenumber",
    "intensity", "displacement_evidence", "source_citations",
)
NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?"
ATOMS = ["C", "O", "H", "H"]
OPT_MARK = "THE OPTIMIZATION HAS CONVERGED"
FINAL_GEOM = "FINAL ENERGY EVALUATION AT THE STATIONARY POINT"


def outcome(ok, detail):
    return {"pass": bool(ok), "detail": str(detail)}


def raw(path):
    return path.read_text(encoding="utf-8", errors="replace")


def contained(root, value):
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ValueError("Expected a work-relative output filename")
    path = (root / value).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Output file missing or outside submitted workspace")
    return path


def xyz_rows(lines):
    parts = [line.split() for line in lines]
    if len(parts) != 4 or [p[0] for p in parts] != ATOMS:
        raise ValueError("Expected C, O, H, H in frozen atom order")
    return [tuple(map(float, p[1:4])) for p in parts]


def geometries(text):
    pat = r"(?m)^CARTESIAN COORDINATES \(ANGSTROEM\)\s*?\r?\n-+\r?\n((?:[^\n]+\n){4})"
    matches = list(re.finditer(pat, text))
    return [(m.start(), xyz_rows(m[1].splitlines())) for m in matches]


def geom_difference(a, b):
    return max(abs(math.dist(a[i], a[j]) - math.dist(b[i], b[j]))
               for i, j in combinations(range(4), 2))


def method_and_state(text):
    inp = " ".join(re.findall(r"(?m)^\|\s*\d+>\s*(.*)$", text)).upper()
    words = re.findall(r"(?m)^\|\s*\d+>\s*!\s*(.+)$", text)
    opts = set(" ".join(words).upper().split())
    # Opt can set TightSCF implicitly. Read the effective printed tolerances.
    scf = re.search(r"(?ms)^Convergence Tolerance:\s*\n(.*?)\n\s*\n", text)
    expected = {"TolE": 1e-8, "TolG": 1e-5, "TolX": 1e-5, "TolErr": 5e-7}
    tight = bool(scf)
    if scf:
        for name, limit in expected.items():
            match = re.search(rf"\b{name}\s*[.]+\s*({NUMBER})", scf[1])
            tight = tight and bool(match and math.isclose(float(match[1]), limit))
    return ({"PBE0", "DEF2-SVP"} <= opts and tight
            and re.search(r"%MAXCORE\s+192(?:\s|$)", inp) is not None
            and not re.search(r"(?:\b(?:SMD|CPCM|COSMO|ALPB|DRCOSMO|SOLVATOR)\b|%CPCM)", inp)
            and not re.search(r"\bPAL(?:[2-9]|\d{2,})\b|\bNPROCS\s+(?:[2-9]|\d{2,})\b", inp)
            and re.search(r"\*\s+XYZ(?:FILE)?\s+0\s+1\b", inp) is not None
            and "Program Version" in text and "ORCA TERMINATED NORMALLY" in text
            and "SCF CONVERGED" in text)


def final_geometry(text):
    start = text.rfind(OPT_MARK)
    final = text.find(FINAL_GEOM, start) if start >= 0 else -1
    if final < 0:
        raise ValueError("Converged stationary-point evaluation missing")
    return next(pos for index, pos in geometries(text) if index > final)


def values(text, start, end):
    left = text.rfind(start)
    right = text.find(end, left + len(start)) if end else len(text)
    if left < 0 or right <= left:
        raise ValueError(f"Incomplete {start} section")
    return text[left:right]


def spectrum(text):
    freq = values(text, "VIBRATIONAL FREQUENCIES", "NORMAL MODES")
    if not re.search(r"Scaling factor for frequencies\s*=\s*1\.0+\b", freq):
        raise ValueError("Frequency factor missing or different from 1")
    modes = {int(k): float(v) for k, v in re.findall(
        rf"(?m)^\s*(\d+):\s*({NUMBER})\s+cm\*\*-1", freq)}
    if set(modes) != set(range(12)):
        raise ValueError("Expected all 12 Cartesian frequency rows")
    ir = values(text, "IR SPECTRUM", None)
    if not re.search(r"freq\s+eps\s+Int\s+T\*\*2", ir) or "km/mol" not in ir:
        raise ValueError("Missing ORCA IR table headers")
    intensities = {int(k): (float(nu),float(inten)) for k, nu, inten in re.findall(
        rf"(?m)^\s*(\d+):\s*({NUMBER})\s+{NUMBER}\s+({NUMBER})\s+{NUMBER}\s+\(", ir)}
    block = values(text, "NORMAL MODES", "IR SPECTRUM")
    vectors = {i: {} for i in range(12)}
    cols = []
    for line in block.splitlines():
        fields = line.split()
        if fields and all(value.isdecimal() for value in fields):
            cols = list(map(int, fields))
        elif (cols and len(fields) == len(cols) + 1 and fields[0].isdecimal()
              and all(re.fullmatch(NUMBER, token) for token in fields[1:])):
            idx = int(fields[0])
            for mode, value in zip(cols, fields[1:]):
                vectors[mode][idx] = float(value)
    if any(set(vectors[i]) != set(range(12)) for i in range(6, 12)):
        raise ValueError("Incomplete Cartesian normal-mode vectors")
    geo = geometries(text)[-1][1]
    dist = math.dist(geo[1], geo[0])
    axis = [(o-c)/dist for o, c in zip(geo[1], geo[0])]
    projections = {}
    for mode in range(6, 12):
        v = vectors[mode]
        rel = sum((v[3+i]-v[i]) * axis[i] for i in range(3))
        projections[mode] = abs(rel) / math.sqrt(sum(x*x for x in v.values()))
    mode = max(projections, key=projections.get)
    if mode not in intensities or abs(intensities[mode][0] - modes[mode]) > 0.1:
        raise ValueError("IR and frequency tables disagree for C=O mode")
    negatives = sum(modes[i] < LIMIT["imaginary_cutoff_cm1"] for i in range(6, 12))
    return mode, modes[mode], intensities[mode][1], projections, negatives


def reference_output(ref):
    if ref.is_file():
        sources = [ref]
    else:
        sources = [x for x in ref.rglob("*") if x.is_file() and x.suffix.lower() in {".out", ".log"}]
    for path in sources:
        text = raw(path)
        if OPT_MARK in text and "VIBRATIONAL FREQUENCIES" in text and method_and_state(text):
            final_geometry(text)
            spectrum(text)
            return text
    raise ValueError("The independent native reference output is missing or incomplete")


def near(answer, expected, tolerance):
    return (type(answer) in (int,float) and math.isfinite(answer)
            and abs(answer - expected) <= tolerance)


def located(value, paths, terms):
    citation = json.dumps(value, ensure_ascii=False).lower()
    for path in paths:
        if path.name.lower() not in citation:
            continue
        lines = raw(path).splitlines()
        if any(term in citation and any(term in l.lower() for l in lines) for term in terms):
            return True
        ns = list(map(int, re.findall(r"(?<![\d.])\d+(?![\d.])", citation)))
        if len(ns) > 1 and min(ns) > 0 and max(ns)-min(ns) <= 100:
            ns = list(range(min(ns),max(ns)+1))
        for n in ns:
            if 0 < n <= len(lines):
                line = lines[n-1].lower()
                if any(term in line for term in terms):
                    return True
                # Numbered data rows under the cited header can also resolve the evidence.
                preceding = "\n".join(lines[max(0,n-35):n]).lower()
                if any(term in preceding for term in terms):
                    return True
    return False


def grade(work: Path, reference: Path) -> dict:
    work = Path(work).resolve()
    truth = reference_output(Path(reference).resolve())
    ref_mode, ref_nu, ref_int, _, ref_imag = spectrum(truth)
    rows = {key: outcome(False, "No supporting submission evidence") for key in CRITERIA}
    try:
        answer = json.loads((work / "result.json").read_text(encoding="utf-8-sig"))
        op = contained(work, answer["opt_output"])
        fp = contained(work, answer["freq_output"])
        opt, freq = raw(op), raw(fp)
        geo0 = xyz_rows(raw(HERE / "inputs/formaldehyde.xyz").splitlines()[2:])
        opt_geos = geometries(opt)
        freq_geos = geometries(freq)
        initial_delta = geom_difference(geo0, opt_geos[0][1])
        correct_settings = all(method_and_state(txt) for txt in (opt, freq))
        rows["conditions_and_provenance"] = outcome(
            correct_settings and initial_delta <= LIMIT["first_geometry_distance_angstrom"],
            f"Expected method, state, limits and normal SCF/ORCA exits={correct_settings}; "
            f"maximum initial pair-distance deviation={initial_delta:.6g} angstrom")
        geom = final_geometry(opt)
        freq_geo = freq_geos[-1][1]
        linked = geom_difference(geom, freq_geo)
        ref_delta = geom_difference(geom, final_geometry(truth))
        limit = LIMIT["geometry_distance_angstrom"]
        stationary = linked <= limit and ref_delta <= limit
        rows["optimization_and_final_geometry"] = outcome(
            stationary, f"Max distance deviations (opt vs freq; opt vs independent) "
            f"{linked:.6g}, {ref_delta:.6g} angstrom")
        mode, nu, inten, projection, imaginary = spectrum(freq)
        is_min = stationary and ref_imag == imaginary == 0
        rows["minimum_and_mode_counts"] = outcome(
            type(answer.get("internal_modes")) is int and answer["internal_modes"] == 6
            and type(answer.get("imaginary_modes")) is int and answer["imaginary_modes"] == imaginary
            and answer.get("local_minimum") is True and is_min,
            f"Six internal modes; {imaginary} significant imaginary modes; local minimum={is_min}")
        rows["carbonyl_mode"] = outcome(
            type(answer.get("mode_index")) is int and answer["mode_index"] == mode == ref_mode,
            f"Largest C–O projection mode from raw and independent outputs: {mode}, {ref_mode}")
        for key, field, measured, expected, printed, rerun in (
            ("wavenumber","wavenumber_cm1",nu,ref_nu,"reported_wavenumber_abs_cm1","recompute_wavenumber_abs_cm1"),
            ("intensity","intensity_km_mol",inten,ref_int,"reported_intensity_abs_km_mol","recompute_intensity_abs_km_mol"),
        ):
            rows[key] = outcome(near(answer.get(field), measured, LIMIT[printed])
                                and abs(measured-expected) <= LIMIT[rerun],
                                f"Submitted={answer.get(field)}, raw={measured}, independent={expected}")
        runnerup = sorted(projection.values(), reverse=True)[1]
        claim = str(answer.get("assignment_evidence", "")).lower()
        mentions_comparison = bool(re.search(
            r"最高|最大|最强|最显著|所有|其[他它]|次[高中大]|大于|高于|强于|领先|largest|highest|strongest|other|next|second|dominant",
            claim))
        rows["displacement_evidence"] = outcome(
            near(answer.get("bond_projection"), projection[mode], LIMIT["reported_bond_projection_abs"])
            and mentions_comparison,
            f"Bond-axis projection={projection[mode]:.6f}; next highest={runnerup:.6f}; "
            f"submitted={answer.get('bond_projection')}, comparison={mentions_comparison}")
        ev = answer.get("evidence", {})
        hess = [f for f in fp.parent.glob("*.hess") if f.is_file() and f.resolve().is_relative_to(work)]
        refs = {
            "optimization":([op],["the optimization has converged","final energy evaluation at the stationary point","geometry convergence"]),
            "frequencies":([fp],["vibrational frequencies","cm**-1"]),
            "ir":([fp],["ir spectrum","km/mol"]),
            "displacement":([fp,*hess],["normal modes","$normal_modes"]),
        }
        hits = {k: located(ev.get(k), paths, terms) for k, (paths, terms) in refs.items()}
        rows["source_citations"] = outcome(all(hits.values()), hits)
    except (AttributeError, IndexError, KeyError, OSError, StopIteration, TypeError, ValueError, ZeroDivisionError) as err:
        for item in rows.values():
            if item["detail"] == "No supporting submission evidence":
                item["detail"] = f"Missing or invalid evidence: {type(err).__name__}: {err}"
    return rows
