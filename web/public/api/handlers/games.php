<?php

declare(strict_types=1);

// dashboard.php (ルーター) から require される。共通変数
// ($pdo / $repository / $useDb / $action / $gameTitle /
//  $versionParam / $GEMINI_ENABLED) は _common.php で初期化済み。

use LudusCartographer\EvidenceRepository;

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
// --- get_coverage アクション ---
if ($action === 'get_coverage') {
    $coverage = ($useDb && $repository instanceof EvidenceRepository)
        ? $repository->getProjectCoverage($gameTitle)
        : ['unique_screens' => 0, 'max_depth_reached' => 0, 'total_sessions' => 0];
    echo json_encode($coverage, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
    exit;
}
