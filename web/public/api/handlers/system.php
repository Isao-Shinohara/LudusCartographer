<?php

declare(strict_types=1);

// dashboard.php (ルーター) から require される。共通変数
// ($pdo / $repository / $useDb / $action / $gameTitle /
//  $versionParam / $GEMINI_ENABLED) は _common.php で初期化済み。

use LudusCartographer\EvidenceRepository;

// --- heartbeat アクション (ダッシュボード生存通知) ---
if ($action === 'heartbeat') {
    $hbFile = sys_get_temp_dir() . '/lc_dashboard_heartbeat';
    file_put_contents($hbFile, (string)time());
    echo json_encode(['ok' => true, 'ts' => time()]);
    exit;
}
