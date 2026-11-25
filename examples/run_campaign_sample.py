import json
from pathlib import Path
import sys

# Allow running directly from a source checkout without installing the package.
SRC = Path(__file__).resolve().parents[1] / "src"
if SRC.exists() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pipeline_interface import plan_campaign

path = Path(__file__).resolve().parents[1] / "fixtures" / "campaign_input.json"
result = plan_campaign(json.loads(path.read_text()))
print(json.dumps(result, indent=2, ensure_ascii=False))
