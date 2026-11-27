"""Run a local ORCA teaching job or inspect its existing output."""

import argparse
import codecs
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from orca_evidence import parse_output

ELEMENTS = (
    "H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co "
    "Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb "
    "Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re "
    "Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es "
    "Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og"
).split()
ATOMIC_NUMBERS = {symbol: number for number, symbol in enumerate(ELEMENTS, 1)}
TASK_KEYWORDS = {"sp": "SP", "opt": "Opt", "freq": "Freq", "optfreq": "Opt Freq"}
DFT_METHODS = ("PBE0", "B3LYP", "CAM-B3LYP")
METHODS = (*DFT_METHODS, "r2SCAN-3c")
BASES = ("def2-SVP", "def2-SVPD", "def2-TZVP", "def2-TZVPD")
READ_TASKS = (*TASK_KEYWORDS, "tddft")


@dataclass(frozen=True)
class Atom:
    symbol: str
    x: float
    y: float
    z: float


def decode_source(raw: bytes) -> str:
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise ValueError("expected a positive integer")
    return number


def parse_atom(line: str) -> Atom:
    fields = line.split()
    if len(fields) != 4 or fields[0].capitalize() not in ATOMIC_NUMBERS:
        raise ValueError(f"expected an element symbol and three Cartesian coordinates: {line!r}")
    coords = [float(value) for value in fields[1:]]
    if not all(math.isfinite(value) for value in coords):
        raise ValueError(f"coordinates must be finite: {line!r}")
    return Atom(fields[0].capitalize(), *coords)


def parse_xyz(raw: bytes) -> list[Atom]:
    lines = decode_source(raw).splitlines()
    if len(lines) < 3:
        raise ValueError("XYZ needs an atom count, a comment line, and coordinates in angstroms")
    count = positive_int(lines[0])
    comment = lines[1].strip()
    units = re.search(r"""\bunits?\s*[=:]\s*["']?([\w.ÅÅ-]+)""", comment, flags=re.I)
    other_units = re.search(r"\b(?:bohrs?|atomic units?|nanomet(?:er|re)s?|nm|pm)\b|\ba\.u\.", comment, re.I)
    if units:
        accepted = {"a", "å", "ang", "angs", "angstrom", "angstroms", "ångström", "ångströms"}
        if units[1].casefold() not in accepted:
            raise ValueError(f"the XYZ declares {units[1]!r}; supply Cartesian coordinates in angstroms")
    elif other_units:
        raise ValueError("the XYZ comment indicates other units; supply Cartesian coordinates in angstroms")
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) - 2 != count:
        raise ValueError(f"XYZ declares {count} atoms but contains {len(lines) - 2} coordinate lines")
    return [parse_atom(line) for line in lines[2:]]


def geometry_block(atoms: list[Atom], charge: int, mult: int) -> list[str]:
    electrons = sum(ATOMIC_NUMBERS[atom.symbol] for atom in atoms) - charge
    if electrons < mult - 1 or electrons < 1 or (electrons - (mult - 1)) % 2:
        raise ValueError(f"{electrons} electrons are incompatible with multiplicity {mult}")
    return [
        f"* xyz {charge} {mult}",
        *(f"{atom.symbol} {atom.x} {atom.y} {atom.z}" for atom in atoms),
        "*",
    ]


def template_input(args, raw: bytes) -> tuple[list[str], dict]:
    if args.task not in TASK_KEYWORDS or not args.method or args.charge is None or args.mult is None:
        raise ValueError("--xyz requires --task (sp/opt/freq/optfreq), --method, --charge and --mult")
    if args.method == "r2SCAN-3c" and args.basis:
        raise ValueError("r2SCAN-3c includes its basis; omit --basis")
    if args.method != "r2SCAN-3c" and not args.basis:
        raise ValueError(f"specify --basis for {args.method}")
    if args.opt_maxiter is not None and args.task not in ("opt", "optfreq"):
        raise ValueError("--opt-maxiter applies to opt or optfreq")
    if args.temperature is not None:
        if args.task not in ("freq", "optfreq"):
            raise ValueError("--temperature applies to freq or optfreq thermochemistry")
        if not math.isfinite(args.temperature) or args.temperature <= 0:
            raise ValueError("--temperature must be a finite positive Kelvin temperature")
    atoms = parse_xyz(raw)
    cores = args.cores or 1
    maxcore = args.maxcore_mb or 512
    words = [args.method, *([args.basis] if args.basis else []), TASK_KEYWORDS[args.task]]
    tightscf = getattr(args, "tightscf", False)
    if tightscf:
        words.append("TightSCF")
    lines = ["! " + " ".join(words), f"%maxcore {maxcore}"]
    if cores > 1:
        lines.append(f"%pal nprocs {cores} end")
    if args.scf_maxiter is not None:
        lines.append(f"%scf MaxIter {args.scf_maxiter} end")
    if args.opt_maxiter is not None:
        lines.append(f"%geom MaxIter {args.opt_maxiter} end")
    if args.temperature is not None:
        lines.append(f"%freq Temp {args.temperature} end")
    lines.extend(geometry_block(atoms, args.charge, args.mult))
    conditions = {
        "task": args.task, "method": args.method, "basis": args.basis,
        "charge": args.charge, "multiplicity": args.mult, "cores": cores,
        "maxcore_mb_per_process": maxcore, "temperature_setting_K": args.temperature,
        "scf_maxiter": args.scf_maxiter, "opt_maxiter": args.opt_maxiter,
        "geometry_unit": "angstrom", "atom_count": len(atoms), "medium": "gas",
    }
    if tightscf:
        conditions["scf_convergence_keyword"] = "TightSCF"
    return lines, conditions


def native_input(raw: bytes) -> tuple[list[str], dict]:
    """Parse the supported single-job TD-DFT grammar, then render only checked tokens."""
    clean = (line.partition("#")[0].strip() for line in decode_source(raw).splitlines())
    lines = iter(line for line in clean if line)
    blocks = {}
    atoms = []
    lookup = {word.lower(): word for word in (
        *DFT_METHODS, *BASES, "SP", "TightSCF", "VeryTightSCF", "RIJCOSX", "def2/J",
    )}
    for line in lines:
        words = line.lower().split()
        kind = "!" if line.startswith("!") else words[0]
        if kind in blocks:
            raise ValueError(f"duplicate native input section {kind}")
        if kind == "!":
            keywords = line[1:].lower().split()
            for keyword in keywords:
                if keyword not in lookup:
                    raise ValueError(f"unsupported native keyword {keyword!r}")
            blocks[kind] = [lookup[keyword] for keyword in keywords]
        elif kind == "%maxcore" and len(words) == 2:
            blocks[kind] = positive_int(words[1])
        elif kind in ("%tddft", "%pal"):
            while words[-1] != "end":
                more = next(lines, None)
                if more is None:
                    raise ValueError(f"missing end for {kind}")
                words.extend(more.lower().split())
            tokens = words[1:-1]
            if not tokens or len(tokens) % 2:
                raise ValueError(f"expected name/value pairs inside {kind}")
            options = {}
            for key, value in zip(tokens[::2], tokens[1::2]):
                if key in options:
                    raise ValueError(f"duplicate {kind} option {key}")
                if (kind, key) in (("%tddft", "nroots"), ("%pal", "nprocs")):
                    options[key] = positive_int(value)
                elif kind == "%tddft" and key == "tda" and value in ("true", "false"):
                    options[key] = value
                elif kind == "%tddft" and (key, value) in (("triplets", "false"), ("singlets", "true")):
                    options[key] = value
                else:
                    raise ValueError(f"unsupported {kind} option {key} {value}")
            blocks[kind] = options
        elif kind == "*" and len(words) == 4 and words[1] == "xyz":
            charge, mult = int(words[2]), positive_int(words[3])
            for row in lines:
                if row == "*":
                    break
                atoms.append(parse_atom(row))
            else:
                raise ValueError("missing * at the end of the inline geometry")
            if not atoms or mult != 1:
                raise ValueError("native singlet TD-DFT requires inline coordinates and multiplicity 1")
            blocks[kind] = geometry_block(atoms, charge, mult)
        else:
            raise ValueError(f"unsupported native input section {kind!r}; use one inline * xyz job")
    keywords = blocks.get("!", [])
    methods = [word for word in keywords if word in METHODS]
    bases = [word for word in keywords if word in BASES]
    td = blocks.get("%tddft", {})
    if len(methods) != 1 or len(bases) != 1 or "nroots" not in td or "*" not in blocks:
        raise ValueError("native TD-DFT requires one method/basis, %tddft nroots and an inline * xyz block")
    # The parser accepts the legacy spelling only for a restricted singlet.
    # ORCA computes singlets by default and rejects "singlets true" in %tddft.
    td.pop("singlets", None)
    rendered = ["! " + " ".join(keywords)]
    if "%maxcore" in blocks:
        rendered.append(f"%maxcore {blocks['%maxcore']}")
    cores = blocks.get("%pal", {}).get("nprocs", 1)
    if cores > 1:
        rendered.append(f"%pal nprocs {cores} end")
    rendered.extend(["%tddft", *(f"  {key} {value}" for key, value in td.items()), "end", *blocks["*"]])
    return rendered, {
        "task": "tddft", "method": methods[0], "basis": bases[0], "charge": charge,
        "multiplicity": mult, "atom_count": len(atoms), "geometry_unit": "angstrom",
        "medium": "gas", "cores": cores, "maxcore_mb_per_process": blocks.get("%maxcore"),
        "tddft": td,
    }


def locate_orca(explicit: Path | None) -> Path:
    location = explicit or os.environ.get("ORCA_EXE") or shutil.which("orca")
    if not location:
        raise ValueError("ORCA was not found; pass --orca or set ORCA_EXE to the executable path")
    executable = Path(location).expanduser().resolve()
    if not executable.is_file():
        raise ValueError(f"ORCA executable was not found at {executable}")
    return executable


def output_digest(output: Path) -> str:
    digest = hashlib.sha256()
    with output.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_job(output: Path, task: str) -> dict:
    code = None
    receipt_path = output.with_name("result.json")
    if output.name == "job.out" and receipt_path.is_file():
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            receipt = {}
        if isinstance(receipt, dict) and type(receipt.get("process_exit_code")) is int:
            if receipt.get("output_sha256") == output_digest(output):
                code = receipt["process_exit_code"]
    return parse_output(output, code, task)


def run_job(args) -> dict:
    if args.input:
        extra = [getattr(args, field) for field in
                 ("method", "basis", "charge", "mult", "cores", "maxcore_mb", "temperature",
                  "scf_maxiter", "opt_maxiter")]
        if (any(value is not None for value in extra) or getattr(args, "tightscf", False)
                or args.task not in (None, "tddft")):
            raise ValueError("--input takes its method, task and conditions from the native file")
        raw = args.input.read_bytes()
        lines, conditions = native_input(raw)
        source_name = "source.inp"
    else:
        raw = args.xyz.read_bytes()
        lines, conditions = template_input(args, raw)
        source_name = "input.xyz"
    executable = locate_orca(args.orca)
    directory = args.out_dir.resolve()
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise ValueError(f"job directory already exists: {directory}; choose a new --out-dir") from None
    print(f"ORCA job: {directory}", file=sys.stderr, flush=True)
    (directory / source_name).write_bytes(raw)
    (directory / "job.inp").write_text("\n".join(lines) + "\n", encoding="ascii")
    output = directory / "job.out"
    launch_error = None
    with output.open("wb") as stream:
        try:
            process = subprocess.run(
                [str(executable), "job.inp"], cwd=directory, stdin=subprocess.DEVNULL,
                stdout=stream, stderr=subprocess.STDOUT, check=False,
            )
            exit_code = process.returncode
        except OSError as exc:
            launch_error = str(exc)
            exit_code = None
    result = parse_output(output, exit_code, conditions["task"])
    result.update(job_directory=str(directory), submitted_conditions=conditions,
                  output_sha256=output_digest(output))
    if launch_error:
        result["launch_error"] = launch_error
        result["missing"].append("ORCA could not start; check the selected executable and its runtime")
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    if os.name == "nt" and not sys.stderr.isatty():
        # Both chat hosts collect piped progress as UTF-8 on Windows.
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(required=True, dest="command")
    run = commands.add_parser(
        "run", help="start one calculation in a new job directory",
        epilog=("Native --input accepts a closed-shell, single-job TD-DFT input. Give one "
                "! method/basis line, %tddft nroots N [tda true|false] end, and inline "
                "* xyz CHARGE 1 coordinates. Optional %maxcore and %pal nprocs blocks "
                "set resources. Closed-shell singlets are the default. A legacy singlets true "
                "line is omitted from job.inp; triplets false is preserved. "
                "Resolve external geometry locally and inline the coordinates before submitting."),
    )
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--xyz", type=Path, help="XYZ geometry in angstroms")
    source.add_argument("--input", type=Path, help="single-job TD-DFT input with inline coordinates")
    run.add_argument("--out-dir", type=Path, required=True)
    run.add_argument("--orca", type=Path, help="ORCA executable; otherwise use ORCA_EXE or PATH")
    run.add_argument("--task", choices=READ_TASKS)
    run.add_argument("--method", choices=METHODS)
    run.add_argument("--basis", choices=BASES)
    run.add_argument("--charge", type=int)
    run.add_argument("--mult", type=positive_int)
    run.add_argument("--cores", type=positive_int, help="ORCA processes (XYZ default 1)")
    run.add_argument("--maxcore-mb", type=positive_int, help="MB per ORCA process (XYZ default 512)")
    run.add_argument("--temperature", type=float, help="set the thermochemistry temperature in kelvin")
    run.add_argument("--tightscf", action="store_true",
                     help="add explicit TightSCF to the XYZ job (otherwise use ORCA defaults)")
    run.add_argument("--scf-maxiter", type=positive_int, help="set an explicit SCF iteration limit")
    run.add_argument("--opt-maxiter", type=positive_int, help="set an explicit geometry optimization iteration limit")
    read = commands.add_parser("read", help="inspect an existing output and associated evidence")
    read.add_argument("--output", required=True, type=Path)
    read.add_argument("--task", required=True, choices=READ_TASKS)
    args = parser.parse_args()
    try:
        result = (run_job(args) if args.command == "run"
                  else read_job(args.output.resolve(strict=True), args.task))
    except (ValueError, OSError) as exc:
        parser.exit(2, f"{parser.prog}: {exc}\n")
    print(json.dumps(result, indent=2))
    return 0 if result["checks"]["task_verified"] else 1


if __name__ == "__main__":
    sys.exit(main())
