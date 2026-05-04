<?php

declare(strict_types=1);

// dashboard.php (ルーター) から require される。共通変数
// ($pdo / $repository / $useDb / $action / $gameTitle /
//  $versionParam / $GEMINI_ENABLED) は _common.php で初期化済み。

use LudusCartographer\EvidenceRepository;

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

// --- finalize_session アクション (paused → completed; マージタブに移動) ---
if ($action === 'finalize_session') {
    $sessionId = trim($_GET['session_id'] ?? '');
    if ($sessionId === '' || !($useDb && $repository instanceof EvidenceRepository)) {
        echo json_encode(['error' => 'session_id required']);
        exit;
    }
    $result = $repository->finalizeSession($sessionId);
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
