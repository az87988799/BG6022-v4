import json

with open('gas_energy.json', 'r') as f:
    gas = json.load(f)['energy']
with open('water_energy.json', 'r') as f:
    water = json.load(f)['energy']

kj_per_eh = 2625.499638
delta_kj = (water - gas) * kj_per_eh

obs = {
    "version": 1,
    "observations": [
        {
            "name": "energy_difference",
            "value": delta_kj,
            "unit": "kJ/mol",
            "sources": [
                {"filename": "gas_energy.json", "location": "energy field"},
                {"filename": "water_energy.json", "location": "energy field"}
            ],
            "formula": "E(CPCM water) - E(gas)",
            "artifacts": [],
            "limitations": ["Specified model electronic energy difference; not solvation free energy; no thermal or standard-state corrections."]
        }
    ]
}
with open('observations.json', 'w') as f:
    json.dump(obs, f)
