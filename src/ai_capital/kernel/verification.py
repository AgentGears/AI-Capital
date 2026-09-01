from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from .claim_store import ClaimRepository
from .durable_program import ProgramRepository
from .enums import ProgramStatus, VerificationResult
from .errors import (
    EvidenceInvalid,
    EvidenceMissing,
    IntegrityViolation,
    InvalidRequest,
    PersistenceConflict,
    VerificationStale,
)
from .events import event_digest_fields, utc_now, verify_event_digest
from .models import Event, Program, Verification
from .schema_codec import record_from_json, record_to_json
from .serialization import canonical_digest, to_canonical_data


_COMPONENT = "verification"
_COMPONENT_SCHEMA_VERSION = 1


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
            if version not in {None, _COMPONENT_SCHEMA_VERSION}:
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
            if version is None:
                self._host_store._db.execute(
                    "INSERT INTO component_schema(component, version) VALUES (?, ?)",
                    (_COMPONENT, _COMPONENT_SCHEMA_VERSION),
                )

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
        row = self._host_store._db.execute(
            """
            SELECT contract_id, program_id, contract_json, contract_digest,
                   registered_event_id
            FROM verification_contracts WHERE contract_id = ?
            """,
            (contract_id,),
        ).fetchone()
        if row is None:
            raise InvalidRequest(f"unknown Verification contract: {contract_id}")
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
        ):
            raise IntegrityViolation("Verification contract row integrity mismatch")
        event = self._event(str(row["registered_event_id"]))
        if (
            event.event_type != "verification.contract_registered"
            or event.correlation_id != contract.program_id
            or to_canonical_data(event.payload.get("contract")) != to_canonical_data(contract)
        ):
            raise IntegrityViolation("Verification contract diverges from semantic Event")
        self._validate_contract(contract, self._host_store.get(contract.program_id))
        return contract

    def contracts_for_program(self, program_id: str) -> tuple[VerificationContract, ...]:
        rows = self._host_store._db.execute(
            """
            SELECT contract_id FROM verification_contracts
            WHERE program_id = ? ORDER BY contract_id
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
            event = self._append_event(
                "verification.recorded",
                {"verification": verification, "rationale_code": observation.rationale_code},
                program_id=program.program_id,
            )
            try:
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
            raise InvalidRequest(f"unknown Verification: {verification_id}")
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
        ):
            raise IntegrityViolation("Verification receipt row integrity mismatch")
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
            SELECT verification_id FROM verification_receipts
            WHERE contract_id = ? ORDER BY event_sequence DESC LIMIT 1
            """,
            (contract_id,),
        ).fetchone()
        if row is None:
            return None
        return self.get(str(row["verification_id"]))

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
        return verification

    def current_latest_for_contract(self, contract_id: str) -> Verification | None:
        latest = self.latest_for_contract(contract_id)
        if latest is None:
            return None
        return self.current(latest.verification_id)
