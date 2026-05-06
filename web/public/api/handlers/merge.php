<?php

declare(strict_types=1);

// dashboard.php (ルーター) から require される。共通変数
// ($pdo / $repository / $useDb / $action / $gameTitle /
//  $versionParam / $GEMINI_ENABLED) は _common.php で初期化済み。

use LudusCartographer\EvidenceRepository;

// --- merge_progress アクション (結果ファイルの出現で完了判定) ---
if ($action === 'merge_progress') {
    $crawlerDir = realpath(__DIR__ . '/../../../..') . '/crawler';
    $resultFile = $crawlerDir . '/storage/merge_result.json';
    if (!file_exists($resultFile)) {
        // Phase 進捗を返す
        $dbPath = $crawlerDir . '/storage/ludus.db';
        $progress = null;
        try {
            $db = new PDO('sqlite:' . $dbPath);
            $stmt = $db->query("SELECT value FROM auto_pilot_state WHERE key = 'merge_phase'");
            $row = $stmt->fetch(PDO::FETCH_ASSOC);
            if ($row) $progress = json_decode($row['value'], true);
        } catch (Exception $e) {}
        echo json_encode(['done' => false, 'result' => null, 'progress' => $progress]);
        exit;
    }
    $raw = file_get_contents($resultFile);
    $result = json_decode($raw, true);
    if ($result === null && json_last_error() !== JSON_ERROR_NONE) {
        // JSON パース失敗 → ファイルを残してエラーを返す (書き込み途中の可能性)
        echo json_encode(['done' => false, 'result' => null, 'error' => 'JSON parse error: ' . json_last_error_msg()]);
        exit;
    }
    // 正常に読めた場合のみ削除
    @unlink($resultFile);
    echo json_encode([
        'done' => true,
        'result' => $result,
    ], JSON_UNESCAPED_UNICODE);
    exit;
}
// --- preview_merge アクション (バックグラウンド実行) ---
if ($action === 'preview_merge') {
    $sessionId = $_GET['session_id'] ?? '';
    if ($sessionId === '') {
        echo json_encode(['error' => 'session_id required']);
        exit;
    }
    $crawlerDir = escapeshellarg(realpath(__DIR__ . '/../../../..') . '/crawler');
    $resultFile = realpath(__DIR__ . '/../../../..') . '/crawler/storage/merge_result.json';
    @unlink($resultFile);
    // 二重起動防止はフロント側の withOperationLock で制御
    // スクリプトファイルに書き出してバックグラウンド実行 (クォート問題を回避)
    $scriptFile = realpath(__DIR__ . '/../../../..') . '/crawler/storage/_preview_merge.py';
    $excludeFpsPreview = $_GET['exclude_fps'] ?? '[]';
    $includeFpsPreview = $_GET['include_fps'] ?? '[]';
    file_put_contents($scriptFile, <<<PYTHON
import json, logging, sys, os, traceback
sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())
if len(sys.argv) < 2 or sys.argv[1].startswith("<") or "object at 0x" in sys.argv[1]:
    sys.stderr.write("[ERROR] invalid output path: %r\\n" % (sys.argv[1] if len(sys.argv) > 1 else None,))
    sys.exit(1)
logging.basicConfig(level=logging.INFO, format='%(message)s', stream=sys.stderr)
try:
    from dotenv import load_dotenv
    _env = os.path.join("config", ".env")
    if os.path.exists(_env): load_dotenv(_env)
except ImportError:
    pass
try:
    from pathlib import Path
    from tools.cross_session_merger import CrossSessionMerger
    exclude_fps = set(json.loads('{$excludeFpsPreview}'))
    include_fps = set(json.loads('{$includeFpsPreview}'))
    m = CrossSessionMerger(Path("storage/ludus.db"))
    r = m.preview_merge("{$sessionId}", exclude_fps=exclude_fps or None, include_fps=include_fps or None)
    m.close()
    print("[preview] JSON 書き出し開始...", file=sys.stderr)
    with open(sys.argv[1], "w") as f:
        json.dump(r, f, ensure_ascii=False)
    print(f"[preview] 完了: {os.path.getsize(sys.argv[1])} bytes", file=sys.stderr)
except Exception:
    traceback.print_exc(file=sys.stderr)
    sys.exit(1)
PYTHON);
    $crawlerDirRaw = realpath(__DIR__ . '/../../../..') . '/crawler';
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
    $crawlerDir = escapeshellarg(realpath(__DIR__ . '/../../../..') . '/crawler');
    $resultFile = realpath(__DIR__ . '/../../../..') . '/crawler/storage/merge_result.json';
    @unlink($resultFile);
    $scriptFile = realpath(__DIR__ . '/../../../..') . '/crawler/storage/_execute_merge.py';
    $excludeFps = $_GET['exclude_fps'] ?? '[]';
    $includeFps = $_GET['include_fps'] ?? '[]';
    file_put_contents($scriptFile, <<<PYTHON
import json, logging, sys, os
sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())
if len(sys.argv) < 2 or sys.argv[1].startswith("<") or "object at 0x" in sys.argv[1]:
    sys.stderr.write("[ERROR] invalid output path: %r\\n" % (sys.argv[1] if len(sys.argv) > 1 else None,))
    sys.exit(1)
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
try:
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
except Exception as e:
    with open(sys.argv[1], "w") as f:
        json.dump({"ok": False, "error": str(e)}, f, ensure_ascii=False)
PYTHON);
    $crawlerDirRaw = realpath(__DIR__ . '/../../../..') . '/crawler';
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
        escapeshellarg(realpath(__DIR__ . '/../../../..') . '/crawler'),
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
        escapeshellarg(realpath(__DIR__ . '/../../../..') . '/crawler'),
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
