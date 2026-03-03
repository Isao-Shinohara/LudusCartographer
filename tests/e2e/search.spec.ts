import { test, expect } from '@playwright/test';

/**
 * LudusCartographer Web UI — E2E テストスイート
 *
 * PHPビルトインサーバー (localhost:8080) に対してテストを実行する。
 * DB未接続時はサンプルデータにフォールバックするため、
 * MySQL なしでも全テストがパスする。
 */

test.describe('検索ページ — 基本表示', () => {

    test('ページタイトルが正しい', async ({ page }) => {
        await page.goto('/');
        await expect(page).toHaveTitle(/LudusCartographer/);
    });

    test('ヘッダーにブランド名が表示される', async ({ page }) => {
        await page.goto('/');
        const header = page.locator('header');
        await expect(header).toContainText('LudusCartographer');
    });

    test('検索フォームが存在する', async ({ page }) => {
        await page.goto('/');
        const input = page.locator('#search-input');
        await expect(input).toBeVisible();
        await expect(input).toHaveAttribute('placeholder', /検索/);
    });

    test('サンプルデータのスクリーンカードが表示される', async ({ page }) => {
        await page.goto('/');
        // DB未接続時はサンプルデータにフォールバック
        const cards = page.locator('article');
        await expect(cards).toHaveCount(3);
    });

    test('タイトル画面カードが表示される', async ({ page }) => {
        await page.goto('/');
        const grid = page.locator('#results-grid');
        await expect(grid).toContainText('タイトル画面');
    });

    test('ホーム画面カードが表示される', async ({ page }) => {
        await page.goto('/');
        await expect(page.locator('#results-grid')).toContainText('ホーム画面');
    });

    test('ショップ画面カードが表示される', async ({ page }) => {
        await page.goto('/');
        await expect(page.locator('#results-grid')).toContainText('ショップ画面');
    });

    test('件数表示が正しい', async ({ page }) => {
        await page.goto('/');
        await expect(page.locator('body')).toContainText('全スクリーン');
        await expect(page.locator('body')).toContainText('3');
    });

});

test.describe('検索フォーム — インタラクション', () => {

    test('キーワード入力後に検索ボタンをクリックするとURLが変化する', async ({ page }) => {
        await page.goto('/');
        await page.fill('#search-input', 'ショップ');
        await page.click('button[type=submit]');
        await expect(page).toHaveURL(/q=%E3%82%B7%E3%83%A7%E3%83%83%E3%83%97/);
    });

    test('Enterキーで検索が実行される', async ({ page }) => {
        await page.goto('/');
        await page.fill('#search-input', 'ホーム');
        await page.press('#search-input', 'Enter');
        await expect(page).toHaveURL(/q=/);
    });

    test('キーワード検索で結果が絞り込まれる', async ({ page }) => {
        await page.goto('/?q=ショップ');
        // ホーム画面のOCRにも「ショップ」が含まれるため2件
        const cards = page.locator('article');
        const count = await cards.count();
        expect(count).toBeGreaterThan(0);
        await expect(page.locator('body')).toContainText('ショップ');
    });

    test('存在しないキーワードで "見つかりませんでした" が表示される', async ({ page }) => {
        await page.goto('/?q=xyzzy999notfound');
        await expect(page.locator('body')).toContainText('見つかりませんでした');
    });

    test('クリアリンクが表示され、クリックで全件表示に戻る', async ({ page }) => {
        await page.goto('/?q=ショップ');
        const clearLink = page.locator('a', { hasText: 'クリア' });
        await expect(clearLink).toBeVisible();
        await clearLink.click();
        // クリアリンク href="?" → URL は /?  (q パラメータなし)
        await expect(page).not.toHaveURL(/[?&]q=/);
        await expect(page.locator('article')).toHaveCount(3);
    });

    test('検索結果の件数が表示される', async ({ page }) => {
        await page.goto('/?q=ショップ');
        await expect(page.locator('body')).toContainText('検索結果');
    });

});

test.describe('カードコンテンツ', () => {

    test('各カードにカテゴリバッジがある', async ({ page }) => {
        await page.goto('/');
        const badges = page.locator('article span.rounded-full');
        const badgeCount = await badges.count();
        expect(badgeCount).toBeGreaterThan(0);
    });

    test('各カードに訪問回数が表示される', async ({ page }) => {
        await page.goto('/');
        const firstCard = page.locator('article').first();
        await expect(firstCard).toContainText('回訪問');
    });

    test('フッターが表示される', async ({ page }) => {
        await page.goto('/');
        const footer = page.locator('footer');
        await expect(footer).toContainText('LudusCartographer');
        await expect(footer).toContainText('Appium');
    });

});

test.describe('詳細検索パネル', () => {

    test('詳細検索パネルが存在する', async ({ page }) => {
        await page.goto('/');
        const panel = page.locator('details');
        await expect(panel).toBeVisible();
        await expect(panel).toContainText('詳細検索');
    });

    test('詳細検索パネルを開くと入力フォームが表示される', async ({ page }) => {
        await page.goto('/');
        await page.click('details summary');
        await expect(page.locator('#adv-title')).toBeVisible();
        await expect(page.locator('#adv-keyword')).toBeVisible();
        await expect(page.locator('#adv-session')).toBeVisible();
    });

    test('詳細検索ボタンをクリックするとAPI呼び出しが行われる', async ({ page }) => {
        await page.goto('/');
        await page.click('details summary');
        await page.fill('#adv-keyword', 'ショップ');

        // API レスポンスを待つ
        const responsePromise = page.waitForResponse(r => r.url().includes('/api/search.php'));
        await page.click('details button');
        const response = await responsePromise;
        expect(response.status()).toBe(200);

        const body = await response.json();
        expect(body).toHaveProperty('screens');
        expect(body).toHaveProperty('count');
    });

});

test.describe('API エンドポイント', () => {

    test('search API が JSON を返す', async ({ page }) => {
        const res  = await page.request.get('/api/search.php?action=search');
        expect(res.status()).toBe(200);
        expect(res.headers()['content-type']).toContain('application/json');
        const body = await res.json();
        expect(body).toHaveProperty('screens');
        expect(Array.isArray(body.screens)).toBe(true);
    });

    test('search API がキーワードでフィルタリングする', async ({ page }) => {
        const res  = await page.request.get('/api/search.php?action=search&keyword=ショップ');
        const body = await res.json();
        expect(body.count).toBeGreaterThan(0);
    });

    test('detail API が screen と elements を返す', async ({ page }) => {
        const res  = await page.request.get('/api/search.php?action=detail&id=1');
        expect(res.status()).toBe(200);
        const body = await res.json();
        expect(body).toHaveProperty('screen');
        expect(body).toHaveProperty('elements');
    });

    test('detail API で無効な ID は 400 を返す', async ({ page }) => {
        const res = await page.request.get('/api/search.php?action=detail&id=0');
        expect(res.status()).toBe(400);
    });

});

test.describe('モーダル', () => {

    test('カードをクリックするとモーダルが表示される', async ({ page }) => {
        await page.goto('/');
        const modal = page.locator('#modal');
        await expect(modal).toHaveClass(/hidden/);

        await page.locator('article').first().click();
        await expect(modal).not.toHaveClass(/hidden/);
    });

    test('モーダルにスクリーン情報が表示される', async ({ page }) => {
        await page.goto('/');
        await page.locator('article').first().click();
        const modalBody = page.locator('#modal-body');
        // サンプルデータのゲーム名が表示される
        await expect(modalBody).toContainText('Demo Game', { timeout: 5000 });
    });

    test('モーダルを閉じるとモーダルが非表示になる', async ({ page }) => {
        await page.goto('/');
        await page.locator('article').first().click();
        await expect(page.locator('#modal')).not.toHaveClass(/hidden/);

        await page.click('#modal button[aria-label="閉じる"]');
        await expect(page.locator('#modal')).toHaveClass(/hidden/);
    });

    test('Escape キーでモーダルを閉じられる', async ({ page }) => {
        await page.goto('/');
        await page.locator('article').first().click();
        await expect(page.locator('#modal')).not.toHaveClass(/hidden/);

        await page.keyboard.press('Escape');
        await expect(page.locator('#modal')).toHaveClass(/hidden/);
    });

    test('モーダルの detail API レスポンスに parents フィールドがある', async ({ page }) => {
        const res  = await page.request.get('/api/search.php?action=detail&id=1');
        const body = await res.json();
        expect(body).toHaveProperty('parents');
        expect(Array.isArray(body.parents)).toBe(true);
    });

});

test.describe('セッション統計パネル', () => {

    test('セッション統計パネルが存在する', async ({ page }) => {
        await page.goto('/');
        await expect(page.locator('#session-panel')).toBeVisible();
        await expect(page.locator('#session-panel')).toContainText('クローラー セッション統計');
    });

    test('セッション統計パネルがデータを読み込む', async ({ page }) => {
        await page.goto('/');
        // JS による loadSessions() 完了を待つ
        await expect(page.locator('#session-table')).toBeVisible({ timeout: 5000 });
        // サンプルデータの "Demo Game" が表示される
        await expect(page.locator('#session-table')).toContainText('Demo Game');
    });

    test('セッション統計パネルに画面数(Fingerprint数)が表示される', async ({ page }) => {
        await page.goto('/');
        await expect(page.locator('#session-table')).toBeVisible({ timeout: 5000 });
        await expect(page.locator('#session-table')).toContainText('画面');
    });

    test('get_sessions API が sessions 配列を返す', async ({ page }) => {
        const res  = await page.request.get('/api/search.php?action=get_sessions');
        expect(res.status()).toBe(200);
        const body = await res.json();
        expect(body).toHaveProperty('sessions');
        expect(Array.isArray(body.sessions)).toBe(true);
        expect(body.sessions.length).toBeGreaterThan(0);
    });

    test('get_sessions API の各セッションに必須フィールドがある', async ({ page }) => {
        const res  = await page.request.get('/api/search.php?action=get_sessions');
        const body = await res.json();
        const s    = body.sessions[0];
        expect(s).toHaveProperty('id');
        expect(s).toHaveProperty('status');
        expect(s).toHaveProperty('screens_found');
        expect(s).toHaveProperty('started_at');
        expect(s).toHaveProperty('game_name');
        expect(s).toHaveProperty('session_dir');
    });

    test('セッション行クリックで詳細検索パネルが開く', async ({ page }) => {
        await page.goto('/');
        await expect(page.locator('#session-table')).toBeVisible({ timeout: 5000 });

        // details パネルは初期状態で閉じている
        const details = page.locator('details');
        await expect(details).not.toHaveAttribute('open', '');

        // セッション行をクリック
        await page.locator('#session-table tbody tr').first().click();

        // details が開き、adv-session に session_dir がセットされる
        await expect(details).toHaveAttribute('open', '');
        const sessionInput = page.locator('#adv-session');
        await expect(sessionInput).not.toHaveValue('');
    });

});
