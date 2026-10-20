"""Small local configuration; no scientific defaults hidden in environment variables."""

import shutil
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer

from orca_agent.models import (
    BudgetLimits,
    ExecutionConfiguration,
    ModelLimits,
    PermissionSnapshot,
    ResourceLimits,
    RunConfiguration,
)

ModelProfile = Literal["disabled", "thinking_low"]


class TextProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    permission: PermissionSnapshot = Field(default_factory=PermissionSnapshot)
    budget: BudgetLimits = Field(default_factory=BudgetLimits)
    defaults: dict = Field(default_factory=lambda: {
        "method": "HF", "basis": "STO-3G", "charge": 0, "multiplicity": 1,
        "electronic_state": "RHF", "environment": "gas_phase",
    })


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    orca_path: Path | None = None
    mpi_path: Path | None = None
    data_root: Path = Path("data")
    model_profile: ModelProfile = "disabled"
    resources: ResourceLimits = Field(default_factory=ResourceLimits)
    model: ModelLimits = Field(default_factory=ModelLimits)
    text: TextProfile = Field(default_factory=TextProfile)
    execution: ExecutionConfiguration = Field(default_factory=ExecutionConfiguration)
    mechanical_progression: bool = Field(default=True, strict=True)
    workflow_policy: Literal["simplified-1"] | None = "simplified-1"
    observation_delivery_policy: Literal["reported-observation-1"] | None = None

    @model_serializer(mode="wrap")
    def preserve_historical_observation_policy(self, handler):
        value = handler(self)
        if self.observation_delivery_policy is None:
            value.pop("observation_delivery_policy", None)
        return value

    def run_configuration(self) -> RunConfiguration:
        return RunConfiguration(resources=self.resources, model=self.model,
                                model_profile=self.model_profile, execution=self.execution,
                                mechanical_progression=self.mechanical_progression,
                                workflow_policy=self.workflow_policy,
                                observation_delivery_policy=self.observation_delivery_policy)


def load_config(path: Path | None = None) -> Config:
    values = {}
    if path is not None:
        with path.open("rb") as stream:
            values = tomllib.load(stream)
    if values.get("execution", {}).get("backend", "windows-lpac") == "windows-lpac":
        for key, command in (("orca_path", "orca"), ("mpi_path", "mpiexec")):
            if key not in values and (found := shutil.which(command)):
                values[key] = found
    config = Config.model_validate(values)
    for name in ("orca_path", "mpi_path", "data_root"):
        value = getattr(config, name)
        if value is not None:
            setattr(config, name, value.expanduser().resolve())
    return config
