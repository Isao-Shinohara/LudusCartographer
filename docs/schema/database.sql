-- ============================================================
-- LudusCartographer データベーススキーマ
-- MySQL 8.0+
-- ============================================================

CREATE DATABASE IF NOT EXISTS ludus_cartographer
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE ludus_cartographer;

-- ------------------------------------------------------------
-- games: 対象ゲームマスタ
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS games (
    id          INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(255) NOT NULL COMMENT 'ゲーム名',
    package     VARCHAR(255) NOT NULL COMMENT 'アプリパッケージ名 (e.g. com.example.game)',
    platform    ENUM('ios', 'android', 'both') NOT NULL DEFAULT 'both',
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_package_platform (package, platform)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='対象ゲームマスタ';

-- ------------------------------------------------------------
-- screens: 画面記録テーブル
-- ゲームの各画面（状態）を一意に管理する。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS screens (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    game_id         INT UNSIGNED NOT NULL COMMENT 'games.id への外部キー',
    screen_hash     VARCHAR(64) NOT NULL COMMENT '画面の知覚ハッシュ（重複検出用）',
    name            VARCHAR(255) DEFAULT NULL COMMENT 'OCR/手動ラベリングによる画面名',
    category        VARCHAR(100) DEFAULT NULL COMMENT '画面カテゴリ (例: home, battle, shop)',
    screenshot_path VARCHAR(1024) DEFAULT NULL COMMENT 'GCS上のスクリーンショットパス',
    thumbnail_path  VARCHAR(1024) DEFAULT NULL COMMENT 'GCS上のサムネイルパス',
    ocr_text        MEDIUMTEXT DEFAULT NULL COMMENT 'PaddleOCRによる全文テキスト',
    visited_count   INT UNSIGNED NOT NULL DEFAULT 1 COMMENT 'このスクリーンへの訪問回数',
    first_seen_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '初回発見日時',
    last_seen_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最終発見日時',
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_game_hash (game_id, screen_hash),
    KEY idx_game_id (game_id),
    KEY idx_category (category),
    KEY idx_last_seen (last_seen_at),
    FULLTEXT KEY ft_ocr_text (ocr_text),
    CONSTRAINT fk_screens_game FOREIGN KEY (game_id) REFERENCES games (id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ゲーム画面記録';

-- ------------------------------------------------------------
-- ui_elements: UI要素テーブル
-- 各スクリーン上のインタラクション可能な要素を記録する。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ui_elements (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    screen_id       BIGINT UNSIGNED NOT NULL COMMENT 'screens.id への外部キー',
    element_type    ENUM(
                        'button',
                        'text',
                        'image',
                        'input',
                        'icon',
                        'tab',
                        'menu',
                        'dialog',
                        'unknown'
                    ) NOT NULL DEFAULT 'unknown' COMMENT 'UI要素の種類',
    label           VARCHAR(500) DEFAULT NULL COMMENT 'OCRで読み取ったテキストラベル',
    -- バウンディングボックス (スクリーン座標、ピクセル)
    bbox_x          INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '左上X座標',
    bbox_y          INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '左上Y座標',
    bbox_w          INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '幅',
    bbox_h          INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '高さ',
    -- タップ可能か
    is_tappable     TINYINT(1) NOT NULL DEFAULT 1 COMMENT 'タップ可能フラグ',
    -- このUI要素をタップした後に遷移した先のスクリーン
    navigates_to    BIGINT UNSIGNED DEFAULT NULL COMMENT '遷移先 screens.id',
    confidence      FLOAT DEFAULT NULL COMMENT 'OCR/検出信頼スコア (0.0-1.0)',
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_screen_id (screen_id),
    KEY idx_element_type (element_type),
    KEY idx_navigates_to (navigates_to),
    FULLTEXT KEY ft_label (label),
    CONSTRAINT fk_elements_screen FOREIGN KEY (screen_id) REFERENCES screens (id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_elements_navigates FOREIGN KEY (navigates_to) REFERENCES screens (id)
        ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='画面内UI要素';

-- ------------------------------------------------------------
-- crawl_sessions: クロールセッション管理
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS crawl_sessions (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    game_id         INT UNSIGNED NOT NULL,
    device_id       VARCHAR(255) NOT NULL COMMENT 'Appiumデバイス識別子',
    status          ENUM('running', 'completed', 'failed', 'paused') NOT NULL DEFAULT 'running',
    screens_found   INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '発見したスクリーン数',
    started_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at        DATETIME DEFAULT NULL,
    error_message   TEXT DEFAULT NULL,
    KEY idx_game_status (game_id, status),
    CONSTRAINT fk_session_game FOREIGN KEY (game_id) REFERENCES games (id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='クロールセッション管理';

-- ------------------------------------------------------------
-- サンプルデータ（開発・テスト用）
-- ------------------------------------------------------------
INSERT INTO games (name, package, platform) VALUES
    ('Demo Game', 'com.example.demogame', 'android');

INSERT INTO screens (game_id, screen_hash, name, category, ocr_text) VALUES
    (1, 'abc123hash001', 'タイトル画面', 'title', 'DEMO GAME\nTAP TO START'),
    (1, 'abc123hash002', 'ホーム画面', 'home', 'ホーム\nクエスト\nショップ\nガチャ\nランキング'),
    (1, 'abc123hash003', 'ショップ画面', 'shop', 'ショップ\n宝石 x500\nコイン x1200\n購入する\n戻る');

INSERT INTO ui_elements (screen_id, element_type, label, bbox_x, bbox_y, bbox_w, bbox_h, is_tappable, navigates_to) VALUES
    (1, 'button', 'TAP TO START', 100, 500, 280, 60, 1, 2),
    (2, 'tab', 'クエスト', 0, 700, 90, 80, 1, NULL),
    (2, 'tab', 'ショップ', 90, 700, 90, 80, 1, 3),
    (2, 'tab', 'ガチャ', 180, 700, 90, 80, 1, NULL),
    (3, 'button', '購入する', 80, 600, 200, 60, 1, NULL),
    (3, 'button', '戻る', 10, 30, 80, 40, 1, 2);
