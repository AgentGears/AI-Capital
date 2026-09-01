from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from .claim_store import ClaimRepository
from .durable_program import ProgramRepository
from .enums import EffectClass, ProgramStatus, VerificationResult
from .errors import (
    EvidenceInvalid,
    EvidenceMissing,
    IntegrityViolation,
    InvalidRequest,
    PersistenceConflict,
    VerificationStale,
)
from .events import event_digest_fields, utc_now, verify_event_digest
from .models import CapabilityResolution, Event, Operation, Program, Verification
from .schema_codec import record_from_json, record_to_json
from .serialization import canonical_digest, canonical_json, to_canonical_data


_COMPONENT = "verification"
_COMPONENT_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class VerificationContract:
    contract_id: str
    program_id: str
    success_criteria: tuple[str, ...]
    required_claim_refs: tuple[str, ...]
    mandatory: bool
    require_effect_certainty: bool
    created_at: str


@dataclass(frozen=True, slots=True)
class VerificationObservation:
    result: VerificationResult
    rationale_code: str


class Verifier(Protocol):
    def verify(
        self,
        contract: VerificationContract,
        program: Program,
        evidence_refs: tuple[str, ...],
    ) -> VerificationObservation: ...


class VerificationRepository:
    """Host-owned Verification contracts, receipts, and currentness checks."""

    def __init__(
        self,
        host_store: ProgramRepository,
        claims: ClaimRepository,
    ):
        if claims._host_store is not host_store:
            raise InvalidRequest("Verification and Claim repositories must share one Host store")
        self._host_store = host_store
        self._claims = claims
        self._migrate()

    def _migrate(self) -> None:
        with self._host_store._transaction():
            self._host_store._db.execute(
                """
                CREATE TABLE IF NOT EXISTS component_schema (
                    component TEXT PRIMARY KEY,
                    version INTEGER NOT NULL
                )
                """
            )
            row = self._host_store._db.execute(
                "SELECT version FROM component_schema WHERE component = ?",
                (_COMPONENT,),
            ).fetchone()
            version = None if row is None else int(row[0])
            if version is not None and version > _COMPONENT_SCHEMA_VERSION:
                raise IntegrityViolation(
                    f"Verification schema version {version} is newer than supported "
                    f"{_COMPONENT_SCHEMA_VERSION}"
                )
            if version not in {None, 1, _COMPONENT_SCHEMA_VERSION}:
                raise IntegrityViolation(f"unsupported Verification schema version {version}")

            self._host_store._db.execute(
                """
                CREATE TABLE IF NOT EXISTS verification_contracts (
                    contract_id TEXT PRIMARY KEY,
                    program_id TEXT NOT NULL,
                    contract_json TEXT NOT NULL,
                    contract_digest TEXT NOT NULL,
                    registered_event_id TEXT NOT NULL UNIQUE
                )
                """
            )
            self._host_store._db.execute(
                """
                CREATE INDEX IF NOT EXISTS verification_contracts_program
                ON verification_contracts(program_id, contract_id)
                """
            )
            self._host_store._db.execute(
                """
                CREATE TABLE IF NOT EXISTS verification_receipts (
                    verification_id TEXT PRIMARY KEY,
                    contract_id TEXT NOT NULL,
                    program_id TEXT NOT NULL,
                    event_sequence INTEGER NOT NULL UNIQUE,
                    event_id TEXT NOT NULL UNIQUE,
                    verification_json TEXT NOT NULL,
                    verification_digest TEXT NOT NULL,
                    rationale_code TEXT NOT NULL,
                    FOREIGN KEY(contract_id) REFERENCES verification_contracts(contract_id)
                )
                """
            )
            self._host_store._db.execute(
                """
                CREATE INDEX IF NOT EXISTS verification_receipts_contract_sequence
                ON verification_receipts(contract_id, event_sequence)
                """
            )
            self._host_store._db.execute(
                """
                CREATE INDEX IF NOT EXISTS verification_receipts_program_sequence
                ON verification_receipts(program_id, event_sequence)
                """
            )
            self._host_store._db.execute(
                """
                CREATE TABLE IF NOT EXISTS verification_contract_event_index (
                    sequence INTEGER PRIMARY KEY,
                    program_id TEXT NOT NULL,
                    contract_id TEXT NOT NULL UNIQUE,
                    event_id TEXT NOT NULL UNIQUE,
                    FOREIGN KEY(sequence) REFERENCES events(sequence)
                )
                """
            )
            self._host_store._db.execute(
                """
                CREATE INDEX IF NOT EXISTS verification_contract_event_program
                ON verification_contract_event_index(program_id, sequence)
                """
            )
            self._host_store._db.execute(
                """
                CREATE TABLE IF NOT EXISTS verification_event_index (
                    sequence INTEGER PRIMARY KEY,
                    program_id TEXT NOT NULL,
                    contract_id TEXT NOT NULL,
                    verification_id TEXT NOT NULL UNIQUE,
                    event_id TEXT NOT NULL UNIQUE,
                    FOREIGN KEY(sequence) REFERENCES events(sequence)
                )
                """
            )
            self._host_store._db.execute(
                """
                CREATE INDEX IF NOT EXISTS verification_event_contract_sequence
                ON verification_event_index(contract_id, sequence)
                """
            )
            self._host_store._db.execute(
                """
                CREATE INDEX IF NOT EXISTS verification_event_program_sequence
                ON verification_event_index(program_id, sequence)
                """
            )
            self._host_store._db.execute(
                """
                CREATE INDEX IF NOT EXISTS events_type_sequence
                ON events(event_type, sequence)
                """
            )

            if version == 1:
                self._rebuild_event_indexes()
                self._host_store._db.execute(
                    "UPDATE component_schema SET version = ? WHERE component = ?",
                    (_COMPONENT_SCHEMA_VERSION, _COMPONENT),
                )
                version = _COMPONENT_SCHEMA_VERSION
            elif version is None:
                self._host_store._db.execute(
                    "INSERT INTO component_schema(component, version) VALUES (?, ?)",
                    (_COMPONENT, _COMPONENT_SCHEMA_VERSION),
                )
                version = _COMPONENT_SCHEMA_VERSION

            if version != _COMPONENT_SCHEMA_VERSION:
                raise IntegrityViolation(f"unsupported Verification schema version {version}")
            self._validate_index_alignment()

    def _append_event(
        self,
        event_type: str,
        payload: object,
        *,
        program_id: str,
    ) -> Event:
        sequence = self._host_store._next_sequence()
        event_id = str(uuid4())
        occurred_at = utc_now()
        recorded_at = utc_now()
        canonical_payload = to_canonical_data(payload)
        digest = event_digest_fields(
            event_id=event_id,
            sequence=sequence,
            event_type=event_type,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            payload=canonical_payload,
            actor_id=None,
            program_id=None,
            causation_id=None,
            correlation_id=program_id,
        )
        event = Event(
            event_id=event_id,
            sequence=sequence,
            event_type=event_type,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            payload=canonical_payload,
            digest=digest,
            correlation_id=program_id,
        )
        self._host_store._insert_event(event)
        return event

    def _decode_event_row(self, row: sqlite3.Row) -> Event:
        try:
            event = record_from_json(Event, row["event_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Verification Event cannot be decoded") from exc
        if not isinstance(event, Event):
            raise IntegrityViolation("Verification Event decoded wrong type")
        if (
            event.sequence != int(row["sequence"])
            or event.event_id != row["event_id"]
            or event.program_id != row["program_id"]
            or event.event_type != row["event_type"]
            or event.digest != row["event_digest"]
            or not verify_event_digest(event)
        ):
            raise IntegrityViolation("Verification Event integrity mismatch")
        return event

    def _event(self, event_id: str) -> Event:
        row = self._host_store._db.execute(
            """
            SELECT sequence, event_id, program_id, event_type, event_json, event_digest
            FROM events WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()
        if row is None:
            raise IntegrityViolation("Verification Event is missing")
        return self._decode_event_row(row)

    def _rebuild_event_indexes(self) -> None:
        self._host_store._db.execute("DELETE FROM verification_contract_event_index")
        self._host_store._db.execute("DELETE FROM verification_event_index")
        rows = self._host_store._db.execute(
            """
            SELECT sequence, event_id, program_id, event_type, event_json, event_digest
            FROM events
            WHERE event_type IN ('verification.contract_registered', 'verification.recorded')
            ORDER BY sequence
            """
        ).fetchall()
        for row in rows:
            event = self._decode_event_row(row)
            if event.event_type == "verification.contract_registered":
                try:
                    contract = record_from_json(
                        VerificationContract,
                        canonical_json(event.payload["contract"]),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise IntegrityViolation("Verification contract Event is malformed") from exc
                if not isinstance(contract, VerificationContract):
                    raise IntegrityViolation("Verification contract Event decoded wrong type")
                if event.correlation_id != contract.program_id:
                    raise IntegrityViolation("Verification contract Event Program mismatch")
                self._host_store._db.execute(
                    """
                    INSERT INTO verification_contract_event_index(
                        sequence, program_id, contract_id, event_id
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (event.sequence, contract.program_id, contract.contract_id, event.event_id),
                )
                continue

            try:
                verification = record_from_json(
                    Verification,
                    canonical_json(event.payload["verification"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise IntegrityViolation("Verification recorded Event is malformed") from exc
            if not isinstance(verification, Verification):
                raise IntegrityViolation("Verification recorded Event decoded wrong type")
            program_id = verification.subject_ref.removeprefix("program:")
            if not program_id or event.correlation_id != program_id:
                raise IntegrityViolation("Verification recorded Event Program mismatch")
            self._host_store._db.execute(
                """
                INSERT INTO verification_event_index(
                    sequence, program_id, contract_id, verification_id, event_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.sequence,
                    program_id,
                    verification.contract_ref,
                    verification.verification_id,
                    event.event_id,
                ),
            )
        self._validate_index_alignment()

    def _semantic_event_sets(
        self,
    ) -> tuple[
        set[tuple[str, str, str]],
        set[tuple[str, str, str, str]],
    ]:
        contract_events: set[tuple[str, str, str]] = set()
        verification_events: set[tuple[str, str, str, str]] = set()
        rows = self._host_store._db.execute(
            """
            SELECT sequence, event_id, program_id, event_type, event_json, event_digest
            FROM events
            WHERE event_type IN ('verification.contract_registered', 'verification.recorded')
            ORDER BY sequence
            """
        ).fetchall()
        for row in rows:
            event = self._decode_event_row(row)
            if event.event_type == "verification.contract_registered":
                try:
                    contract = record_from_json(
                        VerificationContract,
                        canonical_json(event.payload["contract"]),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise IntegrityViolation("Verification contract Event is malformed") from exc
                if not isinstance(contract, VerificationContract):
                    raise IntegrityViolation("Verification contract Event decoded wrong type")
                if event.correlation_id != contract.program_id:
                    raise IntegrityViolation("Verification contract Event Program mismatch")
                binding = (contract.contract_id, contract.program_id, event.event_id)
                if any(item[0] == contract.contract_id for item in contract_events):
                    raise IntegrityViolation("duplicate Verification contract semantic identity")
                contract_events.add(binding)
                continue

            try:
                verification = record_from_json(
                    Verification,
                    canonical_json(event.payload["verification"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise IntegrityViolation("Verification recorded Event is malformed") from exc
            if not isinstance(verification, Verification):
                raise IntegrityViolation("Verification recorded Event decoded wrong type")
            program_id = verification.subject_ref.removeprefix("program:")
            if not program_id or event.correlation_id != program_id:
                raise IntegrityViolation("Verification recorded Event Program mismatch")
            binding = (
                verification.verification_id,
                verification.contract_ref,
                program_id,
                event.event_id,
            )
            if any(item[0] == verification.verification_id for item in verification_events):
                raise IntegrityViolation("duplicate Verification semantic identity")
            verification_events.add(binding)
        return contract_events, verification_events

    def _validate_index_alignment(self) -> None:
        contract_rows = self._host_store._db.execute(
            "SELECT contract_id, program_id, registered_event_id FROM verification_contracts"
        ).fetchall()
        indexed_contracts = self._host_store._db.execute(
            "SELECT contract_id, program_id, event_id FROM verification_contract_event_index"
        ).fetchall()
        canonical_contracts = {
            (str(row["contract_id"]), str(row["program_id"]), str(row["registered_event_id"]))
            for row in contract_rows
        }
        indexed_contract_set = {
            (str(row["contract_id"]), str(row["program_id"]), str(row["event_id"]))
            for row in indexed_contracts
        }

        receipt_rows = self._host_store._db.execute(
            """
            SELECT verification_id, contract_id, program_id, event_id
            FROM verification_receipts
            """
        ).fetchall()
        indexed_receipts = self._host_store._db.execute(
            """
            SELECT verification_id, contract_id, program_id, event_id
            FROM verification_event_index
            """
        ).fetchall()
        canonical_receipts = {
            (
                str(row["verification_id"]),
                str(row["contract_id"]),
                str(row["program_id"]),
                str(row["event_id"]),
            )
            for row in receipt_rows
        }
        indexed_receipt_set = {
            (
                str(row["verification_id"]),
                str(row["contract_id"]),
                str(row["program_id"]),
                str(row["event_id"]),
            )
            for row in indexed_receipts
        }
        event_contracts, event_receipts = self._semantic_event_sets()
        if not (
            canonical_contracts == indexed_contract_set == event_contracts
        ):
            raise IntegrityViolation(
                "Verification contract records/index diverge from semantic Events"
            )
        if not (
            canonical_receipts == indexed_receipt_set == event_receipts
        ):
            raise IntegrityViolation(
                "Verification receipt records/index diverge from semantic Events"
            )
        for row in receipt_rows:
            self.get(str(row["verification_id"]))

    @staticmethod
    def _validate_contract(contract: VerificationContract, program: Program) -> None:
        if not contract.contract_id.strip() or not contract.program_id.strip():
            raise InvalidRequest("Verification contract identities must be non-empty")
        if contract.program_id != program.program_id:
            raise InvalidRequest("Verification contract Program identity mismatch")
        if len(set(contract.success_criteria)) != len(contract.success_criteria):
            raise InvalidRequest("Verification contract success criteria must be unique")
        if any(not criterion.strip() for criterion in contract.success_criteria):
            raise InvalidRequest("Verification contract criteria must be non-empty")
        unknown = set(contract.success_criteria) - set(program.success_criteria)
        if unknown:
            raise InvalidRequest("Verification contract references unknown success criteria")
        if len(set(contract.required_claim_refs)) != len(contract.required_claim_refs):
            raise InvalidRequest("Verification contract Claim references must be unique")
        if any(not claim_id.strip() for claim_id in contract.required_claim_refs):
            raise InvalidRequest("Verification contract Claim identities must be non-empty")

    def register_contract(
        self,
        *,
        program_id: str,
        success_criteria: tuple[str, ...],
        required_claim_refs: tuple[str, ...] = (),
        mandatory: bool = True,
        require_effect_certainty: bool = True,
    ) -> VerificationContract:
        program = self._host_store.get(program_id)
        if program.status in {
            ProgramStatus.COMPLETED,
            ProgramStatus.FAILED,
            ProgramStatus.CANCELLED,
        }:
            raise InvalidRequest("terminal Program cannot receive a Verification contract")
        for claim_id in required_claim_refs:
            self._claims.get(claim_id)
        contract = VerificationContract(
            contract_id=str(uuid4()),
            program_id=program_id,
            success_criteria=success_criteria,
            required_claim_refs=required_claim_refs,
            mandatory=mandatory,
            require_effect_certainty=require_effect_certainty,
            created_at=utc_now(),
        )
        self._validate_contract(contract, program)
        with self._host_store._transaction():
            current = self._host_store.get(program_id)
            self._validate_contract(contract, current)
            if current.success_criteria != program.success_criteria:
                raise PersistenceConflict("Program success criteria changed before contract admission")
            event = self._append_event(
                "verification.contract_registered",
                {"contract": contract},
                program_id=program_id,
            )
            try:
                self._host_store._db.execute(
                    """
                    INSERT INTO verification_contract_event_index(
                        sequence, program_id, contract_id, event_id
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (event.sequence, contract.program_id, contract.contract_id, event.event_id),
                )
                self._host_store._db.execute(
                    """
                    INSERT INTO verification_contracts(
                        contract_id, program_id, contract_json, contract_digest,
                        registered_event_id
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        contract.contract_id,
                        contract.program_id,
                        record_to_json(contract),
                        canonical_digest(contract),
                        event.event_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PersistenceConflict("Verification contract identity collision") from exc
        return contract

    def contract(self, contract_id: str) -> VerificationContract:
        index = self._host_store._db.execute(
            """
            SELECT sequence, program_id, contract_id, event_id
            FROM verification_contract_event_index WHERE contract_id = ?
            """,
            (contract_id,),
        ).fetchone()
        if index is None:
            raise InvalidRequest(f"unknown Verification contract: {contract_id}")
        row = self._host_store._db.execute(
            """
            SELECT contract_id, program_id, contract_json, contract_digest,
                   registered_event_id
            FROM verification_contracts WHERE contract_id = ?
            """,
            (contract_id,),
        ).fetchone()
        if row is None:
            raise IntegrityViolation("Verification contract index lacks canonical record")
        try:
            contract = record_from_json(VerificationContract, row["contract_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Verification contract cannot be decoded") from exc
        if not isinstance(contract, VerificationContract):
            raise IntegrityViolation("Verification contract decoded wrong type")
        if (
            contract.contract_id != row["contract_id"]
            or contract.program_id != row["program_id"]
            or canonical_digest(contract) != row["contract_digest"]
            or index["program_id"] != contract.program_id
            or index["event_id"] != row["registered_event_id"]
        ):
            raise IntegrityViolation("Verification contract row/index integrity mismatch")
        event = self._event(str(row["registered_event_id"]))
        if (
            event.sequence != int(index["sequence"])
            or event.event_type != "verification.contract_registered"
            or event.correlation_id != contract.program_id
            or to_canonical_data(event.payload.get("contract")) != to_canonical_data(contract)
        ):
            raise IntegrityViolation("Verification contract diverges from semantic Event")
        self._validate_contract(contract, self._host_store.get(contract.program_id))
        return contract

    def contracts_for_program(self, program_id: str) -> tuple[VerificationContract, ...]:
        self._validate_index_alignment()
        rows = self._host_store._db.execute(
            """
            SELECT contract_id FROM verification_contract_event_index
            WHERE program_id = ? ORDER BY sequence
            """,
            (program_id,),
        ).fetchall()
        return tuple(self.contract(str(row["contract_id"])) for row in rows)

    def _required_evidence_refs(self, contract: VerificationContract) -> tuple[str, ...]:
        evidence_ids: set[str] = set()
        for claim_id in contract.required_claim_refs:
            for evidence in self._claims.verification_evidence(claim_id):
                evidence_ids.add(evidence.evidence_id)
        return tuple(sorted(evidence_ids))

    @staticmethod
    def _validate_observation(observation: VerificationObservation) -> None:
        if not isinstance(observation.result, VerificationResult):
            raise InvalidRequest("Verifier returned an invalid Verification result")
        if not observation.rationale_code.strip():
            raise InvalidRequest("Verifier rationale code must be non-empty")

    def _event_high_water(self) -> int:
        row = self._host_store._db.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM events"
        ).fetchone()
        return int(row[0])

    def run(
        self,
        contract_id: str,
        *,
        expected_program_revision: int,
        verifier: Verifier,
    ) -> Verification:
        contract = self.contract(contract_id)
        program = self._host_store.get(contract.program_id)
        if program.revision != expected_program_revision:
            raise VerificationStale(
                f"Verification expected Program revision {expected_program_revision}, "
                f"current revision {program.revision}"
            )
        if program.status is not ProgramStatus.COMPLETION_PENDING:
            raise InvalidRequest("Verification runs only during completion_pending")
        evidence_refs = self._required_evidence_refs(contract)
        evaluation_boundary = self._event_high_water()
        observation = verifier.verify(contract, program, evidence_refs)
        self._validate_observation(observation)
        verification = Verification(
            verification_id=str(uuid4()),
            subject_ref=f"program:{program.program_id}",
            subject_revision=program.revision,
            subject_digest=canonical_digest(program),
            contract_ref=contract.contract_id,
            result=observation.result,
            evidence_refs=evidence_refs,
            performed_at=utc_now(),
        )
        with self._host_store._transaction():
            current = self._host_store.get(program.program_id)
            if (
                current.revision != program.revision
                or canonical_digest(current) != verification.subject_digest
                or current.status is not ProgramStatus.COMPLETION_PENDING
            ):
                raise VerificationStale("Program changed before Verification admission")
            current_contract = self.contract(contract.contract_id)
            if current_contract != contract:
                raise IntegrityViolation("Verification contract changed after evaluation")
            if self._required_evidence_refs(contract) != verification.evidence_refs:
                raise VerificationStale("Verification Evidence roots changed before admission")
            if self._has_later_protected_operation_change(
                program.program_id,
                after_sequence=evaluation_boundary,
            ):
                raise VerificationStale(
                    "protected mutating Operation changed during Verification evaluation"
                )
            event = self._append_event(
                "verification.recorded",
                {"verification": verification, "rationale_code": observation.rationale_code},
                program_id=program.program_id,
            )
            try:
                self._host_store._db.execute(
                    """
                    INSERT INTO verification_event_index(
                        sequence, program_id, contract_id, verification_id, event_id
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        event.sequence,
                        program.program_id,
                        contract.contract_id,
                        verification.verification_id,
                        event.event_id,
                    ),
                )
                self._host_store._db.execute(
                    """
                    INSERT INTO verification_receipts(
                        verification_id, contract_id, program_id,
                        event_sequence, event_id, verification_json,
                        verification_digest, rationale_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        verification.verification_id,
                        contract.contract_id,
                        program.program_id,
                        event.sequence,
                        event.event_id,
                        record_to_json(verification),
                        canonical_digest(verification),
                        observation.rationale_code,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PersistenceConflict("Verification receipt identity collision") from exc
        return verification

    def get(self, verification_id: str) -> Verification:
        index = self._host_store._db.execute(
            """
            SELECT sequence, program_id, contract_id, verification_id, event_id
            FROM verification_event_index WHERE verification_id = ?
            """,
            (verification_id,),
        ).fetchone()
        if index is None:
            raise InvalidRequest(f"unknown Verification: {verification_id}")
        row = self._host_store._db.execute(
            """
            SELECT verification_id, contract_id, program_id,
                   event_sequence, event_id, verification_json,
                   verification_digest, rationale_code
            FROM verification_receipts WHERE verification_id = ?
            """,
            (verification_id,),
        ).fetchone()
        if row is None:
            raise IntegrityViolation("Verification Event index lacks canonical receipt")
        try:
            verification = record_from_json(Verification, row["verification_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Verification receipt cannot be decoded") from exc
        if not isinstance(verification, Verification):
            raise IntegrityViolation("Verification receipt decoded wrong type")
        if (
            verification.verification_id != row["verification_id"]
            or verification.contract_ref != row["contract_id"]
            or verification.subject_ref != f"program:{row['program_id']}"
            or canonical_digest(verification) != row["verification_digest"]
            or not str(row["rationale_code"]).strip()
            or index["program_id"] != row["program_id"]
            or index["contract_id"] != row["contract_id"]
            or index["event_id"] != row["event_id"]
            or int(index["sequence"]) != int(row["event_sequence"])
        ):
            raise IntegrityViolation("Verification receipt row/index integrity mismatch")
        event = self._event(str(row["event_id"]))
        if (
            event.sequence != int(row["event_sequence"])
            or event.event_type != "verification.recorded"
            or event.correlation_id != row["program_id"]
            or to_canonical_data(event.payload.get("verification"))
            != to_canonical_data(verification)
            or event.payload.get("rationale_code") != row["rationale_code"]
        ):
            raise IntegrityViolation("Verification receipt diverges from semantic Event")
        return verification

    def latest_for_contract(self, contract_id: str) -> Verification | None:
        self.contract(contract_id)
        row = self._host_store._db.execute(
            """
            SELECT verification_id FROM verification_event_index
            WHERE contract_id = ? ORDER BY sequence DESC LIMIT 1
            """,
            (contract_id,),
        ).fetchone()
        if row is None:
            return None
        return self.get(str(row["verification_id"]))

    def _requested_operation_effects(self, program_id: str) -> dict[str, EffectClass]:
        rows = self._host_store._db.execute(
            """
            SELECT sequence, event_id, program_id, event_type, event_json, event_digest
            FROM events WHERE event_type = 'operation.requested' ORDER BY sequence
            """,
        ).fetchall()
        effects: dict[str, EffectClass] = {}
        for row in rows:
            event = self._decode_event_row(row)
            try:
                operation = record_from_json(
                    Operation,
                    canonical_json(event.payload["operation"]),
                )
                resolution = record_from_json(
                    CapabilityResolution,
                    canonical_json(event.payload["resolution"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise IntegrityViolation("requested Operation Event is malformed") from exc
            if not isinstance(operation, Operation) or not isinstance(
                resolution, CapabilityResolution
            ):
                raise IntegrityViolation("requested Operation Event decoded wrong type")
            if (
                event.correlation_id != operation.program_id
                or event.actor_id != operation.actor_id
                or operation.capability_id != resolution.capability_id
                or operation.request_digest != canonical_digest(resolution)
            ):
                raise IntegrityViolation("requested Operation Event binding mismatch")
            if operation.program_id != program_id:
                continue
            if operation.operation_id in effects:
                raise IntegrityViolation("duplicate requested Operation identity")
            effects[operation.operation_id] = resolution.resolved_effect.effect_class
        return effects

    def _has_later_protected_operation_change(
        self,
        program_id: str,
        *,
        after_sequence: int,
    ) -> bool:
        effects = self._requested_operation_effects(program_id)
        rows = self._host_store._db.execute(
            """
            SELECT sequence, event_id, program_id, event_type, event_json, event_digest
            FROM events
            WHERE sequence > ?
              AND event_type IN (
                  'operation.requested', 'operation.admitted', 'operation.started',
                  'operation.finished', 'operation.interrupted', 'operation.reconciled'
              )
            ORDER BY sequence
            """,
            (after_sequence,),
        ).fetchall()
        for row in rows:
            event = self._decode_event_row(row)
            if event.correlation_id != program_id:
                continue
            try:
                operation = record_from_json(
                    Operation,
                    canonical_json(event.payload["operation"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise IntegrityViolation("Operation change Event is malformed") from exc
            if not isinstance(operation, Operation):
                raise IntegrityViolation("Operation change Event decoded wrong type")
            if (
                operation.program_id != program_id
                or event.actor_id != operation.actor_id
            ):
                raise IntegrityViolation("Operation change Event context mismatch")
            effect_class = effects.get(operation.operation_id)
            if effect_class is None:
                raise IntegrityViolation("Operation change lacks requested semantic root")
            if effect_class is not EffectClass.OBSERVE:
                return True
        return False

    def current(self, verification_id: str) -> Verification:
        verification = self.get(verification_id)
        contract = self.contract(verification.contract_ref)
        program = self._host_store.get(contract.program_id)
        if (
            verification.subject_ref != f"program:{program.program_id}"
            or verification.subject_revision != program.revision
            or verification.subject_digest != canonical_digest(program)
            or program.status is not ProgramStatus.COMPLETION_PENDING
        ):
            raise VerificationStale("Verification no longer matches current Program state")
        try:
            current_evidence = self._required_evidence_refs(contract)
        except (EvidenceMissing, EvidenceInvalid) as exc:
            raise VerificationStale("Verification Claim/Evidence roots are no longer current") from exc
        if current_evidence != verification.evidence_refs:
            raise VerificationStale("Verification Evidence roots are no longer current")
        index = self._host_store._db.execute(
            "SELECT sequence FROM verification_event_index WHERE verification_id = ?",
            (verification_id,),
        ).fetchone()
        if index is None:
            raise IntegrityViolation("Verification currentness lacks semantic Event index")
        if self._has_later_protected_operation_change(
            program.program_id,
            after_sequence=int(index["sequence"]),
        ):
            raise VerificationStale(
                "Verification predates a protected mutating Operation change"
            )
        return verification

    def current_latest_for_contract(self, contract_id: str) -> Verification | None:
        latest = self.latest_for_contract(contract_id)
        if latest is None:
            return None
        return self.current(latest.verification_id)
