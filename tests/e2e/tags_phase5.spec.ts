import { test, expect } from '@playwright/test';

/**
 * マスターノードタグ機能 — Phase 5 E2E
 * (タグ検索 / 絞り込み統合)
 *
 * 設計書: docs/design/master_node_tags.md §11 (検索機能との統合)
 */

test.describe.configure({ mode: 'serial' });

test.describe('Tag 絞り込み Phase 5', () => {
    test.beforeEach(async ({ page }) => {
        await page.goto('/dashboard.php');
        await page.click('#tab-final');
    });

    test('Final タブのアクションバーに タグ絞り込み ボタンが表示される', async ({ page }) => {
        const btn = page.locator('#tag-filter-btn');
        await expect(btn).toBeVisible();
        await expect(btn).toContainText('タグ絞り込み');
    });

    test('Live タブでは タグ絞り込み ボタンが非表示 (final-actions が hidden のため)', async ({ page }) => {
        await page.click('#tab-live');
        // final-actions 全体が hidden
        await expect(page.locator('#final-actions')).toBeHidden();
    });

    test('ボタン押下でモーダルが開き、3 種別のリストが表示される', async ({ page }) => {
        await page.click('#tag-filter-btn');
        const modal = page.locator('#tag-filter-modal');
        await expect(modal).toBeVisible();
        // 3 種別のセクション
        await expect(modal.locator('[data-tag-type="operation"]')).toBeVisible();
        await expect(modal.locator('[data-tag-type="scene"]')).toBeVisible();
        await expect(modal.locator('[data-tag-type="sub_scene"]')).toBeVisible();
    });

    test('シーン/詳細セクションに初期タグの checkbox が表示される', async ({ page }) => {
        await page.click('#tag-filter-btn');
        await page.waitForSelector('#tag-filter-modal [data-tag-type="scene"] input.tag-filter-cb', { timeout: 5000 });
        const sceneBoxes = page.locator('#tag-filter-modal [data-tag-type="scene"] input.tag-filter-cb');
        const cnt = await sceneBoxes.count();
        expect(cnt).toBeGreaterThanOrEqual(11);

        const subBoxes = page.locator('#tag-filter-modal [data-tag-type="sub_scene"] input.tag-filter-cb');
        const cnt2 = await subBoxes.count();
        expect(cnt2).toBeGreaterThanOrEqual(9);
    });

    test('タグを選択するとプレビュー件数が更新される', async ({ page }) => {
        await page.click('#tag-filter-btn');
        await page.waitForSelector('#tag-filter-modal [data-tag-type="scene"] input.tag-filter-cb', { timeout: 5000 });
        // 「(全件表示)」が初期表示
        await expect(page.locator('#tag-filter-result-count')).toContainText('全件表示');
        // 1 個チェック
        await page.locator('#tag-filter-modal [data-tag-type="scene"] input.tag-filter-cb').first().check();
        // プレビュー結果が「件にマッチ」を含むようになる
        await expect(page.locator('#tag-filter-result-count')).toContainText('件にマッチ', { timeout: 5000 });
    });

    test('適用ボタンでバッジが選択数で更新される', async ({ page }) => {
        await page.click('#tag-filter-btn');
        await page.waitForSelector('#tag-filter-modal [data-tag-type="scene"] input.tag-filter-cb', { timeout: 5000 });
        await page.locator('#tag-filter-modal [data-tag-type="scene"] input.tag-filter-cb').first().check();
        await page.locator('#tag-filter-modal [data-tag-type="sub_scene"] input.tag-filter-cb').first().check();
        await page.click('#tag-filter-apply');
        // モーダルが閉じる
        await expect(page.locator('#tag-filter-modal')).toBeHidden();
        // バッジに 2 が表示される
        const badge = page.locator('#tag-filter-badge');
        await expect(badge).toBeVisible();
        await expect(badge).toHaveText('2');
    });

    test('クリアボタンで選択がリセット + バッジが消える', async ({ page }) => {
        // 事前: 何か選択して適用
        await page.click('#tag-filter-btn');
        await page.waitForSelector('#tag-filter-modal [data-tag-type="scene"] input.tag-filter-cb', { timeout: 5000 });
        await page.locator('#tag-filter-modal [data-tag-type="scene"] input.tag-filter-cb').first().check();
        await page.click('#tag-filter-apply');
        await expect(page.locator('#tag-filter-badge')).toBeVisible();

        // 再度開いて クリア
        await page.click('#tag-filter-btn');
        await page.click('#tag-filter-clear');
        await expect(page.locator('#tag-filter-badge')).toBeHidden();
        // チェックボックスも全て off
        const checked = await page.locator('#tag-filter-modal .tag-filter-cb:checked').count();
        expect(checked).toBe(0);
    });

    test('キャンセルボタンで適用なしに閉じる', async ({ page }) => {
        await page.click('#tag-filter-btn');
        await expect(page.locator('#tag-filter-modal')).toBeVisible();
        await page.click('#tag-filter-cancel');
        await expect(page.locator('#tag-filter-modal')).toBeHidden();
        // バッジは変わらない (= 0 のまま)
        await expect(page.locator('#tag-filter-badge')).toBeHidden();
    });

    test('×ボタンで閉じる', async ({ page }) => {
        await page.click('#tag-filter-btn');
        await expect(page.locator('#tag-filter-modal')).toBeVisible();
        await page.click('#tag-filter-close');
        await expect(page.locator('#tag-filter-modal')).toBeHidden();
    });

    test('再度開くと前回選択が保持される', async ({ page }) => {
        await page.click('#tag-filter-btn');
        await page.waitForSelector('#tag-filter-modal [data-tag-type="scene"] input.tag-filter-cb', { timeout: 5000 });
        const firstCb = page.locator('#tag-filter-modal [data-tag-type="scene"] input.tag-filter-cb').first();
        const tagId = await firstCb.getAttribute('data-id');
        await firstCb.check();
        await page.click('#tag-filter-apply');

        // 再度開く
        await page.click('#tag-filter-btn');
        await page.waitForSelector('#tag-filter-modal [data-tag-type="scene"] input.tag-filter-cb', { timeout: 5000 });
        const cb = page.locator(`#tag-filter-modal .tag-filter-cb[data-id="${tagId}"]`);
        await expect(cb).toBeChecked();

        // 後始末
        await page.click('#tag-filter-clear');
    });
});
