"""Bounded immutable task-file creation; writing never executes its content."""

from typing import Literal

from pydantic import Field, field_validator

from orca_agent.models import InputManifestEntry, Record


class WriteParameters(Record):
    name: str = Field(strict=True, min_length=1, max_length=120)
    content: str = Field(strict=True, max_length=65536)
    role: Literal["python", "orca", "text"]

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
        if len(value.encode("utf-8")) > 65536 or "\x00" in value:
            raise ValueError("task file must be UTF-8 text of at most 64 KiB without NUL")
        return value


def write(store, run, call):
    from orca_agent.store import ControlChanged, atomic_write

    parameters = WriteParameters.model_validate(call.parameters)
    with store.control_lock(run.id):
        store.check_execution_control(run)
        if call.control_generation != run.control_generation:
            raise ControlChanged("reserved task file basis differs from current control")
        path = store.path(f"runs/{run.id}/calls/{call.id}/files/{parameters.name}")
        content = parameters.content.encode("utf-8")
        atomic_write(path, content, immutable=True)
        artifact = store.import_artifact(path, "code" if parameters.role == "python" else
            "native_input" if parameters.role == "orca" else "task_text", run_id=run.id,
            source={"call_id": call.id, "role": parameters.role, "name": parameters.name})
    return {"artifact_id": artifact.id, "sha256": artifact.sha256, "name": parameters.name,
            "bytes": len(content), "role": parameters.role, "artifact_ids": [artifact.id]}
