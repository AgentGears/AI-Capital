from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from enum import Enum
from uuid import uuid4

from .durable_program import ProgramRepository
from .enums import ClaimStatus
from .errors import EvidenceInvalid, EvidenceMissing, IntegrityViolation, InvalidRequest, PersistenceConflict
from .events import event_digest_fields, utc_now, verify_event_digest
from .evidence_store import EvidenceReference, EvidenceRepository
from .models import Claim, Event
from .schema_codec import record_from_json, record_to_json
from .serialization import canonical_digest, to_canonical_data


_COMPONENT = "claim_store"
_COMPONENT_SCHEMA_VERSION = 1


class ClaimEvidenceRelation(str, Enum):
    REFERENCE = "reference"
    SUPPORT = "support"
    CONTRADICTION = "contradiction"


@dataclass(frozen=True, slots=True)
class ClaimEvidenceLink:
    claim_id: str
    evidence_id: str
    relation: ClaimEvidenceRelation
    linked_at: str


@dataclass(frozen=True, slots=True)
class ClaimStateReceipt:
    receipt_id: str
    claim_id: str
    prior_status: ClaimStatus | None
    status: ClaimStatus
    rationale_code: str
    changed_at: str
    successor_claim_id: str | None = None


@dataclass(frozen=True, slots=True)
class DispositionInputs:
    claim_id: str
    claim_status: ClaimStatus
    support_refs: tuple[EvidenceReference, ...]
    contradiction_refs: tuple[EvidenceReference, ...]
    reference_refs: tuple[EvidenceReference, ...]


class ClaimRepository:
    """Host-owned Claim state with explicit admitted-Evidence relations."""

    def __init__(self, host_store: ProgramRepository, evidence: EvidenceRepository):
        if evidence._host_store is not host_store:
            raise InvalidRequest("Claim and Evidence repositories must share one Host store")
        self._host_store = host_store
        self._evidence = evidence
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
            if version is not None:
                if version > _COMPONENT_SCHEMA_VERSION:
                    raise IntegrityViolation(
                        f"Claim schema version {version} is newer than supported "
                        f"{_COMPONENT_SCHEMA_VERSION}"
                    )
                if version != _COMPONENT_SCHEMA_VERSION:
                    raise IntegrityViolation(f"unsupported Claim schema version {version}")
                return

            statements = (
                """
                CREATE TABLE claim_projections (
                    claim_id TEXT PRIMARY KEY,
                    claim_json TEXT NOT NULL,
                    claim_digest TEXT NOT NULL,
                    last_sequence INTEGER NOT NULL
                )
                """,
                """
                CREATE TABLE claim_history (
                    sequence INTEGER PRIMARY KEY,
                    claim_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    claim_json TEXT NOT NULL,
                    claim_digest TEXT NOT NULL
                )
                """,
                """
                CREATE INDEX claim_history_claim
                    ON claim_history(claim_id, sequence)
                """,
                """
                CREATE TABLE claim_evidence_links (
                    claim_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    event_sequence INTEGER NOT NULL,
                    link_json TEXT NOT NULL,
                    link_digest TEXT NOT NULL,
                    PRIMARY KEY(claim_id, evidence_id, relation),
                    FOREIGN KEY(claim_id) REFERENCES claim_projections(claim_id),
                    FOREIGN KEY(evidence_id) REFERENCES evidence_records(evidence_id)
                )
                """,
                """
                CREATE TABLE claim_supersessions (
                    claim_id TEXT PRIMARY KEY,
                    successor_claim_id TEXT NOT NULL,
                    event_sequence INTEGER NOT NULL UNIQUE,
                    receipt_json TEXT NOT NULL,
                    receipt_digest TEXT NOT NULL,
                    FOREIGN KEY(claim_id) REFERENCES claim_projections(claim_id),
                    FOREIGN KEY(successor_claim_id) REFERENCES claim_projections(claim_id)
                )
                """,
            )
            for statement in statements:
                self._host_store._db.execute(statement)
            self._host_store._db.execute(
                "INSERT INTO component_schema(component, version) VALUES (?, ?)",
                (_COMPONENT, _COMPONENT_SCHEMA_VERSION),
            )

    def _append_event(self, event_type: str, payload: object, *, claim_id: str) -> Event:
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
            correlation_id=claim_id,
        )
        event = Event(
            event_id=event_id,
            sequence=sequence,
            event_type=event_type,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            payload=canonical_payload,
            digest=digest,
            correlation_id=claim_id,
        )
        self._host_store._insert_event(event)
        return event

    def _event_by_sequence(self, sequence: int) -> Event:
        row = self._host_store._db.execute(
            """
            SELECT sequence, event_id, program_id, event_type, event_json, event_digest
            FROM events WHERE sequence = ?
            """,
            (sequence,),
        ).fetchone()
        if row is None:
            raise IntegrityViolation("Claim history Event is missing")
        try:
            event = record_from_json(Event, row["event_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Claim history Event cannot be decoded") from exc
        if not isinstance(event, Event):
            raise IntegrityViolation("Claim history Event decoded wrong type")
        if (
            event.sequence != int(row["sequence"])
            or event.event_id != row["event_id"]
            or event.program_id != row["program_id"]
            or event.event_type != row["event_type"]
            or event.digest != row["event_digest"]
            or not verify_event_digest(event)
        ):
            raise IntegrityViolation("Claim history Event integrity mismatch")
        return event

    def _claim_event_sequences(self, claim_id: str) -> tuple[int, ...]:
        rows = self._host_store._db.execute(
            """
            SELECT sequence FROM events
            WHERE event_type LIKE 'claim.%'
            ORDER BY sequence
            """
        ).fetchall()
        sequences: list[int] = []
        for row in rows:
            event = self._event_by_sequence(int(row["sequence"]))
            if event.correlation_id == claim_id:
                sequences.append(event.sequence)
        return tuple(sequences)

    @staticmethod
    def _validate_claim(claim: Claim) -> None:
        if not claim.claim_id.strip() or not claim.statement.strip():
            raise InvalidRequest("Claim identity and statement must be non-empty")
        if len(set(claim.evidence_refs)) != len(claim.evidence_refs):
            raise IntegrityViolation("Claim contains duplicate Evidence references")

    def _row(self, claim_id: str) -> sqlite3.Row:
        row = self._host_store._db.execute(
            """
            SELECT claim_id, claim_json, claim_digest, last_sequence
            FROM claim_projections WHERE claim_id = ?
            """,
            (claim_id,),
        ).fetchone()
        if row is None:
            raise InvalidRequest(f"unknown Claim: {claim_id}")
        return row

    def _history_rows(self, claim_id: str) -> tuple[sqlite3.Row, ...]:
        rows = tuple(
            self._host_store._db.execute(
                """
                SELECT sequence, claim_id, event_type, claim_json, claim_digest
                FROM claim_history WHERE claim_id = ? ORDER BY sequence
                """,
                (claim_id,),
            ).fetchall()
        )
        history_sequences = tuple(int(row["sequence"]) for row in rows)
        event_sequences = self._claim_event_sequences(claim_id)
        if history_sequences != event_sequences:
            raise IntegrityViolation("Claim history sequence diverges from semantic Event history")
        return rows

    def _history_claim(self, row: sqlite3.Row) -> Claim:
        try:
            claim = record_from_json(Claim, row["claim_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Claim history cannot be decoded") from exc
        if not isinstance(claim, Claim) or claim.claim_id != row["claim_id"]:
            raise IntegrityViolation("Claim history identity mismatch")
        if canonical_digest(claim) != row["claim_digest"]:
            raise IntegrityViolation("Claim history digest mismatch")
        event = self._event_by_sequence(int(row["sequence"]))
        if event.event_type != row["event_type"] or event.correlation_id != claim.claim_id:
            raise IntegrityViolation("Claim history/Event binding mismatch")
        try:
            event_claim = event.payload["claim"]
        except KeyError as exc:
            raise IntegrityViolation("Claim Event lacks canonical Claim payload") from exc
        if to_canonical_data(event_claim) != to_canonical_data(claim):
            raise IntegrityViolation("Claim history diverges from semantic Event")
        return claim

    def _links(self, claim_id: str) -> tuple[ClaimEvidenceLink, ...]:
        rows = self._host_store._db.execute(
            """
            SELECT claim_id, evidence_id, relation, event_sequence, link_json, link_digest
            FROM claim_evidence_links
            WHERE claim_id = ? ORDER BY relation, evidence_id
            """,
            (claim_id,),
        ).fetchall()
        links: list[ClaimEvidenceLink] = []
        for row in rows:
            try:
                link = record_from_json(ClaimEvidenceLink, row["link_json"])
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation("Claim Evidence link cannot be decoded") from exc
            if not isinstance(link, ClaimEvidenceLink):
                raise IntegrityViolation("Claim Evidence link decoded wrong type")
            if (
                link.claim_id != row["claim_id"]
                or link.evidence_id != row["evidence_id"]
                or link.relation.value != row["relation"]
                or canonical_digest(link) != row["link_digest"]
            ):
                raise IntegrityViolation("Claim Evidence link row integrity mismatch")
            self._evidence.get(link.evidence_id)
            event = self._event_by_sequence(int(row["event_sequence"]))
            if event.correlation_id != claim_id:
                raise IntegrityViolation("Claim Evidence link Event correlation mismatch")
            payload_links: list[object] = []
            if "link" in event.payload:
                payload_links.append(event.payload["link"])
            if "links" in event.payload:
                raw_links = event.payload["links"]
                if not isinstance(raw_links, tuple):
                    raise IntegrityViolation("Claim Evidence link Event payload is malformed")
                payload_links.extend(raw_links)
            canonical_link = to_canonical_data(link)
            if canonical_link not in tuple(to_canonical_data(item) for item in payload_links):
                raise IntegrityViolation("Claim Evidence link diverges from semantic Event")
            links.append(link)
        return tuple(links)

    def get(self, claim_id: str) -> Claim:
        row = self._row(claim_id)
        try:
            claim = record_from_json(Claim, row["claim_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Claim projection cannot be decoded") from exc
        if not isinstance(claim, Claim) or claim.claim_id != row["claim_id"]:
            raise IntegrityViolation("Claim projection identity mismatch")
        if canonical_digest(claim) != row["claim_digest"]:
            raise IntegrityViolation("Claim projection digest mismatch")
        self._validate_claim(claim)

        history_rows = self._history_rows(claim_id)
        if (
            not history_rows
            or int(history_rows[-1]["sequence"]) != int(row["last_sequence"])
            or self._history_claim(history_rows[-1]) != claim
        ):
            raise IntegrityViolation("Claim projection diverges from durable history")

        links = self._links(claim_id)
        linked_refs = tuple(sorted({link.evidence_id for link in links}))
        if tuple(sorted(claim.evidence_refs)) != linked_refs:
            raise IntegrityViolation("Claim Evidence references disagree with durable links")
        support = tuple(link for link in links if link.relation is ClaimEvidenceRelation.SUPPORT)
        contradiction = tuple(
            link for link in links if link.relation is ClaimEvidenceRelation.CONTRADICTION
        )
        if claim.status is ClaimStatus.SUPPORTED and not support:
            raise IntegrityViolation("supported Claim lacks admitted support Evidence")
        if claim.status is ClaimStatus.CONTRADICTED and not contradiction:
            raise IntegrityViolation("contradicted Claim lacks admitted contradiction Evidence")

        supersession = self._host_store._db.execute(
            """
            SELECT successor_claim_id, event_sequence, receipt_json, receipt_digest
            FROM claim_supersessions WHERE claim_id = ?
            """,
            (claim_id,),
        ).fetchone()
        if claim.status is ClaimStatus.SUPERSEDED:
            if supersession is None:
                raise IntegrityViolation("superseded Claim lacks successor relation")
            try:
                receipt = record_from_json(ClaimStateReceipt, supersession["receipt_json"])
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation("Claim supersession receipt cannot be decoded") from exc
            if not isinstance(receipt, ClaimStateReceipt):
                raise IntegrityViolation("Claim supersession receipt decoded wrong type")
            if canonical_digest(receipt) != supersession["receipt_digest"]:
                raise IntegrityViolation("Claim supersession receipt digest mismatch")
            if receipt.claim_id != claim_id or receipt.successor_claim_id != supersession["successor_claim_id"]:
                raise IntegrityViolation("Claim supersession binding mismatch")
            event = self._event_by_sequence(int(supersession["event_sequence"]))
            if event.event_type != "claim.superseded" or event.correlation_id != claim_id:
                raise IntegrityViolation("Claim supersession Event binding mismatch")
            if to_canonical_data(event.payload.get("state")) != to_canonical_data(receipt):
                raise IntegrityViolation("Claim supersession receipt diverges from semantic Event")
        elif supersession is not None:
            raise IntegrityViolation("non-superseded Claim has successor relation")
        return claim

    def create(self, statement: str, *, claim_id: str | None = None) -> Claim:
        claim = Claim(
            claim_id=claim_id or str(uuid4()),
            statement=statement,
            evidence_refs=(),
            status=ClaimStatus.PROPOSED,
            created_at=utc_now(),
        )
        self._validate_claim(claim)
        state = ClaimStateReceipt(
            receipt_id=str(uuid4()),
            claim_id=claim.claim_id,
            prior_status=None,
            status=ClaimStatus.PROPOSED,
            rationale_code="claim_created",
            changed_at=claim.created_at,
        )
        try:
            with self._host_store._transaction():
                history_row = self._host_store._db.execute(
                    "SELECT 1 FROM claim_history WHERE claim_id = ? LIMIT 1",
                    (claim.claim_id,),
                ).fetchone()
                if history_row is not None or self._claim_event_sequences(claim.claim_id):
                    raise PersistenceConflict(
                        f"Claim identity already exists in durable history: {claim.claim_id}"
                    )
                event = self._append_event(
                    "claim.created",
                    {"claim": claim, "state": state},
                    claim_id=claim.claim_id,
                )
                self._host_store._db.execute(
                    """
                    INSERT INTO claim_projections(claim_id, claim_json, claim_digest, last_sequence)
                    VALUES (?, ?, ?, ?)
                    """,
                    (claim.claim_id, record_to_json(claim), canonical_digest(claim), event.sequence),
                )
                self._host_store._db.execute(
                    """
                    INSERT INTO claim_history(sequence, claim_id, event_type, claim_json, claim_digest)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (event.sequence, claim.claim_id, event.event_type, record_to_json(claim), canonical_digest(claim)),
                )
        except sqlite3.IntegrityError as exc:
            raise PersistenceConflict(f"Claim identity already exists: {claim.claim_id}") from exc
        return claim

    def _insert_link(self, link: ClaimEvidenceLink, *, event_sequence: int) -> None:
        self._host_store._db.execute(
            """
            INSERT INTO claim_evidence_links(
                claim_id, evidence_id, relation, event_sequence, link_json, link_digest
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                link.claim_id,
                link.evidence_id,
                link.relation.value,
                event_sequence,
                record_to_json(link),
                canonical_digest(link),
            ),
        )

    def _write_projection(self, previous: Claim, updated: Claim, event: Event) -> None:
        self._validate_claim(updated)
        if previous.claim_id != updated.claim_id:
            raise IntegrityViolation("Claim mutation changed identity")
        cursor = self._host_store._db.execute(
            """
            UPDATE claim_projections
            SET claim_json = ?, claim_digest = ?, last_sequence = ?
            WHERE claim_id = ? AND claim_digest = ?
            """,
            (
                record_to_json(updated),
                canonical_digest(updated),
                event.sequence,
                updated.claim_id,
                canonical_digest(previous),
            ),
        )
        if cursor.rowcount != 1:
            raise PersistenceConflict("Claim changed during epistemic state commit")
        self._host_store._db.execute(
            """
            INSERT INTO claim_history(sequence, claim_id, event_type, claim_json, claim_digest)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event.sequence, updated.claim_id, event.event_type, record_to_json(updated), canonical_digest(updated)),
        )

    def add_reference(self, claim_id: str, evidence_id: str) -> Claim:
        self._evidence.get(evidence_id)
        with self._host_store._transaction():
            previous = self.get(claim_id)
            if previous.status is ClaimStatus.SUPERSEDED:
                raise InvalidRequest("superseded Claim cannot accept new Evidence")
            link = ClaimEvidenceLink(
                claim_id=claim_id,
                evidence_id=evidence_id,
                relation=ClaimEvidenceRelation.REFERENCE,
                linked_at=utc_now(),
            )
            updated = replace(
                previous,
                evidence_refs=tuple(sorted(set(previous.evidence_refs) | {evidence_id})),
            )
            event = self._append_event(
                "claim.evidence_linked",
                {"claim": updated, "link": link},
                claim_id=claim_id,
            )
            try:
                self._insert_link(link, event_sequence=event.sequence)
            except sqlite3.IntegrityError as exc:
                raise PersistenceConflict("Claim already has this Evidence reference") from exc
            self._write_projection(previous, updated, event)
        return updated

    def _transition_with_evidence(
        self,
        *,
        claim_id: str,
        evidence_ids: tuple[str, ...],
        relation: ClaimEvidenceRelation,
        target_status: ClaimStatus,
        rationale_code: str,
        event_type: str,
    ) -> Claim:
        if not evidence_ids or len(set(evidence_ids)) != len(evidence_ids):
            raise EvidenceInvalid("Claim transition requires unique admitted Evidence")
        for evidence_id in evidence_ids:
            self._evidence.get(evidence_id)
        if not rationale_code.strip():
            raise InvalidRequest("Claim transition requires a rationale code")

        with self._host_store._transaction():
            previous = self.get(claim_id)
            if previous.status is ClaimStatus.SUPERSEDED:
                raise InvalidRequest("superseded Claim cannot change epistemic state")
            if target_status is ClaimStatus.SUPPORTED and previous.status is not ClaimStatus.PROPOSED:
                raise InvalidRequest("only a proposed Claim may become supported")
            if target_status is ClaimStatus.CONTRADICTED and previous.status not in {
                ClaimStatus.PROPOSED,
                ClaimStatus.SUPPORTED,
            }:
                raise InvalidRequest("Claim cannot enter contradicted state from current status")
            if relation in {
                ClaimEvidenceRelation.SUPPORT,
                ClaimEvidenceRelation.CONTRADICTION,
            }:
                opposite = (
                    ClaimEvidenceRelation.CONTRADICTION
                    if relation is ClaimEvidenceRelation.SUPPORT
                    else ClaimEvidenceRelation.SUPPORT
                )
                rows = self._host_store._db.execute(
                    """
                    SELECT evidence_id FROM claim_evidence_links
                    WHERE claim_id = ? AND relation = ?
                    """,
                    (claim_id, opposite.value),
                ).fetchall()
                opposite_ids = {str(row["evidence_id"]) for row in rows}
                if opposite_ids.intersection(evidence_ids):
                    raise EvidenceInvalid(
                        "the same Evidence cannot both support and contradict one Claim"
                    )
            links = tuple(
                ClaimEvidenceLink(
                    claim_id=claim_id,
                    evidence_id=evidence_id,
                    relation=relation,
                    linked_at=utc_now(),
                )
                for evidence_id in evidence_ids
            )
            updated = replace(
                previous,
                evidence_refs=tuple(sorted(set(previous.evidence_refs) | set(evidence_ids))),
                status=target_status,
            )
            state = ClaimStateReceipt(
                receipt_id=str(uuid4()),
                claim_id=claim_id,
                prior_status=previous.status,
                status=target_status,
                rationale_code=rationale_code,
                changed_at=utc_now(),
            )
            event = self._append_event(
                event_type,
                {"claim": updated, "links": links, "state": state},
                claim_id=claim_id,
            )
            try:
                for link in links:
                    self._insert_link(link, event_sequence=event.sequence)
            except sqlite3.IntegrityError as exc:
                raise PersistenceConflict("Claim Evidence relation already exists") from exc
            self._write_projection(previous, updated, event)
        return updated

    def support(
        self,
        claim_id: str,
        evidence_ids: tuple[str, ...],
        *,
        rationale_code: str = "admitted_support",
    ) -> Claim:
        return self._transition_with_evidence(
            claim_id=claim_id,
            evidence_ids=evidence_ids,
            relation=ClaimEvidenceRelation.SUPPORT,
            target_status=ClaimStatus.SUPPORTED,
            rationale_code=rationale_code,
            event_type="claim.supported",
        )

    def contradict(
        self,
        claim_id: str,
        evidence_ids: tuple[str, ...],
        *,
        rationale_code: str = "admitted_contradiction",
    ) -> Claim:
        return self._transition_with_evidence(
            claim_id=claim_id,
            evidence_ids=evidence_ids,
            relation=ClaimEvidenceRelation.CONTRADICTION,
            target_status=ClaimStatus.CONTRADICTED,
            rationale_code=rationale_code,
            event_type="claim.contradicted",
        )

    def supersede(
        self,
        claim_id: str,
        successor_claim_id: str,
        *,
        rationale_code: str = "superseded_by_new_claim",
    ) -> Claim:
        if claim_id == successor_claim_id:
            raise InvalidRequest("Claim cannot supersede itself")
        if not rationale_code.strip():
            raise InvalidRequest("Claim supersession requires a rationale code")
        successor = self.get(successor_claim_id)
        if successor.status is ClaimStatus.SUPERSEDED:
            raise InvalidRequest("successor Claim is already superseded")
        with self._host_store._transaction():
            previous = self.get(claim_id)
            if previous.status is ClaimStatus.SUPERSEDED:
                raise InvalidRequest("Claim is already superseded")
            updated = replace(previous, status=ClaimStatus.SUPERSEDED)
            state = ClaimStateReceipt(
                receipt_id=str(uuid4()),
                claim_id=claim_id,
                prior_status=previous.status,
                status=ClaimStatus.SUPERSEDED,
                rationale_code=rationale_code,
                changed_at=utc_now(),
                successor_claim_id=successor_claim_id,
            )
            event = self._append_event(
                "claim.superseded",
                {"claim": updated, "state": state},
                claim_id=claim_id,
            )
            self._host_store._db.execute(
                """
                INSERT INTO claim_supersessions(
                    claim_id, successor_claim_id, event_sequence, receipt_json, receipt_digest
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    claim_id,
                    successor_claim_id,
                    event.sequence,
                    record_to_json(state),
                    canonical_digest(state),
                ),
            )
            self._write_projection(previous, updated, event)
        return updated

    def history(self, claim_id: str) -> tuple[Claim, ...]:
        self._row(claim_id)
        rows = self._history_rows(claim_id)
        history = tuple(self._history_claim(row) for row in rows)
        if not history or history[0].status is not ClaimStatus.PROPOSED:
            raise IntegrityViolation("Claim history must begin in proposed state")
        if any(item.status is ClaimStatus.SUPERSEDED for item in history[:-1]):
            raise IntegrityViolation("Claim history continued after supersession")
        if history[-1] != self.get(claim_id):
            raise IntegrityViolation("Claim projection diverges from Claim history")
        return history

    def _refs(
        self,
        claim_id: str,
        relation: ClaimEvidenceRelation,
    ) -> tuple[EvidenceReference, ...]:
        return tuple(
            self._evidence.reference(link.evidence_id)
            for link in self._links(claim_id)
            if link.relation is relation
        )

    def verification_evidence(self, claim_id: str) -> tuple[EvidenceReference, ...]:
        claim = self.get(claim_id)
        if claim.status is not ClaimStatus.SUPPORTED:
            raise EvidenceMissing("verification-critical positive Claim is not supported")
        support = self._refs(claim_id, ClaimEvidenceRelation.SUPPORT)
        contradictions = self._refs(claim_id, ClaimEvidenceRelation.CONTRADICTION)
        if not support:
            raise EvidenceMissing("supported Claim has no admitted support Evidence")
        if contradictions:
            raise EvidenceInvalid("supported Claim has unresolved contradiction Evidence")
        return support

    def disposition_inputs(self, claim_id: str) -> DispositionInputs:
        claim = self.get(claim_id)
        return DispositionInputs(
            claim_id=claim.claim_id,
            claim_status=claim.status,
            support_refs=self._refs(claim_id, ClaimEvidenceRelation.SUPPORT),
            contradiction_refs=self._refs(claim_id, ClaimEvidenceRelation.CONTRADICTION),
            reference_refs=self._refs(claim_id, ClaimEvidenceRelation.REFERENCE),
        )
