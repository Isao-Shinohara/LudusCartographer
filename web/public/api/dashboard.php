<?php

declare(strict_types=1);

// ダッシュボード API ルーター。
// `?action=xxx` を見て該当 handlers/*.php を require する。
// 共通初期化 (env / DB connection / GEMINI_ENABLED) は _common.php に配置。

require_once __DIR__ . '/_common.php';

// action → handler ファイル名マップ
$ACTION_HANDLERS = [
    // system
    'heartbeat'                 => 'system',
    // versions
    'get_versions'              => 'versions',
    'create_version'            => 'versions',
    'activate_version'          => 'versions',
    'rename_version'            => 'versions',
    'finalize_session'          => 'versions',
    'delete_version'            => 'versions',
    'get_active_version'        => 'versions',
    // games
    'get_games'                 => 'games',
    'get_coverage'              => 'games',
    // sessions
    'get_sessions'              => 'sessions',
    'get_pending_merges'        => 'sessions',
    'process_session_bg'        => 'sessions',
    'build_session_graph'       => 'sessions',
    'delete_session'            => 'sessions',
    // screens
    'get_project_screens'       => 'screens',
    'get_recent_screens'        => 'screens',
    'get_final_screens'         => 'screens',
    'get_final_screens_all'     => 'screens',
    'get_cluster_siblings'      => 'screens',
    'promote_representative'    => 'screens',
    'toggle_exclude'            => 'screens',
    'adopt_and_rebuild'         => 'screens',
    'check_screen_master'       => 'screens',
    'toggle_screen_artifact'    => 'screens',
    'update_manual_text'        => 'screens',
    'get_cleanable_excluded'    => 'screens',
    'cleanup_excluded'          => 'screens',
    // merge
    'merge_progress'            => 'merge',
    'preview_merge'             => 'merge',
    'execute_merge'             => 'merge',
    'can_unmerge'               => 'merge',
    'execute_unmerge'           => 'merge',
    'merge_manual_group'        => 'merge',
    'unmerge_manual_group'      => 'merge',
    'get_manual_group_members'  => 'merge',
    // ocr
    'get_noise_words'           => 'ocr',
    'delete_noise_word'         => 'ocr',
    'add_noise_word'            => 'ocr',
    'get_correction_candidates' => 'ocr',
    'apply_correction_rule'     => 'ocr',
    // api_usage
    'get_api_usage'             => 'api_usage',
    // graph
    'get_graph'                 => 'graph',
];

$handlerName = $ACTION_HANDLERS[$action] ?? null;
if ($handlerName !== null) {
    $handlerFile = __DIR__ . "/handlers/{$handlerName}.php";
    if (is_file($handlerFile)) {
        require $handlerFile;
        // 各ハンドラーは action マッチで exit; する想定。ここに来たらフォールスルー。
    }
}

// --- 未対応 action ---
http_response_code(400);
echo json_encode(
    ['error' => 'unknown action', 'action' => $action],
    JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR,
);
