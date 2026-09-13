from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ai_capital.kernel.errors import InvalidRequest
from ai_capital.product.git_repository_guard import validate_git_repository


class H2GitObservationGuardTests(unittest.TestCase):
    @staticmethod
    def _repository(root: Path, config: str) -> Path:
        repository = root / "repo"
        git_dir = repository / ".git"
        git_dir.mkdir(parents=True)
        (git_dir / "config").write_text(config)
        return repository

    def test_standard_local_config_is_admitted(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = self._repository(
                Path(directory),
                "[core]\n"
                "\trepositoryformatversion = 0\n"
                "\tfilemode = true\n"
                "\tbare = false\n"
                "\tlogallrefupdates = true\n",
            )
            validate_git_repository(repository)

    def test_filter_sections_are_rejected_in_current_and_legacy_forms(self):
        configs = (
            "[filter \"driver\"]\n\trequired = true\n",
            "[filter.driver]\n\trequired = true\n",
        )
        for index, config in enumerate(configs):
            with self.subTest(index=index):
                with tempfile.TemporaryDirectory() as directory:
                    repository = self._repository(Path(directory), config)
                    with self.assertRaises(InvalidRequest):
                        validate_git_repository(repository)


if __name__ == "__main__":
    unittest.main()
