<?php

declare(strict_types=1);

// dashboard.php (ルーター) から require される。共通変数
// ($pdo / $repository / $useDb / $action / $gameTitle /
//  $versionParam / $GEMINI_ENABLED) は _common.php で初期化済み。

use LudusCartographer\EvidenceRepository;

// --- get_api_usage アクション ---
if ($action === 'get_api_usage') {
    $crawlerDir = realpath(__DIR__ . '/../../..') . '/crawler';
    try {
        $db = new PDO('sqlite:' . $crawlerDir . '/storage/ludus.db');
        $db->setAttribute(PDO::ATTR_TIMEOUT, 2);

        // テーブル存在チェック
        $tableExists = $db->query("SELECT name FROM sqlite_master WHERE type='table' AND name='lc_api_usage'")->fetch();
        if (!$tableExists) {
            echo json_encode(['daily' => [], 'by_model' => [], 'by_purpose' => [], 'total' => ['input_tokens' => 0, 'output_tokens' => 0, 'count' => 0]], JSON_UNESCAPED_UNICODE);
            exit;
        }

        // 日別集計
        $daily = $db->query(
            "SELECT date(created_at) as day, model, purpose,"
            . " SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens,"
            . " COUNT(*) as call_count"
            . " FROM lc_api_usage"
            . " GROUP BY day, model, purpose"
            . " ORDER BY day DESC, model, purpose"
        )->fetchAll(PDO::FETCH_ASSOC);

        // モデル別集計
        $byModel = $db->query(
            "SELECT model, SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens,"
            . " COUNT(*) as call_count"
            . " FROM lc_api_usage GROUP BY model ORDER BY model"
        )->fetchAll(PDO::FETCH_ASSOC);

        // 用途別集計
        $byPurpose = $db->query(
            "SELECT purpose, SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens,"
            . " COUNT(*) as call_count"
            . " FROM lc_api_usage GROUP BY purpose ORDER BY purpose"
        )->fetchAll(PDO::FETCH_ASSOC);

        // 月別集計
        $monthly = $db->query(
            "SELECT strftime('%Y-%m', created_at) as month, model, purpose,"
            . " SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens,"
            . " COUNT(*) as call_count"
            . " FROM lc_api_usage"
            . " GROUP BY month, model, purpose"
            . " ORDER BY month DESC, model, purpose"
        )->fetchAll(PDO::FETCH_ASSOC);

        // 全体合計
        $total = $db->query(
            "SELECT SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens,"
            . " COUNT(*) as count"
            . " FROM lc_api_usage"
        )->fetch(PDO::FETCH_ASSOC);

        echo json_encode([
            'daily' => $daily ?: [],
            'monthly' => $monthly ?: [],
            'by_model' => $byModel ?: [],
            'by_purpose' => $byPurpose ?: [],
            'total' => $total ?: ['input_tokens' => 0, 'output_tokens' => 0, 'count' => 0],
        ], JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
    } catch (\Throwable $e) {
        echo json_encode(['daily' => [], 'by_model' => [], 'by_purpose' => [], 'total' => ['input_tokens' => 0, 'output_tokens' => 0, 'count' => 0], 'error' => $e->getMessage()]);
    }
    exit;
}
