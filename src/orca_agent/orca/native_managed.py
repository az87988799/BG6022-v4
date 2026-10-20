"""Managed ORCA input structure and execution impact, without a method catalogue.

This version deliberately preserves the historical parsers.  It parses the
control structures whose finite work and dependencies we can account for; ORCA
owns scientific keyword validity.  Admission is never scientific qualification.
"""

from __future__ import annotations

import math
import re
from functools import lru_cache

from orca_agent.orca.native import _NUMBER, _dependency_name, _digest, _lines, _xyz_file

ADMISSION_VERSION = "native-orca-input-3"
CONDITIONS = ("method", "basis", "charge", "multiplicity", "electronic_state", "environment")
STATES = {"rhf", "rks", "uhf", "uks", "rohf", "roks", "anyscf"}
OPTIMIZATIONS = {"opt", "tightopt", "verytightopt", "looseopt", "normalopt", "copt"}
# These are execution domains, not scientific method/property enumerations.
SCALAR_BLOCKS = {"scf", "method", "basis", "mp2", "mdci", "autoci", "casscf", "mrci",
                 "rel", "tddft", "cis", "cpcm", "freq", "geom", "output", "elprop", "eprnmr"}
_TOKEN = re.compile(r'"[^"\r\n]*"|[{}=,;]|[^\s{}=,;]+')
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+().,/*\-]*\Z")
# Explicit launch/multi-job/restart operations need their own managed contracts.
_UNMANAGED = re.compile(
    r"(?:extopt|progext|ext_params|external|compound|new_job|newjob|qmmm|crystal|"
    r"mrcc|dmrg|nbo|docker|solvator|goat|neb|mecp|surfcross|optcross|optconical|"
    r"md-l|irc|esd|vpt2|single_aniso|polyaniso|autofrag|extrapolat|"
    r"modetrajectory|trajectory|numgrad|numnac|calcesthess|energygrad|enmgrad|"
    r"nmscan|normalmodescan|nmgrad|normalmodegrad|printthermochem|cosmors|ecrism|draco)", re.I)
_UNMANAGED_EXACT = {"md", "mm", "cim", "eda", "led", "mt", "mtr", "autostart", "readints",
                    "rescue", "propertiesonly", "fastopt", "optts", "moread", "scan", "xtb", "xtb1", "xtb2"}
_CONTROL_FIELD = re.compile(
    r"(?:file|path|directory|command|executable|external|progext|extopt|moinp|"
    r"restart|read|write|load|save|runtyp|job|nprocs|nworkers|process|"
    r"calchess|recalchess|calc_hess|recalc_hess|hybrid_hess|partial_hess|"
    r"inhess|out_hess|numgrad|numnac|orca|nbo|dmrg|mrcc|draco)", re.I)
_FALSE = {"false", "0"}


@lru_cache(maxsize=1)
def _known_selectors():
    """Use the locked OPI vocabulary to detect conflicting selectors, not to
    restrict eligibility. A new method/basis absent from OPI is still admitted.
    """
    from opi.input.simple_keywords import BasisSet, Dft, Method, SimpleKeyword, Sqm, Wft

    def words(*classes):
        return {value.keyword.casefold() for cls in classes for value in vars(cls).values()
                if isinstance(value, SimpleKeyword)}

    return words(Method, Dft, Wft, Sqm), words(BasisSet)


def canonical_managed_condition(name, value):
    """Compare declared input labels without inventing a scientific default."""
    if name in {"charge", "multiplicity"}:
        return value if type(value) is int else None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    lower = text.casefold()
    if name == "method":
        if lower in {"hf", "rhf", "uhf", "rohf"}:
            return "hf"
        return lower.replace("-", "") if lower.replace("-", "").endswith("3c") else lower
    if name == "basis":
        return {"6-31g(d)": "6-31g*", "6-31g(d,p)": "6-31g**",
                "builtin": "built-in", "built_in": "built-in"}.get(lower, lower)
    if name == "electronic_state":
        return lower if lower in STATES else None
    if name == "environment":
        if lower in {"gas", "gas_phase", "gas phase"}:
            return "gas_phase"
        match = re.fullmatch(r"(cpcm|smd)\s*\(\s*(.+?)\s*\)", lower)
        if match:
            descriptor = match[2].strip('"').strip()
            if descriptor.startswith("epsilon="):
                try:
                    number = float(descriptor.partition("=")[2])
                except ValueError:
                    return None
                if not math.isfinite(number) or number <= 1:
                    return None
                descriptor = "epsilon=" + format(number, ".15g")
            return f"{match[1]}({descriptor})"
    return None


def profile_contract(policy):
    return {"profile": policy.profile, "admission_version": ADMISSION_VERSION,
        "limits": policy.model_dump(mode="json", exclude={"profile"}),
        "methods": "Open explicit ORCA method label; scientific validity is checked by ORCA, not an enum.",
        "basis_spellings": "Open orbital basis label; basis='built-in' for a self-contained *-3c method.",
        "simple_input": "Place method and orbital basis first (an explicit wavefunction may precede/intervene); "
            "then scientific modifiers and managed job keywords. Do not provide competing method/basis selectors.",
        "wavefunction": "Explicit RHF/RKS/UHF/UKS/ROHF/ROKS/AnySCF keyword; no guessed spin default.",
        "environment": "Gas only when no solvent declaration exists; CPCM(solvent), SMD(solvent), or %cpcm "
            "solvent/SMDsolvent and epsilon are parsed and compared with Request.",
        "elements": "Finite Cartesian XYZ atom rows; no H-Ar restriction.",
        "jobs": "SP, independent Opt, Freq/AnFreq, NumFreq, Opt plus frequency, finite relaxed %geom Scan, "
            "and TDDFT/CIS response blocks. Unsupported launch/workflow control fails explicitly.",
        "required_keywords": ["NoAutoStart"],
        "required_blocks": {"maxcore": "explicit positive integer within Run resource limits",
            "scf": "MaxIter within frozen bound; AutoTRAH false prevents implicit fallback iterations",
            "geom": "MaxIter within frozen bound for optimization and relaxed scan",
            "tddft/cis": "NRoots and MaxIter within frozen response bound when present"},
        "scalar_blocks": sorted(SCALAR_BLOCKS),
        "block_syntax": "Open scalar scientific key/value pairs, including comma-separated values; "
            "managed %geom Constraints {...} end and Scan B/A/D indices = start,end,count end. "
            "Unknown control blocks, file paths, external programs, restarts and unaccounted nesting are refused.",
        "geometry": "* xyzfile <charge> <multiplicity> <basename.xyz>; pin and declare that XYZ dependency.",
        "task_geometry": {"enabled": policy.allow_task_geometry,
            "scope": "Must preserve initial atom count and ordered elements when task geometry is authorized."},
        "request_binding": "Explicit method/basis/state/charge/multiplicity/environment compared with Request; "
            "all input keywords and scalar parameters retained; this is not independent output verification.",
        "work_units": "Conservative declared slots for optimization iterations, finite scan points, Hessian "
            "displacements and response roots; cumulative Run budget admission still applies.",
        "unknown_syntax": "Unknown scientific labels may fail in ORCA; unmanaged execution structures are rejected.",
        "scientific_validation": "unverified"}


def _scalar(token):
    value = token[1:-1] if token.startswith('"') and token.endswith('"') else token
    if (not value or any(char in value for char in "\\:$<>|&`'\n\r") or ".." in value
            or value.startswith("/") or re.search(r"\.(?:exe|dll|py|sh|bat|cmd|inp|gbw|xyz|hess|dat)\b", value, re.I)):
        raise ValueError("scientific scalar contains a path, file dependency or command syntax")
    if re.fullmatch(_NUMBER, value):
        if not math.isfinite(float(value.replace("D", "E").replace("d", "e"))):
            raise ValueError("scientific scalar must be finite")
    elif not re.fullmatch(r"[A-Za-z0-9_+().*/ ,\-]+", value):
        raise ValueError("unsupported scalar syntax; no executable expressions or nested control")
    if value.casefold() in {"nan", "inf", "infinity", "+inf", "-inf"}:
        raise ValueError("scientific scalar must be finite")
    return value


def _number(value, label, *, positive=False):
    try:
        number = float(str(value).replace("D", "E").replace("d", "e"))
    except ValueError as error:
        raise ValueError(f"{label} must be a finite number") from error
    if not math.isfinite(number) or positive and number <= 0:
        raise ValueError(f"{label} must be a finite positive number")
    return number


def _integer(value, label, maximum=None):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value):
        raise ValueError(f"{label} requires an explicit positive integer")
    number = int(value)
    if maximum is not None and number > maximum:
        raise ValueError(f"{label} exceeds its frozen bound")
    return number


def _indices(values, atom_count, *, wildcard=False):
    for value in values:
        if wildcard and value == "*":
            continue
        if not re.fullmatch(r"[0-9]+", value) or int(value) >= atom_count:
            raise ValueError("geometry control references an atom outside the input geometry")


def _nested(tokens, position, kind):
    end = position
    while end < len(tokens) and tokens[end].casefold() != "end":
        if tokens[end].startswith(("%", "!", "$")):
            raise ValueError("nested geometry control cannot contain directives")
        end += 1
    if end == len(tokens):
        raise ValueError(f"unterminated geometry {kind}")
    return tokens[position:end], end + 1


def _block(tokens, position, name):
    if name not in SCALAR_BLOCKS | {"pal"}:
        raise ValueError(f"%{name} has no managed execution-impact/dependency contract")
    values = {}
    while position < len(tokens):
        key = tokens[position].casefold()
        position += 1
        if key == "end":
            return values, position
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key) or key in values:
            raise ValueError(f"ambiguous or duplicate %{name} scalar/control field")
        if name == "geom" and key in {"constraints", "scan"}:
            values[key], position = _nested(tokens, position, key)
            continue
        if position < len(tokens) and tokens[position] == "=":
            position += 1
        if position >= len(tokens) or tokens[position].casefold() == "end":
            raise ValueError(f"%{name}.{key} requires a scalar value")
        value = _scalar(tokens[position])
        position += 1
        while position < len(tokens) and tokens[position] == ",":
            position += 1
            if position >= len(tokens) or tokens[position].casefold() == "end":
                raise ValueError("truncated scientific scalar list")
            value += "," + _scalar(tokens[position])
            position += 1
        if name == "pal" and key != "nprocs":
            raise ValueError("parallel resource control requires managed %pal nprocs only")
        if name != "pal" and _CONTROL_FIELD.search(key):
            if not (key in {"restart", "calchess", "recalchess", "calc_hess", "recalc_hess",
                            "jsongbwfile"} and value.casefold() in _FALSE):
                # JsonPropFile is an in-process property-output declaration; GBW
                # conversion and arbitrary file read/write controls are separate execution.
                if not (name == "output" and key == "jsonpropfile" and value.casefold() in _FALSE | {"true", "1"}):
                    raise ValueError(f"%{name}.{key} requires an explicit managed execution/dependency contract")
        if _UNMANAGED.search(key) or _UNMANAGED.search(value):
            raise ValueError(f"%{name}.{key} enables unmanaged external or internal execution")
        values[key] = value
        if position < len(tokens) and tokens[position] == ";":
            position += 1
    raise ValueError(f"unterminated %{name} block")


def _geometry_controls(block, atom_count):
    constraints = block.get("constraints", [])
    position = 0
    while position < len(constraints):
        if constraints[position] != "{":
            raise ValueError("geometry constraints require explicit brace records")
        try:
            end = constraints.index("}", position + 1)
        except ValueError as error:
            raise ValueError("unterminated geometry constraint") from error
        row = constraints[position + 1:end]
        if len(row) < 3 or row[-1].casefold() != "c":
            raise ValueError("invalid geometry constraint record")
        count = {"b": 2, "a": 3, "d": 4, "c": 1, "x": 1, "y": 1, "z": 1}.get(row[0].casefold())
        if count is None or len(row) not in {count + 2, count + 3}:
            raise ValueError("unsupported geometry constraint shape")
        _indices(row[1:1 + count], atom_count, wildcard=True)
        if len(row) == count + 3:
            _number(row[-2], "constraint value")
        position = end + 1
    scan, position, points = block.get("scan", []), 0, 1
    while position < len(scan):
        count = {"b": 2, "a": 3, "d": 4}.get(scan[position].casefold())
        if count is None or len(scan) < position + count + 7:
            raise ValueError("scan requires B/A/D indices = finite_start,finite_end,positive_count")
        _indices(scan[position + 1:position + count + 1], atom_count)
        tail = scan[position + count + 1:position + count + 7]
        if (tail[0], tail[2], tail[4]) != ("=", ",", ","):
            raise ValueError("scan requires an explicit finite start,end,count")
        _number(tail[1], "scan start")
        _number(tail[3], "scan end")
        points *= _integer(tail[5], "scan count")
        if points > 2**31 - 1:
            raise ValueError("scan point product exceeds the bounded execution counter")
        position += count + 7
    return points


def _environment(keywords, blocks):
    lower = {word.casefold() for word in keywords}
    if any(re.match(r"(?:cosmo|gbsa|alpb|smd18)(?:\(|$)", word) for word in lower):
        raise ValueError("solvent selector needs an explicit managed environment-condition contract")
    declarations = [word for word in keywords if re.match(r"^(?:cpcm|smd)\(", word, re.I)]
    if len(declarations) > 1:
        raise ValueError("competing solvent declarations are ambiguous")
    cpcm = blocks.get("cpcm", {})
    if "nocpcm" in lower and (declarations or cpcm or "cpcm" in lower or "smd" in lower):
        raise ValueError("NoCPCM conflicts with the explicit solvent declarations")
    if lower & {"cpcm", "smd"} and not declarations and not cpcm:
        raise ValueError("bare solvent selector needs an explicit solvent/dielectric condition")
    if not declarations and not cpcm:
        return "gas_phase"
    smd = cpcm.get("smd", "false").casefold()
    if smd not in {"true", "1", "false", "0"}:
        raise ValueError("%cpcm SMD requires a boolean")
    if "smdsolvent" in cpcm and smd not in {"true", "1"}:
        raise ValueError("SMDsolvent requires explicit SMD true; no implicit solvent model is inferred")
    if declarations and declarations[0].casefold().startswith("smd(") and "smd" in cpcm and smd in _FALSE:
        raise ValueError("simple and block solvent declarations disagree")
    model = "smd" if smd in {"true", "1"} or "smdsolvent" in cpcm else "cpcm"
    solvent = cpcm.get("smdsolvent" if model == "smd" else "solvent")
    block_environment = f"{model}({solvent})" if solvent else None
    if "epsilon" in cpcm:
        epsilon = _number(cpcm["epsilon"], "solvent epsilon", positive=True)
        if epsilon <= 1:
            raise ValueError("explicit solvent epsilon must exceed 1")
        if not solvent:
            if model == "smd":
                raise ValueError("SMD needs an explicit solvent name; custom descriptor binding is not implemented")
            block_environment = f"cpcm(epsilon={epsilon:.15g})"
        else:
            raise ValueError("solvent name plus overridden epsilon needs an explicit compound condition contract")
    header = canonical_managed_condition("environment", declarations[0]) if declarations else None
    block_environment = canonical_managed_condition("environment", block_environment)
    if header and block_environment and header != block_environment:
        raise ValueError("simple and block solvent declarations disagree")
    result = block_environment or header
    if result is None:
        raise ValueError("solvent block has no explicit solvent or dielectric condition")
    return result


def _conditions(keywords, blocks, charge, multiplicity, *, expected_method=None):
    # The contract disambiguates the open method and basis labels by position,
    # while allowing an explicit wavefunction keyword before/between them.
    state_words = [word for word in keywords if word.casefold() in STATES]
    if len(state_words) != 1:
        raise ValueError("one explicit wavefunction keyword is required; no spin-state default is inferred")
    state = state_words[0]
    words = [word for word in keywords if word.casefold() not in STATES]
    known_methods, known_bases = _known_selectors()
    explicit_method_after_state = bool(words) and (
        words[0].casefold() in known_methods
        or canonical_managed_condition("method", words[0]) == canonical_managed_condition("method", expected_method))
    if keywords[0].casefold() in {"rhf", "uhf", "rohf"} and not explicit_method_after_state:
        method, remaining = "HF", words
    elif words:
        method, remaining = words[0], words[1:]
    else:
        raise ValueError("explicit method label is missing")
    if method.casefold().replace("-", "").endswith("3c"):
        basis = "built-in"
        extras = remaining
    elif remaining:
        basis = remaining[0]
        extras = remaining[1:]
    else:
        raise ValueError("explicit orbital basis label is missing")
    for label in (method, basis):
        if not _LABEL.fullmatch(label) or ".." in label:
            raise ValueError("method and basis must be explicit simple scientific labels")
    if basis.casefold() in {"noautostart", "opt", "freq", "numfreq", "sp", "tightscf"}:
        raise ValueError("orbital basis selector is missing before execution modifiers")
    for word in extras:
        if word.casefold() in known_methods:
            raise ValueError("competing scientific method selectors are ambiguous")
        if word.casefold() in known_bases:
            raise ValueError("competing orbital basis selectors are ambiguous")
    method_block = blocks.get("method", {})
    for key in ("functional", "method"):
        override = method_block.get(key)
        generic_method = (key == "method" and override is not None
            and ((override.casefold() in {"dft", "dfgto"} and canonical_managed_condition("method", method) != "hf")
                 or (override.casefold() == "hf" and canonical_managed_condition("method", method) == "hf")))
        if override and not generic_method:
            if canonical_managed_condition("method", override) != canonical_managed_condition("method", method):
                raise ValueError("%method selector conflicts with the explicit simple-input method")
    if override := blocks.get("scf", {}).get("hftyp"):
        families = {"rks": "rhf", "uks": "uhf", "roks": "rohf"}
        if families.get(override.casefold(), override.casefold()) != families.get(state.casefold(), state.casefold()):
            raise ValueError("%scf HFTyp conflicts with the explicit wavefunction selector")
    if override := blocks.get("basis", {}).get("basis"):
        if canonical_managed_condition("basis", override) != canonical_managed_condition("basis", basis):
            raise ValueError("%basis selector conflicts with the explicit orbital basis")
    return {"method": method, "basis": basis, "electronic_state": state.upper(),
            "charge": charge, "multiplicity": multiplicity, "environment": _environment(keywords, blocks)}


def validate_managed_input(text, dependencies, resources, policy, binding):
    lines = _lines(text)
    keywords, blocks = [], {}
    geometry = charge = multiplicity = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("!"):
            words = line[1:].split()
            if not words or any(not _LABEL.fullmatch(word) or ".." in word for word in words):
                raise ValueError("simple scientific keywords cannot contain paths/directives/commands")
            keywords.extend(words)
        elif line.startswith("%"):
            match = re.fullmatch(r"%([A-Za-z][A-Za-z0-9_]*)\s*(.*)", line)
            if not match or match[1].casefold() in blocks:
                raise ValueError("invalid or duplicate native block")
            name, body = match[1].casefold(), match[2]
            if name == "maxcore":
                blocks[name] = _integer(body, "%maxcore")
            else:
                # Consume complete top-level block; nested end tokens are parsed
                # by _block, rather than mistaken for its closing delimiter.
                while index + 1 < len(lines) and not lines[index + 1].startswith(("!", "%", "*", "$")):
                    index += 1
                    body += " " + lines[index]
                tokens = _TOKEN.findall(body)
                value, end = _block(tokens, 0, name)
                if end != len(tokens):
                    raise ValueError("trailing input after a native control block")
                blocks[name] = value
        elif line.startswith("*"):
            match = re.fullmatch(r"\*\s*xyzfile\s+(-?\d+)\s+([1-9][0-9]*)\s+(.+)", line, re.I)
            if not match or geometry is not None:
                raise ValueError("managed geometry requires one pinned xyzfile and explicit charge/multiplicity")
            charge, multiplicity = int(match[1]), int(match[2])
            geometry = _dependency_name(match[3], dependencies, ".xyz")
        else:
            raise ValueError("unmanaged directive or multi-job control")
        index += 1
    if not keywords or geometry is None:
        raise ValueError("native input needs explicit method/basis keywords and pinned XYZ geometry")
    lower = [word.casefold() for word in keywords]
    if len(set(lower)) != len(lower):
        raise ValueError("duplicate simple keywords are ambiguous")
    for word in lower:
        if _UNMANAGED.search(word) or word in _UNMANAGED_EXACT or word.startswith("pal"):
            raise ValueError(f"keyword needs managed external/dependency/workload control: {word}")
    if "noautostart" not in lower:
        raise ValueError("NoAutoStart is required to exclude undeclared checkpoint inheritance")
    if set(dependencies) != {geometry}:
        raise ValueError("native manifest has unreferenced/unmanaged dependencies")
    atom_count = _xyz_file(dependencies[geometry])
    if atom_count > policy.max_atoms:
        raise ValueError("geometry exceeds the frozen configurable atom limit")
    maxcore = blocks.get("maxcore")
    if maxcore is None:
        raise ValueError("explicit %maxcore is required")
    cores = _integer(blocks.get("pal", {}).get("nprocs", "1"), "%pal nprocs")
    if cores > resources.max_cores or maxcore > resources.maxcore_mb or cores * maxcore > resources.total_memory_mb:
        raise ValueError("native input CPU/MaxCore exceeds the frozen resource envelope")
    scf = blocks.get("scf", {})
    scf_iterations = _integer(scf.get("maxiter"), "%scf MaxIter", policy.max_scf_iterations)
    if scf.get("autotrah", "").casefold() not in _FALSE:
        raise ValueError("explicit %scf AutoTRAH false is required to bound fallback iteration control")
    optimizations = set(lower) & OPTIMIZATIONS
    frequencies = set(lower) & {"freq", "anfreq", "numfreq"}
    for key in ("numfreq", "anfreq"):
        if key in blocks.get("freq", {}):
            value = blocks["freq"][key].casefold()
            if value not in _FALSE | {"true", "1"}:
                raise ValueError(f"%freq {key} requires a boolean execution selector")
            aliases = {"freq", "anfreq"} if key == "anfreq" else {"numfreq"}
            if value in _FALSE and frequencies & aliases:
                raise ValueError("simple and block frequency execution selectors disagree")
            if value not in _FALSE and not frequencies & aliases:
                frequencies.add(key)
    if len(optimizations) > 1 or len(frequencies) > 1 or "sp" in lower and (optimizations or frequencies):
        raise ValueError("competing job selectors are ambiguous")
    geom = blocks.get("geom", {})
    scan_points = _geometry_controls(geom, atom_count)
    if geom and not optimizations:
        raise ValueError("%geom controls require explicit optimization; unrelaxed scans need a separate contract")
    geom_iterations = _integer(geom.get("maxiter"), "%geom MaxIter", policy.max_geom_iterations) if optimizations else 0
    if "cis" in blocks and "tddft" in blocks:
        raise ValueError("competing CIS/TDDFT blocks")
    roots, response_iterations = 0, 0
    for name in ("cis", "tddft"):
        if name in blocks:
            roots = _integer(blocks[name].get("nroots"), f"%{name} NRoots", policy.max_response_iterations)
            response_iterations = _integer(blocks[name].get("maxiter"), f"%{name} MaxIter", policy.max_response_iterations)
    for name, block in blocks.items():
        if isinstance(block, dict):
            for key in ("maxiter", "z_maxiter"):
                if key in block and name not in {"scf", "geom", "cis", "tddft"}:
                    _integer(block[key], f"%{name} {key}", policy.max_response_iterations)
            if "nroots" in block and name not in {"cis", "tddft"}:
                count = sum(_integer(value, f"%{name} NRoots", policy.max_response_iterations)
                            for value in block["nroots"].split(","))
                if count > policy.max_response_iterations:
                    raise ValueError(f"%{name} total response roots exceed the frozen bound")
                roots += count
    actual = _conditions(keywords, blocks, charge, multiplicity,
                         expected_method=(binding or {}).get("conditions", {}).get("method"))
    if not binding or set(binding.get("conditions", {})) != set(CONDITIONS):
        raise ValueError("managed native input requires trusted current Request/Step condition binding")
    for name, value in actual.items():
        expected = canonical_managed_condition(name, binding["conditions"][name])
        if expected is None or expected != canonical_managed_condition(name, value):
            raise ValueError("admitted native input differs from current Request/Step condition: " + name)
    frequency_block = blocks.get("freq", {})
    temperatures = [frequency_block[key] for key in ("temp", "t") if key in frequency_block]
    if len(temperatures) > 1:
        raise ValueError("competing explicit thermochemistry temperature declarations")
    if temperatures:
        values = [_number(value, "%freq Temp", positive=True) for value in temperatures[0].split(",")]
        if len(values) > 16:
            raise ValueError("ORCA thermochemistry accepts at most 16 explicit temperatures")
        if len(values) == 1:
            actual["temperature_K"] = values[0]
        else:
            actual["temperatures_K"] = values
    digest = _digest(dependencies[geometry])
    symbols = [row.split()[0] for row in dependencies[geometry].decode("ascii").splitlines()[2:2 + atom_count]]
    relation = "exact_initial"
    if digest != binding.get("geometry_sha256"):
        if not policy.allow_task_geometry or binding.get("allow_task_geometry") is not True:
            raise ValueError("native xyzfile bytes differ from current Request prepared/registered geometry")
        if symbols != binding.get("initial_atom_symbols"):
            raise ValueError("task candidate must preserve initial atom count and ordered elements")
        relation = "task_generated_candidate"
    optimization_slots = (geom_iterations + 1) * scan_points if optimizations else 1
    # NumFreq can differentiate numerical gradients for methods without an
    # analytic derivative. Reserve the full nested finite-difference envelope.
    numerical = "numfreq" in frequencies
    derivative_slots = (6 * atom_count + 1) ** 2 if numerical else 1 + 3 * atom_count if frequencies else 0
    work = optimization_slots + derivative_slots + roots
    run_type = "relaxed_scan" if "scan" in geom else "optimization" if optimizations else "single_point"
    if frequencies:
        frequency_type = "numerical_frequencies" if numerical else "analytic_frequencies"
        run_type = frequency_type if run_type == "single_point" else run_type + "_and_" + frequency_type
    return {"admission_version": ADMISSION_VERSION, "native_input_policy": policy.model_dump(mode="json"),
        "input_binding": binding, "actual_conditions_origin": "admitted_native_input",
        "resources": {"cores": cores, "memory_mb": resources.total_memory_mb, "maxcore_mb": maxcore,
                      "timeout_seconds": resources.job_timeout_seconds},
        "dependencies": {geometry: {"sha256": digest, "bytes": len(dependencies[geometry])}},
        "actual_conditions": {**actual, "input_keywords": keywords, "input_blocks": blocks,
            "atom_count": atom_count, "run_type": run_type, "origin": "admitted_native_input",
            "geometry_sha256": digest, "input_geometry_sha256": digest, "input_geometry_relation": relation,
            "initial_geometry_sha256": binding.get("geometry_sha256"), "request_version": binding.get("request_version"),
            "system_id": binding.get("system_id"), "scientific_validation": "unverified",
            "condition_verification": "explicit input selectors only; all modifiers retained, output not independently verified"},
        "execution_impact": {"orca_starts": 1, "postprocess_starts": 0, "external_programs": False,
            "internal_work_units": work, "work_unit_contract": "declared-managed-workload-1",
            "work_unit_meaning": "conservative declared task slots, not measured SCF/evaluation counts",
            "optimization_evaluation_slots": optimization_slots, "scan_points": scan_points,
            "derivative_slots": derivative_slots, "response_roots": roots,
            "scf_maxiter": scf_iterations, "geom_maxiter": geom_iterations, "response_maxiter": response_iterations}}
