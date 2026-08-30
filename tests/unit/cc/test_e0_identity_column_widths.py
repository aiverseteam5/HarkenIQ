"""Identity columns must fit a real identity.

Found on the live stack during E0.1: `cc_approval_policies.created_by`
was String(32) while a Keycloak subject is a 36-character UUID, so
creating an approval policy raised StringDataRightTruncation on
PostgreSQL and succeeded on the sqlite used in tests. Approval policies
were not merely unenforced -- they were uncreatable on a real deployment.

sqlite ignores VARCHAR length, so no insert-based test can catch this
class of bug. This is a static invariant over the model metadata, which
runs everywhere and catches the next one before a deployment does.

Second instance of the shape after QA-040 (bytes into Text killed
RegisterAgent on postgres and passed on sqlite).
"""

from __future__ import annotations

import pytest
from sqlalchemy import String

from harkeniq_cc.db.models import Base as CCBase

#: A Keycloak subject is a 36-character UUID and an email can be 320.
#: 128 admits every subject and most addresses; columns that store an
#: address specifically are checked against the larger bound.
MIN_PRINCIPAL_WIDTH = 128
MIN_EMAIL_WIDTH = 320

#: Column names that hold a principal identity somewhere in the platform.
PRINCIPAL_COLUMNS = {
    "actor", "approver_ref", "changed_by", "created_by", "decided_by",
    "granted_by", "issued_by", "principal_ref", "updated_by",
    "activated_by", "resolved_by",
}
EMAIL_COLUMNS = {"approver_email", "user_email", "actor_email", "email"}


def _string_columns(base):
    for table in base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, String) and column.type.length:
                yield table.name, column.name, column.type.length


@pytest.mark.parametrize("base,label", [(CCBase, "central_command")])
def test_principal_columns_fit_a_keycloak_subject(base, label):
    too_narrow = [
        f"{table}.{col} is String({length}), needs >= {MIN_PRINCIPAL_WIDTH}"
        for table, col, length in _string_columns(base)
        if col in PRINCIPAL_COLUMNS and length < MIN_PRINCIPAL_WIDTH
    ]
    assert not too_narrow, (
        f"{label}: identity columns too narrow for a Keycloak subject "
        f"(36-char UUID). sqlite will not catch this; PostgreSQL will, "
        f"in production:\n  " + "\n  ".join(too_narrow)
    )


@pytest.mark.parametrize("base,label", [(CCBase, "central_command")])
def test_email_columns_fit_an_address(base, label):
    too_narrow = [
        f"{table}.{col} is String({length}), needs >= {MIN_EMAIL_WIDTH}"
        for table, col, length in _string_columns(base)
        if col in EMAIL_COLUMNS and length < MIN_EMAIL_WIDTH
    ]
    assert not too_narrow, (
        f"{label}: email columns too narrow:\n  " + "\n  ".join(too_narrow)
    )
