from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.authority import PolicySnapshot, effect_allowed_by_ceiling
from ai_capital.kernel.authority_store import AuthorityRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import EffectClass
from ai_capital.kernel.errors import InvalidRequest
from ai_capital.kernel.models import Grant


class K4AuthorityValidationTests(unittest.TestCase):
    def test_effect_ceiling_is_monotonic(self):
        self.assertTrue(
            effect_allowed_by_ceiling(EffectClass.DELETE, EffectClass.OBSERVE)
        )
        self.assertTrue(
            effect_allowed_by_ceiling(EffectClass.DELETE, EffectClass.CREATE)
        )
        self.assertTrue(
            effect_allowed_by_ceiling(EffectClass.DELETE, EffectClass.MODIFY)
        )
        self.assertTrue(
            effect_allowed_by_ceiling(EffectClass.DELETE, EffectClass.DELETE)
        )
        self.assertFalse(
            effect_allowed_by_ceiling(
                EffectClass.DELETE, EffectClass.EXTERNAL_SIDE_EFFECT
            )
        )
        self.assertFalse(
            effect_allowed_by_ceiling(EffectClass.MODIFY, EffectClass.DELETE)
        )

    def test_grant_rejects_invalid_or_non_increasing_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProgramRepository(Path(directory) / "kernel.db") as programs:
                authority = AuthorityRepository(programs)
                with self.assertRaises(InvalidRequest):
                    authority.issue_grant(
                        Grant(
                            "g-invalid",
                            "actor:a-1",
                            ("workspace.read",),
                            ("*",),
                            EffectClass.OBSERVE,
                            (),
                            "not-a-time",
                            None,
                            0,
                        )
                    )
                with self.assertRaises(InvalidRequest):
                    authority.issue_grant(
                        Grant(
                            "g-naive",
                            "actor:a-1",
                            ("workspace.read",),
                            ("*",),
                            EffectClass.OBSERVE,
                            (),
                            "2026-08-30T00:00:00",
                            None,
                            0,
                        )
                    )
                with self.assertRaises(InvalidRequest):
                    authority.issue_grant(
                        Grant(
                            "g-expiry",
                            "actor:a-1",
                            ("workspace.read",),
                            ("*",),
                            EffectClass.OBSERVE,
                            (),
                            "2026-08-30T00:00:00Z",
                            "2026-08-30T00:00:00Z",
                            0,
                        )
                    )

    def test_policy_rejects_naive_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProgramRepository(Path(directory) / "kernel.db") as programs:
                authority = AuthorityRepository(programs)
                with self.assertRaises(InvalidRequest):
                    authority.install_policy(
                        PolicySnapshot(0, (), (), "2026-08-30T00:00:00")
                    )


if __name__ == "__main__":
    unittest.main()
