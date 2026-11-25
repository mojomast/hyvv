"""RED for B1: the /v1 OpenAPI document must parse with a YAML loader."""
import pathlib
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOC = ROOT / "contract" / "v1" / "openapi.yaml"
CMD = [
    "uv", "run", "--with", "pyyaml==6.0.2", "python", "-c",
    "import pathlib,sys,yaml; yaml.safe_load(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))",
    str(DOC),
]

class OpenApiDocumentParses(unittest.TestCase):
    def test_the_document_parses_with_a_yaml_loader(self):
        result = subprocess.run(CMD, cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])

if __name__ == "__main__":
    unittest.main()
