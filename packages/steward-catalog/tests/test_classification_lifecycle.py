"""Approval as one atomic supersession, against a real Postgres (#50).

Every assertion here is about what survives a commit or a rollback, and about
what two connections racing each other end up with. A fake would assert our
beliefs about advisory locks and partial unique indexes rather than their
behaviour, which is the whole subject.

The property under test, stated once: **an asset has at most one approved
classification, publishing a replacement is a single operator action, and the
loser of any race gets a typed conflict rather than a database error.**
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from steward_catalog import EnvSecretResolver, build_scan_source, postgres_inspector, register_source
from steward_catalog.classification import (
    ClassificationConflict,
    ProposalNotPending,
    StaleProposal,
    approve,
    current_classification,
    proposal_history,
    propose,
    record_proposal_reviews,
    reject,
)
from steward_catalog.profiles import record_profile
from steward_queue import SYSTEM_ACTOR, QueueConnection, TaskContext, UsageLedger, connect
from steward_schemas import (
    ClassificationProposal,
    ColumnClassification,
    EvidenceKind,
    EvidenceRef,
    ProposalStatus,
    ReviewDecision,
    ReviewOutcome,
    SensitivityLabel,
    SourceCreate,
    TableProfile,
    TaskSpec,
    TaskStatus,
)

SELECT_ASSET_ID = (
    "SELECT id FROM assets WHERE schema_name = %(schema)s AND name = %(name)s"
)


def _ctx(conn: QueueConnection, spec: TaskSpec) -> TaskContext:
    return TaskContext(
        connection=conn,
        spec=spec,
        attempts=1,
        claimed_by="w-test",
        trace_id="trace-test",
        usage=UsageLedger(),
    )


@pytest.fixture
def asset_id(
    conn: QueueConnection,
    source_create: SourceCreate,
    resolver: EnvSecretResolver,
    spec_factory: Callable[[UUID], TaskSpec],
) -> UUID:
    """One catalogued, profiled asset -- the input a classification needs."""
    source, _ = register_source(conn, source_create, actor=SYSTEM_ACTOR)
    conn.commit()
    result = asyncio.run(
        build_scan_source(resolver=resolver, inspect=postgres_inspector)(
            _ctx(conn, spec_factory(source.id))
        )
    )
    conn.commit()
    assert result.status is TaskStatus.SUCCEEDED, result.error
    row = conn.execute(SELECT_ASSET_ID, {"schema": "sales", "name": "customers"}).fetchone()
    assert row is not None
    identifier: UUID = row[0]
    record_profile(conn, identifier, TableProfile(row_count=10), actor=SYSTEM_ACTOR)
    conn.commit()
    return identifier


def a_proposal(
    asset: UUID, *, profile_version: int = 1, prompt: str = "classify@v1"
) -> ClassificationProposal:
    return ClassificationProposal(
        asset_id=asset,
        profile_version=profile_version,
        prompt_version=prompt,
        model_alias="steward-classify",
        columns=(
            ColumnClassification(
                column_name="email",
                labels=(SensitivityLabel.PII,),
                confidence=Decimal("0.95"),
                evidence=(
                    EvidenceRef(
                        profile_version=profile_version,
                        column_name="email",
                        kind=EvidenceKind.COLUMN_NAME,
                        detail="named 'email'",
                    ),
                ),
            ),
        ),
    )


def a_decision(outcome: ReviewOutcome = ReviewOutcome.APPROVED, actor: str = "reviewer") -> ReviewDecision:
    return ReviewDecision(
        outcome=outcome,
        actor=actor,
        reason="looks right",
        decided_at=datetime.now(UTC),
    )


def recorded(conn: QueueConnection, asset: UUID, **kwargs: object) -> UUID:
    """A pending proposal, committed."""
    record = propose(
        conn,
        a_proposal(asset, **kwargs),  # type: ignore[arg-type]
        run_id=uuid4(),
        task_id=uuid4(),
        trace_id="trace-test",
        actor=SYSTEM_ACTOR,
    )
    conn.commit()
    return record.id


@pytest.fixture
def other(steward_dsn: str) -> Iterator[QueueConnection]:
    """A second connection, for the races."""
    connection = connect(steward_dsn)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


class TestApproval:
    def test_a_first_approval_publishes_it(self, conn: QueueConnection, asset_id: UUID) -> None:
        proposal_id = recorded(conn, asset_id)
        assert current_classification(conn, asset_id) is None

        published = approve(conn, proposal_id, decision=a_decision(), actor=SYSTEM_ACTOR)
        conn.commit()

        assert published.status is ProposalStatus.APPROVED
        current = current_classification(conn, asset_id)
        assert current is not None and current.id == proposal_id
        reviews = record_proposal_reviews(conn, proposal_id)
        assert [r.outcome for r in reviews] == [ReviewOutcome.APPROVED]
        assert reviews[0].actor == "reviewer"

    def test_approving_a_replacement_supersedes_in_one_action(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        """The operator approves the replacement; they do not also demote the
        incumbent. Two actions would leave an asset with no classification at
        all in between -- visible to every reader, and permanent if the second
        never happened."""
        first = recorded(conn, asset_id)
        approve(conn, first, decision=a_decision(), actor=SYSTEM_ACTOR)
        conn.commit()

        record_profile(conn, asset_id, TableProfile(row_count=11), actor=SYSTEM_ACTOR)
        conn.commit()
        second = recorded(conn, asset_id, profile_version=2)

        approve(conn, second, decision=a_decision(), actor=SYSTEM_ACTOR)
        conn.commit()

        current = current_classification(conn, asset_id)
        assert current is not None and current.id == second
        history = {record.id: record.status for record in proposal_history(conn, asset_id)}
        assert history[first] is ProposalStatus.SUPERSEDED
        assert history[second] is ProposalStatus.APPROVED

    def test_a_failed_approval_leaves_the_old_one_published(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        """Rollback is the whole transaction: demotion, promotion and decision.
        A half-applied supersession would unpublish a classification nobody
        rejected."""
        first = recorded(conn, asset_id)
        approve(conn, first, decision=a_decision(), actor=SYSTEM_ACTOR)
        conn.commit()

        record_profile(conn, asset_id, TableProfile(row_count=12), actor=SYSTEM_ACTOR)
        conn.commit()
        second = recorded(conn, asset_id, profile_version=2)

        approve(conn, second, decision=a_decision(), actor=SYSTEM_ACTOR)
        conn.rollback()  # the caller's transaction fails after the repository returned

        current = current_classification(conn, asset_id)
        assert current is not None and current.id == first, "the old approval was lost"
        history = {record.id: record.status for record in proposal_history(conn, asset_id)}
        assert history[second] is ProposalStatus.PENDING_REVIEW
        assert record_proposal_reviews(conn, second) == ()

    def test_an_older_proposal_cannot_supersede_a_newer_classification(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        """Approving an older version would roll the published classification
        backwards, silently -- nothing about the row says it is the older one."""
        first = recorded(conn, asset_id)
        record_profile(conn, asset_id, TableProfile(row_count=13), actor=SYSTEM_ACTOR)
        conn.commit()
        second = recorded(conn, asset_id, profile_version=2)
        approve(conn, second, decision=a_decision(), actor=SYSTEM_ACTOR)
        conn.commit()

        with pytest.raises(StaleProposal):
            approve(conn, first, decision=a_decision(), actor=SYSTEM_ACTOR)
        conn.rollback()

        current = current_classification(conn, asset_id)
        assert current is not None and current.id == second

    def test_a_proposal_whose_profile_moved_on_is_stale(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        proposal_id = recorded(conn, asset_id)
        record_profile(conn, asset_id, TableProfile(row_count=99), actor=SYSTEM_ACTOR)
        conn.commit()

        with pytest.raises(StaleProposal, match="data it describes has changed"):
            approve(conn, proposal_id, decision=a_decision(), actor=SYSTEM_ACTOR)


class TestIdempotency:
    def test_a_repeated_approval_under_one_key_returns_the_original(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        proposal_id = recorded(conn, asset_id)
        first = approve(conn, proposal_id, decision=a_decision(), idempotency_key="k1", actor=SYSTEM_ACTOR)
        conn.commit()

        again = approve(conn, proposal_id, decision=a_decision(), idempotency_key="k1", actor=SYSTEM_ACTOR)
        conn.commit()

        assert again.id == first.id and again.status is ProposalStatus.APPROVED
        # One decision, not two: a replay is not a second event.
        assert len(record_proposal_reviews(conn, proposal_id)) == 1


class TestRejection:
    def test_rejecting_leaves_the_published_version_alone(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        first = recorded(conn, asset_id)
        approve(conn, first, decision=a_decision(), actor=SYSTEM_ACTOR)
        conn.commit()

        record_profile(conn, asset_id, TableProfile(row_count=14), actor=SYSTEM_ACTOR)
        conn.commit()
        second = recorded(conn, asset_id, profile_version=2)
        reject(conn, second, decision=a_decision(ReviewOutcome.REJECTED), actor=SYSTEM_ACTOR)
        conn.commit()

        current = current_classification(conn, asset_id)
        assert current is not None and current.id == first
        history = {record.id: record.status for record in proposal_history(conn, asset_id)}
        assert history[second] is ProposalStatus.REJECTED

    def test_a_decided_proposal_cannot_be_decided_again(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        proposal_id = recorded(conn, asset_id)
        reject(conn, proposal_id, decision=a_decision(ReviewOutcome.REJECTED), actor=SYSTEM_ACTOR)
        conn.commit()

        with pytest.raises(ProposalNotPending):
            approve(conn, proposal_id, decision=a_decision(), actor=SYSTEM_ACTOR)


class TestConcurrency:
    """Two connections, one asset. The lock is the subject.

    Note what is *not* done here: no test leaves one transaction holding the
    lock while another waits for it indefinitely. That is a deadlock in the
    test, not a property of the code -- an earlier draft did exactly that and
    hung the suite. Serialisation is proven instead by a bounded wait, and the
    outcome of a race by running the two decisions in sequence, which is what
    the lock makes the concurrent case equivalent to.
    """

    def test_the_loser_of_a_true_race_gets_a_typed_conflict_not_a_db_error(
        self, conn: QueueConnection, steward_dsn: str, asset_id: UUID
    ) -> None:
        """The advisory lock's own contribution, isolated.

        Two approvals for *different* proposals of an asset with **no
        incumbent** -- the case a row lock cannot serialise, because there is no
        approved row to lock. Both are released together and run to completion
        on their own connections.

        With the lock, one waits, then sees the winner and refuses with a typed
        `ClassificationConflict`. Without it, both reach the partial unique index
        and the loser surfaces `psycopg.errors.UniqueViolation` -- a database
        error where the product has a decision to report. This test is written
        to fail in exactly that way: an earlier version asserted a
        `lock_timeout` instead, which passed with the lock removed because the
        *index* was doing the waiting.
        """
        record_profile(conn, asset_id, TableProfile(row_count=17), actor=SYSTEM_ACTOR)
        conn.commit()
        older = recorded(conn, asset_id, profile_version=2)
        newer = recorded(conn, asset_id, profile_version=2, prompt="classify@v2")
        assert current_classification(conn, asset_id) is None

        start = threading.Barrier(2)
        failures: list[BaseException] = []
        published: list[UUID] = []

        def decide(proposal_id: UUID) -> None:
            connection = connect(steward_dsn)
            try:
                start.wait(timeout=10)
                record = approve(
                    connection, proposal_id, decision=a_decision(), actor=SYSTEM_ACTOR
                )
                connection.commit()
                published.append(record.id)
            except BaseException as exc:  # noqa: BLE001 -- the type is the assertion
                connection.rollback()
                failures.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=decide, args=(pid,)) for pid in (older, newer)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert len(published) + len(failures) == 2
        for failure in failures:
            assert isinstance(failure, ClassificationConflict), (
                f"the loser got {type(failure).__name__}: a raw database error is not "
                "a decision an API can report"
            )
        approved = [
            record for record in proposal_history(conn, asset_id)
            if record.status is ProposalStatus.APPROVED
        ]
        assert len(approved) == 1, "two decisions both published"

    def test_two_approvals_for_different_proposals_leave_one_current_version(
        self, conn: QueueConnection, other: QueueConnection, asset_id: UUID
    ) -> None:
        """The loser gets a typed conflict, not a `UniqueViolation`. Without the
        advisory lock both would find no incumbent -- there is none on a first
        approval -- and both would promote, leaving the partial unique index to
        surface a database error where the product has a decision to report."""
        first = recorded(conn, asset_id)
        record_profile(conn, asset_id, TableProfile(row_count=16), actor=SYSTEM_ACTOR)
        conn.commit()
        second = recorded(conn, asset_id, profile_version=2)

        approve(conn, second, decision=a_decision(), actor=SYSTEM_ACTOR)
        conn.commit()

        with pytest.raises(ClassificationConflict):
            approve(other, first, decision=a_decision(), actor=SYSTEM_ACTOR)
        other.rollback()

        approved = [r for r in proposal_history(conn, asset_id) if r.status is ProposalStatus.APPROVED]
        assert [r.id for r in approved] == [second]

    def test_approve_versus_reject_produces_one_winner(
        self, conn: QueueConnection, other: QueueConnection, asset_id: UUID
    ) -> None:
        proposal_id = recorded(conn, asset_id)

        approve(conn, proposal_id, decision=a_decision(), actor=SYSTEM_ACTOR)
        conn.commit()

        with pytest.raises(ProposalNotPending):
            reject(other, proposal_id, decision=a_decision(ReviewOutcome.REJECTED), actor=SYSTEM_ACTOR)
        other.rollback()

        current = current_classification(conn, asset_id)
        assert current is not None and current.id == proposal_id
