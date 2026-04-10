<?php

declare(strict_types=1);

require_once __DIR__ . '/../vendor/autoload.php';

use LudusCartographer\Database;
use LudusCartographer\EvidenceRepository;

$envPath = __DIR__ . '/../config/.env';
if (file_exists($envPath)) {
    $dotenv = Dotenv\Dotenv::createImmutable(dirname($envPath), '.env');
    $dotenv->safeLoad();
}

$loader = new \Twig\Loader\FilesystemLoader(__DIR__ . '/../templates');
$twig   = new \Twig\Environment($loader);

$gameTitles  = [];
$currentGame = trim(strip_tags($_GET['game'] ?? ''));

try {
    $pdo        = Database::getSqliteConnection();
    $repository = new EvidenceRepository($pdo);
    $gameTitles = $repository->getGameTitles();
} catch (\Throwable) {
    // DB 接続失敗時は空で進む
}

echo $twig->render('dashboard.html.twig', [
    'game_titles' => $gameTitles,
    'current_game' => $currentGame,
]);
