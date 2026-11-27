"""Evidence reader for single-job ORCA output. The requested task is a check, not evidence."""

import codecs
import io
import math
import re
from collections import defaultdict
from pathlib import Path


NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"
ENERGY = re.compile(rf"FINAL SINGLE POINT ENERGY\s+({NUMBER})")
SCF = re.compile(r"SCF (NOT )?CONVERGED AFTER\s+\d+\s+CYCLES?", re.I)
COORD = re.compile(rf"([A-Z][a-z]?)\s+({NUMBER})\s+({NUMBER})\s+({NUMBER})")
FREQ = re.compile(rf"(\d+):\s*({NUMBER})\s+cm\*\*-1(?:\s+.*)?")
IR = re.compile(rf"(\d+):\s*({NUMBER})\s+{NUMBER}\s+({NUMBER})\s+")
TRANSITION = re.compile(
    rf"(0-1[\w'\".+-]+)\s+->\s+(\d+-1[\w'\".+-]+)\s+"
    rf"({NUMBER})\s+{NUMBER}\s+({NUMBER})\s+({NUMBER})\s+"
)
HEADINGS = {
    "INPUT FILE": "input",
    "CARTESIAN COORDINATES (ANGSTROEM)": "coordinates",
    "THE OPTIMIZATION HAS CONVERGED": "opt_convergence",
    "FINAL ENERGY EVALUATION AT THE STATIONARY POINT": "opt_evaluation",
    "OPTIMIZATION RUN DONE": "opt_end",
    "VIBRATIONAL FREQUENCIES": "freq",
    "IR SPECTRUM": "ir",
    "ABSORPTION SPECTRUM VIA TRANSITION ELECTRIC DIPOLE MOMENTS": "transitions",
    "ORCA TERMINATED NORMALLY": "normal",
}


def _index(lines):
    marks = defaultdict(list)
    conditions = {"keywords": [], "input_echo_lines": []}
    for i, line in enumerate(lines):
        heading = line.strip("* ").strip()
        key = HEADINGS.get(heading)
        if key:
            marks[key].append(i)
        elif SCF.fullmatch(heading):
            marks["scf"].append(i)
        elif line.startswith("FINAL SINGLE POINT ENERGY"):
            marks["energy"].append(i)
        elif re.match(r"Program Version\s+\d", line):
            marks["version"].append(i)
        elif re.fullmatch(r"O\s+R\s+C\s+A", heading):
            marks["banner"].append(i)
        elif heading.startswith("GEOMETRY OPTIMIZATION CYCLE"):
            marks["opt_cycle"].append(i)
        elif heading.lower().startswith("the optimization did not converge"):
            marks["opt_failure"].append(i)
        elif re.match(r"JOB NUMBER\s+\d+", heading, re.I):
            marks["job_number"].append(i)
        elif line.startswith("THERMOCHEMISTRY AT "):
            marks["thermo"].append(i)
        elif line.startswith("Magnitude (Debye)"):
            marks["dipole"].append(i)
        elif m := re.match(r"Number of atoms\s+\.{2,}\s*(\d+)\s*$", line):
            conditions["atom_count"] = int(m[1])
        elif m := re.match(r"Number of roots to be determined\s+\.{2,}\s*(\d+)\s*$", line):
            conditions["roots"] = int(m[1])
        elif m := re.match(r"Total Charge\s+Charge\s+\.{2,}\s*(-?\d+)\s*$", line):
            conditions["charge"] = int(m[1])
        elif m := re.match(r"Multiplicity\s+Mult\s+\.{2,}\s*(\d+)\s*$", line):
            conditions["multiplicity"] = int(m[1])
        elif m := re.match(r"\|\s*\d+>\s*(.*)$", line):
            conditions["input_echo_lines"].append(m[1])
            entry = m[1].partition("#")[0].strip()
            if entry.startswith("!"):
                conditions["keywords"].extend(entry[1:].split())
            elif re.search(r"\$(?:new_?job)|^\s*%compound\b", entry, re.I):
                marks["multi_input"].append(i)
            if re.search(r"\bconstraints\b", entry, re.I):
                marks["constraints"].append(i)
    return marks, conditions


def _coordinates(lines, start):
    atoms = []
    for line in lines[start + 1:]:
        if not atoms and (not line or set(line) == {"-"}):
            continue
        match = COORD.fullmatch(line)
        if not match:
            break
        xyz = [float(x) for x in match.groups()[1:]]
        if not all(map(math.isfinite, xyz)):
            return []
        atoms.append([match[1], *xyz])
    return atoms


def _rigid_count(atoms):
    """Rank of translations/rotations, using Cartesian collinearity at printed precision."""
    if len(atoms) == 1:
        return 3
    origin = atoms[0][1:]
    vectors = [[a - b for a, b in zip(atom[1:], origin)] for atom in atoms[1:]]
    axis = max(vectors, key=lambda v: math.hypot(*v))
    length = math.hypot(*axis)
    if not length:
        return None
    ux, uy, uz = (c / length for c in axis)
    for x, y, z in vectors:
        if math.hypot(y * uz - z * uy, z * ux - x * uz, x * uy - y * ux) > 1e-5:
            return 6
    return 5


def _wave_data(lines, start, atoms):
    rows = []
    scale = None
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if match := FREQ.fullmatch(line):
            rows.append({"mode": int(match[1]), "value": float(match[2]), "line": i + 1})
        elif not rows and (match := re.match(rf"Scaling factor for frequencies\s*=\s*({NUMBER})", line)):
            scale = float(match[1])
        elif rows or (line and set(line) != {"-"}):
            break
    rigid = _rigid_count(atoms) if atoms else None
    expected = 3 * len(atoms) if atoms else None
    complete = bool(
        expected and rigid is not None
        and [r["mode"] for r in rows] == list(range(expected))
        and all(math.isfinite(r["value"]) for r in rows)
        and all(abs(r["value"]) <= 0.01 for r in rows[:rigid])
    )
    return {
        "unit": "cm^-1", "rows": rows, "rigid_mode_count": rigid,
        "expected_internal_modes": expected - rigid if rigid is not None else None,
        "scale_factor": scale,
    }, complete


def _table_rows(lines, start, pattern, header_tokens, unit_tokens):
    """Check the column names and units in this section before reading consecutive rows."""
    header = unit_line = None
    for i in range(start + 1, min(start + 9, len(lines))):
        if lines[i].split()[:len(header_tokens)] == header_tokens:
            header = i
        if lines[i].split()[:len(unit_tokens)] == unit_tokens:
            unit_line = i
        match = pattern.match(lines[i])
        if match:
            if header is None or unit_line is None or not header < unit_line < i:
                return
            for j in range(i, len(lines)):
                match = pattern.match(lines[j])
                if not match:
                    break
                yield j, match
            return


def _thermochemistry(lines, start, stop):
    temperature = pressure = energy = None
    model = {}
    for i in range(start + 1, stop):
        line = lines[i]
        if m := re.fullmatch(rf"Temperature\s+\.{{2,}}\s+({NUMBER}) K", line):
            temperature = float(m[1])
        elif m := re.fullmatch(rf"Pressure\s+\.{{2,}}\s+({NUMBER}) atm", line):
            pressure = float(m[1])
        elif m := re.fullmatch(rf"Total Mass\s+\.{{2,}}\s+({NUMBER}) AMU", line):
            model["mass_amu"] = float(m[1])
        elif m := re.fullmatch(r"Quasi RRHO\s+\.{2,}\s+(True|False)", line):
            model["quasi_rrho"] = m[1] == "True"
        elif m := re.fullmatch(rf"Cut-Off Frequency\s+\.{{2,}}\s+({NUMBER}) cm\^-1", line):
            model["cutoff_cm1"] = float(m[1])
        elif m := re.fullmatch(r"Point Group:\s*([^,]+),\s*Symmetry Number:\s*(\d+)", line):
            model.update(point_group=m[1].strip(), symmetry_number=int(m[2]))
        elif m := re.fullmatch(rf"Final Gibbs free energy\s+\.{{2,}}\s+({NUMBER}) Eh", line):
            energy = {"unit": "Eh", "value": m[1], "line": i + 1}
    if energy and temperature and pressure:
        return dict(energy, temperature_K=temperature, pressure_atm=pressure, **model)


def parse_output(output: Path, exit_code: int | None, task: str) -> dict:
    """Inspect a single ORCA output and preserve its original decimal energy and line numbers."""
    with output.open("rb") as raw:
        encoding = "utf-16" if raw.peek(2)[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE) else "utf-8-sig"
        with io.TextIOWrapper(raw, encoding=encoding, errors="replace") as stream:
            lines = [line.strip() for line in stream]
    marks, conditions = _index(lines)
    evidence = {}
    quantities = {}
    missing = []

    def cite(key, i):
        evidence[key] = {"file": output.name, "line": i + 1, "text": lines[i]}

    def after(key, start, stop):
        return [i for i in marks[key] if start < i < stop]

    for key in ("normal", "version", "opt_convergence", "opt_failure", "freq", "ir", "transitions", "input"):
        if marks[key]:
            cite({"normal": "normal_termination", "version": "orca_version"}.get(key, key), marks[key][-1])
    version = lines[marks["version"][0]].split()[2] if marks["version"] else None
    normal = len(marks["normal"]) == 1
    end = marks["normal"][0] if marks["normal"] else len(lines)
    ambiguous = any(len(marks[key]) > 1 for key in (
        "version", "banner", "input", "normal", "opt_evaluation", "freq", "transitions", "job_number",
    )) or bool(marks["multi_input"])
    first_scf = marks["scf"][0] if marks["scf"] else len(lines)
    ambiguous |= any(i > first_scf for key in ("banner", "input", "version") for i in marks[key])
    ambiguous |= any(i > end for key in ("scf", "energy", "coordinates", "freq", "transitions", "opt_cycle")
                     for i in marks[key])
    finished = exit_code in (None, 0) and normal and not ambiguous
    opt = bool(marks["opt_cycle"] or marks["opt_convergence"] or marks["opt_failure"])
    freq = bool(marks["freq"])
    td = bool(marks["transitions"])
    observed = ("optfreq" if freq else "opt") if opt else ("freq" if freq else ("tddft" if td else "sp"))
    if not marks["energy"] and observed == "sp":
        observed = "unknown"
    # A final-energy line also appears in calculations whose next stage failed.
    # An input echo can rule out fixed-geometry SP even without that stage's table.
    keywords = {word.lower() for word in conditions["keywords"]}
    other_requested = bool(keywords & {
        "opt", "tightopt", "looseopt", "verytightopt", "optts", "scan-ts", "copt",
        "freq", "numfreq", "anfreq", "irc", "md", "neb", "neb-ts",
    }) or any(re.match(r"%(?:tddft|cis)\b", line, re.I) for line in conditions["input_echo_lines"])
    fixed_sp = observed == "sp" and not other_requested

    # An optimization may print a converged energy on every cycle. Only the
    # explicit final stationary-point evaluation can qualify its final geometry.
    start = -1
    stop = end
    if opt:
        start = marks["opt_evaluation"][-1] if marks["opt_evaluation"] else end
        boundaries = after("opt_end", start, end) + after("freq", start, end)
        stop = min(boundaries, default=end)
    scfs = after("scf", start, stop)
    energies = after("energy", start, stop)
    energy = ENERGY.fullmatch(lines[energies[0]]) if len(energies) == 1 else None
    scf = len(scfs) == 1 and "NOT CONVERGED" not in lines[scfs[0]].upper()
    if scfs:
        cite("scf_convergence", scfs[-1])
    if energies:
        cite("electronic_energy", energies[-1])
    energy_ok = bool(finished and scf and energy and scfs[0] < energies[0])
    if energy_ok:
        quantities["electronic_energy"] = {"value": energy[1], "unit": "Eh"}

    geometry_stop = energies[0] if len(energies) == 1 else stop
    geometry_marks = after("coordinates", start, geometry_stop)
    geom_i = geometry_marks[-1] if geometry_marks else None
    atoms = _coordinates(lines, geom_i) if geom_i is not None else []
    if atoms and len(atoms) != conditions.get("atom_count", len(atoms)):
        atoms = []
    opt_ok = bool(
        energy_ok and opt and atoms and marks["opt_convergence"]
        and marks["opt_convergence"][-1] < start < geom_i < energies[0]
        and not marks["opt_failure"]
    )
    if opt_ok:
        cite("final_geometry", geom_i)
        quantities["final_geometry"] = {"unit": "angstrom", "atoms": atoms, "line": geom_i + 1}
    elif energy_ok and atoms and not opt:
        cite("evaluated_geometry", geom_i)
        quantities["evaluated_geometry"] = {"unit": "angstrom", "atoms": atoms, "line": geom_i + 1}

    frequency_ok = False
    minimum = None
    if freq and not ambiguous:
        fstart = marks["freq"][0]
        wave, complete = _wave_data(lines, fstart, atoms)
        quantities["frequencies"] = wave
        # A new energy/geometry between the qualified evaluation and the Hessian
        # breaks attribution, even when a concatenated fragment omits its banner.
        next_evaluation = bool(energy_ok and any(
            after(key, energies[0], fstart) for key in ("coordinates", "energy", "scf")
        ))
        frequency_ok = bool(energy_ok and complete and energies[0] < fstart < end and not next_evaluation)
        if frequency_ok:
            internal = wave["rows"][wave["rigid_mode_count"]:]
            wave["imaginary_modes"] = [r for r in internal if r["value"] < 0]
            if opt_ok and not marks["constraints"] and internal:
                if any(r["value"] < 0 for r in internal):
                    minimum = False
                elif all(r["value"] > 0 for r in internal):
                    minimum = True
        ir_marks = after("ir", fstart, end)
        if frequency_ok and len(ir_marks) == 1:
            rows = []
            for i, m in _table_rows(lines, ir_marks[0], IR, ["Mode", "freq", "eps", "Int"], ["cm**-1", "L/(mol*cm)", "km/mol"]):
                mode = int(m[1])
                if mode < len(wave["rows"]) and float(m[2]) == wave["rows"][mode]["value"]:
                    rows.append({"mode": mode, "wavenumber_cm1": float(m[2]), "value": float(m[3]), "line": i + 1})
            if rows:
                quantities["ir_intensities"] = {"unit": "km/mol", "rows": rows}
        if frequency_ok:
            starts = after("thermo", fstart, end)
            values = []
            for first, last in zip(starts, starts[1:] + [end]):
                value = _thermochemistry(lines, first, last)
                if value:
                    values.append(value)
            if len(values) == 1:
                quantities["gibbs_free_energy"] = values[0]
            elif values:
                quantities["gibbs_free_energies"] = values

    if energy_ok:
        dipoles = after("dipole", start, end)
        if len(dipoles) == 1:
            i = dipoles[0]
            if m := re.fullmatch(rf"Magnitude \(Debye\)\s*:\s*({NUMBER})", lines[i]):
                quantities["dipole_magnitude"] = {"unit": "D", "value": m[1], "line": i + 1}

    td_ok = False
    if energy_ok and td:
        rows = []
        specs = _table_rows(lines, marks["transitions"][0], TRANSITION,
                            ["Transition", "Energy", "Energy", "Wavelength", "fosc(D2)"],
                            ["(eV)", "(cm-1)", "(nm)"])
        for i, m in specs:
            rows.append({"initial": m[1], "final": m[2], "energy_eV": float(m[3]),
                         "wavelength_nm": float(m[4]), "oscillator_strength": float(m[5]), "line": i + 1})
        if rows:
            quantities["singlet_transitions"] = {"rows": rows}
            roots = conditions.get("roots")
            td_ok = bool(roots and [int(r["final"].split("-")[0]) for r in rows] == list(range(1, roots + 1)))

    checks = {
        "process_succeeded": None if exit_code is None else exit_code == 0,
        "single_job_scope": not ambiguous, "scf_converged": scf, "energy_verified": energy_ok,
        "optimization_converged": opt_ok, "frequency_complete": frequency_ok,
        "local_minimum": minimum, "singlet_transitions_complete": td_ok,
    }
    checks["task_verified"] = {
        "sp": energy_ok and fixed_sp, "opt": opt_ok, "freq": frequency_ok,
        "optfreq": opt_ok and frequency_ok, "tddft": td_ok,
    }[task]
    if exit_code not in (0, None):
        missing.append(f"ORCA exited with status {exit_code}.")
    if not normal:
        missing.append("A unique normal termination was not confirmed.")
    if ambiguous:
        missing.append("The records cannot be attributed to one ORCA job.")
    if not scf or not energy_ok:
        missing.append("Converged SCF followed by a single final electronic energy in the requested evaluation was not verified.")
    if task in ("opt", "optfreq") and not opt_ok:
        if marks["opt_failure"]:
            missing.append("ORCA reported that the optimization did not converge; see evidence.opt_failure.")
        missing.append("Optimization convergence and its final stationary-point geometry were not verified.")
    if task in ("freq", "optfreq") and not frequency_ok:
        missing.append("A complete 3N frequency table for the evaluated geometry was not verified.")
    if task == "tddft" and not td_ok:
        missing.append("The full electric-dipole singlet transition table and its units were not verified.")
    if task == "sp" and not fixed_sp:
        missing.append("The output is not a verified fixed-geometry single point.")
    return {
        "process_exit_code": exit_code, "normal_termination": normal, "orca_version": version,
        "requested_task": task, "observed_task": observed, "conditions": conditions,
        "checks": checks, "quantities": quantities, "evidence": evidence, "missing": missing,
    }
