const batchStatsElements = {
    listContainer: document.getElementById('batch-stats-list'),
    detailContainer: document.getElementById('batch-stats-detail'),
    compareContainer: document.getElementById('batch-stats-compare'),
};

let selectedBatchIds = [];
let selectionVersion = 0;

document.addEventListener('DOMContentLoaded', () => {
    if (!batchStatsElements.listContainer) {
        return;
    }
    initializeBatchStatsPage();
});

function initializeBatchStatsPage() {
    loadBatchStatsList();
    renderBatchStatsDetail(null);
    renderBatchStatsCompare(null);

    batchStatsElements.listContainer.addEventListener('change', (event) => {
        const target = event.target;
        if (!target || !target.dataset || !target.dataset.batchId) {
            return;
        }
        handleBatchSelectionChange(target);
    });
}

async function loadBatchStatsList() {
    if (!batchStatsElements.listContainer) return;
    try {
        const payload = await api.get('/registration/batch-stats');
        renderBatchStatsList(payload?.items || []);
    } catch (error) {
        toast.error(error.message || '加载批次统计列表失败');
        batchStatsElements.listContainer.innerHTML = renderEmptyState('暂无批次数据', '请稍后刷新重试');
    }
}

function renderBatchStatsList(items) {
    if (!batchStatsElements.listContainer) return;

    if (!Array.isArray(items) || items.length === 0) {
        batchStatsElements.listContainer.innerHTML = renderEmptyState('暂无批次统计', '完成批次后将展示统计数据');
        return;
    }

    const rows = items.map((item) => {
        const id = Number(item.id);
        const checked = selectedBatchIds.includes(id) ? 'checked' : '';
        const successRate = calcRate(item.success_count, item.finished_count ?? item.target_count);
        return `
            <tr>
                <td><input type="checkbox" data-batch-id="${id}" ${checked}></td>
                <td>${escapeHtml(item.batch_id || String(id))}</td>
                <td>${escapeHtml(item.status || '-')}</td>
                <td>${escapeHtml(item.pipeline_key || '-')}</td>
                <td>${formatRate(successRate)}</td>
                <td>${formatDuration(item.avg_duration_ms)}</td>
            </tr>
        `;
    }).join('');

    batchStatsElements.listContainer.innerHTML = `
        <div class="table-container">
            <table class="data-table">
                <thead>
                    <tr>
                        <th></th>
                        <th>批次</th>
                        <th>状态</th>
                        <th>Pipeline</th>
                        <th>成功率</th>
                        <th>平均耗时</th>
                    </tr>
                </thead>
                <tbody>
                    ${rows}
                </tbody>
            </table>
        </div>
    `;
}

async function handleBatchSelectionChange(checkbox) {
    const id = Number(checkbox.dataset.batchId);
    if (!id) return;

    if (checkbox.checked) {
        if (selectedBatchIds.includes(id)) {
            return;
        }
        if (selectedBatchIds.length >= 2) {
            checkbox.checked = false;
            toast.warning('最多只能选择 2 个批次');
            return;
        }
        selectedBatchIds.push(id);
    } else {
        selectedBatchIds = selectedBatchIds.filter((value) => value !== id);
    }

    const token = bumpSelectionVersion();
    const snapshot = [...selectedBatchIds];
    await updateSelectionView(token, snapshot);
}

function getSelectedBatchIds() {
    return [...selectedBatchIds];
}

async function updateSelectionView(token, snapshot) {
    const activeToken = typeof token === 'number' ? token : selectionVersion;
    const activeSnapshot = Array.isArray(snapshot) ? snapshot : [...selectedBatchIds];

    if (activeSnapshot.length === 0) {
        renderBatchStatsDetail(null);
        renderBatchStatsCompare(null);
        return;
    }

    if (activeSnapshot.length === 1) {
        renderBatchStatsCompare(null);
        await loadBatchStatsDetail(activeSnapshot[0], activeToken, activeSnapshot);
        return;
    }

    if (activeSnapshot.length === 2) {
        renderBatchStatsDetailPlaceholder();
        await loadBatchStatsCompare(activeSnapshot[0], activeSnapshot[1], activeToken, activeSnapshot);
    }
}

async function loadBatchStatsDetail(statId, token, snapshot) {
    if (!batchStatsElements.detailContainer) return;
    const requestToken = typeof token === 'number' ? token : selectionVersion;
    const expectedIds = Array.isArray(snapshot) && snapshot.length ? snapshot : [statId];
    try {
        const payload = await api.get(`/registration/batch-stats/${statId}`);
        if (!isSelectionCurrent(requestToken, expectedIds)) {
            return;
        }
        renderBatchStatsDetail(payload);
    } catch (error) {
        if (!isSelectionCurrent(requestToken, expectedIds)) {
            return;
        }
        toast.error(error.message || '加载批次详情失败');
        renderBatchStatsDetail(null);
    }
}

async function loadBatchStatsCompare(leftId, rightId, token, snapshot) {
    if (!batchStatsElements.compareContainer) return;
    const requestToken = typeof token === 'number' ? token : selectionVersion;
    const expectedIds = Array.isArray(snapshot) && snapshot.length ? snapshot : [leftId, rightId];
    try {
        const payload = await api.post('/registration/batch-stats/compare', {
            left_id: leftId,
            right_id: rightId,
        });
        if (!isSelectionCurrent(requestToken, expectedIds)) {
            return;
        }
        renderBatchStatsCompare(payload);
    } catch (error) {
        if (!isSelectionCurrent(requestToken, expectedIds)) {
            return;
        }
        toast.error(error.message || '加载批次对比失败');
        renderBatchStatsCompare(null);
    }
}

function renderBatchStatsDetail(stat) {
    if (!batchStatsElements.detailContainer) return;

    if (!stat) {
        batchStatsElements.detailContainer.innerHTML = renderEmptyState('请选择批次', '从左侧列表选择一个批次查看详情');
        return;
    }

    const summaryCards = [
        { label: '目标数量', value: stat.target_count },
        { label: '完成数量', value: stat.finished_count },
        { label: '成功数量', value: stat.success_count },
        { label: '失败数量', value: stat.failed_count },
    ];

    const meta = `
        <div class="batch-stats-meta-grid">
            <div>
                <div class="batch-stats-meta-label">批次 ID</div>
                <div class="batch-stats-meta-value">${escapeHtml(stat.batch_id || String(stat.id))}</div>
            </div>
            <div>
                <div class="batch-stats-meta-label">状态</div>
                <div class="batch-stats-meta-value">${escapeHtml(stat.status || '-')}</div>
            </div>
            <div>
                <div class="batch-stats-meta-label">Pipeline</div>
                <div class="batch-stats-meta-value">${escapeHtml(stat.pipeline_key || '-')}</div>
            </div>
            <div>
                <div class="batch-stats-meta-label">平均耗时</div>
                <div class="batch-stats-meta-value">${formatDuration(stat.avg_duration_ms)}</div>
            </div>
        </div>
    `;

    const summary = `
        <div class="stats-grid batch-stats-summary-grid">
            ${summaryCards.map((card) => `
                <div class="stat-card info">
                    <div class="stat-value">${formatNumber(card.value)}</div>
                    <div class="stat-label">${escapeHtml(card.label)}</div>
                </div>
            `).join('')}
        </div>
    `;

    const stepStats = renderBatchStatsTable(
        '步骤统计',
        stat.step_stats || [],
        ['step_key', 'sample_count', 'success_count', 'avg_duration_ms', 'p50_duration_ms', 'p90_duration_ms'],
        ['Step', '样本数', '成功数', '平均耗时', 'P50', 'P90'],
    );

    const stageStats = renderBatchStatsTable(
        '阶段统计',
        stat.stage_stats || [],
        ['stage_key', 'sample_count', 'avg_duration_ms', 'p50_duration_ms', 'p90_duration_ms'],
        ['Stage', '样本数', '平均耗时', 'P50', 'P90'],
    );

    batchStatsElements.detailContainer.innerHTML = `${meta}${summary}${stepStats}${stageStats}`;
}

function renderBatchStatsDetailPlaceholder() {
    if (!batchStatsElements.detailContainer) return;
    batchStatsElements.detailContainer.innerHTML = renderEmptyState('已选择两个批次', '请查看右侧批次对比结果');
}

function renderBatchStatsCompare(compare) {
    if (!batchStatsElements.compareContainer) return;

    if (!compare) {
        batchStatsElements.compareContainer.innerHTML = renderEmptyState('请选择两个批次', '选择两个批次进行对比');
        return;
    }

    const left = compare.left || {};
    const right = compare.right || {};
    const diff = compare.summary_diff || {};

    const summary = `
        <div class="batch-stats-compare-grid">
            <div class="batch-stats-compare-panel">
                <div class="batch-stats-compare-title">左侧批次</div>
                <div class="batch-stats-compare-value">${escapeHtml(left.batch_id || String(left.id || '-'))}</div>
                <div class="batch-stats-compare-meta">成功 ${formatNumber(left.success_count)} / 目标 ${formatNumber(left.target_count)}</div>
                <div class="batch-stats-compare-meta">平均耗时 ${formatDuration(left.avg_duration_ms)}</div>
            </div>
            <div class="batch-stats-compare-panel">
                <div class="batch-stats-compare-title">右侧批次</div>
                <div class="batch-stats-compare-value">${escapeHtml(right.batch_id || String(right.id || '-'))}</div>
                <div class="batch-stats-compare-meta">成功 ${formatNumber(right.success_count)} / 目标 ${formatNumber(right.target_count)}</div>
                <div class="batch-stats-compare-meta">平均耗时 ${formatDuration(right.avg_duration_ms)}</div>
            </div>
            <div class="batch-stats-compare-panel batch-stats-compare-diff">
                <div class="batch-stats-compare-title">差值</div>
                <div class="batch-stats-compare-value">成功率差 ${formatRate(diff.success_rate)}</div>
                <div class="batch-stats-compare-meta">目标差 ${formatNumber(diff.target_count)}</div>
                <div class="batch-stats-compare-meta">平均耗时差 ${formatDuration(diff.avg_duration_ms)}</div>
            </div>
        </div>
    `;

    const stepCompare = renderCompareTable(
        '步骤对比',
        compare.step_diffs || [],
        'step_key',
    );

    const stageCompare = renderCompareTable(
        '阶段对比',
        compare.stage_diffs || [],
        'stage_key',
    );

    batchStatsElements.compareContainer.innerHTML = `${summary}${stepCompare}${stageCompare}`;
}

function bumpSelectionVersion() {
    selectionVersion += 1;
    return selectionVersion;
}

function isSelectionCurrent(token, expectedIds) {
    if (token !== selectionVersion) {
        return false;
    }
    if (!Array.isArray(expectedIds)) {
        return true;
    }
    if (expectedIds.length !== selectedBatchIds.length) {
        return false;
    }
    return expectedIds.every((id) => selectedBatchIds.includes(id));
}

function renderBatchStatsTable(title, rows, keys, headers) {
    if (!Array.isArray(rows) || rows.length === 0) {
        return `
            <div class="batch-stats-subsection">
                <h4>${escapeHtml(title)}</h4>
                ${renderEmptyState('暂无数据', '等待任务完成后生成统计')}
            </div>
        `;
    }

    const bodyRows = rows.map((row) => `
        <tr>
            ${keys.map((key) => {
                const value = row?.[key];
                if (key === 'avg_duration_ms' || key === 'p50_duration_ms' || key === 'p90_duration_ms') {
                    return `<td>${formatDuration(value)}</td>`;
                }
                return `<td>${escapeHtml(formatNumber(value))}</td>`;
            }).join('')}
        </tr>
    `).join('');

    return `
        <div class="batch-stats-subsection">
            <h4>${escapeHtml(title)}</h4>
            <div class="table-container">
                <table class="data-table">
                    <thead>
                        <tr>${headers.map((header) => `<th>${escapeHtml(header)}</th>`).join('')}</tr>
                    </thead>
                    <tbody>
                        ${bodyRows}
                    </tbody>
                </table>
            </div>
        </div>
    `;
}

function renderCompareTable(title, rows, keyField) {
    if (!Array.isArray(rows) || rows.length === 0) {
        return `
            <div class="batch-stats-subsection">
                <h4>${escapeHtml(title)}</h4>
                ${renderEmptyState('暂无数据', '没有可对比的统计项')}
            </div>
        `;
    }

    const bodyRows = rows.map((row) => {
        const left = row.left || {};
        const right = row.right || {};
        return `
            <tr>
                <td>${escapeHtml(row[keyField] || '-')}</td>
                <td>${formatNumber(left.sample_count)}</td>
                <td>${formatNumber(right.sample_count)}</td>
                <td>${formatDuration(left.avg_duration_ms)}</td>
                <td>${formatDuration(right.avg_duration_ms)}</td>
                <td>${formatDuration(row.delta_duration_ms)}</td>
                <td>${formatRate(row.delta_rate)}</td>
            </tr>
        `;
    }).join('');

    return `
        <div class="batch-stats-subsection">
            <h4>${escapeHtml(title)}</h4>
            <div class="table-container">
                <table class="data-table">
                    <thead>
                        <tr>
                            <th>Key</th>
                            <th>左样本</th>
                            <th>右样本</th>
                            <th>左平均耗时</th>
                            <th>右平均耗时</th>
                            <th>耗时差</th>
                            <th>耗时差率</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${bodyRows}
                    </tbody>
                </table>
            </div>
        </div>
    `;
}

function renderEmptyState(title, description) {
    return `
        <div class="empty-state">
            <div class="empty-state-title">${escapeHtml(title)}</div>
            <div class="empty-state-description">${escapeHtml(description)}</div>
        </div>
    `;
}

function formatNumber(value) {
    if (value === null || value === undefined || Number.isNaN(value)) {
        return '-';
    }
    return String(value);
}

function calcRate(successCount, totalCount) {
    const success = Number(successCount);
    const total = Number(totalCount);
    if (!Number.isFinite(success) || !Number.isFinite(total) || total <= 0) {
        return null;
    }
    return success / total;
}

function formatRate(value) {
    if (typeof value !== 'number' || Number.isNaN(value)) {
        return '-';
    }
    return `${(value * 100).toFixed(1)}%`;
}

function formatDuration(value) {
    if (typeof value !== 'number' || Number.isNaN(value)) {
        return '-';
    }
    return `${Math.round(value)}ms`;
}

function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    const div = document.createElement('div');
    div.textContent = String(text);
    return div.innerHTML;
}
