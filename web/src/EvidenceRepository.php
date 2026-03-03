<?php

declare(strict_types=1);

namespace LudusCartographer;

use PDO;

/**
 * SQLite evidence DB (crawler/storage/ludus.db) 用リポジトリ。
 *
 * ScreenRepository と同一のメソッドシグネチャを持ち、
 * search.php が MySQL / SQLite どちらでも同じコードで動作できるようにする。
 */
class EvidenceRepository
{
    public function __construct(private PDO $db) {}

    // ------------------------------------------------------------------
    // public API (ScreenRepository と同一シグネチャ)
    // ------------------------------------------------------------------

    /** @return array<int, array<string, mixed>> */
    public function search(string $keyword = '', int $limit = 50): array
    {
        if ($keyword === '') {
            $sql = <<<SQL
                SELECT id, title AS name, depth AS category,
                       screenshot_path, NULL AS thumbnail_path,
                       ocr_text, 1 AS visited_count, discovered_at AS last_seen_at,
                       session_id AS game_name, 'ios' AS platform,
                       fingerprint AS screen_hash
                FROM lc_screens
                ORDER BY discovered_at DESC
                LIMIT :limit
            SQL;
            $stmt = $this->db->prepare($sql);
            $stmt->bindValue(':limit', $limit, PDO::PARAM_INT);
        } else {
            $sql = <<<SQL
                SELECT id, title AS name, depth AS category,
                       screenshot_path, NULL AS thumbnail_path,
                       ocr_text, 1 AS visited_count, discovered_at AS last_seen_at,
                       session_id AS game_name, 'ios' AS platform,
                       fingerprint AS screen_hash
                FROM lc_screens
                WHERE title LIKE :like OR ocr_text LIKE :like2
                ORDER BY discovered_at DESC
                LIMIT :limit
            SQL;
            $stmt = $this->db->prepare($sql);
            $stmt->bindValue(':like',  '%' . $keyword . '%');
            $stmt->bindValue(':like2', '%' . $keyword . '%');
            $stmt->bindValue(':limit', $limit, PDO::PARAM_INT);
        }

        $stmt->execute();
        return array_map([$this, 'formatRow'], $stmt->fetchAll());
    }

    /** @return array<int, array<string, mixed>> */
    public function searchAdvanced(
        string $title     = '',
        string $keyword   = '',
        string $sessionId = '',
        int    $limit     = 100,
    ): array {
        $conditions = [];
        $bindings   = [':limit' => $limit];

        if ($title !== '') {
            $conditions[]       = 'title LIKE :title';
            $bindings[':title'] = '%' . $title . '%';
        }

        if ($keyword !== '') {
            $conditions[]        = '(title LIKE :kw OR ocr_text LIKE :kw2)';
            $bindings[':kw']     = '%' . $keyword . '%';
            $bindings[':kw2']    = '%' . $keyword . '%';
        }

        if ($sessionId !== '') {
            $conditions[]          = 'session_id = :session';
            $bindings[':session']  = $sessionId;
        }

        $where = $conditions ? 'WHERE ' . implode(' AND ', $conditions) : '';

        $sql = <<<SQL
            SELECT id, title AS name, depth AS category,
                   screenshot_path, NULL AS thumbnail_path,
                   ocr_text, 1 AS visited_count, discovered_at AS last_seen_at,
                   session_id AS game_name, 'ios' AS platform,
                   fingerprint AS screen_hash
            FROM lc_screens
            {$where}
            ORDER BY discovered_at DESC
            LIMIT :limit
        SQL;

        $stmt = $this->db->prepare($sql);
        foreach ($bindings as $key => $value) {
            $type = ($key === ':limit') ? PDO::PARAM_INT : PDO::PARAM_STR;
            $stmt->bindValue($key, $value, $type);
        }
        $stmt->execute();
        return array_map([$this, 'formatRow'], $stmt->fetchAll());
    }

    /**
     * @return array{screen: array<string,mixed>|null, elements: array<int,array<string,mixed>>, parents: array<int,array<string,mixed>>}
     */
    public function findWithElements(int $screenId): array
    {
        $stmt = $this->db->prepare(<<<SQL
            SELECT id, title, depth, screenshot_path, ocr_text,
                   discovered_at, session_id, fingerprint, parent_fp
            FROM lc_screens WHERE id = :id
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
                SELECT id, title AS name, fingerprint AS screen_hash, NULL AS via_label
                FROM lc_screens
                WHERE fingerprint = :fp AND session_id = :sid
                LIMIT 1
            SQL);
            $stmt->execute([':fp' => $raw['parent_fp'], ':sid' => $raw['session_id']]);
            $parents = $stmt->fetchAll();
        }

        return compact('screen', 'elements', 'parents');
    }

    /** @return array<int, array<string, mixed>> */
    public function getSessions(int $limit = 20): array
    {
        $stmt = $this->db->prepare(<<<SQL
            SELECT id,
                   session_id AS game_name,
                   'ios'      AS platform,
                   status,
                   screens_found,
                   started_at,
                   NULL       AS ended_at,
                   NULL       AS error_message,
                   session_id AS session_dir
            FROM lc_sessions
            ORDER BY started_at DESC
            LIMIT :limit
        SQL);
        $stmt->bindValue(':limit', $limit, PDO::PARAM_INT);
        $stmt->execute();
        return $stmt->fetchAll();
    }

    // ------------------------------------------------------------------
    // private helpers
    // ------------------------------------------------------------------

    /** lc_screens の raw 行を API 出力フォーマットに変換する。 */
    private function toScreenArray(array $raw): array
    {
        return [
            'id'              => $raw['id'],
            'name'            => $raw['title'],
            'category'        => 'depth=' . $raw['depth'],
            'screenshot_path' => $raw['screenshot_path'],
            'thumbnail_path'  => null,
            'ocr_text'        => $raw['ocr_text'],
            'visited_count'   => 1,
            'last_seen_at'    => $raw['discovered_at'],
            'game_name'       => $raw['session_id'],
            'platform'        => 'ios',
            'screen_hash'     => $raw['fingerprint'],
        ];
    }

    /** SELECT 結果の行をフォーマットする（search/searchAdvanced 用）。 */
    private function formatRow(array $row): array
    {
        $row['category'] = 'depth=' . $row['category'];
        return $row;
    }
}
