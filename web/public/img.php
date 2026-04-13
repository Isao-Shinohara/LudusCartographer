<?php

declare(strict_types=1);

/**
 * 証拠画像プロキシ
 *
 * crawler/evidence/ および crawler/screenshots/ 以下の画像のみを配信する。
 * realpath() でパストラバーサルを防止する。
 */

$allowedRoots = array_filter([
    realpath(__DIR__ . '/../../crawler/evidence'),
    realpath(__DIR__ . '/../../crawler/screenshots'),
    realpath(__DIR__ . '/../../crawler/storage/screenshots'),
]);

$requestedPath = $_GET['path'] ?? '';

if ($requestedPath === '') {
    http_response_code(400);
    exit;
}

$realPath = realpath($requestedPath);

if ($realPath === false || !is_file($realPath)) {
    http_response_code(404);
    exit;
}

$allowed = false;
foreach ($allowedRoots as $root) {
    if (str_starts_with($realPath, $root . DIRECTORY_SEPARATOR)) {
        $allowed = true;
        break;
    }
}

if (!$allowed) {
    http_response_code(403);
    exit;
}

$ext  = strtolower(pathinfo($realPath, PATHINFO_EXTENSION));
$mime = match ($ext) {
    'jpg', 'jpeg' => 'image/jpeg',
    'png'         => 'image/png',
    'gif'         => 'image/gif',
    'webp'        => 'image/webp',
    default       => null,
};

if ($mime === null) {
    http_response_code(400);
    exit;
}

$mtime = filemtime($realPath);
$etag = sprintf('"%x-%x"', $mtime, filesize($realPath));

// ETag / If-None-Match で変更検知
if (isset($_SERVER['HTTP_IF_NONE_MATCH']) && $_SERVER['HTTP_IF_NONE_MATCH'] === $etag) {
    http_response_code(304);
    exit;
}

header('Content-Type: ' . $mime);
header('Cache-Control: public, max-age=3600');
header('ETag: ' . $etag);
header('Content-Length: ' . filesize($realPath));
readfile($realPath);
