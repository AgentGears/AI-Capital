from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.errors import InvalidRequest
from ai_capital.kernel.evidence_store import EvidenceRepository


class K8ReviewRound2Tests(unittest.TestCase):
    def test_compiler_rejects_evidence_not_installed_on_context_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            host = ProgramRepository(Path(directory) / "host.db")
            try:
                evidence = EvidenceRepository(host)
                contexts = ContextRepository(host)
                with self.assertRaises(InvalidRequest):
                    ContextCompiler(contexts, evidence=evidence)
            finally:
                host.close()

    def test_compiler_rejects_different_evidence_repository_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            host = ProgramRepository(Path(directory) / "host.db")
            try:
                installed = EvidenceRepository(host)
                supplied = EvidenceRepository(host)
                contexts = ContextRepository(host, installed)
                with self.assertRaises(InvalidRequest):
                    ContextCompiler(contexts, evidence=supplied)
            finally:
                host.close()

    def test_compiler_accepts_context_installed_evidence_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            host = ProgramRepository(Path(directory) / "host.db")
            try:
                evidence = EvidenceRepository(host)
                contexts = ContextRepository(host, evidence)
                compiler = ContextCompiler(contexts, evidence=evidence)
                self.assertIs(compiler._evidence, evidence)
            finally:
                host.close()


if __name__ == "__main__":
    unittest.main()
