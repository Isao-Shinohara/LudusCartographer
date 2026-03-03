<?php

declare(strict_types=1);

require_once __DIR__ . '/../vendor/autoload.php';

use LudusCartographer\Database;
use LudusCartographer\EvidenceRepository;
use LudusCartographer\ScreenRepository;
use Twig\Environment;
use Twig\Loader\FilesystemLoader;

// --- 環境変数ロード (.env がある場合) ---
$envPath = __DIR__ . '/../config/.env';
if (file_exists($envPath)) {
    $dotenv = Dotenv\Dotenv::createImmutable(dirname($envPath), '.env');
    $dotenv->safeLoad();
}

// --- Twig 初期化 ---
$loader = new FilesystemLoader(__DIR__ . '/../templates');
$twig   = new Environment($loader, [
    'cache' => false,
    'debug' => ($_ENV['APP_DEBUG'] ?? 'false') === 'true',
]);

// --- リクエストパラメータ取得 ---
$keyword   = trim(strip_tags($_GET['q']    ?? ''));
$gameTitle = trim(strip_tags($_GET['game'] ?? ''));

// --- データ取得 ---
$gameTitles = [];
$dbError    = null;

try {
    $pdo        = Database::getConnection();
    $repository = new ScreenRepository($pdo);
    $screens    = $repository->search($keyword, 50, $gameTitle);
} catch (\Throwable) {
    // MySQL 未接続 → SQLite evidence DB にフォールバック
    try {
        $pdo        = Database::getSqliteConnection();
        $repository = new EvidenceRepository($pdo);
        $screens    = $repository->search($keyword, 50, $gameTitle);
        $gameTitles = $repository->getGameTitles();
    } catch (\Throwable $e) {
        // どちらも使えない場合はサンプルデータ
        $repository = null;
        $screens    = ScreenRepository::getSampleData();
        if ($keyword !== '') {
            $kw = mb_strtolower($keyword);
            $screens = array_values(array_filter($screens, function (array $s) use ($kw): bool {
                return str_contains(mb_strtolower((string)($s['name'] ?? '')), $kw)
                    || str_contains(mb_strtolower((string)($s['ocr_text'] ?? '')), $kw);
            }));
        }
        $dbError = $e->getMessage();
    }
}

// --- レンダリング ---
echo $twig->render('search.html.twig', [
    'keyword'      => $keyword,
    'screens'      => $screens,
    'game_titles'  => $gameTitles,
    'current_game' => $gameTitle,
    'db_error'     => $dbError,
]);
