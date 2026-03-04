<?php

declare(strict_types=1);

require_once __DIR__ . '/../../vendor/autoload.php';

use LudusCartographer\Database;
use LudusCartographer\EvidenceRepository;
use LudusCartographer\ScreenRepository;

// --- 環境変数ロード ---
$envPath = __DIR__ . '/../../config/.env';
if (file_exists($envPath)) {
    $dotenv = Dotenv\Dotenv::createImmutable(dirname($envPath), '.env');
    $dotenv->safeLoad();
}

header('Content-Type: application/json; charset=utf-8');

$action    = $_GET['action'] ?? 'search';
$gameTitle = trim(strip_tags($_GET['game'] ?? ''));

try {
    $pdo        = Database::getConnection();
    $repository = new ScreenRepository($pdo);
    $useDb      = true;
} catch (\Throwable) {
    // MySQL が使えない場合は SQLite evidence DB にフォールバック
    try {
        $pdo        = Database::getSqliteConnection();
        $repository = new EvidenceRepository($pdo);
        $useDb      = true;
    } catch (\Throwable) {
        $useDb = false;
    }
}

// --- get_games アクション ---
if ($action === 'get_games') {
    $games = ($useDb && $repository instanceof EvidenceRepository)
        ? $repository->getGameTitles()
        : [];
    echo json_encode(
        ['games' => $games, 'count' => count($games)],
        JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR,
    );
    exit;
}

// --- get_sessions アクション ---
if ($action === 'get_sessions') {
    $limit = min((int)($_GET['limit'] ?? 20), 100);

    if ($useDb) {
        $sessions = $repository->getSessions($limit, $gameTitle);
    } else {
        $sessions = ScreenRepository::getSampleSessions();
    }

    echo json_encode(
        ['sessions' => $sessions, 'count' => count($sessions)],
        JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR,
    );
    exit;
}

// --- get_coverage アクション ---
if ($action === 'get_coverage') {
    $coverage = ($useDb && $repository instanceof EvidenceRepository)
        ? $repository->getProjectCoverage($gameTitle)
        : ['unique_screens' => 0, 'max_depth_reached' => 0, 'total_sessions' => 0];
    echo json_encode($coverage, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
    exit;
}

// --- get_project_screens アクション ---
if ($action === 'get_project_screens') {
    $limit = min((int)($_GET['limit'] ?? 100), 500);
    $screens = ($useDb && $repository instanceof EvidenceRepository)
        ? $repository->getProjectScreens($gameTitle, $limit)
        : [];
    echo json_encode(
        ['screens' => $screens, 'count' => count($screens)],
        JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR,
    );
    exit;
}

// --- detail アクション ---
if ($action === 'detail') {
    $id = (int)($_GET['id'] ?? 0);
    if ($id <= 0) {
        http_response_code(400);
        echo json_encode(['error' => 'invalid id']);
        exit;
    }

    if ($useDb) {
        $result = $repository->findWithElements($id);
    } else {
        $screen = null;
        foreach (ScreenRepository::getSampleData() as $s) {
            if ((int)$s['id'] === $id) {
                $screen = $s;
                break;
            }
        }
        $result = ['screen' => $screen, 'elements' => [], 'parents' => []];
    }

    echo json_encode($result, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
    exit;
}

// --- search アクション (default) ---
$title     = trim(strip_tags($_GET['title']      ?? ''));
$keyword   = trim(strip_tags($_GET['keyword']    ?? ''));
$sessionId = trim(strip_tags($_GET['session_id'] ?? ''));
$limit     = min((int)($_GET['limit'] ?? 100), 500);

if ($useDb) {
    $screens = $repository->searchAdvanced($title, $keyword, $sessionId, $limit, $gameTitle);
} else {
    $screens = ScreenRepository::getSampleData();
    foreach (array_filter([$title, $keyword]) as $f) {
        $fl = mb_strtolower($f);
        $screens = array_values(array_filter(
            $screens,
            static function (array $s) use ($fl): bool {
                return str_contains(mb_strtolower((string)($s['name'] ?? '')), $fl)
                    || str_contains(mb_strtolower((string)($s['ocr_text'] ?? '')), $fl);
            },
        ));
    }
}

echo json_encode(
    ['screens' => $screens, 'count' => count($screens)],
    JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR,
);
