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

// --- crawler/config/.env から GEMINI_API_KEY 検出 ---
function _gemini_enabled(): bool {
    $crawlerEnv = realpath(__DIR__ . '/../../..') . '/crawler/config/.env';
    if (!file_exists($crawlerEnv)) return false;
    $content = @file_get_contents($crawlerEnv);
    if (!$content) return false;
    return preg_match('/^GEMINI_API_KEY\s*=\s*\S+/m', $content) === 1;
}
$GEMINI_ENABLED = _gemini_enabled();

header('Content-Type: application/json; charset=utf-8');

$action    = $_GET['action'] ?? 'search';
$gameTitle = trim(strip_tags($_GET['game'] ?? ''));
$versionParam = isset($_GET['version']) ? (int)$_GET['version'] : null;

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

// --- get_versions アクション ---
if ($action === 'get_versions') {
    if ($useDb && $repository instanceof EvidenceRepository) {
        $versions = $repository->getVersions();
    } else {
        $versions = [];
    }
    echo json_encode(
        ['versions' => $versions],
        JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR,
    );
    exit;
}

// --- create_version アクション ---
if ($action === 'create_version') {
    $name = trim(strip_tags($_GET['name'] ?? ''));
    if ($name === '' || !($useDb && $repository instanceof EvidenceRepository)) {
        echo json_encode(['error' => 'name required']);
        exit;
    }
    $result = $repository->createVersion($name);
    echo json_encode($result, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
    exit;
}

// --- activate_version アクション ---
if ($action === 'activate_version') {
    $versionId = (int)($_GET['version_id'] ?? 0);
    if ($versionId <= 0 || !($useDb && $repository instanceof EvidenceRepository)) {
        echo json_encode(['error' => 'valid version_id required']);
        exit;
    }
    $result = $repository->activateVersion($versionId);
    echo json_encode($result, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
    exit;
}

// --- rename_version アクション ---
if ($action === 'rename_version') {
    $versionId = (int)($_GET['version_id'] ?? 0);
    $newName = trim(strip_tags($_GET['name'] ?? ''));
    if ($versionId <= 0 || $newName === '' || !($useDb && $repository instanceof EvidenceRepository)) {
        echo json_encode(['error' => 'version_id and name required']);
        exit;
    }
    $result = $repository->renameVersion($versionId, $newName);
    echo json_encode($result, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
    exit;
}

// --- delete_version アクション ---
if ($action === 'delete_version') {
    $versionId = (int)($_GET['version_id'] ?? 0);
    if ($versionId <= 0 || !($useDb && $repository instanceof EvidenceRepository)) {
        echo json_encode(['error' => 'valid version_id required']);
        exit;
    }
    $result = $repository->deleteVersion($versionId);
    // ローカル画像ファイル削除
    if (($result['ok'] ?? false) && !empty($result['session_ids'])) {
        $crawlerDir = realpath(__DIR__ . '/../../..') . '/crawler';
        $imageDirs = [
            $crawlerDir . '/storage/screenshots/',
            $crawlerDir . '/storage/reinstall/',
            $crawlerDir . '/storage/evidence/',
            $crawlerDir . '/evidence/',
            $crawlerDir . '/screenshots/',
        ];
        $deleted = 0;
        foreach ($result['session_ids'] as $sid) {
            foreach ($imageDirs as $base) {
                $dir = $base . $sid;
                if (is_dir($dir)) {
                    $files = glob("$dir/*");
                    if ($files) { array_map('unlink', $files); $deleted += count($files); }
                    @rmdir($dir);
                }
            }
        }
        $result['deleted_files'] = $deleted;
    }
    echo json_encode($result, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
    exit;
}

// --- get_active_version アクション ---
if ($action === 'get_active_version') {
    if ($useDb && $repository instanceof EvidenceRepository) {
        $activeId = $repository->getActiveVersionId();
        $stmt = $repository->getPdo()->prepare(
            "SELECT id, name, created_at, is_active FROM lc_versions WHERE id = :vid"
        );
        $stmt->execute([':vid' => $activeId]);
        $version = $stmt->fetch(\PDO::FETCH_ASSOC);
        echo json_encode(
            ['version' => $version ?: ['id' => $activeId, 'name' => 'default']],
            JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR,
        );
    } else {
        echo json_encode(['version' => ['id' => 1, 'name' => 'default']]);
    }
    exit;
}

// --- get_games アクション ---
if ($action === 'get_games') {
    $games = ($useDb && $repository instanceof EvidenceRepository)
        ? $repository->getGameTitles($versionParam)
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
        $sessions = ($repository instanceof EvidenceRepository)
            ? $repository->getSessions($limit, $gameTitle, $versionParam)
            : $repository->getSessions($limit, $gameTitle);
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
        $screens = $repository->getRecentScreens($limit, $gameTitle, $afterId, $sessionId, $versionParam);
    } else {
        $screens = [];
    }

    echo json_encode(
        ['screens' => $screens, 'count' => count($screens), 'gemini_enabled' => $GEMINI_ENABLED],
        JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR,
    );
    exit;
}

// --- get_sessions アクション (セッション一覧) ---
if ($action === 'get_sessions') {
    if ($useDb && $repository instanceof EvidenceRepository) {
        $sessions = $repository->getSessions(100, $gameTitle, $versionParam);
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
        $screens = $repository->getFinalScreens($limit, $gameTitle, $versionParam);
    } else {
        $screens = [];
    }

    echo json_encode(
        ['screens' => $screens, 'count' => count($screens)],
        JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR,
    );
    exit;
}

// --- get_cluster_siblings アクション ---
if ($action === 'get_cluster_siblings') {
    $screenId = (int)($_GET['screen_id'] ?? 0);
    if ($screenId <= 0 || !($useDb && $repository instanceof EvidenceRepository)) {
        echo json_encode(['siblings' => []]);
        exit;
    }
    $siblings = $repository->getClusterSiblings($screenId);
    echo json_encode(
        ['siblings' => $siblings],
        JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR,
    );
    exit;
}

// --- promote_representative アクション ---
if ($action === 'promote_representative') {
    $masterFp = $_GET['master_fp'] ?? '';
    $newScreenId = (int)($_GET['screen_id'] ?? 0);
    if ($masterFp === '' || $newScreenId <= 0 || !($useDb && $repository instanceof EvidenceRepository)) {
        echo json_encode(['error' => 'invalid request']);
        exit;
    }
    $result = $repository->promoteRepresentative($masterFp, $newScreenId);
    echo json_encode($result, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
    exit;
}

// --- get_final_screens_all アクション (除外含む全件) ---
if ($action === 'get_final_screens_all') {
    $limit = min((int)($_GET['limit'] ?? 10000), 10000);
    if ($useDb && $repository instanceof EvidenceRepository) {
        $screens = $repository->getFinalScreensIncludeExcluded($limit, $gameTitle, $versionParam);
    } else {
        $screens = [];
    }
    echo json_encode(
        ['screens' => $screens, 'count' => count($screens), 'gemini_enabled' => $GEMINI_ENABLED],
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
    $result = $repository->toggleExclude($masterFp, $versionParam);
    echo json_encode($result, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
    exit;
}

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

// --- get_pending_merges アクション ---
if ($action === 'get_pending_merges') {
    if ($useDb && $repository instanceof EvidenceRepository) {
        $pending = $repository->getPendingMerges($versionParam);
        $merged = $repository->getMergedSessions($versionParam);
        $empty = $repository->getEmptySessions($versionParam);
        $running = $repository->getRunningSessions($versionParam);
        $no_transition = $repository->getNoTransitionSessions($versionParam);
        $bg_pending = $repository->getBgPendingSessions($versionParam);
    } else {
        $pending = [];
        $merged = [];
        $empty = [];
        $running = [];
        $no_transition = [];
        $bg_pending = [];
    }
    echo json_encode(
        ['pending' => $pending, 'merged' => $merged, 'empty' => $empty,
         'running' => $running, 'no_transition' => $no_transition,
         'bg_pending' => $bg_pending],
        JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR,
    );
    exit;
}

// --- merge_progress アクション (結果ファイルの出現で完了判定) ---
if ($action === 'merge_progress') {
    $crawlerDir = realpath(__DIR__ . '/../../..') . '/crawler';
    $resultFile = $crawlerDir . '/storage/merge_result.json';
    $done = file_exists($resultFile);
    $result = $done ? json_decode(file_get_contents($resultFile), true) : null;
    if ($done) @unlink($resultFile);
    echo json_encode([
        'done' => $done,
        'result' => $result,
    ], JSON_UNESCAPED_UNICODE);
    exit;
}

// --- process_session_bg アクション (バックグラウンド未完了の手動完了) ---
if ($action === 'process_session_bg') {
    $sessionId = $_GET['session_id'] ?? '';
    if ($sessionId === '') {
        echo json_encode(['error' => 'session_id required']);
        exit;
    }
    $crawlerDir = escapeshellarg(realpath(__DIR__ . '/../../..') . '/crawler');
    $resultFile = realpath(__DIR__ . '/../../..') . '/crawler/storage/merge_result.json';
    @unlink($resultFile);
    // Gemini OCR + dedup + graph build をフルパイプライン実行
    // 内部的にループして全件完了させる (1クリックで完了)
    $sidEsc = addslashes($sessionId);
    $script = <<<PYTHON
import json, sqlite3, os
from pathlib import Path
try:
    from dotenv import load_dotenv
    _env_path = Path("config/.env")
    if _env_path.exists():
        load_dotenv(_env_path)
except ImportError:
    pass
from tools.ap.background_worker import BackgroundWorker
from tools.batch_processor import BatchProcessor
SID = "$sidEsc"
DB = Path("storage/ludus.db")
def _pending():
    c = sqlite3.connect(str(DB))
    total = c.execute("SELECT COUNT(*) FROM lc_screens WHERE session_id=? AND is_representative=1", (SID,)).fetchone()[0]
    g = c.execute("SELECT COUNT(*) FROM lc_screens WHERE session_id=? AND is_representative=1 AND ocr_text_gemini IS NULL", (SID,)).fetchone()[0]
    d = c.execute("SELECT COUNT(*) FROM lc_screens WHERE session_id=? AND cluster_id IS NULL AND phash IS NOT NULL AND phash != ''", (SID,)).fetchone()[0]
    c.close()
    return total, g, d
def _update_progress(phase, total, done, remaining_g, remaining_d):
    c = sqlite3.connect(str(DB))
    c.execute("INSERT OR REPLACE INTO auto_pilot_state (key, value) VALUES ('merge_progress', ?)",
              (json.dumps({"phase": phase, "total": total, "ocr_done": total - remaining_g, "ocr_total": total, "dedup_remaining": remaining_d}),))
    c.commit()
    c.close()
w = BackgroundWorker(db_path=DB, session_id=SID)
MAX_ITERATIONS = 100
iters = 0
total, g, d = _pending()
while iters < MAX_ITERATIONS:
    iters += 1
    w._run_incremental_dedup()
    w._run_gemini_batch_correction()
    total, g, d = _pending()
    _update_progress("OCR", total, total - g, g, d)
    if g == 0 and d == 0:
        break
w._run_incremental_dedup()
w._synthesize_auto_edges()
_update_progress("graph", total, total, 0, 0)
bp = BatchProcessor(db_path=DB)
sccs = bp.build_graph(session_id=SID)
bp.close()
total, g, d = _pending()
print(json.dumps({"ok": True, "sccs": sccs, "iterations": iters, "remaining_gemini": g, "remaining_dedup": d}, ensure_ascii=False))
PYTHON;
    $cmd = sprintf(
        'cd %s && exec 3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&- && ./venv/bin/python -B -W ignore -c %s > %s 2>>storage/process_session_bg.err.log </dev/null &',
        $crawlerDir,
        escapeshellarg($script),
        escapeshellarg($resultFile),
    );
    exec($cmd);
    header('Content-Type: application/json');
    echo json_encode(['started' => true]);
    exit;
}

// --- build_session_graph アクション (バックグラウンド実行) ---
if ($action === 'build_session_graph') {
    $sessionId = $_GET['session_id'] ?? '';
    if ($sessionId === '') {
        echo json_encode(['error' => 'session_id required']);
        exit;
    }
    $crawlerDir = escapeshellarg(realpath(__DIR__ . '/../../..') . '/crawler');
    $resultFile = realpath(__DIR__ . '/../../..') . '/crawler/storage/merge_result.json';
    @unlink($resultFile);
    $scriptFile = realpath(__DIR__ . '/../../..') . '/crawler/storage/_build_graph.py';
    file_put_contents($scriptFile, <<<PYTHON
import json, logging, sys, os
sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())
logging.disable(logging.CRITICAL)
from pathlib import Path
from tools.batch_processor import BatchProcessor
bp = BatchProcessor(db_path=Path("storage/ludus.db"))
sccs = bp.build_graph(session_id="{$sessionId}")
bp.close()
with open(sys.argv[1], "w") as f:
    json.dump({"ok": True, "sccs": sccs}, f, ensure_ascii=False)
PYTHON);
    $crawlerDirRaw = realpath(__DIR__ . '/../../..') . '/crawler';
    $bgCmd = "cd " . escapeshellarg($crawlerDirRaw)
           . " && ./venv/bin/python -B -W ignore " . escapeshellarg($scriptFile)
           . " " . escapeshellarg($resultFile)
           . " </dev/null > /dev/null 2>storage/preview_merge.err.log &";
    pclose(popen($bgCmd, 'r'));
    header('Content-Type: application/json');
    echo json_encode(['started' => true]);
    exit;
}

// --- preview_merge アクション (バックグラウンド実行) ---
if ($action === 'preview_merge') {
    $sessionId = $_GET['session_id'] ?? '';
    if ($sessionId === '') {
        echo json_encode(['error' => 'session_id required']);
        exit;
    }
    $crawlerDir = escapeshellarg(realpath(__DIR__ . '/../../..') . '/crawler');
    $resultFile = realpath(__DIR__ . '/../../..') . '/crawler/storage/merge_result.json';
    @unlink($resultFile);
    // 二重起動防止はフロント側の withOperationLock で制御
    // スクリプトファイルに書き出してバックグラウンド実行 (クォート問題を回避)
    $scriptFile = realpath(__DIR__ . '/../../..') . '/crawler/storage/_preview_merge.py';
    $excludeFpsPreview = $_GET['exclude_fps'] ?? '[]';
    $includeFpsPreview = $_GET['include_fps'] ?? '[]';
    file_put_contents($scriptFile, <<<PYTHON
import json, logging, sys, os
sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())
logging.basicConfig(level=logging.INFO, format='%(message)s', stream=sys.stderr)
try:
    from dotenv import load_dotenv
    _env = os.path.join("config", ".env")
    if os.path.exists(_env): load_dotenv(_env)
except ImportError:
    pass
from pathlib import Path
from tools.cross_session_merger import CrossSessionMerger
exclude_fps = set(json.loads('{$excludeFpsPreview}'))
include_fps = set(json.loads('{$includeFpsPreview}'))
m = CrossSessionMerger(Path("storage/ludus.db"))
r = m.preview_merge("{$sessionId}", exclude_fps=exclude_fps or None, include_fps=include_fps or None)
m.close()
with open(sys.argv[1], "w") as f:
    json.dump(r, f, ensure_ascii=False)
PYTHON);
    $crawlerDirRaw = realpath(__DIR__ . '/../../..') . '/crawler';
    $bgCmd = "cd " . escapeshellarg($crawlerDirRaw)
           . " && ./venv/bin/python -B -W ignore " . escapeshellarg($scriptFile)
           . " " . escapeshellarg($resultFile)
           . " </dev/null > /dev/null 2>storage/preview_merge.err.log &";
    pclose(popen($bgCmd, 'r'));
    header('Content-Type: application/json');
    echo json_encode(['started' => true]);
    exit;
}

// --- execute_merge アクション (バックグラウンド実行) ---
if ($action === 'execute_merge') {
    $sessionId = $_GET['session_id'] ?? '';
    if ($sessionId === '') {
        echo json_encode(['error' => 'session_id required']);
        exit;
    }
    $crawlerDir = escapeshellarg(realpath(__DIR__ . '/../../..') . '/crawler');
    $resultFile = realpath(__DIR__ . '/../../..') . '/crawler/storage/merge_result.json';
    @unlink($resultFile);
    $scriptFile = realpath(__DIR__ . '/../../..') . '/crawler/storage/_execute_merge.py';
    $excludeFps = $_GET['exclude_fps'] ?? '[]';
    $includeFps = $_GET['include_fps'] ?? '[]';
    file_put_contents($scriptFile, <<<PYTHON
import json, logging, sys, os
sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())
logging.disable(logging.CRITICAL)
try:
    from dotenv import load_dotenv
    _env = os.path.join("config", ".env")
    if os.path.exists(_env): load_dotenv(_env)
except ImportError:
    pass
from pathlib import Path
from tools.cross_session_merger import CrossSessionMerger
import sqlite3
exclude_fps = set(json.loads('{$excludeFps}'))
include_fps = set(json.loads('{$includeFps}'))
m = CrossSessionMerger(Path("storage/ludus.db"))
n = m.merge_to_master("{$sessionId}", exclude_fps=exclude_fps, include_fps=include_fps)
conn = sqlite3.connect("storage/ludus.db")
master_nodes = conn.execute("SELECT COUNT(*) FROM lc_master_nodes").fetchone()[0]
anchors = conn.execute("SELECT COUNT(*) FROM lc_node_mappings WHERE session_id=? AND match_method != 'new'", ("{$sessionId}",)).fetchone()[0]
conn.close()
m.close()
with open(sys.argv[1], "w") as f:
    json.dump({"ok": True, "new_nodes": n, "anchors": anchors, "master_nodes": master_nodes}, f, ensure_ascii=False)
PYTHON);
    $crawlerDirRaw = realpath(__DIR__ . '/../../..') . '/crawler';
    $bgCmd = "cd " . escapeshellarg($crawlerDirRaw)
           . " && ./venv/bin/python -B -W ignore " . escapeshellarg($scriptFile)
           . " " . escapeshellarg($resultFile)
           . " </dev/null > /dev/null 2>storage/preview_merge.err.log &";
    pclose(popen($bgCmd, 'r'));
    header('Content-Type: application/json');
    echo json_encode(['started' => true]);
    exit;
}

// --- can_unmerge アクション ---
if ($action === 'can_unmerge') {
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
            . "r = m.can_unmerge('" . addslashes($sessionId) . "'); "
            . "m.close(); "
            . "print(json.dumps(r, ensure_ascii=False))"
        ),
    );
    $output = shell_exec($cmd);
    header('Content-Type: application/json');
    echo $output ?: json_encode(['ok' => false, 'reason' => 'check failed']);
    exit;
}

// --- execute_unmerge アクション ---
if ($action === 'execute_unmerge') {
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
            . "r = m.unmerge_session('" . addslashes($sessionId) . "'); "
            . "m.close(); "
            . "print(json.dumps(r, ensure_ascii=False))"
        ),
    );
    $output = shell_exec($cmd);
    header('Content-Type: application/json');
    echo $output ?: json_encode(['ok' => false, 'error' => 'unmerge failed']);
    exit;
}

// --- update_manual_text アクション (Phase 1: 手動編集) ---
if ($action === 'update_manual_text') {
    $masterFp = $_POST['master_fp'] ?? $_GET['master_fp'] ?? '';
    $newText = $_POST['text'] ?? $_GET['text'] ?? '';
    $newTitle = $_POST['title'] ?? $_GET['title'] ?? null;
    if ($masterFp === '') {
        echo json_encode(['error' => 'master_fp required']);
        exit;
    }
    try {
        $crawlerDir = realpath(__DIR__ . '/../../..') . '/crawler';
        $db = new PDO('sqlite:' . $crawlerDir . '/storage/ludus.db');
        $db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);

        // 編集前の値を取得（修正ルール抽出用）
        $stmt = $db->prepare(
            "SELECT COALESCE(ocr_text_manual, '') AS prev_text, "
            . "COALESCE(ocr_text_gemini, ocr_text, '') AS auto_text, "
            . "COALESCE(title_manual, title, '') AS prev_title "
            . "FROM lc_master_nodes WHERE master_fp = ?"
        );
        $stmt->execute([$masterFp]);
        $prev = $stmt->fetch(PDO::FETCH_ASSOC);
        if (!$prev) {
            echo json_encode(['error' => 'master_fp not found']);
            exit;
        }

        // 手動編集を保存
        $stmt = $db->prepare(
            "UPDATE lc_master_nodes SET "
            . "ocr_text_manual = ?, "
            . ($newTitle !== null ? "title_manual = ?, " : "")
            . "manual_edited_at = datetime('now') "
            . "WHERE master_fp = ?"
        );
        $params = [$newText];
        if ($newTitle !== null) $params[] = $newTitle;
        $params[] = $masterFp;
        $stmt->execute($params);

        // Phase 2: 自動 OCR 結果と手動編集の diff から修正ルールを抽出
        $autoText = $prev['auto_text'] ?? '';
        $rulesAdded = 0;
        if ($autoText && $newText && $autoText !== $newText) {
            // 単語レベルの diff を抽出（簡易版: 単純な置換ペアとして全文を保存）
            $stmt = $db->prepare(
                "INSERT INTO lc_ocr_corrections (before_text, after_text, scope, source) "
                . "VALUES (?, ?, 'global', 'manual') "
                . "ON CONFLICT(before_text, after_text, scope, scope_id) "
                . "DO UPDATE SET frequency = frequency + 1, "
                . "last_applied_at = datetime('now')"
            );
            try {
                $stmt->execute([$autoText, $newText]);
                $rulesAdded = 1;
            } catch (\Throwable $e) {}
        }

        echo json_encode([
            'ok' => true,
            'rules_added' => $rulesAdded,
        ], JSON_UNESCAPED_UNICODE);
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
        foreach ($rows as $row) {
            $newText = str_replace($rule['before_text'], $rule['after_text'], $row['auto_text']);
            if ($newText !== $row['auto_text']) {
                $updateStmt->execute([$newText, $row['master_fp']]);
                $applied++;
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

        // 全体合計
        $total = $db->query(
            "SELECT SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens,"
            . " COUNT(*) as count"
            . " FROM lc_api_usage"
        )->fetch(PDO::FETCH_ASSOC);

        echo json_encode([
            'daily' => $daily ?: [],
            'by_model' => $byModel ?: [],
            'by_purpose' => $byPurpose ?: [],
            'total' => $total ?: ['input_tokens' => 0, 'output_tokens' => 0, 'count' => 0],
        ], JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
    } catch (\Throwable $e) {
        echo json_encode(['daily' => [], 'by_model' => [], 'by_purpose' => [], 'total' => ['input_tokens' => 0, 'output_tokens' => 0, 'count' => 0], 'error' => $e->getMessage()]);
    }
    exit;
}

// --- delete_session アクション ---
if ($action === 'delete_session') {
    $sessionId = $_GET['session_id'] ?? '';
    if ($sessionId === '' || !($useDb && $repository instanceof EvidenceRepository)) {
        echo json_encode(['error' => 'invalid request']);
        exit;
    }
    $result = $repository->deleteSession($sessionId);
    echo json_encode($result, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
    exit;
}

// --- get_cleanable_excluded アクション ---
if ($action === 'get_cleanable_excluded') {
    if ($useDb && $repository instanceof EvidenceRepository) {
        $items = $repository->getCleanableExcluded($versionParam);
    } else {
        $items = [];
    }
    echo json_encode(
        ['items' => $items, 'count' => count($items)],
        JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR,
    );
    exit;
}

// --- cleanup_excluded アクション ---
if ($action === 'cleanup_excluded') {
    if (!($useDb && $repository instanceof EvidenceRepository)) {
        echo json_encode(['error' => 'invalid request']);
        exit;
    }
    $result = $repository->cleanupExcluded($versionParam);
    echo json_encode($result, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
    exit;
}

// --- merge_manual_group アクション ---
if ($action === 'merge_manual_group') {
    $masterFps = json_decode($_GET['master_fps'] ?? '[]', true);
    $repFp = $_GET['representative_fp'] ?? '';
    if (!$masterFps || !$repFp || !($useDb && $repository instanceof EvidenceRepository)) {
        echo json_encode(['error' => 'invalid request']);
        exit;
    }
    $result = $repository->mergeManualGroup($masterFps, $repFp, $versionParam);
    echo json_encode($result, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
    exit;
}

// --- unmerge_manual_group アクション ---
if ($action === 'unmerge_manual_group') {
    $groupId = (int)($_GET['group_id'] ?? 0);
    if ($groupId <= 0 || !($useDb && $repository instanceof EvidenceRepository)) {
        echo json_encode(['error' => 'invalid request']);
        exit;
    }
    $result = $repository->unmergeManualGroup($groupId);
    echo json_encode($result, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
    exit;
}

// --- get_manual_group_members アクション ---
if ($action === 'get_manual_group_members') {
    $groupId = (int)($_GET['group_id'] ?? 0);
    if ($groupId <= 0 || !($useDb && $repository instanceof EvidenceRepository)) {
        echo json_encode(['members' => []]);
        exit;
    }
    $members = $repository->getManualGroupMembers($groupId, $versionParam);
    echo json_encode(['members' => $members], JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
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
