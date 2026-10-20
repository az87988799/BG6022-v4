"""Reproduce two V3 figures from saved evidence only.

No model, ORCA, network, or product-runtime invocation. Requires Matplotlib;
an existing figures/.runtime is used when present, without installing anything.
Run from any working directory: python path/to/make_v3_figures.py
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "figures"
RUNTIME = OUT / ".runtime"
ABI = f"cp{sys.version_info.major}{sys.version_info.minor}"
if RUNTIME.is_dir() and any((RUNTIME / "numpy" / "_core").glob(f"_multiarray_umath.{ABI}-*.pyd")):
    sys.path.insert(0, str(RUNTIME))
    # Reuse the pre-existing ignored font cache; never install dependencies.
    os.environ.setdefault("MPLCONFIGDIR", str(RUNTIME / "mplconfig"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch, Rectangle
from matplotlib.ticker import MaxNLocator

NAVY = "#17324D"
TEAL = "#148C8C"
AMBER = "#D99B2B"
INK = "#26384A"
GRAY = "#EDF0F3"
GRID = "#DCE3E8"
MUTED = "#61717F"

TASKS = ["T001", "T002", "T003", "T004", "T005"]
OBJECTS = ["h2", "water", "ammonia", "co2", "methane", "ethanol"]


def validate(data: dict) -> None:
    matrix, core = data["matrix"], data["core"]
    assert data["schema"] == "bg6022.report.historical-v3-evidence.v1"
    assert len(matrix["cells"]) == matrix["cell_count"] == 11
    assert sum(cell["passed"] for cell in matrix["cells"]) == 11
    assert sum(cell["orca_attempts"] for cell in matrix["cells"]) == 16
    assert matrix["cost"]["calls"] == 0
    assert matrix["cost"]["orca_attempts"] == 16
    assert len({(cell["task_id"], cell["object_id"]) for cell in matrix["cells"]}) == 11
    for cell in matrix["cells"]:
        assert cell["task_id"] in TASKS and cell["object_id"] in OBJECTS
        assert cell["wall_time_seconds"] >= 0
        assert all(result["status"] == "succeeded" for result in cell["results"])
    assert len(core["items"]) == core["item_count"] == core["passed_items"] == 18
    assert sum(row["orca_attempts"] > 0 for row in core["items"]) == 16
    assert all(row["passed"] for row in core["items"])
    assert core["cost"]["calls"] == 37
    expected_calls = {"semantic": 19, "answer": 17, "repair": 1, "intake": 0, "planner": 0}
    assert {key: core["cost"]["by_purpose"][key]["calls"] for key in expected_calls} == expected_calls
    assert sum(row["llm_calls"] for row in core["items"]) == 37
    assert sum(row["orca_attempts"] for row in core["items"]) == core["cost"]["orca_attempts"] == 23
    assert sum(row["repair_attempts"] for row in core["items"]) == 1
    assert sum(row["orca_failures"] for row in core["items"]) == 1
    assert len(core["by_task_time"]) == 10
    for task, group in core["by_task_time"].items():
        rows = [row for row in core["items"] if row["task_id"] == task]
        assert len(rows) == group["items"]
        assert math.isclose(sum(row["wall_time_seconds"] for row in rows), group["wall_time_seconds"], abs_tol=1e-7)
    for task in ("C003_ambiguous_iteration", "C004_unsupported_global_conformer"):
        assert core["by_task_time"][task]["orca_attempts"] == 0
    verification = data["verification_summary"]
    assert verification["artifact_missing_or_mismatched"] == []
    assert verification["raw_results_not_matching_report_snapshot"] == []
    assert verification["energy_token_checks_failed"] == []


def style() -> None:
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 10,
        "axes.labelsize": 10, "axes.titlesize": 10.5,
        "xtick.labelsize": 9, "ytick.labelsize": 9,
        "legend.fontsize": 9, "text.color": INK,
        "axes.labelcolor": INK, "xtick.color": INK, "ytick.color": INK,
        "axes.edgecolor": "#9AA7B1", "axes.linewidth": 0.7,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.axisbelow": True, "grid.color": GRID, "grid.linewidth": 0.6,
        "figure.facecolor": "white", "axes.facecolor": "white",
        "savefig.facecolor": "white", "pdf.fonttype": 42, "ps.fonttype": 42,
        "savefig.dpi": 220,
    })


def export(fig: plt.Figure, name: str) -> None:
    # Fixed 7.2-inch width; avoid bbox_inches='tight' changing the publication size.
    fig.canvas.draw()
    for text in fig.findobj(matplotlib.text.Text):
        if text.get_text() and text.get_visible():
            assert text.get_fontsize() >= 9, text.get_text()
    fig.savefig(OUT / f"{name}.pdf", metadata={
        "Creator": "make_v3_figures.py / Matplotlib",
        "Title": name,
        "Subject": "V3 evaluation evidence; no new scientific execution",
    })
    fig.savefig(OUT / f"{name}.png", dpi=220)
    plt.close(fig)


def scientific_matrix(data: dict) -> None:
    cells = {(cell["task_id"], cell["object_id"]): cell for cell in data["matrix"]["cells"]}
    fig = plt.figure(figsize=(7.2, 3.8))
    ax = fig.add_axes([0.285, 0.245, 0.665, 0.54])
    for row, task in enumerate(TASKS):
        for column, obj in enumerate(OBJECTS):
            cell = cells.get((task, obj))
            ax.add_patch(Rectangle((column - 0.5, row - 0.5), 1, 1,
                                   facecolor=TEAL if cell else GRAY,
                                   edgecolor="white", linewidth=2.5))
            if cell:
                ax.text(column, row - 0.12, "PASS", ha="center", va="center",
                        color="white", weight="bold", fontsize=10)
                ax.text(column, row + 0.21, f"{cell['wall_time_seconds']:.1f} s",
                        ha="center", va="center", color="white", fontsize=9)
    ax.set_xlim(-0.5, len(OBJECTS) - 0.5)
    ax.set_ylim(len(TASKS) - 0.5, -0.5)
    ax.set_xticks(range(len(OBJECTS)), [r"H$_2$", r"H$_2$O", r"NH$_3$", r"CO$_2$", r"CH$_4$", "Ethanol"])
    ax.xaxis.tick_top()
    ax.set_yticks(range(len(TASKS)), [
        "T001  Single point", "T002  Optimization", "T003  Opt + frequency",
        "T004  Dual SP difference", "T005  Independent dual Opt",
    ])
    ax.tick_params(axis="both", length=0, pad=8)
    for spine in ax.spines.values():
        spine.set_visible(False)
    fig.text(0.055, 0.965, "V3 fixed scientific task matrix", fontsize=12,
             weight="bold", color=NAVY, va="top")
    fig.text(0.055, 0.895, "11 selected cells passed | 16 real ORCA attempts | 0 LLM calls",
             fontsize=10, va="top")
    fig.legend(handles=[Patch(facecolor=TEAL, label="Selected cell: PASS"),
                        Patch(facecolor=GRAY, label="Outside selected matrix")],
               loc="lower left", bbox_to_anchor=(0.045, 0.12), ncol=2,
               frameon=False, handlelength=1.2, columnspacing=1.8)
    fig.text(0.055, 0.092, "Fixed requests and plans; cell annotations report wall time.", fontsize=9)
    fig.text(0.055, 0.035, "ORCA 6.1.1 | 4 cores / 1024 MB / MaxCore 192 MB | 2026-09-24", fontsize=9, color=MUTED)
    export(fig, "v3-scientific-matrix")


def core_costs(data: dict) -> None:
    core = data["core"]
    fig = plt.figure(figsize=(7.2, 4.8))
    left = fig.add_axes([0.14, 0.29, 0.235, 0.50])
    right = fig.add_axes([0.645, 0.29, 0.30, 0.50])

    purposes = ["semantic", "answer", "repair", "intake", "planner"]
    calls = [core["cost"]["by_purpose"][purpose]["calls"] for purpose in purposes]
    left.barh(range(5), calls, height=0.58, color=[NAVY, TEAL, AMBER, GRAY, GRAY])
    left.set_yticks(range(5), [purpose.capitalize() for purpose in purposes])
    left.invert_yaxis()
    left.set_xlim(0, 24)
    left.set_xticks([0, 10, 20])
    left.set_xlabel("Model calls")
    left.set_title("LLM calls (total 37)", loc="left", pad=13, weight="bold", color=NAVY)
    left.grid(axis="x")
    left.tick_params(axis="y", length=0, pad=5)
    for y, value in enumerate(calls):
        left.text(value + 0.6, y, str(value), va="center", fontsize=9.5)

    tasks = ["T001", "T002", "T003", "T004", "T005", "C001_modify_second_method",
             "C002_history_energy_query", "C003_ambiguous_iteration",
             "C004_unsupported_global_conformer", "C005_controlled_repair"]
    labels = ["T001  SP", "T002  Opt", "T003  Opt / Freq", "T004  Dual SP", "T005  Dual Opt",
              "C001  Method edit", "C002  History query", "C003  Clarify*", "C004  Refusal*", "C005  Repair"]
    seconds = [core["by_task_time"][task]["wall_time_seconds"] for task in tasks]
    colors = [AMBER if task.startswith(("C003", "C004")) else TEAL for task in tasks]
    right.barh(range(10), seconds, height=0.64, color=colors)
    right.set_yticks(range(10), labels)
    right.invert_yaxis()
    right.set_xlim(0, 470)
    right.set_xticks([0, 200, 400])
    right.set_xlabel("Item wall time, summed (s)")
    right.set_title("Time by task group", loc="left", pad=13, weight="bold", color=NAVY)
    right.grid(axis="x")
    right.tick_params(axis="y", length=0, pad=5)
    for y, value in enumerate(seconds):
        label = f"{value:.2f}" if value < 0.1 else f"{value:.1f}"
        right.text(value + 8, y, label, va="center", fontsize=9)

    fig.text(0.055, 0.965, "V3 fixed-scenario evaluation: computational costs", fontsize=12,
             weight="bold", color=NAVY, va="top")
    fig.text(0.055, 0.905, "18/18 fixed scenarios | 16 compute items | 147,938 model tokens",
             fontsize=10, va="top")
    fig.text(0.055, 0.175, "* Two no-compute scenarios: clarification and refusal (0 ORCA attempts).", fontsize=9)
    fig.text(0.055, 0.125, "23 ORCA attempts include one preset optimization failure and bounded repair.", fontsize=9)
    fig.text(0.055, 0.075, "Summed item times; total elapsed time: 667.55 s. T002 includes 2 name cases.", fontsize=9)
    fig.text(0.055, 0.025, "Fixed prompts; scripted confirmations | DeepSeek Flash | 2026-09-24", fontsize=9, color=MUTED)
    export(fig, "v3-core-costs")


def main() -> None:
    data = json.loads((ROOT / "historical-v3-evidence.json").read_text(encoding="utf-8"))
    validate(data)
    style()
    scientific_matrix(data)
    core_costs(data)
    print(json.dumps({"validation": "passed", "historical_core_scores": "18/18 fixed scenarios",
                      "historical_matrix": "11 selected cells / 16 real ORCA attempts / 0 LLM calls",
                      "generated": [str(OUT / f"{name}.{extension}")
                                    for name in ("v3-scientific-matrix", "v3-core-costs")
                                    for extension in ("pdf", "png")]}, indent=2))


if __name__ == "__main__":
    main()
