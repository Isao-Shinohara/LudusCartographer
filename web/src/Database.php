<?php

declare(strict_types=1);

namespace LudusCartographer;

use PDO;
use PDOException;

class Database
{
    private static ?PDO $instance       = null;
    private static ?PDO $sqliteInstance = null;

    public static function getConnection(): PDO
    {
        if (self::$instance === null) {
            $host     = $_ENV['DB_HOST']     ?? 'localhost';
            $port     = $_ENV['DB_PORT']     ?? '3306';
            $dbname   = $_ENV['DB_NAME']     ?? 'ludus_cartographer';
            $user     = $_ENV['DB_USER']     ?? 'root';
            $password = $_ENV['DB_PASSWORD'] ?? '';

            $dsn = "mysql:host={$host};port={$port};dbname={$dbname};charset=utf8mb4";

            try {
                self::$instance = new PDO($dsn, $user, $password, [
                    PDO::ATTR_ERRMODE            => PDO::ERRMODE_EXCEPTION,
                    PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
                    PDO::ATTR_EMULATE_PREPARES   => false,
                ]);
            } catch (PDOException $e) {
                throw new \RuntimeException('Database connection failed: ' . $e->getMessage());
            }
        }

        return self::$instance;
    }

    /**
     * SQLite フォールバック接続。
     * crawler/storage/ludus.db が存在する場合に接続を返す。
     *
     * @throws \RuntimeException DB ファイルが見つからない場合
     */
    public static function getSqliteConnection(): PDO
    {
        if (self::$sqliteInstance === null) {
            $dbFile = realpath(__DIR__ . '/../../crawler/storage/ludus.db');
            if ($dbFile === false || !is_file($dbFile)) {
                throw new \RuntimeException('SQLite DB not found: crawler/storage/ludus.db');
            }
            self::$sqliteInstance = new PDO('sqlite:' . $dbFile, null, null, [
                PDO::ATTR_ERRMODE            => PDO::ERRMODE_EXCEPTION,
                PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
            ]);
        }

        return self::$sqliteInstance;
    }
}
