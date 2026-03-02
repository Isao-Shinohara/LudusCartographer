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
     * スクリーン詳細 + そのUI要素一覧を取得する。
     *
     * @return array{screen: array<string,mixed>|null, elements: array<int,array<string,mixed>>}
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

        return compact('screen', 'elements');
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
