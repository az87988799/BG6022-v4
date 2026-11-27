"""Focused input-contract checks. Run with Python's standard unittest runner."""

import importlib
import io
import json
import sys
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "auto-orca"
with patch.object(sys, "path", [str(SKILL_DIR / "scripts"), *sys.path]):
    orca_job = importlib.import_module("orca_job")

XYZ = b"2\nhydrogen; units=angstrom\nH 0 0 0\nH 0 0 0.74\n"
NATIVE = (
    "! CAM-B3LYP def2-SVPD SP TightSCF\n"
    "%maxcore 256\n"
    "%tddft\n  nroots 3\n  tda true\n"
    "  SiNgLeTs TRUE # legacy option for restricted singlets\n"
    "  Triplets FALSE\nend\n"
    "* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
)


def xyz_args(task):
    # Also exercise old library callers whose Namespace predates --tightscf.
    return Namespace(task=task, method="PBE0", basis="def2-SVP", charge=0, mult=1,
                     cores=1, maxcore_mb=256, opt_maxiter=None, scf_maxiter=60,
                     temperature=None)


def invoke(argv):
    stdout = io.StringIO()
    with io.TextIOWrapper(io.BytesIO(), encoding="utf-8") as stderr:
        with patch.object(sys, "argv", ["orca_job.py", *argv]), redirect_stdout(stdout), redirect_stderr(stderr):
            return orca_job.main()


class InputContracts(unittest.TestCase):
    def test_all_xyz_tasks_keep_existing_default_and_support_explicit_tightscf(self):
        for task, keywords in (("sp", "SP"), ("opt", "Opt"), ("freq", "Freq"), ("optfreq", "Opt Freq")):
            with self.subTest(task=task):
                args = xyz_args(task)
                original, original_conditions = orca_job.template_input(args, XYZ)
                self.assertEqual(original[0], f"! PBE0 def2-SVP {keywords}")
                self.assertIn("%scf MaxIter 60 end", original)
                self.assertNotIn("scf_convergence_keyword", original_conditions)
                args.tightscf = False
                self.assertEqual((original, original_conditions), orca_job.template_input(args, XYZ))
                args.tightscf = True
                tight, tight_conditions = orca_job.template_input(args, XYZ)
                self.assertEqual(tight, [original[0] + " TightSCF", *original[1:]])
                self.assertEqual(tight_conditions, {**original_conditions, "scf_convergence_keyword": "TightSCF"})

    def test_native_legacy_keyword_and_canonical_input_submit_identical_options(self):
        rendered, conditions = orca_job.native_input(NATIVE.encode())
        canonical = NATIVE.replace("  SiNgLeTs TRUE # legacy option for restricted singlets\n", "")
        self.assertEqual((rendered, conditions), orca_job.native_input(canonical.encode()))
        self.assertEqual(conditions["tddft"], {"nroots": 3, "tda": "true", "triplets": "false"})
        self.assertIn("  triplets false", rendered)
        self.assertFalse(any("singlets" in line.lower() for line in rendered))
        implicit = canonical.replace("  Triplets FALSE\n", "").replace("tda true", "tda false")
        implicit_lines, implicit_conditions = orca_job.native_input(implicit.encode())
        self.assertEqual(implicit_conditions["tddft"], {"nroots": 3, "tda": "false"})
        self.assertIn("  tda false", implicit_lines)

    def test_native_unsupported_states_and_duplicates_rejected(self):
        for needle, substitute in [
            ("SiNgLeTs TRUE", "singlets false"), ("SiNgLeTs TRUE", "singlets 1"),
            ("Triplets FALSE", "triplets true"), ("* xyz 0 1", "* xyz 0 3"),
            ("SiNgLeTs TRUE", "singlets true singlets true"),
        ]:
            with self.subTest(substitute=substitute), self.assertRaises(ValueError):
                orca_job.native_input(NATIVE.replace(needle, substitute).encode())

    def test_native_receipt_and_submitted_input_preserve_source_and_default_state(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "legacy.inp"
            raw = NATIVE.replace("\n", "\r\n").encode("utf-16")
            source.write_bytes(raw)
            directory = root / "native-job"
            with patch.object(orca_job.subprocess, "run", side_effect=OSError("test intercept")) as launch:
                code = invoke(["run", "--input", str(source), "--out-dir", str(directory), "--orca", sys.executable])
            self.assertEqual(code, 1)
            self.assertEqual(launch.call_args.args[0][1], "job.inp")
            self.assertTrue(launch.call_args.kwargs["cwd"].samefile(directory))
            self.assertEqual((directory / "source.inp").read_bytes(), raw)
            self.assertEqual(source.read_bytes(), raw)
            actual_lines = (directory / "job.inp").read_text(encoding="ascii").splitlines()
            expected_lines, expected_conditions = orca_job.native_input(raw)
            self.assertEqual(actual_lines, expected_lines)
            receipt = json.loads((directory / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["submitted_conditions"], expected_conditions)
            self.assertIn("test intercept", receipt["launch_error"])
            self.assertFalse(receipt["checks"]["task_verified"])

    def test_cli_xyz_flag_and_native_route_conflict(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            xyz = root / "input.xyz"
            xyz.write_bytes(XYZ)
            with patch.object(orca_job.subprocess, "run", side_effect=OSError("test intercept")) as launch:
                for task in ("sp", "optfreq"):
                    for tight in (False, True):
                        with self.subTest(task=task, tight=tight):
                            directory = root / f"{task}-{tight}"
                            argv = ["run", "--xyz", str(xyz), "--task", task, "--method", "PBE0",
                                    "--basis", "def2-SVP", "--charge", "0", "--mult", "1",
                                    "--out-dir", str(directory), "--orca", sys.executable]
                            if tight:
                                argv.append("--tightscf")
                            self.assertEqual(invoke(argv), 1)
                            first_line = (directory / "job.inp").read_text(encoding="ascii").splitlines()[0]
                            self.assertEqual("TightSCF" in first_line.split(), tight)
                            rec = json.loads((directory / "result.json").read_text(encoding="utf-8"))
                            self.assertEqual(rec["submitted_conditions"].get("scf_convergence_keyword"),
                                             "TightSCF" if tight else None)
                self.assertEqual(launch.call_count, 4)
                source = root / "native.inp"
                source.write_text(NATIVE, encoding="ascii")
                with self.assertRaises(SystemExit) as error:
                    invoke(["run", "--input", str(source), "--tightscf", "--out-dir", str(root/"conflict")])
                self.assertEqual(error.exception.code, 2)
                self.assertFalse((root / "conflict").exists())
                self.assertEqual(launch.call_count, 4)


if __name__ == "__main__":
    unittest.main()
