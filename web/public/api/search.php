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

// --- heartbeat アクション (ダッシュボード生存通知) ---
if ($action === 'heartbeat') {
    $hbFile = sys_get_temp_dir() . '/lc_dashboard_heartbeat';
    file_put_contents($hbFile, (string)time());
    echo json_encode(['ok' => true, 'ts' => time()]);
    exit;
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

// --- get_recent_screens アクション (ダッシュボード用) ---
if ($action === 'get_recent_screens') {
    $limit = min((int)($_GET['limit'] ?? 50), 10000);
    $afterId = (int)($_GET['after_id'] ?? 0);
    $sessionId = $_GET['session_id'] ?? '';

    if ($useDb && $repository instanceof EvidenceRepository) {
        $screens = $repository->getRecentScreens($limit, $gameTitle, $afterId, $sessionId);
    } else {
        $screens = [];
    }

    echo json_encode(
        ['screens' => $screens, 'count' => count($screens)],
        JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR,
    );
    exit;
}

// --- get_sessions アクション (セッション一覧) ---
if ($action === 'get_sessions') {
    if ($useDb && $repository instanceof EvidenceRepository) {
        $sessions = $repository->getSessions(100, $gameTitle);
    } else {
        $sessions = [];
    }
    echo json_encode(
        ['sessions' => $sessions],
        JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR,
    );
    exit;
}

// --- get_final_screens アクション (決定版ダッシュボード用) ---
if ($action === 'get_final_screens') {
    $limit = min((int)($_GET['limit'] ?? 10000), 10000);

    if ($useDb && $repository instanceof EvidenceRepository) {
        $screens = $repository->getFinalScreens($limit, $gameTitle);
    } else {
        $screens = [];
    }

    echo json_encode(
        ['screens' => $screens, 'count' => count($screens)],
        JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR,
    );
    exit;
}

// --- get_final_screens_all アクション (除外含む全件) ---
if ($action === 'get_final_screens_all') {
    $limit = min((int)($_GET['limit'] ?? 10000), 10000);
    if ($useDb && $repository instanceof EvidenceRepository) {
        $screens = $repository->getFinalScreensIncludeExcluded($limit, $gameTitle);
    } else {
        $screens = [];
    }
    echo json_encode(
        ['screens' => $screens, 'count' => count($screens)],
        JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR,
    );
    exit;
}

// --- toggle_exclude アクション ---
if ($action === 'toggle_exclude') {
    $masterFp = $_GET['master_fp'] ?? '';
    if ($masterFp === '' || !($useDb && $repository instanceof EvidenceRepository)) {
        echo json_encode(['error' => 'invalid request']);
        exit;
    }
    $result = $repository->toggleExclude($masterFp);
    echo json_encode($result, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
    exit;
}

// --- get_pending_merges アクション ---
if ($action === 'get_pending_merges') {
    if ($useDb && $repository instanceof EvidenceRepository) {
        $pending = $repository->getPendingMerges();
        $merged = $repository->getMergedSessions();
    } else {
        $pending = [];
        $merged = [];
    }
    echo json_encode(
        ['pending' => $pending, 'merged' => $merged],
        JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR,
    );
    exit;
}

// --- preview_merge アクション ---
if ($action === 'preview_merge') {
    $sessionId = $_GET['session_id'] ?? '';
    if ($sessionId === '') {
        echo json_encode(['error' => 'session_id required']);
        exit;
    }
    // Python の CrossSessionMerger.preview_merge() を呼ぶ
    $dbPath = $repository instanceof EvidenceRepository
        ? realpath(__DIR__ . '/../../..') . '/crawler/storage/ludus.db'
        : '';
    $cmd = sprintf(
        'cd %s && ./venv/bin/python -c %s 2>&1',
        escapeshellarg(realpath(__DIR__ . '/../../..') . '/crawler'),
        escapeshellarg(
            "import json; from pathlib import Path; from tools.cross_session_merger import CrossSessionMerger; "
            . "m = CrossSessionMerger(Path('storage/ludus.db')); "
            . "print(json.dumps(m.preview_merge('" . addslashes($sessionId) . "'), ensure_ascii=False)); "
            . "m.close()"
        ),
    );
    $output = shell_exec($cmd);
    header('Content-Type: application/json');
    echo $output ?: json_encode(['error' => 'preview failed']);
    exit;
}

// --- execute_merge アクション ---
if ($action === 'execute_merge') {
    $sessionId = $_GET['session_id'] ?? '';
    if ($sessionId === '') {
        echo json_encode(['error' => 'session_id required']);
        exit;
    }
    $cmd = sprintf(
        'cd %s && ./venv/bin/python -c %s 2>&1',
        escapeshellarg(realpath(__DIR__ . '/../../..') . '/crawler'),
        escapeshellarg(
            "import json; from pathlib import Path; from tools.cross_session_merger import CrossSessionMerger; "
            . "m = CrossSessionMerger(Path('storage/ludus.db')); "
            . "n = m.merge_to_master('" . addslashes($sessionId) . "'); "
            . "m.close(); "
            . "print(json.dumps({'ok': True, 'new_nodes': n}, ensure_ascii=False))"
        ),
    );
    $output = shell_exec($cmd);
    header('Content-Type: application/json');
    echo $output ?: json_encode(['error' => 'merge failed']);
    exit;
}

// --- get_graph アクション (遷移グラフ Cytoscape.js 用) ---
if ($action === 'get_graph') {
    $nodes = [];
    $edges = [];
    $sccGroups = [];
    $sessionId = $_GET['session_id'] ?? '';

    if ($useDb && $repository instanceof EvidenceRepository) {
        $pdo = $repository->getPdo();

        // マスターグラフが存在し、セッション指定がなければマスターを使用
        $hasMaster = false;
        if (!$sessionId) {
            $masterCount = $pdo->query("SELECT COUNT(*) FROM lc_master_nodes")->fetchColumn();
            $hasMaster = $masterCount > 0;
        }

        if ($hasMaster) {
            // --- マスターグラフモード ---
            $nodeStmt = $pdo->query(
                "SELECT mn.master_fp AS fingerprint, mn.title, mn.scene,"
                . " s.thumbnail_path, s.screenshot_path,"
                . " mn.bfs_depth, mn.scc_id, mn.scc_label, mn.visit_count"
                . " FROM lc_master_nodes mn"
                . " LEFT JOIN lc_screens s ON s.id = mn.representative_screen_id"
                . " ORDER BY mn.bfs_depth ASC"
            );
            if ($nodeStmt) {
                while ($row = $nodeStmt->fetch(PDO::FETCH_ASSOC)) {
                    $imgPath = $row['thumbnail_path'] ?: $row['screenshot_path'] ?: '';
                    $nodes[] = [
                        'id'          => $row['fingerprint'],
                        'fingerprint' => $row['fingerprint'],
                        'title'       => $row['title'] ?: '',
                        'scene'       => $row['scene'] ?: '',
                        'thumbnail'   => $imgPath ? ('img.php?path=' . urlencode($imgPath)) : '',
                        'bfs_depth'   => $row['bfs_depth'] !== null ? (int)$row['bfs_depth'] : null,
                        'scc_id'      => $row['scc_id'] !== null ? (int)$row['scc_id'] : null,
                        'scc_label'   => $row['scc_label'] ?: '',
                        'visit_count' => (int)($row['visit_count'] ?? 1),
                    ];
                }
            }

            $edgeStmt = $pdo->query(
                "SELECT from_master_fp AS from_fp, to_master_fp AS to_fp,"
                . " tap_label, action_name, count"
                . " FROM lc_master_edges"
            );
            if ($edgeStmt) {
                while ($row = $edgeStmt->fetch(PDO::FETCH_ASSOC)) {
                    $isBack = stripos($row['action_name'] ?? '', 'BACK') !== false;
                    $edges[] = [
                        'source'      => $row['from_fp'],
                        'target'      => $row['to_fp'],
                        'tap_label'   => $row['tap_label'] ?: '',
                        'action_name' => $row['action_name'] ?: '',
                        'count'       => (int)$row['count'],
                        'is_back'     => $isBack,
                    ];
                }
            }
        } else {
            // --- セッション別 or フォールバックモード ---
            $sidFilter = '';
            $sidParam = [];
            if ($sessionId) {
                $sidFilter = ' AND session_id = ?';
                $sidParam = [$sessionId];
            }

            $nodeStmt = $pdo->prepare(
                "SELECT fingerprint, title, scene, thumbnail_path, screenshot_path,"
                . " bfs_depth, scc_id, scc_label"
                . " FROM lc_screens WHERE is_representative = 1" . $sidFilter
                . " ORDER BY bfs_depth ASC, discovered_at ASC"
            );
            $nodeStmt->execute($sidParam);
            while ($row = $nodeStmt->fetch(PDO::FETCH_ASSOC)) {
                $imgPath = $row['thumbnail_path'] ?: $row['screenshot_path'] ?: '';
                $nodes[] = [
                    'id'          => $row['fingerprint'],
                    'fingerprint' => $row['fingerprint'],
                    'title'       => $row['title'] ?: '',
                    'scene'       => $row['scene'] ?: '',
                    'thumbnail'   => $imgPath ? ('img.php?path=' . urlencode($imgPath)) : '',
                    'bfs_depth'   => $row['bfs_depth'] !== null ? (int)$row['bfs_depth'] : null,
                    'scc_id'      => $row['scc_id'] !== null ? (int)$row['scc_id'] : null,
                    'scc_label'   => $row['scc_label'] ?: '',
                    'visit_count' => 1,
                ];
            }

            $edgeStmt = $pdo->prepare(
                "SELECT from_fp, to_fp, tap_label, action_name,"
                . " COUNT(*) as count"
                . " FROM lc_transitions WHERE to_fp IS NOT NULL" . $sidFilter
                . " GROUP BY from_fp, to_fp"
            );
            $edgeStmt->execute($sidParam);
            while ($row = $edgeStmt->fetch(PDO::FETCH_ASSOC)) {
                $isBack = stripos($row['action_name'] ?? '', 'BACK') !== false;
                $edges[] = [
                    'source'      => $row['from_fp'],
                    'target'      => $row['to_fp'],
                    'tap_label'   => $row['tap_label'] ?: '',
                    'action_name' => $row['action_name'] ?: '',
                    'count'       => (int)$row['count'],
                    'is_back'     => $isBack,
                ];
            }
        }

        // SCC グループ
        $sccStmt = $pdo->query("SELECT * FROM lc_scc_groups ORDER BY id");
        if ($sccStmt) {
            while ($row = $sccStmt->fetch(PDO::FETCH_ASSOC)) {
                $sccGroups[] = [
                    'id'           => (int)$row['id'],
                    'label'        => $row['label'] ?: '',
                    'screen_count' => (int)$row['screen_count'],
                    'root_fp'      => $row['root_fp'] ?: '',
                ];
            }
        }
    }

    echo json_encode(
        ['nodes' => $nodes, 'edges' => $edges, 'scc_groups' => $sccGroups],
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
