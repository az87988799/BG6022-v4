"""Reproduce report figures from experiment-data.json; no scientific execution.

Requires Python 3.11+ and Matplotlib.  Validates all plotted aggregate values
and recomputes relative energies with Decimal before exporting vector PDF/PNG.
Run from any working directory: python path/to/make_figures.py
"""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "figures"
RUNTIME = OUT / ".runtime"
ABI = f"cp{sys.version_info.major}{sys.version_info.minor}"
if RUNTIME.is_dir() and any((RUNTIME / "numpy" / "_core").glob(f"_multiarray_umath.{ABI}-*.pyd")):
    sys.path.insert(0, str(RUNTIME))
    os.environ.setdefault("MPLCONFIGDIR", str(RUNTIME / "mplconfig"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

NAVY, TEAL, AMBER, INK = "#17324D", "#148C8C", "#D99B2B", "#26384A"


def validate(data: dict) -> dict:
    latest = data["latest_batch"]
    rows = latest["tasks"]
    assert [row["id"] for row in rows] == ["T1", "T2", "T3", "T4", "T5", "H1", "H2"]
    for metric in ("model_calls", "model_tokens", "orca_starts", "orca_normal_terminations"):
        assert sum(row[metric] for row in rows) == latest["reported_totals"][metric], metric
    assert all(row["orca_normal_terminations"] <= row["orca_starts"] for row in rows)
    assert sum(row["complete_goal_pass"] for row in rows) == 0
    history = data["evaluation_history"]
    for metric in ("model_calls", "model_tokens", "orca_starts", "task_attempts"):
        assert sum(row[metric] for row in history["batches"]) == history["reported_totals"][metric], metric
    assert all(row["complete_goal_passes"] == 0 and row["task_attempts"] == 7 for row in history["batches"])
    assert history["batches"][-1]["model_tokens"] == latest["reported_totals"]["model_tokens"]
    conformers = data["bounded_conformers"]
    members = conformers["members"]
    minimum = min(Decimal(row["energy_hartree"]) for row in members)
    factor = Decimal(str(conformers["hartree_to_kj_mol"]["value"]))
    calculated = []
    for row in members:
        relative = (Decimal(row["energy_hartree"]) - minimum) * factor
        assert abs(relative - Decimal(row["relative_energy_kj_mol_reported"])) <= Decimal("0.00000051"), row["id"]
        calculated.append(float(relative))
    assert len(members) == conformers["orca_evaluated_count"]["value"] == 4
    assert conformers["saved_candidate_count"]["value"] == 12
    return {"latest_model_tokens": 1048678, "latest_orca_starts": 8,
            "latest_orca_normal_terminations": 7, "history_model_tokens": 5512391,
            "history_model_calls": 294, "history_orca_starts": 13,
            "relative_energies_recomputed_kj_mol": calculated}


def style() -> None:
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 10.5,
        "axes.labelsize": 10.5, "axes.titlesize": 11,
        "xtick.labelsize": 10, "ytick.labelsize": 10,
        "legend.fontsize": 9.5, "text.color": INK,
        "axes.labelcolor": INK, "xtick.color": INK, "ytick.color": INK,
        "axes.edgecolor": "#9AA7B1", "axes.linewidth": 0.7,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.axisbelow": True, "grid.color": "#DCE3E8",
        "grid.linewidth": 0.6, "figure.facecolor": "white",
        "axes.facecolor": "white", "savefig.facecolor": "white",
        "pdf.fonttype": 42, "ps.fonttype": 42,
        "svg.fonttype": "none", "savefig.dpi": 220,
    })


def export(fig: plt.Figure, name: str) -> None:
    fig.canvas.draw()
    for text in fig.findobj(matplotlib.text.Text):
        if text.get_text() and text.get_visible():
            assert text.get_fontsize() >= 9, text.get_text()
    fig.savefig(OUT / f"{name}.pdf", metadata={"Creator": "make_figures.py / Matplotlib", "Title": name})
    fig.savefig(OUT / f"{name}.png", dpi=220)
    plt.close(fig)


def latest_tasks(data: dict) -> None:
    rows = data["latest_batch"]["tasks"]
    labels = [row["id"] for row in rows]
    y = list(range(len(rows)))
    tokens = [row["model_tokens"] / 1000 for row in rows]
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 4.4), gridspec_kw={"width_ratios": [1.25, 1.0]})
    fig.subplots_adjust(left=0.075, right=0.97, top=0.85, bottom=0.23, wspace=0.35)
    left, right = axes
    left.barh(y, tokens, color=NAVY, height=0.61)
    left.set(yticks=y, yticklabels=labels, xlabel="Model tokens (thousands)", xlim=(0, 405))
    left.set_title("(a) Model usage", loc="left", pad=12)
    left.invert_yaxis()
    left.xaxis.set_major_locator(MaxNLocator(4))
    left.grid(axis="x")
    for yi, value in zip(y, tokens):
        left.text(value + 5, yi, f"{value:.1f}", va="center", fontsize=9.5)
    starts = [row["orca_starts"] for row in rows]
    normals = [row["orca_normal_terminations"] for row in rows]
    right.barh([yi - 0.18 for yi in y], starts, height=0.32, color=TEAL, label="Started")
    right.barh([yi + 0.18 for yi in y], normals, height=0.32, color=AMBER, label="Normal termination")
    right.set(yticks=y, yticklabels=labels, xlabel="ORCA calculations", xlim=(0, 6.8))
    right.set_title("(b) Execution facts", loc="left", pad=12)
    right.invert_yaxis()
    right.xaxis.set_major_locator(MaxNLocator(integer=True, nbins=4))
    right.grid(axis="x")
    for yi, (started, normal) in enumerate(zip(starts, normals)):
        right.text(started + 0.10, yi - 0.18, str(started), va="center", fontsize=9.5)
        right.text(normal + 0.10, yi + 0.18, str(normal), va="center", fontsize=9.5)
    handles, legend_labels = right.get_legend_handles_labels()
    fig.legend(handles, legend_labels, loc="lower right", bbox_to_anchor=(0.97, 0.072),
               frameon=False, ncol=2, borderaxespad=0, handlelength=1.1,
               columnspacing=1.1, handletextpad=0.5)
    fig.text(0.075, 0.965, "V4 final evaluation: complete task goals 0/7", fontsize=11, weight="bold", va="top")
    fig.text(0.075, 0.025, "Normal termination does not establish scientific qualification or goal completion.", fontsize=9)
    export(fig, "latest-tasks")


def bounded_conformers(data: dict, verified: dict) -> None:
    conformers = data["bounded_conformers"]
    rows = conformers["members"]
    energies = verified["relative_energies_recomputed_kj_mol"]
    fig, ax = plt.subplots(figsize=(6.7, 4.0))
    fig.subplots_adjust(left=0.13, right=0.97, top=0.86, bottom=0.34)
    bars = ax.bar([row["id"] for row in rows], energies, color=[TEAL, NAVY, NAVY, NAVY], width=0.62)
    ax.scatter([0], [0], s=25, color=TEAL, zorder=3)
    ax.set(ylabel=r"$\Delta E$ (kJ mol$^{-1}$)", ylim=(0, 6.3))
    ax.yaxis.set_major_locator(MaxNLocator(integer=True, nbins=5))
    ax.grid(axis="y")
    for bar, value in zip(bars, energies):
        ax.text(bar.get_x() + bar.get_width() / 2, value + 0.13, f"{value:.3f}", ha="center", fontsize=10)
    fig.text(0.13, 0.965, "V4 n-hexane: relative energies in a bounded sample", fontsize=11, weight="bold", va="top")
    fig.text(0.13, 0.205, "B3LYP/6-31G single points; reference: lowest of 4/12 candidates.", fontsize=9)
    fig.text(0.13, 0.15, "No optimization, frequencies, free energies or global-minimum claim.", fontsize=9)
    fig.text(0.13, 0.095, "Force-field convergence and candidate distinctness not fully verified.", fontsize=9)
    fig.text(0.13, 0.04, "Metadata-assisted reporting; not an autonomous benchmark success.", fontsize=9)
    export(fig, "bounded-conformers")


def evaluation_history(data: dict) -> None:
    rows = data["evaluation_history"]["batches"]
    labels = [str(index) for index, _ in enumerate(rows, start=1)]
    tokens = [row["model_tokens"] / 1_000_000 for row in rows]
    starts = [row["orca_starts"] for row in rows]
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.95))
    fig.subplots_adjust(left=0.085, right=0.98, top=0.82, bottom=0.23, wspace=0.36)
    for ax, values, color, title, ylabel, ymax in [
        (axes[0], tokens, NAVY, "(a) Model usage", "Model tokens (millions)", 2.55),
        (axes[1], starts, TEAL, "(b) ORCA starts", "Actual ORCA starts", 9.5),
    ]:
        bars = ax.bar(labels, values, width=0.63, color=color)
        ax.set(ylabel=ylabel, ylim=(0, ymax), xlabel="Evaluation iteration")
        ax.set_title(title, loc="left", pad=12)
        ax.grid(axis="y")
        if ax is axes[1]:
            ax.yaxis.set_major_locator(MaxNLocator(integer=True, nbins=5))
        for bar, value in zip(bars, values):
            display = f"{value:.3f}" if ax is axes[0] else str(value)
            ax.text(bar.get_x() + bar.get_width() / 2, value + ymax * 0.035, display,
                    ha="center", fontsize=9.5)
    fig.text(0.085, 0.965, "V4 successive evaluations: complete task goals 0/7 in each", fontsize=10.5, weight="bold", va="top")
    fig.text(0.085, 0.075, "Different versions and repeated task exposure preclude causal comparisons.", fontsize=9)
    fig.text(0.085, 0.025, "Resource use and ORCA starts do not establish improved task completion.", fontsize=9)
    export(fig, "evaluation-history")


def main() -> None:
    data = json.loads((ROOT / "experiment-data.json").read_text(encoding="utf-8"))
    verified = validate(data)
    OUT.mkdir(exist_ok=True)
    style()
    latest_tasks(data)
    bounded_conformers(data, verified)
    evaluation_history(data)
    print(json.dumps({"validation": "passed", **verified,
                      "exports": [str(OUT / f"{name}.{extension}")
                                  for name in ("latest-tasks", "bounded-conformers", "evaluation-history")
                                  for extension in ("pdf", "png")]}, indent=2))


if __name__ == "__main__":
    main()
