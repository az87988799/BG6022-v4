"""Bounded immutable task-file creation; writing never executes its content."""

import hashlib
from typing import Literal

from pydantic import Field, field_validator, model_serializer, model_validator

from orca_agent.models import CodeRef, InputManifestEntry, Record


class ExactReplacement(Record):
    old: str = Field(strict=True, min_length=1, max_length=16384)
    new: str = Field(strict=True, max_length=16384)


class WriteParameters(Record):
    model_config = {"json_schema_extra": {"oneOf": [
        {"required": ["content"], "properties": {
            "content": {"type": "string"}, "base": {"type": "null"}, "edits": {"maxItems": 0}}},
        {"required": ["base", "edits"], "properties": {
            "content": {"type": "null"}, "role": {"const": "python"},
            "base": {"type": "object", "properties": {"language": {"const": "python"}}},
            "edits": {"minItems": 1}}}]}}
    name: str = Field(strict=True, min_length=1, max_length=120)
    content: str | None = Field(default=None, strict=True, max_length=65536)
    role: Literal["python", "orca", "text"]
    base: CodeRef | None = None
    edits: list[ExactReplacement] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def one_write_form(self):
        if self.content is not None:
            if self.base is not None or self.edits:
                raise ValueError("content and base/edits are mutually exclusive")
        elif self.base is None or not self.edits or self.role != "python" or self.base.language != "python":
            raise ValueError("patch requires a Python base, role=python and explicit edits")
        return self

    @model_serializer(mode="wrap")
    def preserve_original_write_shape(self, handler):
        value = handler(self)
        for name in ("content", "base", "edits"):
            if value.get(name) is None or name == "edits" and not value[name]:
                value.pop(name, None)
        return value

    @field_validator("name")
    @classmethod
    def basename_only(cls, value):
        InputManifestEntry(artifact_id="validation", sha256="0" * 64,
                           target_path=value, purpose="task_file")
        if "/" in value or "\\" in value:
            raise ValueError("task file name must be a basename")
        return value

    @field_validator("content")
    @classmethod
    def utf8_bound(cls, value):
        if value is not None and (len(value.encode("utf-8")) > 65536 or "\x00" in value):
            raise ValueError("task file must be UTF-8 text of at most 64 KiB without NUL")
        return value


def _content(store, run, parameters):
    """Resolve only an explicitly authorized immutable base, never a host path."""
    if parameters.content is not None:
        return parameters.content
    from orca_agent.store import StoreError
    base = parameters.base
    authorized = set(run.permission.artifact_ids) | store.verified_own_artifacts(run)
    if base.artifact_id not in authorized:
        raise StoreError("patch base is outside authorized settled artifacts")
    artifact = store.load_artifact(base.artifact_id)
    if artifact.sha256 != base.sha256:
        raise StoreError("patch base hash differs from pinned Artifact")
    if artifact.size > 65536:
        raise ValueError("patch base exceeds 64 KiB")
    path = store.artifact_path(base.artifact_id)
    with path.open("rb") as source:
        raw = source.read(65537)
    if len(raw) != artifact.size or len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != base.sha256:
        raise StoreError("patch base changed while reading pinned content")
    original = raw.decode("utf-8")
    WriteParameters.utf8_bound(original)
    ranges = []
    for edit in parameters.edits:
        # All offsets refer to the same original bytes/text. New text never
        # becomes another edit's matching input; no fuzzy or sequential repair.
        start = original.find(edit.old)
        if start < 0 or original.find(edit.old, start + 1) != -1:
            raise ValueError("each patch old fragment must match exactly once")
        end = start + len(edit.old)
        if edit.old == edit.new or any(start < prior_end and prior_start < end
                                      for prior_start, prior_end, _ in ranges):
            raise ValueError("patch edits must change text and must not overlap")
        ranges.append((start, end, edit.new))
    updated = original
    for start, end, replacement in sorted(ranges, reverse=True):
        updated = updated[:start] + replacement + updated[end:]
    WriteParameters.utf8_bound(updated)
    if updated == original:
        raise ValueError("patch must change the original artifact")
    return updated


def preflight(store, run, parameters, tool, consumption):
    """Validate base access and exact edits before reserving a write quota."""
    _content(store, run, WriteParameters.model_validate(parameters))


def write(store, run, call):
    from orca_agent.store import ControlChanged, atomic_write

    parameters = WriteParameters.model_validate(call.parameters)
    with store.control_lock(run.id):
        store.check_execution_control(run)
        if call.control_generation != run.control_generation:
            raise ControlChanged("reserved task file basis differs from current control")
        path = store.path(f"runs/{run.id}/calls/{call.id}/files/{parameters.name}")
        content = _content(store, run, parameters).encode("utf-8")
        atomic_write(path, content, immutable=True)
        artifact = store.import_artifact(path, "code" if parameters.role == "python" else
            "native_input" if parameters.role == "orca" else "task_text", run_id=run.id,
            source={"call_id": call.id, "role": parameters.role, "name": parameters.name})
    return {"artifact_id": artifact.id, "sha256": artifact.sha256, "name": parameters.name,
            "bytes": len(content), "role": parameters.role, "artifact_ids": [artifact.id]}
