"""RED for B1: the /v1 OpenAPI document must parse with a YAML loader.

The sandbox and CI both provide a YAML loader; when the ambient interpreter has
one it is used directly, otherwise the test falls back to a job-local
`uv run --with pyyaml==6.0.2` invocation so the repo keeps no runtime or dev
dependency on PyYAML.
"""
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOC = ROOT / "contract" / "v1" / "openapi.yaml"

def load():
    """Parse the document and return (ok, detail) without importing yaml at module scope."""
    try:
        import yaml  # noqa: PLC0415 - optional ambient loader
    except Exception:
        cmd = [sys.executable, "-m", "uv", "run", "--with", "pyyaml==6.0.2",
               "python", "-c", "import pathlib,sys,yaml; yaml.safe_load(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))", str(DOC)]
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        return r.returncode == 0, r.stderr[-2000:]
    try:
        yaml.safe_load(DOC.read_text(encoding="utf-8"))
        return True, ""
    except Exception as exc:
        return False, str(exc)

class OpenApiDocumentParses(unittest.TestCase):
    def test_the_document_parses_with_a_yaml_loader(self):
        ok, detail = load()
        self.assertTrue(ok, detail)

if __name__ == "__main__":
    unittest.main()
