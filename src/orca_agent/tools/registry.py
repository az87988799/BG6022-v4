"""Single source of Tool schemas, effects, ports and registered entry points."""

from __future__ import annotations

import importlib
import math
from collections import Counter
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import Field, field_validator

from orca_agent.models import CalculationParameters, Identifier, Record, Tool


class EvidenceListParameters(Record):
    run_id: Identifier
    offset: Annotated[int, Field(strict=True, ge=0, le=10000)] = 0
    limit: Annotated[int, Field(strict=True, ge=1, le=200)] = 40


class EvidenceTextParameters(Record):
    artifact_id: Identifier
    start_line: Annotated[int, Field(strict=True, ge=1, le=10000)] = 1
    lines: Annotated[int, Field(strict=True, ge=1, le=200)] = 40


class EvidenceFieldParameters(Record):
    artifact_id: Identifier
    field: Annotated[str, Field(strict=True, min_length=1, max_length=1211)]

    @field_validator("field")
    @classmethod
    def bounded_path(cls, value: str) -> str:
        parts = value.split(".")
        if len(parts) > 12 or any(not part or len(part) > 100 for part in parts):
            raise ValueError("field path exceeds bounded depth or key length")
        return value


@dataclass(frozen=True)
class _Registration:
    """Local immutable registry entry, with no independent persisted lifecycle."""

    tool: Tool
    parameters: type[Record]


def _register(parameters: type[Record], **metadata: Any) -> _Registration:
    return _Registration(Tool(parameter_schema=parameters.model_json_schema(), **metadata), parameters)


TOOLS: dict[str, _Registration] = {
    "orca.sp": _register(
        CalculationParameters,
        name="orca.sp",
        description="H2O/CH4 HF/STO-3G neutral singlet single-point electronic energy in Eh.",
        output_ports=["energy"],
        implementation="orca_agent.tools.electronic.execute",
    ),
    "orca.opt": _register(
        CalculationParameters,
        name="orca.opt",
        description=(
            "H2O/CH4 unconstrained HF/STO-3G geometry optimization in angstrom. Convergence does not "
            "establish a minimum or its vibrational stability."
        ),
        output_ports=["energy", "optimized_geometry"],
        implementation="orca_agent.tools.geometry.execute",
    ),
    "evidence.list": _register(
        EvidenceListParameters, name="evidence.list",
        description="List a bounded page of registered evidence metadata; no scientific qualification.",
        input_roles=["run_evidence_index"], output_ports=[], observation_outputs=["artifact_metadata"],
        effects=["read_registered_artifact"], max_cores=0, max_memory_mb=0,
        check_version="evidence-read-1", implementation="orca_agent.tools.evidence.list_artifacts",
    ),
    "evidence.text": _register(
        EvidenceTextParameters, name="evidence.text",
        description="Read bounded raw text with line numbers and verified source hash; no execution.",
        input_roles=["registered_artifact"], output_ports=[], observation_outputs=["text_window"],
        effects=["read_registered_artifact"], max_cores=0, max_memory_mb=0,
        check_version="evidence-read-1", implementation="orca_agent.tools.evidence.inspect_artifact",
    ),
    "evidence.field": _register(
        EvidenceFieldParameters, name="evidence.field",
        description="Read a bounded existing JSON field as an unverified observation; no conversion.",
        input_roles=["registered_artifact"], output_ports=[], observation_outputs=["field_observation"],
        effects=["read_registered_artifact"], max_cores=0, max_memory_mb=0,
        check_version="evidence-read-1", implementation="orca_agent.tools.evidence.read_field",
    ),
}


def _definition(name: str) -> _Registration:
    try:
        return TOOLS[name]
    except KeyError as exc:
        raise ValueError("unregistered tool") from exc


def get_tool(name: str) -> Tool:
    return _definition(name).tool.model_copy(deep=True)


def catalog() -> list[dict[str, Any]]:
    return [get_tool(name).model_dump(mode="json") for name in TOOLS]


def validate_parameters(name: str, value: CalculationParameters | dict) -> CalculationParameters:
    definition = _definition(name)
    if definition.parameters is not CalculationParameters:
        raise ValueError("a scientific Step requires a registered scientific tool")
    parameters = definition.parameters.model_validate(
        value.model_dump() if isinstance(value, CalculationParameters) else value
    )
    if name == "orca.sp" and parameters.timeout_seconds > 300:
        raise ValueError("single-point deadline may not exceed 300 seconds")
    return parameters


def dispatch_evidence(store: Any, name: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """Dispatch only a fixed, registered observation operation after schema validation."""
    definition = _definition(name)
    if definition.tool.effects != ["read_registered_artifact"] or definition.tool.output_ports:
        raise ValueError("evidence dispatch cannot execute scientific tools")
    validated = definition.parameters.model_validate(parameters)
    module, function = definition.tool.implementation.rsplit(".", 1)
    implementation = getattr(importlib.import_module(module), function)
    return implementation(store, **validated.model_dump())


def validate_geometry(
    xyz_text: str, parameters: CalculationParameters
) -> list[tuple[str, float, float, float]]:
    """Validate the frozen XYZ profile; coordinates are explicitly angstrom.

    XYZ comment text remains data, including when it resembles instructions.
    Closed-shell parity is checked without attempting to select an electronic state.
    Only the frozen H2O and CH4 compositions are admitted; arbitrary H/C/O molecules
    have no representative scientific validation in this initial profile.
    """
    if len(xyz_text.encode("utf-8")) > 65536:
        raise ValueError("geometry exceeds the 64 KiB input limit")
    lines = xyz_text.splitlines()
    if len(lines) < 3:
        raise ValueError("XYZ requires a count, comment, and coordinates")
    try:
        count = int(lines[0].strip())
    except ValueError as exc:
        raise ValueError("XYZ first line must be an atom count") from exc
    if not 1 <= count <= 20:
        raise ValueError("only 1 to 20 atoms are within this profile")
    rows = lines[2:]
    while rows and not rows[-1].strip():
        rows.pop()
    if len(rows) != count:
        raise ValueError("XYZ atom count does not match coordinates")
    atoms: list[tuple[str, float, float, float]] = []
    nuclear_charge = {"H": 1, "C": 6, "O": 8}
    for row in rows:
        tokens = row.split()
        if len(tokens) != 4 or tokens[0] not in nuclear_charge:
            raise ValueError("XYZ supports only H/C/O and exactly three coordinates")
        try:
            coordinates = tuple(float(token) for token in tokens[1:])
        except ValueError as exc:
            raise ValueError("XYZ coordinate is not numeric") from exc
        if any(not math.isfinite(x) or abs(x) > 1000 for x in coordinates):
            raise ValueError("coordinates must be finite and within 1000 angstrom")
        atoms.append((tokens[0], *coordinates))
    electrons = sum(nuclear_charge[atom[0]] for atom in atoms) - parameters.charge
    if electrons <= 0 or electrons % 2 != (parameters.multiplicity - 1) % 2:
        raise ValueError("electron count is incompatible with the explicitly supplied multiplicity")
    composition = Counter(atom[0] for atom in atoms)
    if composition not in ({"H": 2, "O": 1}, {"H": 4, "C": 1}):
        raise ValueError("only the frozen H2O and CH4 compositions are admitted")
    for index, atom in enumerate(atoms):
        for other in atoms[:index]:
            if math.dist(atom[1:], other[1:]) < 0.1:
                raise ValueError("coincident or unphysically close atoms")
    return atoms
