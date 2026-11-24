import json
from pathlib import Path
from pipeline_interface import process

path=Path(__file__).parents[1]/"fixtures"/"input.json"
print(json.dumps(process(json.loads(path.read_text())), indent=2))

