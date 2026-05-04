<?php

declare(strict_types=1);

// ルートアクセスは Dashboard へ転送
header('Location: dashboard.php', true, 302);
exit;
