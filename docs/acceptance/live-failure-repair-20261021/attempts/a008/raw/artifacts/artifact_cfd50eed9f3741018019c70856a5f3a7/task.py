import json

observations = {
    "version": 1,
    "observations": [
        {
            "name": "random_seed",
            "value": 1701,
            "unit": None,
            "sources": [],
            "formula": None,
            "artifacts": ["observations.json"],
            "limitations": [
                "No stochastic structure-generation or search algorithm was used in this task; the requested seed 1701 is recorded as the task-level reproducibility seed, but it does not parameterize a random-number generator here."
            ]
        }
    ]
}
with open("observations.json", "w") as f:
    json.dump(observations, f, indent=2)
print(json.dumps(observations))
