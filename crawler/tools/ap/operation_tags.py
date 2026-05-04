"""操縦カテゴリの定義と DB 同期。

CLAUDE.md §21 ルール 1 (操縦カテゴリの追加) に従い、ID は再利用禁止。
廃止する場合は ``_DEPRECATED`` コメントで残し、新 ID で再定義する。

設計書: docs/design/master_node_tags.md §7
"""
from __future__ import annotations

import logging
import sqlite3
from enum import IntEnum

logger = logging.getLogger(__name__)


class OperationTag(IntEnum):
    """操縦カテゴリ ID。

    CLAUDE.md §21 ルール 1 厳格:
    - 既存 ID は変更禁止・削除禁止 (reserved)
    - 廃止する場合は _DEPRECATED コメントで残し、ID は再利用禁止
    """

    TUTORIAL = 1


# 表示名 (Tag タブで表示される)
OPERATION_TAG_NAMES: dict[OperationTag, str] = {
    OperationTag.TUTORIAL: "チュートリアル",
}

# auto_pilot --operation 引数で指定する code_key
OPERATION_TAG_CODE_KEYS: dict[OperationTag, str] = {
    OperationTag.TUTORIAL: "tutorial",
}


def resolve_operation_code_key(code_key: str) -> OperationTag:
    """code_key から OperationTag を逆引きする。

    未登録なら SystemExit (auto_pilot 起動を拒否する目的)。
    """
    for tag, key in OPERATION_TAG_CODE_KEYS.items():
        if key == code_key:
            return tag
    valid = ", ".join(OPERATION_TAG_CODE_KEYS.values())
    raise SystemExit(
        f"[OPERATION] 未登録の操縦カテゴリ: {code_key!r}\n"
        f"  有効な値: {valid}"
    )


def upsert_operation_tag(conn: sqlite3.Connection, op: OperationTag) -> int:
    """OperationTag を ``lc_tags`` に upsert し、tag_id を返す。

    - 既存レコードがあれば name を最新コード値に同期する。
    - 既存がなければ ``is_system=1`` で新規 INSERT。
    - 呼び出し元が commit する責務 (本関数は commit しない)。
    """
    code_key = OPERATION_TAG_CODE_KEYS[op]
    name = OPERATION_TAG_NAMES[op]

    cur = conn.execute(
        "SELECT id, name FROM lc_tags"
        " WHERE code_key = ? AND tag_type = 'operation' AND is_deleted = 0",
        (code_key,),
    )
    row = cur.fetchone()

    if row is None:
        cur = conn.execute(
            "INSERT INTO lc_tags (code_key, name, tag_type, is_system)"
            " VALUES (?, ?, 'operation', 1)",
            (code_key, name),
        )
        tag_id = cur.lastrowid
        logger.info("[OPERATION] タグ新規登録: %s (id=%d)", name, tag_id)
        return int(tag_id)

    existing_id = int(row[0])
    existing_name = row[1]
    if existing_name != name:
        conn.execute(
            "UPDATE lc_tags SET name = ?, updated_at = datetime('now')"
            " WHERE id = ?",
            (name, existing_id),
        )
        logger.info("[OPERATION] タグ名を同期: %s → %s", existing_name, name)
    return existing_id
