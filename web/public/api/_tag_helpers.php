<?php

declare(strict_types=1);

/**
 * タグ機能 API 共通ヘルパ。
 *
 * 設計書: docs/design/master_node_tags.md §5
 * 詳細計画: docs/design/master_node_tags_phase1.md §4
 */

require_once __DIR__ . '/../../vendor/autoload.php';

use LudusCartographer\Database;

/**
 * SQLite 接続を取得する。
 * タグ機能は SQLite DB (crawler/storage/ludus.db) のみで動作する。
 */
function tag_db(): PDO
{
    static $pdo = null;
    if ($pdo === null) {
        $pdo = Database::getSqliteConnection();
    }
    return $pdo;
}

/**
 * JSON レスポンスを返して exit する。
 */
function tag_json(array $payload, int $status = 200): never
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
    exit;
}

function tag_ok(array $extra = []): never
{
    tag_json(array_merge(['ok' => true], $extra));
}

function tag_error(string $error, int $status = 400, ?string $message = null): never
{
    tag_json([
        'ok'      => false,
        'error'   => $error,
        'message' => $message,
    ], $status);
}

/**
 * JSON リクエストボディを取得する (PUT/DELETE/POST 共通)。
 */
function tag_read_body(): array
{
    $raw = file_get_contents('php://input');
    if ($raw === false || $raw === '') {
        return [];
    }
    try {
        $decoded = json_decode($raw, true, flags: JSON_THROW_ON_ERROR);
    } catch (\JsonException) {
        tag_error('invalid_request', 400, 'JSON parse error');
    }
    return is_array($decoded) ? $decoded : [];
}

/**
 * #RRGGBB 形式のチェック。
 */
function tag_is_valid_color(?string $color): bool
{
    if ($color === null || $color === '') return true; // optional
    return preg_match('/^#[0-9A-Fa-f]{6}$/', $color) === 1;
}

/**
 * tag_type のチェック。operation は API 経由で作成不可。
 */
function tag_is_valid_user_tag_type(?string $type): bool
{
    return in_array($type, ['scene', 'sub_scene'], true);
}

/**
 * 既存 active タグで (name, tag_type) が重複していないか。
 */
function tag_name_duplicate(PDO $pdo, string $name, string $type, ?int $excludeId = null): bool
{
    $sql = "SELECT 1 FROM lc_tags"
        . " WHERE name = :name AND tag_type = :type AND is_deleted = 0";
    $params = [':name' => $name, ':type' => $type];
    if ($excludeId !== null) {
        $sql .= " AND id != :id";
        $params[':id'] = $excludeId;
    }
    $stmt = $pdo->prepare($sql);
    $stmt->execute($params);
    return (bool)$stmt->fetchColumn();
}

/**
 * リクエストの HTTP メソッド (大文字)。_method 上書きにも対応。
 */
function tag_method(): string
{
    $method = strtoupper($_SERVER['REQUEST_METHOD'] ?? 'GET');
    $override = strtoupper($_GET['_method'] ?? '');
    if ($override !== '' && in_array($override, ['PUT', 'DELETE'], true)) {
        return $override;
    }
    return $method;
}
