/**
 * ClusterDiffView — 2 つのクラスタリング結果を並列比較する汎用 UI 部品
 *
 * Union-Find で関連クラスタを行にまとめ、左右並列 + 行揃え + zebra + 代表
 * バッジ + 行通し番号 (#N or #N-i) ラベルで表示する。
 *
 * 使い方:
 *   ClusterDiffView.render(rootEl, screens, {
 *       getCidA: (s) => s.cluster_id_dhash,
 *       getCidB: (s) => s.cluster_id_hybrid,
 *       labelA: 'dHash 単独',
 *       labelB: 'dHash + ヒスト',
 *       imgUrl: (path) => path,
 *   });
 */
(function (global) {
    'use strict';

    const escapeHtml = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));

    // 16進ハッシュ文字列同士の Hamming 距離 (XOR ビット数)
    function hammingDistance(h1, h2) {
        if (!h1 || !h2 || h1.length !== h2.length) return null;
        try {
            const xor = BigInt('0x' + h1) ^ BigInt('0x' + h2);
            // popcount
            let n = xor, c = 0;
            while (n > 0n) { c += Number(n & 1n); n >>= 1n; }
            return c;
        } catch (e) { return null; }
    }

    function defaultOpts(o) {
        return Object.assign({
            getCidA: (s) => s.cluster_id_dhash,
            getCidB: (s) => s.cluster_id_hybrid,
            labelA: 'A',
            labelB: 'B',
            colorA: 'text-amber-300',
            colorB: 'text-emerald-300',
            getRepA: (items) => Math.min(...items.map(s => s.id)),
            getRepB: (items) => ((items.find(s => s.is_representative) || items[0]).id),
            diffFilter: 'all',
            imgUrl: (path) => path,
            screenName: (s) => s.name || '',
            onScreenClick: null,  // (screen, side) => void  カード単位のクリック (詳細モーダル表示用)
            keyboardNav: false,   // true で矢印キーペイン移動を有効化
            shouldHandleKey: () => true,  // false を返すとキー処理スキップ (モーダル開時等)
            decisionLabel: null,  // (method) => {color, label} | null  判定理由 → ラベル変換
        }, o || {});
    }

    // document keydown リスナーは ClusterDiffView 全体で 1 つだけ
    let _activeKeyHandler = null;

    function groupBy(screens, getCid) {
        const m = {};
        for (const s of screens) {
            const cid = getCid(s);
            if (cid === null || cid === undefined) continue;
            (m[cid] ??= []).push(s);
        }
        const arr = Object.entries(m).map(([cid, items]) => {
            items.sort((a, b) => (a.id || 0) - (b.id || 0));
            return { cid: Number(cid), items, first_id: items[0].id };
        });
        arr.sort((a, b) => a.first_id - b.first_id);
        return arr;
    }

    function computeDiffMap(screens, getCidA, getCidB) {
        const aGroups = {};
        const bGroups = {};
        for (const s of screens) {
            const a = getCidA(s);
            const b = getCidB(s);
            if (a !== null && a !== undefined) (aGroups[a] ??= new Set()).add(s.id);
            if (b !== null && b !== undefined) (bGroups[b] ??= new Set()).add(s.id);
        }
        const map = {};
        for (const s of screens) {
            const a = getCidA(s);
            const b = getCidB(s);
            const aSet = (a !== null && a !== undefined) ? aGroups[a] : null;
            const bSet = (b !== null && b !== undefined) ? bGroups[b] : null;
            if (!aSet || !bSet) { map[s.id] = 'same'; continue; }
            let onlyInB = 0, onlyInA = 0;
            for (const id of bSet) if (!aSet.has(id)) onlyInB++;
            for (const id of aSet) if (!bSet.has(id)) onlyInA++;
            if (onlyInB === 0 && onlyInA === 0) map[s.id] = 'same';
            else if (onlyInB > 0 && onlyInA > 0) map[s.id] = 'mixed';
            else if (onlyInB > 0) map[s.id] = 'merged';
            else map[s.id] = 'split';
        }
        return map;
    }

    function buildUnionFindRows(aGroups, bGroups, screens, getCidA, getCidB) {
        const parent = {};
        const find = (x) => {
            if (parent[x] === undefined) { parent[x] = x; return x; }
            if (parent[x] === x) return x;
            return (parent[x] = find(parent[x]));
        };
        const union = (x, y) => {
            const rx = find(x), ry = find(y);
            if (rx !== ry) parent[rx] = ry;
        };
        for (const s of screens) {
            const a = getCidA(s);
            const b = getCidB(s);
            if (a !== null && a !== undefined && b !== null && b !== undefined) {
                union(`A#${a}`, `B#${b}`);
            }
        }
        const components = {};
        for (const g of aGroups) {
            const root = find(`A#${g.cid}`);
            const c = (components[root] ??= { aCids: new Set(), bCids: new Set(), firstId: g.first_id });
            c.aCids.add(g.cid);
            c.firstId = Math.min(c.firstId, g.first_id);
        }
        for (const g of bGroups) {
            const root = find(`B#${g.cid}`);
            const c = (components[root] ??= { aCids: new Set(), bCids: new Set(), firstId: g.first_id });
            c.bCids.add(g.cid);
            c.firstId = Math.min(c.firstId, g.first_id);
        }
        const rows = Object.values(components).map(c => ({
            aCids: [...c.aCids].sort((a, b) => a - b),
            bCids: [...c.bCids].sort((a, b) => a - b),
            firstId: c.firstId,
        }));
        rows.sort((x, y) => x.firstId - y.firstId);
        return rows;
    }

    function isCompleteMatch(row, aByCid, bByCid) {
        if (row.aCids.length !== 1 || row.bCids.length !== 1) return false;
        const ag = aByCid[row.aCids[0]];
        const bg = bByCid[row.bCids[0]];
        if (!ag || !bg || ag.items.length !== bg.items.length) return false;
        const aSet = new Set(ag.items.map(s => s.id));
        return bg.items.every(s => aSet.has(s.id));
    }

    function countOther(group, side, screens, getCidA, getCidB) {
        const c = {};
        for (const s of group.items) {
            const o = (side === 'A') ? getCidB(s) : getCidA(s);
            if (o === null || o === undefined) continue;
            c[o] = (c[o] ?? 0) + 1;
        }
        return c;
    }

    function renderBlock(group, side, opts, ctx) {
        const { aByCid, bByCid, cidLabel, screens, diffMap } = ctx;
        const otherCounts = countOther(group, side, screens, opts.getCidA, opts.getCidB);
        const otherKeys = Object.keys(otherCounts).sort((x, y) => Number(x) - Number(y));
        const otherSidePrefix = side === 'A' ? 'B' : 'A';
        const arrow = side === 'A' ? '→' : '←';
        const sideColor = side === 'A' ? opts.colorA : opts.colorB;
        const myLabel = cidLabel[`${side}${group.cid}`] || `#${group.cid}`;
        // 判定理由 (先頭 screen の cluster_decision_method = このクラスタが前から分かれた理由)
        // 代表は「テキストあり等で交代」した結果なので、代表ではなく先頭 (時系列最古) を使う
        let reasonHtml = '';
        const firstItem = group.items[0];  // group.items は既に id 昇順ソート済み
        const method = firstItem ? firstItem.cluster_decision_method : null;
        if (method && typeof opts.decisionLabel === 'function') {
            const lbl = opts.decisionLabel(method);
            if (lbl) {
                reasonHtml = `<span class="px-1.5 py-0.5 rounded text-[10px] ${lbl.color}" title="${escapeHtml(method)}">${escapeHtml(lbl.label)}</span>`;
            }
        } else if (method) {
            reasonHtml = `<span class="text-[10px] text-gray-400">${escapeHtml(method)}</span>`;
        }
        const repId = side === 'A' ? opts.getRepA(group.items) : opts.getRepB(group.items);
        const repItem = group.items.find(s => s.id === repId);
        const repDhash = repItem ? repItem.dhash : null;
        const cardsHtml = group.items.map(s => {
            const thumb = s.thumbnail_path ? opts.imgUrl(s.thumbnail_path)
                       : (s.screenshot_path ? opts.imgUrl(s.screenshot_path) : '');
            const otherCid = side === 'A' ? opts.getCidB(s) : opts.getCidA(s);
            const otherLabel = cidLabel[`${otherSidePrefix}${otherCid}`] || `?#${otherCid}`;
            const isRep = s.id === repId;
            const repRing = isRep ? ' ring-2 ring-amber-500/80' : '';
            const repBadge = isRep
                ? '<span class="absolute top-1 left-1 w-5 h-5 rounded-full bg-emerald-500 text-white flex items-center justify-center text-[11px] shadow" title="代表">★</span>'
                : '';
            // メタ情報: dhash 値, 平均輝度, 代表との dHash 距離 (フロントで Hamming 計算)
            const dhashShort = s.dhash ? String(s.dhash).slice(0, 8) : '';
            const br = (typeof s.avg_brightness === 'number') ? s.avg_brightness.toFixed(0) : null;
            const dDist = (!isRep && repDhash && s.dhash) ? hammingDistance(repDhash, s.dhash) : null;
            const metaParts = [];
            if (dhashShort) metaParts.push(`<span class="font-mono">d:${escapeHtml(dhashShort)}</span>`);
            if (br !== null) metaParts.push(`<span>br:${escapeHtml(br)}</span>`);
            if (dDist !== null) metaParts.push(`<span class="text-amber-300">Δd:${dDist}</span>`);
            const metaHtml = metaParts.length > 0
                ? `<div class="px-1 py-0.5 text-[9px] text-gray-400 bg-gray-950/70 flex flex-wrap gap-1.5 border-t border-gray-800">${metaParts.join('')}</div>`
                : '';
            return `<div class="bg-gray-900 border border-gray-800 rounded overflow-hidden${repRing}" title="${escapeHtml(opts.screenName(s))}" data-screen-id="${s.id}">
                <div class="relative">
                    ${thumb ? `<img src="${escapeHtml(thumb)}" class="w-full aspect-video object-cover" loading="lazy" />` : '<div class="w-full aspect-video bg-gray-800"></div>'}
                    ${repBadge}
                    <span class="absolute top-1 right-1 text-[9px] px-1 py-0.5 rounded bg-black/60 text-gray-300 font-mono">${arrow}${escapeHtml(otherLabel)}</span>
                </div>
                ${metaHtml}
            </div>`;
        }).join('');
        return `<div class="cdv-block border border-gray-800 rounded-lg p-2 bg-gray-950/40 cursor-pointer hover:border-gray-600 transition-colors h-full"
                    data-side="${side}" data-cid="${group.cid}"
                    data-pair-cids="${otherKeys.join(',')}">
            <div class="flex items-center justify-between mb-2">
                <span class="text-xs font-bold ${sideColor}">${escapeHtml(myLabel)}</span>
                ${reasonHtml}
            </div>
            <div class="grid grid-cols-3 gap-1.5">${cardsHtml}</div>
        </div>`;
    }

    function render(rootEl, screens, options) {
        if (!rootEl) return;
        const opts = defaultOpts(options);

        // 差分 map
        const diffMap = computeDiffMap(screens, opts.getCidA, opts.getCidB);

        // フィルタ適用 (差分のみ等)
        let filtered = screens;
        if (opts.diffFilter !== 'all') {
            const passSet = new Set();
            for (const s of screens) {
                const k = diffMap[s.id] || 'same';
                let pass = false;
                if (opts.diffFilter === 'diff') pass = (k !== 'same');
                else if (opts.diffFilter === 'merged') pass = (k === 'merged' || k === 'mixed');
                else if (opts.diffFilter === 'split') pass = (k === 'split' || k === 'mixed');
                if (pass) passSet.add(s.id);
            }
            const includeIds = new Set(passSet);
            for (const s of screens) {
                if (passSet.has(s.id)) {
                    for (const t of screens) {
                        if (opts.getCidA(t) === opts.getCidA(s) || opts.getCidB(t) === opts.getCidB(s)) {
                            includeIds.add(t.id);
                        }
                    }
                }
            }
            filtered = screens.filter(s => includeIds.has(s.id));
        }

        const aGroups = groupBy(filtered, opts.getCidA);
        const bGroups = groupBy(filtered, opts.getCidB);
        const aByCid = {}; for (const g of aGroups) aByCid[g.cid] = g;
        const bByCid = {}; for (const g of bGroups) bByCid[g.cid] = g;

        if (aGroups.length === 0 && bGroups.length === 0) {
            rootEl.innerHTML = '<div class="col-span-full text-center text-gray-600 py-8 text-xs">該当なし</div>';
            return { diffMap, summary: { same: 0, merged: 0, split: 0, mixed: 0, total: 0 } };
        }

        const rows = buildUnionFindRows(aGroups, bGroups, filtered, opts.getCidA, opts.getCidB);
        const filterDiffOnly = (opts.diffFilter !== 'all');
        const visibleRows = filterDiffOnly ? rows.filter(r => !isCompleteMatch(r, aByCid, bByCid)) : rows;

        // 行通し番号 + 枝番ラベル
        const cidLabel = {};
        visibleRows.forEach((row, idx) => {
            row.aCids.forEach((cid, i) => {
                cidLabel[`A${cid}`] = row.aCids.length > 1 ? `#${idx}-${i}` : `#${idx}`;
            });
            row.bCids.forEach((cid, i) => {
                cidLabel[`B${cid}`] = row.bCids.length > 1 ? `#${idx}-${i}` : `#${idx}`;
            });
        });

        const ctx = { aByCid, bByCid, cidLabel, screens: filtered, diffMap };

        // CSS Grid セル生成
        const cells = [];
        visibleRows.forEach((row, idx) => {
            const gridRow = idx + 1;
            const rowBg = (idx % 2 === 1) ? 'bg-gray-700/50' : 'bg-gray-800/40';
            // 左
            if (row.aCids.length > 0) {
                const blocks = row.aCids.map(cid => renderBlock(aByCid[cid], 'A', opts, ctx)).join('');
                cells.push(`<div style="grid-row: ${gridRow}; grid-column: 1;" class="flex flex-col gap-3 h-full ${rowBg} rounded-lg p-2">${blocks}</div>`);
            } else {
                cells.push(`<div style="grid-row: ${gridRow}; grid-column: 1;" class="${rowBg} rounded-lg p-2 flex items-center justify-center"><span class="opacity-30 text-[10px] text-gray-600">— なし —</span></div>`);
            }
            // 右
            if (row.bCids.length > 0) {
                const blocks = row.bCids.map(cid => renderBlock(bByCid[cid], 'B', opts, ctx)).join('');
                cells.push(`<div style="grid-row: ${gridRow}; grid-column: 2;" class="flex flex-col gap-3 h-full ${rowBg} rounded-lg p-2">${blocks}</div>`);
            } else {
                cells.push(`<div style="grid-row: ${gridRow}; grid-column: 2;" class="${rowBg} rounded-lg p-2 flex items-center justify-center"><span class="opacity-30 text-[10px] text-gray-600">— なし —</span></div>`);
            }
        });

        rootEl.innerHTML = cells.join('');

        // 各カード (スクショ単位) クリックで onScreenClick を呼ぶ (詳細モーダル等)
        if (typeof opts.onScreenClick === 'function') {
            const idMap = {};
            for (const s of filtered) idMap[s.id] = s;
            rootEl.querySelectorAll('[data-screen-id]').forEach(el => {
                el.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const sid = Number(el.dataset.screenId);
                    const s = idMap[sid];
                    if (!s) return;
                    const block = el.closest('.cdv-block');
                    const side = block ? block.dataset.side : null;
                    opts.onScreenClick(s, side);
                });
            });
        }

        // キーボードナビゲーション (←→: 左右ペイン、↑↓: 同カラム上下ペイン)
        if (opts.keyboardNav) {
            const blockEls = [...rootEl.querySelectorAll('.cdv-block')];
            // 各ペインの (gridRow, side) を取得
            const blockMeta = blockEls.map(el => ({
                el,
                row: Number(el.parentElement.style.gridRow),
                side: el.dataset.side,
            }));
            let selectedIdx = -1;
            const setSelected = (idx, scroll) => {
                if (selectedIdx >= 0 && blockEls[selectedIdx]) {
                    blockEls[selectedIdx].classList.remove('ring-4', 'ring-yellow-400');
                }
                selectedIdx = idx;
                if (idx >= 0 && blockEls[idx]) {
                    blockEls[idx].classList.add('ring-4', 'ring-yellow-400');
                    if (scroll) {
                        // block: 'start' でペイン上部を画面上端に揃える + sticky ヘッダ分のオフセット
                        const el = blockEls[idx];
                        el.style.scrollMarginTop = '120px';
                        el.scrollIntoView({ block: 'start', behavior: 'smooth' });
                    }
                }
            };
            const handleKey = (e) => {
                if (!opts.shouldHandleKey(e)) return;
                if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.isContentEditable)) return;
                if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(e.key)) return;
                if (selectedIdx < 0) {
                    if (blockEls.length > 0) { setSelected(0, true); e.preventDefault(); }
                    return;
                }
                const cur = blockMeta[selectedIdx];
                let target = null;
                if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
                    const targetSide = cur.side === 'A' ? 'B' : 'A';
                    target = blockMeta.find(b => b.row === cur.row && b.side === targetSide);
                } else {
                    const dir = e.key === 'ArrowUp' ? -1 : 1;
                    // 同 side の最も近い別 row を探す (空ペインは飛ばす)
                    let nearest = null;
                    for (const b of blockMeta) {
                        if (b.side !== cur.side) continue;
                        if (dir > 0 && b.row > cur.row) {
                            if (!nearest || b.row < nearest.row) nearest = b;
                        } else if (dir < 0 && b.row < cur.row) {
                            if (!nearest || b.row > nearest.row) nearest = b;
                        }
                    }
                    target = nearest;
                }
                if (target) {
                    e.preventDefault();
                    setSelected(blockMeta.indexOf(target), true);
                }
            };
            // 既存リスナー解除して新規登録
            if (_activeKeyHandler) document.removeEventListener('keydown', _activeKeyHandler);
            _activeKeyHandler = handleKey;
            document.addEventListener('keydown', handleKey);
        } else {
            // 無効化: 残存リスナー解除
            if (_activeKeyHandler) {
                document.removeEventListener('keydown', _activeKeyHandler);
                _activeKeyHandler = null;
            }
        }

        // サマリ計算
        const counts = { same: 0, merged: 0, split: 0, mixed: 0 };
        for (const s of filtered) counts[diffMap[s.id] || 'same']++;
        return { diffMap, summary: { ...counts, total: filtered.length } };
    }

    global.ClusterDiffView = { render };
})(window);
