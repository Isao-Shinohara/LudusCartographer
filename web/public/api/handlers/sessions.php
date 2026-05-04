<?php

declare(strict_types=1);

// dashboard.php (ルーター) から require される。共通変数
// ($pdo / $repository / $useDb / $action / $gameTitle /
//  $versionParam / $GEMINI_ENABLED) は _common.php で初期化済み。

use LudusCartographer\EvidenceRepository;

// --- get_sessions アクション ---
if ($action === 'get_sessions') {
    $limit = min((int)($_GET['limit'] ?? 20), 100);

    $sessions = $useDb
        ? $repository->getSessions($limit, $gameTitle, $versionParam)
        : [];

    echo json_encode(
        ['sessions' => $sessions, 'count' => count($sessions)],
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
// --- process_session_bg アクション (バックグラウンド未完了の手動完了) ---
if ($action === 'process_session_bg') {
    $sessionId = $_GET['session_id'] ?? '';
    if ($sessionId === '') {
        echo json_encode(['error' => 'session_id required']);
        exit;
    }
    $crawlerDirRaw = realpath(__DIR__ . '/../../..') . '/crawler';
    $crawlerDir = escapeshellarg($crawlerDirRaw);
    $resultFile = $crawlerDirRaw . '/storage/merge_result.json';
    $dbPath = $crawlerDirRaw . '/storage/ludus.db';

    // ❶ ロックチェック: 二重起動防止 (DB 同時書き込み回避)
    try {
        $lockDb = new PDO('sqlite:' . $dbPath);
        $lockDb->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
        $stmt = $lockDb->prepare("SELECT value, updated_at FROM auto_pilot_state WHERE key = 'process_session_bg_lock'");
        $stmt->execute();
        $lockRow = $stmt->fetch(PDO::FETCH_ASSOC);
        if ($lockRow) {
            $lockData = json_decode($lockRow['value'] ?? '', true) ?: [];
            $lockPid = (int)($lockData['pid'] ?? 0);
            $lockAge = max(0, time() - strtotime(($lockRow['updated_at'] ?? '') . ' UTC'));
            $isBusy = false;
            if ($lockPid > 0) {
                if (function_exists('posix_kill')) {
                    $isBusy = @posix_kill($lockPid, 0);
                } else {
                    $isBusy = $lockAge < 600;
                }
            } elseif ($lockAge < 30) {
                $isBusy = true;
            }
            if ($isBusy) {
                header('Content-Type: application/json');
                echo json_encode([
                    'error' => 'already_running',
                    'pid' => $lockPid,
                    'age_sec' => $lockAge,
                    'session_id' => $lockData['session_id'] ?? null,
                ]);
                exit;
            }
        }
        $stmt = $lockDb->prepare(
            "INSERT INTO auto_pilot_state (key, value, updated_at) VALUES ('process_session_bg_lock', ?, CURRENT_TIMESTAMP)
             ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP"
        );
        $stmt->execute([json_encode([
            'pid' => 0,
            'session_id' => $sessionId,
            'phase' => 'starting',
        ])]);
        $lockDb = null;
    } catch (\Throwable $e) {
        header('Content-Type: application/json');
        echo json_encode(['error' => 'lock_failed', 'message' => $e->getMessage()]);
        exit;
    }

    @unlink($resultFile);
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
    # PHP merge_progress action は 'merge_phase' キーを参照する。
    # 既存の adopt_rebuild と同じキーを使い、ダッシュボードに進捗を確実に届ける。
    c = sqlite3.connect(str(DB))
    c.execute("INSERT OR REPLACE INTO auto_pilot_state (key, value) VALUES ('merge_phase', ?)",
              (json.dumps({"phase": phase, "total": total, "ocr_done": total - remaining_g, "ocr_total": total, "clustering_remaining": remaining_d}),))
    c.commit()
    c.close()
def _set_lock_phase(phase):
    c = sqlite3.connect(str(DB))
    c.execute("INSERT OR REPLACE INTO auto_pilot_state(key, value, updated_at) VALUES('process_session_bg_lock', ?, CURRENT_TIMESTAMP)",
              (json.dumps({"pid": os.getpid(), "session_id": SID, "phase": phase}),))
    c.commit()
    c.close()
def _release_lock():
    try:
        c = sqlite3.connect(str(DB))
        c.execute("DELETE FROM auto_pilot_state WHERE key = 'process_session_bg_lock'")
        c.commit()
        c.close()
    except Exception:
        pass
_set_lock_phase("starting")
try:
    w = BackgroundWorker(db_path=DB, session_id=SID)
    MAX_ITERATIONS = 100
    # 2 iter 連続で g が減らなければ sentinel で打ち切り (= 3 iter 目で発動)。
    # Gemini API 障害時の待ち時間を最小化。
    NO_PROGRESS_LIMIT = 2
    iters = 0
    total, g, d = _pending()
    prev_g = None
    no_progress = 0
    aborted_unrecoverable = 0
    while iters < MAX_ITERATIONS:
        iters += 1
        w._run_incremental_clustering()
        w._run_gemini_batch_correction()
        total, g, d = _pending()
        _update_progress("OCR", total, total - g, g, d)
        if g == 0 and d == 0:
            break
        if prev_g is not None and g == prev_g and g > 0:
            no_progress += 1
            if no_progress >= NO_PROGRESS_LIMIT:
                _c = sqlite3.connect(str(DB))
                _cur = _c.execute(
                    "UPDATE lc_screens SET ocr_text_gemini = ''"
                    " WHERE session_id = ? AND is_representative = 1 AND ocr_text_gemini IS NULL",
                    (SID,))
                aborted_unrecoverable = _cur.rowcount
                _c.commit()
                _c.close()
                total, g, d = _pending()
                break
        else:
            no_progress = 0
        prev_g = g
    _set_lock_phase("synthesizing_edges")
    _update_progress("synthesizing_edges", total, total, 0, 0)
    w._run_incremental_clustering()
    w._synthesize_auto_edges()
    _set_lock_phase("building_graph")
    _update_progress("building_graph", total, total, 0, 0)
    bp = BatchProcessor(db_path=DB)
    sccs = bp.build_graph(session_id=SID)
    bp.close()
    total, g, d = _pending()
    print(json.dumps({
        "ok": True, "sccs": sccs, "iterations": iters,
        "remaining_gemini": g, "remaining_clustering": d,
        "aborted_unrecoverable": aborted_unrecoverable,
    }, ensure_ascii=False))
finally:
    _release_lock()
PYTHON;
    $cmd = sprintf(
        'cd %s && exec 3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&- && ./venv/bin/python -B -W ignore -c %s > %s 2>>storage/process_session_bg.err.log </dev/null &',
        $crawlerDir,
        escapeshellarg($script),
        escapeshellarg($resultFile),
    );
    pclose(popen($cmd, 'r'));
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
// --- delete_session アクション ---
if ($action === 'delete_session') {
    $sessionId = $_GET['session_id'] ?? '';
    if ($sessionId === '' || !($useDb && $repository instanceof EvidenceRepository)) {
        echo json_encode(['error' => 'invalid request']);
        exit;
    }
    $result = $repository->deleteSession($sessionId);
    // セッションディレクトリが空なら削除
    if ($result['ok'] ?? false) {
        $crawlerDir = realpath(__DIR__ . '/../../..') . '/crawler';
        $imageDirs = [
            $crawlerDir . '/storage/screenshots/',
            $crawlerDir . '/storage/evidence/',
            $crawlerDir . '/evidence/',
        ];
        foreach ($imageDirs as $base) {
            $dir = $base . $sessionId;
            if (is_dir($dir)) {
                // ファイルが残っていなければディレクトリ削除
                $remaining = glob("$dir/*");
                if (empty($remaining)) {
                    @rmdir($dir);
                }
            }
        }
    }
    echo json_encode($result, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
    exit;
}
