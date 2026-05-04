import { test, expect } from '@playwright/test';

/**
 * マスターノードタグ機能 — Phase 3 E2E
 * (シーンタグ Gemini 判定 + プロンプト編集)
 *
 * 設計書: docs/design/master_node_tags.md §5.2 §5.3 §6.6 §6.7
 */

test.describe.configure({ mode: 'serial' });

test.describe('Tag タブ Phase 3', () => {
    test.beforeEach(async ({ page }) => {
        await page.goto('/dashboard.php');
        await page.click('#tab-tags');
    });

    test('プロンプト編集ボタンでモーダルが開く', async ({ page }) => {
        await page.click('#tag-prompt-edit-scene');
        const modal = page.locator('#prompt-edit-modal');
        await expect(modal).toBeVisible();
        await expect(page.locator('#prompt-edit-title')).toContainText('シーンタグ');
        // プロンプト本文が読み込まれる (textarea の value をチェック)
        await page.waitForFunction(() =>
            (document.getElementById('prompt-edit-text') as HTMLTextAreaElement).value
                .includes('画面分類器'),
            null,
            { timeout: 5000 },
        );
    });

    test('プロンプト編集モーダルにプレースホルダ案内が表示される', async ({ page }) => {
        await page.click('#tag-prompt-edit-scene');
        await expect(page.locator('#prompt-edit-modal')).toContainText('{tag_candidates}');
        await expect(page.locator('#prompt-edit-modal')).toContainText('{detected_scene}');
        await expect(page.locator('#prompt-edit-modal')).toContainText('{ocr_text}');
    });

    test('プロンプト編集 → 保存 → ユーザー編集済み表示になる', async ({ page }) => {
        await page.click('#tag-prompt-edit-scene');
        await page.waitForFunction(() =>
            (document.getElementById('prompt-edit-text') as HTMLTextAreaElement).value !== ''
        );
        const original = await page.locator('#prompt-edit-text').inputValue();
        const tweaked = original + '\n# テスト編集_' + Date.now();
        await page.locator('#prompt-edit-text').fill(tweaked);
        await page.click('#prompt-save-btn');
        // 警告/成功メッセージが出る
        await expect(page.locator('#prompt-edit-warn')).toBeVisible({ timeout: 5000 });
        // モーダルが自動で閉じる
        await expect(page.locator('#prompt-edit-modal')).toBeHidden({ timeout: 5000 });

        // 再度開いて「ユーザー編集済み」になっていることを確認
        await page.click('#tag-prompt-edit-scene');
        await expect(page.locator('#prompt-edit-status')).toContainText('ユーザー編集済み', { timeout: 5000 });
    });

    test('デフォルトに戻すボタンで is_default 表示に戻る', async ({ page }) => {
        await page.click('#tag-prompt-edit-scene');
        await page.waitForFunction(() =>
            (document.getElementById('prompt-edit-text') as HTMLTextAreaElement).value !== ''
        );
        await page.click('#prompt-reset-btn');
        await expect(page.locator('#prompt-edit-status')).toContainText('デフォルト', { timeout: 5000 });
    });

    test('「未付与のみ判定」ボタンで確認モーダルが開く', async ({ page }) => {
        await page.click('#tag-content-scene >> text=未付与のみ判定');
        const modal = page.locator('#tag-run-confirm-modal');
        await expect(modal).toBeVisible();
        await expect(page.locator('#tag-run-confirm-title')).toContainText('未付与のみ');
        // estimate が読み込まれる
        await expect(page.locator('#tag-run-confirm-body')).toBeVisible({ timeout: 5000 });
        await expect(page.locator('#tag-run-confirm-body')).toContainText('対象件数');
    });

    test('「全件再判定」ボタンで確認モーダルが開く', async ({ page }) => {
        await page.click('#tag-content-scene >> text=全件再判定');
        await expect(page.locator('#tag-run-confirm-modal')).toBeVisible();
        await expect(page.locator('#tag-run-confirm-title')).toContainText('全件再判定');
    });

    test('確認モーダルに 完全リセット チェックボックスが存在する', async ({ page }) => {
        await page.click('#tag-content-scene >> text=全件再判定');
        await expect(page.locator('#tag-run-reset-manual')).toBeVisible();
        await expect(page.locator('#tag-run-reset-manual')).not.toBeChecked();
    });

    test('対象 0 件の場合は実行ボタンが disabled', async ({ page }) => {
        // production DB は master_fp が 0 件のはず
        await page.click('#tag-content-scene >> text=未付与のみ判定');
        await page.waitForSelector('#tag-run-confirm-body:not(.hidden)', { timeout: 5000 });
        await expect(page.locator('#tag-run-confirm')).toBeDisabled();
    });

    test('キャンセルボタンで確認モーダルが閉じる', async ({ page }) => {
        await page.click('#tag-content-scene >> text=未付与のみ判定');
        await expect(page.locator('#tag-run-confirm-modal')).toBeVisible();
        await page.click('#tag-run-cancel');
        await expect(page.locator('#tag-run-confirm-modal')).toBeHidden();
    });

    test('プロンプト編集モーダルのキャンセルで閉じる', async ({ page }) => {
        await page.click('#tag-prompt-edit-scene');
        await expect(page.locator('#prompt-edit-modal')).toBeVisible();
        await page.click('#prompt-cancel-btn');
        await expect(page.locator('#prompt-edit-modal')).toBeHidden();
    });

    test('詳細サブタブにもプロンプト編集 / 判定ボタンが配置される (P4 で完成)', async ({ page }) => {
        await page.click('#tag-subtab-sub_scene');
        await expect(page.locator('#tag-prompt-edit-sub_scene')).toBeVisible();
        await expect(page.locator('#tag-prompt-edit-sub_scene')).toBeEnabled();
        const modeButtons = page.locator('#tag-content-sub_scene .tag-judge-mode-btn');
        await expect(modeButtons).toHaveCount(2);
    });
});
