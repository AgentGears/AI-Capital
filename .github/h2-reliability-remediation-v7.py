from pathlib import Path


def once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


path = Path("src/ai_capital/kernel/authority_store.py")
text = path.read_text()
marker = "    def consume_execution_authority(self, receipt_id: str) -> None:\n"
method = '''    def unconsumed_execution_authority_for_decision(
        self,
        decision_id: str,
    ) -> ExecutionAuthorityReceipt | None:
        if type(decision_id) is not str or not decision_id.strip():
            raise InvalidRequest("decision_id must be non-empty")
        rows = self._host_store._db.execute(
            """
            SELECT receipt_id, single_use_identity, receipt_json, receipt_digest, consumed_at
            FROM execution_authority_receipts ORDER BY receipt_id
            """
        ).fetchall()
        match: ExecutionAuthorityReceipt | None = None
        match_consumed = False
        found = 0
        for row in rows:
            try:
                receipt = record_from_json(ExecutionAuthorityReceipt, row["receipt_json"])
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation("execution authority cannot be decoded") from exc
            if not isinstance(receipt, ExecutionAuthorityReceipt):
                raise IntegrityViolation("decoded execution authority has wrong type")
            if receipt.receipt_id != row["receipt_id"]:
                raise IntegrityViolation("execution authority row identity mismatch")
            if receipt.single_use_identity != row["single_use_identity"]:
                raise IntegrityViolation("execution authority single-use identity mismatch")
            if canonical_digest(receipt) != row["receipt_digest"]:
                raise IntegrityViolation("execution authority digest mismatch")
            context = self.get_decision(receipt.decision_id)
            self._validate_receipt_context_binding(receipt, context)
            if receipt.decision_id != decision_id:
                continue
            found += 1
            if found > 1:
                raise IntegrityViolation(
                    "AuthorityDecision maps to multiple execution-authority receipts"
                )
            match = receipt
            match_consumed = row["consumed_at"] is not None
        if match is None or match_consumed:
            return None
        return match

'''
text = once(text, marker, method + marker, "authority lookup helper")
path.write_text(text)

path = Path("src/ai_capital/product/capability_operator.py")
text = path.read_text()
needle = '''        if operation is not None:
            if context is None:
                raise IntegrityViolation("Operation request lacks AuthorityDecision context")
            result = self._result_for_operation(
                operation,
                to_canonical_data(context.decision),
            )
            return self._requests.complete(request_id, result).result or result

        if context is not None:
'''
replacement = '''        if operation is not None:
            if context is None:
                raise IntegrityViolation("Operation request lacks AuthorityDecision context")
            result = self._result_for_operation(
                operation,
                to_canonical_data(context.decision),
            )
            return self._requests.complete(request_id, result).result or result

        if context is not None:
            issued_authority = self._authority_store.unconsumed_execution_authority_for_decision(
                context.decision.decision_id
            )
            if issued_authority is not None:
                self._require_program_ready(context.program_id)
                return self._execute(
                    program_id=context.program_id,
                    resolution=context.resolution,
                    authority_receipt_id=issued_authority.receipt_id,
                    decision=to_canonical_data(context.decision),
                )

        if context is not None:
'''
text = once(text, needle, replacement, "recover issued authority")
path.write_text(text)

path = Path("tests/test_h2_reliability_review.py")
text = path.read_text()
marker = "    def test_approved_request_recovers_running_operation_after_restart(self):\n"
test = '''    def test_approved_request_recovers_issued_authority_before_operation_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            arguments = {"path": "approved-issued.txt", "content": "approved once\\n"}
            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="workspace.write",
                    resource_scope=("approved-issued.txt",),
                    approval_required=True,
                )
                pending = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="workspace.write",
                    arguments=arguments,
                    request_id="req-approved-issued-restart",
                )
            decision_id = pending["decision"]["decision_id"]
            with LocalProgramOperator.open(database) as programs:
                approved = programs.approve(decision_id)
                approval_id = approved["approval"]["receipt"]["approval_id"]

            with self._open(database, workspace, artifacts) as operator:
                issued = operator._authority.issue_execution_authority(
                    decision_id=decision_id,
                    approval_id=approval_id,
                )
                issued_id = issued.receipt_id
                self.assertEqual(
                    int(
                        operator._programs._db.execute(
                            "SELECT COUNT(*) FROM operation_projections"
                        ).fetchone()[0]
                    ),
                    0,
                )

            with self._open(database, workspace, artifacts) as operator:
                replay = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="workspace.write",
                    arguments=arguments,
                    request_id="req-approved-issued-restart",
                )
                authorities = operator._programs._db.execute(
                    "SELECT receipt_id, consumed_at FROM execution_authority_receipts"
                ).fetchall()
                operation_count = int(
                    operator._programs._db.execute(
                        "SELECT COUNT(*) FROM operation_projections"
                    ).fetchone()[0]
                )
            self.assertEqual(replay["state"], "executed")
            self.assertEqual(len(authorities), 1)
            self.assertEqual(str(authorities[0]["receipt_id"]), issued_id)
            self.assertIsNotNone(authorities[0]["consumed_at"])
            self.assertEqual(operation_count, 1)
            self.assertEqual((workspace / "approved-issued.txt").read_text(), "approved once\\n")

'''
if "test_approved_request_recovers_issued_authority_before_operation_after_restart" not in text:
    text = once(text, marker, test + marker, "review regression insertion")
path.write_text(text)
