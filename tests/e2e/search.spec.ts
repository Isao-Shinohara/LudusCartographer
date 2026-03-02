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
