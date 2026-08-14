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
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from psycopg import IsolationLevel
from psycopg.rows import dict_row
from steward_catalog import EnvSecretResolver, build_scan_source, postgres_inspector, register_source
from steward_catalog import _classification_sql as _sql
from steward_catalog.classification import (
    AssetNotClassifiable,
    ClassificationConflict,
    EvidenceNotResolvable,
    IdempotencyKeyReused,
    ProposalNotPending,
    ReadCommittedDetail,
    StaleProposal,
    _lock_by_proposal,
    _proposal,
    approve,
    current_classification,
    get_proposal,
    proposal_detail,
    proposal_history,
    propose,
    record_proposal_reviews,
    reject,
)
from steward_catalog.profiles import record_profile
from steward_queue import (
    SYSTEM_ACTOR,
    Actor,
    ActorKind,
    QueueConnection,
    TaskContext,
    UsageLedger,
    connect,
)
from steward_schemas import (
    ClassificationProposal,
    ColumnClassification,
    ColumnProfile,
    EvidenceKind,
    EvidenceRef,
    MaskedSample,
    ProposalStatus,
    ReviewCommand,
    ReviewOutcome,
    SemanticType,
    SensitivityLabel,
    SourceCreate,
    TableProfile,
    TaskSpec,
    TaskStatus,
    ValueFrequency,
)

MASKED_EMAIL = "j***@g***.***"

EXPECTED_PROPOSAL_COLUMNS = (
    "id", "asset_id", "version", "profile_version", "prompt_version", "model_alias",
    "status", "proposal", "run_id", "task_id", "trace_id", "created_at",
)
"""The projection every proposal-reading statement selects, in its usual order.

Written out here rather than imported so that a change to the module's
statements has to be made deliberately in both places; the tests below assert
the decoder does not care about this order, not that it matches.
"""

REVERSED_PROPOSAL_PROJECTION = """
SELECT created_at, trace_id, task_id, run_id, proposal, status, model_alias,
       prompt_version, profile_version, version, asset_id, id
FROM classification_proposals
WHERE id = %(id)s
"""

PROPOSAL_PROJECTION_WITHOUT_STATUS = """
SELECT id, asset_id, version, profile_version, prompt_version, model_alias,
       proposal, run_id, task_id, trace_id, created_at
FROM classification_proposals
WHERE id = %(id)s
"""

SELECT_ASSET_ID = (
    "SELECT id FROM assets WHERE schema_name = %(schema)s AND name = %(name)s"
)


def a_profile(row_count: int) -> TableProfile:
    """A profile that actually contains the column the proposals cite.

    The first version of this fixture recorded `a_profile(10)` with
    no columns at all, and every test passed -- because nothing resolved a
    citation against the stored profile. It does now, so the fixture has to be
    the real shape.
    """
    return TableProfile(
        row_count=row_count,
        columns=(
            ColumnProfile(
                name="email",
                data_type="text",
                null_count=0,
                null_ratio=Decimal("0"),
                distinct_count=row_count,
                distinct_ratio=Decimal("1"),
                top_values=(
                    ValueFrequency(
                        value=MaskedSample(masked=MASKED_EMAIL, semantic_type=SemanticType.EMAIL),
                        count=row_count,
                    ),
                ),
            ),
        ),
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
    record_profile(conn, identifier, a_profile(10), actor=SYSTEM_ACTOR)
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
                        locator="email",
                        detail="the column is named 'email'",
                    ),
                ),
            ),
        ),
    )


def a_command(policy_id: str | None = None) -> ReviewCommand:
    """What a reviewer supplies: a reason, and at most a policy. Outcome, actor
    and time are the repository's."""
    return ReviewCommand(reason="looks right", policy_id=policy_id)


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
def second_asset_id(conn: QueueConnection, asset_id: UUID) -> UUID:
    """A second profiled asset, so two decisions hold different asset locks."""
    row = conn.execute(SELECT_ASSET_ID, {"schema": "sales", "name": "orders"}).fetchone()
    assert row is not None, "the fixture estate has no second table to classify"
    identifier: UUID = row[0]
    record_profile(conn, identifier, a_profile(7), actor=SYSTEM_ACTOR)
    conn.commit()
    return identifier


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

        published = approve(conn, proposal_id, command=a_command(), actor=SYSTEM_ACTOR)
        conn.commit()

        assert published.status is ProposalStatus.APPROVED
        current = current_classification(conn, asset_id)
        assert current is not None and current.id == proposal_id
        reviews = record_proposal_reviews(conn, proposal_id)
        assert [r.outcome for r in reviews] == [ReviewOutcome.APPROVED]
        # The same author as the audit row: attribution is the trusted actor's,
        # not something the caller passed in.
        assert reviews[0].actor == SYSTEM_ACTOR

    def test_approving_a_replacement_supersedes_in_one_action(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        """The operator approves the replacement; they do not also demote the
        incumbent. Two actions would leave an asset with no classification at
        all in between -- visible to every reader, and permanent if the second
        never happened."""
        first = recorded(conn, asset_id)
        approve(conn, first, command=a_command(), actor=SYSTEM_ACTOR)
        conn.commit()

        record_profile(conn, asset_id, a_profile(11), actor=SYSTEM_ACTOR)
        conn.commit()
        second = recorded(conn, asset_id, profile_version=2)

        approve(conn, second, command=a_command(), actor=SYSTEM_ACTOR)
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
        approve(conn, first, command=a_command(), actor=SYSTEM_ACTOR)
        conn.commit()

        record_profile(conn, asset_id, a_profile(12), actor=SYSTEM_ACTOR)
        conn.commit()
        second = recorded(conn, asset_id, profile_version=2)

        approve(conn, second, command=a_command(), actor=SYSTEM_ACTOR)
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
        record_profile(conn, asset_id, a_profile(13), actor=SYSTEM_ACTOR)
        conn.commit()
        second = recorded(conn, asset_id, profile_version=2)
        approve(conn, second, command=a_command(), actor=SYSTEM_ACTOR)
        conn.commit()

        with pytest.raises(StaleProposal):
            approve(conn, first, command=a_command(), actor=SYSTEM_ACTOR)
        conn.rollback()

        current = current_classification(conn, asset_id)
        assert current is not None and current.id == second

    def test_a_proposal_whose_profile_moved_on_is_stale(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        proposal_id = recorded(conn, asset_id)
        record_profile(conn, asset_id, a_profile(99), actor=SYSTEM_ACTOR)
        conn.commit()

        with pytest.raises(StaleProposal, match="data it describes has changed"):
            approve(conn, proposal_id, command=a_command(), actor=SYSTEM_ACTOR)


class TestIdempotency:
    def test_a_repeated_approval_under_one_key_returns_the_original(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        proposal_id = recorded(conn, asset_id)
        first = approve(conn, proposal_id, command=a_command(), idempotency_key="k1", actor=SYSTEM_ACTOR)
        conn.commit()

        again = approve(conn, proposal_id, command=a_command(), idempotency_key="k1", actor=SYSTEM_ACTOR)
        conn.commit()

        assert again.id == first.id and again.status is ProposalStatus.APPROVED
        # One decision, not two: a replay is not a second event.
        assert len(record_proposal_reviews(conn, proposal_id)) == 1


class TestIdempotencyKeyMisuse:
    """A key identifies one request, not one caller.

    Approve and reject are opposite governance actions sharing one key index.
    Before this was checked, replaying a *reject* under an *approve*'s key
    returned the approved record and left the rejected proposal untouched -- the
    caller was told their rejection succeeded while nothing had happened to the
    proposal they named.
    """

    def test_a_key_cannot_settle_a_different_proposal(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        record_profile(conn, asset_id, a_profile(21), actor=SYSTEM_ACTOR)
        conn.commit()
        settled = recorded(conn, asset_id, profile_version=2)
        untouched = recorded(conn, asset_id, profile_version=2, prompt="classify@v2")

        approve(conn, settled, command=a_command(), idempotency_key="shared", actor=SYSTEM_ACTOR)
        conn.commit()

        # Same key, different proposal: a replay of someone else's request.
        with pytest.raises(IdempotencyKeyReused):
            approve(
                conn, untouched, command=a_command(), idempotency_key="shared", actor=SYSTEM_ACTOR
            )
        conn.rollback()

        history = {record.id: record.status for record in proposal_history(conn, asset_id)}
        assert history[untouched] is ProposalStatus.PENDING_REVIEW

    def test_a_reject_cannot_replay_under_an_approvals_key(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        proposal_id = recorded(conn, asset_id)
        approve(conn, proposal_id, command=a_command(), idempotency_key="k", actor=SYSTEM_ACTOR)
        conn.commit()

        with pytest.raises(IdempotencyKeyReused):
            reject(
                conn,
                proposal_id,
                command=a_command(),
                idempotency_key="k",
                actor=SYSTEM_ACTOR,
            )
        conn.rollback()

        current = current_classification(conn, asset_id)
        assert current is not None and current.id == proposal_id


class TestUnpublishableInputs:
    def test_an_inactive_asset_cannot_publish(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        """The half of `_require_classifiable` nothing exercised: deleting the
        lifecycle check left all 11 tests passing."""
        proposal_id = recorded(conn, asset_id)
        conn.execute("UPDATE assets SET lifecycle = 'missing' WHERE id = %s", (asset_id,))
        conn.commit()

        with pytest.raises(AssetNotClassifiable):
            approve(conn, proposal_id, command=a_command(), actor=SYSTEM_ACTOR)
        conn.rollback()
        assert current_classification(conn, asset_id) is None

    def test_evidence_citing_a_column_the_profile_lacks_is_refused(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        """The type checks a citation against its own column; only the stored
        profile knows whether that column ever existed."""
        invented = ClassificationProposal(
            asset_id=asset_id,
            profile_version=1,
            prompt_version="classify@v1",
            model_alias="steward-classify",
            columns=(
                ColumnClassification(
                    column_name="not_a_real_column",
                    labels=(SensitivityLabel.PII,),
                    confidence=Decimal("0.9"),
                    evidence=(
                        EvidenceRef(
                            profile_version=1,
                            column_name="not_a_real_column",
                            kind=EvidenceKind.COLUMN_NAME,
                            locator="not_a_real_column",
                            detail="looks sensitive",
                        ),
                    ),
                ),
            ),
        )
        with pytest.raises(EvidenceNotResolvable):
            propose(
                conn,
                invented,
                run_id=uuid4(),
                task_id=uuid4(),
                trace_id="trace-test",
                actor=SYSTEM_ACTOR,
            )
        conn.rollback()


    def test_a_masked_sample_the_profile_never_recorded_is_refused(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        """The gap "the column exists" left open: a real column, an invented
        sample. `detail` is prose and resolves to nothing, so before locators
        this passed."""
        invented = ClassificationProposal(
            asset_id=asset_id,
            profile_version=1,
            prompt_version="classify@v1",
            model_alias="steward-classify",
            columns=(
                ColumnClassification(
                    column_name="email",
                    labels=(SensitivityLabel.PII,),
                    confidence=Decimal("0.9"),
                    evidence=(
                        EvidenceRef(
                            profile_version=1,
                            column_name="email",
                            kind=EvidenceKind.MASKED_SAMPLE,
                            # The profile records `j***@g***.***` and nothing
                            # else, so this names a sample that was never taken.
                            locator="4***-****-****-1234",
                            detail="a card-shaped sample",
                        ),
                    ),
                ),
            ),
        )
        with pytest.raises(EvidenceNotResolvable, match="MASKED_SAMPLE|masked_sample"):
            propose(
                conn,
                invented,
                run_id=uuid4(),
                task_id=uuid4(),
                trace_id="trace-test",
                actor=SYSTEM_ACTOR,
            )
        conn.rollback()


class TestRejection:
    def test_rejecting_leaves_the_published_version_alone(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        first = recorded(conn, asset_id)
        approve(conn, first, command=a_command(), actor=SYSTEM_ACTOR)
        conn.commit()

        record_profile(conn, asset_id, a_profile(14), actor=SYSTEM_ACTOR)
        conn.commit()
        second = recorded(conn, asset_id, profile_version=2)
        reject(conn, second, command=a_command(), actor=SYSTEM_ACTOR)
        conn.commit()

        current = current_classification(conn, asset_id)
        assert current is not None and current.id == first
        history = {record.id: record.status for record in proposal_history(conn, asset_id)}
        assert history[second] is ProposalStatus.REJECTED

    def test_a_decided_proposal_cannot_be_decided_again(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        proposal_id = recorded(conn, asset_id)
        reject(conn, proposal_id, command=a_command(), actor=SYSTEM_ACTOR)
        conn.commit()

        with pytest.raises(ProposalNotPending):
            approve(conn, proposal_id, command=a_command(), actor=SYSTEM_ACTOR)


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
        record_profile(conn, asset_id, a_profile(17), actor=SYSTEM_ACTOR)
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
                    connection, proposal_id, command=a_command(), actor=SYSTEM_ACTOR
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
        record_profile(conn, asset_id, a_profile(16), actor=SYSTEM_ACTOR)
        conn.commit()
        second = recorded(conn, asset_id, profile_version=2)

        approve(conn, second, command=a_command(), actor=SYSTEM_ACTOR)
        conn.commit()

        with pytest.raises(ClassificationConflict):
            approve(other, first, command=a_command(), actor=SYSTEM_ACTOR)
        other.rollback()

        approved = [r for r in proposal_history(conn, asset_id) if r.status is ProposalStatus.APPROVED]
        assert [r.id for r in approved] == [second]

    def test_approve_versus_reject_produces_one_winner(
        self, conn: QueueConnection, other: QueueConnection, asset_id: UUID
    ) -> None:
        proposal_id = recorded(conn, asset_id)

        approve(conn, proposal_id, command=a_command(), actor=SYSTEM_ACTOR)
        conn.commit()

        with pytest.raises(ProposalNotPending):
            reject(other, proposal_id, command=a_command(), actor=SYSTEM_ACTOR)
        other.rollback()

        current = current_classification(conn, asset_id)
        assert current is not None and current.id == proposal_id


class TestAttribution:
    """The review table and the audit log must name the same author (I7)."""

    def test_a_human_cannot_record_a_policy_approval(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        """SPEC §3.3 allows auto-approval only through a configured policy, and
        requires it to be auditable back to that policy. A person supplying a
        policy id would be claiming a policy approved something."""
        proposal_id = recorded(conn, asset_id)
        human = Actor(kind=ActorKind.HUMAN, id="alice")

        with pytest.raises(ClassificationConflict, match="policy id"):
            approve(
                conn,
                proposal_id,
                command=a_command(policy_id="auto-approve-low-risk"),
                actor=human,
            )
        conn.rollback()

    def test_a_policy_actor_may_record_one(self, conn: QueueConnection, asset_id: UUID) -> None:
        proposal_id = recorded(conn, asset_id)
        policy = Actor(kind=ActorKind.POLICY, id="auto-approve-low-risk")

        approve(conn, proposal_id, command=a_command(policy_id=policy.id), actor=policy)
        conn.commit()

        review = record_proposal_reviews(conn, proposal_id)[0]
        assert review.actor == policy
        assert review.policy_id == "auto-approve-low-risk"

    def test_the_recorded_time_is_the_databases(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        """No caller-supplied timestamp exists to be ignored: the command has no
        such field, and the column defaults to `now()`."""
        proposal_id = recorded(conn, asset_id)
        approve(conn, proposal_id, command=a_command(), actor=SYSTEM_ACTOR)
        conn.commit()

        review = record_proposal_reviews(conn, proposal_id)[0]
        assert review.decided_at is not None
        assert not hasattr(a_command(), "decided_at")
        assert not hasattr(a_command(), "outcome")
        assert not hasattr(a_command(), "actor")


class TestConcurrentKeyReuse:
    """One key, two assets, two connections.

    Different assets hold *different* advisory locks, so nothing serialises
    these two decisions until they reach the reviews table's key index. Before
    `_record_decision` checked its own insert, both would see no existing key,
    both would proceed, one insert would silently do nothing, and two proposals
    would change status on the strength of a single review event.
    """

    def test_one_key_cannot_settle_decisions_on_two_assets(
        self, conn: QueueConnection, steward_dsn: str, asset_id: UUID, second_asset_id: UUID
    ) -> None:
        first = recorded(conn, asset_id)
        second = recorded(conn, second_asset_id)

        start = threading.Barrier(2)
        outcomes: list[tuple[UUID, BaseException | None]] = []

        def decide(proposal_id: UUID) -> None:
            connection = connect(steward_dsn)
            try:
                start.wait(timeout=10)
                approve(
                    connection,
                    proposal_id,
                    command=a_command(),
                    idempotency_key="one-key",
                    actor=SYSTEM_ACTOR,
                )
                connection.commit()
                outcomes.append((proposal_id, None))
            except BaseException as exc:  # noqa: BLE001 -- the type is the assertion
                connection.rollback()
                outcomes.append((proposal_id, exc))
            finally:
                connection.close()

        threads = [threading.Thread(target=decide, args=(pid,)) for pid in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert len(outcomes) == 2
        settled = [pid for pid, exc in outcomes if exc is None]
        refused = [exc for _, exc in outcomes if exc is not None]
        assert len(settled) == 1, "one review event settled two governance actions"
        for failure in refused:
            assert isinstance(failure, IdempotencyKeyReused), (
                f"the loser got {type(failure).__name__} rather than a typed conflict"
            )

        # Exactly one proposal moved; the other is untouched.
        statuses = {
            record.id: record.status
            for asset in (asset_id, second_asset_id)
            for record in proposal_history(conn, asset)
        }
        assert sorted(statuses[pid].value for pid in (first, second)) == [
            "approved",
            "pending_review",
        ]


class TestEvidenceResolvesPositively:
    """The path the negative tests could not see.

    "Rejects a sample the profile never recorded" passed while the
    implementation rejected *every* sample: the locator set was built from
    `str(MaskedSample(...))`, which is the model's repr, not the masked value.
    Only citing a **real** sample can tell those two apart.
    """

    def test_a_citation_of_a_recorded_masked_sample_is_accepted(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        cited = ClassificationProposal(
            asset_id=asset_id,
            profile_version=1,
            prompt_version="classify@v1",
            model_alias="steward-classify",
            columns=(
                ColumnClassification(
                    column_name="email",
                    labels=(SensitivityLabel.PII,),
                    confidence=Decimal("0.97"),
                    evidence=(
                        EvidenceRef(
                            profile_version=1,
                            column_name="email",
                            kind=EvidenceKind.MASKED_SAMPLE,
                            locator=MASKED_EMAIL,
                            detail="the sampled values are email-shaped",
                        ),
                    ),
                ),
            ),
        )
        record = propose(
            conn, cited, run_id=uuid4(), task_id=uuid4(), trace_id="t", actor=SYSTEM_ACTOR
        )
        conn.commit()
        assert record.status is ProposalStatus.PENDING_REVIEW

    def test_every_kind_of_locator_resolves_against_the_stored_profile(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        """One citation of each kind, all naming what the fixture profile holds."""
        kinds = (
            (EvidenceKind.COLUMN_NAME, "email"),
            (EvidenceKind.DATA_TYPE, "text"),
            (EvidenceKind.NULL_RATIO, "0"),
            (EvidenceKind.DISTINCT_RATIO, "1"),
            (EvidenceKind.SEMANTIC_TYPE, SemanticType.UNKNOWN.value),
            (EvidenceKind.MASKED_SAMPLE, MASKED_EMAIL),
        )
        every_kind = ClassificationProposal(
            asset_id=asset_id,
            profile_version=1,
            prompt_version="classify@every-kind",
            model_alias="steward-classify",
            columns=(
                ColumnClassification(
                    column_name="email",
                    labels=(SensitivityLabel.PII,),
                    confidence=Decimal("0.99"),
                    evidence=tuple(
                        EvidenceRef(
                            profile_version=1,
                            column_name="email",
                            kind=kind,
                            locator=locator,
                            detail=f"cited as {kind.value}",
                        )
                        for kind, locator in kinds
                    ),
                ),
            ),
        )
        record = propose(
            conn, every_kind, run_id=uuid4(), task_id=uuid4(), trace_id="t", actor=SYSTEM_ACTOR
        )
        conn.commit()
        assert len(record.proposal.columns[0].evidence) == len(kinds)


class TestKeyIdentifiesTheWholeCommand:
    """A key names a governance command, not a target and a verb.

    Comparing only proposal and outcome let one caller's key carry another's
    decision: alice approves P with reason A under key K; bob approves P with
    reason B under K and is told his review succeeded, though only alice's was
    ever recorded.
    """

    def test_a_different_actor_under_the_same_key_is_refused(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        proposal_id = recorded(conn, asset_id)
        alice = Actor(kind=ActorKind.HUMAN, id="alice")
        bob = Actor(kind=ActorKind.HUMAN, id="bob")
        approve(conn, proposal_id, command=a_command(), idempotency_key="k", actor=alice)
        conn.commit()

        with pytest.raises(IdempotencyKeyReused):
            approve(conn, proposal_id, command=a_command(), idempotency_key="k", actor=bob)
        conn.rollback()

        assert record_proposal_reviews(conn, proposal_id)[0].actor == alice

    def test_a_different_reason_under_the_same_key_is_refused(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        proposal_id = recorded(conn, asset_id)
        approve(conn, proposal_id, command=a_command(), idempotency_key="k", actor=SYSTEM_ACTOR)
        conn.commit()

        with pytest.raises(IdempotencyKeyReused):
            approve(
                conn,
                proposal_id,
                command=ReviewCommand(reason="a different justification entirely"),
                idempotency_key="k",
                actor=SYSTEM_ACTOR,
            )
        conn.rollback()
        assert len(record_proposal_reviews(conn, proposal_id)) == 1

    def test_a_different_policy_under_the_same_key_is_refused(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        proposal_id = recorded(conn, asset_id)
        first = Actor(kind=ActorKind.POLICY, id="policy-a")
        approve(
            conn, proposal_id, command=a_command(policy_id="policy-a"), idempotency_key="k", actor=first
        )
        conn.commit()

        second = Actor(kind=ActorKind.POLICY, id="policy-b")
        with pytest.raises(IdempotencyKeyReused):
            approve(
                conn,
                proposal_id,
                command=a_command(policy_id="policy-b"),
                idempotency_key="k",
                actor=second,
            )
        conn.rollback()
        assert record_proposal_reviews(conn, proposal_id)[0].policy_id == "policy-a"

    def test_the_identical_command_still_replays(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        """Scoping must not break the case idempotency exists for."""
        proposal_id = recorded(conn, asset_id)
        first = approve(
            conn, proposal_id, command=a_command(), idempotency_key="k", actor=SYSTEM_ACTOR
        )
        conn.commit()
        again = approve(
            conn, proposal_id, command=a_command(), idempotency_key="k", actor=SYSTEM_ACTOR
        )
        conn.commit()

        assert again.id == first.id and again.status is ProposalStatus.APPROVED
        assert len(record_proposal_reviews(conn, proposal_id)) == 1


class TestPolicyAttribution:
    """An automatic approval resolves to the policy that made it — exactly."""

    def test_a_policy_actor_without_a_policy_id_is_refused(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        proposal_id = recorded(conn, asset_id)
        with pytest.raises(ClassificationConflict, match="no policy id"):
            approve(
                conn,
                proposal_id,
                command=a_command(),
                actor=Actor(kind=ActorKind.POLICY, id="auto"),
            )
        conn.rollback()

    def test_a_policy_cannot_attribute_to_another_policy(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        proposal_id = recorded(conn, asset_id)
        with pytest.raises(ClassificationConflict, match="other than the one making it"):
            approve(
                conn,
                proposal_id,
                command=a_command(policy_id="policy-b"),
                actor=Actor(kind=ActorKind.POLICY, id="policy-a"),
            )
        conn.rollback()


class TestReading:
    """The read path a reviewer's GET takes. Its property is what it does *not* do."""

    def test_a_proposal_reads_back_by_id(self, conn: QueueConnection, asset_id: UUID) -> None:
        proposal_id = recorded(conn, asset_id)

        record = get_proposal(conn, proposal_id)

        assert record is not None
        assert (record.id, record.asset_id, record.version) == (proposal_id, asset_id, 1)
        assert record.status is ProposalStatus.PENDING_REVIEW
        assert [column.column_name for column in record.proposal.columns] == ["email"]

    def test_an_unknown_id_reads_as_none_rather_than_raising(self, conn: QueueConnection) -> None:
        assert get_proposal(conn, uuid4()) is None

    def test_reading_does_not_wait_on_a_decision_in_progress(
        self, conn: QueueConnection, other: QueueConnection, asset_id: UUID
    ) -> None:
        """A GET must not queue behind whoever is deciding.

        `approve` on `conn` holds the asset's advisory lock and the proposal row
        under `FOR UPDATE`, uncommitted, for the length of this block. A plain
        read is unaffected by both; the locking read `approve` itself uses would
        block until that transaction ends -- which is why serving a GET through
        it would hold a reader for the length of someone else's decision.

        The timeout is what makes this an assertion rather than a hang: swap
        `SELECT_PROPOSAL` for `SELECT_PROPOSAL_FOR_UPDATE` and this fails in two
        seconds instead of passing.
        """
        proposal_id = recorded(conn, asset_id)
        approve(conn, proposal_id, command=a_command(), actor=SYSTEM_ACTOR)  # left uncommitted

        other.execute("SET LOCAL statement_timeout = '2s'")
        record = get_proposal(other, proposal_id)

        assert record is not None
        # The uncommitted approval is invisible, as it must be: this reader is
        # in its own transaction and the decision has not landed yet.
        assert record.status is ProposalStatus.PENDING_REVIEW
        conn.rollback()


class TestDecoding:
    """Rows are decoded by column name, so a projection cannot drift silently.

    The same twelve columns are written out in seven separate statements in
    `_classification_sql`, because S608 (the check enforcing I5) cannot tell a
    column list composed from a module constant from a query composed from user
    input -- factoring it out would cost a `noqa` on each and disable the
    SQL-safety gate for the file. So the duplication stays, and the decoder is
    what stops it being dangerous.
    """

    def test_the_same_row_decodes_the_same_under_a_different_column_order(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        """A reordered projection of one row decodes to an identical record.

        This is the property the seven duplicated column lists depend on. Under
        positional decoding it does not hold: `status` would be read out of
        whichever column happens to sit seventh, and the record would come back
        describing a proposal nobody wrote.
        """
        proposal_id = recorded(conn, asset_id)
        [expected] = proposal_history(conn, asset_id)

        reordered = conn.cursor(row_factory=dict_row).execute(
            REVERSED_PROPOSAL_PROJECTION, {"id": proposal_id}
        ).fetchone()
        assert reordered is not None
        assert list(reordered) != list(EXPECTED_PROPOSAL_COLUMNS), "the projection was not reordered"

        assert _proposal(reordered) == expected

    def test_a_projection_missing_a_column_fails_loudly(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        """The drift this guards against, made to happen.

        A statement that forgets `status` is exactly the mistake seven copies
        invite. By name it is a `KeyError` naming the column; by position it was
        a silent shift of every field after it.
        """
        proposal_id = recorded(conn, asset_id)

        row = conn.cursor(row_factory=dict_row).execute(
            PROPOSAL_PROJECTION_WITHOUT_STATUS, {"id": proposal_id}
        ).fetchone()
        assert row is not None

        with pytest.raises(KeyError, match="status"):
            _proposal(row)


class TestDetailConsistency:
    """A proposal and its reviews must come from one moment, not two.

    `proposal_detail` runs two statements. Under PostgreSQL's default READ
    COMMITTED each takes its own snapshot, so a decision committing between them
    yields a reply that says `pending_review` and carries an `approved` review —
    each query correct, the pair incoherent. These tests are about the pair.
    """

    def test_a_decision_committing_mid_read_cannot_split_the_answer(
        self, conn: QueueConnection, other: QueueConnection, asset_id: UUID
    ) -> None:
        """The interleaving, performed rather than imagined.

        `conn` opens a repeatable-read transaction and takes its snapshot. `other`
        then approves and commits. `conn` reads the reviews. Both halves must
        describe the world *before* the decision — that is what "one snapshot"
        means, and it is the whole fix.
        """
        proposal_id = recorded(conn, asset_id)
        conn.commit()
        conn.isolation_level = IsolationLevel.REPEATABLE_READ

        # First statement of the transaction: this is where the snapshot is taken.
        before = get_proposal(conn, proposal_id)
        assert before is not None and before.status is ProposalStatus.PENDING_REVIEW

        approve(other, proposal_id, command=a_command(), actor=SYSTEM_ACTOR)
        other.commit()

        reviews = record_proposal_reviews(conn, proposal_id)
        still_pending = get_proposal(conn, proposal_id)

        assert reviews == (), "the decision leaked into a snapshot taken before it"
        assert still_pending is not None
        assert still_pending.status is ProposalStatus.PENDING_REVIEW
        conn.rollback()

        # And a transaction opened afterwards sees all of it — otherwise the
        # assertions above would also pass against a reader that sees nothing.
        after = proposal_detail(conn, proposal_id)
        assert after is not None
        decided, decisions = after
        assert decided.status is ProposalStatus.APPROVED
        assert [review.outcome for review in decisions] == [ReviewOutcome.APPROVED]
        conn.rollback()

    def test_read_committed_really_does_split_it(
        self, conn: QueueConnection, other: QueueConnection, asset_id: UUID
    ) -> None:
        """The defect, reproduced — so the guard above is not guarding a myth.

        The same interleaving under the default isolation level produces exactly
        the contradiction: a proposal read as `pending_review` beside the
        approval that has already happened.
        """
        proposal_id = recorded(conn, asset_id)
        conn.commit()

        before = get_proposal(conn, proposal_id)
        assert before is not None and before.status is ProposalStatus.PENDING_REVIEW

        approve(other, proposal_id, command=a_command(), actor=SYSTEM_ACTOR)
        other.commit()

        reviews = record_proposal_reviews(conn, proposal_id)

        assert [review.outcome for review in reviews] == [ReviewOutcome.APPROVED]
        assert before.status is ProposalStatus.PENDING_REVIEW  # the pair disagrees
        conn.rollback()

    def test_the_detail_read_refuses_a_transaction_that_cannot_answer_it(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        """The guard, on the pathology rather than on a convention.

        A caller that forgets the isolation level gets a refusal, not a subtly
        wrong answer under load. Documenting the requirement instead would leave
        it true only while everyone remembers.
        """
        proposal_id = recorded(conn, asset_id)

        with pytest.raises(ReadCommittedDetail, match="read committed"):
            proposal_detail(conn, proposal_id)
        conn.rollback()

    def test_the_detail_read_returns_both_halves(
        self, conn: QueueConnection, asset_id: UUID
    ) -> None:
        """The positive case beside the refusals: it does return the thing."""
        proposal_id = recorded(conn, asset_id)
        approve(conn, proposal_id, command=a_command(), actor=SYSTEM_ACTOR)
        conn.commit()
        conn.isolation_level = IsolationLevel.REPEATABLE_READ

        detail = proposal_detail(conn, proposal_id)

        assert detail is not None
        record, reviews = detail
        assert record.id == proposal_id
        assert record.status is ProposalStatus.APPROVED
        assert [review.reason for review in reviews] == ["looks right"]
        assert proposal_detail(conn, uuid4()) is None
        conn.rollback()


class TestLockOrder:
    """Decisions take this module's locks in one order: asset first, row second.

    `_lock_by_proposal` reads a proposal only to learn which asset to lock. If
    that read takes a row lock, the order inverts — a decision would hold a row
    another decision needs while waiting for the advisory lock that decision
    holds — and Postgres resolves the cycle by aborting one with
    `DeadlockDetected`. That is an `OperationalError` no caller catches, so it
    reaches an API client as a 500: a raw database error in exactly the place
    this module's advisory lock exists to produce a typed conflict instead.
    """

    def test_finding_the_asset_does_not_take_a_row_lock(
        self, conn: QueueConnection, other: QueueConnection, asset_id: UUID
    ) -> None:
        """The property, as a bounded wait rather than a deadlock.

        `other` holds the proposal row under `FOR UPDATE`. A lock-free read gets
        past it and goes on to take the (uncontended) advisory lock; a
        `FOR UPDATE` read blocks there and, under the timeout, fails. So this
        passes in two seconds or fails in two — it cannot hang the suite, which
        is the constraint `TestConcurrency` states.
        """
        proposal_id = recorded(conn, asset_id)
        other.execute(_sql.SELECT_PROPOSAL_FOR_UPDATE, {"id": proposal_id})  # held, uncommitted

        conn.execute("SET LOCAL statement_timeout = '2s'")
        _lock_by_proposal(conn, proposal_id)  # must not wait on the row lock

        conn.rollback()
        other.rollback()
