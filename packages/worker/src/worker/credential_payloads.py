from __future__ import annotations

from shared.contracts import ContractModel
from shared.identity import TokenKind

WORKER_TOKEN_KINDS = frozenset({TokenKind.Worker, TokenKind.WorkerPrivate})


class WorkerCredentialPrincipal(ContractModel):
    workspace_id: str
    token_kind: TokenKind = TokenKind.Worker
    token_id: str = ""

    @property
    def is_worker(self) -> bool:
        return self.token_kind in WORKER_TOKEN_KINDS
