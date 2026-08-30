from pathlib import Path
import ast
import sys
import unittest


class DependencyBoundaryTests(unittest.TestCase):
    def test_trusted_kernel_has_no_external_runtime_dependencies(self):
        root = Path(__file__).resolve().parents[1] / "src" / "ai_capital" / "kernel"
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top_level = alias.name.split(".", 1)[0]
                        self.assertIn(
                            top_level,
                            sys.stdlib_module_names,
                            f"{path.name} imports non-kernel dependency {top_level}",
                        )
                elif isinstance(node, ast.ImportFrom):
                    if node.level > 0:
                        continue
                    module = node.module or ""
                    top_level = module.split(".", 1)[0]
                    self.assertIn(
                        top_level,
                        sys.stdlib_module_names | {"ai_capital"},
                        f"{path.name} imports non-kernel dependency {top_level}",
                    )


if __name__ == "__main__":
    unittest.main()
