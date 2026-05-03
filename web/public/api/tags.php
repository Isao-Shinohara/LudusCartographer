<?php

declare(strict_types=1);

/**
 * Tags API — タグ CRUD + ノードタグ操作。
 *
 * 設計書: docs/design/master_node_tags.md §5
 * 詳細計画: docs/design/master_node_tags_phase1.md §4
 *
 * ルーティング:
 *   GET    /api/tags.php?type={operation|scene|sub_scene}&include_deleted=0
 *      → タグ一覧
 *   POST   /api/tags.php
 *      → タグ新規作成
 *   PUT    /api/tags.php?id={id}      (or POST + _method=PUT)
 *      → タグ更新
 *   DELETE /api/tags.php?id={id}      (or POST + _method=DELETE)
 *      → タグ論理削除
 *
 *   GET    /api/tags.php?master_fp={fp}&version_id={vid}
 *      → ノード付与済みタグ一覧 (Step 4)
 *   POST   /api/tags.php?master_fp={fp}&version_id={vid}
 *      → 手動付与 (Step 4)
 *   DELETE /api/tags.php?master_fp={fp}&version_id={vid}&tag_id={tid}
 *      → 手動解除 (Step 4)
 */

require_once __DIR__ . '/_tag_helpers.php';

try {
    $pdo = tag_db();
} catch (\Throwable $e) {
    tag_error('db_error', 500, $e->getMessage());
}

$method   = tag_method();
$tagId    = isset($_GET['id']) ? (int)$_GET['id'] : null;
$masterFp = isset($_GET['master_fp']) ? (string)$_GET['master_fp'] : null;
$versionId = isset($_GET['version_id']) ? (int)$_GET['version_id'] : null;
$nodeMode = $masterFp !== null && $versionId !== null;

try {
    if ($nodeMode) {
        // ノードタグ操作 (Step 4 で実装)
        handle_node_tags($pdo, $method, $masterFp, $versionId);
    } else {
        // タグ CRUD (Step 3)
        handle_tag_crud($pdo, $method, $tagId);
    }
} catch (\PDOException $e) {
    tag_error('db_error', 500, $e->getMessage());
} catch (\Throwable $e) {
    tag_error('internal_error', 500, $e->getMessage());
}


// ─── タグ CRUD (Step 3) ─────────────────────────────

function handle_tag_crud(PDO $pdo, string $method, ?int $tagId): void
{
    if ($method === 'GET') {
        list_tags($pdo);
    } elseif ($method === 'POST') {
        create_tag($pdo);
    } elseif ($method === 'PUT') {
        if ($tagId === null) tag_error('invalid_request', 400, 'id is required');
        update_tag($pdo, $tagId);
    } elseif ($method === 'DELETE') {
        if ($tagId === null) tag_error('invalid_request', 400, 'id is required');
        delete_tag($pdo, $tagId);
    } else {
        tag_error('invalid_request', 405, 'unsupported method: ' . $method);
    }
}

function list_tags(PDO $pdo): void
{
    $type = $_GET['type'] ?? null;
    $includeDeleted = (int)($_GET['include_deleted'] ?? 0) === 1;

    if ($type !== null && !in_array($type, ['operation', 'scene', 'sub_scene'], true)) {
        tag_error('validation_error', 400, 'invalid type');
    }

    $sql = "SELECT t.id, t.code_key, t.name, t.tag_type, t.description,"
        . " t.color, t.sort_order, t.is_system, t.is_deleted,"
        . " t.created_at, t.updated_at,"
        . " COALESCE(c.cnt, 0) AS assigned_count"
        . " FROM lc_tags t"
        . " LEFT JOIN ("
        . "   SELECT tag_id, COUNT(*) AS cnt FROM lc_master_node_tags GROUP BY tag_id"
        . " ) c ON c.tag_id = t.id"
        . " WHERE 1=1";
    $params = [];
    if ($type !== null) {
        $sql .= " AND t.tag_type = :type";
        $params[':type'] = $type;
    }
    if (!$includeDeleted) {
        $sql .= " AND t.is_deleted = 0";
    }
    $sql .= " ORDER BY"
        . " CASE t.tag_type WHEN 'scene' THEN 1 WHEN 'sub_scene' THEN 2 WHEN 'operation' THEN 3 ELSE 9 END,"
        . " t.sort_order, t.id";

    $stmt = $pdo->prepare($sql);
    $stmt->execute($params);
    $tags = $stmt->fetchAll();

    // 型変換: int カラムは int で返す
    foreach ($tags as &$t) {
        $t['id'] = (int)$t['id'];
        $t['sort_order'] = (int)$t['sort_order'];
        $t['is_system'] = (int)$t['is_system'];
        $t['is_deleted'] = (int)$t['is_deleted'];
        $t['assigned_count'] = (int)$t['assigned_count'];
    }
    unset($t);

    tag_ok(['tags' => $tags]);
}

function create_tag(PDO $pdo): void
{
    $body = tag_read_body();
    $name = isset($body['name']) ? trim((string)$body['name']) : '';
    $type = $body['tag_type'] ?? null;
    $description = isset($body['description']) ? (string)$body['description'] : null;
    $color = isset($body['color']) ? (string)$body['color'] : null;
    $sortOrder = isset($body['sort_order']) ? (int)$body['sort_order'] : 0;

    // operation は API 経由で作成不可
    if ($type === 'operation') {
        tag_error('operation_tag_creation_forbidden', 400,
            'operation tags must be defined in code (OperationTag enum)');
    }
    if (!tag_is_valid_user_tag_type($type)) {
        tag_error('validation_error', 400, 'tag_type must be scene or sub_scene');
    }
    if ($name === '' || mb_strlen($name) > 50) {
        tag_error('validation_error', 400, 'name must be 1-50 chars');
    }
    if ($description !== null && mb_strlen($description) > 500) {
        tag_error('validation_error', 400, 'description must be <=500 chars');
    }
    if (!tag_is_valid_color($color)) {
        tag_error('validation_error', 400, 'color must be #RRGGBB');
    }
    if (tag_name_duplicate($pdo, $name, $type)) {
        tag_error('duplicate_name', 400, "name '{$name}' already exists for {$type}");
    }

    $stmt = $pdo->prepare(
        "INSERT INTO lc_tags (name, tag_type, description, color, sort_order, is_system)"
        . " VALUES (:name, :type, :desc, :color, :sort_order, 0)"
    );
    $stmt->execute([
        ':name' => $name,
        ':type' => $type,
        ':desc' => $description,
        ':color' => $color,
        ':sort_order' => $sortOrder,
    ]);
    tag_ok(['id' => (int)$pdo->lastInsertId()]);
}

function update_tag(PDO $pdo, int $tagId): void
{
    $stmt = $pdo->prepare(
        "SELECT id, tag_type, is_system, is_deleted FROM lc_tags WHERE id = :id"
    );
    $stmt->execute([':id' => $tagId]);
    $tag = $stmt->fetch();
    if (!$tag) {
        tag_error('not_found', 404, "tag id {$tagId} not found");
    }
    if ((int)$tag['is_deleted'] === 1) {
        tag_error('not_found', 404, "tag id {$tagId} is deleted");
    }
    if ((int)$tag['is_system'] === 1) {
        tag_error('system_tag_modification_forbidden', 403,
            'is_system=1 tag (operation category) cannot be edited');
    }

    $body = tag_read_body();
    $name = isset($body['name']) ? trim((string)$body['name']) : null;
    $description = array_key_exists('description', $body) ? (string)$body['description'] : null;
    $color = array_key_exists('color', $body) ? (string)$body['color'] : null;
    $sortOrder = isset($body['sort_order']) ? (int)$body['sort_order'] : null;

    if ($name !== null && ($name === '' || mb_strlen($name) > 50)) {
        tag_error('validation_error', 400, 'name must be 1-50 chars');
    }
    if ($description !== null && mb_strlen($description) > 500) {
        tag_error('validation_error', 400, 'description must be <=500 chars');
    }
    if ($color !== null && !tag_is_valid_color($color)) {
        tag_error('validation_error', 400, 'color must be #RRGGBB');
    }
    if ($name !== null && tag_name_duplicate($pdo, $name, $tag['tag_type'], $tagId)) {
        tag_error('duplicate_name', 400, "name '{$name}' already exists for {$tag['tag_type']}");
    }

    // 部分更新: 渡されたフィールドのみ更新
    $sets = [];
    $params = [':id' => $tagId];
    if ($name !== null) { $sets[] = 'name = :name'; $params[':name'] = $name; }
    if (array_key_exists('description', $body)) { $sets[] = 'description = :desc'; $params[':desc'] = $description; }
    if (array_key_exists('color', $body)) { $sets[] = 'color = :color'; $params[':color'] = $color; }
    if ($sortOrder !== null) { $sets[] = 'sort_order = :sort_order'; $params[':sort_order'] = $sortOrder; }

    if (!$sets) {
        tag_ok(); // no-op
    }
    $sets[] = "updated_at = datetime('now')";

    $sql = "UPDATE lc_tags SET " . implode(', ', $sets)
        . " WHERE id = :id AND is_system = 0 AND is_deleted = 0";
    $stmt = $pdo->prepare($sql);
    $stmt->execute($params);
    tag_ok();
}

function delete_tag(PDO $pdo, int $tagId): void
{
    $stmt = $pdo->prepare(
        "SELECT id, is_system, is_deleted FROM lc_tags WHERE id = :id"
    );
    $stmt->execute([':id' => $tagId]);
    $tag = $stmt->fetch();
    if (!$tag) {
        tag_error('not_found', 404, "tag id {$tagId} not found");
    }
    if ((int)$tag['is_deleted'] === 1) {
        tag_ok(['affected_assignments' => 0]); // 既に削除済み (idempotent)
    }
    if ((int)$tag['is_system'] === 1) {
        tag_error('system_tag_modification_forbidden', 403,
            'is_system=1 tag (operation category) cannot be deleted');
    }

    $cntStmt = $pdo->prepare(
        "SELECT COUNT(*) FROM lc_master_node_tags WHERE tag_id = :id"
    );
    $cntStmt->execute([':id' => $tagId]);
    $affected = (int)$cntStmt->fetchColumn();

    $upd = $pdo->prepare(
        "UPDATE lc_tags SET is_deleted = 1, updated_at = datetime('now')"
        . " WHERE id = :id AND is_system = 0 AND is_deleted = 0"
    );
    $upd->execute([':id' => $tagId]);

    tag_ok(['affected_assignments' => $affected]);
}


// ─── ノードタグ操作 (Step 4) ───────────────────────

function handle_node_tags(PDO $pdo, string $method, string $masterFp, int $versionId): void
{
    if ($method === 'GET') {
        list_node_tags($pdo, $masterFp, $versionId);
    } elseif ($method === 'POST') {
        assign_node_tag($pdo, $masterFp, $versionId);
    } elseif ($method === 'DELETE') {
        $tagId = isset($_GET['tag_id']) ? (int)$_GET['tag_id'] : null;
        if ($tagId === null) tag_error('invalid_request', 400, 'tag_id is required');
        unassign_node_tag($pdo, $masterFp, $versionId, $tagId);
    } else {
        tag_error('invalid_request', 405, 'unsupported method: ' . $method);
    }
}

function list_node_tags(PDO $pdo, string $masterFp, int $versionId): void
{
    $stmt = $pdo->prepare(
        "SELECT t.id, t.name, t.tag_type, t.color, t.is_system,"
        . " mnt.assigned_by, mnt.confidence, mnt.assigned_at"
        . " FROM lc_master_node_tags mnt"
        . " JOIN lc_tags t ON t.id = mnt.tag_id"
        . " WHERE mnt.master_fp = :fp AND mnt.version_id = :vid"
        . "   AND t.is_deleted = 0"
        . " ORDER BY"
        . "   CASE t.tag_type WHEN 'scene' THEN 1 WHEN 'sub_scene' THEN 2 ELSE 3 END,"
        . "   t.sort_order, t.id"
    );
    $stmt->execute([':fp' => $masterFp, ':vid' => $versionId]);
    $tags = $stmt->fetchAll();
    foreach ($tags as &$t) {
        $t['id'] = (int)$t['id'];
        $t['is_system'] = (int)$t['is_system'];
        $t['confidence'] = $t['confidence'] !== null ? (float)$t['confidence'] : null;
    }
    unset($t);
    tag_ok(['tags' => $tags]);
}

function assign_node_tag(PDO $pdo, string $masterFp, int $versionId): void
{
    $body = tag_read_body();
    $tagId = isset($body['tag_id']) ? (int)$body['tag_id'] : null;
    if ($tagId === null) {
        tag_error('invalid_request', 400, 'tag_id is required');
    }

    // タグ存在 + 種別取得
    $stmt = $pdo->prepare(
        "SELECT id, tag_type, is_deleted FROM lc_tags WHERE id = :id"
    );
    $stmt->execute([':id' => $tagId]);
    $tag = $stmt->fetch();
    if (!$tag || (int)$tag['is_deleted'] === 1) {
        tag_error('not_found', 404, "tag id {$tagId} not found or deleted");
    }

    $pdo->beginTransaction();
    try {
        // シーンタグの「1 個必須」制約: 既存シーンタグを置換
        if ($tag['tag_type'] === 'scene') {
            replace_scene_tag($pdo, $masterFp, $versionId, $tagId);
        }
        // 付与 (UNIQUE 違反は no-op)
        $ins = $pdo->prepare(
            "INSERT OR IGNORE INTO lc_master_node_tags"
            . " (master_fp, version_id, tag_id, assigned_by, confidence, assigned_at)"
            . " VALUES (:fp, :vid, :tid, 'manual', 1.0, datetime('now'))"
        );
        $ins->execute([':fp' => $masterFp, ':vid' => $versionId, ':tid' => $tagId]);
        $pdo->commit();
    } catch (\Throwable $e) {
        $pdo->rollBack();
        throw $e;
    }
    tag_ok();
}

/**
 * シーンタグ置換: 既存シーンタグを物理削除 + 履歴記録 (manual_scene_replaced)。
 */
function replace_scene_tag(PDO $pdo, string $masterFp, int $versionId, int $newTagId): void
{
    $stmt = $pdo->prepare(
        "SELECT mnt.id, mnt.tag_id FROM lc_master_node_tags mnt"
        . " JOIN lc_tags t ON t.id = mnt.tag_id"
        . " WHERE mnt.master_fp = :fp AND mnt.version_id = :vid"
        . "   AND t.tag_type = 'scene'"
    );
    $stmt->execute([':fp' => $masterFp, ':vid' => $versionId]);
    $existing = $stmt->fetchAll();
    if (!$existing) return;

    // 履歴記録
    $oldIds = array_map(fn($r) => (int)$r['tag_id'], $existing);
    if (!in_array($newTagId, $oldIds, true)) {
        $hist = $pdo->prepare(
            "INSERT INTO lc_master_node_tag_history"
            . " (master_fp, version_id, event_type, old_tag_ids, new_tag_ids)"
            . " VALUES (:fp, :vid, 'manual_scene_replaced', :old, :new)"
        );
        $hist->execute([
            ':fp' => $masterFp,
            ':vid' => $versionId,
            ':old' => json_encode($oldIds, JSON_UNESCAPED_UNICODE),
            ':new' => json_encode([$newTagId], JSON_UNESCAPED_UNICODE),
        ]);
    }

    // 既存削除
    $del = $pdo->prepare(
        "DELETE FROM lc_master_node_tags"
        . " WHERE id IN ("
        . "   SELECT mnt.id FROM lc_master_node_tags mnt"
        . "   JOIN lc_tags t ON t.id = mnt.tag_id"
        . "   WHERE mnt.master_fp = :fp AND mnt.version_id = :vid"
        . "     AND t.tag_type = 'scene'"
        . " )"
    );
    $del->execute([':fp' => $masterFp, ':vid' => $versionId]);
}

function unassign_node_tag(PDO $pdo, string $masterFp, int $versionId, int $tagId): void
{
    $stmt = $pdo->prepare(
        "SELECT mnt.id, mnt.tag_id, mnt.assigned_by, t.is_system"
        . " FROM lc_master_node_tags mnt"
        . " JOIN lc_tags t ON t.id = mnt.tag_id"
        . " WHERE mnt.master_fp = :fp AND mnt.version_id = :vid AND mnt.tag_id = :tid"
    );
    $stmt->execute([':fp' => $masterFp, ':vid' => $versionId, ':tid' => $tagId]);
    $rec = $stmt->fetch();
    if (!$rec) {
        tag_error('not_found', 404, 'assignment not found');
    }
    if ((int)$rec['is_system'] === 1) {
        tag_error('system_tag_modification_forbidden', 403,
            'is_system=1 tag (operation category) cannot be unassigned');
    }

    $pdo->beginTransaction();
    try {
        $hist = $pdo->prepare(
            "INSERT INTO lc_master_node_tag_history"
            . " (master_fp, version_id, event_type, old_tag_ids)"
            . " VALUES (:fp, :vid, 'manual_unassigned', :old)"
        );
        $hist->execute([
            ':fp' => $masterFp,
            ':vid' => $versionId,
            ':old' => json_encode([$tagId], JSON_UNESCAPED_UNICODE),
        ]);
        $del = $pdo->prepare(
            "DELETE FROM lc_master_node_tags"
            . " WHERE master_fp = :fp AND version_id = :vid AND tag_id = :tid"
        );
        $del->execute([':fp' => $masterFp, ':vid' => $versionId, ':tid' => $tagId]);
        $pdo->commit();
    } catch (\Throwable $e) {
        $pdo->rollBack();
        throw $e;
    }
    tag_ok();
}
