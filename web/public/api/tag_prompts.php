<?php

declare(strict_types=1);

/**
 * Tag Prompts API — プロンプト編集 + テスト判定 + デフォルト復帰
 *
 * 設計書: docs/design/master_node_tags.md §5.2.3
 * 詳細計画: docs/design/master_node_tags_phase1.md §11 (P3 スコープ)
 *
 * ルーティング:
 *   GET  /api/tag_prompts.php?type=scene
 *      → 現在のプロンプト + デフォルト
 *   PUT  /api/tag_prompts.php?type=scene  body: {prompt_text}
 *      → プロンプト保存 (is_default=0)
 *   POST /api/tag_prompts.php?type=scene&action=test  body: {prompt_text, sample_size, version_id}
 *      → 5 件サンプルテスト判定 (DB 書き込みなし)
 *   POST /api/tag_prompts.php?type=scene&action=reset
 *      → コード側のデフォルトに戻す
 */

require_once __DIR__ . '/_tag_helpers.php';

try {
    $pdo = tag_db();
} catch (\Throwable $e) {
    tag_error('db_error', 500, $e->getMessage());
}

$method = tag_method();
$type = $_GET['type'] ?? null;
$action = $_GET['action'] ?? null;

if (!in_array($type, ['scene', 'sub_scene'], true)) {
    tag_error('validation_error', 400, 'type must be scene or sub_scene');
}

if ($method === 'GET') {
    handle_get($pdo, $type);
} elseif ($method === 'PUT') {
    handle_put($pdo, $type);
} elseif ($method === 'POST' && $action === 'test') {
    handle_test($pdo, $type);
} elseif ($method === 'POST' && $action === 'reset') {
    handle_reset($pdo, $type);
} else {
    tag_error('invalid_request', 405, 'unsupported method/action');
}


function handle_get(PDO $pdo, string $type): void
{
    $stmt = $pdo->prepare(
        "SELECT prompt_text, is_default, updated_at"
        . " FROM lc_tag_prompts WHERE tag_type = ?"
    );
    $stmt->execute([$type]);
    $row = $stmt->fetch();

    // 初回はデフォルトを Python 側から取得して INSERT (lc_tag_prompts に未挿入の場合)
    $defaultText = read_default_prompt($type);

    if (!$row) {
        $pdo->prepare(
            "INSERT INTO lc_tag_prompts (tag_type, prompt_text, is_default)"
            . " VALUES (?, ?, 1)"
        )->execute([$type, $defaultText]);
        $current = ['prompt_text' => $defaultText, 'is_default' => 1, 'updated_at' => null];
    } else {
        $current = [
            'prompt_text' => $row['prompt_text'],
            'is_default' => (int)$row['is_default'],
            'updated_at' => $row['updated_at'],
        ];
    }

    tag_ok([
        'tag_type' => $type,
        'current' => $current,
        'default' => ['prompt_text' => $defaultText],
        'placeholders' => ['{tag_candidates}', '{detected_scene}', '{ocr_text}'],
    ]);
}


function handle_put(PDO $pdo, string $type): void
{
    $body = tag_read_body();
    $promptText = isset($body['prompt_text']) ? (string)$body['prompt_text'] : '';
    if (trim($promptText) === '') {
        tag_error('validation_error', 400, 'prompt_text is required');
    }
    if (mb_strlen($promptText) > 8000) {
        tag_error('validation_error', 400, 'prompt_text too long (max 8000 chars)');
    }

    $stmt = $pdo->prepare(
        "INSERT INTO lc_tag_prompts (tag_type, prompt_text, is_default, updated_at)"
        . " VALUES (?, ?, 0, datetime('now'))"
        . " ON CONFLICT(tag_type) DO UPDATE SET"
        . "   prompt_text = excluded.prompt_text,"
        . "   is_default = 0,"
        . "   updated_at = datetime('now')"
    );
    $stmt->execute([$type, $promptText]);

    // キャッシュ無効化見積もり (新 prompt_hash で hit しなくなる件数を返す)
    $cnt = (int)$pdo->query(
        "SELECT COUNT(*) FROM lc_master_nodes WHERE version_id = "
        . (int)($_GET['version_id'] ?? 1)
    )->fetchColumn();
    $message = $cnt > 0
        ? "プロンプト変更により次回タグ付け実行時に最大 {$cnt} 件が再判定されます"
        : "プロンプトを保存しました";
    tag_ok([
        'cache_invalidated_estimate' => $cnt,
        'warning' => $message,
    ]);
}


function handle_test(PDO $pdo, string $type): void
{
    $body = tag_read_body();
    $promptText = isset($body['prompt_text']) ? (string)$body['prompt_text'] : '';
    $sampleSize = (int)($body['sample_size'] ?? 5);
    $versionId = (int)($body['version_id'] ?? 1);
    if (trim($promptText) === '') {
        tag_error('validation_error', 400, 'prompt_text is required');
    }
    if ($sampleSize < 1 || $sampleSize > 20) {
        tag_error('validation_error', 400, 'sample_size must be 1-20');
    }

    // インラインで Python を呼んで JSON を取得
    $code = "import sys, json\n"
        . "sys.path.insert(0, 'crawler')\n"
        . "from pathlib import Path\n"
        . "from tools.tag_judgment import test_prompt_with_samples\n"
        . "db = Path('crawler/storage/ludus.db')\n"
        . "result = test_prompt_with_samples(db, " . json_encode($type) . ","
        . " " . php_str_to_python($promptText) . ", "
        . (int)$sampleSize . ", " . (int)$versionId . ")\n"
        . "print(json.dumps(result, ensure_ascii=False))\n";
    $stdout = run_python_inline_local($code);
    $payload = parse_json_local($stdout);
    if (!($payload['ok'] ?? false)) {
        tag_error('test_failed', 500,
            $payload['error'] ?? json_encode($payload, JSON_UNESCAPED_UNICODE));
    }
    tag_ok([
        'samples' => $payload['samples'] ?? [],
        'duration_seconds' => $payload['duration_seconds'] ?? 0,
    ]);
}


function handle_reset(PDO $pdo, string $type): void
{
    $defaultText = read_default_prompt($type);
    $stmt = $pdo->prepare(
        "INSERT INTO lc_tag_prompts (tag_type, prompt_text, is_default, updated_at)"
        . " VALUES (?, ?, 1, datetime('now'))"
        . " ON CONFLICT(tag_type) DO UPDATE SET"
        . "   prompt_text = excluded.prompt_text,"
        . "   is_default = 1,"
        . "   updated_at = datetime('now')"
    );
    $stmt->execute([$type, $defaultText]);
    tag_ok();
}


function read_default_prompt(string $type): string
{
    $code = "import sys, json\n"
        . "sys.path.insert(0, 'crawler')\n"
        . "from tools.tag_judgment import DEFAULT_PROMPTS\n"
        . "print(json.dumps({'text': DEFAULT_PROMPTS[" . json_encode($type) . "]}, ensure_ascii=False))\n";
    $stdout = run_python_inline_local($code);
    $payload = parse_json_local($stdout);
    return (string)($payload['text'] ?? '');
}


function project_root_local(): string
{
    $r = realpath(__DIR__ . '/../../..');
    if ($r === false) throw new \RuntimeException('project root not found');
    return $r;
}

function python_bin_local(): string
{
    $venv = project_root_local() . '/crawler/venv/bin/python';
    return is_executable($venv) ? $venv : 'python3';
}

function run_python_inline_local(string $code): string
{
    $cwd = project_root_local();
    $cmd = escapeshellarg(python_bin_local()) . ' -c ' . escapeshellarg($code);
    $cmd = "cd " . escapeshellarg($cwd) . " && " . $cmd . " 2>&1";
    return shell_exec($cmd) ?? '';
}

function parse_json_local(string $stdout): array
{
    $stdout = trim($stdout);
    $first = strpos($stdout, '{');
    $last = strrpos($stdout, '}');
    if ($first === false || $last === false || $last < $first) {
        tag_error('subprocess_invalid_output', 500,
            'no JSON: ' . substr($stdout, 0, 200));
    }
    $decoded = json_decode(substr($stdout, $first, $last - $first + 1), true);
    if (!is_array($decoded)) {
        tag_error('subprocess_invalid_json', 500, 'json_decode failed');
    }
    return $decoded;
}

function php_str_to_python(string $s): string
{
    // PHP 文字列を Python リテラルとして安全に埋め込む
    return 'json.loads(' . json_encode(json_encode($s, JSON_UNESCAPED_UNICODE)) . ')';
}
