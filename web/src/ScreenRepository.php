<?php

declare(strict_types=1);

namespace LudusCartographer;

use PDO;

class ScreenRepository
{
    public function __construct(private PDO $db) {}

    /**
     * キーワードでスクリーンを検索する（OCRテキスト・画面名）。
     *
     * @param string $keyword 検索キーワード（空の場合は全件返す）
     * @param int    $limit   最大取得件数
     * @return array<int, array<string, mixed>>
     */
    public function search(string $keyword = '', int $limit = 50): array
    {
        if ($keyword === '') {
            $sql = <<<SQL
                SELECT
                    s.id,
                    s.name,
                    s.category,
                    s.screenshot_path,
                    s.thumbnail_path,
                    s.ocr_text,
                    s.visited_count,
                    s.last_seen_at,
                    g.name  AS game_name,
                    g.platform
                FROM screens s
                JOIN games g ON g.id = s.game_id
                ORDER BY s.last_seen_at DESC
                LIMIT :limit
            SQL;
            $stmt = $this->db->prepare($sql);
            $stmt->bindValue(':limit', $limit, PDO::PARAM_INT);
        } else {
            $sql = <<<SQL
                SELECT
                    s.id,
                    s.name,
                    s.category,
                    s.screenshot_path,
                    s.thumbnail_path,
                    s.ocr_text,
                    s.visited_count,
                    s.last_seen_at,
                    g.name  AS game_name,
                    g.platform,
                    MATCH(s.ocr_text) AGAINST (:kw IN BOOLEAN MODE) AS relevance
                FROM screens s
                JOIN games g ON g.id = s.game_id
                WHERE
                    s.name LIKE :like
                    OR MATCH(s.ocr_text) AGAINST (:kw IN BOOLEAN MODE)
                ORDER BY relevance DESC, s.last_seen_at DESC
                LIMIT :limit
            SQL;
            $stmt = $this->db->prepare($sql);
            $stmt->bindValue(':kw',    $keyword . '*');
            $stmt->bindValue(':like',  '%' . $keyword . '%');
            $stmt->bindValue(':limit', $limit, PDO::PARAM_INT);
        }

        $stmt->execute();
        return $stmt->fetchAll();
    }

    /**
     * スクリーン詳細 + UI要素一覧 + 親画面一覧を取得する。
     *
     * @return array{screen: array<string,mixed>|null, elements: array<int,array<string,mixed>>, parents: array<int,array<string,mixed>>}
     */
    public function findWithElements(int $screenId): array
    {
        $screenSql = <<<SQL
            SELECT s.*, g.name AS game_name, g.platform
            FROM screens s
            JOIN games g ON g.id = s.game_id
            WHERE s.id = :id
        SQL;
        $stmt = $this->db->prepare($screenSql);
        $stmt->execute([':id' => $screenId]);
        $screen = $stmt->fetch() ?: null;

        $elementsSql = <<<SQL
            SELECT e.*, ns.name AS navigates_to_name
            FROM ui_elements e
            LEFT JOIN screens ns ON ns.id = e.navigates_to
            WHERE e.screen_id = :id
            ORDER BY e.bbox_y, e.bbox_x
        SQL;
        $stmt = $this->db->prepare($elementsSql);
        $stmt->execute([':id' => $screenId]);
        $elements = $stmt->fetchAll();

        // この画面を navigates_to として持つ親画面を取得する（接続マップの "A → B" 用）
        $parentsSql = <<<SQL
            SELECT DISTINCT s.id, s.name, s.screen_hash, e.label AS via_label
            FROM ui_elements e
            JOIN screens s ON s.id = e.screen_id
            WHERE e.navigates_to = :id
            ORDER BY s.id
        SQL;
        $stmt = $this->db->prepare($parentsSql);
        $stmt->execute([':id' => $screenId]);
        $parents = $stmt->fetchAll();

        return compact('screen', 'elements', 'parents');
    }

    /**
     * クロールセッション一覧を取得する（最新順）。
     *
     * @param int $limit 最大取得件数
     * @return array<int, array<string, mixed>>
     */
    public function getSessions(int $limit = 20): array
    {
        $sql = <<<SQL
            SELECT
                cs.id,
                cs.status,
                cs.screens_found,
                cs.started_at,
                cs.ended_at,
                cs.error_message,
                g.name     AS game_name,
                g.platform AS platform
            FROM crawl_sessions cs
            JOIN games g ON g.id = cs.game_id
            ORDER BY cs.started_at DESC
            LIMIT :limit
        SQL;
        $stmt = $this->db->prepare($sql);
        $stmt->bindValue(':limit', $limit, PDO::PARAM_INT);
        $stmt->execute();
        $rows = $stmt->fetchAll();

        // started_at → session_dir (YYYYMMDD_HHmmss) を付加（screenshot_path フィルタ用）
        foreach ($rows as &$row) {
            $dt                = new \DateTime($row['started_at']);
            $row['session_dir'] = $dt->format('Ymd_His');
        }
        return $rows;
    }

    /**
     * セッション一覧のサンプルデータを返す（DB不要）。
     *
     * @return array<int, array<string, mixed>>
     */
    public static function getSampleSessions(): array
    {
        return [
            [
                'id'            => 1,
                'status'        => 'completed',
                'screens_found' => 3,
                'started_at'    => '2026-03-03 13:00:00',
                'ended_at'      => '2026-03-03 13:05:00',
                'error_message' => null,
                'game_name'     => 'Demo Game',
                'platform'      => 'android',
                'session_dir'   => '20260303_130000',
            ],
        ];
    }

    /**
     * title / keyword / session_id の複合条件でスクリーンを検索する。
     *
     * - title     : screens.name の部分一致（LIKE）
     * - keyword   : screens.name LIKE + screens.ocr_text FULLTEXT の OR
     * - session_id: screenshot_path に含まれるセッションディレクトリ名
     *
     * 各条件は AND で結合する。すべて空の場合は全件を返す。
     *
     * @param string $title     画面タイトル絞り込み
     * @param string $keyword   OCR 全文キーワード
     * @param string $sessionId セッション ID（例: 20260303_181720）
     * @param int    $limit     最大取得件数
     * @return array<int, array<string, mixed>>
     */
    public function searchAdvanced(
        string $title     = '',
        string $keyword   = '',
        string $sessionId = '',
        int    $limit     = 100,
    ): array {
        $conditions = [];
        $bindings   = [':limit' => $limit];

        if ($title !== '') {
            $conditions[]       = 's.name LIKE :title';
            $bindings[':title'] = '%' . $title . '%';
        }

        if ($keyword !== '') {
            $conditions[]          = '(s.name LIKE :kw_like OR MATCH(s.ocr_text) AGAINST (:kw_ft IN BOOLEAN MODE))';
            $bindings[':kw_like']  = '%' . $keyword . '%';
            $bindings[':kw_ft']    = $keyword . '*';
        }

        if ($sessionId !== '') {
            $conditions[]           = 's.screenshot_path LIKE :session';
            $bindings[':session']   = '%/' . $sessionId . '/%';
        }

        $where = $conditions ? 'WHERE ' . implode(' AND ', $conditions) : '';

        $sql = <<<SQL
            SELECT
                s.id,
                s.name,
                s.category,
                s.screenshot_path,
                s.thumbnail_path,
                s.ocr_text,
                s.visited_count,
                s.last_seen_at,
                g.name     AS game_name,
                g.platform
            FROM screens s
            JOIN games g ON g.id = s.game_id
            {$where}
            ORDER BY s.last_seen_at DESC
            LIMIT :limit
        SQL;

        $stmt = $this->db->prepare($sql);
        foreach ($bindings as $key => $value) {
            $type = ($key === ':limit') ? \PDO::PARAM_INT : \PDO::PARAM_STR;
            $stmt->bindValue($key, $value, $type);
        }
        $stmt->execute();
        return $stmt->fetchAll();
    }

    /**
     * テスト・デモ用のインメモリサンプルデータを返す（DB不要）。
     *
     * @return array<int, array<string, mixed>>
     */
    public static function getSampleData(): array
    {
        return [
            [
                'id'            => 1,
                'name'          => 'タイトル画面',
                'category'      => 'title',
                'ocr_text'      => 'DEMO GAME TAP TO START',
                'visited_count' => 5,
                'last_seen_at'  => '2026-03-03 00:00:00',
                'game_name'     => 'Demo Game',
                'platform'      => 'android',
                'screenshot_path' => null,
                'thumbnail_path'  => null,
            ],
            [
                'id'            => 2,
                'name'          => 'ホーム画面',
                'category'      => 'home',
                'ocr_text'      => 'ホーム クエスト ショップ ガチャ ランキング',
                'visited_count' => 42,
                'last_seen_at'  => '2026-03-03 01:00:00',
                'game_name'     => 'Demo Game',
                'platform'      => 'android',
                'screenshot_path' => null,
                'thumbnail_path'  => null,
            ],
            [
                'id'            => 3,
                'name'          => 'ショップ画面',
                'category'      => 'shop',
                'ocr_text'      => 'ショップ 宝石 x500 コイン x1200 購入する 戻る',
                'visited_count' => 18,
                'last_seen_at'  => '2026-03-03 01:30:00',
                'game_name'     => 'Demo Game',
                'platform'      => 'android',
                'screenshot_path' => null,
                'thumbnail_path'  => null,
            ],
        ];
    }
}
