"""Registered single-point entry; the common execution chain owns lifecycle."""

from orca_agent.tools.calculation import execute_calculation


def execute(store, run, step, attempt, config, fault=None):
    return execute_calculation(store, run, step, attempt, config, fault=fault)
