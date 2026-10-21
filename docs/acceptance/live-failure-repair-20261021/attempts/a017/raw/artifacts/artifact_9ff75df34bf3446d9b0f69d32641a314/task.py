from orca_task_sdk import run_orca

receipt = run_orca(
    'hcn_nmr.inp',
    dependencies=['hcn.xyz'],
    request_key='hcn_giao_nmr',
    purpose='new_candidate',
)
print(receipt)
