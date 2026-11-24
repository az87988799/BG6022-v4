"""Independent ORCA 6 validator for the fixed-geometry formaldehyde trial."""

import json
import math
import re
from pathlib import Path


NUM = r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[Ee][+-]?\d+)?"
LENGTH_TABLE = "ABSORPTION SPECTRUM VIA TRANSITION ELECTRIC DIPOLE MOMENTS"
ROW = re.compile(
    rf"^\s*0-1\S*\s*->\s*(\d+)-1\S*\s+({NUM})\s+{NUM}\s+{NUM}\s+({NUM})\s+"
)
ENERGY_ATOL = 0.002
FOSC_ATOL = 0.00005
DARK_CUTOFF = 0.00001
ROOTS = list(range(1, 7))
CHECKS = (
    "reference", "provenance", "fixed_conditions", "computed_spectrum",
    "reported_values", "source_lines", "root_assignments", "interpretation",
)


def read_text(path):
    return path.read_text(encoding="utf-8-sig", errors="replace")


def file_in(work, relative):
    if not isinstance(relative, str) or not relative.strip() or Path(relative).is_absolute():
        raise ValueError("Expected a relative evidence path")
    path = (work / relative).resolve()
    if not path.is_relative_to(work.resolve()) or not path.is_file():
        raise ValueError(f"Missing or outside trial: {relative}")
    return path


def spectrum(text):
    lines = text.splitlines()
    heads = [i for i, line in enumerate(lines) if line.strip() == LENGTH_TABLE]
    if len(heads) != 1:
        raise ValueError(f"Found {len(heads)} electric-dipole tables")
    found = {}
    start = heads[0]
    if not any("fosc(D2)" in line for line in lines[start:start + 6]):
        raise ValueError("Length-form oscillator-strength header missing")
    for i in range(start + 1, len(lines)):
        m = ROW.match(lines[i])
        if m:
            k = int(m[1])
            if k in found:
                raise ValueError("Duplicate root in length table")
            found[k] = (float(m[2]), float(m[3]), i + 1)
        elif found:
            break
    if list(found) != ROOTS:
        raise ValueError(f"Expected six ordered transitions, found {list(found)}")
    if not re.search(r"EXCITED STATES\s*\(SINGLETS\)", text, re.I):
        raise ValueError("Singlet excited-state section missing")
    if not re.search(r"SCF CONVERGED AFTER", text) or "ORCA TERMINATED NORMALLY" not in text:
        raise ValueError("SCF convergence or normal termination missing")
    if "ORCA finished by error termination" in text:
        raise ValueError("ORCA reported error termination")
    return found


def echoed_input(text):
    return "\n".join(
        m[1] for m in re.finditer(r"^\|\s*\d+>\s?(.*)$", text, re.M)
        if "****END OF INPUT****" not in m[1]
    )


def normalized_input(text):
    lines = (s.partition("#")[0].strip().lower().split() for s in text.splitlines())
    return [" ".join(words) for words in lines if words]


def coordinates(text):
    lines = text.splitlines()
    geometry = []
    pos = next(i for i, s in enumerate(lines) if s.strip() == "CARTESIAN COORDINATES (ANGSTROEM)")
    for line in lines[pos + 1:]:
        tokens = line.split()
        if len(tokens) == 4 and re.fullmatch(r"[A-Z][a-z]?", tokens[0]):
            geometry.append((tokens[0], *map(float, tokens[1:])))
        elif geometry:
            break
    return geometry


def conditions(raw, inp):
    geometry_file = Path(__file__).resolve().parent / "inputs" / "formaldehyde.xyz"
    expected = [s.split() for s in read_text(geometry_file).splitlines()[2:] if s.strip()]
    got = coordinates(raw)
    geom_ok = len(got) == len(expected) and all(
        a[0] == b[0] and all(abs(a[i] - float(b[i])) <= 2e-5 for i in (1, 2, 3))
        for a, b in zip(got, expected)
    )
    spec = " ".join(normalized_input(inp))
    keywords = " ".join(s.lstrip("!") for s in normalized_input(inp) if s.startswith("!"))
    terms = set(keywords.split())
    protocols = all(re.search(p, spec) for p in (
        r"\btda\s+true\b", r"\bnroots\s+6\b", r"%maxcore\s+192\b",
    )) and not re.search(r"\btda\s+false\b|\btriplets\s+true\b", spec)
    parallel = re.findall(r"\bnprocs\s+(\d+)\b|\bpal(\d+)\b", spec)
    ncores_ok = all(int(a or b) == 1 for a, b in parallel)
    fixed = not re.search(r"\b(?:opt|tightopt|looseopt|verytightopt|optts|cpcm|smd|alpb|cosmo)\b", spec)
    state = re.search(r"Total Charge\s+Charge\s+\.+\s+0\b", raw) and re.search(
        r"Multiplicity\s+Mult\s+\.+\s+1\b", raw
    )
    flags = {
        "geometry": geom_ok, "charge_and_spin": bool(state), "method_basis_scf":
        {"pbe0", "def2-svp", "tightscf"} <= terms, "tda_roots_memory": protocols,
        "single_process": ncores_ok, "gas_fixed_geometry": fixed,
    }
    return all(flags.values()), ", ".join(f"{k}={v}" for k, v in flags.items())


def close(a, b):
    return abs(a[0] - b[0]) <= ENERGY_ATOL and abs(a[1] - b[1]) <= FOSC_ATOL


def grade(work: Path, reference: Path) -> dict:
    report = {key: {"pass": False, "detail": "Evidence unavailable"} for key in CHECKS}

    def set_check(key, passed, detail):
        report[key] = {"pass": bool(passed), "detail": str(detail)}

    try:
        ref = spectrum(read_text(reference / "job.out"))
        set_check("reference", True, "Independent run has six converged singlet transitions")
    except (OSError, ValueError) as exc:
        ref = {}
        set_check("reference", False, exc)
    try:
        submission = json.loads(read_text(work / "result.json"))
        raw_file = file_in(work, submission["raw_output"])
        inp_file = file_in(work, submission["raw_input"])
        raw = read_text(raw_file)
        native = read_text(inp_file)
        echo = echoed_input(raw)
        matching = normalized_input(echo) == normalized_input(native)
        normal = "ORCA TERMINATED NORMALLY" in raw and bool(re.search(r"Program Version\s+6\.", raw))
        set_check("provenance", raw_file != inp_file and matching and normal,
                  f"Input/echo match={matching}, ORCA 6 normal termination={normal}")
        try:
            passed, detail = conditions(raw, echo)
            set_check("fixed_conditions", passed, detail)
        except (StopIteration, OSError, ValueError) as exc:
            set_check("fixed_conditions", False, f"Geometry or conditions unreadable: {exc}")
        actual = spectrum(raw)
        reference_match = bool(ref) and all(close(actual[k], ref[k]) for k in ROOTS)
        set_check("computed_spectrum", reference_match, f"Independent raw-spectrum agreement={reference_match}")
        rows = submission["rows"]
        if not isinstance(rows, list) or [r["root"] for r in rows] != ROOTS:
            raise ValueError("Expected rows in order 1 through 6")
        numbers = [(r["energy_eV"], r["fosc_length"]) for r in rows]
        finite = all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for pair in numbers for v in pair)
        values = finite and bool(ref) and all(
            close(v, actual[k]) and close(v, ref[k]) for k, v in zip(ROOTS, numbers)
        ) and all(numbers[i][0] <= numbers[i+1][0] for i in range(5))
        set_check("reported_values", values, f"Six energy/strength pairs within both tolerances={values}")
        lines = all(type(r["line"]) is int and r["line"] == actual[k][2] for k, r in zip(ROOTS, rows))
        set_check("source_lines", lines, f"All six citations point to the corresponding length rows={lines}")
        lo, hi, dark = (submission[key] for key in ("lowest_root", "brightest_root", "near_dark_root"))
        assigned = all(type(k) is int and k in ROOTS for k in (lo, hi, dark))
        assigned = assigned and finite and bool(ref) and all(
            lo == min(spec, key=lambda k: spec[k][0]) and hi == max(spec, key=lambda k: spec[k][1])
            and spec[dark][1] <= DARK_CUTOFF for spec in (actual, ref)
        ) and numbers[dark-1][1] <= DARK_CUTOFF
        set_check("root_assignments", assigned, f"Lowest/brightest/near-dark supported={assigned}")
        note = submission.get("interpretation", "")
        words = isinstance(note, str) and all(re.search(regex, note, re.I) for regex in (
            r"能量|激发能|最低能|能隙|energ", r"吸收|振子|跃迁偶极|强度|强弱|intens|oscillat|dipole|probabilit|absorp",
            r"六|6|six|本次计算|所算|computed roots|calculated roots",
        ))
        set_check("interpretation", words, "Terminology and finite scope checked; semantic quality requires review")
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        for v in report.values():
            if v["detail"] == "Evidence unavailable":
                v["detail"] = f"Missing or malformed submitted evidence: {exc}"
    return report
