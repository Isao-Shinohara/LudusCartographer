<?php

declare(strict_types=1);

require_once __DIR__ . '/../vendor/autoload.php';

use LudusCartographer\Database;
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

// --- 検索キーワード取得 (XSS対策: strip_tags + htmlspecialchars は Twig が行う) ---
$keyword = trim(strip_tags($_GET['q'] ?? ''));

// --- データ取得 ---
try {
    $pdo        = Database::getConnection();
    $repository = new ScreenRepository($pdo);
    $screens    = $repository->search($keyword);
    $dbError    = null;
} catch (\Throwable $e) {
    // DB 未接続時はサンプルデータにフォールバック
    $screens = ScreenRepository::getSampleData();
    if ($keyword !== '') {
        // キーワードでインメモリフィルタリング
        $kw = mb_strtolower($keyword);
        $screens = array_values(array_filter($screens, function (array $s) use ($kw): bool {
            return str_contains(mb_strtolower((string)($s['name'] ?? '')), $kw)
                || str_contains(mb_strtolower((string)($s['ocr_text'] ?? '')), $kw);
        }));
    }
    $dbError = $e->getMessage();
}

// --- レンダリング ---
echo $twig->render('search.html.twig', [
    'keyword' => $keyword,
    'screens' => $screens,
    'db_error' => $dbError,
]);
