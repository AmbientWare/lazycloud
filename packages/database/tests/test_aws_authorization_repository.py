from uuid import uuid4

from database.repositories.aws_connections import AwsAccountConnectionRepository
from database.repositories.identity import UserRepository
from shared.aws_connections import (
    AwsAccountAuthorizationGeneration,
    AwsAccountAuthorizationMode,
    AwsAccountAuthorizationPhase,
    AwsAccountConnection,
    AwsAccountConnectionPhase,
)
from shared.timestamps import utc_now

from database import DatabaseClient


def test_authorization_promotion_preserves_the_predecessor_until_retired(
    database: DatabaseClient,
) -> None:
    now = utc_now()
    active = AwsAccountAuthorizationGeneration(
        id=str(uuid4()),
        generation=1,
        role_arn="arn:aws:iam::123456789012:role/generation-one",
        authorization_mode=AwsAccountAuthorizationMode.ExistingRole,
        phase=AwsAccountAuthorizationPhase.Ready,
        last_validated_at=now,
        created_at=now,
        updated_at=now,
    )
    pending = active.model_copy(
        update={
            "id": str(uuid4()),
            "generation": 2,
            "role_arn": "arn:aws:iam::123456789012:role/generation-two",
        }
    )
    with database.session() as session:
        user = UserRepository(session).create()
        connection = AwsAccountConnectionRepository(session).create(
            AwsAccountConnection(
                id=str(uuid4()),
                user_id=user.id,
                account_id="123456789012",
                external_id="synthetic-" + str(uuid4()),
                phase=AwsAccountConnectionPhase.ReconnectPending,
                active_authorization=active,
                pending_authorization=pending,
                created_at=now,
                updated_at=now,
            )
        )
    with database.session() as session:
        repository = AwsAccountConnectionRepository(session)
        current = repository.get(connection.id, for_update=True)
        assert current is not None
        repository.save(
            current.model_copy(
                update={
                    "phase": AwsAccountConnectionPhase.RetiringAuthorization,
                    "active_authorization": pending,
                    "pending_authorization": None,
                    "retiring_authorization": active.model_copy(
                        update={
                            "phase": AwsAccountAuthorizationPhase.Retiring,
                        }
                    ),
                    "provider_operation_id": "authorization-retirement-test",
                    "provider_operation_started_at": now,
                }
            )
        )
    with database.session() as session:
        repository = AwsAccountConnectionRepository(session)
        promoted = repository.get(connection.id, for_update=True)
        assert promoted is not None
        assert promoted.active_authorization == pending
        assert promoted.pending_authorization is None
        assert promoted.retiring_authorization is not None
        assert promoted.retiring_authorization.id == active.id
        repository.save(
            promoted.model_copy(
                update={
                    "phase": AwsAccountConnectionPhase.Degraded,
                    "retiring_authorization": None,
                    "provider_operation_id": None,
                    "provider_operation_started_at": None,
                }
            )
        )
    with database.session() as session:
        settled = AwsAccountConnectionRepository(session).get(connection.id)
        assert settled is not None
        assert settled.active_authorization == pending
        assert settled.retiring_authorization is None
