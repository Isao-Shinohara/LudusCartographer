<?php

declare(strict_types=1);

// dashboard.php (ルーター) から require される。共通変数
// ($pdo / $repository / $useDb / $action / $gameTitle /
//  $versionParam / $GEMINI_ENABLED) は _common.php で初期化済み。

use LudusCartographer\EvidenceRepository;

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
