<?php

declare(strict_types=1);

// ダッシュボード API ハンドラー共通初期化。
// dashboard.php (ルーター) から require される想定。各ハンドラーからは
// $pdo / $repository / $useDb / $action / $gameTitle / $versionParam /
// $GEMINI_ENABLED が参照可能。

require_once __DIR__ . '/../../vendor/autoload.php';

use LudusCartographer\Database;
use LudusCartographer\EvidenceRepository;

// --- 環境変数ロード ---
$envPath = __DIR__ . '/../../config/.env';
if (file_exists($envPath)) {
    $dotenv = Dotenv\Dotenv::createImmutable(dirname($envPath), '.env');
    $dotenv->safeLoad();
}

// --- crawler/config/.env から GEMINI_API_KEY 検出 ---
if (!function_exists('_gemini_enabled')) {
    function _gemini_enabled(): bool {
        $crawlerEnv = realpath(__DIR__ . '/../../..') . '/crawler/config/.env';
        if (!file_exists($crawlerEnv)) return false;
        $content = @file_get_contents($crawlerEnv);
        if (!$content) return false;
        return preg_match('/^GEMINI_API_KEY\s*=\s*\S+/m', $content) === 1;
    }
}
$GEMINI_ENABLED = _gemini_enabled();

header('Content-Type: application/json; charset=utf-8');

$action       = $_GET['action'] ?? '';
$gameTitle    = trim(strip_tags($_GET['game'] ?? ''));
$versionParam = isset($_GET['version']) ? (int)$_GET['version'] : null;

try {
    $pdo        = Database::getSqliteConnection();
    $repository = new EvidenceRepository($pdo);
    $useDb      = true;
} catch (\Throwable) {
    $useDb = false;
}
