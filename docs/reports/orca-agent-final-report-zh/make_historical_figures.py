"""Reproduce historical scientific figures from audited frozen evidence.

Requires Matplotlib. Run with a Python environment that already provides it;
this script neither installs dependencies nor launches scientific calculations.
The V2 graph contains numbered optimization cycles only. The separate final
evaluation supplies the reference energy and is never drawn as an extra cycle.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "figures"
NAVY, TEAL, AMBER, INK = "#17324D", "#148C8C", "#D99B2B", "#26384A"


def validate_v2(data: dict) -> list[dict]:
    """Check frozen audit facts and recompute every plotted value with Decimal.

    The archive hash checks were performed during the read-only audit. Here we
    validate their persisted outcomes, without relying on the original machine
    or claiming a new verification of archive bytes.
    """
    records = data["priority_results"]
    assert [row["label"] for row in records] == ["water", "ethanol"]
    verified = []
    for record, expected_cycles, expected_modes in zip(records, [4, 11], [3, 21]):
        assert record["packet_manifest_file_entries_checked"] == 33
        assert record["packet_files_all_hashes_and_sizes_match"] is True
        assert record["optimization_cycles"] == expected_cycles
        cycle_rows = record["optimization_cycle_series"]
        assert len(cycle_rows) == expected_cycles
        assert [row["cycle"] for row in cycle_rows] == list(range(1, expected_cycles + 1))
        assert all(row["stage"] == "optimization_cycle" for row in cycle_rows)
        reference = record["post_convergence_final_evaluation"]
        assert reference["cycle"] is None
        assert reference["stage"] == "final_evaluation"
        final_energy = Decimal(reference["energy_token"])
        relative = [
            (Decimal(row["energy_token"]) - final_energy) * Decimal("1000")
            for row in cycle_rows
        ]
        for row, value in zip(cycle_rows, relative):
            assert value == Decimal(str(row["relative_to_opt_final_mEh"]))
        hessian = record["hessian"]
        assert hessian["mode_count"] == expected_modes + 6
        assert hessian["vibrational_count"] == expected_modes
        assert hessian["negative_vibrational_count"] == 0
        modes = hessian["modes"]
        assert [row["index"] for row in modes] == list(range(expected_modes + 6))
        assert all(Decimal(row["raw_token"]) == 0 for row in modes[:6])
        assert all(Decimal(row["raw_token"]) > 0 for row in modes[6:])
        assert len(record["positive_vibrational_frequencies_cm1"]) == expected_modes
        assert len(record["nodes"]) == record["launches"] == 3
        assert record["approval_grant_records"] == 3
        assert all(row["raw_energy_matches_snapshot"] for row in record["nodes"])
        verified.append({"record": record, "cycles": cycle_rows,
                         "relative_mEh": relative, "final_energy_Eh": final_energy})
    return verified


def style() -> None:
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 10,
        "axes.labelsize": 10, "axes.titlesize": 11,
        "xtick.labelsize": 9, "ytick.labelsize": 9,
        "legend.fontsize": 9, "text.color": INK,
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
    OUT.mkdir(exist_ok=True)
    fig.savefig(OUT / f"{name}.pdf", metadata={
        "Creator": "make_historical_figures.py / Matplotlib", "Title": name})
    fig.savefig(OUT / f"{name}.png", dpi=220)
    plt.close(fig)


def v2_optimization(verified: list[dict]) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.7))
    fig.subplots_adjust(left=0.09, right=0.97, top=0.74,
                        bottom=0.35, wspace=0.30)
    for index, (ax, row, color) in enumerate(zip(axes, verified, [TEAL, NAVY])):
        record = row["record"]
        cycles = [point["cycle"] for point in row["cycles"]]
        energy = [float(value) for value in row["relative_mEh"]]
        ax.plot(cycles, energy, color=color, linewidth=1.8,
                marker="o", markersize=4.2, zorder=3)
        ax.axhline(0, color=AMBER, linewidth=1, linestyle=(0, (4, 3)))
        ax.set_xlabel("Optimization cycle")
        ax.text(0, 1.30,
                f"({chr(97 + index)}) {record['label'].capitalize()}"
                f" | {len(cycles)} cycles",
                transform=ax.transAxes, fontsize=11, va="bottom")
        ax.text(0, 1.12,
                f"r\N{SUPERSCRIPT TWO}SCAN-3c | ORCA 6.1.1 | {record['cores']} core"
                f"{'s' if record['cores'] != 1 else ''}",
                transform=ax.transAxes, fontsize=9, va="bottom")
        ax.set_xlim(0.7, cycles[-1] + 0.3)
        ax.set_ylim(-max(energy) * 0.04, max(energy) * 1.16)
        ax.set_xticks(cycles if len(cycles) <= 5 else cycles[::2])
        ax.yaxis.set_major_locator(MaxNLocator(nbins=4))
        ax.grid(axis="y")
        ax.text(cycles[0] + 0.12, energy[0] + max(energy) * 0.045,
                f"{energy[0]:.3f} mEh", fontsize=9, color=color)
        ax.text(0.98, 0.91,
                f"Final evaluation\n{row['final_energy_Eh']:.9f} Eh",
                transform=ax.transAxes, fontsize=9, ha="right", va="top")
    axes[0].set_ylabel(r"$E_{\mathrm{cycle}}-E_{\mathrm{final}}$ (mEh)")
    fig.text(0.09, 0.965, "V2 historical ORCA optimization trajectories",
             fontsize=11, weight="bold", va="top")
    fig.text(0.09, 0.17,
             "Human-approved chains; water collection required a developer repair.",
             fontsize=9)
    fig.text(0.09, 0.105,
             "Different molecules and core counts; no speedup comparison. Both: 2048 MB total.",
             fontsize=9)
    fig.text(0.09, 0.04,
             "Final evaluation sets the zero reference and is not an additional optimization cycle.",
             fontsize=9)
    export(fig, "v2-optimization")


def main() -> None:
    data = json.loads((ROOT / "historical-v2-evidence.json").read_text(encoding="utf-8"))
    verified = validate_v2(data)
    style()
    v2_optimization(verified)
    print(json.dumps({
        "v2_cycles": [len(row["cycles"]) for row in verified],
        "v2_vibrational_modes": [row["record"]["hessian"]["vibrational_count"]
                                 for row in verified],
        "archive_entries_per_packet": 33,
        "decimal_values_recomputed": True,
        "figures": ["figures/v2-optimization.pdf", "figures/v2-optimization.png"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
