import json
import tempfile
from pathlib import Path

from scar.sarif_bridge import IkosSarifBridge, Finding


SAMPLE_SARIF = {
    "runs": [{
        "results": [{
            "ruleId": "boa",
            "level": "error",
            "message": {"text": "buffer overflow: accessing index 10 of array of size 8"},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": "src/parser.c"},
                    "region": {"startLine": 42, "startColumn": 5}
                }
            }]
        }]
    }]
}


def test_parse_returns_findings():
    with tempfile.TemporaryDirectory() as tmpdir:
        sarif_path = Path(tmpdir) / "results.sarif"
        sarif_path.write_text(json.dumps(SAMPLE_SARIF))

        bridge = IkosSarifBridge(sarif_path, "/repo")
        findings = bridge.parse()

    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == "boa"
    assert f.line == 42
    assert "buffer overflow" in f.message


def test_parse_missing_file_raises():
    bridge = IkosSarifBridge("/nonexistent/results.sarif", "/repo")
    try:
        bridge.parse()
        assert False, "Expected FileNotFoundError"
    except FileNotFoundError:
        pass
