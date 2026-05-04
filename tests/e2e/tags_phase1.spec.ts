import { test, expect } from '@playwright/test';

/**
 * マスターノードタグ機能 — Phase 1 E2E
 *
 * 設計書: docs/design/master_node_tags.md
 * 詳細計画: docs/design/master_node_tags_phase1.md §8
 */

test.describe.configure({ mode: 'serial' });

test.describe('Tag タブ Phase 1', () => {
    test.beforeEach(async ({ page }) => {
        await page.goto('/dashboard.php');
    });

    test('タブ末尾に Tag タブが追加される', async ({ page }) => {
        const tabBtn = page.locator('#tab-tags');
        await expect(tabBtn).toBeVisible();
        await expect(tabBtn).toContainText('Tag');
    });

    test('Tag タブを開くと 3 サブタブが表示される', async ({ page }) => {
        await page.click('#tab-tags');
        await expect(page.locator('#tag-subtab-scene')).toBeVisible();
        await expect(page.locator('#tag-subtab-sub_scene')).toBeVisible();
        await expect(page.locator('#tag-subtab-operation')).toBeVisible();
    });

    test('シーンサブタブで初期タグが表示される (>=11)', async ({ page }) => {
        await page.click('#tab-tags');
        await page.click('#tag-subtab-scene');
        await page.waitForSelector('#tag-list-scene .tag-edit-btn', { timeout: 5000 });
        const rows = page.locator('#tag-list-scene > div');
        const count = await rows.count();
        expect(count).toBeGreaterThanOrEqual(11);
        // ホームタグが先頭に表示される (sort_order=0)
        await expect(rows.first()).toContainText('ホーム');
        // 必須の初期タグが含まれる
        const text = await page.locator('#tag-list-scene').innerText();
        for (const name of ['ホーム', 'クエスト', 'バトル', 'ADV', '動画', 'ガチャ',
                            'ショップ', 'ロード', 'メニュー', '3D 探索', 'その他']) {
            expect(text).toContain(name);
        }
    });

    test('詳細サブタブで初期タグが表示される (>=9)', async ({ page }) => {
        await page.click('#tab-tags');
        await page.click('#tag-subtab-sub_scene');
        await page.waitForSelector('#tag-list-sub_scene .tag-edit-btn', { timeout: 5000 });
        const rows = page.locator('#tag-list-sub_scene > div');
        const count = await rows.count();
        expect(count).toBeGreaterThanOrEqual(9);
        const text = await page.locator('#tag-list-sub_scene').innerText();
        for (const name of ['ダイアログ', 'ミニ会話', 'ログインボーナス', 'リザルト',
                            'お知らせ', 'チュートリアル説明', 'メニュー画面',
                            'イベント告知', 'ダウンロード']) {
            expect(text).toContain(name);
        }
    });

    test('操縦カテゴリサブタブは説明文 + 空表示', async ({ page }) => {
        await page.click('#tab-tags');
        await page.click('#tag-subtab-operation');
        // 説明文
        await expect(page.locator('#tag-content-operation')).toContainText('Phase 2 以降で auto_pilot 起動時に自動登録');
        // 「+ 新規タグ追加」ボタンが操縦カテゴリには存在しない
        await expect(page.locator('#tag-add-btn-operation')).toHaveCount(0);
    });

    test('プロンプト編集 / 判定実行ボタンが表示される (P3 で有効化)', async ({ page }) => {
        await page.click('#tab-tags');
        await page.click('#tag-subtab-scene');
        await expect(page.locator('#tag-prompt-edit-scene')).toBeVisible();
        await expect(page.locator('#tag-prompt-edit-scene')).toBeEnabled();
        // 判定実行ボタンはモードごとに 2 ボタン (未付与のみ / 全件再判定)
        const modeButtons = page.locator('#tag-content-scene .tag-judge-mode-btn');
        await expect(modeButtons).toHaveCount(2);
    });

    test('+ 新規タグ追加ボタンでモーダルが開く', async ({ page }) => {
        await page.click('#tab-tags');
        await page.click('#tag-subtab-scene');
        await page.click('#tag-add-btn-scene');
        const modal = page.locator('#tag-edit-modal');
        await expect(modal).toBeVisible();
        await expect(page.locator('#tag-edit-modal-title')).toContainText('タグ追加');
    });

    test('タグ追加モーダルの名称必須バリデーション', async ({ page }) => {
        await page.click('#tab-tags');
        await page.click('#tag-subtab-scene');
        await page.click('#tag-add-btn-scene');
        // 名称未入力で保存
        await page.click('#tag-edit-save');
        await expect(page.locator('#tag-edit-error')).toBeVisible();
        await expect(page.locator('#tag-edit-error')).toContainText('名称は必須');
    });

    test('色のバリデーション (RGB 形式)', async ({ page }) => {
        await page.click('#tab-tags');
        await page.click('#tag-subtab-scene');
        await page.click('#tag-add-btn-scene');
        await page.fill('#tag-edit-name', 'テスト');
        await page.fill('#tag-edit-color', 'invalid');
        await page.click('#tag-edit-save');
        await expect(page.locator('#tag-edit-error')).toContainText('#RRGGBB');
    });

    test('シーンタグの新規追加 → 一覧に反映される', async ({ page }) => {
        await page.click('#tab-tags');
        await page.click('#tag-subtab-scene');
        await page.waitForSelector('#tag-list-scene .tag-edit-btn');

        await page.click('#tag-add-btn-scene');
        const uniq = Date.now() + '_' + Math.random().toString(36).slice(2, 8);
        const newName = 'E2E_新規シーン_' + uniq;
        await page.fill('#tag-edit-name', newName);
        await page.fill('#tag-edit-color', '#123456');
        await page.click('#tag-edit-save');

        // モーダル閉じる + 当該タグが一覧に出る
        await expect(page.locator('#tag-edit-modal')).toBeHidden();
        await expect(page.locator('#tag-list-scene')).toContainText(newName);

        // 後始末: 追加したタグを削除
        const newRow = page.locator('#tag-list-scene > div', { hasText: newName });
        await newRow.locator('.tag-del-btn').click();
        await page.click('#tag-delete-confirm');
    });

    test('タグ削除 → 一覧から消える (論理削除)', async ({ page }) => {
        await page.click('#tab-tags');
        await page.click('#tag-subtab-scene');
        await page.waitForSelector('#tag-list-scene .tag-edit-btn');

        // 一時的にテストタグを追加
        await page.click('#tag-add-btn-scene');
        const ts = Date.now() + '_' + Math.random().toString(36).slice(2, 8);
        const tmpName = 'E2E_削除対象_' + ts;
        await page.fill('#tag-edit-name', tmpName);
        await page.click('#tag-edit-save');
        await page.waitForSelector(`#tag-list-scene >> text="${tmpName}"`);

        const targetRow = page.locator('#tag-list-scene > div', { hasText: tmpName });
        await targetRow.locator('.tag-del-btn').click();
        await expect(page.locator('#tag-delete-modal')).toBeVisible();
        await page.click('#tag-delete-confirm');

        // 削除モーダル閉じる + 当該タグ名が一覧から消えるのを待つ
        await expect(page.locator('#tag-delete-modal')).toBeHidden();
        await expect(page.locator('#tag-list-scene')).not.toContainText(tmpName);
    });

    test('Tag タブ以外に切り替えると tags-container が hidden になる', async ({ page }) => {
        await page.click('#tab-tags');
        await expect(page.locator('#tags-container')).toBeVisible();
        await page.click('#tab-live');
        await expect(page.locator('#tags-container')).toBeHidden();
    });
});
