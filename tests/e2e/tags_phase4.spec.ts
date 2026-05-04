import { test, expect } from '@playwright/test';

/**
 * マスターノードタグ機能 — Phase 4 E2E
 * (詳細タグ Gemini 判定 + プロンプト編集拡張)
 *
 * 設計書: docs/design/master_node_tags.md §8.2
 */

test.describe.configure({ mode: 'serial' });

test.describe('Tag タブ Phase 4 (詳細タグ)', () => {
    test.beforeEach(async ({ page }) => {
        await page.goto('/dashboard.php');
        await page.click('#tab-tags');
        await page.click('#tag-subtab-sub_scene');
    });

    test('詳細サブタブで初期 9 件が表示される', async ({ page }) => {
        await page.waitForSelector('#tag-list-sub_scene .tag-edit-btn', { timeout: 5000 });
        const text = await page.locator('#tag-list-sub_scene').innerText();
        for (const name of ['ダイアログ', 'ミニ会話', 'ログインボーナス', 'リザルト',
                            'お知らせ', 'チュートリアル説明', 'メニュー画面',
                            'イベント告知', 'ダウンロード']) {
            expect(text).toContain(name);
        }
    });

    test('詳細プロンプト編集モーダルでデフォルトが読み込まれる', async ({ page }) => {
        await page.click('#tag-prompt-edit-sub_scene');
        await expect(page.locator('#prompt-edit-modal')).toBeVisible();
        await expect(page.locator('#prompt-edit-title')).toContainText('詳細');
        await page.waitForFunction(() => {
            const v = (document.getElementById('prompt-edit-text') as HTMLTextAreaElement).value;
            return v.includes('詳細属性分類器') && v.includes('0 個以上');
        }, null, { timeout: 5000 });
    });

    test('詳細プロンプトに sub_scene 用ガイドラインが含まれる', async ({ page }) => {
        await page.click('#tag-prompt-edit-sub_scene');
        await page.waitForFunction(() =>
            (document.getElementById('prompt-edit-text') as HTMLTextAreaElement).value !== '',
            null, { timeout: 5000 },
        );
        const value = await page.locator('#prompt-edit-text').inputValue();
        // 詳細タグ特有の文言
        expect(value).toContain('シーンに依存しない');
        expect(value).toContain('tag_ids');
    });

    test('詳細「未付与のみ判定」モーダルで model が flash になる', async ({ page }) => {
        await page.click('#tag-judge-btn-sub_scene');
        await page.locator('#tag-judge-menu-modal >> text=未付与のみ判定').click();
        await expect(page.locator('#tag-run-confirm-modal')).toBeVisible();
        // estimate を待つ
        await page.waitForSelector('#tag-run-confirm-body:not(.hidden)', { timeout: 5000 });
        await expect(page.locator('#tag-run-confirm-body')).toContainText('gemini-2.5-flash');
    });

    test('詳細プロンプト保存 → ユーザー編集済みになる', async ({ page }) => {
        await page.click('#tag-prompt-edit-sub_scene');
        await page.waitForFunction(() =>
            (document.getElementById('prompt-edit-text') as HTMLTextAreaElement).value !== '',
            null, { timeout: 5000 },
        );
        const original = await page.locator('#prompt-edit-text').inputValue();
        const tweaked = original + '\n# P4 編集テスト_' + Date.now();
        await page.locator('#prompt-edit-text').fill(tweaked);
        await page.click('#prompt-save-btn');
        await expect(page.locator('#prompt-edit-warn')).toBeVisible({ timeout: 5000 });
        await expect(page.locator('#prompt-edit-modal')).toBeHidden({ timeout: 5000 });

        // 再度開いて確認
        await page.click('#tag-prompt-edit-sub_scene');
        await expect(page.locator('#prompt-edit-status')).toContainText('ユーザー編集済み', { timeout: 5000 });
        // 元に戻す (テスト後始末)
        await page.click('#prompt-reset-btn');
        await expect(page.locator('#prompt-edit-status')).toContainText('デフォルト', { timeout: 5000 });
    });

    test('シーン側プロンプトと詳細側プロンプトは独立保存される', async ({ page }) => {
        // 詳細側を編集
        await page.click('#tag-prompt-edit-sub_scene');
        await page.waitForFunction(() =>
            (document.getElementById('prompt-edit-text') as HTMLTextAreaElement).value !== '',
            null, { timeout: 5000 },
        );
        const subText = await page.locator('#prompt-edit-text').inputValue();
        await page.click('#prompt-cancel-btn');

        // シーン側に切替
        await page.click('#tag-subtab-scene');
        await page.click('#tag-prompt-edit-scene');
        await page.waitForFunction(() =>
            (document.getElementById('prompt-edit-text') as HTMLTextAreaElement).value !== '',
            null, { timeout: 5000 },
        );
        const sceneText = await page.locator('#prompt-edit-text').inputValue();

        // 内容が異なる (sub_scene は「詳細属性」、scene は「画面分類」)
        expect(sceneText).not.toBe(subText);
        expect(sceneText).toContain('画面分類器');
        expect(subText).toContain('詳細属性分類器');
    });
});
