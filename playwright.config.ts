import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
    testDir: './tests/e2e',
    fullyParallel: true,
    forbidOnly: !!process.env.CI,
    retries: process.env.CI ? 2 : 0,
    workers: process.env.CI ? 1 : undefined,
    reporter: 'html',

    use: {
        baseURL: process.env.BASE_URL || 'http://localhost:8080',
        trace: 'on-first-retry',
    },

    projects: [
        {
            name: 'chromium',
            use: { ...devices['Desktop Chrome'] },
        },
    ],

    // ローカルPHPサーバーを自動起動
    webServer: {
        command: 'php -S localhost:8080 -t web/public',
        url: 'http://localhost:8080',
        reuseExistingServer: !process.env.CI,
        timeout: 10000,
        stdout: 'ignore',
        stderr: 'ignore',
    },
});
