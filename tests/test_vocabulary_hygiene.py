from pathlib import Path
import re
import unittest


CANONICAL_DOCUMENTS = (
    "ARCHITECTURE_CONSTITUTION.md",
    "AI_CAPITAL_AUTONOMOUS_WORK_KERNEL_ROADMAP.md",
    "AI_CAPITAL_MASTER_ROADMAP.md",
    "AI_CAPITAL_ARCHITECTURE_PATTERN_REGISTER.md",
)

# This is a structural guard, not a substitute for architecture review. It avoids
# embedding external product/project names in the repository while still catching
# common external-identity leakage forms.
FORBIDDEN_PATTERNS = (
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"\bexternal/source system\b", re.IGNORECASE),
    re.compile(r"\bcommit\s+[0-9a-f]{7,40}\b", re.IGNORECASE),
    re.compile(r"\b[0-9a-f]{40}\b", re.IGNORECASE),
)


class VocabularyHygieneTests(unittest.TestCase):
    def test_canonical_documents_avoid_external_identity_markers(self):
        root = Path(__file__).resolve().parents[1]
        for relative_path in CANONICAL_DOCUMENTS:
            path = root / relative_path
            self.assertTrue(path.exists(), relative_path)
            text = path.read_text(encoding="utf-8")
            for pattern in FORBIDDEN_PATTERNS:
                self.assertIsNone(
                    pattern.search(text),
                    f"{relative_path} contains forbidden external-identity marker",
                )


if __name__ == "__main__":
    unittest.main()
