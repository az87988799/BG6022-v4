"""Small local configuration; no scientific defaults hidden in environment variables."""

import shutil
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

ModelProfile = Literal["disabled", "thinking_low"]


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    orca_path: Path | None = None
    mpi_path: Path | None = None
    data_root: Path = Path("data")
    model_profile: ModelProfile = "disabled"


def load_config(path: Path | None = None) -> Config:
    values = {}
    if path is not None:
        with path.open("rb") as stream:
            values = tomllib.load(stream)
    for key, command in (("orca_path", "orca"), ("mpi_path", "mpiexec")):
        if key not in values and (found := shutil.which(command)):
            values[key] = found
    config = Config.model_validate(values)
    for name in ("orca_path", "mpi_path", "data_root"):
        value = getattr(config, name)
        if value is not None:
            setattr(config, name, value.expanduser().resolve())
    return config
