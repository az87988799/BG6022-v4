"""Pure presentation projection shared by context and semantic schemas."""

from typing import Any


def project_schema(schema: Any) -> Any:
    if isinstance(schema, dict):
        projected = {key: project_schema(value) for key, value in schema.items()
                # Pydantic's discriminator is a dispatch hint, not a JSON Schema
                # constraint. Annotation keywords including default do not
                # validate input; omitting them does not change registry/Pydantic
                # defaults. Required/type/limits and oneOf kind constants remain.
                if key not in {"title", "description", "default", "discriminator"}
                and not (key == "additionalProperties" and value is True)
                and not (key == "type" and "const" in schema)
                and not (key == "type" and "enum" in schema)}
        if (projected.get("additionalProperties") is False and projected.get("required")
                and set(projected["required"]) == set(projected.get("properties", {}))):
            projected["minProperties"] = len(projected.pop("required"))
        alternatives = projected.get("anyOf")
        if alternatives and all(set(branch) == {"type"} and isinstance(branch["type"], str)
                                for branch in alternatives):
            projected["type"] = [branch["type"] for branch in projected.pop("anyOf")]
        return projected
    if isinstance(schema, list):
        return [project_schema(value) for value in schema]
    return schema
