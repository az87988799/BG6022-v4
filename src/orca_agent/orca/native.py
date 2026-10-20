"""Bounded native input preparation, shared by the OPI and Artifact adapters.

This is an admission grammar, not a complete ORCA parser or scientific checker.
The accepted grammar is documented in docs/native-orca-input.md. OS isolation is
still mandatory: parsing alone is never an execution security boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from orca_agent.models import NativeInputPolicy, NativeOrcaParameters, ResourceLimits

MAX_INPUT_BYTES = 1024 * 1024
MAX_DEPENDENCY_BYTES = 10 * 1024 * 1024
ADMISSION_VERSION = "native-orca-input-1"
_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?"
_KEYWORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+(),./*\-]*\Z")
_OPT = {"opt", "tightopt", "verytightopt", "looseopt", "normalopt"}
_BLOCK_FIELDS = {
    "pal": {"nprocs"},
    "scf": {"maxiter", "convforced", "tolenergy", "tole", "toldens", "tolrms", "tolmax",
            "convergence", "dampfac", "shift", "soscfstart", "directresetfreq"},
    "output": {"jsonpropfile", "jsongbwfile", "printlevel"},
    "geom": {"maxiter", "enforcestrictconvergence"},
    "tddft": {"nroots", "tda", "maxiter", "maxdim", "triplets"},
    "cis": {"nroots", "tda", "maxiter", "maxdim", "triplets"},
    "cpcm": {"epsilon", "refrac", "smd"},
}
_BOOL_FIELDS = {"enforcestrictconvergence", "jsonpropfile", "jsongbwfile", "tda",
                "triplets", "smd"}
_INT_FIELDS = {"nprocs", "maxiter", "nroots", "maxdim", "printlevel", "directresetfreq"}
_BLOCKED_EXACT = {
    "autostart", "readints", "rescue", "md", "mm", "cim", "eda", "led", "esd",
    "mt", "mtr", "modetrajectory", "printthermochem", "propertiesonly", "anfreq",
    "autofrag", "bpop", "polyaniso", "single_aniso", "nearir", "vpt2", "calcesthess",
    "energygrad", "engrad", "gradient", "enmgrad", "trajectory", "surfcross",
}
_BLOCKED_FRAGMENTS = (
    "extopt", "progext", "ext_params", "compound", "new_job", "scan", "freq", "numgrad",
    "numnac", "neb", "goat", "docker", "solvator", "qmmm", "mrcc", "dmrg", "nbo",
    "xtb", "irc", "mecp", "esd(", "optts", "optconical", "optcross", "fastopt",
)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unlinked(path: Path) -> None:
    for item in (path, *path.parents):
        if not item.exists():
            continue
        value = item.lstat()
        if item.is_symlink() or getattr(value, "st_file_attributes", 0) & 0x400:
            raise ValueError("input path traverses a symlink or reparse point")
    if path.exists() and path.is_file() and path.stat().st_nlink > 1:
        raise ValueError("hard-linked task inputs are not admitted")


def read_pinned_input(path: str | Path, expected_sha256: str, *, limit: int) -> bytes:
    """Read one bounded immutable copy; hash the exact bytes subsequently staged."""
    source = Path(path).absolute()
    _unlinked(source)
    if not stat.S_ISREG(source.stat().st_mode) or source.stat().st_size > limit:
        raise ValueError("input is not a bounded regular file")
    with source.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit or _digest(data) != expected_sha256:
        raise ValueError("input Artifact hash changed or size limit exceeded")
    return data


def _lines(text: str) -> list[str]:
    if len(text.encode("utf-8")) > MAX_INPUT_BYTES or not text.isascii():
        raise ValueError("native input must be bounded ASCII text")
    if any(ord(char) < 32 and char not in "\r\n\t" for char in text) or "\x7f" in text:
        raise ValueError("native input contains control characters")
    result = []
    for line in text.splitlines():
        # ORCA permits #comment# followed by active syntax. Reject that form
        # instead of accidentally treating its suffix as a harmless comment.
        parts = line.split("#")
        if len(parts) > 2 and any(part.strip() for part in parts[2::2]):
            raise ValueError("inline closed comments with trailing input are not supported")
        clean = parts[0].strip()
        if clean:
            result.append(clean)
    return result


def _dependency_name(value: str, dependencies: Mapping[str, bytes], suffix: str) -> str:
    name = value.strip('"')
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name)
            or name.lower().startswith("job.") or name.endswith((".", " "))
            or not name.lower().endswith(suffix)):
        raise ValueError("native dependency must be a task-local basename with the required suffix")
    if name not in dependencies:
        raise ValueError(f"native dependency is absent from the pinned manifest: {name}")
    return name


def _xyz_atoms(lines: list[str]) -> int:
    if not lines:
        raise ValueError("geometry must contain at least one Cartesian atom")
    for line in lines:
        match = re.fullmatch(rf"([A-Z][a-z]?)\s+({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})", line.strip())
        if not match or not all(math.isfinite(float(x.replace("D", "E").replace("d", "e")))
                                for x in match.groups()[1:]):
            raise ValueError("geometry must contain finite Cartesian atom rows only")
    return len(lines)


def _xyz_file(data: bytes) -> int:
    try:
        lines = data.decode("ascii").splitlines()
        count = int(lines[0].strip())
    except (UnicodeError, ValueError, IndexError) as error:
        raise ValueError("XYZ dependency has no valid atom count") from error
    # OPI serializes a terminal blank line. Trim only this validation view;
    # staging and Request bindings continue to use the exact original bytes.
    while lines and not lines[-1].strip():
        lines.pop()
    if count < 1 or len(lines) != count + 2:
        raise ValueError("XYZ dependency must contain exactly one frame and its declared atom count")
    return _xyz_atoms(lines[2:])


def _block(name: str, tokens: list[str], fields=None) -> dict[str, str]:
    fields = _BLOCK_FIELDS if fields is None else fields
    if name not in fields:
        raise ValueError(f"unsupported execution impact or input grammar for %{name}")
    tokens = [token for token in tokens if token not in {"=", ";"}]
    if len(tokens) % 2:
        raise ValueError(f"%{name} requires admitted scalar key/value pairs; no nested blocks")
    values = {}
    for index in range(0, len(tokens), 2):
        key, value = tokens[index].lower(), tokens[index + 1]
        if key not in fields[name]:
            raise ValueError(f"unsupported execution impact or field %{name}.{key}")
        if key in values:
            raise ValueError(f"duplicate %{name}.{key} is ambiguous")
        if key in _BOOL_FIELDS or key == "autotrah" or (fields is not _BLOCK_FIELDS and key == "convforced"):
            if value.lower() not in {"true", "false", "0", "1"}:
                raise ValueError(f"%{name}.{key} requires a boolean")
        elif key in _INT_FIELDS or key == "z_maxiter":
            if not re.fullmatch(r"[1-9][0-9]*", value):
                raise ValueError(f"%{name}.{key} requires a positive integer")
        elif key == "convergence":
            if value.lower() not in {"sloppy", "loose", "normal", "tight", "verytight", "extreme"}:
                raise ValueError("unsupported SCF convergence selector")
        elif not re.fullmatch(_NUMBER, value) or not math.isfinite(float(value.replace("D", "E"))):
            raise ValueError(f"%{name}.{key} requires a finite numeric scalar")
        values[key] = value
    return values


def validate_native_input(text: str, dependencies: Mapping[str, bytes],
                          resources: ResourceLimits, *, legacy_optimization: bool = False,
                          policy: NativeInputPolicy | None = None, binding: dict | None = None) -> dict:
    """Validate exact staged bytes and return the managed launch's declared impact.

    Method/basis keyword spellings are not a product whitelist. Explicit runtime
    operations and scalar block syntax are bounded separately. The legacy-only
    optimization switch is called after the existing RHF adapter profile check.
    """
    if policy is not None:
        policy = NativeInputPolicy.model_validate(policy)
        if policy.profile == "managed-native-1":
            if legacy_optimization:
                raise ValueError("legacy adapter cannot inherit the new native execution profile")
            from orca_agent.orca.native_managed import validate_managed_input
            return validate_managed_input(text, dependencies, resources, policy, binding)
        from orca_agent.orca.native_profile import BLOCK_FIELDS
        if legacy_optimization:
            raise ValueError("legacy adapter cannot inherit the new native execution profile")
        fields = BLOCK_FIELDS
    else:
        fields = _BLOCK_FIELDS
    lines = _lines(text)
    keywords, blocks, referenced = [], {}, set()
    charge = multiplicity = atom_count = geometry_dependency = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("!"):
            words = line[1:].split()
            if not words or any(not _KEYWORD.fullmatch(word) or ".." in word for word in words):
                raise ValueError("simple keywords cannot contain paths, directives, quotes or redirection")
            keywords.extend(words)
        elif line.startswith("%"):
            match = re.fullmatch(r"%([A-Za-z][A-Za-z0-9_]*)\s*(.*)", line)
            if not match:
                raise ValueError("invalid native input block")
            name, tail = match[1].lower(), match[2]
            if name in blocks or (name == "cis" and "tddft" in blocks) or (name == "tddft" and "cis" in blocks):
                raise ValueError(f"duplicate %{name} declaration")
            if name == "maxcore":
                if not re.fullmatch(r"[1-9][0-9]*", tail):
                    raise ValueError("%maxcore requires one positive integer")
                blocks[name] = int(tail)
            elif name == "moinp":
                if policy is not None:
                    raise ValueError("bounded analytic profile does not admit checkpoint inheritance")
                dependency = _dependency_name(tail, dependencies, ".gbw")
                referenced.add(dependency)
                blocks[name] = dependency
            else:
                if name not in fields:
                    raise ValueError(f"unsupported execution impact or input grammar for %{name}")
                body = tail
                while not re.search(r"\bend\s*$", body, flags=re.I):
                    index += 1
                    if index >= len(lines) or lines[index].startswith(("!", "%", "*", "$")):
                        raise ValueError(f"unterminated or nested %{name} block")
                    body += " " + lines[index]
                body = re.sub(r"\bend\s*$", "", body, flags=re.I)
                tokens = re.findall(r"[^\s=;]+|[=;]", body)
                blocks[name] = _block(name, tokens, fields)
        elif line.startswith("*"):
            if atom_count is not None:
                raise ValueError("multiple coordinate declarations are not admitted")
            match = re.fullmatch(r"\*\s*(xyzfile|xyz)\s+(-?\d+)\s+([1-9]\d*)(?:\s+(.+))?", line, re.I)
            if not match:
                raise ValueError("only one explicit Cartesian xyz or xyzfile geometry is admitted")
            charge, multiplicity = int(match[2]), int(match[3])
            if match[1].lower() == "xyzfile":
                dependency = _dependency_name(match[4] or "", dependencies, ".xyz")
                referenced.add(dependency)
                atom_count = _xyz_file(dependencies[dependency])
                geometry_dependency = dependency
            else:
                if match[4]:
                    raise ValueError("inline xyz header cannot include a path or trailing control")
                atoms = []
                index += 1
                while index < len(lines) and lines[index] != "*":
                    atoms.append(lines[index])
                    index += 1
                if index >= len(lines):
                    raise ValueError("unterminated inline xyz geometry")
                atom_count = _xyz_atoms(atoms)
        else:
            raise ValueError("unsupported native input directive or multi-job control")
        index += 1
    lower = [word.lower() for word in keywords]
    for word in lower if policy is None else []:
        if word in _BLOCKED_EXACT or any(fragment in word for fragment in _BLOCKED_FRAGMENTS):
            raise ValueError(f"unmanaged external program, dependency or internal workload keyword: {word}")
        if word.startswith("pal"):
            raise ValueError("parallel resources must be declared once through %pal nprocs")
        if "opt" in word and (word not in _OPT or not legacy_optimization):
            raise ValueError("native optimization requires additional bounded derivative/workload admission")
    if not keywords or atom_count is None:
        raise ValueError("native input requires simple scientific keywords and one explicit geometry")
    if "noautostart" not in lower:
        raise ValueError("NoAutoStart is required to prevent undeclared checkpoint inheritance")
    if "moread" in lower and "moinp" not in blocks:
        raise ValueError("MORead requires an explicit pinned %moinp dependency")
    if set(dependencies) != referenced:
        raise ValueError("manifest contains unreferenced or unsupported native dependencies")
    if "maxcore" not in blocks:
        raise ValueError("explicit %maxcore is required; the ORCA default exceeds the resource envelope")
    cores = int(blocks.get("pal", {}).get("nprocs", "1"))
    maxcore = blocks["maxcore"]
    if cores > resources.max_cores or maxcore > resources.maxcore_mb or cores * maxcore > resources.total_memory_mb:
        raise ValueError("native input CPU/MaxCore exceeds the frozen resource envelope")
    optimization = bool(set(lower) & _OPT)
    if "geom" in blocks and not optimization:
        raise ValueError("%geom is admitted only with a bounded optimization")
    if optimization and "maxiter" not in blocks.get("geom", {}):
        raise ValueError("optimization must declare a finite %geom maxiter")
    if blocks.get("output", {}).get("jsongbwfile", "false").lower() in {"1", "true"}:
        raise ValueError("JSON GBW conversion is not admitted by this execution budget")
    launch = {
        "admission_version": ADMISSION_VERSION,
        "resources": {"cores": cores, "memory_mb": resources.total_memory_mb,
                      "maxcore_mb": maxcore, "timeout_seconds": resources.job_timeout_seconds},
        "execution_impact": {"orca_starts": 1, "internal_work_units": 1,
                             "electronic_evaluation_upper_bound": int(blocks["geom"]["maxiter"]) + 1
                             if optimization else 1,
                             "external_programs": False, "postprocess_starts": 0},
        "actual_conditions": {"input_keywords": keywords, "input_blocks": blocks,
                              "charge": charge, "multiplicity": multiplicity, "atom_count": atom_count,
                              "run_type": "optimization" if optimization else "single_point",
                              "scientific_validation": "unverified"},
        "dependencies": {name: {"sha256": _digest(dependencies[name]), "bytes": len(dependencies[name])}
                         for name in sorted(referenced)},
    }
    if policy is not None:
        from orca_agent.orca.native_profile import admit_profile
        launch = admit_profile(launch, dependencies, geometry_dependency, policy, binding)
    return launch


def prepare_native_input(workdir: str | Path, parameters: NativeOrcaParameters,
                         resolve_artifact: Callable[[str], Path], resources: ResourceLimits, *,
                         policy: NativeInputPolicy | None = None, binding: dict | None = None) -> dict[str, Any]:
    """Copy pinned inputs into a fresh attempt after all admission checks succeed."""
    parameters = NativeOrcaParameters.model_validate(parameters)
    code = read_pinned_input(resolve_artifact(parameters.input.artifact_id), parameters.input.sha256,
                             limit=MAX_INPUT_BYTES)
    dependencies = {}
    for entry in parameters.inputs.entries:
        # Only consumed data files can enter the ORCA working directory; a DLL,
        # executable or importable module cannot be smuggled in as an unused file.
        name = entry.target_path
        if "/" in name or Path(name).suffix.lower() not in {".xyz", ".gbw"}:
            raise ValueError("native dependencies currently support local XYZ and GBW files only")
        dependencies[name] = read_pinned_input(resolve_artifact(entry.artifact_id), entry.sha256,
                                               limit=MAX_DEPENDENCY_BYTES)
    if len(dependencies) > 32 or sum(map(len, dependencies.values())) > MAX_DEPENDENCY_BYTES:
        raise ValueError("native dependency count or aggregate byte bound exceeded")
    try:
        text = code.decode("ascii")
    except UnicodeError as error:
        raise ValueError("native input must be ASCII") from error
    source_code = code
    materialization = []
    if policy is not None and policy.profile == "managed-native-1":
        from orca_agent.orca.native_managed import materialize_managed_input
        text, materialization = materialize_managed_input(text, resources, policy, binding)
        code = text.encode("ascii")
    launch = validate_native_input(text, dependencies, resources, policy=policy, binding=binding)
    directory = Path(workdir).absolute()
    _unlinked(directory)
    directory.mkdir(parents=True, exist_ok=True)
    targets = ["job.inp", "input-manifest.json", *dependencies]
    if materialization:
        targets.append("source-input.inp")
    if any((directory / name).exists() for name in targets):
        raise FileExistsError("an attempt's input or dependencies may not be overwritten")
    if any(path.name != "intent.json" for path in directory.iterdir()):
        raise ValueError("native ORCA staging requires a fresh attempt directory")
    files = {"job.inp": code, **dependencies}
    if materialization:
        files["source-input.inp"] = source_code
    for name, data in files.items():
        with (directory / name).open("xb") as stream:
            stream.write(data)
    manifest = {"tool": "orca.run", "input_file": "job.inp", "input_sha256": _digest(code),
                "input_artifact": parameters.input.model_dump(mode="json"),
                "input_manifest": parameters.inputs.model_dump(mode="json"), **launch}
    if materialization:
        manifest.update(source_input_sha256=_digest(source_code),
                        materialization=materialization)
    with (directory / "input-manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2, allow_nan=False)
        stream.write("\n")
    return {**manifest, "input_path": str(directory / "job.inp")}
