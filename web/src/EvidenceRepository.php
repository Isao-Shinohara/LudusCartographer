<?php

declare(strict_types=1);

namespace LudusCartographer;

use PDO;

/**
 * SQLite evidence DB (crawler/storage/ludus.db) 用リポジトリ。
 *
 * ScreenRepository と同一のメソッドシグネチャを持ち、
 * search.php / index.php が MySQL / SQLite どちらでも同じコードで動作できるようにする。
 */
class EvidenceRepository
{
    public function __construct(private PDO $db) {}

    public function getPdo(): PDO
    {
        return $this->db;
    }

    // ------------------------------------------------------------------
    // version management
    // ------------------------------------------------------------------

    /**
     * アクティブな version_id を返す（フォールバック: 1）。
     */
    public function getActiveVersionId(): int
    {
        $stmt = $this->db->query("SELECT id FROM lc_versions WHERE is_active = 1 LIMIT 1");
        $id = $stmt ? $stmt->fetchColumn() : false;
        return $id !== false ? (int)$id : 1;
    }

    /**
     * 全バージョン一覧を返す。
     *
     * @return array<int, array<string, mixed>>
     */
    public function getVersions(): array
    {
        return $this->db->query(
            "SELECT id, name, created_at, is_active FROM lc_versions ORDER BY id DESC"
        )->fetchAll(\PDO::FETCH_ASSOC);
    }

    /**
     * 新しいバージョンを作成して返す。
     *
     * @return array<string, mixed>
     */
    public function createVersion(string $name): array
    {
        $stmt = $this->db->prepare(
            "INSERT INTO lc_versions (name, is_active) VALUES (:name, 0)"
        );
        $stmt->execute([':name' => $name]);
        $id = (int)$this->db->lastInsertId();
        return ['id' => $id, 'name' => $name, 'is_active' => 0];
    }

    /**
     * 指定バージョンをアクティブにする（他は非アクティブ）。
     * 切替前に旧アクティブバージョンの running セッションを完了扱いにする。
     *
     * @return array<string, mixed>
     */
    public function activateVersion(int $versionId): array
    {
        // 旧アクティブバージョンの running セッションを強制完了
        $oldVersionId = $this->getActiveVersionId();
        if ($oldVersionId !== $versionId) {
            $this->db->prepare(
                "UPDATE lc_sessions SET status = 'completed', completion_type = 'version_switch'"
                . " WHERE status = 'running' AND version_id = :old_vid"
            )->execute([':old_vid' => $oldVersionId]);
        }

        // 全バージョンを非アクティブ
        $this->db->exec("UPDATE lc_versions SET is_active = 0");

        // 指定バージョンをアクティブ
        $this->db->prepare(
            "UPDATE lc_versions SET is_active = 1 WHERE id = :vid"
        )->execute([':vid' => $versionId]);

        // 返却
        $stmt = $this->db->prepare(
            "SELECT id, name, created_at, is_active FROM lc_versions WHERE id = :vid"
        );
        $stmt->execute([':vid' => $versionId]);
        $row = $stmt->fetch(\PDO::FETCH_ASSOC);
        return $row ?: ['error' => 'version not found'];
    }

    // ------------------------------------------------------------------
    // public API
    // ------------------------------------------------------------------

    /**
     * DB に存在するゲームタイトルの一覧を返す（ヘッダーセレクター用）。
     *
     * @return string[]
     */
    public function getGameTitles(?int $versionId = null): array
    {
        $where = "WHERE game_title IS NOT NULL";
        $bindings = [];
        if ($versionId !== null) {
            $where .= " AND version_id = :vid";
            $bindings[':vid'] = $versionId;
        }
        $stmt = $this->db->prepare(
            "SELECT DISTINCT game_title FROM lc_sessions {$where} ORDER BY game_title"
        );
        foreach ($bindings as $k => $v) {
            $stmt->bindValue($k, $v, PDO::PARAM_INT);
        }
        $stmt->execute();
        return array_column($stmt->fetchAll(), 'game_title');
    }

    /**
     * キーワードとゲームタイトルでスクリーンを検索する。
     *
     * @return array<int, array<string, mixed>>
     */
    public function search(
        string $keyword   = '',
        int    $limit     = 50,
        string $gameTitle = '',
    ): array {
        [$where, $bindings] = $this->buildScreenWhere($keyword, '', $gameTitle);

        $sql = <<<SQL
            SELECT s.id, s.title, s.depth, s.screenshot_path,
                   s.ocr_text, s.discovered_at, s.session_id, s.fingerprint,
                   COALESCE(sess.game_title, 'Unknown Game') AS game_title
            FROM lc_screens s
            LEFT JOIN lc_sessions sess ON sess.session_id = s.session_id
            {$where}
            ORDER BY s.discovered_at DESC
            LIMIT :limit
        SQL;

        $bindings[':limit'] = $limit;
        $stmt = $this->db->prepare($sql);
        foreach ($bindings as $key => $value) {
            $type = ($key === ':limit') ? PDO::PARAM_INT : PDO::PARAM_STR;
            $stmt->bindValue($key, $value, $type);
        }
        $stmt->execute();
        return array_map([$this, 'toScreenArray'], $stmt->fetchAll());
    }

    /**
     * title / keyword / session_id / game_title の複合条件でスクリーンを検索する。
     *
     * @return array<int, array<string, mixed>>
     */
    public function searchAdvanced(
        string $title     = '',
        string $keyword   = '',
        string $sessionId = '',
        int    $limit     = 100,
        string $gameTitle = '',
    ): array {
        [$where, $bindings] = $this->buildScreenWhere($keyword, $sessionId, $gameTitle);

        // title 条件を追加
        if ($title !== '') {
            $cond = 's.title LIKE :title';
            $where = ($where === '') ? "WHERE {$cond}" : "{$where} AND {$cond}";
            $bindings[':title'] = '%' . $title . '%';
        }

        $sql = <<<SQL
            SELECT s.id, s.title, s.depth, s.screenshot_path,
                   s.ocr_text, s.discovered_at, s.session_id, s.fingerprint,
                   COALESCE(sess.game_title, 'Unknown Game') AS game_title
            FROM lc_screens s
            LEFT JOIN lc_sessions sess ON sess.session_id = s.session_id
            {$where}
            ORDER BY s.discovered_at DESC
            LIMIT :limit
        SQL;

        $bindings[':limit'] = $limit;
        $stmt = $this->db->prepare($sql);
        foreach ($bindings as $key => $value) {
            $type = ($key === ':limit') ? PDO::PARAM_INT : PDO::PARAM_STR;
            $stmt->bindValue($key, $value, $type);
        }
        $stmt->execute();
        return array_map([$this, 'toScreenArray'], $stmt->fetchAll());
    }

    /**
     * @return array{screen: array<string,mixed>|null, elements: array<int,array<string,mixed>>, parents: array<int,array<string,mixed>>}
     */
    public function findWithElements(int $screenId): array
    {
        $stmt = $this->db->prepare(<<<SQL
            SELECT s.id, s.title, s.depth, s.screenshot_path, s.ocr_text,
                   s.discovered_at, s.session_id, s.fingerprint, s.parent_fp,
                   COALESCE(sess.game_title, 'Unknown Game') AS game_title
            FROM lc_screens s
            LEFT JOIN lc_sessions sess ON sess.session_id = s.session_id
            WHERE s.id = :id
        SQL);
        $stmt->execute([':id' => $screenId]);
        $raw = $stmt->fetch() ?: null;

        $screen = $raw ? $this->toScreenArray($raw) : null;

        // タップ候補を UI 要素として返す
        $elements = [];
        if ($raw) {
            $stmt = $this->db->prepare(<<<SQL
                SELECT text AS label, 'button' AS element_type, NULL AS navigates_to_name
                FROM lc_tappable_items WHERE screen_id = :id ORDER BY id
            SQL);
            $stmt->execute([':id' => $screenId]);
            $elements = $stmt->fetchAll();
        }

        // 親画面（この画面の parent_fp が指す画面）
        $parents = [];
        if ($raw && $raw['parent_fp'] !== null) {
            $stmt = $this->db->prepare(<<<SQL
                SELECT s.id, s.title AS name, s.fingerprint AS screen_hash, NULL AS via_label,
                       COALESCE(sess.game_title, 'Unknown Game') AS game_title
                FROM lc_screens s
                LEFT JOIN lc_sessions sess ON sess.session_id = s.session_id
                WHERE s.fingerprint = :fp AND s.session_id = :sid
                LIMIT 1
            SQL);
            $stmt->execute([':fp' => $raw['parent_fp'], ':sid' => $raw['session_id']]);
            $parents = $stmt->fetchAll();
        }

        return compact('screen', 'elements', 'parents');
    }

    /**
     * クロールセッション一覧を返す。
     *
     * @return array<int, array<string, mixed>>
     */
    public function getSessions(int $limit = 20, string $gameTitle = '', ?int $versionId = null): array
    {
        $conditions = ["status != 'archived'"];
        $bindings   = [':limit' => $limit];

        if ($gameTitle !== '') {
            $conditions[]          = 'game_title = :game_title';
            $bindings[':game_title'] = $gameTitle;
        }
        if ($versionId !== null) {
            $conditions[]      = 'version_id = :vid';
            $bindings[':vid']  = $versionId;
        }

        $where = $conditions ? 'WHERE ' . implode(' AND ', $conditions) : '';

        $sql = <<<SQL
            SELECT id,
                   COALESCE(game_title, session_id) AS game_name,
                   game_title,
                   COALESCE(device_mode, 'SIMULATOR') AS device_mode,
                   'ios'  AS platform,
                   status,
                   screens_found,
                   started_at,
                   NULL   AS ended_at,
                   NULL   AS error_message,
                   session_id AS session_dir
            FROM lc_sessions
            {$where}
            ORDER BY started_at DESC
            LIMIT :limit
        SQL;

        $stmt = $this->db->prepare($sql);
        foreach ($bindings as $key => $value) {
            $type = ($key === ':limit' || $key === ':vid') ? PDO::PARAM_INT : PDO::PARAM_STR;
            $stmt->bindValue($key, $value, $type);
        }
        $stmt->execute();
        return $stmt->fetchAll();
    }

    /**
     * game_title の探索網羅率サマリーを返す。
     *
     * @return array{unique_screens: int, max_depth_reached: int, total_sessions: int}
     */
    public function getProjectCoverage(string $gameTitle): array
    {
        $stmt = $this->db->prepare(<<<SQL
            SELECT COUNT(DISTINCT s.fingerprint) AS unique_screens,
                   COALESCE(MAX(s.depth), 0)     AS max_depth_reached,
                   COUNT(DISTINCT sess.session_id) AS total_sessions
            FROM lc_screens s
            JOIN lc_sessions sess ON sess.session_id = s.session_id
            WHERE sess.game_title = :game_title
        SQL);
        $stmt->bindValue(':game_title', $gameTitle, PDO::PARAM_STR);
        $stmt->execute();
        $row = $stmt->fetch();
        return [
            'unique_screens'    => (int)($row['unique_screens']    ?? 0),
            'max_depth_reached' => (int)($row['max_depth_reached'] ?? 0),
            'total_sessions'    => (int)($row['total_sessions']    ?? 0),
        ];
    }

    /**
     * game_title に属する全セッションのユニーク画面を返す（fingerprint でデデュープ）。
     *
     * 同じ fingerprint が複数セッションに存在する場合は、最初に発見された画面のみ返す。
     * depth 昇順・発見日時昇順でソートするため、マップ全体の俯瞰に最適。
     *
     * @return array<int, array<string, mixed>>
     */
    public function getProjectScreens(string $gameTitle, int $limit = 100): array
    {
        $stmt = $this->db->prepare(<<<SQL
            SELECT MIN(s.id) AS id, s.fingerprint,
                   s.title, MIN(s.depth) AS depth,
                   s.screenshot_path, s.ocr_text,
                   MIN(s.discovered_at) AS discovered_at, s.session_id,
                   COALESCE(sess.game_title, 'Unknown Game') AS game_title
            FROM lc_screens s
            LEFT JOIN lc_sessions sess ON sess.session_id = s.session_id
            WHERE sess.game_title = :game_title
            GROUP BY s.fingerprint
            ORDER BY MIN(s.depth) ASC, MIN(s.discovered_at) ASC
            LIMIT :limit
        SQL);
        $stmt->bindValue(':game_title', $gameTitle, PDO::PARAM_STR);
        $stmt->bindValue(':limit',      $limit,     PDO::PARAM_INT);
        $stmt->execute();
        return array_map([$this, 'toScreenArray'], $stmt->fetchAll());
    }

    /**
     * 最新のスクリーンを discovered_at DESC で返す（ダッシュボード用）。
     *
     * @return array<int, array<string, mixed>>
     */
    public function getRecentScreens(
        int    $limit     = 50,
        string $gameTitle = '',
        int    $afterId   = 0,
        string $sessionId = '',
        ?int   $versionId = null,
    ): array {
        $conditions = ["COALESCE(sess.status, '') != 'archived'"];
        $bindings   = [':limit' => $limit];

        if ($gameTitle !== '') {
            $conditions[]             = 'sess.game_title = :game_title';
            $bindings[':game_title']  = $gameTitle;
        }
        if ($afterId > 0) {
            $conditions[]          = 's.id > :after_id';
            $bindings[':after_id'] = $afterId;
        }
        if ($sessionId !== '') {
            $conditions[]            = 's.session_id = :session_id';
            $bindings[':session_id'] = $sessionId;
        }
        if ($versionId !== null) {
            $conditions[]      = 'sess.version_id = :vid';
            $bindings[':vid']  = $versionId;
        }

        $where = $conditions ? 'WHERE ' . implode(' AND ', $conditions) : '';

        $sql = <<<SQL
            SELECT s.id, s.title, s.depth, s.screenshot_path, s.thumbnail_path,
                   s.ocr_text, s.ocr_text_hq, s.ocr_text_gemini, s.is_artifact,
                   s.discovered_at, s.session_id,
                   s.fingerprint, s.scene,
                   s.is_representative, s.cluster_id,
                   COALESCE(sess.game_title, 'Unknown Game') AS game_title
            FROM lc_screens s
            LEFT JOIN lc_sessions sess ON sess.session_id = s.session_id
            {$where}
            ORDER BY s.id DESC
            LIMIT :limit
        SQL;

        $stmt = $this->db->prepare($sql);
        foreach ($bindings as $key => $value) {
            $type = ($key === ':limit' || $key === ':after_id' || $key === ':vid') ? PDO::PARAM_INT : PDO::PARAM_STR;
            $stmt->bindValue($key, $value, $type);
        }
        $stmt->execute();
        return array_map([$this, 'toScreenArray'], $stmt->fetchAll());
    }

    // ------------------------------------------------------------------
    // private helpers
    // ------------------------------------------------------------------

    /**
     * lc_screens クエリ用の WHERE 句とバインド値を構築する。
     *
     * @return array{string, array<string, mixed>}  [WHERE句, bindings]
     */
    private function buildScreenWhere(
        string $keyword   = '',
        string $sessionId = '',
        string $gameTitle = '',
    ): array {
        $conditions = [];
        $bindings   = [];

        if ($keyword !== '') {
            $conditions[]        = '(s.title LIKE :kw OR s.ocr_text LIKE :kw2)';
            $bindings[':kw']     = '%' . $keyword . '%';
            $bindings[':kw2']    = '%' . $keyword . '%';
        }

        if ($sessionId !== '') {
            $conditions[]           = 's.session_id = :session';
            $bindings[':session']   = $sessionId;
        }

        if ($gameTitle !== '') {
            $conditions[]             = 'sess.game_title = :game_title';
            $bindings[':game_title']  = $gameTitle;
        }

        $where = $conditions ? 'WHERE ' . implode(' AND ', $conditions) : '';
        return [$where, $bindings];
    }

    /** lc_screens の raw 行を API 出力フォーマットに変換する。 */
    /**
     * 決定版 (final/) の代表画像を取得する。
     * is_representative=1 かつ session 横断で fingerprint 重複排除。
     */
    public function getFinalScreens(
        int    $limit     = 10000,
        string $gameTitle = '',
        ?int   $versionId = null,
    ): array {
        $versionId ??= $this->getActiveVersionId();
        $bindings = [':limit' => $limit, ':vid' => $versionId];

        // lc_master_nodes (クロスセッションマージ済み) から取得
        $gameFilter = '';
        if ($gameTitle !== '') {
            $gameFilter = 'AND COALESCE(sess.game_title, \'Unknown Game\') = :game_title';
            $bindings[':game_title'] = $gameTitle;
        }

        $sql = <<<SQL
            SELECT s.id, COALESCE(m.title_manual, m.title) AS title, s.depth,
                   s.screenshot_path, s.thumbnail_path,
                   s.ocr_text, s.ocr_text_hq, s.ocr_text_gemini, s.is_artifact,
                   m.ocr_text_manual, m.title_manual, m.manual_edited_at,
                   m.last_seen_at AS discovered_at,
                   s.session_id, m.master_fp AS fingerprint, m.scene,
                   COALESCE(sess.game_title, 'Unknown Game') AS game_title,
                   m.visit_count, m.bfs_depth,
                   1 AS is_representative, s.cluster_id,
                   m.user_excluded, m.master_fp,
                   m.manual_group_id, m.is_group_representative,
                   (SELECT GROUP_CONCAT(nm2.match_method || ':' || nm2.session_id, ',')
                    FROM lc_node_mappings nm2
                    WHERE nm2.master_fp = m.master_fp AND nm2.match_method != 'seed' AND nm2.match_method != 'new'
                   ) AS anchor_info,
                   (SELECT nm3.match_method FROM lc_node_mappings nm3
                    WHERE nm3.master_fp = m.master_fp AND nm3.match_method != 'seed'
                    ORDER BY nm3.rowid DESC LIMIT 1
                   ) AS last_match_method
            FROM lc_master_nodes m
            JOIN lc_screens s ON s.id = m.representative_screen_id
            LEFT JOIN lc_sessions sess ON sess.session_id = s.session_id
            WHERE m.user_excluded = 0 AND m.is_group_representative = 1
              AND m.version_id = :vid {$gameFilter}
            ORDER BY m.sort_order ASC
            LIMIT :limit
        SQL;

        $stmt = $this->db->prepare($sql);
        foreach ($bindings as $key => $value) {
            $type = ($key === ':limit' || $key === ':vid') ? PDO::PARAM_INT : PDO::PARAM_STR;
            $stmt->bindValue($key, $value, $type);
        }
        $stmt->execute();
        return array_map([$this, 'toScreenArray'], $stmt->fetchAll());
    }

    private function toScreenArray(array $raw): array
    {
        return [
            'id'              => $raw['id'],
            'name'            => $raw['title'],
            'category'        => $raw['scene'] ?? ('depth=' . $raw['depth']),
            'screenshot_path' => $raw['screenshot_path'],
            'thumbnail_path'  => $raw['thumbnail_path'] ?? null,
            'ocr_text'        => ($raw['ocr_text_manual'] ?? null) ?: (isset($raw['ocr_text_gemini']) && $raw['ocr_text_gemini'] !== null ? $raw['ocr_text_gemini'] : (($raw['ocr_text_hq'] ?? null) ?: ($raw['ocr_text'] ?? ''))),
            'ocr_text_raw'    => $raw['ocr_text'] ?? '',
            'ocr_text_hq'     => $raw['ocr_text_hq'] ?? null,
            'ocr_text_gemini' => $raw['ocr_text_gemini'] ?? null,
            'ocr_text_manual' => $raw['ocr_text_manual'] ?? null,
            'title_manual'    => $raw['title_manual'] ?? null,
            'manual_edited_at' => $raw['manual_edited_at'] ?? null,
            'has_manual'      => !empty($raw['ocr_text_manual'] ?? null),
            'visited_count'   => 1,
            'last_seen_at'    => $raw['discovered_at'],
            'game_name'       => $raw['game_title'] ?? $raw['session_id'],
            'platform'        => 'ios',
            'screen_hash'     => $raw['fingerprint'],
            'game_title'      => $raw['game_title'] ?? 'Unknown Game',
            'session_id'      => $raw['session_id'] ?? '',
            'is_representative' => (bool)($raw['is_representative'] ?? false),
            'cluster_id'      => $raw['cluster_id'] ?? null,
            'has_hq_ocr'      => ($raw['ocr_text_hq'] ?? null) !== null,
            'has_gemini'      => ($raw['ocr_text_gemini'] ?? null) !== null,
            'user_excluded'   => (bool)($raw['user_excluded'] ?? false),
            'master_fp'       => $raw['master_fp'] ?? null,
            'manual_group_id' => $raw['manual_group_id'] ?? null,
            'is_artifact'     => (bool)($raw['is_artifact'] ?? false),
            'anchor_info'     => $raw['anchor_info'] ?? null,
            'last_match_method' => $raw['last_match_method'] ?? null,
        ];
    }

    // ─── マージ管理 ─────────────────────────────────

    public function getPendingMerges(?int $versionId = null): array
    {
        $versionId ??= $this->getActiveVersionId();
        $sql = <<<SQL
            SELECT sg.session_id, sg.node_count, sg.edge_count, sg.built_at,
                   s.screens_found, s.started_at, s.status, s.completion_type,
                   COALESCE(s.game_title, 'Unknown Game') AS game_title
            FROM lc_session_graphs sg
            JOIN lc_sessions s ON s.session_id = sg.session_id
            WHERE s.status = 'completed'
              AND s.version_id = :vid
              AND NOT EXISTS (
                SELECT 1 FROM lc_node_mappings nm
                WHERE nm.session_id = sg.session_id
              )
            ORDER BY s.started_at ASC
        SQL;
        $stmt = $this->db->prepare($sql);
        $stmt->bindValue(':vid', $versionId, \PDO::PARAM_INT);
        $stmt->execute();
        return $stmt->fetchAll(\PDO::FETCH_ASSOC);
    }

    public function getMergedSessions(?int $versionId = null): array
    {
        $versionId ??= $this->getActiveVersionId();
        $sql = <<<SQL
            SELECT sg.session_id, sg.node_count, sg.edge_count, sg.built_at,
                   s.screens_found, s.started_at, s.completion_type,
                   COALESCE(s.game_title, 'Unknown Game') AS game_title,
                   COUNT(nm.id) AS mapped_nodes,
                   SUM(CASE WHEN nm.match_method = 'new' THEN 1 ELSE 0 END) AS new_nodes,
                   SUM(CASE WHEN nm.match_method != 'new' THEN 1 ELSE 0 END) AS anchor_nodes
            FROM lc_session_graphs sg
            JOIN lc_sessions s ON s.session_id = sg.session_id
            JOIN lc_node_mappings nm ON nm.session_id = sg.session_id
            WHERE s.status != 'archived'
              AND s.version_id = :vid
            GROUP BY sg.session_id
            ORDER BY s.started_at DESC
        SQL;
        $stmt = $this->db->prepare($sql);
        $stmt->bindValue(':vid', $versionId, \PDO::PARAM_INT);
        $stmt->execute();
        return $stmt->fetchAll(\PDO::FETCH_ASSOC);
    }

    public function getEmptySessions(?int $versionId = null): array
    {
        $versionId ??= $this->getActiveVersionId();
        // 完了済みだが session_graph がないセッション (画面ありで遷移ありのもの = グラフ構築可能)
        // または画面なし (削除のみ可能)
        $sql = <<<SQL
            SELECT s.session_id, s.started_at, s.screens_found, s.status, s.completion_type,
                   COALESCE(s.game_title, 'Unknown Game') AS game_title,
                   (SELECT COUNT(*) FROM lc_screens WHERE session_id = s.session_id) AS actual_screens,
                   (SELECT COUNT(*) FROM lc_transitions WHERE session_id = s.session_id AND to_fp IS NOT NULL) AS transitions
            FROM lc_sessions s
            WHERE s.status = 'completed'
              AND s.version_id = :vid
              AND NOT EXISTS (
                SELECT 1 FROM lc_session_graphs sg WHERE sg.session_id = s.session_id
              )
            ORDER BY s.started_at DESC
        SQL;
        $stmt = $this->db->prepare($sql);
        $stmt->bindValue(':vid', $versionId, \PDO::PARAM_INT);
        $stmt->execute();
        $rows = $stmt->fetchAll(\PDO::FETCH_ASSOC);
        // 遷移データなしのものは getNoTransitionSessions で扱うため除外
        // (画面あり+遷移0 = 遷移データなし)
        return array_values(array_filter($rows, function ($r) {
            return (int)$r['actual_screens'] === 0 || (int)$r['transitions'] > 0;
        }));
    }

    public function getNoTransitionSessions(?int $versionId = null): array
    {
        $versionId ??= $this->getActiveVersionId();
        // 画面はあるが遷移データなし → グラフ構築不可、削除のみ可能
        $sql = <<<SQL
            SELECT s.session_id, s.started_at, s.screens_found, s.completion_type,
                   COALESCE(s.game_title, 'Unknown Game') AS game_title,
                   (SELECT COUNT(*) FROM lc_screens WHERE session_id = s.session_id) AS actual_screens
            FROM lc_sessions s
            WHERE s.status = 'completed'
              AND s.version_id = :vid
              AND NOT EXISTS (
                SELECT 1 FROM lc_session_graphs sg WHERE sg.session_id = s.session_id
              )
              AND EXISTS (
                SELECT 1 FROM lc_screens sc WHERE sc.session_id = s.session_id
              )
              AND NOT EXISTS (
                SELECT 1 FROM lc_transitions t
                WHERE t.session_id = s.session_id AND t.to_fp IS NOT NULL
              )
            ORDER BY s.started_at DESC
        SQL;
        $stmt = $this->db->prepare($sql);
        $stmt->bindValue(':vid', $versionId, \PDO::PARAM_INT);
        $stmt->execute();
        return $stmt->fetchAll(\PDO::FETCH_ASSOC);
    }

    public function getRunningSessions(?int $versionId = null): array
    {
        $versionId ??= $this->getActiveVersionId();
        // 進行中セッション: status='running' のみ
        $sql = <<<SQL
            SELECT s.session_id, s.started_at, s.screens_found, s.status, s.completion_type,
                   COALESCE(s.game_title, 'Unknown Game') AS game_title,
                   (SELECT COUNT(*) FROM lc_screens WHERE session_id = s.session_id) AS actual_screens
            FROM lc_sessions s
            WHERE s.status = 'running'
              AND s.version_id = :vid
            ORDER BY s.started_at DESC
        SQL;
        $stmt = $this->db->prepare($sql);
        $stmt->bindValue(':vid', $versionId, \PDO::PARAM_INT);
        $stmt->execute();
        return $stmt->fetchAll(\PDO::FETCH_ASSOC);
    }

    public function getBgPendingSessions(?int $versionId = null): array
    {
        $versionId ??= $this->getActiveVersionId();
        // バックグラウンド未完了: 完了済み + 後処理が未完了 (Gemini未処理 or グラフ未構築)
        $sql = <<<SQL
            SELECT s.session_id, s.started_at, s.completion_type,
                   COALESCE(s.game_title, 'Unknown Game') AS game_title,
                   (SELECT COUNT(*) FROM lc_screens WHERE session_id = s.session_id) AS actual_screens,
                   (SELECT COUNT(*) FROM lc_screens
                      WHERE session_id = s.session_id
                        AND is_representative = 1
                        AND ocr_text_gemini IS NULL) AS pending_gemini,
                   (SELECT COUNT(*) FROM lc_screens
                      WHERE session_id = s.session_id
                        AND cluster_id IS NULL
                        AND phash IS NOT NULL AND phash != '') AS pending_dedup,
                   (SELECT COUNT(*) FROM lc_session_graphs WHERE session_id = s.session_id) AS has_graph,
                   (SELECT COUNT(*) FROM lc_transitions
                      WHERE session_id = s.session_id AND to_fp IS NOT NULL) AS transitions
            FROM lc_sessions s
            WHERE s.status = 'completed'
              AND s.version_id = :vid
              AND NOT EXISTS (
                SELECT 1 FROM lc_node_mappings nm WHERE nm.session_id = s.session_id
              )
              AND (
                EXISTS (
                  SELECT 1 FROM lc_screens
                  WHERE session_id = s.session_id
                    AND is_representative = 1
                    AND ocr_text_gemini IS NULL
                )
                OR NOT EXISTS (
                  SELECT 1 FROM lc_session_graphs sg WHERE sg.session_id = s.session_id
                )
              )
            ORDER BY s.started_at DESC
        SQL;
        $stmt = $this->db->prepare($sql);
        $stmt->bindValue(':vid', $versionId, \PDO::PARAM_INT);
        $stmt->execute();
        return $stmt->fetchAll(\PDO::FETCH_ASSOC);
    }

    public function toggleExclude(string $masterFp, ?int $versionId = null): array
    {
        $versionId ??= $this->getActiveVersionId();
        $row = $this->db->prepare(
            "SELECT user_excluded FROM lc_master_nodes WHERE master_fp = :fp AND version_id = :vid"
        );
        $row->execute([':fp' => $masterFp, ':vid' => $versionId]);
        $current = $row->fetchColumn();
        if ($current === false) {
            return ['error' => 'not found'];
        }
        $newVal = $current ? 0 : 1;
        $this->db->prepare(
            "UPDATE lc_master_nodes SET user_excluded = :val WHERE master_fp = :fp AND version_id = :vid"
        )->execute([':val' => $newVal, ':fp' => $masterFp, ':vid' => $versionId]);
        return ['master_fp' => $masterFp, 'user_excluded' => (bool)$newVal];
    }

    public function getFinalScreensIncludeExcluded(
        int    $limit     = 10000,
        string $gameTitle = '',
        ?int   $versionId = null,
    ): array {
        $versionId ??= $this->getActiveVersionId();
        $bindings = [':limit' => $limit, ':vid' => $versionId];
        $gameFilter = '';
        if ($gameTitle !== '') {
            $gameFilter = "AND COALESCE(sess.game_title, 'Unknown Game') = :game_title";
            $bindings[':game_title'] = $gameTitle;
        }
        $sql = <<<SQL
            SELECT s.id, COALESCE(m.title_manual, m.title) AS title, s.depth,
                   s.screenshot_path, s.thumbnail_path,
                   s.ocr_text, s.ocr_text_hq, s.ocr_text_gemini, s.is_artifact,
                   m.ocr_text_manual, m.title_manual, m.manual_edited_at,
                   m.last_seen_at AS discovered_at,
                   s.session_id, m.master_fp AS fingerprint, m.scene,
                   COALESCE(sess.game_title, 'Unknown Game') AS game_title,
                   m.visit_count, m.bfs_depth,
                   1 AS is_representative, s.cluster_id,
                   m.user_excluded, m.master_fp,
                   m.manual_group_id, m.is_group_representative,
                   (SELECT GROUP_CONCAT(nm2.match_method || ':' || nm2.session_id, ',')
                    FROM lc_node_mappings nm2
                    WHERE nm2.master_fp = m.master_fp AND nm2.match_method != 'seed' AND nm2.match_method != 'new'
                   ) AS anchor_info,
                   (SELECT nm3.match_method FROM lc_node_mappings nm3
                    WHERE nm3.master_fp = m.master_fp AND nm3.match_method != 'seed'
                    ORDER BY nm3.rowid DESC LIMIT 1
                   ) AS last_match_method
            FROM lc_master_nodes m
            JOIN lc_screens s ON s.id = m.representative_screen_id
            LEFT JOIN lc_sessions sess ON sess.session_id = s.session_id
            WHERE m.is_group_representative = 1
              AND m.version_id = :vid {$gameFilter}
            ORDER BY m.sort_order ASC
            LIMIT :limit
        SQL;
        $stmt = $this->db->prepare($sql);
        foreach ($bindings as $key => $value) {
            $type = ($key === ':limit' || $key === ':vid') ? \PDO::PARAM_INT : \PDO::PARAM_STR;
            $stmt->bindValue($key, $value, $type);
        }
        $stmt->execute();
        return array_map([$this, 'toScreenArray'], $stmt->fetchAll());
    }

    // ─── クラスタ兄弟・代表昇格 ─────────────────────

    public function getClusterSiblings(int $screenId): array
    {
        // まず対象画面の cluster_id と session_id を取得
        $row = $this->db->prepare(
            "SELECT cluster_id, session_id FROM lc_screens WHERE id = ?"
        );
        $row->execute([$screenId]);
        $info = $row->fetch(\PDO::FETCH_ASSOC);
        if (!$info || $info['cluster_id'] === null) {
            return [];
        }

        $stmt = $this->db->prepare(<<<SQL
            SELECT id, fingerprint, title, screenshot_path, thumbnail_path,
                   ocr_text, ocr_text_hq, is_representative, discovered_at, scene
            FROM lc_screens
            WHERE cluster_id = :cluster_id AND session_id = :session_id
            ORDER BY is_representative DESC, discovered_at ASC
        SQL);
        $stmt->execute([
            ':cluster_id' => $info['cluster_id'],
            ':session_id' => $info['session_id'],
        ]);
        return $stmt->fetchAll(\PDO::FETCH_ASSOC);
    }

    public function promoteRepresentative(string $masterFp, int $newScreenId): array
    {
        // 新しい代表画面の情報を取得
        $stmt = $this->db->prepare(
            "SELECT id, cluster_id, session_id FROM lc_screens WHERE id = ?"
        );
        $stmt->execute([$newScreenId]);
        $newScreen = $stmt->fetch(\PDO::FETCH_ASSOC);
        if (!$newScreen) {
            return ['error' => 'screen not found'];
        }

        // 同クラスタの現在の代表を解除
        $this->db->prepare(
            "UPDATE lc_screens SET is_representative = 0"
            . " WHERE cluster_id = ? AND session_id = ? AND is_representative = 1"
        )->execute([$newScreen['cluster_id'], $newScreen['session_id']]);

        // 新しい代表に設定
        $this->db->prepare(
            "UPDATE lc_screens SET is_representative = 1 WHERE id = ?"
        )->execute([$newScreenId]);

        // マスターノードの representative_screen_id を更新
        $this->db->prepare(
            "UPDATE lc_master_nodes SET representative_screen_id = ? WHERE master_fp = ?"
        )->execute([$newScreenId, $masterFp]);

        return ['ok' => true, 'master_fp' => $masterFp, 'new_screen_id' => $newScreenId];
    }

    // ─── セッション削除 ─────────────────────────────

    public function deleteSession(string $sessionId): array
    {
        // 代表画像のIDを保持 (master_nodes が参照)
        $repIds = $this->db->prepare(
            "SELECT id FROM lc_screens WHERE session_id = ? AND is_representative = 1"
        );
        $repIds->execute([$sessionId]);
        $keepIds = array_column($repIds->fetchAll(\PDO::FETCH_ASSOC), 'id');

        // 1. 不採用スクリーンのファイルパスを取得して削除
        $stmt = $this->db->prepare(
            "SELECT screenshot_path, thumbnail_path FROM lc_screens"
            . " WHERE session_id = ? AND is_representative = 0"
        );
        $stmt->execute([$sessionId]);
        $deletedFiles = 0;
        foreach ($stmt->fetchAll(\PDO::FETCH_ASSOC) as $row) {
            foreach (['screenshot_path', 'thumbnail_path'] as $col) {
                if (!empty($row[$col]) && file_exists($row[$col])) {
                    @unlink($row[$col]);
                    $deletedFiles++;
                }
            }
        }

        // 2. 不採用 lc_tappable_items 削除
        if ($keepIds) {
            $placeholders = implode(',', array_fill(0, count($keepIds), '?'));
            $this->db->prepare(
                "DELETE FROM lc_tappable_items WHERE screen_id IN ("
                . "SELECT id FROM lc_screens WHERE session_id = ? AND id NOT IN ($placeholders))"
            )->execute(array_merge([$sessionId], $keepIds));
        } else {
            $this->db->prepare(
                "DELETE FROM lc_tappable_items WHERE screen_id IN ("
                . "SELECT id FROM lc_screens WHERE session_id = ?)"
            )->execute([$sessionId]);
        }

        // 3. 不採用 lc_screens 削除
        $this->db->prepare(
            "DELETE FROM lc_screens WHERE session_id = ? AND is_representative = 0"
        )->execute([$sessionId]);
        $deletedScreens = $this->db->prepare("SELECT changes()")->fetchColumn();

        // 4. lc_transitions 削除
        $this->db->prepare(
            "DELETE FROM lc_transitions WHERE session_id = ?"
        )->execute([$sessionId]);

        // 5. lc_screen_groups 削除
        $this->db->prepare(
            "DELETE FROM lc_screen_groups WHERE session_id = ?"
        )->execute([$sessionId]);

        // 6. lc_session_graphs 削除
        $this->db->prepare(
            "DELETE FROM lc_session_graphs WHERE session_id = ?"
        )->execute([$sessionId]);

        // 7. セッションを archived に更新
        $this->db->prepare(
            "UPDATE lc_sessions SET status = 'archived' WHERE session_id = ?"
        )->execute([$sessionId]);

        return [
            'ok' => true,
            'session_id' => $sessionId,
            'deleted_screens' => (int)$deletedScreens,
            'deleted_files' => $deletedFiles,
            'kept_screens' => count($keepIds),
        ];
    }

    // ─── 除外済みマスターノードのクリーンアップ ──────

    public function getCleanableExcluded(?int $versionId = null): array
    {
        $versionId ??= $this->getActiveVersionId();
        $sql = <<<SQL
            SELECT m.master_fp, m.title, s.screenshot_path, s.thumbnail_path,
                   sess.session_id, sess.status AS session_status
            FROM lc_master_nodes m
            JOIN lc_screens s ON s.id = m.representative_screen_id
            JOIN lc_sessions sess ON sess.session_id = s.session_id
            WHERE m.user_excluded = 1 AND sess.status = 'archived'
              AND m.version_id = :vid
        SQL;
        $stmt = $this->db->prepare($sql);
        $stmt->bindValue(':vid', $versionId, \PDO::PARAM_INT);
        $stmt->execute();
        return $stmt->fetchAll(\PDO::FETCH_ASSOC);
    }

    public function cleanupExcluded(?int $versionId = null): array
    {
        $versionId ??= $this->getActiveVersionId();
        $targets = $this->getCleanableExcluded($versionId);
        if (empty($targets)) {
            return ['ok' => true, 'deleted_nodes' => 0, 'deleted_files' => 0];
        }

        $deletedFiles = 0;
        $fps = [];
        foreach ($targets as $t) {
            $fps[] = $t['master_fp'];
            // スクリーンショットファイル削除
            foreach (['screenshot_path', 'thumbnail_path'] as $col) {
                if (!empty($t[$col]) && file_exists($t[$col])) {
                    @unlink($t[$col]);
                    $deletedFiles++;
                }
            }
        }

        $placeholders = implode(',', array_fill(0, count($fps), '?'));

        // lc_master_edges 削除 (from/to いずれかが対象)
        $this->db->prepare(
            "DELETE FROM lc_master_edges WHERE from_master_fp IN ($placeholders) OR to_master_fp IN ($placeholders)"
        )->execute(array_merge($fps, $fps));

        // lc_node_mappings 削除
        $this->db->prepare(
            "DELETE FROM lc_node_mappings WHERE master_fp IN ($placeholders)"
        )->execute($fps);

        // 代表 lc_screens 削除
        $this->db->prepare(
            "DELETE FROM lc_screens WHERE id IN ("
            . "SELECT representative_screen_id FROM lc_master_nodes WHERE master_fp IN ($placeholders))"
        )->execute($fps);

        // lc_master_nodes 削除
        $this->db->prepare(
            "DELETE FROM lc_master_nodes WHERE master_fp IN ($placeholders)"
        )->execute($fps);

        return [
            'ok' => true,
            'deleted_nodes' => count($fps),
            'deleted_files' => $deletedFiles,
        ];
    }

    // ─── 手動グループ統合 ────────────────────────────

    public function mergeManualGroup(array $masterFps, string $representativeFp, ?int $versionId = null): array
    {
        $versionId ??= $this->getActiveVersionId();
        if (count($masterFps) < 2) {
            return ['error' => '2件以上選択してください'];
        }
        if (!in_array($representativeFp, $masterFps)) {
            return ['error' => '代表は選択した中から選んでください'];
        }

        // 新しい group_id を発行
        $maxGroup = $this->db->query(
            "SELECT COALESCE(MAX(manual_group_id), 0) FROM lc_master_nodes"
        )->fetchColumn();
        $groupId = $maxGroup + 1;

        $placeholders = implode(',', array_fill(0, count($masterFps), '?'));

        // 全メンバーに group_id を設定 (version_id で絞り込み)
        $this->db->prepare(
            "UPDATE lc_master_nodes SET manual_group_id = ?, is_group_representative = 0"
            . " WHERE master_fp IN ($placeholders) AND version_id = ?"
        )->execute(array_merge([$groupId], $masterFps, [$versionId]));

        // 代表を設定
        $this->db->prepare(
            "UPDATE lc_master_nodes SET is_group_representative = 1 WHERE master_fp = ? AND version_id = ?"
        )->execute([$representativeFp, $versionId]);

        return ['ok' => true, 'group_id' => $groupId, 'count' => count($masterFps)];
    }

    public function unmergeManualGroup(int $groupId): array
    {
        $this->db->prepare(
            "UPDATE lc_master_nodes SET manual_group_id = NULL, is_group_representative = 1"
            . " WHERE manual_group_id = ?"
        )->execute([$groupId]);
        return ['ok' => true, 'group_id' => $groupId];
    }

    public function getManualGroupMembers(int $groupId, ?int $versionId = null): array
    {
        $versionId ??= $this->getActiveVersionId();
        $sql = <<<SQL
            SELECT m.master_fp, m.title, m.is_group_representative,
                   s.screenshot_path, s.thumbnail_path
            FROM lc_master_nodes m
            LEFT JOIN lc_screens s ON s.id = m.representative_screen_id
            WHERE m.manual_group_id = :gid AND m.version_id = :vid
            ORDER BY m.is_group_representative DESC, m.first_seen_at ASC
        SQL;
        $stmt = $this->db->prepare($sql);
        $stmt->execute([':gid' => $groupId, ':vid' => $versionId]);
        return $stmt->fetchAll(\PDO::FETCH_ASSOC);
    }
}
