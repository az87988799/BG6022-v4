"""Pinned inert identities for tests that replace the scientific execution entry."""

from orca_agent.config import Config
from orca_agent.store import sha256_file


def freeze_offline_runtime(store, run):
    """Exercise real identity validation without inspecting installed engines.

    The files are deliberately not executables. Callers must replace the ORCA
    execution entry; this fixture is not evidence of a compatible live runtime.
    """
    directory = store.root / "offline-runtime"
    directory.mkdir(exist_ok=True)
    paths = {name: directory / (name + ".exe") for name in ("orca", "mpi")}
    for name, path in paths.items():
        if not path.exists():
            path.write_bytes(f"inert offline {name} identity; never execute\n".encode())
    config = Config(orca_path=paths["orca"], mpi_path=paths["mpi"])
    snapshot = {"offline_fixture_only": True, "backend": "windows-lpac",
                "execution_configuration": config.execution.model_dump(mode="json"),
                **{name: {"path": str(path), "sha256": sha256_file(path),
                          "version": "6.1.1" if name == "orca" else "offline-inert"}
                   for name, path in paths.items()}}
    store._write_json(f"runs/{run.id}/environment.json", snapshot, immutable=True)
    return config
