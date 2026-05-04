<?php

declare(strict_types=1);

/**
 * Tagging Run API — Gemini 判定実行 + 進捗ポーリング (Phase 3 / Phase 4)
 *
 * 設計書: docs/design/master_node_tags.md §5.2.2
 * 詳細計画: docs/design/master_node_tags_phase1.md §11 (P3 スコープ)
 *
 * ルーティング (action パラメータで分岐):
 *   POST /api/tagging.php?action=run   body: {tag_type, mode, reset_manual, version_id}
 *      → Python サブプロセスを起動して即時 return
 *   GET  /api/tagging.php?action=progress
 *      → auto_pilot_state.tagging_progress を返す
 *   GET  /api/tagging.php?action=estimate&tag_type=&mode=&reset_manual=&version_id=
 *      → 確認モーダル用の対象件数 / 推定トークン
 */

require_once __DIR__ . '/_tag_helpers.php';

$action = $_GET['action'] ?? 'run';
$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';

try {
    $pdo = tag_db();
} catch (\Throwable $e) {
    tag_error('db_error', 500, $e->getMessage());
}

ensure_state_table($pdo);

if ($action === 'estimate') {
    handle_estimate($pdo);
} elseif ($action === 'progress') {
    handle_progress($pdo);
} elseif ($action === 'run') {
    if ($method !== 'POST') {
        tag_error('invalid_request', 405, 'POST required');
    }
    handle_run($pdo);
} else {
    tag_error('invalid_request', 400, 'unknown action: ' . $action);
}


function ensure_state_table(PDO $pdo): void
{
    $pdo->exec("CREATE TABLE IF NOT EXISTS auto_pilot_state ("
        . " key TEXT PRIMARY KEY, value TEXT,"
        . " updated_at TEXT DEFAULT (datetime('now'))"
        . ")");
}


function handle_estimate(PDO $pdo): void
{
    $tagType = $_GET['tag_type'] ?? 'scene';
    $mode = $_GET['mode'] ?? 'unassigned';
    $resetManual = ((int)($_GET['reset_manual'] ?? 0)) === 1;
    $versionId = (int)($_GET['version_id'] ?? 1);

    if (!in_array($tagType, ['scene', 'sub_scene'], true)) {
        tag_error('validation_error', 400, 'tag_type must be scene or sub_scene');
    }

    $args = [
        '--type', $tagType,
        '--mode', $mode,
        '--version-id', (string)$versionId,
        '--dry-run',
    ];
    if ($resetManual) $args[] = '--reset-manual';

    $stdout = run_python_module('tools.tag_judgment', $args);
    $payload = parse_json_or_error($stdout);
    if (!($payload['ok'] ?? false)) {
        tag_error('estimate_failed', 500,
            $payload['message'] ?? json_encode($payload, JSON_UNESCAPED_UNICODE));
    }

    // dry_run のレスポンスには target だけが入っているので、estimate_targets() の値も取得
    $args2 = [
        '--type', $tagType,
        '--mode', $mode,
        '--version-id', (string)$versionId,
        '--estimate',
    ];
    // 簡易: estimate_targets を呼び出すための --estimate フラグは未実装。
    // 代替として、Python 側は dry-run で {target_count, candidate_count, prompt_hash}
    // を返すので、ここでは target_count を主軸に手動算出する。

    // estimate_targets で取得したい値を Python サブプロセスから直接得る:
    $stdout2 = run_python_inline(
        "import sys, json\n" .
        "sys.path.insert(0, 'crawler')\n" .
        "from pathlib import Path\n" .
        "from tools.tag_judgment import estimate_targets\n" .
        "db = Path('crawler/storage/ludus.db')\n" .
        "print(json.dumps(estimate_targets(db, " .
        json_encode($tagType) . ", " .
        json_encode($mode) . ", " .
        ($resetManual ? "True" : "False") . ", " .
        (int)$versionId .
        ")))\n"
    );
    $est = parse_json_or_error($stdout2);
    tag_ok(['estimate' => $est]);
}


function handle_run(PDO $pdo): void
{
    $body = tag_read_body();
    $tagType = $body['tag_type'] ?? 'scene';
    $mode = $body['mode'] ?? 'unassigned';
    $resetManual = (bool)($body['reset_manual'] ?? false);
    $versionId = (int)($body['version_id'] ?? 1);

    if (!in_array($tagType, ['scene', 'sub_scene'], true)) {
        tag_error('validation_error', 400, 'tag_type must be scene or sub_scene');
    }
    if (!in_array($mode, ['unassigned', 'all'], true)) {
        tag_error('validation_error', 400, 'mode must be unassigned or all');
    }

    // 二重起動チェック
    $stmt = $pdo->prepare(
        "SELECT value FROM auto_pilot_state WHERE key = 'tagging_progress'"
    );
    $stmt->execute();
    $row = $stmt->fetch();
    if ($row && $row['value']) {
        $cur = json_decode($row['value'], true);
        if (is_array($cur) && ($cur['running'] ?? false)) {
            tag_error('already_running', 409, 'a tagging job is already running');
        }
    }

    // Python サブプロセスを fire-and-forget で起動
    $args = [
        '--type', $tagType,
        '--mode', $mode,
        '--version-id', (string)$versionId,
    ];
    if ($resetManual) $args[] = '--reset-manual';

    spawn_python_module_async('tools.tag_judgment', $args);

    // 即時に「started」状態を progress に書き込む
    $pdo->prepare(
        "INSERT OR REPLACE INTO auto_pilot_state (key, value, updated_at)"
        . " VALUES ('tagging_progress', ?, datetime('now'))"
    )->execute([
        json_encode([
            'running' => true,
            'phase' => 'queued',
            'tag_type' => $tagType,
            'mode' => $mode,
            'started_at' => time(),
        ], JSON_UNESCAPED_UNICODE),
    ]);

    tag_ok([
        'tag_type' => $tagType,
        'mode' => $mode,
        'reset_manual' => $resetManual,
        'started_at' => time(),
    ]);
}


function handle_progress(PDO $pdo): void
{
    $stmt = $pdo->prepare(
        "SELECT value, updated_at FROM auto_pilot_state WHERE key = 'tagging_progress'"
    );
    $stmt->execute();
    $row = $stmt->fetch();
    if (!$row) {
        tag_ok(['running' => false, 'phase' => 'idle']);
    }
    $payload = json_decode($row['value'], true);
    if (!is_array($payload)) {
        tag_ok(['running' => false, 'phase' => 'idle']);
    }
    tag_ok($payload);
}


// ─── Python subprocess ヘルパ ─────────────────────────


function project_root(): string
{
    $r = realpath(__DIR__ . '/../../..');
    if ($r === false) throw new \RuntimeException('project root not found');
    return $r;
}

function python_bin(): string
{
    $venv = project_root() . '/crawler/venv/bin/python';
    return is_executable($venv) ? $venv : 'python3';
}

function run_python_module(string $module, array $args): string
{
    // -m tools.* は cwd = crawler/ で実行する
    $cwd = project_root() . '/crawler';
    $cmd = escapeshellarg(python_bin()) . ' -m ' . escapeshellarg($module);
    foreach ($args as $a) {
        $cmd .= ' ' . escapeshellarg($a);
    }
    $cmd = "cd " . escapeshellarg($cwd) . " && " . $cmd . " 2>&1";
    return shell_exec($cmd) ?? '';
}

function run_python_inline(string $code): string
{
    // インラインスクリプトは sys.path に 'crawler' を追加して project root から起動
    $cwd = project_root();
    $cmd = escapeshellarg(python_bin()) . ' -c ' . escapeshellarg($code);
    $cmd = "cd " . escapeshellarg($cwd) . " && " . $cmd . " 2>&1";
    return shell_exec($cmd) ?? '';
}

function spawn_python_module_async(string $module, array $args): void
{
    $cwd = project_root() . '/crawler';
    $cmd = escapeshellarg(python_bin()) . ' -m ' . escapeshellarg($module);
    foreach ($args as $a) {
        $cmd .= ' ' . escapeshellarg($a);
    }
    $logFile = sys_get_temp_dir() . '/lc_tagging_' . uniqid() . '.log';
    $full = "cd " . escapeshellarg($cwd) . " && " . $cmd . " > " . escapeshellarg($logFile) . " 2>&1 &";
    exec("nohup sh -c " . escapeshellarg($full) . " > /dev/null 2>&1 &");
}

function parse_json_or_error(string $stdout): array
{
    // stdout の最後の JSON ブロックを抽出 (ログ混在ケース)
    $stdout = trim($stdout);
    if ($stdout === '') {
        tag_error('subprocess_empty_output', 500, 'Python subprocess returned empty');
    }
    // JSON 候補: 最初の { から最後の } まで
    $first = strpos($stdout, '{');
    $last = strrpos($stdout, '}');
    if ($first === false || $last === false || $last < $first) {
        tag_error('subprocess_invalid_output', 500,
            'no JSON in subprocess output: ' . substr($stdout, 0, 200));
    }
    $json = substr($stdout, $first, $last - $first + 1);
    $decoded = json_decode($json, true);
    if (!is_array($decoded)) {
        tag_error('subprocess_invalid_json', 500,
            'json_decode failed: ' . substr($json, 0, 200));
    }
    return $decoded;
}
