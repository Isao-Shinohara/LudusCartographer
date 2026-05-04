<?php

declare(strict_types=1);

// dashboard.php (ルーター) から require される。共通変数
// ($pdo / $repository / $useDb / $action / $gameTitle /
//  $versionParam / $GEMINI_ENABLED) は _common.php で初期化済み。

use LudusCartographer\EvidenceRepository;

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
    $masterFp = $_GET['master_fp'] ?? '';
    if (!($useDb && $repository instanceof EvidenceRepository)) {
        echo json_encode(['siblings' => []]);
        exit;
    }
    // master_fp 指定時: マッピングベースの兄弟 (Final タブ用)
    if ($masterFp !== '') {
        $siblings = $repository->getMasterSiblings($masterFp);
    } elseif ($screenId > 0) {
        $siblings = $repository->getClusterSiblings($screenId);
    } else {
        $siblings = [];
    }
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
    if ($newScreenId <= 0 || !($useDb && $repository instanceof EvidenceRepository)) {
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
// --- adopt_and_rebuild アクション (Live タブ: 採用 + unmerge + 再マージ) ---
if ($action === 'adopt_and_rebuild') {
    $screenId = (int)($_GET['screen_id'] ?? 0);
    if ($screenId <= 0 || !($useDb && $repository instanceof EvidenceRepository)) {
        echo json_encode(['error' => 'invalid request']);
        exit;
    }
    // 1. 採用に戻す (is_artifact = 0, マスターノード復帰)
    $toggleResult = $repository->toggleScreenArtifact($screenId);
    if (isset($toggleResult['error'])) {
        echo json_encode($toggleResult);
        exit;
    }
    $sessionId = $toggleResult['session_id'] ?? '';
    $isSeed = $toggleResult['is_seed'] ?? false;

    // 2. バックグラウンドで unmerge → 再マージ
    $crawlerDirRaw = realpath(__DIR__ . '/../../..') . '/crawler';
    $resultFile = $crawlerDirRaw . '/storage/merge_result.json';
    @unlink($resultFile);
    $scriptFile = $crawlerDirRaw . '/storage/_adopt_rebuild.py';
    $sidEsc = addslashes($sessionId);

    if ($isSeed) {
        // Seed: 全セッション再構築 (unmerge all → re-seed → re-merge all)
        file_put_contents($scriptFile, <<<PYTHON
import json, logging, sys, os, sqlite3
sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("adopt_rebuild")
try:
    from dotenv import load_dotenv
    _env = os.path.join("config", ".env")
    if os.path.exists(_env): load_dotenv(_env)
except ImportError:
    pass
from pathlib import Path
from tools.cross_session_merger import CrossSessionMerger

def _write_progress(msg):
    conn = sqlite3.connect("storage/ludus.db")
    conn.execute("INSERT OR REPLACE INTO auto_pilot_state (key, value) VALUES ('merge_phase', ?)",
                 (json.dumps({"phase": msg, "anchors": 0, "total": 0}, ensure_ascii=False),))
    conn.commit()
    conn.close()

try:
    db = Path("storage/ludus.db")
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    # マージ済みセッション一覧 (seed 以外, built_at 順)
    merged = conn.execute(
        "SELECT sg.session_id FROM lc_session_graphs sg"
        " WHERE sg.built_at IS NOT NULL AND sg.version_id = 1"
        " ORDER BY sg.built_at ASC"
    ).fetchall()
    seed_sid = None
    other_sids = []
    for r in merged:
        nm = conn.execute(
            "SELECT match_method FROM lc_node_mappings WHERE session_id = ? ORDER BY rowid ASC LIMIT 1",
            (r["session_id"],)
        ).fetchone()
        if nm and nm["match_method"] == "seed":
            seed_sid = r["session_id"]
        else:
            other_sids.append(r["session_id"])
    conn.close()

    if not seed_sid:
        raise RuntimeError("Seed session not found")

    # 1. 全セッション unmerge (逆順)
    _write_progress("Unmerge 実行中...")
    m = CrossSessionMerger(db)
    for sid in reversed(other_sids):
        check = m.can_unmerge(sid)
        if check["ok"]:
            logger.info("Unmerge: %s", sid)
            m.unmerge_session(sid)
    m.close()

    # 2. Re-seed
    _write_progress("Seed 再構築中...")
    m = CrossSessionMerger(db)
    # seed の session_graph をリセット
    conn2 = sqlite3.connect(str(db))
    conn2.execute("DELETE FROM lc_master_nodes WHERE version_id = 1")
    conn2.execute("DELETE FROM lc_master_edges WHERE version_id = 1")
    conn2.execute("DELETE FROM lc_node_mappings WHERE version_id = 1")
    conn2.execute("UPDATE lc_session_graphs SET built_at = NULL WHERE session_id = ? AND version_id = 1", (seed_sid,))
    conn2.commit()
    conn2.close()
    n = m.merge_to_master(seed_sid)
    logger.info("Re-seed: %s → %d nodes", seed_sid, n)
    m.close()

    # 3. 再マージ (元の順序で)
    total = len(other_sids)
    for i, sid in enumerate(other_sids):
        _write_progress(f"再マージ中: {sid} ({i+1}/{total})")
        m = CrossSessionMerger(db)
        n = m.merge_to_master(sid)
        logger.info("Re-merge: %s → +%d nodes", sid, n)
        m.close()

    _write_progress("完了")
    conn3 = sqlite3.connect(str(db))
    master_nodes = conn3.execute("SELECT COUNT(*) FROM lc_master_nodes WHERE version_id = 1").fetchone()[0]
    conn3.close()
    with open(sys.argv[1], "w") as f:
        json.dump({"ok": True, "master_nodes": master_nodes, "sessions_rebuilt": total + 1}, f, ensure_ascii=False)
except Exception as e:
    import traceback
    with open(sys.argv[1], "w") as f:
        json.dump({"ok": False, "error": str(e), "trace": traceback.format_exc()}, f, ensure_ascii=False)
PYTHON);
    } else {
        // 非 Seed: 該当セッションのみ unmerge → 再マージ
        file_put_contents($scriptFile, <<<PYTHON
import json, logging, sys, os, sqlite3
sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("adopt_rebuild")
try:
    from dotenv import load_dotenv
    _env = os.path.join("config", ".env")
    if os.path.exists(_env): load_dotenv(_env)
except ImportError:
    pass
from pathlib import Path
from tools.cross_session_merger import CrossSessionMerger

def _write_progress(msg):
    conn = sqlite3.connect("storage/ludus.db")
    conn.execute("INSERT OR REPLACE INTO auto_pilot_state (key, value) VALUES ('merge_phase', ?)",
                 (json.dumps({"phase": msg, "anchors": 0, "total": 0}, ensure_ascii=False),))
    conn.commit()
    conn.close()

try:
    db = Path("storage/ludus.db")
    sid = "{$sidEsc}"

    _write_progress(f"Unmerge 実行中: {sid}")
    m = CrossSessionMerger(db)
    check = m.can_unmerge(sid)
    if check["ok"]:
        m.unmerge_session(sid)
        logger.info("Unmerge: %s", sid)
    m.close()

    _write_progress(f"再マージ中: {sid}")
    m = CrossSessionMerger(db)
    n = m.merge_to_master(sid)
    logger.info("Re-merge: %s → +%d nodes", sid, n)
    m.close()

    conn = sqlite3.connect(str(db))
    master_nodes = conn.execute("SELECT COUNT(*) FROM lc_master_nodes WHERE version_id = 1").fetchone()[0]
    conn.close()
    _write_progress("完了")
    with open(sys.argv[1], "w") as f:
        json.dump({"ok": True, "master_nodes": master_nodes, "new_nodes": n}, f, ensure_ascii=False)
except Exception as e:
    import traceback
    with open(sys.argv[1], "w") as f:
        json.dump({"ok": False, "error": str(e), "trace": traceback.format_exc()}, f, ensure_ascii=False)
PYTHON);
    }

    $bgCmd = "cd " . escapeshellarg($crawlerDirRaw)
           . " && ./venv/bin/python -B -W ignore " . escapeshellarg($scriptFile)
           . " " . escapeshellarg($resultFile)
           . " </dev/null > storage/adopt_rebuild.log 2>&1 &";
    pclose(popen($bgCmd, 'r'));
    header('Content-Type: application/json');
    echo json_encode(['started' => true, 'is_seed' => $isSeed, 'session_id' => $sessionId]);
    exit;
}
// --- check_screen_master アクション (Live タブ: 採用時の確認用) ---
if ($action === 'check_screen_master') {
    $screenId = (int)($_GET['screen_id'] ?? 0);
    if ($screenId <= 0 || !($useDb && $repository instanceof EvidenceRepository)) {
        echo json_encode(['error' => 'invalid request']);
        exit;
    }
    $result = $repository->checkScreenMaster($screenId);
    echo json_encode($result, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
    exit;
}
// --- toggle_screen_artifact アクション (Live タブ用 不採用/採用) ---
if ($action === 'toggle_screen_artifact') {
    $screenId = (int)($_GET['screen_id'] ?? 0);
    if ($screenId <= 0 || !($useDb && $repository instanceof EvidenceRepository)) {
        echo json_encode(['error' => 'invalid request']);
        exit;
    }
    $result = $repository->toggleScreenArtifact($screenId);
    echo json_encode($result, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
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
        // ocr_text_gemini は lc_master_nodes ではなく lc_screens にあるため JOIN で取得。
        $stmt = $db->prepare(
            "SELECT COALESCE(m.ocr_text_manual, '') AS prev_text, "
            . "COALESCE(s.ocr_text_gemini, s.ocr_text_hq, s.ocr_text, m.ocr_text, '') AS auto_text, "
            . "COALESCE(m.title_manual, m.title, '') AS prev_title "
            . "FROM lc_master_nodes m "
            . "LEFT JOIN lc_screens s ON s.id = m.representative_screen_id "
            . "WHERE m.master_fp = ?"
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

        // 編集された master_fp に紐づく Gemini 判定キャッシュを削除。
        // master_fp 不変だが OCR テキストが変わったので、過去の判定 (古いテキストで出した結果) は無効。
        // 次回マージで新テキストに基づいて再判定される。
        try {
            $db->prepare("DELETE FROM lc_anchor_judgments WHERE master_fp = ?")
               ->execute([$masterFp]);
        } catch (\Throwable $e) {
            // テーブル未作成等の場合はサイレントスキップ
        }

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
