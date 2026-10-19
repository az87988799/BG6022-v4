"""Closed execution compatibility for bounded SCF analytic derivatives.

This is not an ORCA scientific checker. Unknown syntax is refused, not assigned
an assumed method, gas phase, Hessian implementation or work estimate.
"""

from __future__ import annotations

import hashlib

from orca_agent.models import NativeInputPolicy

ADMISSION_VERSION = "native-orca-input-2"
BLOCK_FIELDS = {
    "pal": {"nprocs"},
    "scf": {"maxiter", "convforced", "autotrah"},
    "geom": {"maxiter", "enforcestrictconvergence"},
    "method": {"z_maxiter"},
    "output": {"jsonpropfile"},
}
METHODS = {name.casefold(): name for name in ("HF", "BP86", "PBE", "BLYP", "PBE0", "B3LYP", "B3LYP/G")}
BASES = {name.casefold(): name for name in (
    "STO-3G", "3-21G", "6-31G", "6-31G*", "6-31G**", "6-31G(d)", "6-31G(d,p)",
    "def2-SV(P)", "def2-SVP", "def2-TZVP", "def2-TZVPP",
)}
# Alias only where the published orbital basis is identical. Do not collapse
# B3LYP/G into B3LYP (their VWN definitions differ).
BASES.update({"6-31g(d)": "6-31G*", "6-31g(d,p)": "6-31G**"})
ELEMENTS = dict(zip(("H", "He", "Li", "Be", "B", "C", "N", "O", "F", "Ne",
                     "Na", "Mg", "Al", "Si", "P", "S", "Cl", "Ar"), range(1, 19), strict=True))
CONDITION_FIELDS = ("method", "basis", "charge", "multiplicity", "electronic_state", "environment")


def profile_contract(policy: NativeInputPolicy) -> dict:
    """Public guidance from the admission definitions, with the frozen limits."""
    return {"profile": policy.profile, "admission_version": ADMISSION_VERSION,
            "limits": policy.model_dump(mode="json", exclude={"profile"}),
            "methods": sorted(METHODS.values()), "basis_spellings": sorted(BASES),
            "wavefunction": {"HF": "RHF", "DFT": "RKS", "multiplicity": 1,
                "placement": "required explicit keyword on the ! line; no implicit default"},
            "environment": "gas_phase", "elements": list(ELEMENTS),
            "jobs": "one single point, or one combined Opt/TightOpt/VeryTightOpt plus Freq/AnFreq",
            "required_keywords": ["NORI", "NoAutoStart", "exactly one TightSCF/VeryTightSCF"],
            "optional_keywords": ["SP (single point only)", "DefGrid2 or DefGrid3"],
            "required_blocks": {"maxcore": "explicit integer within Run resources",
                "pal": "nprocs within Run resources (omit only for serial)",
                "scf": f"%scf MaxIter <integer 1..{policy.max_scf_iterations}> ConvForced true AutoTRAH false end; required",
                "geom": f"%geom MaxIter <integer 1..{policy.max_geom_iterations}> EnforceStrictConvergence true end; "
                    "required for Opt+Freq; forbidden for SP",
                "method": f"%method Z_MaxIter <integer 1..{policy.max_response_iterations}> end; "
                    "required for Opt+Freq; forbidden for SP"},
            "block_fields": {name: sorted(fields) for name, fields in BLOCK_FIELDS.items()},
            "block_syntax": "%<block> <key> <scalar> ... end; spaces/newlines separate key/value pairs. "
                "Every field needs a value; no bare flags or nested blocks. %maxcore is standalone.",
            "optional_blocks": {"output": {"required": False,
                "fields": {field: ["true", "false", "0", "1"] for field in sorted(BLOCK_FIELDS["output"])},
                "example": "%output JsonPropFile true end"}},
            "geometry": "One line: * xyzfile <charge> 1 <basename.xyz>; no closing *. Pass the current prepared/registered XYZ via SDK dependencies",
            "dependencies": "only the referenced task-local XYZ basename; exact trusted geometry bytes required unless task_geometry explicitly permits a derived candidate",
            "task_geometry": {"enabled": policy.allow_task_geometry,
                "scope": "Python SDK only, with no Request/System/Step/Goal geometry_relation restriction; "
                    "preserve initial atom count and ordered elements; retain initial and candidate hashes. "
                    "Connectivity, atom mapping within equal elements, stereochemistry and geometry quality remain unverified."},
            "request_binding": "method/basis/charge/multiplicity/electronic_state/environment must match current Request and Step",
            "work_units": "SP:1; Opt+Freq:(geom MaxIter+1)+1+3*atom_count; conservative declared slots, not actual iteration counts",
            "unknown_syntax": "rejected; no fallback to input-1", "scientific_validation": "unverified"}


def canonical_native_condition(name, value):
    if name in {"charge", "multiplicity"}:
        return value if type(value) is int else None
    if not isinstance(value, str):
        return None
    lowered = value.strip().casefold()
    if name == "method":
        return "HF" if lowered == "rhf" else METHODS.get(lowered)
    if name == "basis":
        return BASES.get(lowered)
    if name == "electronic_state":
        return {"rhf": "RHF", "rks": "RKS"}.get(lowered)
    if name == "environment":
        return "gas_phase" if lowered in {"gas", "gas_phase", "gas phase"} else None
    return None


def _one(values, category):
    if len(values) != 1:
        raise ValueError(f"bounded analytic profile requires exactly one explicit {category}")
    return values[0]


def admit_profile(launch: dict, dependencies: dict, geometry_name: str | None,
                  policy: NativeInputPolicy, binding: dict | None) -> dict:
    """Derive conditions and conservative reservation units from admitted bytes."""
    facts = launch["actual_conditions"]
    lower = [word.casefold() for word in facts["input_keywords"]]
    if len(set(lower)) != len(lower):
        raise ValueError("duplicate native keywords are ambiguous")
    optimization = [word for word in lower if word in {"opt", "tightopt", "verytightopt"}]
    frequency = [word for word in lower if word in {"freq", "anfreq"}]
    if bool(optimization) != bool(frequency) or len(optimization) > 1 or len(frequency) > 1:
        raise ValueError("bounded profile supports one combined Opt+analytic Freq or one single point")
    if "sp" in lower and optimization:
        raise ValueError("SP conflicts with combined Opt+analytic Freq")
    allowed = set(METHODS) | set(BASES) | {
        "rhf", "rks", "nori", "noautostart", "tightscf", "verytightscf", "sp",
        "opt", "tightopt", "verytightopt", "freq", "anfreq", "defgrid2", "defgrid3",
    }
    if unknown := set(lower) - allowed:
        raise ValueError("unsupported keyword in bounded analytic execution profile: " + ",".join(sorted(unknown)))
    if not {"nori", "noautostart"}.issubset(lower):
        raise ValueError("bounded analytic execution requires explicit NORI and NoAutoStart")
    _one([word for word in lower if word in {"tightscf", "verytightscf"}], "SCF convergence selector")
    if len([word for word in lower if word in {"defgrid2", "defgrid3"}]) > 1:
        raise ValueError("conflicting DFT integration grids")
    method_words = [word for word in lower if word in METHODS]
    if "rhf" in lower and not method_words:
        method_words = ["hf"]
    method = METHODS[_one(method_words, "method")]
    basis = BASES[_one([word for word in lower if word in BASES], "orbital basis")]
    state = _one([word for word in lower if word in {"rhf", "rks"}], "restricted wavefunction type").upper()
    if (method == "HF") != (state == "RHF"):
        raise ValueError("HF requires explicit RHF; the admitted DFT methods require explicit RKS")
    if facts["multiplicity"] != 1:
        raise ValueError("bounded analytic profile currently supports explicit restricted singlets only")
    if geometry_name is None:
        raise ValueError("bounded analytic profile requires a pinned xyzfile matching current Request geometry")
    if facts["atom_count"] > policy.max_atoms:
        raise ValueError("geometry exceeds the frozen native atom limit")
    # Native admission already checked exactly N finite atom rows plus only
    # trailing whitespace; do not interpret the serializer's blank suffix.
    rows = dependencies[geometry_name].decode("ascii").splitlines()[2:2 + facts["atom_count"]]
    symbols = [row.split()[0] for row in rows]
    if set(symbols) - set(ELEMENTS):
        raise ValueError("bounded analytic basis coverage is limited to H-Ar; ECP/relativistic effects are not admitted")
    electrons = sum(ELEMENTS[element] for element in symbols) - facts["charge"]
    if electrons <= 0 or electrons % 2:
        raise ValueError("declared restricted singlet is incompatible with the explicit electron count")
    blocks = facts["input_blocks"]
    scf = blocks.get("scf", {})
    if scf.get("autotrah", "").lower() not in {"false", "0"}:
        raise ValueError("explicit %scf AutoTRAH false is required to exclude implicit fallback iterations")
    if scf.get("convforced", "").lower() not in {"true", "1"}:
        raise ValueError("explicit %scf ConvForced true is required")
    scf_iterations = int(scf.get("maxiter", 0))
    if not 1 <= scf_iterations <= policy.max_scf_iterations:
        raise ValueError("explicit %scf MaxIter exceeds or omits the frozen bound")
    geom_iterations = response_iterations = 0
    if optimization:
        geom = blocks.get("geom", {})
        geom_iterations = int(geom.get("maxiter", 0))
        response_iterations = int(blocks.get("method", {}).get("z_maxiter", 0))
        if not 1 <= geom_iterations <= policy.max_geom_iterations:
            raise ValueError("explicit %geom MaxIter exceeds or omits the frozen bound")
        if geom.get("enforcestrictconvergence", "").lower() not in {"true", "1"}:
            raise ValueError("explicit %geom EnforceStrictConvergence true is required")
        if not 1 <= response_iterations <= policy.max_response_iterations:
            raise ValueError("explicit %method Z_MaxIter exceeds or omits the frozen bound")
    elif "geom" in blocks or "method" in blocks:
        raise ValueError("single point cannot carry optimization/response control blocks")
    actual = {"method": method, "basis": basis, "electronic_state": state,
              "environment": "gas_phase", "charge": facts["charge"], "multiplicity": facts["multiplicity"]}
    if not binding or set(binding.get("conditions", {})) != set(CONDITION_FIELDS):
        raise ValueError("bounded native input requires trusted current Request/Step condition binding")
    for name, value in actual.items():
        expected = canonical_native_condition(name, binding["conditions"][name])
        if expected is None or expected != value:
            raise ValueError("admitted native input differs from current Request/Step condition: " + name)
    geometry_sha256 = hashlib.sha256(dependencies[geometry_name]).hexdigest()
    geometry_relation = "exact_initial"
    if binding.get("geometry_sha256") != geometry_sha256:
        if not (policy.allow_task_geometry and binding.get("allow_task_geometry") is True):
            raise ValueError("native xyzfile bytes differ from current Request prepared/registered geometry")
        if symbols != binding.get("initial_atom_symbols"):
            raise ValueError("task candidate must preserve initial atom count and ordered elements")
        geometry_relation = "task_generated_candidate"
    evaluation_slots = geom_iterations + 1 if optimization else 1
    response_slots = 3 * facts["atom_count"] if frequency else 0
    hessian_slots = int(bool(frequency))
    return {**launch, "admission_version": ADMISSION_VERSION,
            "native_input_policy": policy.model_dump(mode="json"),
            "input_binding": binding,
            "actual_conditions_origin": "admitted_native_input",
            "actual_conditions": {**facts, **actual,
                "run_type": "optimization_and_analytic_frequencies" if optimization else "single_point",
                "origin": "admitted_native_input", "geometry_sha256": geometry_sha256,
                "input_geometry_sha256": geometry_sha256,
                **({"input_geometry_relation": geometry_relation,
                    "initial_geometry_sha256": binding.get("geometry_sha256"),
                    "geometry_validation": "atom count and ordered elements only; topology and quality unverified"}
                   if policy.allow_task_geometry else {}),
                "request_version": binding.get("request_version"),
                "system_id": binding.get("system_id"),
                "scientific_validation": "unverified"},
            "execution_impact": {
                "orca_starts": 1, "postprocess_starts": 0, "external_programs": False,
                "internal_work_units": evaluation_slots + hessian_slots + response_slots,
                "work_unit_contract": "declared-native-workload-1",
                "work_unit_meaning": "conservative declared task slots, not actual SCF/response evaluations",
                "optimization_evaluation_slots": evaluation_slots,
                "analytic_hessian_slots": hessian_slots,
                "response_direction_slots": response_slots,
                "scf_maxiter": scf_iterations, "geom_maxiter": geom_iterations,
                "response_maxiter": response_iterations,
            }}
