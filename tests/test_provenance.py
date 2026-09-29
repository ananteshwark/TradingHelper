import datetime as dt
import json

from igs.provenance import validation_fingerprint
from igs.score.run import load_ic_status


def test_validation_artifacts_require_matching_code_and_past_training(tmp_path):
    path = tmp_path / "ic.json"
    data = {"generated_at": "test", "factors": [{"factor": "roce", "verdict": "DROP"}],
            "trained_through": "2024-12-31"}
    path.write_text(json.dumps(data))
    assert load_ic_status(path) == (set(), None)
    data["validation_fingerprint"] = validation_fingerprint()
    path.write_text(json.dumps(data))
    assert load_ic_status(path, dt.datetime(2025, 1, 1, tzinfo=dt.UTC)) == ({"roce"}, "test")
    assert load_ic_status(path, dt.datetime(2024, 1, 1, tzinfo=dt.UTC)) == (set(), None)
