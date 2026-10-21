"""Draw the supplied candidate energies and discrete excitation results.

This script only visualizes the stated data; it does not run quantum-chemistry
calculations or infer optimized geometries, a broadened spectrum, or measured
absorption. Run with Python and Matplotlib from any working directory.
"""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "figures"
NAVY = "#19374B"
TEAL = "#237D83"
INK = "#283540"
GRID = "#E3E8EB"

# Relative single-point electronic energies, in kJ mol^-1, referenced to
# cand_05. These are saved candidate geometries, not DFT-optimized conformers.
CANDIDATES = (
    ("cand_05", 0.000000),
    ("cand_04", 1.629506),
    ("cand_01", 1.633356),
    ("cand_00", 1.692802),
    ("cand_06", 2.153448),
    ("cand_03", 12.608656),
)

# Full LR-TDDFT (TDA=false), five singlet excitations of acetone at a fixed
# prepared geometry; excitation energy in eV and length-gauge oscillator
# strength (dimensionless). All supplied precision is retained here.
EXCITATIONS = (
    ("S1", 4.197061, 0.000007493),
    ("S2", 7.464499, 0.023781213),
    ("S3", 8.406988, 0.001899769),
    ("S4", 8.572306, 0.060276158),
    ("S5", 8.640904, 0.004121254),
)


def draw() -> None:
    plt.rcParams.update(
        {
            "font.family": "serif",
            "font.serif": ["DejaVu Serif"],
            "mathtext.fontset": "dejavuserif",
            "font.size": 8,
            "axes.labelsize": 8,
            "axes.labelcolor": INK,
            "axes.edgecolor": INK,
            "axes.linewidth": 0.6,
            "xtick.labelsize": 7.4,
            "ytick.labelsize": 7.4,
            "xtick.color": INK,
            "ytick.color": INK,
            "xtick.major.width": 0.5,
            "ytick.major.width": 0.5,
            "text.color": INK,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "savefig.facecolor": "white",
        }
    )
    # Fixed physical canvas: no tight bounding-box resizing at export.
    fig = plt.figure(figsize=(16.3 / 2.54, 7.5 / 2.54), facecolor="white")
    energy_ax = fig.add_axes((0.117, 0.215, 0.350, 0.585))
    excitation_ax = fig.add_axes((0.615, 0.215, 0.358, 0.585))

    for ax in (energy_ax, excitation_ax):
        ax.spines[["top", "right"]].set_visible(False)
        ax.tick_params(length=3, pad=3)
        ax.set_axisbelow(True)

    names, relative = zip(*CANDIDATES)
    energy_ax.barh(range(len(names)), relative, height=0.51, color=TEAL, zorder=3)
    energy_ax.plot(0, 0, "|", color=NAVY, markersize=7, markeredgewidth=1.5, clip_on=False)
    energy_ax.set_yticks(range(len(names)), names)
    energy_ax.set_ylim(5.65, -0.65)
    energy_ax.set_xlim(0, 17.1)
    energy_ax.set_xticks([0, 5, 10, 15])
    energy_ax.xaxis.grid(True, color=GRID, linewidth=0.6)
    energy_ax.tick_params(axis="y", length=0)
    energy_ax.set_xlabel(r"Relative electronic energy / kJ mol$^{-1}$", labelpad=5)
    for i, value in enumerate(relative):
        energy_ax.text(value + 0.25, i, f"{value:.6f}", va="center", fontsize=7.3, color=NAVY)

    energies = [entry[1] for entry in EXCITATIONS]
    strengths = [entry[2] for entry in EXCITATIONS]
    excitation_ax.vlines(energies, 0, strengths, color=NAVY, linewidth=1.15, zorder=3)
    excitation_ax.scatter(
        energies, strengths, s=15, facecolor="white", edgecolor=TEAL, linewidth=0.9, zorder=4
    )
    excitation_ax.axhline(0, color=INK, linewidth=0.5, zorder=2)
    excitation_ax.set_xlim(3.8, 9.15)
    excitation_ax.set_ylim(-0.002, 0.069)
    excitation_ax.set_xticks([4, 5, 6, 7, 8, 9])
    excitation_ax.set_yticks([0, 0.02, 0.04, 0.06], ["0.00", "0.02", "0.04", "0.06"])
    excitation_ax.yaxis.grid(True, color=GRID, linewidth=0.6)
    excitation_ax.set_xlabel("Excitation energy / eV", labelpad=5)
    excitation_ax.set_ylabel(r"Oscillator strength $f$", labelpad=6)

    # Individually placed state labels keep the three nearby high-energy
    # transitions distinct; straight leader lines preserve the exact positions.
    label_positions = {
        "S1": (4.75, 0.0090),
        "S2": (7.30, 0.0290),
        "S3": (7.85, 0.0110),
        "S4": (8.57, 0.0650),
        "S5": (8.93, 0.0185),
    }
    for state, energy, strength in EXCITATIONS:
        excitation_ax.annotate(
            rf"$S_{{{state[1]}}}$",
            xy=(energy, strength),
            xytext=label_positions[state],
            fontsize=8,
            ha="center",
            va="center",
            color=NAVY,
            arrowprops={"arrowstyle": "-", "color": TEAL, "lw": 0.55, "shrinkA": 2, "shrinkB": 3},
        )
    excitation_ax.text(
        4.02, 0.0575, "Discrete singlet transitions\nLength gauge; TDA = false", fontsize=7.3,
        ha="left", va="top", linespacing=1.5, color=INK,
    )

    fig.text(0.117, 0.932, "(a) n-Hexane candidate energies", fontsize=8.8, weight="bold", color=NAVY)
    fig.text(0.117, 0.866, "r2SCAN-3c · gas phase", fontsize=7.7)
    fig.text(0.615, 0.932, "(b) Acetone vertical excitations", fontsize=8.8, weight="bold", color=NAVY)
    fig.text(0.615, 0.866, "LR-TDDFT · PBE0/def2-SVP · gas phase", fontsize=7.7)
    fig.text(0.117, 0.047, "Single points on saved candidate geometries", fontsize=7.3, color=INK)
    fig.text(0.615, 0.047, "Fixed prepared geometry", fontsize=7.3, color=INK)

    OUT.mkdir(exist_ok=True)
    fig.savefig(OUT / "current-results.pdf", metadata={"Title": "Candidate energies and vertical excitations", "Creator": "Matplotlib"})
    fig.savefig(OUT / "current-results.png", dpi=300)
    plt.close(fig)
    print("Wrote figures/current-results.pdf and figures/current-results.png (16.3 × 7.5 cm)")


if __name__ == "__main__":
    draw()
