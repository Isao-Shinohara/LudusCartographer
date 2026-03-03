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

    // ------------------------------------------------------------------
    // public API
    // ------------------------------------------------------------------

    /**
     * DB に存在するゲームタイトルの一覧を返す（ヘッダーセレクター用）。
     *
     * @return string[]
     */
    public function getGameTitles(): array
    {
        $stmt = $this->db->query(
            "SELECT DISTINCT game_title FROM lc_sessions"
            . " WHERE game_title IS NOT NULL ORDER BY game_title"
        );
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
    public function getSessions(int $limit = 20, string $gameTitle = ''): array
    {
        $conditions = [];
        $bindings   = [':limit' => $limit];

        if ($gameTitle !== '') {
            $conditions[]          = 'game_title = :game_title';
            $bindings[':game_title'] = $gameTitle;
        }

        $where = $conditions ? 'WHERE ' . implode(' AND ', $conditions) : '';

        $sql = <<<SQL
            SELECT id,
                   COALESCE(game_title, session_id) AS game_name,
                   game_title,
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
            $type = ($key === ':limit') ? PDO::PARAM_INT : PDO::PARAM_STR;
            $stmt->bindValue($key, $value, $type);
        }
        $stmt->execute();
        return $stmt->fetchAll();
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
            'game_name'       => $raw['game_title'] ?? $raw['session_id'],
            'platform'        => 'ios',
            'screen_hash'     => $raw['fingerprint'],
            'game_title'      => $raw['game_title'] ?? 'Unknown Game',
        ];
    }
}
