"""Independent evidence checks for the frozen fixed-geometry water comparison."""

import json
import math
import re
from decimal import Decimal
from pathlib import Path, PureWindowsPath


HERE = Path(__file__).resolve().parent
FACTOR = Decimal("2625.49964")
NUMBER = r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[Ee][+-]?\d+)?"
FINAL = re.compile(rf"^\s*FINAL SINGLE POINT ENERGY\s+({NUMBER})[ \t]*$", re.M)
CONVERGED = re.compile(r"\bSCF CONVERGED AFTER\s+\d+\s+CYCLES?\b", re.I)
TERMINATED = re.compile(r"\*+\s*ORCA TERMINATED NORMALLY\s*\*+", re.I)


def text_file(path):
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig", errors="replace").replace("\r", "")


def within(base, supplied):
    if not isinstance(supplied, str) or not supplied.strip() or PureWindowsPath(supplied).anchor:
        raise ValueError("Expected a nonempty relative path")
    actual = (base / supplied.replace("\\", "/")).resolve()
    if not actual.is_relative_to(base.resolve()) or not actual.is_file():
        raise ValueError(f"Missing file in trial: {supplied}")
    return actual


def fields(lines):
    return [" ".join(row.partition("#")[0].split()).casefold() for row in lines
            if row.partition("#")[0].strip()]


def atom(row):
    pieces = row.split()
    if len(pieces) != 4 or pieces[0].upper() not in ("O", "H"):
        raise ValueError("Bad coordinate row")
    return pieces[0].upper(), tuple(float(token) for token in pieces[1:])


def distances(atoms):
    oxygen = [p for symbol, p in atoms if symbol == "O"]
    hydrogens = [p for symbol, p in atoms if symbol == "H"]
    if len(oxygen) != 1 or len(hydrogens) != 2 or len(atoms) != 3:
        raise ValueError("Expected exactly one O and two H")
    return [*sorted(math.dist(oxygen[0], h) for h in hydrogens), math.dist(*hydrogens)]


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("Expected a JSON number")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Expected a finite number")
    return result


def parse_output(path, label, inp=None):
    raw = text_file(path)
    energy = list(FINAL.finditer(raw))
    scf = list(CONVERGED.finditer(raw))
    end = list(TERMINATED.finditer(raw))
    if not (len(energy) == len(scf) == len(end) == 1 and scf[0].start() < energy[0].start() < end[0].start()):
        raise ValueError("One converged SCF, one final electronic energy and normal termination must appear in order")
    echoed = [line for line in re.findall(r"^[ \t]*\|[ \t]*\d+>([^\n]*)", raw, re.M)
              if "END OF INPUT" not in line]
    contents = fields(echoed)
    if not contents or (inp is not None and contents != fields(text_file(inp).splitlines())):
        raise ValueError("The original ORCA input differs from the echoed input")
    directives = " ".join(contents)
    if re.search(r"\b(?:opt\w*|freq\w*|numfreq|cpcm\w*|smd\w*|cosmo\w*|alpb|cpcmc|qm/mm|pointcharges|d3\w*|d4)\b|\$new_job", directives):
        raise ValueError("The input includes geometry changes, another task, solvent or an added correction")
    settings = [r"Your calculation utilizes the basis:\s*def2-SVP\s*$",
                r"Exchange Functional\s+Exchange\s+\.+\s*PBE\s*$",
                r"Correlation Functional\s+Correlation\s+\.+\s*PBE\s*$",
                r"Fraction HF Exchange\s+ScalHFX\s+\.+\s*0\.250*\s*$",
                r"Total Charge\s+Charge\s+\.+\s*0\s*$",
                r"Multiplicity\s+Mult\s+\.+\s*1\s*$"]
    if not all(re.search(pattern, raw, re.I | re.M) for pattern in settings):
        raise ValueError("Missing PBE0, def2-SVP, neutral or singlet evidence in ORCA settings")
    for key, limit in {"TolE":1e-8, "TolG":1e-5, "TolX":1e-5, "TolErr":5e-7}.items():
        vals = [float(v) for v in re.findall(rf"\b{key}[ \t]+\.+[ \t]+({NUMBER})", raw, re.I)]
        if len(vals) != 1 or not 0 < vals[0] <= limit * (1 + 1e-10):
            raise ValueError(f"Missing or looser-than-TightSCF threshold: {key}")
    geometry = re.search(r"^CARTESIAN COORDINATES \(ANGSTROEM\)\s*\n-+\s*\n((?:[ \t]*[A-Za-z]+[ \t]+[^\n]+\n)+)", raw, re.M)
    if not geometry:
        raise ValueError("No Cartesian geometry table in angstroms")
    observed = distances([atom(r) for r in geometry[1].strip().splitlines()])
    xyz = text_file(HERE / "inputs" / f"{label}.xyz")
    expected = distances([atom(r) for r in xyz.splitlines()[2:] if r.strip()])
    if not all(abs(a-b) <= 0.000004 for a,b in zip(observed, expected)):
        raise ValueError("The evaluated geometry does not match the supplied structure")
    return Decimal(energy[0][1])


def grade(work: Path, reference: Path) -> dict:
    work, reference = Path(work), Path(reference)
    refs = {s:parse_output(reference / f"{s}.out", s) for s in "AB"}
    verdicts = {}

    def mark(name, passed, detail):
        verdicts[name] = {"pass": bool(passed), "detail": detail}

    try:
        obj = json.loads(text_file(work / "result.json"), parse_float=Decimal)
        if not isinstance(obj, dict):
            raise ValueError("Expected a JSON object")
        mark("submission", True, "Readable result.json")
    except (OSError, ValueError) as error:
        mark("submission", False, str(error))
        obj = {}
    raw_energy, paths = {}, {}
    for s in "AB":
        try:
            job = obj[s]
            inp, out = (within(work, job[key]) for key in ("input", "output"))
            paths[s] = (inp, out)
            if inp == out:
                raise ValueError("Duplicated input and output")
            actual = parse_output(out, s, inp)
            raw_energy[s] = actual
            mark(f"{s}_calculation", True, f"{out.relative_to(work.resolve())}, E={actual} Eh")
        except (OSError, ValueError, KeyError, TypeError) as error:
            mark(f"{s}_calculation", False, str(error))
        try:
            reported = number(obj[s]["energy_hartree"])
            actual = raw_energy[s]
            ok = abs(reported-actual) <= Decimal("2e-8") and abs(actual-refs[s]) <= Decimal("2e-6")
            mark(f"{s}_energy", ok, f"reported={reported}, raw={actual}, reference={refs[s]} Eh")
        except (KeyError, ValueError, TypeError) as error:
            mark(f"{s}_energy", False, f"Missing usable energy or raw evidence: {error}")
    separate = len(paths) == 2 and len(set(paths["A"] + paths["B"])) == 4
    mark("separate_jobs", separate, f"Number of complete input and output pairs: {len(paths)}")
    reference_delta = refs["B"] - refs["A"]
    for unit, factor, raw_tol, ref_tol in [
        ("hartree", Decimal(1), Decimal("3e-8"), Decimal("4e-6")),
        ("kJ_mol", FACTOR, Decimal("0.003"), Decimal("0.012")),
    ]:
        try:
            answer = number(obj["delta_E_B_minus_A_" + unit])
            raw = (raw_energy["B"] - raw_energy["A"]) * factor
            target = reference_delta * factor
            ok = abs(answer - raw) <= raw_tol and abs(answer - target) <= ref_tol
            mark("signed_delta_" + unit, ok, f"reported={answer}, raw difference={raw}, reference={target}")
        except (KeyError, ValueError, TypeError) as error:
            mark("signed_delta_" + unit, False, f"Missing signed difference or underlying energy: {error}")
    lower = "A" if reference_delta > 0 else "B"
    mark("lower_energy_geometry", obj.get("lower_energy_geometry") == lower,
         f"reported={obj.get('lower_energy_geometry')}, independently lower={lower}")
    return verdicts
