<?php

declare(strict_types=1);

// dashboard.php (ルーター) から require される。共通変数
// ($pdo / $repository / $useDb / $action / $gameTitle /
//  $versionParam / $GEMINI_ENABLED) は _common.php で初期化済み。

use LudusCartographer\EvidenceRepository;

// --- get_noise_words アクション ---
if ($action === 'get_noise_words') {
    $crawlerDir = realpath(__DIR__ . '/../../..') . '/crawler';
    try {
        $db = new PDO('sqlite:' . $crawlerDir . '/storage/ludus.db');
        $db->setAttribute(PDO::ATTR_TIMEOUT, 2);
        $rows = $db->query("SELECT word, count, first_seen_at, last_seen_at FROM lc_ocr_noise_words ORDER BY count DESC, word ASC")->fetchAll(PDO::FETCH_ASSOC);
        echo json_encode(['words' => $rows ?: []], JSON_UNESCAPED_UNICODE);
    } catch (\Throwable $e) {
        echo json_encode(['words' => [], 'error' => $e->getMessage()]);
    }
    exit;
}
// --- delete_noise_word アクション ---
if ($action === 'delete_noise_word') {
    $word = $_GET['word'] ?? '';
    if ($word === '') { echo json_encode(['error' => 'word required']); exit; }
    $crawlerDir = realpath(__DIR__ . '/../../..') . '/crawler';
    try {
        $db = new PDO('sqlite:' . $crawlerDir . '/storage/ludus.db');
        $db->prepare("DELETE FROM lc_ocr_noise_words WHERE word = ?")->execute([$word]);
        echo json_encode(['ok' => true]);
    } catch (\Throwable $e) {
        echo json_encode(['error' => $e->getMessage()]);
    }
    exit;
}
// --- add_noise_word アクション ---
if ($action === 'add_noise_word') {
    $word = trim($_GET['word'] ?? '');
    if ($word === '') { echo json_encode(['error' => 'word required']); exit; }
    $crawlerDir = realpath(__DIR__ . '/../../..') . '/crawler';
    try {
        $db = new PDO('sqlite:' . $crawlerDir . '/storage/ludus.db');
        $db->exec("CREATE TABLE IF NOT EXISTS lc_ocr_noise_words (word TEXT PRIMARY KEY, count INTEGER DEFAULT 1, first_seen_at TEXT DEFAULT (datetime('now')), last_seen_at TEXT DEFAULT (datetime('now')))");
        $db->prepare("INSERT OR IGNORE INTO lc_ocr_noise_words (word, count) VALUES (?, 2)")->execute([$word]);
        echo json_encode(['ok' => true]);
    } catch (\Throwable $e) {
        echo json_encode(['error' => $e->getMessage()]);
    }
    exit;
}
// --- get_correction_candidates アクション (Phase 2: 適用候補取得) ---
if ($action === 'get_correction_candidates') {
    try {
        $crawlerDir = realpath(__DIR__ . '/../../..') . '/crawler';
        $db = new PDO('sqlite:' . $crawlerDir . '/storage/ludus.db');

        // 全 global ルールを取得
        $rules = $db->query(
            "SELECT id, before_text, after_text, frequency "
            . "FROM lc_ocr_corrections "
            . "WHERE scope = 'global' AND promoted_to_regex = 0 "
            . "ORDER BY frequency DESC, created_at DESC LIMIT 100"
        )->fetchAll(PDO::FETCH_ASSOC);

        // 各ルールに対して適用可能なノードを検索
        $candidates = [];
        foreach ($rules as $rule) {
            $stmt = $db->prepare(
                "SELECT m.master_fp, m.title, "
                . "COALESCE(s.ocr_text_gemini, s.ocr_text_hq, s.ocr_text, '') AS auto_text, "
                . "s.thumbnail_path "
                . "FROM lc_master_nodes m "
                . "LEFT JOIN lc_screens s ON s.id = m.representative_screen_id "
                . "WHERE m.ocr_text_manual IS NULL "
                . "AND COALESCE(s.ocr_text_gemini, s.ocr_text_hq, s.ocr_text, '') LIKE ? "
                . "LIMIT 50"
            );
            $stmt->execute(['%' . $rule['before_text'] . '%']);
            $matches = $stmt->fetchAll(PDO::FETCH_ASSOC);
            if ($matches) {
                $candidates[] = [
                    'rule' => $rule,
                    'matches' => $matches,
                    'count' => count($matches),
                ];
            }
        }
        echo json_encode([
            'candidates' => $candidates,
            'total_rules' => count($rules),
        ], JSON_UNESCAPED_UNICODE);
    } catch (\Throwable $e) {
        echo json_encode(['error' => $e->getMessage()]);
    }
    exit;
}
// --- apply_correction_rule アクション (Phase 2: ルール一括適用) ---
if ($action === 'apply_correction_rule') {
    $ruleId = (int)($_POST['rule_id'] ?? $_GET['rule_id'] ?? 0);
    $masterFps = json_decode($_POST['master_fps'] ?? $_GET['master_fps'] ?? '[]', true);
    if (!$ruleId || !is_array($masterFps) || empty($masterFps)) {
        echo json_encode(['error' => 'rule_id and master_fps required']);
        exit;
    }
    try {
        $crawlerDir = realpath(__DIR__ . '/../../..') . '/crawler';
        $db = new PDO('sqlite:' . $crawlerDir . '/storage/ludus.db');
        $db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);

        // ルール取得
        $stmt = $db->prepare("SELECT before_text, after_text FROM lc_ocr_corrections WHERE id = ?");
        $stmt->execute([$ruleId]);
        $rule = $stmt->fetch(PDO::FETCH_ASSOC);
        if (!$rule) {
            echo json_encode(['error' => 'rule not found']);
            exit;
        }

        // 各 master_fp に適用
        $applied = 0;
        $placeholders = implode(',', array_fill(0, count($masterFps), '?'));
        $stmt = $db->prepare(
            "SELECT m.master_fp, "
            . "COALESCE(s.ocr_text_gemini, s.ocr_text_hq, s.ocr_text, '') AS auto_text "
            . "FROM lc_master_nodes m "
            . "LEFT JOIN lc_screens s ON s.id = m.representative_screen_id "
            . "WHERE m.master_fp IN ($placeholders)"
        );
        $stmt->execute($masterFps);
        $rows = $stmt->fetchAll(PDO::FETCH_ASSOC);

        $updateStmt = $db->prepare(
            "UPDATE lc_master_nodes SET ocr_text_manual = ?, "
            . "manual_edited_at = datetime('now') WHERE master_fp = ?"
        );
        $updatedFps = [];
        foreach ($rows as $row) {
            $newText = str_replace($rule['before_text'], $rule['after_text'], $row['auto_text']);
            if ($newText !== $row['auto_text']) {
                $updateStmt->execute([$newText, $row['master_fp']]);
                $updatedFps[] = $row['master_fp'];
                $applied++;
            }
        }

        // 実際にテキストが変わった master_fp の Gemini 判定キャッシュのみを削除。
        if ($updatedFps) {
            try {
                $delPlaceholders = implode(',', array_fill(0, count($updatedFps), '?'));
                $db->prepare("DELETE FROM lc_anchor_judgments WHERE master_fp IN ($delPlaceholders)")
                   ->execute($updatedFps);
            } catch (\Throwable $e) {
                // サイレントスキップ
            }
        }

        // 適用回数を increment
        $db->prepare("UPDATE lc_ocr_corrections SET frequency = frequency + ?, last_applied_at = datetime('now') WHERE id = ?")
           ->execute([$applied, $ruleId]);

        echo json_encode(['ok' => true, 'applied' => $applied]);
    } catch (\Throwable $e) {
        echo json_encode(['error' => $e->getMessage()]);
    }
    exit;
}
