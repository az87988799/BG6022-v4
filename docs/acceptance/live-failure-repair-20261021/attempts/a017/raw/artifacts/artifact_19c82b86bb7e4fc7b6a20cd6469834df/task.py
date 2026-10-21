Traceback (most recent call last):
  File "/opt/orca-agent-runtime/python_task.py", line 21, in <module>
    runpy.run_path(sys.argv[0], run_name="__main__")
  File "<frozen runpy>", line 291, in run_path
  File "<frozen runpy>", line 98, in _run_module_code
  File "<frozen runpy>", line 88, in _run_code
  File "/work/sealed/task.py", line 3, in <module>
    receipt = run_orca(
              ^^^^^^^^^
  File "/opt/orca-agent-runtime/venv/lib/python3.11/site-packages/orca_task_sdk.py", line 74, in run_orca
    return _exchange({
           ^^^^^^^^^^^
  File "/opt/orca-agent-runtime/venv/lib/python3.11/site-packages/orca_task_sdk.py", line 55, in _exchange
    raise RuntimeError(response["error"])
RuntimeError: managed_sdk_request_rejected: %eprnmr must follow the xyzfile coordinate declaration when NMR or Nuclei requests nuclear properties; move this block after the coordinates
